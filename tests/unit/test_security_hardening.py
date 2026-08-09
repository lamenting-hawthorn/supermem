"""Regression tests for the validated 2026-07-31 security findings."""

from __future__ import annotations

from contextlib import contextmanager
import json
import zipfile
import stat
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Context
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from mcp.server.lowlevel.server import request_ctx
from starlette.requests import Request

import mcp_server.server as primary_mcp
from agent.tools import (
    check_if_dir_exists,
    check_if_file_exists,
    get_size,
    go_to_link,
    list_files,
    read_file,
)
import memory_connectors.archive as archive_support
from memory_connectors.archive import ArchiveLimits, open_bounded_zip, safe_extract_zip
from memory_connectors.notion.parser import NotionParser
from memory_connectors.nuclino.parser import NuclinoParser
from supermem.core.retriever import RetrievalResult
from supermem.local_cited_memory import LocalCitedMemory, RetrievalQueryV1
from supermem.storage.database import DatabaseManager
import worker.app as worker_app


class _UnknownCompatibilityContext:
    async def report_progress(self, progress: int, total: int | None = None) -> None:
        return None


class _RuntimeErrorCompatibilityContext:
    def get_http_request(self):
        raise RuntimeError("compatibility object has no HTTP request")


class _ContextClassSpoof:
    def __init__(self) -> None:
        self.fastmcp = primary_mcp.mcp
        self.request_context = SimpleNamespace(request=None)

    @property
    def __class__(self):
        return Context


def _http_request(token: str = "configured-secret") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8081),
            "scheme": "http",
            "query_string": b"",
        }
    )


@contextmanager
def _active_server_context(request: Request | None):
    token = request_ctx.set(SimpleNamespace(request=request))
    try:
        yield Context(primary_mcp.mcp)
    finally:
        request_ctx.reset(token)


class _WorkerRetriever:
    def __init__(self) -> None:
        self.tier_limits: list[int] = []

    async def search(self, query: str, *, tier_limit: int) -> RetrievalResult:
        self.tier_limits.append(tier_limit)
        return RetrievalResult(obs_ids=[1], source_tier=1, latency_ms=0.2)

    async def get_observations(self, obs_ids: list[int]) -> list[dict[str, object]]:
        return [{"id": 1, "content": "public fact", "type": "note"}]


@pytest.fixture(autouse=True)
def _isolate_primary_fastmcp_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """ASGI unit tests exercise protocol state without opening local storage."""
    monkeypatch.setattr(primary_mcp, "_startup", AsyncMock())
    monkeypatch.setattr(primary_mcp, "_shutdown", AsyncMock())


def _tool_call(question: str = "hostile imported instruction") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "use_memory_agent", "arguments": {"question": question}},
    }


def _initialize_call() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "security-regression", "version": "1.0"},
        },
    }


def _session_manager_for(app: object):
    route = next(route for route in app.routes if route.path == "/mcp")
    return route.endpoint.session_manager


def _write_zip(path: Path, member_name: str, content: bytes) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, content)


def _extract_with_limits(
    archive_path: Path, destination: Path, limits: ArchiveLimits
) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        safe_extract_zip(archive, destination, limits=limits)


def _set_eocd_field(path: Path, offset: int, value: int, fmt: str = "<I") -> None:
    payload = bytearray(path.read_bytes())
    eocd_offset = payload.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    struct.pack_into(fmt, payload, eocd_offset + offset, value)
    path.write_bytes(payload)


def test_unknown_compatibility_context_cannot_inherit_stdio_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    assert primary_mcp._auth_ok(_UnknownCompatibilityContext()) is False


def test_runtimeerror_compatibility_context_cannot_inherit_stdio_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    assert primary_mcp._auth_ok(_RuntimeErrorCompatibilityContext()) is False


def test_context_spec_mock_cannot_inherit_stdio_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    compatibility = MagicMock(spec=Context)
    compatibility.fastmcp = primary_mcp.mcp
    compatibility.request_context = SimpleNamespace(request=None)

    assert primary_mcp._auth_ok(compatibility) is False
    assert primary_mcp._retrieval_tier_limit(compatibility, 4) == 3

    spoof = _ContextClassSpoof()
    assert isinstance(spoof, Context)
    assert primary_mcp._auth_ok(spoof) is False
    assert primary_mcp._retrieval_tier_limit(spoof, 4) == 3


