"""TimelineQuery — returns chronological context around an observation."""
from __future__ import annotations

from typing import TYPE_CHECKING

from recall.logging import get_logger

if TYPE_CHECKING:
    from recall.storage.database import DatabaseManager

log = get_logger(__name__)


class TimelineQuery:
    """
    Given an observation ID, returns the N observations before and after it.
    Provides temporal context when an AI client retrieves a result.
    """

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def get(self, obs_id: int, window: int = 5) -> list[dict]:
        """
        Return chronological context around obs_id.

        Args:
            obs_id: The anchor observation ID.
            window: Number of observations to return on each side.

        Returns:
            List of observation dicts ordered by created_at, anchor included.
        """
        results = await self._db.get_timeline(obs_id, window)
        log.debug("timeline_query", obs_id=obs_id, window=window, found=len(results))
        return results
