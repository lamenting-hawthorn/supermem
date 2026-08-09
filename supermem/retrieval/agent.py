"""Unavailable Tier 4 sentinel pending a lifecycle-aware source broker."""

from __future__ import annotations

from supermem.core.retriever import BaseRetriever, RetrievalResult
from supermem.logging import get_logger

log = get_logger(__name__)


class AgentRetriever(BaseRetriever):
    """
    Tier 4 is intentionally unavailable.

    Legacy vault observations cannot map a source file to an active/retracted
    lifecycle record. Calling the raw-file Agent would therefore produce an
    ungrounded answer and could persist it as an ``agent_reply`` observation.
    Keep this sentinel for compatibility while a source-aware broker is built.
    """

    UNAVAILABLE_REASON = (
        "Tier 4 Agent memory navigation is unavailable pending a "
        "source-aware lifecycle broker."
    )

    def __init__(
        self,
        memory_path: str | None = None,
        db=None,  # DatabaseManager, typed loosely to avoid circular import
    ) -> None:
        del memory_path, db

    @property
    def tier(self) -> int:
        return 4

    @property
    def available(self) -> bool:
        return False

    async def search(self, query: str, limit: int = 10) -> RetrievalResult:
        """
        Return an empty result without constructing an Agent or writing storage.
        """
        t0 = self._now_ms()
        del query, limit
        log.info("tier4_agent_unavailable")
        return RetrievalResult(
            source_tier=self.tier,
            latency_ms=self._now_ms() - t0,
            metadata={"unavailable_reason": self.UNAVAILABLE_REASON},
        )