def test_context_outside_active_fastmcp_request_cannot_inherit_stdio_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    assert primary_mcp._auth_ok(Context(primary_mcp.mcp)) is False
    assert primary_mcp._retrieval_tier_limit(Context(primary_mcp.mcp), 4) == 3


def test_http_without_configured_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "")
    monkeypatch.setenv("MCP_TRANSPORT", "http")

    with _active_server_context(_http_request()) as ctx:
        assert primary_mcp._auth_ok(ctx) is False


def test_primary_mcp_asgi_requires_bearer_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setattr(primary_mcp._ctx, "retriever", None)
    app = primary_mcp.create_primary_http_app(path="/mcp", json_response=True)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "supermem_hybrid",
            "arguments": {"query": "hostile imported instruction", "tier_limit": 4},
        },
    }

    with TestClient(app) as client:
        missing = client.post("/mcp", headers=headers, json=payload)
        wrong = client.post(
            "/mcp",
            headers={**headers, "Authorization": "Bearer wrong-secret"},
            json=payload,
        )
        allowed = client.post(
            "/mcp",
            headers={
                **headers,
                "Authorization": "Bearer configured-secret",
            },
            json=payload,
        )

    def tool_text(response) -> dict:
        return json.loads(response.json()["result"]["content"][0]["text"])

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert tool_text(allowed)["error"] == "HybridRetriever not initialised"


def test_primary_http_is_authenticated_and_does_not_retain_protocol_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    app = primary_mcp.create_primary_http_app(path="/mcp", json_response=True)
    session_manager = _session_manager_for(app)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    with TestClient(app) as client:
        missing = client.post("/mcp", headers=headers, json=_initialize_call())
        wrong = client.post(
            "/mcp",
            headers={**headers, "Authorization": "Bearer wrong-secret"},
            json=_initialize_call(),
        )
        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert len(session_manager._server_instances) == 0

        assert session_manager.stateless is True
        for _ in range(3):
            allowed = client.post(
                "/mcp",
                headers={**headers, "Authorization": "Bearer configured-secret"},
                json=_initialize_call(),
            )
            assert allowed.status_code == 200
            assert "mcp-session-id" not in allowed.headers
            assert len(session_manager._server_instances) == 0


@pytest.mark.parametrize(
    "host", [None, "", "127.0.0.1", "127.0.0.2", "::1", "localhost"]
)
def test_primary_http_accepts_only_loopback_host_forms(host: str | None) -> None:
    assert primary_mcp._validated_loopback_host(host) in {
        "127.0.0.1",
        "127.0.0.2",
        "::1",
        "localhost",
    }


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.1", "example.test"])
def test_primary_http_rejects_non_loopback_binds(host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        primary_mcp._validated_loopback_host(host)


def test_worker_asgi_requires_key_and_caps_tier_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = _WorkerRetriever()
    monkeypatch.setattr(worker_app, "_retriever", retriever)
    monkeypatch.setattr(worker_app, "SUPERMEM_API_KEY", "configured-secret")
    client = TestClient(worker_app.app)

    assert client.post("/search", json={"query": "fact"}).status_code == 401
    assert (
        client.post(
            "/search",
            headers={"Authorization": "Bearer wrong-secret"},
            json={"query": "fact"},
        ).status_code
        == 401
    )
    allowed = client.post(
        "/search",
        headers={"Authorization": "Bearer configured-secret"},
        json={"query": "fact", "tier_limit": 4},
    )

    assert allowed.status_code == 200
    assert allowed.json()["tier_label"] == "FTS5"
    assert retriever.tier_limits == [3]

    monkeypatch.setattr(worker_app, "SUPERMEM_API_KEY", "")
    assert client.post("/search", json={"query": "fact"}).status_code == 401


@pytest.mark.asyncio
async def test_worker_dashboard_supplies_in_memory_bearer_and_escapes_summaries() -> (
    None
):
    response = await worker_app.index()
    html = response.body.decode()

    assert 'id="api-key" type="password"' in html
    assert "Authorization" in html
    assert "Bearer ${apiKey}" in html
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert "escHtml(String(s.summary))" in html
    assert '<option value="4">' not in html


@pytest.mark.asyncio
async def test_primary_http_startup_rejects_missing_loaded_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "")
    run_async = AsyncMock()
    monkeypatch.setattr(primary_mcp.mcp, "run_async", run_async)

    with pytest.raises(ValueError, match="SUPERMEM_API_KEY"):
        await primary_mcp._main()

    run_async.assert_not_awaited()


