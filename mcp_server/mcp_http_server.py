#!/usr/bin/env python3
"""Disabled standalone JSON-RPC adapter for the retired HTTP transport."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response
import uvicorn

LEGACY_ERROR = {
    "code": -32004,
    "message": (
        "Legacy MCP HTTP transport disabled; use mcp_server.server with "
        "MCP_TRANSPORT=http on loopback."
    ),
}


def _disabled(request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": LEGACY_ERROR.copy()}


class MCPServer:
    """Metadata-only compatibility adapter with no memory or Agent access."""

    def __init__(self) -> None:
        self.app = FastAPI(
            title="Retired Mem-Agent MCP HTTP Server",
            description="Legacy transport disabled for security",
            version="1.0.0",
        )
        self.setup_routes()

    async def handle_mcp_request(self, data: dict[str, Any]) -> dict[str, Any]:
        """Reject every legacy JSON-RPC method without advertising MCP support."""
        return _disabled(data.get("id"))

    def setup_routes(self) -> None:
        async def handle_request():
            # This transport is retired: reject before consuming hostile input.
            return _disabled(None)

        self.app.post("/")(handle_request)
        self.app.post("/mcp")(handle_request)

        @self.app.get("/")
        async def root():
            return {
                "name": "mem-agent-mcp-http-retired",
                "status": "disabled",
            }

        @self.app.head("/")
        async def root_head():
            return Response(status_code=200)

        @self.app.get("/mcp")
        async def mcp_get():
            return {"methods": [], "status": "legacy_transport_disabled"}

        @self.app.options("/mcp")
        async def mcp_options():
            return Response(status_code=204, headers={"Allow": "GET, POST, OPTIONS"})

        @self.app.get("/health")
        async def health_check():
            return {"status": "disabled", "adapter": "legacy_http_retired"}

        @self.app.head("/health")
        async def health_head():
            return Response(status_code=200)


def create_app() -> FastAPI:
    return MCPServer().app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8081, log_level="info")
