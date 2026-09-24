"""Pure scoring metrics over benchmark cases and cited results.

Every function tolerates missing query_ids (zero results) and empty inputs,
returning 0.0 instead of raising.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from benchmarks.harness_types import BenchmarkCase, CitedResult
from benchmarks.oracle import CaseVerdict

Results = dict[str, list[CitedResult]]

# Recall cutoffs scored from a single rank-ordered result list. Cutoffs past
# the retrieved depth score against however many results exist — retrieve
# with k >= max(RECALL_CUTOFFS) when all entries should be meaningful.
RECALL_CUTOFFS = (1, 3, 5, 10, 30, 50)

_PROHIBITED_MAP = {
    "stale": "source_modified",
    "expired": "expired",
    "retracted": "retracted",
    "deleted": "source_deleted",
    "private": "private_canary",
}


def _results_for(results_by_query_id: Results, query_id: str) -> list[CitedResult]:
    return results_by_query_id.get(query_id, [])


def _applicable(
    cases: list[BenchmarkCase],
    results_by_query_id: Results,
) -> list[tuple[BenchmarkCase, list[CitedResult], int]]:
    """Non-expect_empty cases with at least one recall signal (verbatim
    needle or evidence-session URI)."""
    out = []
    for case in cases:
        if case.expected.expect_empty:
            continue
        if not case.expected.must_include and not case.expected.source_uris:
            continue
        results = _results_for(results_by_query_id, case.query_id)
        top_k = min(case.max_records, len(results))
        out.append((case, results, top_k))
    return out


def _is_relevant(case: BenchmarkCase, res: CitedResult) -> bool:
    """A result is relevant when it cites an evidence session (source_uris
    oracle) or its content carries a must_include needle."""
    if case.expected.source_uris:
        return res.source_uri in set(case.expected.source_uris)
    return any(m in res.content for m in case.expected.must_include)


def _recall_hit(case: BenchmarkCase, top: list[CitedResult]) -> bool:
    """Top-k hit: any evidence session (recall_any) or all needles present."""
    if case.expected.source_uris:
        # Session-level recall_any: any evidence-session hit in top-k.
        return any(_is_relevant(case, r) for r in top)
    contents = [r.content for r in top]
    return all(any(m in c for c in contents) for m in case.expected.must_include)


def recall_at_k(
    cases: list[BenchmarkCase], results_by_query_id: Results, k: int
) -> float:
    """Fraction of non-expect_empty cases hit in the top-k results."""
    applicable = [
        (case, results[: max(k, 0)])
        for case, results, _ in _applicable(cases, results_by_query_id)
    ]
    if not applicable:
        return 0.0
    hits = sum(1 for case, top in applicable if _recall_hit(case, top))
    return hits / len(applicable)


def recall_at_k_multi(
    cases: list[BenchmarkCase],
    results_by_query_id: Results,
    ks: Iterable[int] = RECALL_CUTOFFS,
) -> dict[str, float]:
    """recall_any at several cutoffs from one rank-ordered result list.

    Uses the same truncated-at-k view as ``recall_at_k``, so
    ``recall_at_k_multi(cases, res)[str(k)] == recall_at_k(cases, res, k)``.
    Multi-k separates ranking failures (hit at 30, miss at 10) from
    retrieval failures (miss at 50). Cutoffs past the retrieved depth score
    against however many results were returned.
    """
    applicable = [
        (case, results) for case, results, _ in _applicable(cases, results_by_query_id)
    ]
    out: dict[str, float] = {}
    for k in ks:
        kk = max(k, 0)
        hits = sum(1 for case, results in applicable if _recall_hit(case, results[:kk]))
        out[str(k)] = hits / len(applicable) if applicable else 0.0
    return out


def ndcg_at_k(
    cases: list[BenchmarkCase], results_by_query_id: Results, k: int = 10
) -> float:
    """Mean NDCG@k over applicable cases with binary per-rank relevance.

    A rank is relevant when the result cites an evidence session
    (``expected.source_uris``; each session credited once — re-citing the
    same session adds nothing) or, for needle-only cases, when its content
    carries a ``must_include`` term. DCG@k = Σ rel/log2(rank+1); IDCG@k is
    the best possible ordering given the number of evidence items — the
    count of distinct evidence sessions, else the needle count.
    """
    applicable = _applicable(cases, results_by_query_id)
    if not applicable:
        return 0.0
    kk = max(k, 0)
    total = 0.0
    for case, results, _ in applicable:
        seen_sessions: set[str] = set()
        dcg = 0.0
        for rank, res in enumerate(results[:kk], start=1):
            if not _is_relevant(case, res):
                continue
            if case.expected.source_uris:
                if res.source_uri in seen_sessions:
                    continue
                seen_sessions.add(res.source_uri)
            dcg += 1.0 / math.log2(rank + 1)
        n_ideal = (
            len(set(case.expected.source_uris))
            if case.expected.source_uris
            else len(case.expected.must_include)
        )
        idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(n_ideal, kk) + 1))
        if idcg > 0:
            total += dcg / idcg
    return total / len(applicable)


def recall_by_question_type(
    cases: list[BenchmarkCase], results_by_query_id: Results, k: int
) -> dict[str, dict[str, float | int]]:
    """recall@k grouped by ``expected.question_type`` (LongMemEval types).

    Only cases that participate in recall (non-expect_empty with a recall
    signal) count toward a bucket's ``n``; cases without a question_type
    land under ``"unknown"``.
    """
    groups: dict[str, list[tuple[BenchmarkCase, list[CitedResult]]]] = {}
    for case, results, _ in _applicable(cases, results_by_query_id):
        key = case.expected.question_type or "unknown"
        groups.setdefault(key, []).append((case, results))
    out: dict[str, dict[str, float | int]] = {}
    for key in sorted(groups):
        sub = groups[key]
        hits = sum(
            1 for case, results in sub if _recall_hit(case, results[: max(k, 0)])
        )
        out[key] = {"recall_at_k": hits / len(sub), "n": len(sub)}
    return out


def precision_at_k(
    cases: list[BenchmarkCase], results_by_query_id: Results, k: int
) -> float:
    """Relevant / total retrieved across non-expect_empty cases.

    A result is relevant when its content contains any must_include term.
    """
    applicable = [
        (case, results[: max(k, 0)])
        for case, results, _ in _applicable(cases, results_by_query_id)
    ]
    total = sum(len(top) for _, top in applicable)
    if total == 0:
        return 0.0
    relevant = 0
    for case, top in applicable:
        relevant += sum(1 for r in top if _is_relevant(case, r))
    return relevant / total


def mrr(cases: list[BenchmarkCase], results_by_query_id: Results) -> float:
    """Mean of 1/rank of the first relevant result over applicable cases."""
    applicable = [
        (case, results) for case, results, _ in _applicable(cases, results_by_query_id)
    ]
    if not applicable:
        return 0.0
    total = 0.0
    for case, results in applicable:
        rr = 0.0
        for rank, res in enumerate(results, start=1):
            if _is_relevant(case, res):
                rr = 1.0 / rank
                break
        total += rr
    return total / len(applicable)


def prohibited_recall(
    cases: list[BenchmarkCase], results_by_query_id: Results
) -> dict[str, float]:
    """Rate at which prohibited content surfaces for prohibited case types.

    A violation is either any result at all (for expect_empty cases) or the
    appearance of any must_exclude substring (e.g. private_canary cases where
    surfacing the public span is correct but the private canary string must
    never appear).
    """
    counts: dict[str, list[bool]] = {label: [] for label in _PROHIBITED_MAP}
    for case in cases:
        for label, case_type in _PROHIBITED_MAP.items():
            if case.expected.case_type != case_type:
                continue
            results = _results_for(results_by_query_id, case.query_id)
            violated = False
            if results and case.expected.expect_empty:
                violated = True
            elif results:
                violated = any(
                    term in res.content
                    for res in results
                    for term in case.expected.must_exclude
                )
            counts[label].append(violated)
    return {
        label: (sum(rates) / len(rates)) if rates else 0.0
        for label, rates in counts.items()
    }


def unknown_contamination(
    cases: list[BenchmarkCase], results_by_query_id: Results
) -> float:
    """Rate at which unknown_query cases return any result at all."""
    unknown = [c for c in cases if c.expected.case_type == "unknown_query"]
    if not unknown:
        return 0.0
    contaminated = sum(
        1 for case in unknown if _results_for(results_by_query_id, case.query_id)
    )
    return contaminated / len(unknown)


def citation_coverage(results_flat: list[CitedResult]) -> float:
    """Fraction of results with non-empty source_uri, source_span, source_digest."""
    if not results_flat:
        return 0.0
    covered = sum(
        1 for r in results_flat if r.source_uri and r.source_span and r.source_digest
    )
    return covered / len(results_flat)


def citation_verification_rate(verdicts: list[CaseVerdict]) -> float:
    """Fraction of verdicts with returned results whose citations verified.

    Verdicts with zero returned results carry no citation evidence and are
    excluded from the denominator.
    """
    eligible = [v for v in verdicts if v.details.get("citation_checked")]
    if not eligible:
        return 0.0
    ok = sum(1 for v in eligible if not v.details.get("citation_failures"))
    return ok / len(eligible)


def estimate_tokens(chars: int) -> int:
    """Rough token estimate (~4 chars/token) for receipt accounting only."""
    return math.ceil(chars / 4) if chars > 0 else 0


def _case_hit(case: BenchmarkCase, results: list[CitedResult], k: int) -> float:
    """1.0 when all must_include terms appear in the top-k results."""
    top = results[: max(k, 0)]
    return (
        1.0
        if all(any(m in r.content for r in top) for m in case.expected.must_include)
        else 0.0
    )


def context_stats(
    cases: list[BenchmarkCase], results_by_query_id: Results, k: int
) -> dict[str, float | int | None]:
    """Token-efficiency accounting: context injected per query.

    ``context_chars`` is the summed content length of the adapter's top-k
    results — the context a downstream consumer would ingest. Token figures
    are the ~4-chars-per-token estimate and labelled ``*_est`` accordingly.
    """
    chars_all: list[int] = []
    tokens_all: list[int] = []
    recalled_tokens: list[int] = []
    for case in cases:
        results = _results_for(results_by_query_id, case.query_id)
        chars = sum(len(r.content) for r in results[: max(k, 0)])
        chars_all.append(chars)
        tokens_all.append(estimate_tokens(chars))
        if case.expected.expect_empty or not case.expected.must_include:
            continue
        if _case_hit(case, results, k):
            recalled_tokens.append(estimate_tokens(chars))
    return {
        "avg_context_chars": (
            round(sum(chars_all) / len(chars_all), 1) if chars_all else 0.0
        ),
        "max_context_chars": max(chars_all) if chars_all else 0,
        "avg_context_tokens_est": (
            round(sum(tokens_all) / len(tokens_all), 1) if tokens_all else 0.0
        ),
        "total_context_tokens_est": sum(tokens_all),
        "avg_context_tokens_per_recall": (
            round(sum(recalled_tokens) / len(recalled_tokens), 1)
            if recalled_tokens
            else None
        ),
    }


def context_rot(
    cases: list[BenchmarkCase], results_by_query_id: Results, k: int
) -> dict[str, object]:
    """Context-rot probe: does recall degrade as injected context grows?

    Buckets recallable (non-expect_empty) cases into terciles by injected
    context size and reports per-tercile recall. ``detected`` is True when
    the largest-context tercile trails the smallest-context tercile by >=0.1
    recall, False when no rot is measured, and None when the corpus has too
    few recallable cases (<6) to say anything meaningful.
    """
    pts: list[tuple[int, float]] = []
    for case in cases:
        if case.expected.expect_empty or not case.expected.must_include:
            continue
        results = _results_for(results_by_query_id, case.query_id)
        chars = sum(len(r.content) for r in results[: max(k, 0)])
        pts.append((chars, _case_hit(case, results, k)))
    n = len(pts)
    if n < 6:
        return {
            "terciles": [],
            "recall_delta": None,
            "detected": None,
            "n_applicable": n,
        }
    pts.sort()
    third = max(1, n // 3)
    buckets = [b for b in (pts[:third], pts[third : 2 * third], pts[2 * third :]) if b]
    terciles = [
        {
            "context_chars_min": b[0][0],
            "context_chars_max": b[-1][0],
            "n": len(b),
            "recall": round(sum(h for _, h in b) / len(b), 4),
        }
        for b in buckets
    ]
    delta = terciles[0]["recall"] - terciles[-1]["recall"]
    return {
        "terciles": terciles,
        "recall_delta": round(delta, 4),
        "detected": delta >= 0.1,
        "n_applicable": n,
    }


def latency_percentiles(latencies_ms: list[float]) -> dict[str, float]:
    """p50/p95/p99 latency with linear interpolation; empty input -> 0.0."""
    if not latencies_ms:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    ordered = sorted(latencies_ms)

    def pct(fraction: float) -> float:
        if len(ordered) == 1:
            return float(ordered[0])
        pos = fraction * (len(ordered) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(ordered) - 1)
        weight = pos - lo
        return float(ordered[lo] + weight * (ordered[hi] - ordered[lo]))

    return {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}


def variance_rate(repeat_outcomes: list[list[bool]]) -> float:
    """Fraction of per-position outcomes that disagree across repeats.

    Positions beyond the shortest repeat length are ignored. Empty input or a
    single repeat yields 0.0.
    """
    usable = [run for run in repeat_outcomes if run]
    if len(usable) < 2:
        return 0.0
    width = min(len(run) for run in usable)
    if width == 0:
        return 0.0
    varying = 0
    for i in range(width):
        column = {run[i] for run in usable}
        if len(column) > 1:
            varying += 1
    return varying / width


def receipt_header(metrics: Mapping[str, Any]) -> str:
    """MemScore-style headline triple: recall / p50 latency / context tokens.

    Reads the aggregate metrics dict (``recall_at_k``, ``latency.p50``,
    ``context.avg_context_tokens_est``) and renders the one-line receipt
    header, e.g. ``recall@k 0.833 / p50 12.4ms / 1832tok``. Missing context
    stats render the token leg as ``n/a``.
    """
    recall = metrics.get("recall_at_k") or 0.0
    latency = metrics.get("latency") or {}
    p50 = latency.get("p50") or 0.0
    context = metrics.get("context") or {}
    tokens = context.get("avg_context_tokens_est")
    tok = f"{float(tokens):.0f}tok" if isinstance(tokens, (int, float)) else "n/a"
    return f"recall@k {float(recall):.3f} / p50 {float(p50):.1f}ms / {tok}"