def test_make_http_target_delegates_dotenv_validation_to_application() -> None:
    makefile = (Path(__file__).parents[2] / "Makefile").read_text()
    recipe = makefile.split("serve-mcp-http:", 1)[1].split("\n\n", 1)[0]

    assert 'if [ -z "$$SUPERMEM_API_KEY" ]' not in recipe
    assert "python -m mcp_server.server" in recipe


@pytest.mark.asyncio
async def test_worker_observation_listing_excludes_retracted_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = DatabaseManager(tmp_path / "worker.sqlite")
    await database.init()
    active_id = await database.write_observation("active-control")
    retract_id = await database.write_observation("retracted-canary")
    monkeypatch.setattr(worker_app, "_db", database)
    monkeypatch.setattr(worker_app, "_chroma", None)
    monkeypatch.setattr(worker_app, "SUPERMEM_API_KEY", "configured-secret")
    auth = {"Authorization": "Bearer configured-secret"}

    try:
        async with AsyncClient(
            transport=ASGITransport(app=worker_app.app),
            base_url="http://worker.test",
        ) as client:
            assert (await client.get("/observations")).status_code == 401

            before = await client.get("/observations", headers=auth)
            assert before.status_code == 200
            assert {row["id"] for row in before.json()} == {active_id, retract_id}

            retracted = await client.post(
                f"/observations/{retract_id}/retract",
                headers=auth,
                json={"reason": "security regression"},
            )
            assert retracted.status_code == 200

            after = await client.get("/observations", headers=auth)
            assert after.status_code == 200
            assert {row["id"] for row in after.json()} == {active_id}
            assert "retracted-canary" not in after.text
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_remote_primary_tool_caps_retrieval_before_agent_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = AsyncMock()
    retriever.search.return_value = RetrievalResult(
        obs_ids=[1], source_tier=1, latency_ms=0.2
    )
    retriever.get_observations.return_value = [{"id": 1, "content": "public fact"}]
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setattr(primary_mcp._ctx, "retriever", retriever)

    with _active_server_context(_http_request()) as ctx:
        result = await primary_mcp.use_memory_agent.fn("fact", ctx)

    assert "public fact" in result
    retriever.search.assert_awaited_once_with(
        query="fact",
        tier_limit=3,
        min_results=primary_mcp.SUPERMEM_MIN_RESULTS,
    )


@pytest.mark.asyncio
async def test_local_stdio_preserves_configured_retrieval_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = AsyncMock()
    retriever.search.return_value = RetrievalResult(
        obs_ids=[1], source_tier=1, latency_ms=0.2
    )
    retriever.get_observations.return_value = [{"id": 1, "content": "local fact"}]
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setattr(primary_mcp._ctx, "retriever", retriever)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    with _active_server_context(None) as ctx:
        result = await primary_mcp.use_memory_agent.fn("fact", ctx)

    assert "local fact" in result
    retriever.search.assert_awaited_once_with(
        query="fact",
        tier_limit=3,
        min_results=primary_mcp.SUPERMEM_MIN_RESULTS,
    )


@pytest.mark.asyncio
async def test_remote_primary_tool_does_not_fall_through_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = AsyncMock()
    retriever.search.return_value = RetrievalResult(
        obs_ids=[], source_tier=0, latency_ms=0.2
    )
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setattr(primary_mcp._ctx, "retriever", retriever)

    with _active_server_context(_http_request()) as ctx:
        result = await primary_mcp.use_memory_agent.fn("unknown", ctx)

    assert result == "No eligible memory results found."
    retriever.search.assert_awaited_once_with(
        query="unknown",
        tier_limit=3,
        min_results=primary_mcp.SUPERMEM_MIN_RESULTS,
    )


@pytest.mark.asyncio
async def test_remote_explicit_hybrid_tier_is_capped_before_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = AsyncMock()
    retriever.search.return_value = RetrievalResult(
        obs_ids=[], source_tier=0, latency_ms=0.2
    )
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setattr(primary_mcp._ctx, "retriever", retriever)

    with _active_server_context(_http_request()) as ctx:
        await primary_mcp.supermem_hybrid.fn("unknown", 4, ctx)

    retriever.search.assert_awaited_once_with(query="unknown", tier_limit=3)


