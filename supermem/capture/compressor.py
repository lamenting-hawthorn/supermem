"""MemoryCompressor — LLM-based compression of recent observations into summaries."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from supermem.config import (
    SUPERMEM_COMPRESS_BUDGET_CHARS,
    SUPERMEM_COMPRESS_EVERY,
    SUPERMEM_COMPRESS_MIN_COVERAGE,
)
from supermem.logging import get_logger

if TYPE_CHECKING:
    from supermem.core.model_client import BaseModelClient
    from supermem.storage.database import DatabaseManager

log = get_logger(__name__)

_SALIENT_RE = re.compile(r"[a-z0-9]{4,}")
"""Tokens >=4 chars — long enough to be content-bearing, short enough to match
across paraphrase. Digits qualify (dates, ids are exactly what must survive)."""

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "have",
        "has",
        "are",
        "was",
        "were",
        "will",
        "would",
        "been",
        "their",
        "they",
        "them",
        "then",
        "than",
        "when",
        "where",
        "which",
        "what",
        "who",
        "how",
        "all",
        "each",
        "every",
        "some",
        "such",
        "into",
        "about",
        "over",
        "after",
        "before",
        "between",
        "also",
        "just",
        "only",
        "very",
        "more",
        "most",
        "other",
        "these",
        "those",
        "there",
        "here",
        "your",
        "his",
        "her",
        "its",
        "our",
        "out",
        "not",
        "but",
        "can",
        "could",
        "should",
        "shall",
        "obs",
    }
)


def _salient_terms(text: str) -> set[str]:
    """Content-bearing tokens of a text — the terms a faithful summary keeps."""
    return {t for t in _SALIENT_RE.findall(text.lower()) if t not in _STOPWORDS}


def _probe_term(text: str) -> str | None:
    """Longest salient term — a distinctive probe for FTS retrievability."""
    terms = _salient_terms(text)
    return max(terms, key=len) if terms else None


_COMPRESS_PROMPT = """You are a memory compression agent for supermem.
Compress the following recent observations into a single dense summary.
Keep all important facts, decisions, entities, and relationships.
Remove redundancy. Write in the third person. Be concise but complete.

Observations to compress:
{observations}

