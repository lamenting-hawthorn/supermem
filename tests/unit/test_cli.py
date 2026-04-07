"""Unit tests for supermem CLI (__main__.py) — argument parsing and command dispatch."""

from __future__ import annotations

import sys
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse(args: list[str]):
    """Parse CLI args without executing any command."""
    import argparse
    from supermem.__main__ import main

    # Capture the parsed namespace by patching dispatch
    captured = {}

    def fake_dispatch(ns):
        captured["ns"] = ns

    with (
        patch("supermem.__main__.cmd_serve", fake_dispatch),
        patch("supermem.__main__.cmd_chat", fake_dispatch),
        patch("supermem.__main__.cmd_backup", fake_dispatch),
        patch("supermem.__main__.cmd_restore", fake_dispatch),
        patch("supermem.__main__.cmd_connect", fake_dispatch),
        patch("sys.argv", ["supermem"] + args),
    ):
        main()

    return captured.get("ns")


# ── Argument parsing ──────────────────────────────────────────────────────────


def test_serve_no_worker():
    ns = _parse(["serve"])
    assert ns.command == "serve"
    assert ns.worker is False


def test_serve_with_worker():
    ns = _parse(["serve", "--worker"])
    assert ns.worker is True


def test_backup_default_output():
    ns = _parse(["backup"])
    assert ns.command == "backup"
    assert ns.output is None


def test_backup_custom_output():
    ns = _parse(["backup", "--output", "/tmp/my.tar.gz"])
    assert ns.output == "/tmp/my.tar.gz"


def test_restore_archive_arg():
    ns = _parse(["restore", "/tmp/backup.tar.gz"])
    assert ns.archive == "/tmp/backup.tar.gz"


def test_connect_chatgpt():
    ns = _parse(["connect", "chatgpt", "/tmp/export.zip"])
    assert ns.connector == "chatgpt"
    assert ns.source == "/tmp/export.zip"
    assert ns.token is None
    assert ns.max_items is None


def test_connect_github_with_token():
    ns = _parse(["connect", "github", "owner/repo", "--token", "ghp_abc123"])
    assert ns.connector == "github"
    assert ns.token == "ghp_abc123"


def test_connect_max_items():
    ns = _parse(["connect", "chatgpt", "/tmp/x.zip", "--max-items", "50"])
    assert ns.max_items == 50


def test_no_subcommand_exits():
    with patch("sys.argv", ["supermem"]):
        with pytest.raises(SystemExit):
            from supermem.__main__ import main

            main()


# ── cmd_backup ────────────────────────────────────────────────────────────────


def test_cmd_backup_creates_archive(tmp_path: Path):
    import argparse
    import supermem.config as cfg
    from supermem.__main__ import cmd_backup

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Test note")
    db_file = tmp_path / "supermem.db"
    db_file.write_bytes(b"SQLite fake data")
    output = tmp_path / "backup.tar.gz"

    ns = argparse.Namespace(output=str(output))
    with (
        patch.object(cfg, "SUPERMEM_VAULT_PATH", vault),
        patch.object(cfg, "SUPERMEM_DB_PATH", db_file),
    ):
        cmd_backup(ns)

    assert output.exists()
    with tarfile.open(output, "r:gz") as tar:
        names = tar.getnames()
    assert "vault/note.md" in names
    assert "supermem.db" in names


# ── cmd_restore ───────────────────────────────────────────────────────────────


def test_cmd_restore_missing_archive_exits(tmp_path: Path):
    import argparse
    from supermem.__main__ import cmd_restore

    ns = argparse.Namespace(archive=str(tmp_path / "nonexistent.tar.gz"))
    with pytest.raises(SystemExit):
        cmd_restore(ns)


def test_cmd_restore_extracts_files(tmp_path: Path):
    import argparse
    from supermem.__main__ import cmd_restore

    # Create a backup archive
    archive = tmp_path / "backup.tar.gz"
    vault_restore = tmp_path / "vault_restored"
    db_restore = tmp_path / "restored.db"

    with tarfile.open(archive, "w:gz") as tar:
        note = tmp_path / "note.md"
        note.write_text("# Restored note")
        tar.add(str(note), arcname="vault/note.md")
        db_src = tmp_path / "src.db"
        db_src.write_bytes(b"fake db")
        tar.add(str(db_src), arcname="supermem.db")

    ns = argparse.Namespace(archive=str(archive))
    with (
        patch("supermem.config.SUPERMEM_VAULT_PATH", vault_restore),
        patch("supermem.config.SUPERMEM_DB_PATH", db_restore),
    ):
        cmd_restore(ns)

    assert (vault_restore / "note.md").exists()
    assert db_restore.exists()


# ── _worker_port ──────────────────────────────────────────────────────────────


def test_worker_port_default():
    from supermem.__main__ import _worker_port

    port = _worker_port()
    assert isinstance(port, int)
    assert port > 0


# ── Unknown connector ─────────────────────────────────────────────────────────


def test_connect_unknown_connector_exits():
    import argparse
    from supermem.__main__ import cmd_connect

    ns = argparse.Namespace(
        connector="nonexistent_xyz",
        source="source",
        max_items=None,
        token=None,
    )
    with pytest.raises(SystemExit):
        cmd_connect(ns)
