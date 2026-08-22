"""Tests for the LongMemEval → competitive-harness converter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.adapters.longmemeval_convert import convert, main, parse_epoch
from benchmarks.harness_types import load_cases, load_manifest

SAMPLE_RECORDS = [
    {
        "question_id": "lm-1",
        "question_type": "multi-session",
        "question": "What camera does the user want to buy?",
        "answer": "The user wants a Sony A7IV camera.",
        "question_date": "2023/05/20 (Sat) 14:00",
        "haystack_date": "2023/05/18 (Thu) 09:30",
        "haystack_sessions": [
            [
                {"field": "user", "value": "I am thinking about the Sony A7IV."},
                {"field": "assistant", "value": "Great choice for video work!"},
            ]
        ],
    },
    {
        "question_id": "lm-2",
        "question_type": "abstention",
        "question": "What is my uncle's phone number?",
        "answer": "The conversation does not mention it.",
        "haystack_sessions": [
            [{"field": "user", "value": "I had coffee this morning."}]
        ],
    },
    {
        "question_id": "lm-3",
        "question_type": "temporal_reasoning",
        "question": "When did we discuss the camera?",
        "answer": "2023",
        "question_date": "2023/06/01 (Thu) 10:00",
        "haystack_date": "2023/05/18 (Thu) 09:30",
        "haystack_sessions": [
            [{"field": "user", "value": "Let's talk about the Sony A7IV today."}]
        ],
    },
    "not-valid-json{{{",
    {"unrelated": True},
]


@pytest.fixture
def input_jsonl(tmp_path: Path) -> Path:
    path = tmp_path / "longmemeval.jsonl"
    lines = [r if isinstance(r, str) else json.dumps(r) for r in SAMPLE_RECORDS]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_happy_path_conversion(input_jsonl: Path, tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    stats = convert(input_jsonl, outdir)

    assert stats["n_records_in"] == 3
    assert stats["n_cases_out"] == 3
    assert stats["n_skipped"] == 2  # bad json + missing question_id/question

    cases = load_cases(outdir)
    assert [c.query_id for c in cases] == ["lm-1", "lm-2", "lm-3"]
    manifest = load_manifest(outdir)
    assert manifest["mutations"] == []
    assert manifest["n_cases"] == 3
    # Sources rendered as markdown with frontmatter where dates parse.
    src = (outdir / "sources" / "session-0.md").read_text(encoding="utf-8")
    assert src.startswith("---\nobserved_at:")
    assert "- user:" in src


def test_abstention_maps_to_expect_empty(input_jsonl: Path, tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    cases = {c.query_id: c for c in load_cases(outdir)} if outdir.exists() else {}
    convert(input_jsonl, outdir)
    cases = {c.query_id: c for c in load_cases(outdir)}
    assert cases["lm-2"].expected.case_type == "unknown_query"
    assert cases["lm-2"].expected.expect_empty is True


def test_temporal_reasoning_gets_temporal_bound(
    input_jsonl: Path, tmp_path: Path
) -> None:
    outdir = tmp_path / "out"
    convert(input_jsonl, outdir)
    cases = {c.query_id: c for c in load_cases(outdir)}
    assert cases["lm-3"].expected.case_type == "effective_interval"
    tb = cases["lm-3"].temporal_bound
    assert tb is not None and tb["as_of"] == pytest.approx(
        parse_epoch("2023/05/18 (Thu) 09:30")
    )


def test_must_include_substrings_verified_against_file(
    input_jsonl: Path, tmp_path: Path
) -> None:
    outdir = tmp_path / "out"
    convert(input_jsonl, outdir)
    cases = {c.query_id: c for c in load_cases(outdir)}
    needles = cases["lm-1"].expected.must_include
    assert needles, "answer words like 'sony'/'camera' should survive verification"
    file_text = (
        (outdir / "sources" / "session-0.md").read_text(encoding="utf-8").lower()
    )
    assert all(n in file_text for n in needles)


def test_max_cases_cap(input_jsonl: Path, tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    stats = convert(input_jsonl, outdir, max_cases=1)
    assert stats["n_cases_out"] == 1
    assert len(load_cases(outdir)) == 1


def test_cli_missing_input_returns_2() -> None:
    assert main(["--input", "/nonexistent/x.jsonl"]) == 2
