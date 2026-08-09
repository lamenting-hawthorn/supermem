"""Local process integration coverage for the shipped primary MCP entrypoint."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
_TOKEN = "process-regression-token"
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "primary-process-regression", "version": "1.0"},
    },
}


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _child_env(
    tmp_path: Path, transport: str, port: int | None = None
) -> dict[str, str]:
    """Create a minimal, fixture-only child environment with no inherited keys."""
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    home.mkdir()
    vault.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "SUPERMEM_DB_PATH": str(tmp_path / "supermem.db"),
        "SUPERMEM_VAULT_PATH": str(vault),
        "SUPERMEM_KUZU_PATH": str(tmp_path / "kuzu"),
        "SUPERMEM_CHROMA_PATH": str(tmp_path / "chroma"),
        "SUPERMEM_VECTOR": "false",
        "SUPERMEM_LLM_PROVIDER": "disabled",
        "OPENROUTER_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "SUPERMEM_API_KEY": _TOKEN,
        "MCP_TRANSPORT": transport,
        "MCP_HOST": "127.0.0.1",
        "MCP_PATH": "/mcp",
        "NO_COLOR": "1",
    }
    if port is not None:
        env["MCP_PORT"] = str(port)
    return env


def _start_server(
    tmp_path: Path, transport: str, *, port: int | None = None
) -> tuple[subprocess.Popen[str], Path, Path]:
    log_path = tmp_path / "server.log"
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server"],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=log_handle,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=_child_env(tmp_path, transport, port),
    )
    # The handle is intentionally closed in the parent; the child owns its fd.
    log_handle.close()
    return process, log_path, tmp_path / "supermem.db"


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    if process.stdout is not None and not process.stdout.closed:
        process.stdout.close()


def _read_stdio_response(
    process: subprocess.Popen[str], request_id: int, timeout: float = 10.0
) -> dict[str, Any]:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        if not readable:
            continue
        line = process.stdout.readline()
        if not line:
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == request_id:
            return payload
    raise AssertionError(f"No MCP stdio response for request id {request_id}.")


def _assert_session_closed(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT ended_at FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] is not None


def _http_post(
    port: int, payload: dict[str, Any], *, token: str | None
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=1)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Connection": "close",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        connection.request("POST", "/mcp", body=json.dumps(payload), headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def _wait_for_http_auth_boundary(port: int) -> None:
    deadline = time.monotonic() + 12
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, _, _ = _http_post(port, _INITIALIZE, token=None)
            if status == 401:
                return
        except OSError as exc:
            last_error = exc
        time.sleep(0.05)
    raise AssertionError(
        f"Primary HTTP process did not reach auth boundary: {last_error}"
    )


def test_primary_stdio_process_initializes_lists_tools_and_closes_on_eof(
    tmp_path: Path,
) -> None:
    process, log_path, db_path = _start_server(tmp_path, "stdio")
    try:
        assert process.stdin is not None
        process.stdin.write(json.dumps(_INITIALIZE) + "\n")
        process.stdin.flush()
        initialized = _read_stdio_response(process, 1)
        assert initialized["result"]["serverInfo"]["name"] == "supermem-server"

        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        listed = _read_stdio_response(process, 2)
        assert any(
            tool["name"] == "supermem_hybrid" for tool in listed["result"]["tools"]
        )

        process.stdin.close()
        assert process.wait(timeout=12) == 0
        output = log_path.read_text(encoding="utf-8")
        assert "supermem_server_ready" in output
        assert "supermem_server_stopped" in output
        assert "Already running asyncio" not in output
        assert process.poll() is not None
        _assert_session_closed(db_path)
    finally:
        _stop_process(process)


def test_primary_http_process_requires_bearer_and_gracefully_handles_sigterm(
    tmp_path: Path,
) -> None:
    port = _free_loopback_port()
    process, log_path, db_path = _start_server(tmp_path, "http", port=port)
    try:
        _wait_for_http_auth_boundary(port)
        missing_status, _, _ = _http_post(port, _INITIALIZE, token=None)
        assert missing_status == 401

        for _ in range(3):
            status, headers, _ = _http_post(port, _INITIALIZE, token=_TOKEN)
            assert status == 200
            assert "mcp-session-id" not in {
                name.lower(): value for name, value in headers.items()
            }

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 128 + signal.SIGTERM
        output = log_path.read_text(encoding="utf-8")
        assert "supermem_server_ready" in output
        assert "supermem_server_stopped" in output
        assert "Already running asyncio" not in output
        assert process.poll() is not None
        _assert_session_closed(db_path)
    finally:
        _stop_process(process)
