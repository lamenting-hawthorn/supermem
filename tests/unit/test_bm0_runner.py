"""Regression coverage for the frozen BM-0 fresh-state harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import benchmarks.runner as runner
from supermem.local_cited_memory import RetrievalTimeoutError


def test_bm0_runner_is_complete_redacted_and_deterministic(tmp_path: Path) -> None:
    first_dir, first = runner.run(tmp_path / "first")
    second_dir, second = runner.run(tmp_path / "second")
    assert first["case_count"] == 12
    assert first["passed"] == 12
    assert (
        first["failed"]
        == first["timeouts"]
        == first["unsupported"]
        == first["inconclusive"]
        == 0
    )
    assert first["prohibited_result_count"] == 0
    assert first["normalized_run_digest"] == second["normalized_run_digest"]
    assert "private-canary" not in (first_dir / "cases.jsonl").read_text()
    cases = [
        json.loads(line)
        for line in (second_dir / "cases.jsonl").read_text().splitlines()
    ]
    assert [case["id"] for case in cases] == [
        f"BM0-{number:02d}" for number in range(1, 13)
    ]


def test_runner_records_each_exception_and_continues(tmp_path: Path) -> None:
    def faults(case: dict[str, str], db_path: Path) -> dict[str, object]:
        del db_path
        if case["id"] == "BM0-01":
            raise RetrievalTimeoutError()
        if case["id"] == "BM0-02":
            raise NotImplementedError()
        if case["id"] == "BM0-03":
            raise RuntimeError()
        if case["id"] == "BM0-04":
            raise ValueError("private-canary must never be written")
        return runner._exercise(case, tmp_path / f"{case['id']}.sqlite")

    artifact_dir, metrics = runner.run(tmp_path / "faults", exercise=faults)
    reports = [
        json.loads(line)
        for line in (artifact_dir / "cases.jsonl").read_text().splitlines()
    ]
    assert [report["status"] for report in reports[:4]] == [
        "timeout",
        "unsupported",
        "inconclusive",
        "failed",
    ]
    assert reports[-1]["status"] == "passed"
    assert (
        metrics["timeout"]
        == metrics["unsupported"]
        == metrics["inconclusive"]
        == metrics["failed"]
        == 1
    )
    assert "private-canary" not in (artifact_dir / "cases.jsonl").read_text()


def test_oracle_parity_and_candidate_identity_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("first")
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    first = runner.candidate_identity(("candidate.py",))["digest"]
    candidate.write_text("second")
    assert runner.candidate_identity(("candidate.py",))["digest"] != first
    broken = tmp_path / "dataset"
    broken.mkdir()
    (broken / "dataset.jsonl").write_text(
        '{"id":"BM0-01","scenario":"unknown","query":"x"}\n'
    )
    (broken / "expected-results.json").write_text(
        '{"expected_case_count":1,"cases":[]}'
    )
    monkeypatch.setattr(runner, "DATASET_DIR", broken)
    with pytest.raises(ValueError, match="case IDs differ"):
        runner._load_cases()


def test_oracle_status_is_consumed_and_cannot_silently_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = tmp_path / "dataset"
    frozen.mkdir()
    (frozen / "dataset.jsonl").write_text(
        '{"id":"BM0-01","scenario":"unknown","query":"x"}\n'
    )
    (frozen / "expected-results.json").write_text(
        '{"expected_case_count":1,"cases":[{"id":"BM0-01","status":"failed","must_include":"","must_exclude":""}]}'
    )
    monkeypatch.setattr(runner, "DATASET_DIR", frozen)
    with pytest.raises(ValueError, match="only expected passed"):
        runner._load_cases()
