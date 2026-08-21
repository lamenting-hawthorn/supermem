"""Tests for benchmarks.scoring."""

from __future__ import annotations

import pytest

from benchmarks import scoring
from benchmarks.harness_types import BenchmarkCase, CitedResult, ExpectedOutcome
from benchmarks.oracle import CaseVerdict


def mk_result(
    memory_id: str = "m1",
    content: str = "alpha fact",
    latency: float = 10.0,
    uri: str = "notes/a.md",
) -> CitedResult:
    return CitedResult(
        memory_id=memory_id,
        memory_revision=1,
        content=content,
        source_uri=uri,
        source_revision=1,
        source_span="L1-L2",
        source_digest="0" * 64,
        retrieval_tier="fts",
        retrieval_score=1.0,
        latency_ms=latency,
    )


def mk_case(
    query_id: str,
    case_type: str = "exact_positive",
    must_include: list[str] | None = None,
    must_exclude: list[str] | None = None,
    expect_empty: bool = False,
    max_records: int = 10,
) -> BenchmarkCase:
    return BenchmarkCase(
        query_id=query_id,
        query="q",
        max_records=max_records,
        expected=ExpectedOutcome(
            case_type=case_type,
            must_include=must_include or [],
            must_exclude=must_exclude or [],
            expect_empty=expect_empty,
        ),
    )


class TestRecallAtK:
    def test_all_found(self):
        cases = [mk_case("q1", must_include=["alpha", "beta"])]
        results = {"q1": [mk_result("m1", "alpha fact"), mk_result("m2", "beta fact")]}
        assert scoring.recall_at_k(cases, results, 2) == 1.0

    def test_partial_miss(self):
        cases = [mk_case("q1", must_include=["alpha", "zeta"])]
        results = {"q1": [mk_result("m1", "alpha fact")]}
        assert scoring.recall_at_k(cases, results, 5) == 0.0

    def test_k_truncates(self):
        cases = [mk_case("q1", must_include=["deep"])]
        results = {
            "q1": [
                mk_result("m1", "filler one"),
                mk_result("m2", "has deep"),
                mk_result("m3", "also deep"),
            ]
        }
        assert scoring.recall_at_k(cases, results, 1) == 0.0
        assert scoring.recall_at_k(cases, results, 2) == 1.0

    def test_expect_empty_cases_excluded(self):
        cases = [mk_case("q1", expect_empty=True)]
        results = {"q1": [mk_result()]}
        assert scoring.recall_at_k(cases, results, 3) == 0.0

    def test_missing_query_id_counts_against_recall(self):
        cases = [mk_case("q1", must_include=["alpha"])]
        assert scoring.recall_at_k(cases, {}, 3) == 0.0


class TestPrecisionAtK:
    def test_mixed_relevance(self):
        cases = [mk_case("q1", must_include=["alpha"], max_records=2)]
        results = {"q1": [mk_result("m1", "alpha hit"), mk_result("m2", "noise")]}
        assert scoring.precision_at_k(cases, results, 2) == pytest.approx(0.5)

    def test_no_results(self):
        cases = [mk_case("q1", must_include=["alpha"])]
        assert scoring.precision_at_k(cases, {}, 5) == 0.0


class TestMRR:
    def test_known_ranks(self):
        # q1 first relevant at rank 1 -> 1.0; q2 at rank 3 -> 1/3.
        cases = [
            mk_case("q1", must_include=["hit"]),
            mk_case("q2", must_include=["hit"]),
        ]
        results = {
            "q1": [mk_result("a", "hit here")],
            "q2": [
                mk_result("b", "no"),
                mk_result("c", "still no"),
                mk_result("d", "hit late"),
            ],
        }
        assert scoring.mrr(cases, results) == pytest.approx((1.0 + 1 / 3) / 2)

    def test_never_relevant_scores_zero(self):
        cases = [mk_case("q1", must_include=["missing term"])]
        results = {"q1": [mk_result("a", "irrelevant")]}
        assert scoring.mrr(cases, results) == 0.0

    def test_missing_results_zero(self):
        cases = [mk_case("q1", must_include=["x"])]
        assert scoring.mrr(cases, {}) == 0.0


