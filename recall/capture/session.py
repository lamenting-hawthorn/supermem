"""SessionManager — creates and closes Recall sessions with AI-generated summaries."""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from recall.logging import get_logger

if TYPE_CHECKING:
    from recall.core.model_client import BaseModelClient
    from recall.storage.database import DatabaseManager

log = get_logger(__name__)

_SUMMARY_PROMPT = """You are summarizing a Recall memory session.
Below are the observations captured during this session.
Write a concise 2-3 sentence summary of what happened, what was learned, and what was stored.
Be specific. Use past tense.

Observations:
{observations}

Summary:"""


class SessionManager:
    """Creates sessions on MCP server start and closes them with a summary on stop."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def start(self, correlation_id: str | None = None) -> int:
        """Create a new session row. Returns the session ID."""
        cid = correlation_id or str(uuid.uuid4())
        session_id = await self._db.create_session(correlation_id=cid)
        log.info("session_started", session_id=session_id, correlation_id=cid)
        return session_id

    async def end(
        self,
        session_id: int,
        model_client: "BaseModelClient | None" = None,
    ) -> None:
        """Close the session. If model_client provided, generates an AI summary."""
        summary = ""
        if model_client is not None:
            try:
                obs_list = await self._db.get_recent_observations(session_id, limit=50)
                if obs_list:
                    obs_text = "\n\n".join(
                        f"[{o.get('type', 'obs')}] {o.get('content', '')[:500]}"
                        for o in obs_list
                    )
                    prompt = _SUMMARY_PROMPT.format(observations=obs_text)
                    summary = await model_client.chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        model="",
                        max_tokens=256,
                    )
                    summary = summary.strip()
            except Exception as exc:
                log.warning("session_summary_failed", session_id=session_id, error=str(exc))
                summary = f"[summary generation failed: {exc}]"

        await self._db.close_session(session_id, summary)
        log.info("session_ended", session_id=session_id, has_summary=bool(summary))
