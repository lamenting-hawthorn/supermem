"""ChromaManager — optional vector store for semantic search (tier 3).

Only active when SUPERMEM_VECTOR=true. Degrades gracefully when chromadb
is not installed or the flag is off.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from supermem.config import (
    DEFAULT_FASTEMBED_MODEL,
    SUPERMEM_CHROMA_PATH,
    SUPERMEM_VECTOR,
    embedding_model_from_env,
    embedding_provider_from_env,
)
from supermem.logging import get_logger

log = get_logger(__name__)

# Collection metadata key under which the producing embedder's identity is
# persisted (canonical JSON string). Collections predating this key report
# provider="unknown-legacy".
_IDENTITY_KEY = "embedding_identity"
_LEGACY_IDENTITY: dict[str, Any] = {"provider": "unknown-legacy"}
_DEFAULT_IDENTITY: dict[str, Any] = {"provider": "chroma-default-onnx-minilm"}

_fallback_warned = False


def format_identity(identity: Mapping[str, Any]) -> str:
    """Canonical JSON string used for persistence and equality comparison."""
    return json.dumps(dict(identity), sort_keys=True, separators=(",", ":"))


def parse_stored_identity(raw: Any) -> dict[str, Any]:
    """Parse a persisted identity string; anything unusable reports legacy."""
    if raw:
        try:
            data = json.loads(str(raw))
            if isinstance(data, dict) and data.get("provider"):
                return data
        except (TypeError, ValueError):
            pass
    return dict(_LEGACY_IDENTITY)


def resolve_embedder(provider: str, model: str) -> tuple[dict[str, Any], Any]:
    """Resolve the active embedder. Returns ``(identity, embedding_function)``.

    Never raises: any failure constructing the requested provider logs a
    structured warning once and falls back to Chroma's default ONNX MiniLM
    (identity reflects the actual fallback).
    """
    global _fallback_warned

    if provider == "fastembed":
        try:
            from fastembed import TextEmbedding

            fe_model = TextEmbedding(**({"model_name": model} if model else {}))
            identity: dict[str, Any] = {
                "provider": "fastembed",
                "model": model or DEFAULT_FASTEMBED_MODEL,
            }
            dim = _probe_embedding_dim(fe_model)
            if dim is not None:
                identity["dim"] = dim
            return identity, _FastembedEmbeddingFunction(fe_model)
        except Exception as exc:
            if not _fallback_warned:
                log.warning(
                    "fastembed_unavailable_falling_back",
                    error=str(exc),
                    hint="Install fastembed or unset SUPERMEM_EMBEDDING_PROVIDER",
                )
                _fallback_warned = True
            else:
                log.debug("fastembed_unavailable_falling_back", error=str(exc))
    return dict(_DEFAULT_IDENTITY), None


def _probe_embedding_dim(model: Any) -> int | None:
    for attr in ("embedding_size", "dim"):
        val = getattr(model, attr, None)
        if isinstance(val, int):
            return val
    getter = getattr(model, "get_embedding_size", None)
    if callable(getter):
        try:
            return int(getter())
        except Exception:
            pass
    try:
        return len(next(iter(model.embed([""])))[0])
    except Exception:
        return None


class _FastembedEmbeddingFunction:
    """Adapter exposing fastembed's TextEmbedding via Chroma's
    embedding_function protocol (callable taking documents, returning
    lists of floats)."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def __call__(self, input: Any) -> list[list[float]]:  # noqa: A002
        docs = list(input)
        return [[float(x) for x in vec] for vec in self._model.embed(docs)]


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
        self._active_identity, self._embed_fn = resolve_embedder(
            embedding_provider_from_env(), embedding_model_from_env()
        )

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
        if not identity_a or not identity_b:
            return False
        return format_identity(identity_a) == format_identity(identity_b)

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
