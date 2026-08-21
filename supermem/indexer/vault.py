"""VaultIndexer — walks the markdown vault, populates SQLite + Kuzu.

Also starts a watchdog file-watcher that re-indexes changed files live
without restarting the MCP server.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
from pathlib import Path
from typing import TYPE_CHECKING

from supermem.config import SUPERMEM_VAULT_PATH
from supermem.errors import VaultIndexError
from supermem.logging import get_logger
from supermem.privacy import PrivacyFilter

if TYPE_CHECKING:
    from supermem.storage.database import DatabaseManager
    from supermem.storage.graph import KuzuGraphManager
    from supermem.storage.vector import ChromaManager

log = get_logger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+?)(?:[|#][^\[\]]*)?\]\]")


class VaultIndexer:
    """
    Indexes markdown files from the vault into SQLite (entity_metadata + FTS5),
    Kuzu (entity graph edges from wikilinks) and, when available, ChromaDB
    (semantic vector chunks).

    Call walk() for a full re-index at startup.
    Call start_watcher() to begin live file-change monitoring.
    """

    def __init__(
        self,
        db: "DatabaseManager",
        graph: "KuzuGraphManager",
        vector: "ChromaManager | None" = None,
        vault_path: Path | None = None,
    ):
        self._db = db
        self._graph = graph
        self._vector = vector
        self._vault = vault_path or SUPERMEM_VAULT_PATH

    # ── Public API ────────────────────────────────────────────────────────────

    async def walk(self) -> int:
        """Full re-index of the vault. Returns count of files indexed."""
        md_files = list(self._vault.rglob("*.md"))
        count = 0
        for path in md_files:
            try:
                await self.index_file(path)
                count += 1
            except Exception as exc:
                log.warning("vault_index_file_failed", path=str(path), error=str(exc))
        await self._reconcile_deleted()
        log.info("vault_walk_complete", files=count, vault=str(self._vault))
        return count

    async def index_file(self, path: Path) -> None:
        """Index a single markdown file: entity_metadata, Kuzu graph, FTS, vectors."""
        # Skip if file hasn't changed since last index (compare mtime vs last_indexed)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        entity_name = self._path_to_entity_name(path)
        last_indexed = await self._db.get_entity_last_indexed(entity_name)
        if last_indexed is not None and mtime <= last_indexed:
            return

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise VaultIndexError(f"Cannot read {path}: {exc}") from exc

        clean_content = self._strip_private(content)
        links = self._extract_wikilinks(clean_content)

        await self._db.upsert_entity(
            name=entity_name,
            file_path=str(path),
            wikilink_count=len(links),
        )

        if self._graph is not None and self._graph.available:
            self._graph.incremental_update(
                entity_name=entity_name,
                new_targets=links,
                file_path=str(path),
            )

        # Index file content as an observation for FTS search. source_id is the
        # entity's URI so supersede_by_source can mark stale revisions inactive.
        obs_id = await self._db.write_observation(
            content=f"[entity:{entity_name}]\n{clean_content[:4096]}",
            obs_type="entity_content",
            source_id=entity_name,
        )
        # Only the newest revision stays active (stale-revision bug, SF-4).
        await self._db.supersede_by_source(entity_name, exclude_id=obs_id)

        await self._ingest_vectors(entity_name, clean_content, obs_id)

    async def on_deleted(self, path: Path, entity_name: str | None = None) -> None:
        """Handle deletion of a vault file across all stores. Idempotent.

        Supersedes any active ``entity_content`` observations for the source,
        removes its ``entity_metadata`` row, detaches the Kuzu node, and
        deletes the Chroma vectors. Each step degrades gracefully.
        """
        name = entity_name or self._path_to_entity_name(path)
        log.info("vault_file_deleted", path=str(path), entity=name)

        try:
            await self._db.supersede_by_source(name)
        except Exception as exc:
            log.warning("vault_delete_supersede_failed", entity=name, error=str(exc))

        await self._remove_entity_metadata(name)

        if self._graph is not None and self._graph.available:
            try:
                self._graph.remove_entity(name)
            except Exception as exc:
                log.warning("vault_delete_graph_failed", entity=name, error=str(exc))

        if self._vector is not None and self._vector.available:
            try:
                await self._vector.delete_by_source(name)
            except Exception as exc:
                log.warning("vault_delete_vector_failed", entity=name, error=str(exc))

    async def index_file_list(self, paths: list[Path]) -> None:
        """Batch index a list of paths (instance async method for connectors)."""
        for path in paths:
            if path.suffix == ".md" and path.exists():
                try:
                    await self.index_file(path)
                except Exception as exc:
                    log.warning("index_path_failed", path=str(path), error=str(exc))

    # ── Deletion / reconciliation ────────────────────────────────────────────

    async def _reconcile_deleted(self) -> None:
        """Remove stale store state for entity_metadata rows whose file is gone."""
        try:
            conn = await self._db._ensure_init()
            async with conn.execute(
                "SELECT name, file_path FROM entity_metadata"
            ) as cur:
                rows = await cur.fetchall()
        except Exception as exc:
            log.warning("vault_reconcile_query_failed", error=str(exc))
            return
        for name, file_path in rows:
            if not Path(file_path).exists():
                await self.on_deleted(Path(file_path), entity_name=name)

    async def _remove_entity_metadata(self, entity_name: str) -> None:
        """Delete an entity_metadata row by name. Uses the live connection since
        DatabaseManager exposes no public delete method for entity_metadata."""
        try:
            conn = await self._db._ensure_init()
            await conn.execute(
                "DELETE FROM entity_metadata WHERE name = ?", (entity_name,)
            )
            await conn.commit()
        except Exception as exc:
            log.warning(
                "vault_entity_metadata_delete_failed",
                entity=entity_name,
                error=str(exc),
            )

    # ── Vector ingestion (best-effort, never raises) ─────────────────────────

    async def _ingest_vectors(
        self, entity_name: str, clean_content: str, obs_id: int
    ) -> None:
        """Embed and store the file content as chunks in the vector store."""
        if self._vector is None or not self._vector.available:
            return
        try:
            chunks = self._chunk_text(clean_content)
            if chunks:
                await self._vector.upsert_chunks(
                    chunks=chunks, source_uri=entity_name, obs_id=obs_id
                )
        except Exception as exc:
            log.warning(
                "vault_vector_ingest_failed", entity=entity_name, error=str(exc)
            )

    @staticmethod
    def _chunk_text(text: str, chunk_chars: int = 2000) -> list[str]:
        """Split text into ~500-token chunks (≈2000 chars) for embedding."""
        text = text.strip()
        if not text:
            return []
        if len(text) <= chunk_chars:
            return [text]
        return [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)]

    def start_watcher(self) -> None:
        """
        Start a watchdog observer for live vault file changes.
        Runs in a daemon thread — stops automatically when main process exits.
        """
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.warning(
                "watchdog_unavailable",
                hint="Install with: uv add 'supermem-core[storage]'",
            )
            return

        indexer = self
        # Capture the running loop here (before spawning the daemon thread) so
        # the handler can use it safely — asyncio.get_event_loop() is deprecated
        # in Python 3.10+ when called from a non-async context in another thread.
        loop = asyncio.get_running_loop()

        def _log_failure(fut: "concurrent.futures.Future") -> None:
            """Surface exceptions from fire-and-forget watcher tasks."""
            try:
                fut.result()
            except Exception as exc:
                log.warning("watcher_task_failed", error=str(exc))

        class _Handler(FileSystemEventHandler):  # type: ignore[misc]
            def on_modified(self, event) -> None:  # type: ignore[override]
                self._handle(event)

            def on_created(self, event) -> None:  # type: ignore[override]
                self._handle(event)

            def on_deleted(self, event) -> None:  # type: ignore[override]
                self._handle_deleted(event)

            def _handle(self, event) -> None:
                if event.is_directory:
                    return
                p = Path(str(event.src_path))
                if p.suffix != ".md":
                    return
                log.info("vault_file_changed", path=str(p))
                try:
                    fut = asyncio.run_coroutine_threadsafe(indexer.index_file(p), loop)
                    fut.add_done_callback(_log_failure)
                except Exception as exc:
                    log.warning("watcher_index_failed", path=str(p), error=str(exc))

            def _handle_deleted(self, event) -> None:
                if event.is_directory:
                    return
                p = Path(str(event.src_path))
                if p.suffix != ".md":
                    return
                log.info("vault_file_deleted", path=str(p))
                try:
                    fut = asyncio.run_coroutine_threadsafe(indexer.on_deleted(p), loop)
                    fut.add_done_callback(_log_failure)
                except Exception as exc:
                    log.warning("watcher_delete_failed", path=str(p), error=str(exc))

        observer = Observer()
        observer.schedule(_Handler(), str(self._vault), recursive=True)
        observer.daemon = True
        observer.start()
        log.info("vault_watcher_started", vault=str(self._vault))

    # ── Static / class helpers ────────────────────────────────────────────────

    @staticmethod
    def _extract_wikilinks(content: str) -> list[str]:
        return _WIKILINK_RE.findall(content)

    @staticmethod
    def _strip_private(content: str) -> str:
        return PrivacyFilter.strip(content)

    def _path_to_entity_name(self, path: Path) -> str:
        try:
            rel = path.relative_to(self._vault)
            return str(rel.with_suffix("")).replace("\\", "/")
        except ValueError:
            return path.stem

    @classmethod
    def index_paths(cls, paths: list[Path]) -> None:  # type: ignore[misc]
        """
        Sync class-level entry point used by BaseConnector.run().
        Creates temporary storage instances for the index run.
        """
        from supermem.storage.database import DatabaseManager
        from supermem.storage.graph import KuzuGraphManager

        async def _run() -> None:
            async with DatabaseManager() as db:
                graph = KuzuGraphManager()
                graph.init()
                indexer = cls(db=db, graph=graph)
                await indexer.index_file_list(paths)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_run(), loop).result(timeout=30)
            else:
                asyncio.run(_run())
        except Exception as exc:
            log.warning("vault_index_paths_failed", error=str(exc))
