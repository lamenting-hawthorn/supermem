"""Tests for benchmarks.oracle."""

from __future__ import annotations

import hashlib
from pathlib import Path

from benchmarks.harness_types import BenchmarkCase, CitedResult, ExpectedOutcome
from benchmarks.oracle import judge_case, judge_cases, summary


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mk_result(
    content: str = "alpha fact",
    uri: str = "notes/a.md",
    digest: str = "0" * 64,
    memory_id: str = "m1",
) -> CitedResult:
    return CitedResult(
        memory_id=memory_id,
        memory_revision=1,
        content=content,
        source_uri=uri,
        source_revision=1,
        source_span="L1-L2",
        source_digest=digest,
        retrieval_tier="fts",
        retrieval_score=1.0,
        latency_ms=5.0,
    )


def mk_case(
    query_id: str = "q1",
    case_type: str = "exact_positive",
    must_include: list[str] | None = None,
    must_exclude: list[str] | None = None,
    expect_empty: bool = False,
) -> BenchmarkCase:
    return BenchmarkCase(
        query_id=query_id,
        query="q",
        expected=ExpectedOutcome(
            case_type=case_type,
            must_include=must_include or [],
            must_exclude=must_exclude or [],
            expect_empty=expect_empty,
        ),
    )


class TestExpectEmpty:
    def test_empty_results_pass(self):
        verdict = judge_case(mk_case(expect_empty=True), [])
        assert verdict.passed
        assert verdict.reasons == []

    def test_any_result_fails_with_snippet(self):
        results = [mk_result(content="secret private canary leak")]
        verdict = judge_case(
            mk_case(case_type="private_canary", expect_empty=True), results
        )
        assert not verdict.passed
        assert any("private canary" in r for r in verdict.reasons)


class TestIncludeExclude:
    def test_must_include_all_present(self):
        results = [mk_result("has alpha"), mk_result("has beta")]
        verdict = judge_case(mk_case(must_include=["alpha", "beta"]), results)
        assert verdict.passed

    def test_must_include_missing(self):
        verdict = judge_case(mk_case(must_include=["absent"]), [mk_result()])
        assert not verdict.passed
        assert any("must_include_missing" in r for r in verdict.reasons)

    def test_must_exclude_violation(self):
        results = [mk_result("old revision text")]
        verdict = judge_case(mk_case(must_exclude=["old revision"]), results)
        assert not verdict.passed
        assert any("must_exclude_violated" in r for r in verdict.reasons)

    def test_missing_query_id_treated_as_empty(self):
        cases = [mk_case(query_id="ghost", must_include=["x"])]
        verdicts = judge_cases(cases, {})
        assert len(verdicts) == 1
        assert not verdicts[0].passed


class TestCitationGate:
    def test_missing_fields_fail(self):
        bad = mk_result()
        bad.source_uri = ""
        verdict = judge_case(mk_case(), [bad])
        assert not verdict.passed
        assert any("missing_citation_fields" in r for r in verdict.reasons)

    def test_workspace_none_only_requires_nonempty(self):
        verdict = judge_case(mk_case(), [mk_result(digest="f" * 64)], None)
        assert verdict.passed

    def test_matching_digest_passes(self, tmp_path: Path):
        data = b"current source bytes\n"
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "a.md").write_bytes(data)
        result = mk_result(digest=sha256_hex(data))
        verdict = judge_case(mk_case(), [result], tmp_path)
        assert verdict.passed

    def test_stale_citation_fails_after_mutation(self, tmp_path: Path):
        path = tmp_path / "notes" / "a.md"
        path.parent.mkdir()
        path.write_bytes(b"original revision\n")
        stale_digest = sha256_hex(path.read_bytes())

        # Mutate the file between digest computation and judging.
        path.write_bytes(b"mutated revision\n")

        verdict = judge_case(mk_case(), [mk_result(digest=stale_digest)], tmp_path)
        assert not verdict.passed
        assert any("stale_citation" in r for r in verdict.reasons)
        assert any("does not match current source bytes" in r for r in verdict.reasons)

    def test_absent_file_skips_digest_check(self, tmp_path: Path):
        verdict = judge_case(mk_case(), [mk_result()], tmp_path)
        assert verdict.passed


class TestDetailsAndBatch:
    def test_temporal_bound_recorded(self):
        case = mk_case()
        case.temporal_bound = {"as_of": 1234.5}
        verdict = judge_case(case, [])
        assert verdict.details["temporal_bound"] == {"as_of": 1234.5}

    def test_judge_cases_and_summary(self):
        ok = mk_case("ok", must_include=["alpha"])
        bad = mk_case("bad", expect_empty=True, case_type="unknown_query")
        verdicts = judge_cases([ok, bad], {"ok": [mk_result()], "bad": [mk_result()]})
        assert len(verdicts) == 2
        stats = summary(verdicts)
        assert stats == {"passed": 1, "failed": 1, "total": 2}

    def test_summary_empty(self):
        assert summary([]) == {"passed": 0, "failed": 0, "total": 0}
