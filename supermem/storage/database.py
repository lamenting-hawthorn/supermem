"""DatabaseManager — SQLite + FTS5 source-of-truth storage for Recall v2."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from supermem.config import SUPERMEM_DB_PATH, SUPERMEM_OBS_TTL_DAYS
from supermem.core.storage import BaseStorage
from supermem.errors import StorageError
from supermem.logging import get_logger

log = get_logger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      REAL    NOT NULL,
    ended_at        REAL,
    summary         TEXT,
    correlation_id  TEXT    UNIQUE
);

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    created_at      REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    content         TEXT    NOT NULL,
    content_hash    TEXT    NOT NULL,
    tier_used       INTEGER,
    latency_ms      REAL,
    tool_name       TEXT,
    type            TEXT    NOT NULL DEFAULT 'observation'
);

CREATE TABLE IF NOT EXISTS summaries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    created_at          REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    content             TEXT    NOT NULL,
    obs_ids_compressed  TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS entity_metadata (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    UNIQUE NOT NULL,
    file_path       TEXT    NOT NULL,
    last_indexed    REAL    NOT NULL,
    wikilink_count  INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    content,
    obs_id UNINDEXED,
    tokenize='porter ascii'
);

CREATE INDEX IF NOT EXISTS idx_observations_session ON observations(session_id);
CREATE INDEX IF NOT EXISTS idx_observations_created ON observations(created_at);
CREATE INDEX IF NOT EXISTS idx_observations_hash ON observations(content_hash);
"""


