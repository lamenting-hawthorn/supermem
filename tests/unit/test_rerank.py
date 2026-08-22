"""Unit tests for the flag-gated local cross-encoder reranker.

Covers: Reranker score-sort logic (injected scorer), graceful passthrough
when sentence_transformers is unavailable, the hybrid.py RRF-path wiring
(fake reranker reorders fused candidates, lifecycle set preserved, default
OFF), and rerank input capping.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from supermem.core.retriever import RetrievalResult
from supermem.retrieval.hybrid import HybridRetriever
from supermem.retrieval.rerank import Reranker, get_reranker
from supermem.storage.database import DatabaseManager
from supermem.storage.graph import KuzuGraphManager
from supermem.storage.vector import ChromaManager

# ── Helpers ─────────────────────────────────────────────────────────────────


def _block_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import sentence_transformers`` raise ImportError."""

    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise ImportError("sentence_transformers disabled for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


class _FakeRanked:
    """Stand-in for a content-tier retriever returning a fixed ranking."""

    def __init__(self, tier: int, ranked: list[int]):
        self._tier = tier
        self._ranked = ranked

    @property
    def tier(self) -> int:
        return self._tier

    @property
    def available(self) -> bool:
        return True

    async def search(self, query: str, limit: int = 10) -> RetrievalResult:
        await asyncio.sleep(0)
        return RetrievalResult(obs_ids=self._ranked[:limit], source_tier=self._tier)


class _RecordingStub:
    """Fake reranker that records what it was fed and reorders deterministically."""

    def __init__(self, reverse: bool = True):
        self.reverse = reverse
        self.seen_candidates: list = []

    @property
    def available(self) -> bool:
        return True

    async def rerank(self, query: str, candidates: list) -> list:
        self.seen_candidates = list(candidates)
        return list(reversed(candidates)) if self.reverse else list(candidates)


# ── Fixtures (mirror test_hybrid.py conventions) ─────────────────────────────


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(tmp_path / "rerank_test.db")
    await d.init()
    yield d
    await d.close()


@pytest.fixture
def graph(tmp_path: Path) -> KuzuGraphManager:
    g = KuzuGraphManager(tmp_path / "graph")
    g.init()
    return g


@pytest.fixture
def chroma() -> ChromaManager:
    return ChromaManager()


@pytest_asyncio.fixture
async def retriever(
    db: DatabaseManager, graph: KuzuGraphManager, chroma: ChromaManager
) -> HybridRetriever:
    return HybridRetriever(db=db, graph=graph, chroma=chroma)


# ── Reranker.score-sort logic (injected scorer) ──────────────────────────────


@pytest.mark.asyncio
async def test_rerank_sorts_descending_by_score_with_injected_scorer() -> None:
    reranker = Reranker(scorer=lambda pairs: [len(content) for _q, content in pairs])
    candidates = [
        {"id": 1, "content": "aa"},
        {"id": 2, "content": "aaaa"},
        {"id": 3, "content": "a"},
    ]

    result = await reranker.rerank("query", candidates)

    assert [c["id"] for c in result] == [2, 1, 3]
    assert [c["rerank_score"] for c in result] == [4.0, 2.0, 1.0]


@pytest.mark.asyncio
async def test_rerank_preserves_metadata_and_attaches_score() -> None:
    reranker = Reranker(scorer=lambda pairs: [i for i in range(len(pairs), 0, -1)])
    candidates = [
        {"id": i, "content": f"doc {i}", "metadata": {"source_tier": 1}}
        for i in range(3)
    ]

    result = await reranker.rerank("query", candidates)

    assert [c["id"] for c in result] == [0, 1, 2]
    for candidate in result:
        assert candidate["metadata"]["source_tier"] == 1
        assert "rerank_score" in candidate["metadata"]
        assert candidate["rerank_score"] == candidate["metadata"]["rerank_score"]


@pytest.mark.asyncio
async def test_rerank_stable_for_ties() -> None:
    reranker = Reranker(scorer=lambda pairs: [1.0] * len(pairs))
    candidates = [
        {"id": 1, "content": "tie a"},
        {"id": 2, "content": "tie b"},
        {"id": 3, "content": "tie c"},
    ]

    result = await reranker.rerank("query", candidates)

    # Equal scores must preserve input order (stable sort).
    assert [c["id"] for c in result] == [1, 2, 3]


@pytest.mark.asyncio
async def test_rerank_empty_candidates_returns_empty() -> None:
    reranker = Reranker(scorer=lambda pairs: [])
    assert await reranker.rerank("query", []) == []


# ── Unavailability / failure passthrough ─────────────────────────────────────


def test_reranker_unavailable_without_sentence_transformers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_sentence_transformers(monkeypatch)
    reranker = Reranker()
    assert reranker.available is False


@pytest.mark.asyncio
async def test_rerank_passthrough_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_sentence_transformers(monkeypatch)
    reranker = Reranker()
    candidates = [
        {"id": 1, "content": "alpha"},
        {"id": 2, "content": "beta"},
    ]

    result = await reranker.rerank("query", candidates)

    assert result == candidates
    assert "rerank_score" not in result[0]


@pytest.mark.asyncio
async def test_rerank_never_raises_on_scorer_error() -> None:
    def boom(_pairs):
        raise RuntimeError("scorer exploded")

    reranker = Reranker(scorer=boom)
    candidates = [{"id": 1, "content": "alpha"}]

    result = await reranker.rerank("query", candidates)

    assert result == candidates
    assert "rerank_score" not in result[0]


@pytest.mark.asyncio
async def test_rerank_returns_unchanged_on_score_mismatch() -> None:
    reranker = Reranker(scorer=lambda pairs: [1.0])  # wrong length
    candidates = [
        {"id": 1, "content": "alpha"},
        {"id": 2, "content": "beta"},
    ]

    result = await reranker.rerank("query", candidates)

    assert result == candidates


# ── Hybrid RRF-path wiring (fake reranker) ───────────────────────────────────


@pytest.mark.asyncio
async def test_hybrid_applies_fake_reranker_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    db: DatabaseManager,
    retriever: HybridRetriever,
) -> None:
    ids = {await db.write_observation(f"rrf rerank doc {i}"): i for i in range(4)}
    a, b, c, d = ids
    retriever._fts = _FakeRanked(1, [a, b, c])
    retriever._vector = _FakeRanked(3, [c, d])

    import supermem.retrieval.rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "RERANKER_ENABLED", True)
    monkeypatch.setattr(rerank_mod, "get_reranker", lambda: _RecordingStub())

    result = await retriever.search("rrf rerank", tier_limit=3)

    # Fused order [c, a, b, d] → stub reverses → [d, b, a, c].
    assert result.obs_ids == [d, b, a, c]
    assert result.metadata["tiers"] == ["fts", "vector", "rerank"]
    assert result.source_tier == 3
    assert len(result.obs_ids) == len(set(result.obs_ids))


