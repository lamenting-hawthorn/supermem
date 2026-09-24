"""Unit tests for benchmark receipt metrics: context stats + context rot."""

from __future__ import annotations

import pytest

from benchmarks.harness_types import BenchmarkCase, CitedResult, ExpectedOutcome
from benchmarks.scoring import context_rot, context_stats, estimate_tokens


def _result(content: str) -> CitedResult:
    return CitedResult(
        memory_id="1",
        memory_revision=1,
        content=content,
        source_uri="s.md",
        source_revision=1,
        source_span="s.md#whole",
        source_digest="d" * 64,
        retrieval_tier="test",
        retrieval_score=1.0,
        latency_ms=0.1,
    )


def _case(qid: str, must_include: list[str] | None = None, expect_empty=False):
    return BenchmarkCase(
        query_id=qid,
        query="q",
        expected=ExpectedOutcome(
            case_type="exact_positive" if not expect_empty else "expired",
            must_include=must_include or [],
            expect_empty=expect_empty,
        ),
    )


def test_estimate_tokens_is_chars_over_four() -> None:
    assert estimate_tokens(0) == 0
    assert estimate_tokens(3) == 1
    assert estimate_tokens(400) == 100
    assert estimate_tokens(401) == 101


def test_context_stats_accounts_per_case_context() -> None:
    cases = [
        _case("a", must_include=["alpha"]),
        _case("b", must_include=["beta"]),
        _case("c", expect_empty=True),
    ]
    results = {
        "a": [_result("alpha " + "x" * 94)],  # 100 chars, hit
        "b": [_result("nothing relevant")],  # 16 chars, miss
        "c": [],  # 0 chars
    }
    stats = context_stats(cases, results, k=10)
    assert stats["avg_context_chars"] == pytest.approx((100 + 16 + 0) / 3, abs=0.05)
    assert stats["max_context_chars"] == 100
    assert stats["total_context_tokens_est"] == 25 + 4 + 0
    # Only case "a" recalled → tokens-per-recall counts it alone.
    assert stats["avg_context_tokens_per_recall"] == 25


def test_context_rot_detects_degradation() -> None:
    # Small-context cases all hit; large-context cases all miss.
    cases = [_case(f"q{i}", must_include=[f"needle{i}"]) for i in range(9)]
    results = {
        f"q{i}": (
            [_result(f"needle{i} " + "s" * 10)]
            if i < 3
            else [_result("hay " + "x" * 500)]
        )
        for i in range(9)
    }
    rot = context_rot(cases, results, k=10)
    assert rot["detected"] is True
    assert rot["recall_delta"] == 1.0
    assert len(rot["terciles"]) == 3
    assert rot["terciles"][0]["recall"] == 1.0
    assert rot["terciles"][-1]["recall"] == 0.0


def test_context_rot_clean_when_recall_flat() -> None:
    cases = [_case(f"q{i}", must_include=[f"needle{i}"]) for i in range(6)]
    results = {f"q{i}": [_result(f"needle{i} " + "x" * (100 * i))] for i in range(6)}
    rot = context_rot(cases, results, k=10)
    assert rot["detected"] is False
    assert rot["recall_delta"] == 0.0


def test_context_rot_null_when_corpus_too_thin() -> None:
    cases = [_case(f"q{i}", must_include=[f"n{i}"]) for i in range(3)]
    rot = context_rot(cases, {}, k=10)
    assert rot["detected"] is None
    assert rot["recall_delta"] is None
    assert rot["n_applicable"] == 3


def test_context_rot_ignores_expect_empty_cases() -> None:
    cases = [_case(f"q{i}", must_include=[f"n{i}"]) for i in range(6)] + [
        _case("p1", expect_empty=True)
    ]
    results = {f"q{i}": [_result(f"n{i}")] for i in range(6)} | {
        "p1": [_result("should not count")]
    }
    rot = context_rot(cases, results, k=10)
    # The expect_empty case contributes no recall point.
    assert rot["n_applicable"] == 6
