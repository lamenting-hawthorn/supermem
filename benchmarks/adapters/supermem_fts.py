"""supermem_fts adapter — the real SQLite + FTS5 retrieval pipeline."""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

from supermem.logging import get_logger
from supermem.storage.database import DatabaseManager
from supermem.storage.graph import KuzuGraphManager
from supermem.indexer.vault import VaultIndexer

from benchmarks.harness_types import (
    BaseBenchmarkAdapter,
    CitedResult,
    Mutation,
)

log = get_logger(__name__)


class SupermemFtsAdapter(BaseBenchmarkAdapter):
    name = "supermem_fts"

    def __init__(self) -> None:
        self._workspace: Path | None = None
        self._db: DatabaseManager | None = None
        self._indexer: VaultIndexer | None = None
        self._graph: KuzuGraphManager | None = None
        self._graph_dir: Path | None = None

    async def setup(self, workspace: Path, dataset_dir: Path) -> None:
        self._workspace = workspace
        sources = dataset_dir / "sources"
        entities = workspace / "entities"
        entities.mkdir(parents=True, exist_ok=True)
        for md in sorted(sources.glob("*.md")):
            shutil.copyfile(md, entities / md.name)
        self._db = DatabaseManager(workspace / "bench.db")
        await self._db.init()
        try:
            graph_dir = workspace / "graph"
            graph_dir.mkdir(exist_ok=True)
            self._graph = KuzuGraphManager(graph_dir / "graph.kz")
        except Exception as exc:
            log.warning("bench_graph_unavailable", error=str(exc))
            self._graph = None
        self._indexer = VaultIndexer(
            self._db, self._graph, vector=None, vault_path=workspace
        )
        await self._indexer.walk()
        await self._stamp_observed_at(dataset_dir)

    async def _stamp_observed_at(self, dataset_dir: Path) -> None:
        """Deterministic temporal anchor: sources may declare
        ``observed_at: <epoch>`` in YAML frontmatter; stamp the matching
        observation so effective_interval cases don't depend on wall-clock."""
        assert self._db is not None and self._workspace is not None
        import re

        conn = await self._db._ensure_init()
        for md in sorted((dataset_dir / "sources").glob("*.md")):
            text = md.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\nobserved_at:\s*([0-9.]+)\s*\n---\s*\n", text)
            if not m:
                continue
            entity_name = f"entities/{md.stem}"
            await conn.execute(
                "UPDATE observations SET observed_at = ? WHERE source_id = ?",
                (float(m.group(1)), entity_name),
            )
        await conn.commit()

    async def mutate(self, mutation: Mutation) -> None:
        assert self._workspace is not None and self._db is not None
        p = mutation.payload
        if mutation.type == "modify_file":
            path = self._workspace / p["path"]
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace(p["old_substring"], p["new_substring"]),
                encoding="utf-8",
            )
            await self._indexer.index_file(path)
        elif mutation.type == "delete_file":
            path = self._workspace / p["path"]
            path.unlink(missing_ok=True)
            await self._indexer.on_deleted(path)
        elif mutation.type == "retract_obs":
            obs_id, _ = await self._find_active_obs(p["match_substring"])
            if obs_id is None:
                raise RuntimeError(
                    f"retract_obs: no active observation matches {p['match_substring']!r}"
                )
            await self._db.retract_observation(obs_id, reason="bm0-retraction")
        elif mutation.type == "expire_obs":
            obs_id, _ = await self._find_active_obs(p["match_substring"])
            if obs_id is None:
                raise RuntimeError(
                    f"expire_obs: no active observation matches {p['match_substring']!r}"
                )
            conn = await self._db._ensure_init()
            await conn.execute(
                "UPDATE observations SET expires_at = 1.0 WHERE id = ?", (obs_id,)
            )
            await conn.commit()
        elif mutation.type == "archive_obs":
            obs_id, source_id = await self._find_active_obs(p["match_substring"])
            if obs_id is None:
                raise RuntimeError(
                    f"archive_obs: no active observation matches {p['match_substring']!r}"
                )
            await self._db.archive_observations([obs_id])
            # Simulate the compressor writing its summary as the current
            # retrievable representation. The summary inherits the archived
            # record's source_id so its citation still verifies.
            await self._db.write_observation(
                content=p["summary_text"],
                obs_type="observation",
                source_id=source_id,
            )
        else:
            raise ValueError(f"Unknown mutation type: {mutation.type}")

    async def _find_active_obs(
        self, match_substring: str
    ) -> tuple[int | None, str | None]:
        conn = await self._db._ensure_init()
        async with conn.execute(
            "SELECT id, source_id FROM observations WHERE status = 'active' AND instr(content, ?) > 0 ORDER BY id",
            (match_substring,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None, None
        return row[0], row[1]

    async def retrieve(self, query: str, k: int = 10) -> list[CitedResult]:
        assert self._db is not None and self._workspace is not None
        started = time.perf_counter()
        ids = await self._db.fts_search(query, limit=k)
        rows = await self._db.get_observations(ids)
        by_id = {row["id"]: row for row in rows}
        latency_ms = (time.perf_counter() - started) * 1000.0
        results: list[CitedResult] = []
        for rank, oid in enumerate(ids, start=1):
            row = by_id.get(oid)
            if row is None:
                continue
            source_uri = f"{row['source_id']}.md" if row["source_id"] else ""
            digest = ""
            source_path = self._workspace / source_uri if source_uri else None
            if source_path is not None and source_path.exists():
                digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
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
                    latency_ms=latency_ms / max(len(ids), 1),
                )
            )
        return results

    async def retrieve_with_bound(
        self, query: str, k: int, as_of: float
    ) -> list[CitedResult]:
        """Temporal-bound retrieval: effective time falls back to created_at."""
        results = await self.retrieve(query, k=k)
        conn = await self._db._ensure_init()

        kept: list[CitedResult] = []
        for res in results:
            try:
                oid = int(res.memory_id)
            except ValueError:
                kept.append(res)
                continue
            async with conn.execute(
                "SELECT observed_at, created_at FROM observations WHERE id = ?", (oid,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                continue
            effective_time = row[0] if row[0] is not None else row[1]
            if effective_time is not None and effective_time <= as_of:
                kept.append(res)
        return kept

    async def teardown(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
