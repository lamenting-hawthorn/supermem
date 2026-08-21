"""Pure scoring metrics over benchmark cases and cited results.

Every function tolerates missing query_ids (zero results) and empty inputs,
returning 0.0 instead of raising.
"""

from __future__ import annotations

from benchmarks.harness_types import BenchmarkCase, CitedResult
from benchmarks.oracle import CaseVerdict

Results = dict[str, list[CitedResult]]

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
    """Non-expect_empty cases that have at least one must_include term."""
    out = []
    for case in cases:
        if case.expected.expect_empty:
            continue
        if not case.expected.must_include:
            continue
        results = _results_for(results_by_query_id, case.query_id)
        top_k = min(case.max_records, len(results))
        out.append((case, results, top_k))
    return out


def recall_at_k(
    cases: list[BenchmarkCase], results_by_query_id: Results, k: int
) -> float:
    """Fraction of non-expect_empty cases where all must_include are in top-k."""
    applicable = [
        (case, results[: max(k, 0)])
        for case, results, _ in _applicable(cases, results_by_query_id)
    ]
    if not applicable:
        return 0.0
    hits = 0
    for case, top in applicable:
        contents = [r.content for r in top]
        if all(any(m in c for c in contents) for m in case.expected.must_include):
            hits += 1
    return hits / len(applicable)


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
        terms = case.expected.must_include
        relevant += sum(1 for r in top if any(m in r.content for m in terms))
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
        terms = case.expected.must_include
        rr = 0.0
        for rank, res in enumerate(results, start=1):
            if any(t in res.content for t in terms):
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
