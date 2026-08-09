#!/usr/bin/env python3
"""Disabled compatibility wrapper for the retired legacy HTTP transport."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
import uvicorn

DISABLED_DETAIL = {
    "code": "legacy_transport_disabled",
    "message": (
        "This legacy HTTP wrapper is disabled. Use mcp_server.server with "
        "MCP_TRANSPORT=http on loopback; remote production remains unsupported."
    ),
}


class MCPHTTPWrapper:
    """Non-executing compatibility surface retained for migration errors."""

    def __init__(self) -> None:
        self.app = FastAPI(
            title="Retired Mem-Agent MCP HTTP Wrapper",
            description="Legacy transport disabled for security",
            version="1.0.0",
        )
        self.setup_routes()

    def setup_routes(self) -> None:
        @self.app.get("/")
        async def root():
            return {
                "name": "mem-agent-mcp-server",
                "status": "disabled",
                "replacement": "python -m mcp_server.server (MCP_TRANSPORT=http)",
            }

        @self.app.get("/v1/tools")
        async def list_tools():
            return {"tools": [], "status": "legacy_transport_disabled"}

        @self.app.get("/tools")
        async def list_tools_legacy():
            return await list_tools()

        async def disabled_tool_call():
            raise HTTPException(status_code=410, detail=DISABLED_DETAIL)

        self.app.post("/v1/tools/use_memory_agent")(disabled_tool_call)
        self.app.post("/tools/use_memory_agent")(disabled_tool_call)

        @self.app.get("/health")
        async def health_check():
            return {"status": "disabled", "server": "mem-agent-mcp-legacy-http"}


def create_app() -> FastAPI:
    return MCPHTTPWrapper().app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="info")
