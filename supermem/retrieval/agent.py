"""AgentRetriever — Tier 4: LLM agent fallback (last resort)."""

from __future__ import annotations

from typing import Any

from supermem.core.retriever import BaseRetriever, RetrievalResult
from supermem.logging import get_logger

log = get_logger(__name__)


class AgentRetriever(BaseRetriever):
    """
    Tier 4 — LLM agent fallback.

    Wraps the existing agent.Agent class. Used only when tiers 1–3
    return insufficient results. Preserves supermem's unique value prop:
    immune to embedding drift, capable of multi-hop reasoning.

    The agent's reply is returned to the caller. By default it is NOT
    persisted as an observation — only when SUPERMEM_TIER4_PERSIST is
    enabled is the reply written to SQLite so callers can fetch it via
    get_observations().
    """

    def __init__(
        self,
        memory_path: str | None = None,
        db=None,  # DatabaseManager, typed loosely to avoid circular import
    ) -> None:
        self._memory_path = memory_path
        self._db = db
        self._agent: Any = None  # lazy-initialized on first call

    @property
    def tier(self) -> int:
        return 4

    @property
    def available(self) -> bool:
        """True only if the LLM backend the agent needs is actually configured.

        The agent uses the OpenRouter client by default, which requires an
        API key. Without it the tier cannot answer, so it is reported as
        unavailable rather than hardcoded True.
        """
        try:
            import agent  # noqa: F401  required dependency present
            from agent.settings import OPENROUTER_API_KEY
        except Exception:
            return False
        return bool(OPENROUTER_API_KEY)

    async def search(self, query: str, limit: int = 10) -> RetrievalResult:
        """
        Run the LLM agent on the query. Returns the reply as a single observation.

        This is intentionally slow — call only when tiers 1–3 are insufficient.
        """
        import asyncio

        t0 = self._now_ms()
        try:
            from supermem.config import SUPERMEM_VAULT_PATH, SUPERMEM_TIER4_PERSIST
            from agent import Agent

            mem_path = self._memory_path or str(SUPERMEM_VAULT_PATH)
            if self._agent is None:
                self._agent = Agent(
                    memory_path=mem_path, predetermined_memory_path=False
                )

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._agent.chat, query)
            reply = (result.reply or "").strip()

            if not reply:
                return RetrievalResult(
                    source_tier=self.tier, latency_ms=self._now_ms() - t0
                )

            # Optionally persist the agent reply as an observation so callers
            # can use get_observations(). Disabled by default to avoid writing
            # unverified model text into the memory store.
            obs_id = -1
            if self._db is not None and SUPERMEM_TIER4_PERSIST:
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
            return RetrievalResult(
                source_tier=self.tier, latency_ms=self._now_ms() - t0
            )