class TestProhibitedRecall:
    def test_keys_and_rates(self):
        cases = [
            # Stale detection keys off must_exclude (the superseded content).
            mk_case("mod", case_type="source_modified", must_exclude=["Berlin office"]),
            mk_case("gone", case_type="source_deleted"),
            mk_case("priv", case_type="private_canary", must_exclude=["PIN 4491"]),
            mk_case("ok", case_type="exact_positive"),
        ]
        results = {
            "mod": [mk_result(content="still showing the Berlin office revision")],
            # Public span surfacing is fine; only the canary string violates.
            "priv": [mk_result(content="public budget review notes")],
        }
        rates = scoring.prohibited_recall(cases, results)
        assert set(rates) == {"stale", "expired", "retracted", "deleted", "private"}
        assert rates["stale"] == pytest.approx(1.0)
        assert rates["deleted"] == pytest.approx(0.0)
        assert rates["private"] == pytest.approx(0.0)
        assert rates["expired"] == 0.0

    def test_private_canary_violation_on_must_exclude_hit(self):
        cases = [
            mk_case("priv", case_type="private_canary", must_exclude=["PIN 4491"]),
        ]
        results = {"priv": [mk_result(content="oops PIN 4491 leaked")]}
        rates = scoring.prohibited_recall(cases, results)
        assert rates["private"] == pytest.approx(1.0)

    def test_no_prohibited_cases(self):
        assert scoring.prohibited_recall([], {}) == {
            k: 0.0 for k in ("stale", "expired", "retracted", "deleted", "private")
        }


class TestUnknownContamination:
    def test_contaminated(self):
        cases = [
            mk_case("u1", case_type="unknown_query"),
            mk_case("u2", case_type="unknown_query"),
            mk_case("pos", case_type="exact_positive", must_include=["x"]),
        ]
        results = {"u1": [mk_result()], "pos": [mk_result()]}
        assert scoring.unknown_contamination(cases, results) == pytest.approx(0.5)

    def test_clean_unknown(self):
        cases = [mk_case("u1", case_type="unknown_query")]
        assert scoring.unknown_contamination(cases, {"u1": []}) == 0.0
        assert scoring.unknown_contamination([], {}) == 0.0


class TestCitationCoverage:
    def test_full_coverage(self):
        assert scoring.citation_coverage([mk_result(), mk_result("m2")]) == 1.0

    def test_missing_field(self):
        bad = mk_result()
        bad.source_digest = ""
        assert scoring.citation_coverage([mk_result(), bad]) == pytest.approx(0.5)

    def test_empty(self):
        assert scoring.citation_coverage([]) == 0.0


class TestCitationVerificationRate:
    def test_rate_over_eligible_verdicts(self):
        good = CaseVerdict(passed=True, details={"citation_checked": True})
        bad = CaseVerdict(
            passed=False,
            reasons=["stale_citation[0]"],
            details={"citation_checked": True, "citation_failures": ["stale"]},
        )
        empty = CaseVerdict(passed=True, details={"citation_checked": False})
        rate = scoring.citation_verification_rate([good, bad, empty])
        assert rate == pytest.approx(0.5)

    def test_empty_verdicts(self):
        assert scoring.citation_verification_rate([]) == 0.0


class TestLatencyPercentiles:
    def test_known_inputs(self):
        latencies = [float(i) for i in range(1, 101)]  # 1..100
        p = scoring.latency_percentiles(latencies)
        assert p["p50"] == pytest.approx(50.5)
        assert p["p95"] == pytest.approx(95.05)
        assert p["p99"] == pytest.approx(99.01)

    def test_single_and_empty(self):
        assert scoring.latency_percentiles([42.0])["p50"] == pytest.approx(42.0)
        assert scoring.latency_percentiles([]) == {
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
        }


class TestVarianceRate:
    def test_identical_runs(self):
        runs = [[True, False, True], [True, False, True]]
        assert scoring.variance_rate(runs) == 0.0

    def test_one_varying_position(self):
        runs = [[True, True], [True, False]]
        assert scoring.variance_rate(runs) == pytest.approx(0.5)

    def test_ignores_ragged_tails_and_single_run(self):
        runs = [[True, True, True], [True]]
        assert scoring.variance_rate(runs) == 0.0
        assert scoring.variance_rate([[True]]) == 0.0
        assert scoring.variance_rate([]) == 0.0


class TestEmptyInputSafety:
    @pytest.mark.parametrize(
        "fn",
        [
            lambda: scoring.recall_at_k([], {}, 5),
            lambda: scoring.precision_at_k([], {}, 5),
            lambda: scoring.mrr([], {}),
            lambda: scoring.unknown_contamination([], {}),
        ],
    )
    def test_returns_zero(self, fn):
        assert fn() == 0.0
