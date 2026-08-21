"""Unit tests for Unicode FTS5 tokenizer ('porter unicode61').

Background: FTS5's 'ascii' tokenizer stripped all non-ASCII tokens entirely,
making Japanese / Arabic / accented content unsearchable. Switching to
'unicode61' fixes this.

Empirical findings for unicode61 (SQLite bundled with Python 3.11):
- Diacritics ARE folded by default (remove_diacritics=1): "café" is indexed
  as "cafe", so searching either form matches.
- Contiguous runs of CJK ideographs/kana are treated as ONE single token.
  A phrase query must therefore contain the full contiguous run:
  MATCH '東京' does NOT match "東京タワーは高い", but MATCH '東京タワーは高い'
  does. Sub-string/partial CJK matching would require the trigram tokenizer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from supermem.storage.database import DatabaseManager


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_unicode.db"


@pytest_asyncio.fixture
async def db(db_path: Path) -> DatabaseManager:
    d = DatabaseManager(db_path)
    await d.init()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_accented_content_searchable(db: DatabaseManager) -> None:
    oid = await db.write_observation("the café serves résumé naïve soup")
    # Exact-token match with accents intact
    ids = await db.fts_search("café")
    assert oid in ids
    # unicode61 folds diacritics by default, so unaccented queries also hit
    ids = await db.fts_search("cafe")
    assert oid in ids
    ids = await db.fts_search("naïve")
    assert oid in ids


@pytest.mark.asyncio
async def test_cjk_content_searchable(db: DatabaseManager) -> None:
    """unicode61 indexes contiguous CJK as one token — full-run query required."""
    oid = await db.write_observation("visited 東京タワーは高い yesterday")
    # The full contiguous CJK run matches
    ids = await db.fts_search("東京タワーは高い")
    assert oid in ids
    # Partial CJK sub-strings do NOT match under unicode61 (documented above)
    ids = await db.fts_search("東京")
    assert oid not in ids


@pytest.mark.asyncio
async def test_existing_ascii_behavior_unchanged(db: DatabaseManager) -> None:
    await db.write_observation("alice works at acme corporation")
    await db.write_observation("bob is a software engineer")
    ids = await db.fts_search("alice")
    assert len(ids) >= 1
    ids = await db.fts_search("engineer")
    assert len(ids) == 1