class DatabaseManager(BaseStorage):
    """
    SQLite-backed storage with FTS5 full-text search.

    All methods are async. Call await db.init() before first use,
    or use as an async context manager:

        async with DatabaseManager() as db:
            session_id = await db.create_session()
    """

    def __init__(self, db_path: Path | None = None):
        self._path = db_path or SUPERMEM_DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Open connection and create schema if needed."""
        if self._conn is not None:
            return
        try:
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.executescript(_SCHEMA)
            # Add expires_at column if this is an existing DB without it
            try:
                await self._conn.execute(
                    "ALTER TABLE observations ADD COLUMN expires_at REAL"
                )
            except Exception:
                pass  # column already exists — normal for new installs
            await self._conn.commit()
            # Purge expired observations on startup (lazy cleanup)
            await self._purge_expired()
            log.info("db_init", path=str(self._path))
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"Failed to initialise database: {exc}") from exc

    async def _purge_expired(self) -> None:
        """Delete observations whose TTL has elapsed and clean up FTS index."""
        now = time.time()
        try:
            async with self._conn.execute(
                "SELECT id FROM observations WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            ) as cur:
                expired_ids = [row[0] for row in await cur.fetchall()]
            if not expired_ids:
                return
            placeholders = ",".join("?" * len(expired_ids))
            await self._conn.execute(
                f"DELETE FROM observations WHERE id IN ({placeholders})", expired_ids
            )
            await self._conn.execute(
                f"DELETE FROM content_fts WHERE obs_id IN ({placeholders})", expired_ids
            )
            await self._conn.commit()
            log.info("db_purge_expired", count=len(expired_ids))
        except Exception as exc:
            log.warning("db_purge_failed", error=str(exc))

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "DatabaseManager":
        await self.init()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ── BaseStorage interface ─────────────────────────────────────────────────

    async def write(self, record: dict) -> int:
        """Generic write — routes to the correct table via record['_table'] key."""
        table = record.pop("_table", "observations")
        if table == "observations":
            return await self.write_observation(**record)
        raise StorageError(f"Unknown table: {table}")

    async def read(self, id: int) -> dict | None:
        obs = await self.get_observations([id])
        return obs[0] if obs else None

    async def delete(self, id: int) -> bool:
        await self._ensure_init()
        try:
            async with self._conn.execute(
                "DELETE FROM observations WHERE id = ?", (id,)
            ) as cur:
                deleted = cur.rowcount > 0
            if deleted:
                await self._conn.execute(
                    "DELETE FROM content_fts WHERE obs_id = ?", (id,)
                )
            await self._conn.commit()
            return deleted
        except Exception as exc:
            raise StorageError(f"Delete failed: {exc}") from exc

    async def health(self) -> bool:
        try:
            await self._ensure_init()
            async with self._conn.execute("SELECT 1") as cur:
                await cur.fetchone()
            return True
        except Exception:
            return False

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def create_session(self, correlation_id: str | None = None) -> int:
        await self._ensure_init()
        try:
            async with self._conn.execute(
                "INSERT INTO sessions (started_at, correlation_id) VALUES (?, ?)",
                (time.time(), correlation_id),
            ) as cur:
                session_id = cur.lastrowid
            await self._conn.commit()
            log.info("session_created", session_id=session_id)
            return session_id
        except Exception as exc:
            raise StorageError(f"create_session failed: {exc}") from exc

    async def close_session(self, session_id: int, summary: str) -> None:
        await self._ensure_init()
        try:
            await self._conn.execute(
                "UPDATE sessions SET ended_at = ?, summary = ? WHERE id = ?",
                (time.time(), summary, session_id),
            )
            await self._conn.commit()
            log.info("session_closed", session_id=session_id)
        except Exception as exc:
            raise StorageError(f"close_session failed: {exc}") from exc

    # ── Observations ──────────────────────────────────────────────────────────

    async def write_observation(
        self,
        content: str,
        session_id: int | None = None,
        tier_used: int | None = None,
        latency_ms: float | None = None,
        tool_name: str | None = None,
        obs_type: str = "observation",
    ) -> int:
        await self._ensure_init()
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        # Dedup: skip if identical content already recorded in this session
        async with self._conn.execute(
            "SELECT id FROM observations WHERE content_hash = ? AND session_id IS ?",
            (content_hash, session_id),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            log.debug("obs_dedup", content_hash=content_hash[:8])
            return existing[0]
        # Set TTL only for regular observations (not entity_content or session_note)
        expires_at: float | None = None
        if obs_type == "observation" and SUPERMEM_OBS_TTL_DAYS > 0:
            expires_at = time.time() + SUPERMEM_OBS_TTL_DAYS * 86400
        try:
            async with self._conn.execute(
                """INSERT INTO observations
                   (session_id, content, content_hash, tier_used, latency_ms, tool_name, type, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    content,
                    content_hash,
                    tier_used,
                    latency_ms,
                    tool_name,
                    obs_type,
                    expires_at,
                ),
            ) as cur:
                obs_id = cur.lastrowid
            await self._conn.execute(
                "INSERT INTO content_fts (obs_id, content) VALUES (?, ?)",
                (obs_id, content),
            )
            await self._conn.commit()
            return obs_id
        except Exception as exc:
            raise StorageError(f"write_observation failed: {exc}") from exc

    async def fts_search(self, query: str, limit: int = 20) -> list[int]:
        """FTS5 keyword search. Returns observation IDs ranked by relevance."""
        await self._ensure_init()
        try:
            async with self._conn.execute(
                """SELECT obs_id FROM content_fts
                   WHERE content_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit),
            ) as cur:
                rows = await cur.fetchall()
            return [row[0] for row in rows]
        except Exception as exc:
            # FTS5 can raise on malformed queries — degrade gracefully
            log.warning("fts_search_failed", error=str(exc), query=query)
            return []

    async def get_observations(self, ids: list[int]) -> list[dict]:
        """Batch fetch full observation records by IDs."""
        if not ids:
            return []
        await self._ensure_init()
        placeholders = ",".join("?" * len(ids))
        try:
            async with self._conn.execute(
                f"SELECT * FROM observations WHERE id IN ({placeholders}) ORDER BY created_at",
                ids,
            ) as cur:
                rows = await cur.fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            raise StorageError(f"get_observations failed: {exc}") from exc

    async def get_timeline(self, obs_id: int, window: int = 5) -> list[dict]:
        """Return the N observations before and after obs_id, chronologically."""
        await self._ensure_init()
        try:
            async with self._conn.execute(
                "SELECT created_at, session_id FROM observations WHERE id = ?",
                (obs_id,),
            ) as cur:
                anchor = await cur.fetchone()
            if not anchor:
                return []
            ts, sid = anchor[0], anchor[1]
            before_query = """
                SELECT * FROM observations
                WHERE created_at < ? AND (session_id IS ? OR ? IS NULL)
                ORDER BY created_at DESC LIMIT ?
            """
            after_query = """
                SELECT * FROM observations
                WHERE created_at >= ? AND (session_id IS ? OR ? IS NULL)
                ORDER BY created_at ASC LIMIT ?
            """
            async with self._conn.execute(before_query, (ts, sid, sid, window)) as cur:
                before = [dict(r) for r in await cur.fetchall()]
            async with self._conn.execute(
                after_query, (ts, sid, sid, window + 1)
            ) as cur:
                after = [dict(r) for r in await cur.fetchall()]
            return list(reversed(before)) + after
        except Exception as exc:
            raise StorageError(f"get_timeline failed: {exc}") from exc

    # ── Entity metadata ───────────────────────────────────────────────────────

    async def get_entity_last_indexed(self, name: str) -> float | None:
        """Return last_indexed timestamp for an entity, or None if not indexed yet."""
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT last_indexed FROM entity_metadata WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def upsert_entity(
        self, name: str, file_path: str, wikilink_count: int = 0
    ) -> None:
        await self._ensure_init()
        try:
            await self._conn.execute(
                """INSERT INTO entity_metadata (name, file_path, last_indexed, wikilink_count)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       file_path = excluded.file_path,
                       last_indexed = excluded.last_indexed,
                       wikilink_count = excluded.wikilink_count""",
                (name, file_path, time.time(), wikilink_count),
            )
            await self._conn.commit()
        except Exception as exc:
            raise StorageError(f"upsert_entity failed: {exc}") from exc

    async def entities_for_obs_ids(self, obs_ids: list[int]) -> list[str]:
        """Look up entity names that appear in observations — for graph expansion seed."""
        if not obs_ids:
            return []
        await self._ensure_init()
        placeholders = ",".join("?" * len(obs_ids))
        # Use FTS5 EXISTS subquery — index-accelerated vs instr() full scan
        async with self._conn.execute(
            f"""SELECT DISTINCT em.name FROM entity_metadata em
                WHERE EXISTS (
                    SELECT 1 FROM content_fts
                    WHERE content_fts MATCH ('"' || em.name || '"')
                    AND obs_id IN ({placeholders})
                )""",
            obs_ids,
        ) as cur:
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def obs_ids_for_entities(self, entity_names: list[str]) -> list[int]:
        """Find observations that mention any of the given entity names."""
        if not entity_names:
            return []
        await self._ensure_init()
        results: list[int] = []
        for name in entity_names:
            # FTS5 phrase match — index-accelerated vs instr() full scan
            async with self._conn.execute(
                "SELECT obs_id FROM content_fts WHERE content_fts MATCH ? LIMIT 20",
                (f'"{name}"',),
            ) as cur:
                rows = await cur.fetchall()
            results.extend(row[0] for row in rows)
        return list(dict.fromkeys(results))  # deduplicate, preserve order

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        await self._ensure_init()
        async with self._conn.execute("SELECT COUNT(*) FROM observations") as cur:
            obs_count = (await cur.fetchone())[0]
        async with self._conn.execute("SELECT COUNT(*) FROM entity_metadata") as cur:
            entity_count = (await cur.fetchone())[0]
        async with self._conn.execute("SELECT COUNT(*) FROM sessions") as cur:
            session_count = (await cur.fetchone())[0]
        db_size = self._path.stat().st_size if self._path.exists() else 0
        return {
            "obs_count": obs_count,
            "entity_count": entity_count,
            "session_count": session_count,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
        }

    # ── Summaries ─────────────────────────────────────────────────────────────

    async def write_summary(
        self,
        session_id: int,
        content: str,
        obs_ids_compressed: list[int],
    ) -> int:
        await self._ensure_init()
        try:
            async with self._conn.execute(
                """INSERT INTO summaries (session_id, content, obs_ids_compressed)
                   VALUES (?, ?, ?)""",
                (session_id, content, json.dumps(obs_ids_compressed)),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except Exception as exc:
            raise StorageError(f"write_summary failed: {exc}") from exc

    async def get_recent_observations(
        self, session_id: int, limit: int = 50
    ) -> list[dict]:
        await self._ensure_init()
        async with self._conn.execute(
            """SELECT * FROM observations WHERE session_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (session_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _ensure_init(self) -> None:
        if self._conn is None:
            await self.init()