Compressed summary:"""


class MemoryCompressor:
    """
    After every SUPERMEM_COMPRESS_EVERY observation writes, compresses
    recent observations into a summary entry stored in SQLite.
    """

    def __init__(
        self,
        db: "DatabaseManager",
        model_client: "BaseModelClient | None" = None,
        compress_every: int = SUPERMEM_COMPRESS_EVERY,
    ) -> None:
        self._db = db
        self._model_client = model_client
        self._compress_every = compress_every
        self._session_counts: dict[int, int] = {}

    def set_model_client(self, client: "BaseModelClient") -> None:
        """Inject the model client after construction (deferred for startup order)."""
        self._model_client = client

    async def maybe_compress(self, session_id: int) -> None:
        """Increment per-session counter; compress when threshold is reached."""
        count = self._session_counts.get(session_id, 0) + 1
        self._session_counts[session_id] = count
        if count % self._compress_every != 0:
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
            model_client = self._model_client
            if model_client is None:
                return
            summary = await model_client.chat_completion(
                messages=[
                    {
                        "role": "user",
                        "content": _COMPRESS_PROMPT.format(observations=obs_text),
                    }
                ],
                model="",
                max_tokens=512,
            )
            summary = summary.strip()
            if not summary:
                return

            obs_ids = [o["id"] for o in obs_list if "id" in o]
            await self._db.write_summary(session_id, summary, obs_ids)
            # A summary is derived context, never the only retrievable authority.
            # Retaining source observations avoids silently destroying recall or
            # provenance when the summary is incomplete or incorrect.
            log.info(
                "memory_compressed",
                session_id=session_id,
                obs_count=len(obs_list),
                summary_len=len(summary),
                retained=len(obs_ids),
            )
        except Exception as exc:
            log.warning("compression_failed", session_id=session_id, error=str(exc))

    async def compress_to_budget(
        self,
        obs_list: list[dict[str, Any]],
        *,
        session_id: int | None = None,
        budget_chars: int = SUPERMEM_COMPRESS_BUDGET_CHARS,
        min_coverage: float = SUPERMEM_COMPRESS_MIN_COVERAGE,
    ) -> dict[str, Any]:
        """Compress observations into a bounded summary behind a
        retrievability gate.

        Source observations are archived ONLY when the summary (a) fits
        ``budget_chars``, (b) retains at least ``min_coverage`` of the
        sources' salient terms, and (c) is provably retrievable — written as
        an active observation and found by FTS *before* any source leaves the
        index. Otherwise sources stay active; a summary can never silently
        become the only authority.

        Returns a receipt dict describing the decision — never raises.
        """
        obs_ids = [int(o["id"]) for o in obs_list if o.get("id") is not None]
        receipt: dict[str, Any] = {
            "compressed": False,
            "reason": None,
            "obs_ids": obs_ids,
            "session_id": session_id,
            "budget_chars": budget_chars,
            "min_coverage": min_coverage,
            "coverage": 0.0,
            "summary_obs_id": None,
            "summary_len": 0,
            "archived": 0,
        }
        try:
            if self._model_client is None:
                receipt["reason"] = "no_model_client"
                return receipt
            if not obs_list:
                receipt["reason"] = "empty_input"
                return receipt

            obs_text = "\n\n".join(
                f"[{o.get('type', 'obs')} id={o.get('id')}]"
                f" {o.get('content', '')[:800]}"
                for o in obs_list
            )
            summary = (
                await self._model_client.chat_completion(
                    messages=[
                        {
                            "role": "user",
                            "content": _COMPRESS_PROMPT.format(observations=obs_text),
                        }
                    ],
                    model="",
                    max_tokens=max(budget_chars // 4, 64),
                )
            ).strip()
            receipt["summary_len"] = len(summary)
            if not summary:
                receipt["reason"] = "empty_summary"
                return receipt
            if len(summary) > budget_chars:
                receipt["reason"] = "over_budget"
                return receipt

            salient = set().union(
                *(_salient_terms(str(o.get("content") or "")) for o in obs_list)
            )
            coverage = (
                len(salient & _salient_terms(summary)) / len(salient)
                if salient
                else 1.0
            )
            receipt["coverage"] = round(coverage, 4)
            if coverage < min_coverage:
                receipt["reason"] = "coverage_below_gate"
                return receipt

            sid = (
                session_id
                if session_id is not None
                else int(obs_list[0].get("session_id") or -1)
            )
            if sid >= 0:
                await self._db.write_summary(sid, summary, obs_ids)
            source_ids = {o.get("source_id") for o in obs_list if o.get("source_id")}
            summary_obs_id = await self._db.write_observation(
                summary,
                session_id=sid if sid >= 0 else None,
                obs_type="summary",
                source_id=next(iter(source_ids)) if len(source_ids) == 1 else None,
            )
            receipt["summary_obs_id"] = summary_obs_id

            # Retrievability proof BEFORE archiving: the summary must be an
            # active observation that FTS actually finds.
            active = await self._db.active_obs_ids([summary_obs_id])
            probe = _probe_term(summary)
            found = await self._db.fts_search(probe, limit=10) if probe else []
            if summary_obs_id not in active or summary_obs_id not in found:
                receipt["reason"] = "summary_not_retrievable"
                return receipt

            archived = await self._db.archive_observations(obs_ids)
            receipt.update(compressed=True, archived=archived)
            log.info(
                "compress_to_budget_done",
                session_id=sid,
                obs_count=len(obs_ids),
                coverage=round(coverage, 3),
                archived=archived,
            )
            return receipt
        except Exception as exc:
            log.warning("compress_to_budget_failed", error=str(exc))
            receipt["reason"] = f"error:{type(exc).__name__}"
            return receipt
