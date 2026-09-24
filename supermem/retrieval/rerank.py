"""Optional local cross-encoder reranker for the hybrid retrieval path.

Gated behind ``SUPERMEM_RERANKER=1``/``true`` (default OFF). When enabled,
a local cross-encoder re-ranks the top fused RRF candidates by
query-document relevance. Model name is configurable via
``SUPERMEM_RERANK_MODEL`` and defaults to
``Xenova/ms-marco-MiniLM-L-6-v2`` (~80MB ONNX).

Backed by ``fastembed``'s ``TextCrossEncoder`` — a declared dependency, so
the reranker works on a standard install without the heavyweight
sentence-transformers/torch stack. The model is still loaded lazily and
guarded in try/except: if fastembed is missing or the model fails to load,
``available`` is False and ``rerank()`` is a no-op passthrough that never
raises.

Apache 2.0 — original implementation.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from typing import Any

from supermem.logging import get_logger

log = get_logger(__name__)

SUPERMEM_RERANK_MODEL: str = os.getenv(
    "SUPERMEM_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2"
)
"""Cross-encoder model name used by the singleton reranker."""

RERANKER_ENABLED: bool = os.getenv("SUPERMEM_RERANKER", "false").strip().lower() in (
    "1",
    "true",
)
"""Master switch for the reranker stage in hybrid retrieval. Default OFF."""

RERANK_CAP: int = 50
"""Maximum fused candidates fed to the cross-encoder per search."""

_reranker_singleton: "Reranker | None" = None

Scorer = Callable[[Sequence[tuple[str, str]]], Sequence[float]]
"""Injectable ``(query, content)`` pair → score function, for tests."""


class Reranker:
    """Local cross-encoder reranker.

    Wraps a ``fastembed.rerank.cross_encoder.TextCrossEncoder`` loaded
    lazily on first use. All model work runs in a worker thread via
    ``asyncio.to_thread`` so the event loop is never blocked.

    Args:
        model_name: fastembed cross-encoder model id. Defaults to
            ``SUPERMEM_RERANK_MODEL``.
        scorer: Optional scoring function used instead of the model. Tests
            inject a deterministic stub here to unit-test ranking logic.
    """

    def __init__(
        self,
        model_name: str | None = None,
        scorer: Scorer | None = None,
    ) -> None:
        self._model_name = model_name or SUPERMEM_RERANK_MODEL
        self._scorer = scorer
        self._model: Any | None = None

    @property
    def available(self) -> bool:
        """True when usable: injected scorer, or model loaded / loadable."""
        if self._scorer is not None:
            return True
        if self._model is not None:
            return True
        try:
            import fastembed  # noqa: F401
        except Exception:
            return False
        try:
            self._ensure_model()
            return True
        except Exception as exc:
            log.warning(
                "rerank_model_load_failed",
                error=str(exc),
                model=self._model_name,
            )
            return False

    async def rerank(self, query: str, candidates: list[Any]) -> list[Any]:
        """Score candidates against ``query`` and reorder by score desc.

        Candidates are dicts (observation records) or objects with a
        ``content`` attribute. Each returned candidate carries a
        ``rerank_score`` and, when present, its ``metadata["rerank_score"]``.

        Never raises: on any failure it logs a structured warning and
        returns the candidates unchanged.
        """
        if not candidates:
            return []
        if not self.available:
            return candidates
        try:
            documents = [self._content(c) for c in candidates]
            scores = await asyncio.to_thread(self._predict, query, documents)
        except Exception as exc:
            log.warning("rerank_failed", error=str(exc), candidates=len(candidates))
            return candidates
        if len(scores) != len(candidates):
            log.warning(
                "rerank_score_mismatch",
                expected=len(candidates),
                got=len(scores),
            )
            return candidates
        scored = list(zip(candidates, scores))
        scored.sort(key=lambda item: item[1], reverse=True)
        for candidate, score in scored:
            self._attach_score(candidate, float(score))
        return [candidate for candidate, _score in scored]

    # ── Private ───────────────────────────────────────────────────────────────

    def _predict(self, query: str, documents: Sequence[str]) -> Sequence[float]:
        if self._scorer is not None:
            return self._scorer([(query, doc) for doc in documents])
        if self._model is None:
            self._ensure_model()
        assert self._model is not None
        return list(self._model.rerank(query, list(documents)))

    def _ensure_model(self) -> None:
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(model_name=self._model_name)

    @staticmethod
    def _content(candidate: Any) -> str:
        if isinstance(candidate, dict):
            return candidate.get("content", "")
        return getattr(candidate, "content", "")

    @staticmethod
    def _attach_score(candidate: Any, score: float) -> None:
        if isinstance(candidate, dict):
            candidate["rerank_score"] = score
            metadata = candidate.get("metadata")
        else:
            setattr(candidate, "rerank_score", score)
            metadata = getattr(candidate, "metadata", None)
        if isinstance(metadata, dict):
            metadata["rerank_score"] = score


def get_reranker() -> Reranker:
    """Return the process-wide singleton ``Reranker`` (lazy model load)."""
    global _reranker_singleton
    if _reranker_singleton is None:
        _reranker_singleton = Reranker()
    return _reranker_singleton
