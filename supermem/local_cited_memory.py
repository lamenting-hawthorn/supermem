"""BM-0 local source-revision to cited SQLite FTS retrieval.

This module is intentionally self-contained.  The canonical tables are the
authority; the FTS5 table is a disposable projection rebuilt from them.  It is
local, single-actor, and does not invoke graph, vector, models, or Tier 4.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4

from supermem.privacy import PrivacyFilter

LifecycleState = Literal["active", "superseded", "retracted", "deleted"]
_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)
_MEMORY_URI_RE = re.compile(
    r"^memory://[a-z0-9][a-z0-9-]*/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$"
)
_MAX_QUERY_CHARS = 1024
_MAX_RESULTS = 100
_MAX_TIMEOUT_MS = 5_000

_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS bm0_sources (
    source_id TEXT PRIMARY KEY,
    source_uri TEXT NOT NULL UNIQUE,
    current_revision INTEGER,
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('active','deleted'))
);
CREATE TABLE IF NOT EXISTS bm0_source_revisions (
    source_id TEXT NOT NULL REFERENCES bm0_sources(source_id),
    revision INTEGER NOT NULL,
    source_uri TEXT NOT NULL,
    content TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    captured_at REAL NOT NULL,
    source_span TEXT NOT NULL,
    previous_revision INTEGER,
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('active','superseded','deleted')),
    PRIMARY KEY (source_id, revision)
);
CREATE TABLE IF NOT EXISTS bm0_source_events (
    event_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('ingest','revise','supersede','retract','delete')),
    event_digest TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    FOREIGN KEY (source_id, revision) REFERENCES bm0_source_revisions(source_id, revision)
);
CREATE TRIGGER IF NOT EXISTS bm0_source_events_append_only_update
BEFORE UPDATE ON bm0_source_events BEGIN
    SELECT RAISE(ABORT, 'bm0 source events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS bm0_source_revisions_immutable_update
BEFORE UPDATE ON bm0_source_revisions BEGIN
    SELECT RAISE(ABORT, 'bm0 source revisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS bm0_source_revisions_immutable_delete
BEFORE DELETE ON bm0_source_revisions BEGIN
    SELECT RAISE(ABORT, 'bm0 source revisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS bm0_source_events_append_only_delete
BEFORE DELETE ON bm0_source_events BEGIN
    SELECT RAISE(ABORT, 'bm0 source events are append-only');
END;
CREATE TABLE IF NOT EXISTS bm0_memory_records (
    memory_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    source_span TEXT NOT NULL,
    observed_at REAL NOT NULL,
    effective_from REAL,
    effective_until REAL,
    expires_at REAL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    trust_level TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    scope TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('active','superseded','retracted','deleted')),
    supersedes_revision INTEGER,
    record_digest TEXT NOT NULL,
    FOREIGN KEY (source_id, source_revision) REFERENCES bm0_source_revisions(source_id, revision)
);
CREATE VIRTUAL TABLE IF NOT EXISTS bm0_memory_fts USING fts5(
    content, memory_id UNINDEXED, tokenize='porter ascii'
);
CREATE INDEX IF NOT EXISTS idx_bm0_memory_authority
    ON bm0_memory_records(source_id, source_revision, lifecycle_state, scope);
"""


@dataclass(frozen=True)
class SourceRevisionV1:
    source_id: str
    revision: int
    source_uri: str
    content_digest: str
    captured_at: float
    source_span: str
    previous_revision: int | None
    lifecycle_state: LifecycleState


@dataclass(frozen=True)
class MemoryRecordV1:
    memory_id: str
    revision: int
    kind: str
    content: str
    source_revision_ref: str
    source_span: str
    observed_at: float
    effective_from: float | None
    effective_until: float | None
    expires_at: float | None
    confidence: float
    trust_level: str
    sensitivity: str
    lifecycle_state: LifecycleState
    supersedes_revision: int | None
    record_digest: str


@dataclass(frozen=True)
class RetrievalQueryV1:
    query_id: str
    query: str
    scope: str = "local"
    temporal_bound: float | None = None
    max_records: int = 10
    timeout_ms: int = 1_000
    correlation_id: str = "bm0-local"

    def __post_init__(self) -> None:
        if not self.query_id or not self.correlation_id:
            raise ValueError("query_id and correlation_id are required")
        if not self.query.strip() or len(self.query) > _MAX_QUERY_CHARS:
            raise ValueError("query must be non-empty and at most 1024 characters")
        if self.scope != "local":
            raise ValueError("BM-0 supports only the local scope")
        if not 1 <= self.max_records <= _MAX_RESULTS:
            raise ValueError("max_records must be between 1 and 100")
        if not 1 <= self.timeout_ms <= _MAX_TIMEOUT_MS:
            raise ValueError("timeout_ms must be between 1 and 5000")
        if self.temporal_bound is not None and self.temporal_bound < 0:
            raise ValueError("temporal_bound must be a Unix timestamp")


