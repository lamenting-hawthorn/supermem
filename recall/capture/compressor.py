"""MemoryCompressor — LLM-based compression of recent observations into summaries."""
from __future__ import annotations

from typing import TYPE_CHECKING

from recall.config import RECALL_COMPRESS_EVERY
from recall.logging import get_logger

if TYPE_CHECKING:
    from recall.core.model_client import BaseModelClient
    from recall.storage.database import DatabaseManager

log = get_logger(__name__)

_COMPRESS_PROMPT = """You are a memory compression agent for Recall.
Compress the following recent observations into a single dense summary.
Keep all important facts, decisions, entities, and relationships.
Remove redundancy. Write in the third person. Be concise but complete.

Observations to compress:
{observations}

Compressed summary:"""


class MemoryCompressor:
    """
    After every RECALL_COMPRESS_EVERY observation writes, compresses
    recent observations into a summary entry stored in SQLite.
    """

    def __init__(
        self,
        db: "DatabaseManager",
        model_client: "BaseModelClient | None" = None,
        compress_every: int = RECALL_COMPRESS_EVERY,
    ) -> None:
        self._db = db
        self._model_client = model_client
        self._compress_every = compress_every
        self._write_count = 0

    def set_model_client(self, client: "BaseModelClient") -> None:
        """Inject the model client after construction (deferred for startup order)."""
        self._model_client = client

    async def maybe_compress(self, session_id: int) -> None:
        """Increment counter; compress when threshold is reached."""
        self._write_count += 1
        if self._write_count % self._compress_every != 0:
            return
        if self._model_client is None:
            log.debug("compressor_skipped_no_client")
            return
        await self._compress_session(session_id)

    async def _compress_session(self, session_id: int) -> None:
        try:
            obs_list = await self._db.get_recent_observations(
                session_id, limit=self._compress_every
            )
            if len(obs_list) < 5:
                return

            obs_text = "\n\n".join(
                f"[{o.get('type', 'obs')} id={o.get('id')}] {o.get('content', '')[:800]}"
                for o in obs_list
            )
            summary = await self._model_client.chat_completion(
                messages=[{"role": "user", "content": _COMPRESS_PROMPT.format(observations=obs_text)}],
                model="",
                max_tokens=512,
            )
            summary = summary.strip()
            if not summary:
                return

            obs_ids = [o["id"] for o in obs_list if "id" in o]
            await self._db.write_summary(session_id, summary, obs_ids)
            log.info(
                "memory_compressed",
                session_id=session_id,
                obs_count=len(obs_list),
                summary_len=len(summary),
            )
        except Exception as exc:
            log.warning("compression_failed", session_id=session_id, error=str(exc))
