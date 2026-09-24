"""SqliteVecManager — default vector store backed by the sqlite-vec extension.

Stores embeddings in a ``vec0`` virtual table inside a dedicated
``vectors.db`` beside the main supermem.db (a separate file keeps the
lifecycle DB lean; one backup of ~/.supermem still covers both). Chunk texts
live in a plain ``chunks`` table and are joined back by rowid, so vectors and
texts never duplicate storage in the vec table.

Concurrency / extension loading: sqlite-vec is loaded per connection via
``enable_load_extension`` + ``load_extension(sqlite_vec.loadable_path())``.
aiosqlite can proxy those calls, but to keep semantics explicit and avoid
cross-thread connection reuse every operation opens a short-lived sync
sqlite3 connection inside ``asyncio.to_thread``. This trades negligible
connect overhead for simple, correct threading (each connection is created,
used, and closed on the same worker thread) while keeping the public API
fully async — matching ChromaManager's surface.

Distances are cosine distance (1 − cosine similarity, lower = closer),
computed with ``vec_distance_cosine`` so semantics match the legacy Chroma
backend's ``hnsw:space=cosine``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from supermem.config import SUPERMEM_VECTOR, SUPERMEM_VECTOR_MAX_DISTANCE
from supermem.logging import get_logger
from supermem.storage.vector_factory import (
    format_identity,
    get_embedder,
    identity_matches,
    parse_stored_identity,
)

log = get_logger(__name__)

_IDENTITY_KEY = "embedding_identity"
_DIM_KEY = "embedding_dim"


def _extension_loadable_path() -> str:
    """Path sqlite-vec's loadable extension actually lives at.

    ``sqlite_vec.loadable_path()`` returns ``<pkg>/vec0``; on macOS the file
    is shipped as ``vec0.dylib`` (SQLite's loader appends the platform
    suffix itself). Pick the existing artifact so the pre-flight check in
    :meth:`SqliteVecManager.init` does not reject a perfectly loadable path.
    """
    import sqlite_vec

    base = sqlite_vec.loadable_path()
    for candidate in (base, f"{base}.dylib", f"{base}.so", f"{base}.dll"):
        if Path(candidate).exists():
            return str(candidate)
    return str(base)


class VectorDimMismatchError(ValueError):
    """Raised when a write carries vectors of a dimension different from
    the one the existing store was created with."""


class SqliteVecManager:
    """Optional sqlite-vec vector store; same public surface as ChromaManager.

    Disabled by default (SUPERMEM_VECTOR=false); while disabled the embedder
    is never resolved, so construction performs no provider I/O or model
    load/download. Additionally requires an embedding provider: unlike Chroma
    there is no built-in embedder, so with no fastembed install and no
    local-endpoint configured the manager reports available=False.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        embedder: tuple[dict[str, Any], Any] | None = None,
    ):
        from supermem.storage.vector_factory import default_vectors_db_path

        self._path = Path(db_path) if db_path is not None else default_vectors_db_path()
        if embedder is not None:
            identity, fn = embedder
            self._active_identity: dict[str, Any] | None = dict(identity)
            self._embed_fn: Callable[[Any], list[list[float]]] | None = fn
        elif SUPERMEM_VECTOR:
            self._active_identity, self._embed_fn = get_embedder() or (None, None)
        else:
            # Tier disabled: skip embedder resolution entirely so construction
            # never triggers provider I/O or a local model download.
            self._active_identity, self._embed_fn = None, None
        self._ext_path: str | None = None
        self._initialized = False
        self._stored_identity: dict[str, Any] | None = None
        self._stored_dim: int | None = None
        self._lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def init(self) -> None:
        """Import/verify the extension, create the meta schema, read any
        persisted embedding identity. Never raises."""
        try:
            self._ext_path = _extension_loadable_path()
        except Exception as exc:
            log.warning(
                "sqlite_vec_unavailable",
                error=str(exc),
                hint="Install with: uv add sqlite-vec",
            )
            return

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS vec_meta("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                row = conn.execute(
                    "SELECT value FROM vec_meta WHERE key=?", (_IDENTITY_KEY,)
                ).fetchone()
                dim_row = conn.execute(
                    "SELECT value FROM vec_meta WHERE key=?", (_DIM_KEY,)
                ).fetchone()
                conn.commit()
            self._stored_identity = (
                parse_stored_identity(row[0]) if row is not None else None
            )
            try:
                self._stored_dim = int(dim_row[0]) if dim_row is not None else None
            except (TypeError, ValueError):
                self._stored_dim = None
            self._initialized = True
            log.info(
                "sqlite_vec_init",
                path=str(self._path),
                stored_identity=(
                    format_identity(self._stored_identity)
                    if self._stored_identity
                    else None
                ),
            )
            if (
                SUPERMEM_VECTOR
                and self._stored_identity is not None
                and not identity_matches(self._stored_identity, self.active_identity)
            ):
                log.warning(
                    "embedding_identity_mismatch",
                    stored=format_identity(self._stored_identity),
                    active=format_identity(self.active_identity or {}),
                )
        except Exception as exc:
            log.warning("sqlite_vec_init_failed", path=str(self._path), error=str(exc))
            self._initialized = False

    @property
    def available(self) -> bool:
        # Module-global lookup so tests can patch the flag per-module.
        return bool(
            SUPERMEM_VECTOR and self._initialized and self._embed_fn is not None
        )

    @property
    def active_identity(self) -> dict[str, Any]:
        """Identity of the embedder this manager would embed with."""
        return dict(self._active_identity) if self._active_identity else {}

    def embedding_identity(self) -> dict[str, Any]:
        """Identity of the embedder that produced the stored vectors.

        Reads the persisted vec_meta record (which includes ``dim`` once the
        first write happened); before that, reports the resolved active
        identity.
        """
        if self._stored_identity is not None:
            return dict(self._stored_identity)
        return self.active_identity

    @staticmethod
    def embedding_matches(
        identity_a: dict[str, Any] | None,
        identity_b: dict[str, Any] | None,
    ) -> bool:
        return identity_matches(identity_a, identity_b)

    # ── Connection handling ──────────────────────────────────────────────────

    def _new_connection(self) -> sqlite3.Connection:
        """Open a fresh connection with the sqlite-vec extension loaded.

        Short-lived by design: created, used, and closed on the same
        ``asyncio.to_thread`` worker thread (see module docstring).
        """
        if self._ext_path is None:
            raise RuntimeError("init() must succeed before touching vectors.db")
        conn = sqlite3.connect(str(self._path))
        try:
            conn.enable_load_extension(True)
            conn.load_extension(self._ext_path)
            conn.enable_load_extension(False)
        except Exception:
            conn.close()
            raise
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection, dim: int) -> None:
        """Create vec_chunks/chunks on first write; enforce stored dim."""
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
        ).fetchone()
        if exists:
            if self._stored_dim is not None and self._stored_dim != dim:
                raise VectorDimMismatchError(
                    f"vector store at {self._path} has dim={self._stored_dim}, "
                    f"got {dim}"
                )
            return
        conn.execute(
            f"CREATE VIRTUAL TABLE vec_chunks USING vec0("
            f"embedding float[{dim}], "
            f"+source_uri TEXT, +obs_id INT, +chunk_index INT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks("
            "id INTEGER PRIMARY KEY, "
            "source_uri TEXT NOT NULL, "
            "obs_id INT, "
            "chunk_index INT NOT NULL, "
            "text TEXT NOT NULL, "
            "vec_rowid INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_uri)"
        )
        identity_with_dim = dict(self._active_identity or {})
        identity_with_dim["dim"] = dim
        conn.executemany(
            "INSERT OR REPLACE INTO vec_meta(key, value) VALUES (?, ?)",
            [
                (_IDENTITY_KEY, format_identity(identity_with_dim)),
                (_DIM_KEY, str(dim)),
            ],
        )
        self._stored_identity = identity_with_dim
        self._stored_dim = dim
        log.info("sqlite_vec_store_created", path=str(self._path), dim=dim)

    def _write_sync(
        self,
        chunks: list[str],
        vectors: list[list[float]],
        obs_id: int | None,
        source_uri: str | None,
    ) -> None:
        import sqlite_vec

        dim = len(vectors[0])
        if any(len(v) != dim for v in vectors):
            raise VectorDimMismatchError("inconsistent vector dims within batch")
        with self._lock, self._connection() as conn:
            try:
                self._ensure_schema(conn, dim)
                if source_uri is not None:
                    old_ids = [
                        r[0]
                        for r in conn.execute(
                            "SELECT vec_rowid FROM chunks WHERE source_uri=?",
                            (source_uri,),
                        )
                    ]
                    conn.executemany(
                        "DELETE FROM vec_chunks WHERE rowid=?",
                        [(vid,) for vid in old_ids],
                    )
                    conn.execute("DELETE FROM chunks WHERE source_uri=?", (source_uri,))
                for i, (text, vec) in enumerate(zip(chunks, vectors)):
                    cur = conn.execute(
                        "INSERT INTO vec_chunks(source_uri, obs_id, chunk_index,"
                        " embedding) VALUES (?, ?, ?, ?)",
                        (
                            source_uri,
                            obs_id,
                            i,
                            sqlite_vec.serialize_float32(vec),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO chunks(source_uri, obs_id, chunk_index, text,"
                        " vec_rowid) VALUES (?, ?, ?, ?, ?)",
                        (source_uri, obs_id, i, text, cur.lastrowid),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ── Public API (mirrors ChromaManager) ───────────────────────────────────

    async def upsert_chunks(
        self,
        chunks: list[str],
        *,
        obs_id: int | None = None,
        source_uri: str | None = None,
    ) -> None:
        """Embed and store text chunks keyed by ``(source_uri, chunk_index)``.

        Re-upserting a source replaces its previous rows atomically.
        Raises :class:`VectorDimMismatchError` (a ValueError) when the store
        was created with a different embedding dimension; other failures are
        logged and swallowed for graceful degradation.
        """
        if not self.available or not chunks:
            return
        assert self._embed_fn is not None
        try:
            vectors = await asyncio.to_thread(self._embed_fn, list(chunks))
            await asyncio.to_thread(
                self._write_sync, list(chunks), vectors, obs_id, source_uri
            )
        except VectorDimMismatchError:
            raise
        except Exception as exc:
            log.warning(
                "sqlite_vec_upsert_failed",
                obs_id=obs_id,
                source_uri=source_uri,
                error=str(exc),
            )

    async def upsert_many(
        self, items: list[tuple[list[str], int | None, str | None]]
    ) -> None:
        """Bulk variant of ``upsert_chunks`` for vault-scale ingestion.

        ``items`` are ``(chunks, obs_id, source_uri)`` triples. Embeds the
        flattened chunk list in batches so bulk indexing amortizes model
        forward-pass setup instead of paying per-file call overhead; writes
        stay per-source so re-upsert replacement semantics are unchanged.
        """
        if not self.available:
            return
        assert self._embed_fn is not None
        flat: list[str] = []
        spans: list[tuple[int, int, int | None, str | None]] = []
        for chunks, obs_id, source_uri in items:
            if not chunks:
                continue
            start = len(flat)
            flat.extend(chunks)
            spans.append((start, len(flat), obs_id, source_uri))
        if not flat:
            return
        try:
            vectors: list[list[float]] = []
            for i in range(0, len(flat), 64):
                vectors.extend(
                    await asyncio.to_thread(self._embed_fn, flat[i : i + 64])
                )
            for start, end, obs_id, source_uri in spans:
                await asyncio.to_thread(
                    self._write_sync,
                    flat[start:end],
                    vectors[start:end],
                    obs_id,
                    source_uri,
                )
        except VectorDimMismatchError:
            raise
        except Exception as exc:
            log.warning("sqlite_vec_upsert_many_failed", error=str(exc))

    async def search(
        self,
        query: str,
        limit: int = 10,
        *,
        max_distance: float | None = None,
    ) -> list[tuple[int, float]]:
        """KNN search → ``(obs_id, cosine_distance)`` ranked best-first.

        One result per observation (best chunk wins). Cosine distance is
        computed via ``vec_distance_cosine`` (lower = closer), matching the
        legacy Chroma backend's cosine space. Hits worse than
        ``max_distance`` are dropped — KNN otherwise always returns top-k
        nearest rows, even for out-of-scope queries. ``None`` reads the
        SUPERMEM_VECTOR_MAX_DISTANCE config (0.35 default); pass
        ``float("inf")`` to bypass the floor for a single call.
        """
        if not self.available:
            return []
        assert self._embed_fn is not None
        cutoff = SUPERMEM_VECTOR_MAX_DISTANCE if max_distance is None else max_distance
        # Asymmetric retrieval: providers that distinguish query vs passage
        # embeddings (fastembed's query_embed applies the model's retrieval
        # prefix, e.g. BGE) must embed queries on the query path.
        embed_query = getattr(self._embed_fn, "query_embed", self._embed_fn)
        try:
            qvecs = await asyncio.to_thread(embed_query, [query])
            return await asyncio.to_thread(self._search_sync, qvecs[0], limit, cutoff)
        except Exception as exc:
            log.warning("sqlite_vec_search_failed", error=str(exc))
            return []

    def _search_sync(
        self, qvec: list[float], limit: int, max_distance: float | None
    ) -> list[tuple[int, float]]:
        import sqlite_vec

        fetch_k = max(int(limit), 1)
        if max_distance is not None:
            # Over-fetch: obs-level dedup + the distance floor can still
            # yield up to `limit` rows.
            fetch_k = max(fetch_k * 3, 10)
        with self._lock, self._connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute(
                "SELECT c.obs_id, vec_distance_cosine(v.embedding, ?) AS dist "
                "FROM vec_chunks v JOIN chunks c ON c.vec_rowid = v.rowid "
                "WHERE v.embedding MATCH ? AND k = ? ORDER BY dist",
                (
                    sqlite_vec.serialize_float32(qvec),
                    sqlite_vec.serialize_float32(qvec),
                    fetch_k,
                ),
            ).fetchall()
        out: list[tuple[int, float]] = []
        seen: set[int] = set()
        for oid, dist in rows:
            if max_distance is not None and float(dist) > max_distance:
                # Rows are distance-ordered — everything after is worse.
                break
            if oid is None or int(oid) in seen:
                continue
            seen.add(int(oid))
            out.append((int(oid), float(dist)))
            if len(out) >= int(limit):
                break
        return out

    async def delete_by_source(self, source_uri: str) -> None:
        """Remove all vectors + chunk rows whose source_uri matches."""
        await self._delete_sync("source_uri", source_uri)

    async def delete_obs(self, obs_id: int) -> None:
        """Remove all vectors + chunk rows whose obs_id matches."""
        await self._delete_sync("obs_id", obs_id)

    async def _delete_sync(self, column: str, value: Any) -> None:
        if not self.available:
            return
        try:
            await asyncio.to_thread(self._delete_impl, column, value)
        except Exception as exc:
            log.warning("sqlite_vec_delete_failed", column=column, error=str(exc))

    def _delete_impl(self, column: str, value: Any) -> None:
        with self._lock, self._connection() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    f"SELECT vec_rowid FROM chunks WHERE {column}=?", (value,)
                )
            ]
            conn.executemany(
                "DELETE FROM vec_chunks WHERE rowid=?", [(vid,) for vid in ids]
            )
            conn.execute(f"DELETE FROM chunks WHERE {column}=?", (value,))
            conn.commit()
