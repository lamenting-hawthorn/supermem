"""supermem_hybrid adapter — FTS + vector (Chroma) retrieval, experimental.

Skips cleanly when chromadb is unavailable in the environment.
"""

from __future__ import annotations

import time
from pathlib import Path

from supermem.logging import get_logger
from supermem.storage.database import DatabaseManager
from supermem.storage.vector import ChromaManager
from supermem.indexer.vault import VaultIndexer

from benchmarks.adapters.supermem_fts import SupermemFtsAdapter
from benchmarks.harness_types import CitedResult

log = get_logger(__name__)


class AdapterUnavailable(RuntimeError):
    """Raised when an adapter cannot run in this environment."""


class SupermemHybridAdapter(SupermemFtsAdapter):
    """FTS pipeline plus Chroma vector chunks merged into one ranking.

    Marked experimental per handoff 06 until vector/graph projections have
    populated-store update/delete coverage.
    """

    name = "supermem_hybrid"

    def __init__(self) -> None:
        super().__init__()
        self._vector: ChromaManager | None = None

    async def setup(self, workspace: Path, dataset_dir: Path) -> None:
        try:
            self._vector = ChromaManager(workspace / "chroma")
        except Exception as exc:
            raise AdapterUnavailable(f"chroma unavailable: {exc}") from exc
        # Parent setup builds db + graph + indexer with vector=None; rebuild the
        # indexer with our vector manager before walking.
        await super().setup(workspace, dataset_dir)
        assert isinstance(self._db, DatabaseManager)
        graph = (
            self._graph if (self._graph is not None and self._graph.available) else None
        )
        self._indexer = VaultIndexer(
            self._db, graph, vector=self._vector, vault_path=workspace
        )
        await self._indexer.walk()

    async def retrieve(self, query: str, k: int = 10) -> list[CitedResult]:
        assert self._db is not None and self._workspace is not None
        started = time.perf_counter()
        fts_ids = await self._db.fts_search(query, limit=k)
        vec_hits: list[tuple[int, float]] = []
        if self._vector is not None and self._vector.available:
            try:
                vec_hits = await self._vector.search(query, limit=k)
            except Exception as exc:
                log.warning("bench_vector_search_failed", error=str(exc))
        # Merge: FTS rank first (reciprocal-rank fusion), then vector hits not
        # already present.
        fused_ids: list[int] = list(fts_ids)
        for obs_id, _distance in vec_hits:
            if obs_id not in fused_ids:
                fused_ids.append(obs_id)
        fused_ids = fused_ids[:k]
        rows = await self._db.get_observations(fused_ids)
        by_id = {row["id"]: row for row in rows}
        latency_ms = (time.perf_counter() - started) * 1000.0
        results: list[CitedResult] = []
        for rank, oid in enumerate(fused_ids, start=1):
            row = by_id.get(oid)
            if row is None:
                continue
            source_uri = f"{row['source_id']}.md" if row["source_id"] else ""
            digest = ""
            source_path = self._workspace / source_uri if source_uri else None
            if source_path is not None and source_path.exists():
                digest = hashlib_sha256(source_path)
            results.append(
                CitedResult(
                    memory_id=str(oid),
                    memory_revision=1,
                    content=row["content"],
                    source_uri=source_uri,
                    source_revision=1,
                    source_span=f"{source_uri or 'ad-hoc'}#whole",
                    source_digest=digest,
                    retrieval_tier=self.name,
                    retrieval_score=round(1.0 / rank, 6),
                    latency_ms=latency_ms / max(len(fused_ids), 1),
                )
            )
        return results

    async def teardown(self) -> None:
        await super().teardown()
        self._vector = None


def hashlib_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
