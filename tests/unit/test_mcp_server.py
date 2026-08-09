"""Unit tests for mcp_server/ — pure helpers, tool handlers, and HTTP endpoints.

Strategy:
  - Pure helpers (_check_rate, _format_obs_reply, etc.) tested directly.
  - MCP tool handlers tested by monkeypatching the module-level globals
    (_retriever, _db, etc.) so no live MCP runtime is needed.
  - HTTP endpoints tested via FastAPI TestClient.
  - Error paths covered: retriever=None, exceptions, auth rejection.

TODO(arch): Long-term, extract global state into a ServerContext dataclass
  and pass it via dependency injection. That removes the need for monkeypatching
  entirely. See: https://fastmcp.readthedocs.io/en/latest/patterns/testing/
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Context
from mcp.server.lowlevel.server import request_ctx
from starlette.requests import Request

# ── Module under test ─────────────────────────────────────────────────────────
import mcp_server.server as srv
from supermem.core.retriever import RetrievalResult


def _stdio_context() -> object:
    return srv._TRUSTED_LOCAL_STDIO


def _http_request(auth_header: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [(b"authorization", auth_header.encode())],
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
        yield Context(srv.mcp)
    finally:
        request_ctx.reset(token)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Pure helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckRate:
    """Token-bucket rate limiter."""

    def setup_method(self):
        srv._rate_buckets.clear()

    def test_first_request_passes(self):
        assert srv._check_rate("client-a") is True

    def test_within_limit(self):
        for _ in range(srv.SUPERMEM_RATE_LIMIT - 1):
            assert srv._check_rate("client-b") is True

    def test_exceeds_limit(self):
        for _ in range(srv.SUPERMEM_RATE_LIMIT):
            srv._check_rate("client-c")
        assert srv._check_rate("client-c") is False

    def test_separate_clients_independent(self):
        for _ in range(srv.SUPERMEM_RATE_LIMIT):
            srv._check_rate("client-d")
        # Different client should still pass
        assert srv._check_rate("client-e") is True


class TestFormatObsReply:
    """Observation formatting for human-readable replies."""

    def test_basic_format(self):
        obs = [{"content": "Alice works at Acme"}]
        result = srv._format_obs_reply(obs, tier=1)
        assert "[from FTS5 memory]" in result
        assert "Alice works at Acme" in result

    def test_multiple_observations(self):
        obs = [{"content": f"obs {i}"} for i in range(3)]
        result = srv._format_obs_reply(obs, tier=2)
        assert "[from graph memory]" in result
        assert "obs 0" in result
        assert "obs 2" in result

    def test_caps_at_five(self):
        obs = [{"content": f"obs {i}"} for i in range(10)]
        result = srv._format_obs_reply(obs, tier=1)
        assert "obs 4" in result
        assert "obs 5" not in result

    def test_empty_content_skipped(self):
        obs = [{"content": ""}, {"content": "real content"}]
        result = srv._format_obs_reply(obs, tier=3)
        assert "real content" in result

    def test_supported_tier_labels(self):
        for tier, label in [(1, "FTS5"), (2, "graph"), (3, "vector")]:
            result = srv._format_obs_reply([{"content": "x"}], tier=tier)
            assert label in result

    def test_unavailable_agent_tier_is_not_presented_as_a_memory_source(self):
        result = srv._format_obs_reply([{"content": "x"}], tier=4)
        assert "agent" not in result
        assert "?" in result


class TestReadHelpers:
    """File-reading helpers with missing/malformed files."""

    def test_read_filters_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "FILTERS_PATH", str(tmp_path / "nope"))
        assert srv._read_filters() == ""

    def test_read_filters_with_content(self, tmp_path, monkeypatch):
        f = tmp_path / ".filters"
        f.write_text("type:meeting")
        monkeypatch.setattr(srv, "FILTERS_PATH", str(f))
        assert srv._read_filters() == "type:meeting"


class TestAuthOk:
    """Bearer token auth check."""

    def test_local_stdio_auth_disabled_when_no_key(self, monkeypatch):
        monkeypatch.setattr(srv, "SUPERMEM_API_KEY", "")
        monkeypatch.setenv("MCP_TRANSPORT", "stdio")
        assert srv._auth_ok(_stdio_context()) is True

    def test_auth_passes_with_correct_token(self, monkeypatch):
        monkeypatch.setattr(srv, "SUPERMEM_API_KEY", "secret123")
        with _active_server_context(_http_request("Bearer secret123")) as ctx:
            assert srv._auth_ok(ctx) is True

    def test_auth_fails_with_wrong_token(self, monkeypatch):
        monkeypatch.setattr(srv, "SUPERMEM_API_KEY", "secret123")
        with _active_server_context(_http_request("Bearer wrong")) as ctx:
            assert srv._auth_ok(ctx) is False

    def test_auth_passes_explicit_stdio_no_http_request(self, monkeypatch):
        monkeypatch.setattr(srv, "SUPERMEM_API_KEY", "secret123")
        monkeypatch.setenv("MCP_TRANSPORT", "stdio")
        assert srv._auth_ok(_stdio_context()) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MCP tool handlers (monkeypatched globals)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSupermemHybridTool:
    """supermem_hybrid() tool handler."""

    @pytest.mark.asyncio
    async def test_returns_error_when_retriever_none(self, monkeypatch):
        monkeypatch.setattr(srv._ctx, "retriever", None)
        result = await srv.supermem_hybrid.fn("test query", ctx=_stdio_context())
        data = json.loads(result)
        assert "error" in data
        assert data["obs_ids"] == []

    @pytest.mark.asyncio
    async def test_successful_search(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.search.return_value = RetrievalResult(
            obs_ids=[1, 2], source_tier=1, latency_ms=0.5
        )
        mock_ret.get_observations.return_value = [
            {"id": 1, "content": "Alice works at Acme"},
            {"id": 2, "content": "Bob is her manager"},
        ]
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.supermem_hybrid.fn("alice", ctx=_stdio_context())
        data = json.loads(result)
        assert data["source_tier"] == 1
        assert data["obs_ids"] == [1, 2]
        assert len(data["observations"]) == 2

    @pytest.mark.asyncio
    async def test_search_exception_returns_error(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.search.side_effect = RuntimeError("db connection lost")
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.supermem_hybrid.fn("broken query", ctx=_stdio_context())
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_empty_results(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.search.return_value = RetrievalResult(
            obs_ids=[], source_tier=0, latency_ms=0.1
        )
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.supermem_hybrid.fn("zzznomatch", ctx=_stdio_context())
        data = json.loads(result)
        assert data["obs_ids"] == []
        assert data["observations"] == []


class TestRetractObservationTool:
    """retract_observation() tool handler."""

    @pytest.mark.asyncio
    async def test_retract_success(self, monkeypatch):
        mock_db = AsyncMock()
        mock_db.retract_observation.return_value = True
        mock_chroma = AsyncMock()
        monkeypatch.setattr(srv._ctx, "db", mock_db)
        monkeypatch.setattr(srv._ctx, "chroma", mock_chroma)
        result = await srv.retract_observation.fn(
            obs_id=42, reason="stale", ctx=_stdio_context()
        )
        data = json.loads(result)
        assert data["retracted"] is True
        mock_db.retract_observation.assert_awaited_once_with(42, reason="stale")
        mock_chroma.delete_obs.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_retract_no_db(self, monkeypatch):
        monkeypatch.setattr(srv._ctx, "db", None)
        result = await srv.retract_observation.fn(obs_id=42, ctx=_stdio_context())
        data = json.loads(result)
        assert data["retracted"] is False
        assert "error" in data


class TestGetTimelineTool:
    """get_timeline() tool handler."""

    @pytest.mark.asyncio
    async def test_returns_error_when_retriever_none(self, monkeypatch):
        monkeypatch.setattr(srv._ctx, "retriever", None)
        result = await srv.get_timeline.fn(obs_id=1, ctx=_stdio_context())
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_successful_timeline(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.get_timeline.return_value = [
            {"id": 1, "content": "before", "created_at": 100.0},
            {"id": 2, "content": "anchor", "created_at": 101.0},
            {"id": 3, "content": "after", "created_at": 102.0},
        ]
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.get_timeline.fn(obs_id=2, window=1, ctx=_stdio_context())
        data = json.loads(result)
        assert len(data) == 3

    @pytest.mark.asyncio
    async def test_timeline_exception(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.get_timeline.side_effect = RuntimeError("db error")
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.get_timeline.fn(obs_id=99, ctx=_stdio_context())
        data = json.loads(result)
        assert "error" in data


class TestGetObservationsTool:
    """get_observations() tool handler."""

    @pytest.mark.asyncio
    async def test_returns_error_when_retriever_none(self, monkeypatch):
        monkeypatch.setattr(srv._ctx, "retriever", None)
        result = await srv.get_observations.fn(ids=[1, 2], ctx=_stdio_context())
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_successful_fetch(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.get_observations.return_value = [
            {"id": 1, "content": "first"},
            {"id": 2, "content": "second"},
        ]
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.get_observations.fn(ids=[1, 2], ctx=_stdio_context())
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["content"] == "first"

    @pytest.mark.asyncio
    async def test_exception_returns_error(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.get_observations.side_effect = RuntimeError("boom")
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.get_observations.fn(ids=[1], ctx=_stdio_context())
        data = json.loads(result)
        assert "error" in data


# ═══════════════════════════════════════════════════════════════════════════════
# 3. HTTP endpoint tests (FastAPI TestClient)
# ═══════════════════════════════════════════════════════════════════════════════


class TestHTTPServer:
    """http_server.py — REST wrapper endpoints."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from mcp_server.http_server import create_app

        app = create_app()
        return TestClient(app)

    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert "name" in data
        assert data["name"] == "mem-agent-mcp-server"

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "disabled"

    def test_list_tools(self, client):
        r = client.get("/v1/tools")
        assert r.status_code == 200
        assert r.json()["tools"] == []
        assert r.json()["status"] == "legacy_transport_disabled"

    def test_list_tools_legacy(self, client):
        r = client.get("/tools")
        assert r.status_code == 200
        assert "tools" in r.json()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. JSON-RPC MCP handler tests (mcp_http_server.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestMCPJsonRPC:
    """mcp_http_server.py — JSON-RPC protocol handler."""

    @pytest.fixture
    def server(self):
        from mcp_server.mcp_http_server import MCPServer

        return MCPServer()

    @pytest.mark.asyncio
    async def test_initialize(self, server):
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = await server.handle_mcp_request(req)
        assert resp["id"] == 1
        assert resp["error"]["code"] == -32004

    @pytest.mark.asyncio
    async def test_tools_list(self, server):
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = await server.handle_mcp_request(req)
        assert resp["id"] == 2
        assert resp["error"]["code"] == -32004

    @pytest.mark.asyncio
    async def test_tools_call_missing_question(self, server):
        req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "use_memory_agent", "arguments": {}},
        }
        resp = await server.handle_mcp_request(req)
        assert "error" in resp
        assert resp["error"]["code"] == -32004

    @pytest.mark.asyncio
    async def test_unknown_tool(self, server):
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        }
        resp = await server.handle_mcp_request(req)
        assert "error" in resp
        assert resp["error"]["code"] == -32004

    @pytest.mark.asyncio
    async def test_unknown_method(self, server):
        req = {"jsonrpc": "2.0", "id": 5, "method": "bogus/method", "params": {}}
        resp = await server.handle_mcp_request(req)
        assert "error" in resp
        assert resp["error"]["code"] == -32004


