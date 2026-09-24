"""ChromaManager — legacy ChromaDB vector store (optional tier 3).

Only active when SUPERMEM_VECTOR=true. Degrades gracefully when chromadb
is not installed or the flag is off. Embedding-provider selection and the
pluggable-backend factory live in supermem.storage.vector_factory; prefer
``create_vector_manager()`` at construction sites so SUPERMEM_VECTOR_BACKEND
is honored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from supermem.config import (
    SUPERMEM_CHROMA_PATH,
    SUPERMEM_VECTOR,
    embedding_model_from_env,
    embedding_provider_from_env,
)
from supermem.logging import get_logger
from supermem.storage.vector_factory import (
    UnavailableVectorManager,
    create_vector_manager,
    format_identity,
    get_embedder,
    identity_matches,
    parse_stored_identity,
)

log = get_logger(__name__)

__all__ = [
    "ChromaManager",
    "UnavailableVectorManager",
    "create_vector_manager",
    "format_identity",
    "get_embedder",
    "parse_stored_identity",
]

# Collection metadata key under which the producing embedder's identity is
# persisted (canonical JSON string). Collections predating this key report
# provider="unknown-legacy".
_IDENTITY_KEY = "embedding_identity"
_DEFAULT_IDENTITY: dict[str, Any] = {"provider": "chroma-default-onnx-minilm"}


def resolve_embedder(provider: str, model: str) -> tuple[dict[str, Any], Any]:
    """Resolve the active embedder → ``(identity, embedding_function)``.

    Chroma-specific wrapper around :func:`get_embedder`: when no explicit
    provider resolves (fastembed absent), falls back to Chroma's built-in
    ONNX MiniLM with ``embed_fn=None``.
    """
    resolved = get_embedder(provider, model)
    if resolved is None:
        return dict(_DEFAULT_IDENTITY), None
    return resolved


def _import_chroma() -> Any:
    if not SUPERMEM_VECTOR:
        return None
    try:
        import chromadb

        return chromadb
    except ImportError:
        log.warning(
            "chromadb_unavailable",
            hint="Install with: uv add 'supermem-core[vector]'",
        )
        return None


class ChromaManager:
    """
    Optional ChromaDB vector store for semantic fuzzy search.

    Disabled by default (SUPERMEM_VECTOR=false) so personal users have
    zero extra dependencies. Enable with SUPERMEM_VECTOR=true.
    """

    _COLLECTION = "supermem_memory"

    def __init__(self, db_path: Path | None = None):
        self._path = db_path or SUPERMEM_CHROMA_PATH
        self._chroma = _import_chroma()
        self._client: Any = None
        self._collection: Any = None
        if SUPERMEM_VECTOR:
            self._active_identity, self._embed_fn = resolve_embedder(
                embedding_provider_from_env(), embedding_model_from_env()
            )
        else:
            # Tier disabled: skip embedder resolution so construction never
            # triggers provider I/O or a local model download.
            self._active_identity, self._embed_fn = {}, None

    def init(self) -> None:
        if not self._chroma:
            return
        try:
            self._path.mkdir(parents=True, exist_ok=True)
            self._client = self._chroma.PersistentClient(path=str(self._path))
            metadata: dict[str, Any] = {
                "hnsw:space": "cosine",
                _IDENTITY_KEY: format_identity(self._active_identity),
            }
            if self._embed_fn is not None:
                self._collection = self._client.get_or_create_collection(
                    name=self._COLLECTION,
                    metadata=metadata,
                    embedding_function=self._embed_fn,
                )
            else:
                self._collection = self._client.get_or_create_collection(
                    name=self._COLLECTION,
                    metadata=metadata,
                )
            stored = parse_stored_identity(
                (getattr(self._collection, "metadata", None) or {}).get(_IDENTITY_KEY)
            )
            if stored.get("provider") == "unknown-legacy":
                log.warning(
                    "chroma_legacy_collection_no_identity",
                    path=str(self._path),
                )
            elif stored != self._active_identity:
                log.warning(
                    "embedding_identity_mismatch",
                    stored=format_identity(stored),
                    active=format_identity(self._active_identity),
                )
            log.info("chroma_init", path=str(self._path))
        except Exception as exc:
            log.warning("chroma_init_failed", error=str(exc))
            self._client = None
            self._collection = None

    @property
    def available(self) -> bool:
        return self._chroma is not None and self._collection is not None

    @property
    def active_identity(self) -> dict[str, Any]:
        """Identity of the embedder this manager would embed with."""
        return dict(self._active_identity)

    def embedding_identity(self) -> dict[str, Any]:
        """Identity of the embedder that produced the collection's vectors.

        Reads persisted collection metadata; collections without the key
        (or with an unparseable value) report ``unknown-legacy``. When no
        collection is open, reports the resolved active identity.
        """
        if self._collection is not None:
            meta = getattr(self._collection, "metadata", None) or {}
            return parse_stored_identity(meta.get(_IDENTITY_KEY))
        return dict(self._active_identity)

    @classmethod
    def embedding_matches(
        cls,
        identity_a: Mapping[str, Any] | None,
        identity_b: Mapping[str, Any] | None,
    ) -> bool:
        """True when both identities are present and canonically equal.

        Seam for callers that must detect mismatched collections before
        mixing vectors from different embedding models.
        """
        return identity_matches(identity_a, identity_b)

    async def upsert_chunks(
        self,
        chunks: list[str],
        *,
        obs_id: int | None = None,
        source_uri: str | None = None,
    ) -> None:
        """Store text chunks, tagged with an optional observation ID / source URI.

        Each chunk carries ``{"chunk_index": n}`` plus ``source_uri`` and/or
        ``obs_id`` when provided, so results can be mapped back to a source
        and cleaned up on deletion via :meth:`delete_by_source`.
        """
        if not self.available or not chunks:
            return
        try:
            ids: list[str] = []
            metadatas: list[dict[str, Any]] = []
            for i, chunk in enumerate(chunks):
                meta: dict[str, Any] = {"chunk_index": i}
                if source_uri is not None:
                    meta["source_uri"] = source_uri
                if obs_id is not None:
                    meta["obs_id"] = obs_id
                key = f"{source_uri or 'chunk'}_{i}"
                ids.append(key)
                metadatas.append(meta)
            self._collection.upsert(documents=chunks, ids=ids, metadatas=metadatas)
        except Exception as exc:
            log.warning(
                "chroma_upsert_failed",
                obs_id=obs_id,
                source_uri=source_uri,
                error=str(exc),
            )

    async def search(self, query: str, limit: int = 10) -> list[tuple[int, float]]:
        """Semantic search. Returns ``(obs_id, distance)`` ranked by relevance.

        Distance is the cosine distance returned by Chroma (lower = closer).
        """
        if not self.available:
            return []
        try:
            n = min(limit, self._collection.count() or 1)
            results = self._collection.query(
                query_texts=[query],
                n_results=n,
                include=["metadatas", "distances"],
            )
            meta_lists = results.get("metadatas", []) or []
            dist_lists = results.get("distances", []) or []
            out: list[tuple[int, float]] = []
            seen: set[int] = set()
            for i, meta_list in enumerate(meta_lists):
                dist_list = (
                    dist_lists[i] if i < len(dist_lists) else [0.0] * len(meta_list)
                )
                for meta, dist in zip(meta_list, dist_list):
                    oid = meta.get("obs_id")
                    if oid is None:
                        continue
                    oid = int(oid)
                    if oid in seen:
                        continue
                    seen.add(oid)
                    out.append((oid, float(dist)))
            return out
        except Exception as exc:
            log.warning("chroma_search_failed", error=str(exc))
            return []

    async def delete_by_source(self, source_uri: str) -> None:
        """Remove all vectors whose metadata ``source_uri`` matches."""
        if not self.available:
            return
        try:
            self._collection.delete(where={"source_uri": source_uri})
        except Exception as exc:
            log.warning(
                "chroma_delete_by_source_failed", source_uri=source_uri, error=str(exc)
            )

    async def delete_obs(self, obs_id: int) -> None:
        if not self.available:
            return
        try:
            self._collection.delete(where={"obs_id": obs_id})
        except Exception as exc:
            log.warning("chroma_delete_failed", obs_id=obs_id, error=str(exc))
