"""Unit tests for the sqlite-vec vector backend (SqliteVecManager).

All tests use a deterministic FAKE embedder injected via the constructor —
no real embeddings, no network, no model downloads.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

import supermem.storage.vector_sqlite as vector_sqlite_mod
from supermem.storage.vector_sqlite import SqliteVecManager, VectorDimMismatchError

pytest.importorskip("sqlite_vec", reason="sqlite-vec not installed")

try:
    import fastembed  # noqa: F401

    HAS_FASTEMBED = True
except ImportError:
    HAS_FASTEMBED = False


DIM = 4
# Unit-ish basis vectors → cosine distances are exactly 0 or 1.
_VOCAB: dict[str, list[float]] = {
    "camera": [1.0, 0.0, 0.0, 0.0],
    "sony": [0.0, 1.0, 0.0, 0.0],
    "trip": [0.0, 0.0, 1.0, 0.0],
    "tokyo": [0.0, 0.0, 0.0, 1.0],
}


class FakeEmbedder:
    """Deterministic token-bag embedder over a fixed vocabulary."""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim

    def __call__(self, input: Any) -> list[list[float]]:  # noqa: A002
        out: list[list[float]] = []
        for doc in input:
            acc = [0.0] * self.dim
            for tok in str(doc).lower().split():
                vec = _VOCAB.get(tok)
                if vec:
                    for j in range(min(self.dim, len(vec))):
                        acc[j] += vec[j]
            out.append(acc)
        return out


IDENTITY = {"provider": "fake", "model": "fake-model"}


def make_manager(path: Path, dim: int = DIM) -> SqliteVecManager:
    return SqliteVecManager(
        db_path=path,
        embedder=(dict(IDENTITY), FakeEmbedder(dim=dim)),
    )


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vector_sqlite_mod, "SUPERMEM_VECTOR", True)


def raw_rows(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    """Read the plain chunks table WITHOUT loading the extension."""
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ── Availability semantics ───────────────────────────────────────────────────


def test_unavailable_without_flag(tmp_path: Path) -> None:
    mgr = make_manager(tmp_path / "vectors.db")
    mgr.init()
    assert mgr.available is False
    assert mgr.active_identity["provider"] == "fake"


@pytest.mark.skipif(
    HAS_FASTEMBED, reason="fastembed installed; default-provider path differs"
)
def test_no_provider_reports_unavailable(tmp_path: Path) -> None:
    """No injected embedder + no fastembed + no endpoint → honest unavailability."""
    import supermem.storage.vector_factory as factory

    assert factory.get_embedder("", "") is None
    mgr = SqliteVecManager(db_path=tmp_path / "vectors.db")
    mgr.init()
    assert mgr.available is False


# ── Upsert → search roundtrip ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_search_roundtrip_and_ordering(
    enabled: None, tmp_path: Path
) -> None:
    db = tmp_path / "vectors.db"
    mgr = make_manager(db)
    mgr.init()
    assert mgr.available

    await mgr.upsert_chunks(["camera"], obs_id=7, source_uri="a.md")
    await mgr.upsert_chunks(["tokyo trip"], obs_id=8, source_uri="b.md")

    results = await mgr.search("camera", limit=5)
    assert [oid for oid, _ in results][0] == 7
    # Known vectors: query == chunk of obs 7 (distance 0), orthogonal to obs 8.
    dists = {oid: d for oid, d in results}
    assert dists[7] == pytest.approx(0.0, abs=1e-5)
    assert dists[8] == pytest.approx(1.0, abs=1e-5)


@pytest.mark.asyncio
async def test_search_dedupes_obs_ids_best_chunk_wins(
    enabled: None, tmp_path: Path
) -> None:
    mgr = make_manager(tmp_path / "vectors.db")
    mgr.init()
    await mgr.upsert_chunks(["camera", "tokyo trip"], obs_id=42, source_uri="mixed.md")
    results = await mgr.search("camera", limit=10)
    assert [oid for oid, _ in results].count(42) == 1
    assert results[0] == (42, pytest.approx(0.0, abs=1e-5))


@pytest.mark.asyncio
async def test_distance_ordering_across_sources(enabled: None, tmp_path: Path) -> None:
    mgr = make_manager(tmp_path / "vectors.db")
    mgr.init()
    await mgr.upsert_chunks(["tokyo"], obs_id=1, source_uri="j.md")
    await mgr.upsert_chunks(["sony camera"], obs_id=2, source_uri="c.md")
    results = await mgr.search("camera", limit=2)
    # Exact-match source first; orthogonal second with distance ~1.
    assert [oid for oid, _ in results] == [2, 1]
    assert results[1][1] == pytest.approx(1.0, abs=1e-5)


# ── Replace-on-reupsert semantics ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reupsert_replaces_previous_source_rows(
    enabled: None, tmp_path: Path
) -> None:
    db = tmp_path / "vectors.db"
    mgr = make_manager(db)
    mgr.init()
    await mgr.upsert_chunks(["one two", "three four"], obs_id=5, source_uri="a.md")
    assert len(raw_rows(db, "SELECT id FROM chunks WHERE source_uri='a.md'")) == 2

    await mgr.upsert_chunks(["five"], obs_id=5, source_uri="a.md")
    rows = raw_rows(db, "SELECT text FROM chunks WHERE source_uri='a.md'")
    assert rows == [("five",)]
    # Vec side replaced too: only one live vector remains for the source.
    results = await mgr.search("four", limit=10)
    assert all(oid == 5 for oid, _ in results)
    texts_after = {t for (t,) in raw_rows(db, "SELECT text FROM chunks")}
    assert "three four" not in texts_after and "one two" not in texts_after


@pytest.mark.asyncio
async def test_delete_by_source_clears_both_tables(
    enabled: None, tmp_path: Path
) -> None:
    db = tmp_path / "vectors.db"
    mgr = make_manager(db)
    mgr.init()
    await mgr.upsert_chunks(["camera"], obs_id=7, source_uri="a.md")
    await mgr.upsert_chunks(["tokyo"], obs_id=8, source_uri="b.md")

    await mgr.delete_by_source("a.md")
    assert raw_rows(db, "SELECT id FROM chunks WHERE source_uri='a.md'") == []
    results = await mgr.search("camera", limit=10)
    assert 7 not in [oid for oid, _ in results]
    # Other source untouched.
    assert raw_rows(db, "SELECT source_uri FROM chunks") == [("b.md",)]


@pytest.mark.asyncio
async def test_delete_obs(enabled: None, tmp_path: Path) -> None:
    mgr = make_manager(tmp_path / "vectors.db")
    mgr.init()
    await mgr.upsert_chunks(["camera"], obs_id=7, source_uri="a.md")
    await mgr.delete_obs(7)
    assert await mgr.search("camera", limit=5) == []


# ── Identity persistence & mismatch handling ─────────────────────────────────


@pytest.mark.asyncio
async def test_identity_persists_across_reopen(enabled: None, tmp_path: Path) -> None:
    db = tmp_path / "vectors.db"
    first = make_manager(db)
    first.init()
    # Active identity carries no dim until first write persists it.
    assert "dim" not in first.embedding_identity()
    await first.upsert_chunks(["camera"], obs_id=1, source_uri="a.md")
    stored = first.embedding_identity()
    assert stored == {**IDENTITY, "dim": DIM}

    second = make_manager(db)
    second.init()
    assert second.embedding_identity() == stored
    assert SqliteVecManager.embedding_matches(stored, second.embedding_identity())


@pytest.mark.asyncio
async def test_mismatched_dim_rejected(enabled: None, tmp_path: Path) -> None:
    db = tmp_path / "vectors.db"
    mgr = make_manager(db)
    mgr.init()
    await mgr.upsert_chunks(["camera"], obs_id=1, source_uri="a.md")

    other = make_manager(db, dim=8)
    other.init()
    with pytest.raises(VectorDimMismatchError):
        await other.upsert_chunks(["camera"], obs_id=9, source_uri="c.md")


def test_init_reads_stored_identity_before_writes(
    enabled: None, tmp_path: Path
) -> None:
    db = tmp_path / "vectors.db"
    first = make_manager(db)
    first.init()
    first._write_sync(["camera"], [FakeEmbedder()(["x"])[0]], 1, "a.md")

    second = make_manager(db)
    second.init()
    assert second.embedding_identity() == {**IDENTITY, "dim": DIM}


# ── Graceful degradation ─────────────────────────────────────────────────────


def test_operations_noop_when_unavailable(tmp_path: Path) -> None:
    import asyncio

    mgr = make_manager(tmp_path / "vectors.db")  # flag off → unavailable
    mgr.init()

    async def run() -> None:
        await mgr.upsert_chunks(["camera"], obs_id=1, source_uri="a.md")
        assert await mgr.search("camera", limit=5) == []
        await mgr.delete_by_source("a.md")

    asyncio.run(run())
