"""Pass/fail oracle for BM-0 benchmark cases.

Decides whether one (BenchmarkCase, list[CitedResult]) pair satisfies the
frozen acceptance contract in docs/handoffs/01-bm0-acceptance-contract.md.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.harness_types import BenchmarkCase, CitedResult


@dataclass
class CaseVerdict:
    """Outcome of judging a single benchmark case."""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def _digest_of(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _citation_failures(results: list[CitedResult], workspace: Path | None) -> list[str]:
    failures: list[str] = []
    for idx, res in enumerate(results):
        missing = [
            name
            for name, value in (
                ("source_uri", res.source_uri),
                ("source_span", res.source_span),
                ("source_digest", res.source_digest),
            )
            if not value
        ]
        if missing:
            failures.append(
                f"missing_citation_fields[{idx}]({res.memory_id}): "
                f"{','.join(missing)}"
            )
        if workspace is not None and res.source_uri and res.source_digest:
            source_path = workspace / res.source_uri
            if source_path.is_file():
                actual = _digest_of(source_path)
                if actual is not None and actual != res.source_digest:
                    failures.append(
                        f"stale_citation[{idx}]({res.memory_id}): digest for "
                        f"{res.source_uri} does not match current source bytes"
                    )
    return failures


def judge_case(
    case: BenchmarkCase,
    results: list[CitedResult],
    workspace: Path | None = None,
) -> CaseVerdict:
    """Judge one case against its expected outcome."""
    reasons: list[str] = []
    details: dict[str, Any] = {
        "query_id": case.query_id,
        "case_type": case.expected.case_type,
        "temporal_bound": case.temporal_bound,
        "result_count": len(results),
    }

    cit_failures = _citation_failures(results, workspace)
    details["citation_checked"] = bool(results)
    details["citation_failures"] = cit_failures

    if case.expected.expect_empty:
        if results:
            snippets = [r.content[:80] for r in results]
            reasons.append(
                f"expected_empty_but_got_{len(results)}_results: {snippets!r}"
            )
    else:
        contents = [r.content for r in results]
        for needle in case.expected.must_include:
            if not any(needle in c for c in contents):
                reasons.append(f"must_include_missing: {needle!r}")
        for banned in case.expected.must_exclude:
            offenders = [i for i, c in enumerate(contents) if banned in c]
            if offenders:
                reasons.append(f"must_exclude_violated[{offenders}]: {banned!r}")

    if cit_failures:
        reasons.extend(cit_failures)

    details["reason_count"] = len(reasons)
    return CaseVerdict(passed=not reasons, reasons=reasons, details=details)


def judge_cases(
    cases: list[BenchmarkCase],
    results_by_query_id: dict[str, list[CitedResult]],
    workspace: Path | None = None,
) -> list[CaseVerdict]:
    """Judge every case, tolerating missing query_ids (treated as empty)."""
    verdicts: list[CaseVerdict] = []
    for case in cases:
        results = results_by_query_id.get(case.query_id, [])
        verdicts.append(judge_case(case, results, workspace))
    return verdicts


def summary(verdicts: list[CaseVerdict]) -> dict[str, int]:
    passed = sum(1 for v in verdicts if v.passed)
    failed = len(verdicts) - passed
    return {"passed": passed, "failed": failed, "total": len(verdicts)}
