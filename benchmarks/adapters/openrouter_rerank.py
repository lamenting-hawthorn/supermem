"""OpenRouter reranker scorer for benchmarks.

Injects an HTTP reranker into the product ``Reranker`` via its existing
``scorer`` injection point — no product-code changes. The scorer posts
``(query, [documents])`` to OpenRouter's Cohere-style ``/rerank`` endpoint
and returns per-pair relevance scores.

Env:
    OPENROUTER_API_KEY      required
    SUPERMEM_BENCH_RERANK_MODEL   default "qwen/qwen3-reranker-8b"

The response shape follows Cohere (``{"results": [{"index", "relevance_score"}]}``);
a ``{"rankings": [{"index", "logit"}]}`` fallback covers NIM-style replies.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Sequence

from supermem.logging import get_logger

log = get_logger(__name__)

_OPENROUTER_RERANK_URL = "https://openrouter.ai/api/v1/rerank"
_TIMEOUT_S = 60


def openrouter_rerank_scorer(
    model: str | None = None,
) -> "callable | None":
    """Return a ``Scorer``-compatible callable, or None when no key is set."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    model = model or os.getenv("SUPERMEM_BENCH_RERANK_MODEL", "qwen/qwen3-reranker-8b")

    def score(pairs: Sequence[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        query = pairs[0][0]
        docs = [doc for _q, doc in pairs]
        payload = {
            "model": model,
            "query": query,
            "documents": [{"type": "text", "text": d} for d in docs],
        }
        req = urllib.request.Request(
            _OPENROUTER_RERANK_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                body = json.loads(resp.read().decode())
        except Exception as exc:
            log.warning("openrouter_rerank_failed", error=str(exc))
            return [0.0] * len(pairs)
        return _extract_scores(body, len(pairs))

    return score


def _extract_scores(body: dict, n: int) -> list[float]:
    """Map a rerank response back to input order; missing → 0."""
    scores = [0.0] * n
    entries = body.get("results") or body.get("rankings") or []
    for entry in entries:
        idx = entry.get("index")
        val = entry.get("relevance_score", entry.get("logit", 0.0))
        if isinstance(idx, int) and 0 <= idx < n:
            scores[idx] = float(val)
    return scores