@pytest.mark.asyncio
async def test_stdio_explicit_hybrid_tier_is_also_capped_before_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = AsyncMock()
    retriever.search.return_value = RetrievalResult(
        obs_ids=[], source_tier=0, latency_ms=0.2
    )
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setattr(primary_mcp._ctx, "retriever", retriever)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    with _active_server_context(None) as ctx:
        await primary_mcp.supermem_hybrid.fn("unknown", 4, ctx)

    retriever.search.assert_awaited_once_with(query="unknown", tier_limit=3)


@pytest.mark.asyncio
async def test_stdio_empty_retrieval_does_not_construct_an_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = AsyncMock()
    retriever.search.return_value = RetrievalResult(
        obs_ids=[], source_tier=0, latency_ms=0.2
    )
    monkeypatch.setattr(primary_mcp, "SUPERMEM_API_KEY", "configured-secret")
    monkeypatch.setattr(primary_mcp._ctx, "retriever", retriever)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    with patch("agent.Agent") as agent_class:
        with _active_server_context(None) as ctx:
            result = await primary_mcp.use_memory_agent.fn("unknown", ctx)

    assert result == "No eligible memory results found."
    agent_class.assert_not_called()
    retriever.search.assert_awaited_once_with(
        query="unknown",
        tier_limit=3,
        min_results=primary_mcp.SUPERMEM_MIN_RESULTS,
    )


def test_legacy_http_wrapper_is_explicitly_disabled() -> None:
    import mcp_server.http_server as legacy_wrapper

    response = TestClient(legacy_wrapper.create_app()).post(
        "/tools/use_memory_agent", json={"question": "hostile"}
    )

    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "legacy_transport_disabled"
    assert not hasattr(legacy_wrapper, "use_memory_agent")


def test_standalone_http_adapter_cannot_reach_agent() -> None:
    import mcp_server.mcp_http_server as legacy_http

    response = TestClient(legacy_http.create_app()).post("/mcp", json=_tool_call())

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32004
    assert not hasattr(legacy_http, "run_memory_agent")
    assert not hasattr(legacy_http, "Agent")


def test_standalone_sse_adapter_cannot_reach_agent() -> None:
    import mcp_server.mcp_sse_server as legacy_sse

    response = TestClient(legacy_sse.create_app()).post("/message", json=_tool_call())

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32004
    assert not hasattr(legacy_sse, "run_memory_agent")
    assert not hasattr(legacy_sse, "Agent")


