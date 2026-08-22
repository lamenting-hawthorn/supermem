"""Unit tests for ChromaManager embedding-identity tracking and config parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

import supermem.config as config
import supermem.storage.vector as vector_mod
from supermem.config import DEFAULT_FASTEMBED_MODEL
from supermem.storage.vector import (
    ChromaManager,
    format_identity,
    parse_stored_identity,
    resolve_embedder,
)

try:
    import chromadb  # noqa: F401

    HAS_CHROMADB = True
except ImportError:
    HAS_CHROMADB = False

try:
    import fastembed  # noqa: F401

    HAS_FASTEMBED = True
except ImportError:
    HAS_FASTEMBED = False


# ── Pure logic: identity formatting / parsing ────────────────────────────────


def test_format_identity_canonical_and_stable() -> None:
    a = format_identity({"model": "BAAI/bge-small-en-v1.5", "provider": "fastembed"})
    b = format_identity({"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"})
    assert a == b
    assert '"provider":"fastembed"' in a


def test_parse_stored_identity_roundtrip() -> None:
    identity = {"provider": "fastembed", "model": DEFAULT_FASTEMBED_MODEL, "dim": 384}
    assert parse_stored_identity(format_identity(identity)) == identity


def test_parse_stored_identity_missing_reports_legacy() -> None:
    assert parse_stored_identity(None) == {"provider": "unknown-legacy"}
    assert parse_stored_identity("") == {"provider": "unknown-legacy"}


def test_parse_stored_identity_garbage_reports_legacy() -> None:
    assert parse_stored_identity("not json {") == {"provider": "unknown-legacy"}
    assert parse_stored_identity('{"no_provider_key": true}') == {
        "provider": "unknown-legacy"
    }


def test_embedding_matches() -> None:
    a = {"provider": "chroma-default-onnx-minilm"}
    b = dict(a)
    c = {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5", "dim": 384}
    assert ChromaManager.embedding_matches(a, b) is True
    assert ChromaManager.embedding_matches(a, c) is False
    # Missing identities can never be confirmed as matching.
    assert ChromaManager.embedding_matches(a, None) is False
    assert ChromaManager.embedding_matches(None, None) is False


def test_default_identity_shape() -> None:
    identity, embed_fn = resolve_embedder("", "")
    assert identity == {"provider": "chroma-default-onnx-minilm"}
    assert embed_fn is None


# ── Fastembed fallback (skipped when fastembed IS installed) ─────────────────


@pytest.mark.skipif(HAS_FASTEMBED, reason="fastembed installed; no fallback to test")
def test_fastembed_absent_falls_back_to_default() -> None:
    identity, embed_fn = resolve_embedder("fastembed", "")
    assert identity == {"provider": "chroma-default-onnx-minilm"}
    assert embed_fn is None


@pytest.mark.skipif(not HAS_FASTEMBED, reason="fastembed not installed")
def test_fastembed_present_builds_identity() -> None:
    identity, embed_fn = resolve_embedder("fastembed", "")
    assert identity["provider"] == "fastembed"
    assert identity["model"] == DEFAULT_FASTEMBED_MODEL
    assert isinstance(identity.get("dim"), int)
    assert embed_fn is not None


# ── Config parsing ────────────────────────────────────────────────────────────


def test_parse_embedding_provider_normalization() -> None:
    assert config._parse_embedding_provider("") == ""
    assert config._parse_embedding_provider(None) == ""
    assert config._parse_embedding_provider("FastEmbed") == "fastembed"
    assert config._parse_embedding_provider("  fastembed  ") == "fastembed"
    # Unknown providers normalize to the default (empty string).
    assert config._parse_embedding_provider("onnx") == ""
    assert config._parse_embedding_provider("openai") == ""


def test_parse_embedding_model_strips() -> None:
    assert config._parse_embedding_model(None) == ""
    assert config._parse_embedding_model("  BAAI/bge-small-en-v1.5 ") == (
        "BAAI/bge-small-en-v1.5"
    )


def test_env_accessors_read_live_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERMEM_EMBEDDING_PROVIDER", "fastembed")
    monkeypatch.setenv("SUPERMEM_EMBEDDING_MODEL", DEFAULT_FASTEMBED_MODEL)
    assert config.embedding_provider_from_env() == "fastembed"
    assert config.embedding_model_from_env() == DEFAULT_FASTEMBED_MODEL

    monkeypatch.setenv("SUPERMEM_EMBEDDING_PROVIDER", "bogus")
    monkeypatch.delenv("SUPERMEM_EMBEDDING_MODEL")
    assert config.embedding_provider_from_env() == ""
    assert config.embedding_model_from_env() == ""

    monkeypatch.delenv("SUPERMEM_EMBEDDING_PROVIDER")
    assert config.embedding_provider_from_env() == ""


def test_manager_resolves_identity_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPERMEM_EMBEDDING_PROVIDER", "fastembed")
    monkeypatch.setenv("SUPERMEM_EMBEDDING_MODEL", DEFAULT_FASTEMBED_MODEL)
    mgr = ChromaManager()
    if HAS_FASTEMBED:
        assert mgr.active_identity["provider"] == "fastembed"
        assert mgr.active_identity["model"] == DEFAULT_FASTEMBED_MODEL
    else:
        assert mgr.active_identity == {"provider": "chroma-default-onnx-minilm"}
    # No collection open → embedding_identity reports resolved active identity.
    assert mgr.embedding_identity() == mgr.active_identity


# ── Chroma-backed tests (skip when chromadb unavailable) ─────────────────────


@pytest.fixture
def enabled_chroma(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("chromadb", reason="chromadb not installed")
    monkeypatch.setattr(vector_mod, "SUPERMEM_VECTOR", True)


def test_chroma_identity_persists_across_reopen(
    enabled_chroma: None, tmp_path: Path
) -> None:
    db_path = tmp_path / "chroma"
    first = ChromaManager(db_path=db_path)
    first.init()
    assert first.available
    identity = first.embedding_identity()
    assert identity.get("provider") in {"chroma-default-onnx-minilm", "fastembed"}

    second = ChromaManager(db_path=db_path)
    second.init()
    assert second.available
    assert second.embedding_identity() == identity
    assert ChromaManager.embedding_matches(identity, second.embedding_identity())


@pytest.mark.asyncio
async def test_chroma_search_shape_unaffected(
    enabled_chroma: None, tmp_path: Path
) -> None:
    mgr = ChromaManager(db_path=tmp_path / "chroma")
    await mgr.search("anything")  # unavailable before init → []
    mgr.init()
    await mgr.upsert_chunks(["hello world"], obs_id=7, source_uri="note.md")
    results = await mgr.search("hello", limit=5)
    assert isinstance(results, list)
    for oid, dist in results:
        assert isinstance(oid, int)
        assert isinstance(dist, float)
    await mgr.delete_by_source("note.md")
    assert await mgr.search("hello", limit=5) == []
