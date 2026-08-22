"""DatabaseManager — SQLite + FTS5 source-of-truth storage for supermem."""

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
from supermem.privacy import PrivacyFilter

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
    type            TEXT    NOT NULL DEFAULT 'observation',
    source_id       TEXT,
    source_span     TEXT,
    observed_at     REAL,
    valid_from      REAL,
    valid_until     REAL,
    confidence      REAL    NOT NULL DEFAULT 1.0,
    trust_level     TEXT    NOT NULL DEFAULT 'unknown',
    sensitivity     TEXT    NOT NULL DEFAULT 'normal',
    status          TEXT    NOT NULL DEFAULT 'active',
    expires_at      REAL
);

CREATE TABLE IF NOT EXISTS summaries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    created_at          REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    content             TEXT    NOT NULL,
    obs_ids_compressed  TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS retraction_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_id          INTEGER NOT NULL,
    created_at      REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    reason          TEXT,
    FOREIGN KEY(obs_id) REFERENCES observations(id) ON DELETE CASCADE
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
    tokenize='porter unicode61 remove_diacritics 2'
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
        self._last_purge: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Open connection and create schema if needed."""
        if self._conn is not None:
            return
        try:
            conn = await aiosqlite.connect(self._path)
            self._conn = conn
            conn.row_factory = aiosqlite.Row
            await conn.executescript(_SCHEMA)
            migration_ddls = {
                "expires_at": "ALTER TABLE observations ADD COLUMN expires_at REAL",
                "source_id": "ALTER TABLE observations ADD COLUMN source_id TEXT",
                "source_span": "ALTER TABLE observations ADD COLUMN source_span TEXT",
                "observed_at": "ALTER TABLE observations ADD COLUMN observed_at REAL",
                "valid_from": "ALTER TABLE observations ADD COLUMN valid_from REAL",
                "valid_until": "ALTER TABLE observations ADD COLUMN valid_until REAL",
                "confidence": "ALTER TABLE observations ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0",
                "trust_level": "ALTER TABLE observations ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'unknown'",
                "sensitivity": "ALTER TABLE observations ADD COLUMN sensitivity TEXT NOT NULL DEFAULT 'normal'",
                "status": "ALTER TABLE observations ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
            }
            async with conn.execute("PRAGMA table_info(observations)") as cur:
                existing_columns = {row[1] for row in await cur.fetchall()}
            for column, ddl in migration_ddls.items():
                if column not in existing_columns:
                    await conn.execute(ddl)
            await conn.commit()
            # Purge expired observations on startup (lazy cleanup)
            await self._purge_expired()
            log.info("db_init", path=str(self._path))
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"Failed to initialise database: {exc}") from exc

    @staticmethod
    def _active_clause(alias: str = "observations") -> str:
        """WHERE fragment for rows that are active, unexpired, and effective now.

        Binds two ``now`` parameters (unixepoch floats) in order:
        one for the TTL check and one for the validity window.
        """
        return (
            f"{alias}.status = 'active'"
            f" AND ({alias}.expires_at IS NULL OR {alias}.expires_at > ?)"
            f" AND ({alias}.valid_from IS NULL OR {alias}.valid_from <= ?)"
            f" AND ({alias}.valid_until IS NULL OR {alias}.valid_until > ?)"
        )

    async def set_validity_window(
        self,
        obs_id: int,
        valid_from: float | None = None,
        valid_until: float | None = None,
    ) -> bool:
        """Set the effective-time validity window for an observation.

        Both bounds are optional; pass ``None`` to clear a bound. Rows whose
        window does not cover 'now' are excluded from all retrieval paths.
        Returns False if no observation with that id exists.
        """
        conn = await self._ensure_init()
        try:
            async with conn.execute(
                "UPDATE observations SET valid_from = ?, valid_until = ? WHERE id = ?",
                (valid_from, valid_until, obs_id),
            ) as cur:
                updated = cur.rowcount > 0
            await conn.commit()
            return updated
        except Exception as exc:
            raise StorageError(f"set_validity_window failed: {exc}") from exc

    async def maybe_purge_expired(self, throttle_seconds: int = 300) -> int:
        """Delete expired observations, at most once per throttle window.

        Unlike the startup-only purge, this is safe to call from any write or
        retrieval path so a long-running server never returns expired memories.
        """
        now = time.time()
        if now - self._last_purge < throttle_seconds:
            return 0
        self._last_purge = now
        count = await self._purge_expired()
        return count

    async def _purge_expired(self) -> int:
        """Delete observations whose TTL has elapsed and clean up FTS index."""
        now = time.time()
        conn = await self._ensure_init()
        try:
            async with conn.execute(
                "SELECT id FROM observations WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            ) as cur:
                expired_ids = [row[0] for row in await cur.fetchall()]
            if not expired_ids:
                return 0
            placeholders = ",".join("?" * len(expired_ids))
            await conn.execute(
                f"DELETE FROM observations WHERE id IN ({placeholders})", expired_ids
            )
            await conn.execute(
                f"DELETE FROM content_fts WHERE obs_id IN ({placeholders})", expired_ids
            )
            await conn.commit()
            log.info("db_purge_expired", count=len(expired_ids))
            return len(expired_ids)
        except Exception as exc:
            log.warning("db_purge_failed", error=str(exc))
            return 0

    async def supersede_by_source(
        self, source_uri: str, *, exclude_id: int | None = None
    ) -> int:
        """Mark prior active entity_content observations for a source as superseded.

        A file that changes multiple times should only have ONE current revision
        in the active set; older revisions are superseded and dropped from FTS so
        they can never surface as current memory (stale-revision bug, SF-4).
        Returns the number of rows superseded.
        """
        conn = await self._ensure_init()
        try:
            await conn.execute("BEGIN")
            if exclude_id is not None:
                async with conn.execute(
                    """SELECT id, session_id FROM observations
                       WHERE source_id = ? AND type = 'entity_content'
                         AND status = 'active' AND id != ?""",
                    (source_uri, exclude_id),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with conn.execute(
                    """SELECT id, session_id FROM observations
                       WHERE source_id = ? AND type = 'entity_content'
                         AND status = 'active'""",
                    (source_uri,),
                ) as cur:
                    rows = await cur.fetchall()
            ids = [r[0] for r in rows]
            if not ids:
                await conn.rollback()
                return 0
            placeholders = ",".join("?" * len(ids))
            await conn.execute(
                f"UPDATE observations SET status = 'superseded' WHERE id IN ({placeholders})",
                ids,
            )
            await conn.execute(
                f"DELETE FROM content_fts WHERE obs_id IN ({placeholders})", ids
            )
            for r in rows:
                if r[1] is not None:
                    await conn.execute(
                        "DELETE FROM summaries WHERE session_id = ?", (r[1],)
                    )
            await conn.commit()
            log.info("db_superseded", source_uri=source_uri, count=len(ids))
            return len(ids)
        except Exception as exc:
            await conn.rollback()
            raise StorageError(f"supersede_by_source failed: {exc}") from exc

    async def archive_observations(self, obs_ids: list[int]) -> int:
        """Archive observations without destroying their text.

        Archived rows are excluded from retrieval (status != 'active') but the
        content is preserved for audit/recovery — unlike hard deletion. This lets
        the memory compressor summarise without permanently losing source facts.
        """
        if not obs_ids:
            return 0
        conn = await self._ensure_init()
        try:
            await conn.execute("BEGIN")
            placeholders = ",".join("?" * len(obs_ids))
            await conn.execute(
                f"UPDATE observations SET status = 'archived' WHERE id IN ({placeholders})",
                obs_ids,
            )
            await conn.execute(
                f"DELETE FROM content_fts WHERE obs_id IN ({placeholders})", obs_ids
            )
            await conn.commit()
            return len(obs_ids)
        except Exception as exc:
            await conn.rollback()
            raise StorageError(f"archive_observations failed: {exc}") from exc

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
        conn = await self._ensure_init()
        try:
            async with conn.execute(
                "DELETE FROM observations WHERE id = ?", (id,)
            ) as cur:
                deleted = cur.rowcount > 0
            if deleted:
                await conn.execute("DELETE FROM content_fts WHERE obs_id = ?", (id,))
            await conn.commit()
            return deleted
        except Exception as exc:
            raise StorageError(f"Delete failed: {exc}") from exc

    async def health(self) -> bool:
        try:
            conn = await self._ensure_init()
            async with conn.execute("SELECT 1") as cur:
                await cur.fetchone()
            return True
        except Exception:
            return False

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def create_session(self, correlation_id: str | None = None) -> int:
        conn = await self._ensure_init()
        try:
            async with conn.execute(
                "INSERT INTO sessions (started_at, correlation_id) VALUES (?, ?)",
                (time.time(), correlation_id),
            ) as cur:
                session_id = cur.lastrowid
            await conn.commit()
            log.info("session_created", session_id=session_id)
            return session_id
        except Exception as exc:
            raise StorageError(f"create_session failed: {exc}") from exc

    async def close_session(self, session_id: int, summary: str) -> None:
        conn = await self._ensure_init()
        try:
            await conn.execute(
                "UPDATE sessions SET ended_at = ?, summary = ? WHERE id = ?",
                (time.time(), summary, session_id),
            )
            await conn.commit()
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
        source_id: str | None = None,
        source_span: str | None = None,
        observed_at: float | None = None,
        valid_from: float | None = None,
        valid_until: float | None = None,
        confidence: float = 1.0,
        trust_level: str = "unknown",
        sensitivity: str = "normal",
        status: str = "active",
    ) -> int:
        content = PrivacyFilter.strip(content)
        if not content:
            raise StorageError(
                "Refusing to persist empty or entirely private observation"
            )
        conn = await self._ensure_init()
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        confidence = max(0.0, min(confidence, 1.0))
        try:
            # BEGIN IMMEDIATE takes the write lock up front so the dedup check
            # and the insert are atomic — two concurrent writers can no longer
            # both miss the dedup and create duplicate rows (SF-8).
            await conn.execute("BEGIN IMMEDIATE")
            async with conn.execute(
                """SELECT id FROM observations
                   WHERE content_hash = ? AND session_id IS ? AND type = ?
                     AND source_id IS ? AND source_span IS ? AND observed_at IS ?
                     AND valid_from IS ? AND valid_until IS ? AND confidence = ?
                     AND trust_level = ? AND sensitivity = ? AND status = ?""",
                (
                    content_hash,
                    session_id,
                    obs_type,
                    source_id,
                    source_span,
                    observed_at,
                    valid_from,
                    valid_until,
                    confidence,
                    trust_level,
                    sensitivity,
                    status,
                ),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                log.debug("obs_dedup", content_hash=content_hash[:8])
                await conn.commit()
                return existing[0]
            # Set TTL only for regular observations (not entity_content or session_note)
            expires_at: float | None = None
            if obs_type == "observation" and SUPERMEM_OBS_TTL_DAYS > 0:
                expires_at = time.time() + SUPERMEM_OBS_TTL_DAYS * 86400
            async with conn.execute(
                """INSERT INTO observations
                   (session_id, content, content_hash, tier_used, latency_ms, tool_name,
                    type, source_id, source_span, observed_at, valid_from, valid_until,
                    confidence, trust_level, sensitivity, status, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    content,
                    content_hash,
                    tier_used,
                    latency_ms,
                    tool_name,
                    obs_type,
                    source_id,
                    source_span,
                    observed_at,
                    valid_from,
                    valid_until,
                    confidence,
                    trust_level,
                    sensitivity,
                    status,
                    expires_at,
                ),
            ) as cur:
                obs_id = cur.lastrowid
            if status == "active":
                await conn.execute(
                    "INSERT INTO content_fts (obs_id, content) VALUES (?, ?)",
                    (obs_id, content),
                )
            await conn.commit()
            return obs_id
        except Exception as exc:
            await conn.rollback()
            raise StorageError(f"write_observation failed: {exc}") from exc

    @staticmethod
    def _sanitize_match_query(query: str) -> tuple[str, list[str]]:
        """Make arbitrary user text safe for FTS5 MATCH.

        Raw queries containing apostrophes, hyphens, or punctuation are treated
        as FTS5 syntax and raise. Quoting each whitespace-separated term (with
        embedded quotes doubled) turns them into phrase terms, joined with OR
        so noisy real-world queries still surface their best document instead
        of requiring every term to hit. Returns the MATCH expression plus the
        original terms for downstream coverage scoring.
        """
        terms = query.replace('"', '""').split()
        if not terms:
            return "", []
        return " OR ".join(f'"{term}"' for term in terms), terms

    @staticmethod
    def _fold(text: str) -> str:
        """Case- and accent-fold text so Python-side matching agrees with the
        FTS tokenizer's remove_diacritics behavior."""
        import unicodedata

        return "".join(
            ch
            for ch in unicodedata.normalize("NFD", text.lower())
            if unicodedata.category(ch) != "Mn"
        )

    _STOPWORDS = frozenset(
        """a an the is are was were do does did to of in on for with and or that
        this it its as at by be been my your our their his her we you i me he she
        they them what which who whom when where why how about into over after
        before between out up down from can could should would will shall may
        might must have has had am s t don doesn""".split()
    )

    @classmethod
    def _content_terms(cls, terms: list[str]) -> list[str]:
        """First token of each query term that isn't a stopword.

        The FTS index and this matching key are built from the same first-token
        convention, so a term 'matches' a document when its leading
        content-bearing token appears among the document's tokens.
        """
        import re

        keys: list[str] = []
        for term in terms:
            tokens = re.findall(r"\w+", cls._fold(term))
            if tokens and tokens[0] not in cls._STOPWORDS:
                keys.append(tokens[0])
        return list(dict.fromkeys(keys))

    @classmethod
    def _term_coverage(cls, content: str, terms: list[str]) -> int:
        """How many of the query's content-bearing keys appear in content.

        Stopwords are ignored so a natural-language question isn't penalized
        for missing 'what'/'the'/'to' in a document.
        """
        import re

        doc_tokens = set(re.findall(r"\w+", cls._fold(content)))
        return sum(1 for key in cls._content_terms(terms) if key in doc_tokens)

    @classmethod
    def _min_coverage_for(cls, terms: list[str]) -> int:
        """Require at least half the content-bearing keys (min 1) to hit."""
        n = len(cls._content_terms(terms)) or 1
        return max(1, -(-n // 2))

    async def fts_search(self, query: str, limit: int = 20) -> list[int]:
        """FTS5 keyword search. Returns observation IDs ranked by relevance.

        Multi-term queries require at least half the terms (min 2) to be
        present in a candidate before it is returned — pure OR matching lets
        one coincidental word drag unrelated documents into results, while
        pure AND makes every noisy real-world query return nothing.
        """
        conn = await self._ensure_init()
        match_query, raw_terms = self._sanitize_match_query(query)
        if not match_query:
            return []
        try:
            now = time.time()
            async with conn.execute(
                """SELECT f.obs_id, o.content FROM content_fts f
                   JOIN observations o ON o.id = f.obs_id
                   WHERE content_fts MATCH ? AND """
                + self._active_clause("o")
                + """
                   ORDER BY rank
                   LIMIT ?""",
                (match_query, now, now, now, limit * 3),
            ) as cur:
                rows = await cur.fetchall()
            min_coverage = self._min_coverage_for(raw_terms)
            selected: list[int] = []
            for row in rows:
                if self._term_coverage(row[1], raw_terms) >= min_coverage:
                    selected.append(row[0])
                    if len(selected) >= limit:
                        break
            return selected
        except Exception as exc:
            # FTS5 can raise on malformed queries — degrade gracefully
            log.warning("fts_search_failed", error=str(exc), query=query)
            return []

    async def active_obs_ids(self, ids: list[int]) -> list[int]:
        """Return the subset of ids whose observations are still active."""
        if not ids:
            return []
        conn = await self._ensure_init()
        placeholders = ",".join("?" * len(ids))
        now = time.time()
        async with conn.execute(
            f"SELECT id FROM observations WHERE id IN ({placeholders}) AND "
            + self._active_clause()
            + "",
            (*ids, now, now, now),
        ) as cur:
            rows = await cur.fetchall()
        active = {row[0] for row in rows}
        return [obs_id for obs_id in ids if obs_id in active]

    async def get_observations(self, ids: list[int]) -> list[dict]:
        """Batch fetch full observation records by IDs."""
        if not ids:
            return []
        conn = await self._ensure_init()
        placeholders = ",".join("?" * len(ids))
        try:
            now = time.time()
            async with conn.execute(
                f"SELECT * FROM observations WHERE id IN ({placeholders}) AND "
                + self._active_clause()
                + " ORDER BY created_at",
                (*ids, now, now, now),
            ) as cur:
                rows = await cur.fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            raise StorageError(f"get_observations failed: {exc}") from exc

    async def get_timeline(self, obs_id: int, window: int = 5) -> list[dict]:
        """Return the N observations before and after obs_id, chronologically."""
        conn = await self._ensure_init()
        try:
            now = time.time()
            async with conn.execute(
                "SELECT created_at, session_id FROM observations WHERE id = ? AND "
                + self._active_clause()
                + "",
                (obs_id, now, now, now),
            ) as cur:
                anchor = await cur.fetchone()
            if not anchor:
                return []
            ts, sid = anchor[0], anchor[1]
            before_query = (
                """
                SELECT * FROM observations
                WHERE created_at < ? AND """
                + self._active_clause()
                + """ AND (session_id IS ? OR ? IS NULL)
                ORDER BY created_at DESC LIMIT ?
            """
            )
            after_query = (
                """
                SELECT * FROM observations
                WHERE created_at >= ? AND """
                + self._active_clause()
                + """ AND (session_id IS ? OR ? IS NULL)
                ORDER BY created_at ASC LIMIT ?
            """
            )
            async with conn.execute(
                before_query, (ts, now, now, now, sid, sid, window)
            ) as cur:
                before = [dict(r) for r in await cur.fetchall()]
            async with conn.execute(
                after_query, (ts, now, now, now, sid, sid, window + 1)
            ) as cur:
                after = [dict(r) for r in await cur.fetchall()]
            return list(reversed(before)) + after
        except Exception as exc:
            raise StorageError(f"get_timeline failed: {exc}") from exc

    # ── Entity metadata ───────────────────────────────────────────────────────

    async def get_entity_last_indexed(self, name: str) -> float | None:
        """Return last_indexed timestamp for an entity, or None if not indexed yet."""
        conn = await self._ensure_init()
        async with conn.execute(
            "SELECT last_indexed FROM entity_metadata WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def upsert_entity(
        self, name: str, file_path: str, wikilink_count: int = 0
    ) -> None:
        conn = await self._ensure_init()
        try:
            await conn.execute(
                """INSERT INTO entity_metadata (name, file_path, last_indexed, wikilink_count)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       file_path = excluded.file_path,
                       last_indexed = excluded.last_indexed,
                       wikilink_count = excluded.wikilink_count""",
                (name, file_path, time.time(), wikilink_count),
            )
            await conn.commit()
        except Exception as exc:
            raise StorageError(f"upsert_entity failed: {exc}") from exc

    async def entities_for_obs_ids(self, obs_ids: list[int]) -> list[str]:
        """Look up entity names that appear in observations — for graph expansion seed."""
        if not obs_ids:
            return []
        conn = await self._ensure_init()
        placeholders = ",".join("?" * len(obs_ids))
        # Use FTS5 EXISTS subquery — index-accelerated vs instr() full scan
        async with conn.execute(
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
        conn = await self._ensure_init()
        results: list[int] = []
        for name in entity_names:
            # FTS5 phrase match — index-accelerated vs instr() full scan
            async with conn.execute(
                "SELECT obs_id FROM content_fts WHERE content_fts MATCH ? LIMIT 20",
                (f'"{name}"',),
            ) as cur:
                rows = await cur.fetchall()
            results.extend(row[0] for row in rows)
        return list(dict.fromkeys(results))  # deduplicate, preserve order

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        conn = await self._ensure_init()
        async with conn.execute("SELECT COUNT(*) FROM observations") as cur:
            obs_count = (await cur.fetchone())[0]
        async with conn.execute("SELECT COUNT(*) FROM entity_metadata") as cur:
            entity_count = (await cur.fetchone())[0]
        async with conn.execute("SELECT COUNT(*) FROM sessions") as cur:
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
        content = PrivacyFilter.strip(content)
        if not content:
            raise StorageError("Refusing to persist empty or entirely private summary")
        conn = await self._ensure_init()
        try:
            async with conn.execute(
                """INSERT INTO summaries (session_id, content, obs_ids_compressed)
                   VALUES (?, ?, ?)""",
                (session_id, content, json.dumps(obs_ids_compressed)),
            ) as cur:
                row_id = cur.lastrowid
            await conn.commit()
            return row_id
        except Exception as exc:
            raise StorageError(f"write_summary failed: {exc}") from exc

    async def get_recent_observations(
        self, session_id: int, limit: int = 50
    ) -> list[dict]:
        conn = await self._ensure_init()
        now = time.time()
        async with conn.execute(
            """SELECT * FROM observations WHERE session_id = ? AND """
            + self._active_clause()
            + """
               ORDER BY created_at DESC LIMIT ?""",
            (session_id, now, now, now, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def retract_observation(self, obs_id: int, reason: str | None = None) -> bool:
        """Mark an observation retracted and store a non-retrievable audit reason."""
        conn = await self._ensure_init()
        try:
            await conn.execute("BEGIN")
            async with conn.execute(
                "SELECT session_id FROM observations WHERE id = ?", (obs_id,)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await conn.rollback()
                return False
            session_id = row[0]
            await conn.execute(
                "UPDATE observations SET status = 'retracted' WHERE id = ?", (obs_id,)
            )
            await conn.execute("DELETE FROM content_fts WHERE obs_id = ?", (obs_id,))
            if session_id is not None:
                await conn.execute(
                    "UPDATE sessions SET summary = NULL WHERE id = ?", (session_id,)
                )
                await conn.execute(
                    "DELETE FROM summaries WHERE session_id = ?", (session_id,)
                )
            if reason:
                await conn.execute(
                    "INSERT INTO retraction_audit (obs_id, reason) VALUES (?, ?)",
                    (obs_id, reason),
                )
            await conn.commit()
            return True
        except Exception as exc:
            await conn.rollback()
            raise StorageError(f"retract_observation failed: {exc}") from exc

    async def get_recent_observations_by_age(
        self, days: int = 7, limit: int = 500
    ) -> list[dict]:
        """Return recent observations newest first for local insight tools."""
        conn = await self._ensure_init()
        since = time.time() - max(days, 1) * 86400
        now = time.time()
        async with conn.execute(
            """SELECT * FROM observations
               WHERE created_at >= ? AND """
            + self._active_clause()
            + """
               ORDER BY created_at DESC LIMIT ?""",
            (since, now, now, now, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _ensure_init(self) -> aiosqlite.Connection:
        if self._conn is None:
            await self.init()
        assert self._conn is not None
        return self._conn
