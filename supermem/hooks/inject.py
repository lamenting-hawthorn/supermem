"""
Recall ambient memory injection — UserPromptSubmit hook.

Reads stdin JSON from Claude Code, extracts the user's latest message,
runs FTS5 search against the Recall SQLite DB (read-only, no MCP server
needed), and returns top matching observations as a systemMessage.

Exit codes: always 0. On any error, outputs {"continue": true} silently.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

_DB_PATH = Path(os.getenv("SUPERMEM_DB_PATH", str(Path.home() / ".supermem" / "supermem.db")))
_MAX_RESULTS = 5
_MAX_SNIPPET = 400
_MIN_QUERY_LEN = 3


def _extract_prompt(data: dict) -> str:
    """Pull the latest user message text from the hook payload."""
    transcript = data.get("transcript", [])
    for msg in reversed(transcript):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        return text
    return ""


def _sanitize_query(text: str) -> str:
    """Strip FTS5 special chars; return simple space-separated keywords."""
    cleaned = re.sub(r"[^\w\s]", " ", text[:500])
    words = [w for w in cleaned.split() if len(w) >= 3]
    # Avoid FTS5 operator words
    stop = {"and", "or", "not", "the", "for", "with", "that", "this", "from"}
    words = [w for w in words if w.lower() not in stop]
    return " ".join(words[:10])


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        print(json.dumps({"continue": True}))
        return

    prompt = _extract_prompt(data)
    if len(prompt.strip()) < _MIN_QUERY_LEN:
        print(json.dumps({"continue": True}))
        return

    if not _DB_PATH.exists():
        print(json.dumps({"continue": True}))
        return

    query = _sanitize_query(prompt)
    if not query:
        print(json.dumps({"continue": True}))
        return

    try:
        # Read-only connection — never locks or modifies the DB
        conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        cur = conn.execute(
            "SELECT obs_id FROM content_fts WHERE content_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, _MAX_RESULTS),
        )
        obs_ids = [row[0] for row in cur.fetchall()]

        if not obs_ids:
            conn.close()
            print(json.dumps({"continue": True}))
            return

        placeholders = ",".join("?" * len(obs_ids))
        cur = conn.execute(
            f"SELECT content, type FROM observations"
            f" WHERE id IN ({placeholders}) ORDER BY created_at DESC",
            obs_ids,
        )
        rows = cur.fetchall()
        conn.close()

        snippets = []
        for row in rows:
            content = (row["content"] or "").strip()
            if content:
                snippets.append(content[:_MAX_SNIPPET])

        if not snippets:
            print(json.dumps({"continue": True}))
            return

        body = "\n\n---\n\n".join(snippets)
        system_msg = f"[Recall memory — relevant past context]\n\n{body}"
        print(json.dumps({"systemMessage": system_msg, "continue": True}))

    except Exception:
        print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
