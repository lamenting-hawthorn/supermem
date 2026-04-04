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

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Module under test ─────────────────────────────────────────────────────────
import mcp_server.server as srv
from recall.core.retriever import RetrievalResult


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
        for _ in range(srv.RECALL_RATE_LIMIT - 1):
            assert srv._check_rate("client-b") is True

    def test_exceeds_limit(self):
        for _ in range(srv.RECALL_RATE_LIMIT):
            srv._check_rate("client-c")
        assert srv._check_rate("client-c") is False

    def test_separate_clients_independent(self):
        for _ in range(srv.RECALL_RATE_LIMIT):
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

    def test_tier_labels(self):
        for tier, label in [(1, "FTS5"), (2, "graph"), (3, "vector"), (4, "agent")]:
            result = srv._format_obs_reply([{"content": "x"}], tier=tier)
            assert label in result


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

    def test_read_mlx_model_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "REPO_ROOT", str(tmp_path))
        assert srv._read_mlx_model_name("fallback") == "fallback"

    def test_read_mlx_model_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "REPO_ROOT", str(tmp_path))
        (tmp_path / ".mlx_model_name").write_text("custom-model\n")
        assert srv._read_mlx_model_name("fallback") == "custom-model"


class TestAuthOk:
    """Bearer token auth check."""

    def test_auth_disabled_when_no_key(self, monkeypatch):
        monkeypatch.setattr(srv, "RECALL_API_KEY", "")
        ctx = MagicMock()
        assert srv._auth_ok(ctx) is True

    def test_auth_passes_with_correct_token(self, monkeypatch):
        monkeypatch.setattr(srv, "RECALL_API_KEY", "secret123")
        mock_request = MagicMock()
        mock_request.headers = {"authorization": "Bearer secret123"}
        ctx = MagicMock()
        ctx.get_http_request.return_value = mock_request
        assert srv._auth_ok(ctx) is True

    def test_auth_fails_with_wrong_token(self, monkeypatch):
        monkeypatch.setattr(srv, "RECALL_API_KEY", "secret123")
        mock_request = MagicMock()
        mock_request.headers = {"authorization": "Bearer wrong"}
        ctx = MagicMock()
        ctx.get_http_request.return_value = mock_request
        assert srv._auth_ok(ctx) is False

    def test_auth_passes_stdio_no_http_request(self, monkeypatch):
        monkeypatch.setattr(srv, "RECALL_API_KEY", "secret123")
        ctx = MagicMock()
        ctx.get_http_request.side_effect = Exception("no HTTP context")
        assert srv._auth_ok(ctx) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MCP tool handlers (monkeypatched globals)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRecallHybridTool:
    """recall_hybrid() tool handler."""

    @pytest.mark.asyncio
    async def test_returns_error_when_retriever_none(self, monkeypatch):
        monkeypatch.setattr(srv._ctx, "retriever", None)
        result = await srv.recall_hybrid.fn("test query")
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
        result = await srv.recall_hybrid.fn("alice")
        data = json.loads(result)
        assert data["source_tier"] == 1
        assert data["obs_ids"] == [1, 2]
        assert len(data["observations"]) == 2

    @pytest.mark.asyncio
    async def test_search_exception_returns_error(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.search.side_effect = RuntimeError("db connection lost")
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.recall_hybrid.fn("broken query")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_empty_results(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.search.return_value = RetrievalResult(
            obs_ids=[], source_tier=0, latency_ms=0.1
        )
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.recall_hybrid.fn("zzznomatch")
        data = json.loads(result)
        assert data["obs_ids"] == []
        assert data["observations"] == []


class TestGetTimelineTool:
    """get_timeline() tool handler."""

    @pytest.mark.asyncio
    async def test_returns_error_when_retriever_none(self, monkeypatch):
        monkeypatch.setattr(srv._ctx, "retriever", None)
        result = await srv.get_timeline.fn(obs_id=1)
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
        result = await srv.get_timeline.fn(obs_id=2, window=1)
        data = json.loads(result)
        assert len(data) == 3

    @pytest.mark.asyncio
    async def test_timeline_exception(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.get_timeline.side_effect = RuntimeError("db error")
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.get_timeline.fn(obs_id=99)
        data = json.loads(result)
        assert "error" in data


class TestGetObservationsTool:
    """get_observations() tool handler."""

    @pytest.mark.asyncio
    async def test_returns_error_when_retriever_none(self, monkeypatch):
        monkeypatch.setattr(srv._ctx, "retriever", None)
        result = await srv.get_observations.fn(ids=[1, 2])
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
        result = await srv.get_observations.fn(ids=[1, 2])
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["content"] == "first"

    @pytest.mark.asyncio
    async def test_exception_returns_error(self, monkeypatch):
        mock_ret = AsyncMock()
        mock_ret.get_observations.side_effect = RuntimeError("boom")
        monkeypatch.setattr(srv._ctx, "retriever", mock_ret)
        result = await srv.get_observations.fn(ids=[1])
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
        assert r.json()["status"] == "healthy"

    def test_list_tools(self, client):
        r = client.get("/v1/tools")
        assert r.status_code == 200
        tools = r.json()["tools"]
        assert len(tools) >= 1
        assert tools[0]["name"] == "use_memory_agent"
        assert "inputSchema" in tools[0]

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
        assert "result" in resp
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert "capabilities" in resp["result"]

    @pytest.mark.asyncio
    async def test_tools_list(self, server):
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = await server.handle_mcp_request(req)
        assert resp["id"] == 2
        tools = resp["result"]["tools"]
        assert len(tools) >= 1
        assert tools[0]["name"] == "use_memory_agent"

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
        assert resp["error"]["code"] == -32602

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
        assert resp["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_unknown_method(self, server):
        req = {"jsonrpc": "2.0", "id": 5, "method": "bogus/method", "params": {}}
        resp = await server.handle_mcp_request(req)
        assert "error" in resp
        assert resp["error"]["code"] == -32601


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
        assert r.json()["protocol"] == "MCP over HTTP"

    def test_root_head(self, client):
        r = client.head("/")
        assert r.status_code == 200

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_health_head(self, client):
        r = client.head("/health")
        assert r.status_code == 200

    def test_mcp_get(self, client):
        r = client.get("/mcp")
        assert r.status_code == 200
        data = r.json()
        assert "methods" in data

    def test_mcp_options(self, client):
        r = client.options("/mcp")
        assert r.status_code == 200

    def test_post_initialize(self, client):
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == 1
        assert "result" in data

    def test_post_tools_list(self, client):
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert r.status_code == 200
        tools = r.json()["result"]["tools"]
        assert tools[0]["name"] == "use_memory_agent"

    def test_post_root_mirrors_mcp(self, client):
        """ChatGPT sends JSON-RPC to root — verify it works."""
        r = client.post(
            "/", json={"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}}
        )
        assert r.status_code == 200
        assert "result" in r.json()


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
        assert data["result"]["protocolVersion"] == "2024-11-05"

    def test_message_tools_list(self, client):
        r = client.post(
            "/message",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert r.status_code == 200
        tools = r.json()["result"]["tools"]
        assert tools[0]["name"] == "use_memory_agent"

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
        assert "result" in r.json()


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
