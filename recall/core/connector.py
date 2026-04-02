"""BaseConnector ABC — supersedes memory_connectors/base.py for v2 connectors."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class BaseConnector(ABC):
    """
    Import-only pipeline: extract source data -> write markdown -> index to SQLite+Kuzu.

    Contract rules:
    - extract() and transform() are the ONLY methods subclasses must implement.
    - transform() writes markdown files and returns their Paths — it does NOT index.
    - run() calls extract -> transform -> VaultIndexer.index_paths() in that order.
    - Connectors MUST NOT write directly to SQLite, Kuzu, or Chroma.
    - All markdown written by transform() MUST be human-readable and manually editable.
    """

    def __init__(self, output_path: str, **kwargs):
        self.output_path = Path(output_path)
        self.output_path.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def extract(self, source: str, max_items: int | None = None) -> dict:
        """Fetch or parse source data. Returns raw extracted data."""

    @abstractmethod
    def transform(self, data: dict) -> list[Path]:
        """Convert extracted data to markdown files. Returns list of written Paths."""

    def run(self, source: str, max_items: int | None = None) -> None:
        """Full pipeline: extract -> transform -> index. Do NOT override."""
        from recall.indexer.vault import VaultIndexer  # noqa: PLC0415

        data = self.extract(source, max_items)
        paths = self.transform(data)
        if paths:
            VaultIndexer.index_paths(paths)

    @property
    @abstractmethod
    def connector_name(self) -> str:
        """Human-readable connector name, e.g. 'chatgpt'."""

    @property
    @abstractmethod
    def supported_formats(self) -> list[str]:
        """File extensions or source types this connector accepts."""
