"""
Recall session learning — Stop hook.

On Claude Code session stop, extracts the last substantive assistant
response and writes it as an observation to the Recall SQLite DB.
This captures what Claude actually did/decided, without requiring the
user to manually run /self-learn.

Exit codes: always 0. On any error, exits silently.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

_DB_PATH = Path(os.getenv("SUPERMEM_DB_PATH", str(Path.home() / ".supermem" / "supermem.db")))
_MIN_CONTENT_LEN = 50
_MAX_CONTENT_LEN = 2000


def _extract_last_assistant_text(transcript: list) -> str:
    """Return the last non-trivial assistant message text."""
    for msg in reversed(transcript):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) >= _MIN_CONTENT_LEN:
            return content[:_MAX_CONTENT_LEN]
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if len(text) >= _MIN_CONTENT_LEN:
                        return text[:_MAX_CONTENT_LEN]
    return ""


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return

    if not _DB_PATH.exists():
        return

    transcript = data.get("transcript", [])
    cwd = data.get("cwd", "")

    summary_text = _extract_last_assistant_text(transcript)
    if not summary_text:
        return

    prefix = f"[session-end cwd={cwd}]\n" if cwd else "[session-end]\n"
    obs_text = prefix + summary_text
    content_hash = hashlib.sha256(obs_text.encode()).hexdigest()

    try:
        conn = sqlite3.connect(str(_DB_PATH))

        # Dedup: skip if this exact content already recorded
        cur = conn.execute(
            "SELECT id FROM observations WHERE content_hash = ?",
            (content_hash,),
        )
        if cur.fetchone():
            conn.close()
            return

        conn.execute(
            """INSERT INTO observations
               (content, content_hash, tier_used, tool_name, type)
               VALUES (?, ?, 0, 'session_stop_hook', 'session_note')""",
            (obs_text, content_hash),
        )
        obs_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO content_fts (obs_id, content) VALUES (?, ?)",
            (obs_id, obs_text),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
