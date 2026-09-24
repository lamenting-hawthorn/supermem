"""supermem_hybrid adapter — lifecycle-aware hybrid retrieval (RRF fusion).

Exercises the real product path: ``create_vector_manager()`` (auto-select:
sqlite-vec → chroma → unavailable) feeding ``HybridRetriever``'s RRF fusion
with post-fusion lifecycle filtering. Skips cleanly when no vector backend
or embedding provider is available.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import supermem.config as _config
import supermem.storage.vector as _vector_mod
from supermem.indexer.vault import VaultIndexer
from supermem.logging import get_logger
from supermem.retrieval.hybrid import HybridRetriever
from supermem.storage.database import DatabaseManager
from supermem.storage.vector import create_vector_manager

from benchmarks.adapters.supermem_fts import SupermemFtsAdapter
from benchmarks.harness_types import CitedResult

log = get_logger(__name__)


class AdapterUnavailable(RuntimeError):
    """Raised when an adapter cannot run in this environment."""


class SupermemHybridAdapter(SupermemFtsAdapter):
    """FTS + vector hybrid pipeline via the production RRF fusion path."""

    name = "supermem_hybrid"

    def __init__(self) -> None:
        super().__init__()
        self._vector: Any | None = None
        self._hybrid: HybridRetriever | None = None

    async def setup(self, workspace: Path, dataset_dir: Path) -> None:
        # Enable the vector tier inside this benchmark process. SUPERMEM_VECTOR
        # is a per-module global bound at import, so patch each manager module
        # (same convention the unit tests use).
        os.environ["SUPERMEM_VECTOR"] = "true"
        setattr(_config, "SUPERMEM_VECTOR", True)
        setattr(_vector_mod, "SUPERMEM_VECTOR", True)
        try:
            import supermem.storage.vector_sqlite as _vector_sqlite_mod

            setattr(_vector_sqlite_mod, "SUPERMEM_VECTOR", True)
        except ImportError:
            pass
        # Exercise the full product stack: enable the cross-encoder reranker so
        # the benchmark measures fused-then-reranked retrieval, not just RRF.
        # Same module-flag convention as SUPERMEM_VECTOR (bound at import).
        os.environ["SUPERMEM_RERANKER"] = "true"
        try:
            import supermem.retrieval.rerank as _rerank_mod

            setattr(_rerank_mod, "RERANKER_ENABLED", True)
        except ImportError:
            pass
        # Optional remote reranker: inject an OpenRouter /rerank scorer into
        # the product Reranker's scorer slot (built for tests). Enabled with
        # SUPERMEM_BENCH_REMOTE_RERANK=1; local fastembed otherwise.
        if os.getenv("SUPERMEM_BENCH_REMOTE_RERANK") in ("1", "true"):
            from benchmarks.adapters.openrouter_rerank import (
                openrouter_rerank_scorer,
            )

            scorer = openrouter_rerank_scorer()
            if scorer is not None:
                import supermem.retrieval.rerank as _rr2
                from supermem.retrieval.rerank import Reranker

                _rr2._reranker_singleton = Reranker(scorer=scorer)
        # Optional remote embeddings: SUPERMEM_BENCH_REMOTE_EMBED=1 routes the
        # embedder through an OpenAI-compatible endpoint (OpenRouter by
        # default). Provider env is read live at get_embedder() call time.
        if os.getenv("SUPERMEM_BENCH_REMOTE_EMBED") in ("1", "true"):
            os.environ.setdefault("SUPERMEM_EMBEDDING_PROVIDER", "local-endpoint")
            os.environ.setdefault(
                "SUPERMEM_EMBEDDING_BASE_URL", "https://openrouter.ai/api/v1"
            )
            # Paid route by default: the :free variant is throttled at the
            # upstream provider regardless of OpenRouter tier (observed as
            # connection resets mid-ingest). Override via env if needed.
            os.environ.setdefault(
                "SUPERMEM_EMBEDDING_MODEL", "nvidia/nemotron-3-embed-1b"
            )
            if os.getenv("OPENROUTER_API_KEY"):
                os.environ.setdefault(
                    "SUPERMEM_EMBEDDING_API_KEY", os.environ["OPENROUTER_API_KEY"]
                )
            # input_type is a module-level config attr read at call time.
            if os.getenv("SUPERMEM_BENCH_EMBED_INPUT_TYPE") in ("1", "true"):
                setattr(_config, "SUPERMEM_EMBEDDING_INPUT_TYPE", True)
            # Remote embedders (nemotron-2048d) run a different cosine scale —
            # measured hits ~0.6 vs noise ~0.94, so the bge-calibrated 0.35
            # floor would drop every real hit. Recalibrate the documented knob.
            floor = os.getenv("SUPERMEM_BENCH_MAX_DISTANCE")
            if floor:
                try:
                    import supermem.storage.vector_sqlite as _vs_mod

                    setattr(_config, "SUPERMEM_VECTOR_MAX_DISTANCE", float(floor))
                    setattr(_vs_mod, "SUPERMEM_VECTOR_MAX_DISTANCE", float(floor))
                except ImportError:
                    pass
        _config.SUPERMEM_VECTORS_PATH = workspace / "vectors.db"
        try:
            self._vector = create_vector_manager()
        except Exception as exc:
            raise AdapterUnavailable(f"vector backend unavailable: {exc}") from exc
        self._vector.init()
        if not getattr(self._vector, "available", False):
            raise AdapterUnavailable("vector backend reports unavailable")
        # Copy sources + init stores WITHOUT indexing, then walk exactly once
        # with the vector-enabled indexer. A second walk dead-ends on
        # index_file's mtime/last_indexed guard and never ingests vectors.
        await self._prepare(workspace, dataset_dir)
        assert isinstance(self._db, DatabaseManager)
        graph = (
            self._graph if (self._graph is not None and self._graph.available) else None
        )
        self._indexer = VaultIndexer(
            self._db, graph, vector=self._vector, vault_path=workspace
        )
        await self._indexer.walk()
        await self._stamp_observed_at(dataset_dir)
        # GraphRetriever dereferences .available, so it needs a manager object,
        # never None — an un-initialised manager simply reports unavailable.
        from supermem.storage.graph import KuzuGraphManager

        self._hybrid = HybridRetriever(
            db=self._db,
            graph=(
                self._graph
                if self._graph is not None
                else KuzuGraphManager(workspace / "graph-unused" / "g.kz")
            ),
            chroma=self._vector,
        )

    async def retrieve(self, query: str, k: int = 10) -> list[CitedResult]:
        assert (
            self._db is not None
            and self._workspace is not None
            and self._hybrid is not None
        )
        started = time.perf_counter()
        result = await self._hybrid.search(query, tier_limit=3, limit=k)
        fused_ids = result.obs_ids[:k]
        latency_ms = (time.perf_counter() - started) * 1000.0
        rows = await self._db.get_observations(fused_ids)
        by_id = {row["id"]: row for row in rows}
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
        self._hybrid = None


def hashlib_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
