"""KuzuGraphManager — embedded Kuzu graph for entity-relation traversal."""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from recall.config import RECALL_KUZU_PATH
from recall.errors import GraphTraversalError
from recall.logging import get_logger

log = get_logger(__name__)


def _import_kuzu() -> Any:
    """Lazy import so kuzu is optional at import time."""
    try:
        import kuzu
        return kuzu
    except ImportError:
        return None


class KuzuGraphManager:
    """
    Embedded Kuzu graph database for entity-relation graph.

    Nodes  = markdown entities (one per .md file)
    Edges  = [[wikilinks]] between entities

    Kuzu is a C++-core embedded graph DB with Python bindings.
    No server process — ships as `pip install kuzu`.

    If kuzu is not installed, all methods degrade gracefully
    (available() returns False, search returns empty results).
    """

    def __init__(self, db_path: Path | None = None):
        self._path = db_path or RECALL_KUZU_PATH
        self._path.mkdir(parents=True, exist_ok=True)
        self._kuzu = _import_kuzu()
        self._db: Any = None
        self._conn: Any = None
        self._initialized = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def init(self) -> None:
        """Create schema if needed. Synchronous — Kuzu Python API is sync."""
        if not self._kuzu:
            log.warning("kuzu_unavailable", hint="Install kuzu: uv add kuzu")
            return
        if self._initialized:
            return
        try:
            self._db = self._kuzu.Database(str(self._path))
            self._conn = self._kuzu.Connection(self._db)
            # Idempotent DDL
            with contextlib.suppress(Exception):
                self._conn.execute(
                    "CREATE NODE TABLE IF NOT EXISTS Entity("
                    "name STRING, file_path STRING, PRIMARY KEY(name))"
                )
            with contextlib.suppress(Exception):
                self._conn.execute(
                    "CREATE REL TABLE IF NOT EXISTS LINKS_TO("
                    "FROM Entity TO Entity, anchor STRING)"
                )
            self._initialized = True
            log.info("kuzu_init", path=str(self._path))
        except Exception as exc:
            log.warning("kuzu_init_failed", error=str(exc))
            self._initialized = False

    @property
    def available(self) -> bool:
        return self._kuzu is not None and self._initialized

    # ── Mutations ─────────────────────────────────────────────────────────────

    def upsert_entity(self, name: str, file_path: str) -> None:
        if not self.available:
            return
        try:
            self._conn.execute(
                "MERGE (e:Entity {name: $name}) SET e.file_path = $fp",
                {"name": name, "fp": file_path},
            )
        except Exception as exc:
            log.warning("kuzu_upsert_entity_failed", name=name, error=str(exc))

    def add_edge(self, src: str, dst: str, anchor: str) -> None:
        """Add a directed LINKS_TO edge from src entity to dst entity."""
        if not self.available:
            return
        try:
            # Ensure both nodes exist
            self._conn.execute(
                "MERGE (:Entity {name: $name})", {"name": src}
            )
            self._conn.execute(
                "MERGE (:Entity {name: $name})", {"name": dst}
            )
            self._conn.execute(
                "MATCH (a:Entity {name: $src}), (b:Entity {name: $dst}) "
                "MERGE (a)-[:LINKS_TO {anchor: $anchor}]->(b)",
                {"src": src, "dst": dst, "anchor": anchor},
            )
        except Exception as exc:
            log.warning("kuzu_add_edge_failed", src=src, dst=dst, error=str(exc))

    def remove_edges_from(self, src: str) -> None:
        """Remove all outgoing edges from an entity (used for incremental update)."""
        if not self.available:
            return
        try:
            self._conn.execute(
                "MATCH (a:Entity {name: $name})-[r:LINKS_TO]->() DELETE r",
                {"name": src},
            )
        except Exception as exc:
            log.warning("kuzu_remove_edges_failed", src=src, error=str(exc))

    def incremental_update(
        self,
        entity_name: str,
        new_targets: list[str],
        file_path: str,
    ) -> None:
        """
        Update edges for a single entity after its file changes.
        Removes old outgoing edges, adds new ones.
        """
        if not self.available:
            return
        self.upsert_entity(entity_name, file_path)
        self.remove_edges_from(entity_name)
        for target in new_targets:
            self.add_edge(entity_name, target, anchor=target)

    # ── Traversal ─────────────────────────────────────────────────────────────

    def expand(self, entity_names: list[str], hops: int = 2) -> list[str]:
        """
        BFS from seed entity names up to `hops` hops.
        Returns all reachable entity names (excluding seeds).
        """
        if not self.available or not entity_names:
            return []
        try:
            placeholders = ", ".join(f"$n{i}" for i in range(len(entity_names)))
            params = {f"n{i}": name for i, name in enumerate(entity_names)}
            # Kuzu variable-length path: 1..hops hops
            query = (
                f"MATCH (seed:Entity)-[:LINKS_TO*1..{hops}]->(related:Entity) "
                f"WHERE seed.name IN [{placeholders}] "
                f"AND NOT related.name IN [{placeholders}] "
                f"RETURN DISTINCT related.name AS name"
            )
            result = self._conn.execute(query, params)
            names: list[str] = []
            while result.has_next():
                row = result.get_next()
                names.append(row[0])
            log.debug("graph_expand", seeds=len(entity_names), found=len(names), hops=hops)
            return names
        except Exception as exc:
            log.warning("graph_expand_failed", error=str(exc))
            return []

    def get_neighbors(self, entity_name: str) -> list[str]:
        """Direct (1-hop) neighbors of an entity."""
        return self.expand([entity_name], hops=1)