@dataclass(frozen=True)
class CitedRetrievalResultV1:
    memory_id: str
    memory_revision: int
    content: str
    source_uri: str
    source_revision: int
    source_span: str
    source_digest: str
    retrieval_tier: Literal["fts"]
    retrieval_score: float
    latency_ms: float


class RetrievalTimeoutError(TimeoutError):
    """Raised when the bounded SQLite authority query exceeds its deadline."""


class LocalCitedMemory:
    """A deterministic local retrieval boundary for BM-0."""

    vector_status = "disabled: no BM-0 ingestion path"
    compression_enabled = False

    def __init__(
        self, db_path: Path, *, monotonic: Callable[[], float] = time.perf_counter
    ) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._monotonic = monotonic

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LocalCitedMemory":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _source_id(source_uri: str) -> str:
        if (
            not _MEMORY_URI_RE.fullmatch(source_uri)
            or "/../" in source_uri
            or "/./" in source_uri
        ):
            raise ValueError(
                "BM-0 source_uri must be a canonical memory:// URI without path traversal"
            )
        return "src_" + hashlib.sha256(source_uri.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _digest(value: str | dict[str, object]) -> str:
        encoded = (
            value.encode("utf-8")
            if isinstance(value, str)
            else json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _span(content: str) -> str:
        """Return an exclusive Unicode code-point range in the sanitized source."""
        return f"chars:0:{len(content)}"

    @staticmethod
    def _contained_markdown_path(
        vault_root: Path, relative_path: Path
    ) -> tuple[Path, str]:
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise ValueError("Markdown path must be a contained vault-relative path")
        root = vault_root.resolve(strict=True)
        candidate = (root / relative_path).resolve(strict=True)
        if candidate.suffix.lower() != ".md" or not candidate.is_file():
            raise ValueError("BM-0 accepts only existing Markdown files")
        try:
            canonical_relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("Markdown path escapes the vault root") from exc
        return candidate, canonical_relative.as_posix()

    def ingest_file(
        self,
        vault_root: Path,
        relative_path: Path,
        *,
        effective_from: float | None = None,
        effective_until: float | None = None,
        expires_at: float | None = None,
        confidence: float = 1.0,
        trust_level: str = "local-source",
        sensitivity: str = "normal",
    ) -> SourceRevisionV1:
        """Read one contained Markdown file and ingest it under a canonical URI.

        Resolving both root and candidate rejects traversal and symlinks that
        escape the fixture vault. Text decoding is strict so malformed bytes do
        not become silently altered source evidence.
        """
        source_path, canonical_relative = self._contained_markdown_path(
            vault_root, relative_path
        )
        content = source_path.read_text(encoding="utf-8", errors="strict")
        return self.ingest_markdown(
            f"memory://vault/{canonical_relative}",
            content,
            effective_from=effective_from,
            effective_until=effective_until,
            expires_at=expires_at,
            confidence=confidence,
            trust_level=trust_level,
            sensitivity=sensitivity,
        )

    def _append_event(
        self,
        source_id: str,
        revision: int,
        event_type: Literal["ingest", "revise", "supersede", "retract", "delete"],
        payload: dict[str, object],
    ) -> None:
        digest = self._digest(
            {
                "source_id": source_id,
                "revision": revision,
                "event_type": event_type,
                "payload": payload,
            }
        )
        event_id = f"evt_{digest[:32]}"
        self._conn.execute(
            "INSERT OR IGNORE INTO bm0_source_events (event_id, source_id, revision, event_type, event_digest, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, source_id, revision, event_type, digest, time.time()),
        )

    def ingest_markdown(
        self,
        source_uri: str,
        content: str,
        *,
        effective_from: float | None = None,
        effective_until: float | None = None,
        expires_at: float | None = None,
        confidence: float = 1.0,
        trust_level: str = "local-source",
        sensitivity: str = "normal",
    ) -> SourceRevisionV1:
        """Persist a sanitized immutable revision and atomically project it to FTS."""
        if not isinstance(content, str) or not content:
            raise ValueError("Markdown content is required")
        if (
            effective_from is not None
            and effective_until is not None
            and effective_from > effective_until
        ):
            raise ValueError("effective_from cannot be after effective_until")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if sensitivity not in {"normal", "public"}:
            raise ValueError("BM-0 persists only non-private sensitivity")
        public_content = PrivacyFilter.strip(content)
        if not public_content:
            raise ValueError("source has no persistable public content")
        source_id = self._source_id(source_uri)
        digest = self._digest(public_content)
        captured_at = time.time()
        span = self._span(public_content)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            source = self._conn.execute(
                "SELECT current_revision, lifecycle_state FROM bm0_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if source is None:
                revision, previous = 1, None
                self._conn.execute(
                    "INSERT INTO bm0_sources (source_id, source_uri, current_revision, lifecycle_state) VALUES (?, ?, ?, 'active')",
                    (source_id, source_uri, revision),
                )
            else:
                if source["lifecycle_state"] == "deleted":
                    raise ValueError("deleted sources cannot be revived; use a new URI")
                current = int(source["current_revision"])
                active = self._conn.execute(
                    "SELECT content_digest FROM bm0_source_revisions WHERE source_id = ? AND revision = ?",
                    (source_id, current),
                ).fetchone()
                if active is not None and active["content_digest"] == digest:
                    self._conn.rollback()
                    return self._source_revision(source_id, current)
                revision, previous = current + 1, current
                self._conn.execute(
                    "UPDATE bm0_memory_records SET lifecycle_state = 'superseded' WHERE source_id = ? AND source_revision = ?",
                    (source_id, current),
                )
                self._conn.execute(
                    "DELETE FROM bm0_memory_fts WHERE memory_id = ?",
                    (f"{source_id}:{current}",),
                )
                self._conn.execute(
                    "UPDATE bm0_sources SET current_revision = ? WHERE source_id = ?",
                    (revision, source_id),
                )
            self._conn.execute(
                """INSERT INTO bm0_source_revisions
                (source_id, revision, source_uri, content, content_digest, captured_at, source_span, previous_revision, lifecycle_state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
                (
                    source_id,
                    revision,
                    source_uri,
                    public_content,
                    digest,
                    captured_at,
                    span,
                    previous,
                ),
            )
            if previous is None:
                self._append_event(
                    source_id, revision, "ingest", {"content_digest": digest}
                )
            else:
                self._append_event(
                    source_id,
                    previous,
                    "supersede",
                    {"next_revision": revision},
                )
                self._append_event(
                    source_id,
                    revision,
                    "revise",
                    {"previous_revision": previous, "content_digest": digest},
                )
            memory_id = f"{source_id}:{revision}"
            record_payload: dict[str, object] = {
                "memory_id": memory_id,
                "revision": revision,
                "kind": "markdown",
                "content": public_content,
                "source_revision_ref": f"{source_id}:{revision}",
                "source_span": span,
                "observed_at": captured_at,
                "effective_from": effective_from,
                "effective_until": effective_until,
                "expires_at": expires_at,
                "confidence": confidence,
                "trust_level": trust_level,
                "sensitivity": sensitivity,
                "lifecycle_state": "active",
                "supersedes_revision": previous,
            }
            record_digest = self._digest(record_payload)
            self._conn.execute(
                """INSERT INTO bm0_memory_records
                (memory_id, revision, kind, content, source_id, source_revision, source_span, observed_at,
                effective_from, effective_until, expires_at, confidence, trust_level, sensitivity, scope,
                lifecycle_state, supersedes_revision, record_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local', 'active', ?, ?)""",
                (
                    memory_id,
                    revision,
                    "markdown",
                    public_content,
                    source_id,
                    revision,
                    span,
                    captured_at,
                    effective_from,
                    effective_until,
                    expires_at,
                    confidence,
                    trust_level,
                    sensitivity,
                    previous,
                    record_digest,
                ),
            )
            self._conn.execute(
                "INSERT INTO bm0_memory_fts (memory_id, content) VALUES (?, ?)",
                (memory_id, public_content),
            )
            self._conn.commit()
            return SourceRevisionV1(
                source_id,
                revision,
                source_uri,
                digest,
                captured_at,
                span,
                previous,
                "active",
            )
        except Exception:
            self._conn.rollback()
            raise

    def _source_revision(self, source_id: str, revision: int) -> SourceRevisionV1:
        row = self._conn.execute(
            "SELECT source_id, revision, source_uri, content_digest, captured_at, source_span, previous_revision, lifecycle_state FROM bm0_source_revisions WHERE source_id = ? AND revision = ?",
            (source_id, revision),
        ).fetchone()
        if row is None:
            raise RuntimeError("source revision disappeared during idempotent ingest")
        return SourceRevisionV1(**dict(row))

    def delete_source(self, source_uri: str) -> bool:
        source_id = self._source_id(source_uri)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT lifecycle_state FROM bm0_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return False
            if row["lifecycle_state"] == "deleted":
                self._conn.rollback()
                return True
            revisions = self._conn.execute(
                "SELECT revision FROM bm0_source_revisions WHERE source_id = ? ORDER BY revision",
                (source_id,),
            ).fetchall()
            for revision in revisions:
                self._append_event(
                    source_id, revision["revision"], "delete", {"source_deleted": True}
                )
            self._conn.execute(
                "UPDATE bm0_sources SET lifecycle_state = 'deleted' WHERE source_id = ?",
                (source_id,),
            )
            self._conn.execute(
                "UPDATE bm0_memory_records SET lifecycle_state = 'deleted' WHERE source_id = ?",
                (source_id,),
            )
            self._conn.execute(
                "DELETE FROM bm0_memory_fts WHERE memory_id IN (SELECT memory_id FROM bm0_memory_records WHERE source_id = ?)",
                (source_id,),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def retract(self, memory_id: str) -> bool:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT lifecycle_state, source_id, source_revision FROM bm0_memory_records WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return False
            if row["lifecycle_state"] == "retracted":
                self._conn.rollback()
                return True
            self._append_event(
                row["source_id"],
                row["source_revision"],
                "retract",
                {"memory_id": memory_id},
            )
            self._conn.execute(
                "UPDATE bm0_memory_records SET lifecycle_state = 'retracted' WHERE memory_id = ?",
                (memory_id,),
            )
            self._conn.execute(
                "DELETE FROM bm0_memory_fts WHERE memory_id = ?", (memory_id,)
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def retrieve(self, query: RetrievalQueryV1) -> list[CitedRetrievalResultV1]:
        """Execute the one authoritative FTS query; no fallback or execution path exists."""
        tokens = _TOKEN_RE.findall(query.query)
        if not tokens:
            return []
        safe_fts = " AND ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
        )
        now = time.time()
        temporal = query.temporal_bound if query.temporal_bound is not None else now
        started = self._monotonic()
        deadline = started + query.timeout_ms / 1000

        def _interrupt_when_expired() -> int:
            return int(self._monotonic() > deadline)

        self._conn.set_progress_handler(_interrupt_when_expired, 1)
        try:
            rows = self._conn.execute(
                """SELECT m.memory_id, m.revision, m.content, s.source_uri, r.revision AS source_revision,
                      m.source_span, r.content_digest, bm25(bm0_memory_fts) AS score
               FROM bm0_memory_fts
               JOIN bm0_memory_records AS m ON m.memory_id = bm0_memory_fts.memory_id
               JOIN bm0_sources AS s ON s.source_id = m.source_id
               JOIN bm0_source_revisions AS r ON r.source_id = m.source_id AND r.revision = m.source_revision
               WHERE bm0_memory_fts MATCH ?
                 AND s.lifecycle_state = 'active'
                 AND s.current_revision = m.source_revision
                 AND r.lifecycle_state = 'active'
                 AND m.lifecycle_state = 'active'
                 AND m.scope = ?
                 AND m.sensitivity IN ('normal', 'public')
                 AND (m.effective_from IS NULL OR m.effective_from <= ?)
                 AND (m.effective_until IS NULL OR m.effective_until >= ?)
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
               ORDER BY score ASC, m.memory_id ASC
               LIMIT ?""",
                (safe_fts, query.scope, temporal, temporal, now, query.max_records),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if self._monotonic() > deadline:
                raise RetrievalTimeoutError(
                    "BM-0 SQLite retrieval deadline exceeded"
                ) from exc
            raise
        finally:
            self._conn.set_progress_handler(None, 0)
        latency_ms = (self._monotonic() - started) * 1000
        return [
            CitedRetrievalResultV1(
                memory_id=row["memory_id"],
                memory_revision=row["revision"],
                content=row["content"],
                source_uri=row["source_uri"],
                source_revision=row["source_revision"],
                source_span=row["source_span"],
                source_digest=row["content_digest"],
                retrieval_tier="fts",
                retrieval_score=float(row["score"]),
                latency_ms=latency_ms,
            )
            for row in rows
        ]

    def verify_citation(
        self, result: CitedRetrievalResultV1, *, temporal_bound: float | None = None
    ) -> bool:
        """Verify every cited value against current canonical eligibility."""
        now = time.time()
        temporal = temporal_bound if temporal_bound is not None else now
        row = self._conn.execute(
            """SELECT m.memory_id, m.revision AS memory_revision, m.content,
                      m.source_span, m.lifecycle_state AS memory_state,
                      m.effective_from, m.effective_until, m.expires_at,
                      m.scope, m.sensitivity, s.current_revision,
                      s.lifecycle_state AS source_state, r.revision AS source_revision,
                      r.content AS revision_content,
                      r.content_digest, r.source_span AS revision_span, r.source_uri,
                      r.lifecycle_state AS revision_state
               FROM bm0_source_revisions r JOIN bm0_memory_records m
                 ON m.source_id = r.source_id AND m.source_revision = r.revision
               JOIN bm0_sources s ON s.source_id = m.source_id
               WHERE m.memory_id = ?""",
            (result.memory_id,),
        ).fetchone()
        return bool(
            row
            and result.retrieval_tier == "fts"
            and math.isfinite(result.retrieval_score)
            and math.isfinite(result.latency_ms)
            and result.latency_ms >= 0
            and row["memory_id"] == result.memory_id
            and row["memory_revision"] == result.memory_revision
            and row["source_revision"] == result.source_revision
            and row["source_uri"] == result.source_uri
            and row["source_span"] == result.source_span == row["revision_span"]
            and row["content_digest"] == result.source_digest
            and self._digest(row["revision_content"]) == result.source_digest
            and row["revision_content"] == row["content"]
            and row["content"] == result.content
            and row["source_state"] == "active"
            and row["current_revision"] == row["source_revision"]
            and row["revision_state"] == "active"
            and row["memory_state"] == "active"
            and row["scope"] == "local"
            and row["sensitivity"] in {"normal", "public"}
            and (row["effective_from"] is None or row["effective_from"] <= temporal)
            and (row["effective_until"] is None or row["effective_until"] >= temporal)
            and (row["expires_at"] is None or row["expires_at"] > now)
        )

    def source_events(self, source_id: str) -> list[dict[str, object]]:
        """Read the append-only event ledger in deterministic event order."""
        rows = self._conn.execute(
            "SELECT event_id, source_id, revision, event_type, event_digest, created_at FROM bm0_source_events WHERE source_id = ? ORDER BY rowid",
            (source_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def memory_record(self, memory_id: str) -> MemoryRecordV1 | None:
        row = self._conn.execute(
            "SELECT * FROM bm0_memory_records WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        return MemoryRecordV1(
            memory_id=row["memory_id"],
            revision=row["revision"],
            kind=row["kind"],
            content=row["content"],
            source_revision_ref=f"{row['source_id']}:{row['source_revision']}",
            source_span=row["source_span"],
            observed_at=row["observed_at"],
            effective_from=row["effective_from"],
            effective_until=row["effective_until"],
            expires_at=row["expires_at"],
            confidence=row["confidence"],
            trust_level=row["trust_level"],
            sensitivity=row["sensitivity"],
            lifecycle_state=row["lifecycle_state"],
            supersedes_revision=row["supersedes_revision"],
            record_digest=row["record_digest"],
        )

    def rebuild_fts(self) -> None:
        """Rebuild the disposable projection from currently eligible canonical records."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("DELETE FROM bm0_memory_fts")
            self._conn.execute(
                """INSERT INTO bm0_memory_fts (memory_id, content)
                   SELECT m.memory_id, m.content FROM bm0_memory_records m
                   JOIN bm0_sources s ON s.source_id = m.source_id
                   JOIN bm0_source_revisions r ON r.source_id = m.source_id AND r.revision = m.source_revision
                   WHERE m.lifecycle_state = 'active' AND s.lifecycle_state = 'active'
                     AND s.current_revision = m.source_revision AND r.lifecycle_state = 'active'"""
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def new_query(query: str, **kwargs: object) -> RetrievalQueryV1:
        return RetrievalQueryV1(query_id=uuid4().hex, query=query, **kwargs)  # type: ignore[arg-type]


def normalized_result(result: CitedRetrievalResultV1) -> dict[str, object]:
    """Stable benchmark representation; intentionally excludes wall-clock latency."""
    return {key: value for key, value in asdict(result).items() if key != "latency_ms"}
