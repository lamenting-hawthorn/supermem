"""ChromaManager — optional vector store for semantic search (tier 3).

Only active when SUPERMEM_VECTOR=true. Degrades gracefully when chromadb
is not installed or the flag is off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from supermem.config import SUPERMEM_CHROMA_PATH, SUPERMEM_VECTOR
from supermem.logging import get_logger

log = get_logger(__name__)


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

    def init(self) -> None:
        if not self._chroma:
            return
        try:
            self._path.mkdir(parents=True, exist_ok=True)
            self._client = self._chroma.PersistentClient(path=str(self._path))
            self._collection = self._client.get_or_create_collection(
                name=self._COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
            log.info("chroma_init", path=str(self._path))
        except Exception as exc:
            log.warning("chroma_init_failed", error=str(exc))
            self._client = None
            self._collection = None

    @property
    def available(self) -> bool:
        return self._chroma is not None and self._collection is not None

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