@pytest.mark.asyncio
async def test_rerank_preserves_lifecycle_filtered_set(
    monkeypatch: pytest.MonkeyPatch,
    db: DatabaseManager,
    retriever: HybridRetriever,
) -> None:
    retracted = await db.write_observation("retracted rerank candidate")
    active_one = await db.write_observation("active rerank candidate one")
    active_two = await db.write_observation("active rerank candidate two")
    await db.retract_observation(retracted, reason="superseded")
    retriever._fts = _FakeRanked(1, [retracted, active_one, active_two])

    import supermem.retrieval.rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "RERANKER_ENABLED", True)
    monkeypatch.setattr(rerank_mod, "get_reranker", lambda: _RecordingStub())

    result = await retriever.search("rerank candidate", tier_limit=1)

    # Retracted doc ranked FIRST by FTS must still never surface after rerank.
    assert retracted not in result.obs_ids
    assert result.obs_ids == [active_two, active_one]


@pytest.mark.asyncio
async def test_hybrid_default_off_skips_reranker(
    monkeypatch: pytest.MonkeyPatch,
    db: DatabaseManager,
    retriever: HybridRetriever,
) -> None:
    first = await db.write_observation("default off top hit")
    second = await db.write_observation("default off runner up")
    retriever._fts = _FakeRanked(1, [second, first])

    import supermem.retrieval.rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "RERANKER_ENABLED", False)

    def boom():
        raise AssertionError("reranker must not be invoked when disabled")

    monkeypatch.setattr(rerank_mod, "get_reranker", boom)

    result = await retriever.search("default off", tier_limit=1)

    assert result.obs_ids == [second, first]
    assert "rerank" not in result.metadata["tiers"]
    assert result.metadata["tiers"] == ["fts"]


@pytest.mark.asyncio
async def test_rerank_input_capped_at_top_fifty(
    monkeypatch: pytest.MonkeyPatch,
    db: DatabaseManager,
    retriever: HybridRetriever,
) -> None:
    many = [await db.write_observation(f"rerank capped doc {i}") for i in range(60)]
    retriever._fts = _FakeRanked(1, many)
    stub = _RecordingStub(reverse=False)

    import supermem.retrieval.rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "RERANKER_ENABLED", True)
    monkeypatch.setattr(rerank_mod, "get_reranker", lambda: stub)

    result = await retriever.search("rerank capped", tier_limit=1, limit=60)

    # Only the top RERANK_CAP (50) fused ids reach the reranker; the rest
    # keep fused order behind them and the final limit is respected.
    assert len(stub.seen_candidates) == 50
    assert result.obs_ids == many
    assert "rerank" in result.metadata["tiers"]


# ── Singleton ────────────────────────────────────────────────────────────────


def test_get_reranker_returns_singleton() -> None:
    assert get_reranker() is get_reranker()