# ═══════════════════════════════════════════════════════════════════════════════
# 5. MCP HTTP server endpoints (mcp_http_server.py TestClient)
# ═══════════════════════════════════════════════════════════════════════════════


class TestMCPHTTPEndpoints:
    """Test actual HTTP routes in mcp_http_server.py."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from mcp_server.mcp_http_server import create_app

        app = create_app()
        return TestClient(app)

    def test_root_get(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["status"] == "disabled"
        assert "protocol" not in r.json()

    def test_root_head(self, client):
        r = client.head("/")
        assert r.status_code == 200

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "disabled"

    def test_health_head(self, client):
        r = client.head("/health")
        assert r.status_code == 200

    def test_mcp_get(self, client):
        r = client.get("/mcp")
        assert r.status_code == 200
        data = r.json()
        assert data["methods"] == []

    def test_mcp_options(self, client):
        r = client.options("/mcp")
        assert r.status_code == 204

    def test_post_initialize(self, client):
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["id"] is None
        assert data["error"]["code"] == -32004

    def test_post_tools_list(self, client):
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert r.status_code == 200
        assert r.json()["error"]["code"] == -32004

    def test_post_root_mirrors_mcp(self, client):
        """ChatGPT sends JSON-RPC to root — verify it works."""
        r = client.post(
            "/", json={"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}}
        )
        assert r.status_code == 200
        assert r.json()["error"]["code"] == -32004


# ═══════════════════════════════════════════════════════════════════════════════
# 6. SSE server POST /message handler (mcp_sse_server.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSSEServerMessage:
    """mcp_sse_server.py — POST /message JSON-RPC handler."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from mcp_server.mcp_sse_server import create_app

        app = create_app()
        return TestClient(app)

    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "mem-agent-mcp-sse" in r.json()["name"]

    def test_root_head(self, client):
        r = client.head("/")
        assert r.status_code == 200

    def test_message_initialize(self, client):
        r = client.post(
            "/message",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["error"]["code"] == -32004

    def test_message_tools_list(self, client):
        r = client.post(
            "/message",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert r.status_code == 200
        assert r.json()["error"]["code"] == -32004

    def test_message_unknown_method(self, client):
        r = client.post(
            "/message",
            json={"jsonrpc": "2.0", "id": 99, "method": "bogus", "params": {}},
        )
        assert r.status_code == 200
        assert "error" in r.json()

    def test_sse_post_mirrors_message(self, client):
        """POST /sse should also handle JSON-RPC (some clients POST here)."""
        r = client.post(
            "/sse",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
        assert r.status_code == 200
        assert r.json()["error"]["code"] == -32004

    def test_sse_stream_endpoint_is_retired(self, client):
        r = client.get("/sse")

        assert r.status_code == 410
        assert r.json()["detail"]["code"] == -32004


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Settings module (trivial import coverage)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSettings:
    def test_constants_exist(self):
        from mcp_server.settings import (
            MEMORY_AGENT_NAME,
            MLX_4BIT_MEMORY_AGENT_NAME,
            MLX_8BIT_MEMORY_AGENT_NAME,
        )

        assert isinstance(MEMORY_AGENT_NAME, str)
        assert isinstance(MLX_4BIT_MEMORY_AGENT_NAME, str)
        assert isinstance(MLX_8BIT_MEMORY_AGENT_NAME, str)


class TestInsightToolValidation:
    @pytest.mark.asyncio
    async def test_list_open_tasks_rejects_invalid_bounds_before_db(self, monkeypatch):
        mock_db = AsyncMock()
        monkeypatch.setattr(srv._ctx, "db", mock_db)
        result = await srv.list_open_tasks.fn(days=0, limit=20, ctx=_stdio_context())
        data = json.loads(result)
        assert "error" in data
        mock_db.get_recent_observations_by_age.assert_not_called()

    @pytest.mark.asyncio
    async def test_suggest_followups_rejects_excessive_limit(self, monkeypatch):
        mock_db = AsyncMock()
        monkeypatch.setattr(srv._ctx, "db", mock_db)
        result = await srv.suggest_followups.fn(days=14, limit=51, ctx=_stdio_context())
        data = json.loads(result)
        assert "error" in data
        mock_db.get_recent_observations_by_age.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_day_summaries_rejects_excessive_days(self, monkeypatch):
        mock_db = AsyncMock()
        monkeypatch.setattr(srv._ctx, "db", mock_db)
        result = await srv.list_day_summaries.fn(days=32, ctx=_stdio_context())
        data = json.loads(result)
        assert "error" in data
        mock_db.get_recent_observations_by_age.assert_not_called()


def test_auth_context_error_denies_when_http_transport(monkeypatch):
    monkeypatch.setattr(srv, "SUPERMEM_API_KEY", "secret")
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    ctx = Context(srv.mcp)

    assert srv._auth_ok(ctx) is False


def test_rate_limit_bucket_is_shared_across_tools(monkeypatch):
    srv._rate_buckets.clear()
    monkeypatch.setattr(srv, "SUPERMEM_API_KEY", "")
    monkeypatch.setattr(srv, "SUPERMEM_RATE_LIMIT", 1)

    assert srv._guard_tool(_stdio_context(), "supermem_hybrid") is None
    denial = srv._guard_tool(_stdio_context(), "get_observations")

    assert denial is not None
    assert "rate_limit_error" in denial
