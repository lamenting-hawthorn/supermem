"""Recall v2 CLI entry point.

Commands:
  recall serve            Start the MCP server (stdio transport)
  recall serve --worker   Start MCP server + Worker HTTP API on :37777
  recall chat             Interactive terminal REPL
  recall backup           Archive vault + SQLite to timestamped .tar.gz
  recall restore <file>   Restore vault + SQLite from archive
  recall connect <type> <source>  Run a memory connector

Usage examples:
  recall serve
  recall serve --worker
  recall chat
  recall backup
  recall backup --output /tmp/my_backup.tar.gz
  recall restore /tmp/recall_backup_20260101_120000.tar.gz
  recall connect chatgpt ~/Downloads/chatgpt-export.zip
  recall connect github owner/repo --token ghp_xxx
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

# Ensure project root is on path when running as `python -m recall`
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the MCP server. Optionally also start the Worker HTTP service."""
    if args.worker:
        print("Starting Recall MCP server + Worker HTTP API…", flush=True)
        # Start worker in a subprocess alongside the MCP server
        worker_proc = subprocess.Popen(
            [sys.executable, "-m", "worker.app"],
            cwd=str(_ROOT),
        )
        print(f"Worker started (pid {worker_proc.pid}), listening on :{_worker_port()}", flush=True)
        try:
            # Run MCP server in foreground
            os.execv(sys.executable, [sys.executable, str(_ROOT / "mcp_server" / "server.py")])
        except KeyboardInterrupt:
            worker_proc.terminate()
    else:
        os.execv(sys.executable, [sys.executable, str(_ROOT / "mcp_server" / "server.py")])


def _worker_port() -> int:
    try:
        from recall.config import RECALL_WORKER_PORT
        return RECALL_WORKER_PORT
    except Exception:
        return 37777


def cmd_chat(_args: argparse.Namespace) -> None:
    """Start the interactive terminal REPL."""
    os.execv(sys.executable, [sys.executable, str(_ROOT / "chat_cli.py")])


def cmd_backup(args: argparse.Namespace) -> None:
    """Archive the markdown vault + SQLite database to a timestamped tar.gz."""
    from recall.config import RECALL_DB_PATH, RECALL_VAULT_PATH

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output = Path(args.output) if args.output else Path.cwd() / f"recall_backup_{timestamp}.tar.gz"

    print(f"Creating backup: {output}", flush=True)
    with tarfile.open(output, "w:gz") as tar:
        if RECALL_VAULT_PATH.exists():
            for md in RECALL_VAULT_PATH.rglob("*.md"):
                tar.add(str(md), arcname=f"vault/{md.relative_to(RECALL_VAULT_PATH)}")
                print(f"  + {md.relative_to(RECALL_VAULT_PATH)}", flush=True)
        if RECALL_DB_PATH.exists():
            tar.add(str(RECALL_DB_PATH), arcname="recall.db")
            print(f"  + recall.db ({RECALL_DB_PATH.stat().st_size // 1024} KB)", flush=True)

    print(f"Backup complete: {output} ({output.stat().st_size // 1024} KB)", flush=True)


def cmd_restore(args: argparse.Namespace) -> None:
    """Restore vault + SQLite from a backup archive."""
    from recall.config import RECALL_DB_PATH, RECALL_VAULT_PATH

    archive = Path(args.archive)
    if not archive.exists():
        print(f"Error: archive not found: {archive}", file=sys.stderr)
        sys.exit(1)

    print(f"Restoring from {archive}…", flush=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith("vault/"):
                rel = member.name[len("vault/"):]
                dest = RECALL_VAULT_PATH / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(member)
                if f:
                    dest.write_bytes(f.read())
                    print(f"  restored: {rel}", flush=True)
            elif member.name == "recall.db":
                RECALL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(member)
                if f:
                    RECALL_DB_PATH.write_bytes(f.read())
                    print("  restored: recall.db", flush=True)

    print("Restore complete.", flush=True)


def cmd_connect(args: argparse.Namespace) -> None:
    """Run a memory connector to import data into the vault."""
    connector_type = args.connector
    source = args.source
    max_items = args.max_items
    token = args.token

    # Dynamic import of connector
    connector_map = {
        "chatgpt": "memory_connectors.chatgpt_history.connector.ChatGPTHistoryConnector",
        "notion": "memory_connectors.notion.connector.NotionConnector",
        "nuclino": "memory_connectors.nuclino.connector.NuclinoConnector",
        "github": "memory_connectors.github.connector.GitHubConnector",
        "google-docs": "memory_connectors.google_docs.connector.GoogleDocsConnector",
    }

    fqcn = connector_map.get(connector_type)
    if not fqcn:
        print(f"Error: unknown connector '{connector_type}'.", file=sys.stderr)
        print(f"Available: {', '.join(connector_map)}", file=sys.stderr)
        sys.exit(1)

    module_path, cls_name = fqcn.rsplit(".", 1)
    try:
        import importlib
        module = importlib.import_module(module_path)
        cls = getattr(module, cls_name)
    except (ImportError, AttributeError) as exc:
        print(f"Error loading connector '{connector_type}': {exc}", file=sys.stderr)
        sys.exit(1)

    from recall.config import RECALL_VAULT_PATH
    kwargs: dict = {}
    if token:
        kwargs["token"] = token

    connector = cls(output_path=str(RECALL_VAULT_PATH), **kwargs)
    print(f"Running {connector_type} connector → {RECALL_VAULT_PATH}", flush=True)
    connector.run(source=source, max_items=max_items)
    print("Import complete.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="recall",
        description="Recall v2 — persistent AI memory without RAG",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # serve
    p_serve = sub.add_parser("serve", help="Start the MCP server")
    p_serve.add_argument("--worker", action="store_true", help="Also start Worker HTTP API on :37777")

    # chat
    sub.add_parser("chat", help="Interactive terminal REPL")

    # backup
    p_backup = sub.add_parser("backup", help="Archive vault + database")
    p_backup.add_argument("--output", "-o", help="Output path (default: cwd/recall_backup_<ts>.tar.gz)")

    # restore
    p_restore = sub.add_parser("restore", help="Restore from backup archive")
    p_restore.add_argument("archive", help="Path to backup .tar.gz file")

    # connect
    p_connect = sub.add_parser("connect", help="Import data via a memory connector")
    p_connect.add_argument("connector", help="Connector type: chatgpt|notion|nuclino|github|google-docs")
    p_connect.add_argument("source", help="Source path, repo name, or folder ID")
    p_connect.add_argument("--max-items", "-n", type=int, default=None, help="Limit items to import")
    p_connect.add_argument("--token", "-t", default=None, help="API token (GitHub, Google Docs)")

    args = parser.parse_args()
    dispatch = {
        "serve": cmd_serve,
        "chat": cmd_chat,
        "backup": cmd_backup,
        "restore": cmd_restore,
        "connect": cmd_connect,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