@pytest.mark.parametrize(
    ("module_name", "path"),
    [
        ("mcp_server.mcp_http_server", "/mcp"),
        ("mcp_server.mcp_sse_server", "/message"),
    ],
)
def test_retired_json_adapters_disable_without_parsing_request_body(
    module_name: str, path: str
) -> None:
    module = __import__(module_name, fromlist=["create_app"])

    with patch(
        "starlette.requests.Request.json",
        side_effect=AssertionError("retired transport must not parse request body"),
    ) as parse_body:
        response = TestClient(module.create_app()).post(
            path,
            content=b"{malformed-json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json()["id"] is None
    assert response.json()["error"]["code"] == -32004
    parse_body.assert_not_called()


def test_legacy_http_wrapper_disables_without_parsing_request_body() -> None:
    import mcp_server.http_server as legacy_wrapper

    with patch(
        "starlette.requests.Request.json",
        side_effect=AssertionError("retired transport must not parse request body"),
    ) as parse_body:
        response = TestClient(legacy_wrapper.create_app()).post(
            "/tools/use_memory_agent",
            content=b"{malformed-json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "legacy_transport_disabled"
    parse_body.assert_not_called()


def test_retracted_and_deleted_bm0_sources_cannot_reach_agent_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    store = LocalCitedMemory(tmp_path / "bm0.sqlite")
    try:
        retracted = vault / "retracted.md"
        retracted.write_text("retracted-lifecycle-canary")
        retracted_revision = store.ingest_file(vault, retracted.relative_to(vault))
        assert store.retract(
            f"{retracted_revision.source_id}:{retracted_revision.revision}"
        )
        assert (
            store.retrieve(
                RetrievalQueryV1(
                    query_id="retracted",
                    query="retracted-lifecycle-canary",
                    correlation_id="security-regression",
                )
            )
            == []
        )

        deleted = vault / "deleted.md"
        deleted.write_text("deleted-lifecycle-canary")
        deleted_revision = store.ingest_file(vault, deleted.relative_to(vault))
        assert store.delete_source(deleted_revision.source_uri)
        assert (
            store.retrieve(
                RetrievalQueryV1(
                    query_id="deleted",
                    query="deleted-lifecycle-canary",
                    correlation_id="security-regression",
                )
            )
            == []
        )

        monkeypatch.chdir(vault)
        denial = read_file(str(retracted))
        for result in (
            denial,
            read_file(str(deleted)),
            go_to_link("[[retracted]]"),
            go_to_link("[[deleted]]"),
            list_files(),
        ):
            assert "unavailable" in result.lower()
            assert "retracted-lifecycle-canary" not in result
            assert "deleted-lifecycle-canary" not in result
        assert check_if_file_exists(str(retracted)) is False
        assert check_if_file_exists(str(deleted)) is False
        assert check_if_dir_exists(str(vault)) is False
        with pytest.raises(PermissionError, match="unavailable"):
            get_size(str(retracted))
    finally:
        store.close()


@pytest.mark.parametrize("parser", [NotionParser(), NuclinoParser()])
def test_connector_parser_rejects_oversized_high_ratio_zip(
    tmp_path: Path, parser: object
) -> None:
    archive = tmp_path / "oversized.zip"
    _write_zip(archive, "Export/blob.bin", b"0" * (2 * 1024 * 1024))

    with pytest.raises(ValueError, match="archive"):
        parser.parse_export(str(archive))


@pytest.mark.parametrize("parser", [NotionParser(), NuclinoParser()])
def test_connector_parser_rejects_oversized_central_directory_before_open(
    tmp_path: Path, parser: object
) -> None:
    archive = tmp_path / "central-directory.zip"
    _write_zip(archive, "Export/Page.md", b"ordinary content")
    _set_eocd_field(archive, 12, 9 * 1024 * 1024)

    with pytest.raises(ValueError, match="central directory"):
        parser.parse_export(str(archive))


@pytest.mark.parametrize(
    ("parser", "result_count"),
    [
        (NotionParser(), lambda result: result.total_pages),
        (NuclinoParser(), lambda result: result.total_items),
    ],
)
def test_connector_parser_accepts_small_legitimate_zip(
    tmp_path: Path, parser: object, result_count
) -> None:
    archive = tmp_path / "export.zip"
    _write_zip(archive, "Export/Page.md", b"# Public page\nOrdinary content")

    result = parser.parse_export(str(archive))

    assert result_count(result) == 1


@pytest.mark.parametrize(
    ("parser", "result_count"),
    [
        (NotionParser(), lambda result: result.total_pages),
        (NuclinoParser(), lambda result: result.total_items),
    ],
)
def test_connector_parser_accepts_legitimate_zip_larger_than_eocd_window(
    tmp_path: Path, parser: object, result_count
) -> None:
    archive = tmp_path / "large-export.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as writer:
        writer.writestr("Export/Page.md", b"# Public page\n" + b"x" * 76_800)

    assert archive.stat().st_size > 65_557
    result = parser.parse_export(str(archive))

    assert result_count(result) == 1


def test_bounded_zip_accepts_maximum_standard_comment(tmp_path: Path) -> None:
    archive = tmp_path / "commented.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("Export/Page.md", b"ordinary content")
        writer.comment = b"x" * 65_535

    with open_bounded_zip(archive) as reader:
        assert reader.comment == b"x" * 65_535


def test_bounded_zip_owns_validated_source_until_close(tmp_path: Path) -> None:
    archive = tmp_path / "owned-source.zip"
    _write_zip(archive, "Export/Page.md", b"ordinary content")

    reader = open_bounded_zip(archive)
    source = reader._bounded_source
    assert source.closed is False
    reader.close()

    assert source.closed is True


def test_bounded_zip_rejects_trailing_bytes(tmp_path: Path) -> None:
    archive = tmp_path / "trailing.zip"
    _write_zip(archive, "Export/Page.md", b"ordinary content")
    with archive.open("ab") as output:
        output.write(b"trailing")

    with pytest.raises(ValueError, match="malformed ZIP end metadata"):
        open_bounded_zip(archive)


def test_archive_member_count_limit(tmp_path: Path) -> None:
    archive = tmp_path / "members.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("one.txt", b"1")
        writer.writestr("two.txt", b"2")

    with pytest.raises(ValueError, match="2 members"):
        _extract_with_limits(
            archive,
            tmp_path / "out",
            ArchiveLimits(max_members=1, max_member_bytes=10, max_total_bytes=10),
        )


def test_bounded_zip_opener_rejects_member_count_before_zipfile_parse(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "many-members.zip"
    _write_zip(archive, "one.txt", b"1")
    _set_eocd_field(archive, 10, 2, "<H")

    with pytest.raises(ValueError, match="2 members"):
        open_bounded_zip(archive, limits=ArchiveLimits(max_members=1))


def test_bounded_zip_opener_rejects_forged_central_directory_before_zipfile_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "forged-central-directory.zip"
    _write_zip(archive, "Export/Page.md", b"ordinary content")
    _set_eocd_field(archive, 12, 9 * 1024 * 1024)
    zipfile_constructor = MagicMock(
        side_effect=AssertionError("ZipFile must not parse forged metadata")
    )
    monkeypatch.setattr(archive_support, "_BoundedZipFile", zipfile_constructor)

    with pytest.raises(ValueError, match="central directory"):
        open_bounded_zip(archive)

    zipfile_constructor.assert_not_called()


def test_bounded_zip_rejects_actual_central_count_before_zipfile_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "forged-member-count.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as writer:
        for index in range(10_001):
            writer.writestr(f"Export/{index}.txt", b"x")
    # Lie in both classic EOCD count fields. The central directory still has
    # 10,001 complete records, which must be bounded before parser allocation.
    _set_eocd_field(archive, 8, 1, "<H")
    _set_eocd_field(archive, 10, 1, "<H")
    zipfile_constructor = MagicMock(
        side_effect=AssertionError("ZipFile must not parse forged member metadata")
    )
    monkeypatch.setattr(archive_support, "_BoundedZipFile", zipfile_constructor)

    with pytest.raises(ValueError, match="10001 members"):
        open_bounded_zip(archive, limits=ArchiveLimits(max_members=10_000))

    zipfile_constructor.assert_not_called()


def test_archive_total_size_limit(tmp_path: Path) -> None:
    archive = tmp_path / "total.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("one.txt", b"12345678")
        writer.writestr("two.txt", b"12345678")

    with pytest.raises(ValueError, match="total bytes"):
        _extract_with_limits(
            archive,
            tmp_path / "out",
            ArchiveLimits(max_member_bytes=8, max_total_bytes=12),
        )


def test_archive_compression_ratio_limit(tmp_path: Path) -> None:
    archive = tmp_path / "ratio.zip"
    _write_zip(archive, "zeros.txt", b"0" * 4096)

    with pytest.raises(ValueError, match="compression ratio"):
        _extract_with_limits(
            archive,
            tmp_path / "out",
            ArchiveLimits(
                max_member_bytes=4096,
                max_total_bytes=4096,
                max_compression_ratio=2,
            ),
        )


@pytest.mark.parametrize("member", ["../escape.txt", "/absolute.txt", "C:/drive.txt"])
def test_archive_unsafe_path_is_rejected(tmp_path: Path, member: str) -> None:
    archive = tmp_path / "path.zip"
    _write_zip(archive, member, b"hostile")

    with pytest.raises(ValueError, match="unsafe path"):
        _extract_with_limits(archive, tmp_path / "out", ArchiveLimits())


@pytest.mark.parametrize(
    "special_mode",
    [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK],
)
def test_archive_special_file_metadata_is_rejected(
    tmp_path: Path, special_mode: int
) -> None:
    archive = tmp_path / "special.zip"
    member = zipfile.ZipInfo("hostile-entry")
    member.create_system = 3
    member.external_attr = (special_mode | 0o644) << 16
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr(member, b"hostile")

    with pytest.raises(ValueError, match="not a regular file"):
        _extract_with_limits(archive, tmp_path / "out", ArchiveLimits())


def test_archive_explicit_regular_file_and_directory_are_allowed(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "typed-valid.zip"
    directory = zipfile.ZipInfo("Export/")
    directory.create_system = 3
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    page = zipfile.ZipInfo("Export/Page.md")
    page.create_system = 3
    page.external_attr = (stat.S_IFREG | 0o644) << 16
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr(directory, b"")
        writer.writestr(page, b"ordinary content")

    _extract_with_limits(archive, tmp_path / "out", ArchiveLimits())

    assert (tmp_path / "out" / "Export" / "Page.md").read_bytes() == b"ordinary content"


def test_bounded_archive_extraction_preserves_legitimate_files(tmp_path: Path) -> None:
    archive = tmp_path / "valid.zip"
    _write_zip(archive, "Export/Page.md", b"ordinary content")
    destination = tmp_path / "out"

    _extract_with_limits(archive, destination, ArchiveLimits())

    assert (destination / "Export" / "Page.md").read_bytes() == b"ordinary content"
