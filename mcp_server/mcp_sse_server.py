#!/usr/bin/env python3
"""Disabled standalone adapter for the retired MCP SSE transport."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
import uvicorn

LEGACY_ERROR = {
    "code": -32004,
    "message": "Legacy MCP SSE transport disabled; use the primary loopback transport.",
}


def _disabled(request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": LEGACY_ERROR.copy()}


class MCPSSEServer:
    """Metadata-only compatibility adapter with no memory or Agent access."""

    def __init__(self) -> None:
        self.app = FastAPI(
            title="Retired Mem-Agent MCP SSE Server",
            description="Legacy transport disabled for security",
            version="1.0.0",
        )
        self.setup_routes()

    def setup_routes(self) -> None:
        @self.app.get("/")
        async def root():
            return {
                "name": "mem-agent-mcp-sse-retired",
                "status": "disabled",
            }

        @self.app.head("/")
        async def root_head():
            return Response(status_code=200)

        @self.app.get("/sse")
        async def sse_endpoint():
            raise HTTPException(status_code=410, detail=LEGACY_ERROR)

        async def handle_message():
            # This transport is retired: reject before consuming hostile input.
            return _disabled(None)

        self.app.post("/message")(handle_message)
        self.app.post("/sse")(handle_message)


def create_app() -> FastAPI:
    return MCPSSEServer().app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8082, log_level="info")
