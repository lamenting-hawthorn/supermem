"""AgentRetriever — Tier 4: LLM agent fallback (last resort)."""
from __future__ import annotations

from recall.core.retriever import BaseRetriever, RetrievalResult
from recall.logging import get_logger

log = get_logger(__name__)


class AgentRetriever(BaseRetriever):
    """
    Tier 4 — LLM agent fallback.

    Wraps the existing agent.Agent class. Used only when tiers 1–3
    return insufficient results. Preserves recall's unique value prop:
    immune to embedding drift, capable of multi-hop reasoning.

    The agent's reply is written to SQLite as an observation and its
    ID is returned so the caller can fetch full content via get_observations().
    """

    def __init__(
        self,
        memory_path: str | None = None,
        db=None,  # DatabaseManager, typed loosely to avoid circular import
    ) -> None:
        self._memory_path = memory_path
        self._db = db

    @property
    def tier(self) -> int:
        return 4

    @property
    def available(self) -> bool:
        return True  # Agent is always available as last resort

    async def search(self, query: str, limit: int = 10) -> RetrievalResult:
        """
        Run the LLM agent on the query. Returns the reply as a single observation.

        This is intentionally slow — call only when tiers 1–3 are insufficient.
        """
        import asyncio
        t0 = self._now_ms()
        try:
            from recall.config import RECALL_VAULT_PATH
            from agent import Agent

            mem_path = self._memory_path or str(RECALL_VAULT_PATH)
            agent = Agent(memory_path=mem_path, predetermined_memory_path=False)

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, agent.chat, query)
            reply = (result.reply or "").strip()

            if not reply:
                return RetrievalResult(source_tier=self.tier, latency_ms=self._now_ms() - t0)

            # Write the agent reply as an observation so callers can use get_observations()
            obs_id = -1
            if self._db is not None:
                obs_id = await self._db.write_observation(
                    content=reply,
                    tier_used=4,
                    latency_ms=self._now_ms() - t0,
                    tool_name="agent_retriever",
                    obs_type="agent_reply",
                )

            latency = self._now_ms() - t0
            log.info("agent_retriever_used", latency_ms=round(latency, 1))
            return RetrievalResult(
                obs_ids=[obs_id] if obs_id != -1 else [],
                source_tier=self.tier,
                latency_ms=latency,
                metadata={"reply": reply},
            )
        except Exception as exc:
            log.warning("agent_retriever_failed", error=str(exc))
            return RetrievalResult(source_tier=self.tier, latency_ms=self._now_ms() - t0)
