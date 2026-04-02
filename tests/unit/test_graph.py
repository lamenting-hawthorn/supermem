"""Unit tests for KuzuGraphManager — entity nodes, edges, BFS expansion."""
from __future__ import annotations

from pathlib import Path

import pytest

from recall.storage.graph import KuzuGraphManager


@pytest.fixture
def graph(tmp_path: Path) -> KuzuGraphManager:
    g = KuzuGraphManager(tmp_path / "graph")
    g.init()
    return g


def test_available_after_init(graph: KuzuGraphManager) -> None:
    # kuzu is installed in the dev environment
    if not graph.available:
        pytest.skip("kuzu not installed — skipping graph tests")
    assert graph.available is True


def test_upsert_entity_creates_node(graph: KuzuGraphManager) -> None:
    if not graph.available:
        pytest.skip("kuzu not installed")
    graph.upsert_entity("alice", "/vault/alice.md")
    # No exception means success; re-upsert is idempotent
    graph.upsert_entity("alice", "/vault/alice.md")


def test_add_edge_creates_relationship(graph: KuzuGraphManager) -> None:
    if not graph.available:
        pytest.skip("kuzu not installed")
    graph.upsert_entity("alice", "/vault/alice.md")
    graph.upsert_entity("bob", "/vault/bob.md")
    graph.add_edge("alice", "bob", anchor="alice mentions bob")


def test_expand_returns_related_entities(graph: KuzuGraphManager) -> None:
    if not graph.available:
        pytest.skip("kuzu not installed")
    graph.upsert_entity("alice", "/vault/alice.md")
    graph.upsert_entity("bob", "/vault/bob.md")
    graph.upsert_entity("carol", "/vault/carol.md")
    graph.add_edge("alice", "bob", anchor="link")
    graph.add_edge("bob", "carol", anchor="link")

    # 1-hop from alice → should find bob
    result_1hop = graph.expand(["alice"], hops=1)
    assert "bob" in result_1hop

    # 2-hop from alice → should find both bob and carol
    result_2hop = graph.expand(["alice"], hops=2)
    assert "carol" in result_2hop


def test_expand_unknown_entity_returns_empty(graph: KuzuGraphManager) -> None:
    if not graph.available:
        pytest.skip("kuzu not installed")
    result = graph.expand(["nonexistent_entity_xyz"], hops=2)
    assert isinstance(result, list)
    assert len(result) == 0


def test_expand_empty_seeds_returns_empty(graph: KuzuGraphManager) -> None:
    if not graph.available:
        pytest.skip("kuzu not installed")
    result = graph.expand([], hops=2)
    assert result == []


def test_remove_edges_from_clears_edges(graph: KuzuGraphManager) -> None:
    if not graph.available:
        pytest.skip("kuzu not installed")
    graph.upsert_entity("alice", "/vault/alice.md")
    graph.upsert_entity("bob", "/vault/bob.md")
    graph.add_edge("alice", "bob", anchor="link")
    graph.remove_edges_from("/vault/alice.md")
    result = graph.expand(["alice"], hops=1)
    assert "bob" not in result


def test_incremental_update(graph: KuzuGraphManager) -> None:
    if not graph.available:
        pytest.skip("kuzu not installed")
    graph.upsert_entity("alice", "/vault/alice.md")
    graph.upsert_entity("bob", "/vault/bob.md")
    graph.upsert_entity("carol", "/vault/carol.md")
    # Initial: alice→bob
    graph.add_edge("alice", "bob", anchor="link")
    # Update: old=[bob], new=[carol] — alice should now point to carol, not bob
    graph.incremental_update("/vault/alice.md", old_links=["bob"], new_links=["carol"])
    result = graph.expand(["alice"], hops=1)
    assert "carol" in result


def test_unavailable_gracefully_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When kuzu is not installed, all methods return empty/False without raising."""
    import recall.storage.graph as graph_mod
    monkeypatch.setattr(graph_mod, "_import_kuzu", lambda: None)
    g = KuzuGraphManager(tmp_path / "no_kuzu_graph")
    g.init()
    assert g.available is False
    # These should all be silent no-ops
    g.upsert_entity("x", "/x.md")
    g.add_edge("x", "y", anchor="link")
    result = g.expand(["x"], hops=2)
    assert result == []
