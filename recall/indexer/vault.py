"""VaultIndexer — walks the markdown vault, populates SQLite + Kuzu.

Also starts a watchdog file-watcher that re-indexes changed files live
without restarting the MCP server.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from recall.config import RECALL_VAULT_PATH
from recall.errors import VaultIndexError
from recall.logging import get_logger

if TYPE_CHECKING:
    from recall.storage.database import DatabaseManager
    from recall.storage.graph import KuzuGraphManager

log = get_logger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+?)(?:[|#][^\[\]]*)?\]\]")
_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)


class VaultIndexer:
    """
    Indexes markdown files from the vault into SQLite (entity_metadata + FTS5)
    and Kuzu (entity graph edges from wikilinks).

    Call walk() for a full re-index at startup.
    Call start_watcher() to begin live file-change monitoring.
    """

    def __init__(
        self,
        db: "DatabaseManager",
        graph: "KuzuGraphManager",
        vault_path: Path | None = None,
    ):
        self._db = db
        self._graph = graph
        self._vault = vault_path or RECALL_VAULT_PATH

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
        log.info("vault_walk_complete", files=count, vault=str(self._vault))
        return count

    async def index_file(self, path: Path) -> None:
        """Index a single markdown file: update entity_metadata + Kuzu graph."""
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise VaultIndexError(f"Cannot read {path}: {exc}") from exc

        clean_content = self._strip_private(content)
        links = self._extract_wikilinks(clean_content)
        entity_name = self._path_to_entity_name(path)

        await self._db.upsert_entity(
            name=entity_name,
            file_path=str(path),
            wikilink_count=len(links),
        )

        if self._graph.available:
            self._graph.incremental_update(
                entity_name=entity_name,
                new_targets=links,
                file_path=str(path),
            )

        # Index file content as an observation for FTS search
        await self._db.write_observation(
            content=f"[entity:{entity_name}]\n{clean_content[:4096]}",
            obs_type="entity_content",
        )

    async def index_file_list(self, paths: list[Path]) -> None:
        """Batch index a list of paths (instance async method for connectors)."""
        for path in paths:
            if path.suffix == ".md" and path.exists():
                try:
                    await self.index_file(path)
                except Exception as exc:
                    log.warning("index_path_failed", path=str(path), error=str(exc))

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
                hint="Install with: uv add 'recall-core[storage]'",
            )
            return

        indexer = self

        class _Handler(FileSystemEventHandler):  # type: ignore[misc]
            def on_modified(self, event) -> None:  # type: ignore[override]
                self._handle(event)

            def on_created(self, event) -> None:  # type: ignore[override]
                self._handle(event)

            def _handle(self, event) -> None:
                if event.is_directory:
                    return
                p = Path(str(event.src_path))
                if p.suffix != ".md":
                    return
                log.info("vault_file_changed", path=str(p))
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            indexer.index_file(p), loop
                        )
                    else:
                        asyncio.run(indexer.index_file(p))
                except Exception as exc:
                    log.warning("watcher_index_failed", path=str(p), error=str(exc))

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
        return _PRIVATE_RE.sub("", content)

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
        from recall.storage.database import DatabaseManager
        from recall.storage.graph import KuzuGraphManager

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
