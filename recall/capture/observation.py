"""ObservationCapture — writes a structured observation record on every MCP tool call."""
from __future__ import annotations

from typing import TYPE_CHECKING

from recall.logging import get_logger
from recall.privacy.filter import PrivacyFilter

if TYPE_CHECKING:
    from recall.capture.compressor import MemoryCompressor
    from recall.storage.database import DatabaseManager

log = get_logger(__name__)


class ObservationCapture:
    """
    Records an observation in SQLite on every MCP tool call.

    - Strips <private> blocks before writing (PrivacyFilter).
    - Deduplicates by content hash (handled in DatabaseManager).
    - Increments write counter and triggers MemoryCompressor when threshold reached.
    """

    def __init__(
        self,
        db: "DatabaseManager",
        compressor: "MemoryCompressor | None" = None,
    ) -> None:
        self._db = db
        self._compressor = compressor

    async def record(
        self,
        content: str,
        session_id: int | None = None,
        tool_name: str = "",
        tier_used: int = 0,
        latency_ms: float = 0.0,
        obs_type: str = "observation",
    ) -> int:
        """Write an observation to SQLite. Returns the observation ID (-1 if skipped)."""
        clean = PrivacyFilter.strip(content)
        if not clean:
            log.debug("obs_all_private_skipped")
            return -1

        obs_id = await self._db.write_observation(
            content=clean,
            session_id=session_id,
            tier_used=tier_used,
            latency_ms=latency_ms,
            tool_name=tool_name,
            obs_type=obs_type,
        )

        log.debug(
            "obs_recorded",
            obs_id=obs_id,
            session_id=session_id,
            tier=tier_used,
            latency_ms=round(latency_ms, 2),
        )

        if self._compressor and session_id is not None:
            try:
                await self._compressor.maybe_compress(session_id)
            except Exception as exc:
                log.warning("compressor_error", error=str(exc))

        return obs_id
