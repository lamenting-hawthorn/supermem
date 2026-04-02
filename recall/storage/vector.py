"""ChromaManager — optional vector store for semantic search (tier 3).

Only active when RECALL_VECTOR=true. Degrades gracefully when chromadb
is not installed or the flag is off.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from recall.config import RECALL_CHROMA_PATH, RECALL_VECTOR
from recall.logging import get_logger

log = get_logger(__name__)


def _import_chroma() -> Any:
    if not RECALL_VECTOR:
        return None
    try:
        import chromadb
        return chromadb
    except ImportError:
        log.warning(
            "chromadb_unavailable",
            hint="Install with: uv add 'recall-core[vector]'",
        )
        return None


class ChromaManager:
    """
    Optional ChromaDB vector store for semantic fuzzy search.

    Disabled by default (RECALL_VECTOR=false) so personal users have
    zero extra dependencies. Enable with RECALL_VECTOR=true.
    """

    _COLLECTION = "recall_memory"

    def __init__(self, db_path: Path | None = None):
        self._path = db_path or RECALL_CHROMA_PATH
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

    async def upsert_chunks(self, obs_id: int, chunks: list[str]) -> None:
        """Store text chunks associated with an observation ID."""
        if not self.available or not chunks:
            return
        try:
            ids = [f"obs_{obs_id}_{i}" for i in range(len(chunks))]
            metadatas = [{"obs_id": obs_id} for _ in chunks]
            self._collection.upsert(documents=chunks, ids=ids, metadatas=metadatas)
        except Exception as exc:
            log.warning("chroma_upsert_failed", obs_id=obs_id, error=str(exc))

    async def search(self, query: str, limit: int = 10) -> list[int]:
        """Semantic search. Returns observation IDs ranked by cosine similarity."""
        if not self.available:
            return []
        try:
            n = min(limit, self._collection.count() or 1)
            results = self._collection.query(
                query_texts=[query],
                n_results=n,
                include=["metadatas"],
            )
            obs_ids: list[int] = []
            for meta_list in results.get("metadatas", []):
                for meta in meta_list:
                    oid = meta.get("obs_id")
                    if oid is not None and oid not in obs_ids:
                        obs_ids.append(int(oid))
            return obs_ids
        except Exception as exc:
            log.warning("chroma_search_failed", error=str(exc))
            return []

    async def delete_obs(self, obs_id: int) -> None:
        if not self.available:
            return
        try:
            self._collection.delete(where={"obs_id": obs_id})
        except Exception as exc:
            log.warning("chroma_delete_failed", obs_id=obs_id, error=str(exc))
