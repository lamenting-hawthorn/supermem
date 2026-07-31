"""Deterministic fresh-temp runner for frozen BM-0 Markdown fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from supermem.local_cited_memory import (
    LocalCitedMemory,
    RetrievalQueryV1,
    RetrievalTimeoutError,
)

DATASET_DIR = Path(__file__).parent / "datasets" / "bm0-local"
REPO_ROOT = Path(__file__).parents[1]
CANDIDATE_PATHS = (
    "supermem/local_cited_memory.py",
    "supermem/privacy/filter.py",
    "supermem/storage/database.py",
    "supermem/indexer/vault.py",
    "supermem/capture/compressor.py",
    "benchmarks",
    "tests/unit/test_database.py",
    "tests/unit/test_privacy.py",
    "tests/unit/test_vault_indexer.py",
    "tests/unit/test_local_cited_memory.py",
    "tests/unit/test_bm0_runner.py",
)


def _digest_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_identity(paths: tuple[str, ...] = CANDIDATE_PATHS) -> dict[str, Any]:
    """Hash every BM-0 candidate byte; HEAD alone is not a candidate identity."""
    manifest: list[dict[str, str]] = []
    for relative in paths:
        path = REPO_ROOT / relative
        files = (
            sorted(
                item
                for item in path.rglob("*")
                if item.is_file()
                and "__pycache__" not in item.parts
                and item.suffix != ".pyc"
            )
            if path.is_dir()
            else [path]
        )
        for file_path in files:
            manifest.append(
                {
                    "path": str(file_path.relative_to(REPO_ROOT)),
                    "sha256": _digest_path(file_path),
                }
            )
    manifest.sort(key=lambda item: item["path"])
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return {"digest": hashlib.sha256(encoded).hexdigest(), "manifest": manifest}


def _git_head() -> str:
    head = (REPO_ROOT / ".git").read_text().strip()
    if head.startswith("gitdir: "):
        return (Path(head.removeprefix("gitdir: ")) / "HEAD").read_text().strip()
    return head


def _query(text: str, *, temporal_bound: float | None = None) -> RetrievalQueryV1:
    return RetrievalQueryV1(
        query_id=hashlib.sha256(text.encode()).hexdigest()[:24],
        query=text,
        temporal_bound=temporal_bound,
        max_records=10,
        timeout_ms=1000,
        correlation_id="bm0-runner",
    )


def _load_cases() -> list[dict[str, str]]:
    dataset = [
        json.loads(line)
        for line in (DATASET_DIR / "dataset.jsonl").read_text().splitlines()
        if line.strip()
    ]
    oracle = json.loads((DATASET_DIR / "expected-results.json").read_text())
    expected = oracle.get("cases")
    if not isinstance(expected, list) or oracle.get("expected_case_count") != len(
        dataset
    ):
        raise ValueError("expected-results.json has an invalid case count")
    dataset_ids = [case.get("id") for case in dataset]
    expected_ids = [case.get("id") for case in expected]
    if len(set(dataset_ids)) != len(dataset_ids) or len(set(expected_ids)) != len(
        expected_ids
    ):
        raise ValueError("dataset or oracle contains duplicate case IDs")
    if set(dataset_ids) != set(expected_ids):
        raise ValueError("dataset and oracle case IDs differ")
    expected_by_id = {case["id"]: case for case in expected}
    merged: list[dict[str, str]] = []
    for case in dataset:
        if set(case) != {"id", "scenario", "query"}:
            raise ValueError("dataset rows may contain only id, scenario, and query")
        expectation = expected_by_id[case["id"]]
        if set(expectation) != {"id", "status", "must_include", "must_exclude"}:
            raise ValueError("oracle rows are incomplete or contain unknown fields")
        if expectation["status"] != "passed":
            raise ValueError("BM-0 frozen oracle supports only expected passed cases")
        if not all(isinstance(expectation[field], str) for field in expectation):
            raise ValueError("oracle fields must be strings")
        merged.append({**case, **expectation})
    return merged


def _ingest(
    store: LocalCitedMemory, vault: Path, filename: str, **metadata: Any
) -> Any:
    return store.ingest_file(vault, Path(filename), **metadata)


def _exercise(case: dict[str, str], db_path: Path) -> dict[str, Any]:
    """Exercise one copied frozen Markdown vault without writing source fixtures."""
    scenario, query_text = case["scenario"], case["query"]
    with tempfile.TemporaryDirectory(prefix="supermem-bm0-vault-") as temp_vault:
        vault = Path(temp_vault) / "vault"
        shutil.copytree(DATASET_DIR / "sources", vault)
        with LocalCitedMemory(db_path) as store:
            if scenario in {"exact_positive", "unknown"}:
                _ingest(store, vault, "exact.md")
                _ingest(store, vault, "unrelated.md")
            elif scenario == "source_modified":
                _ingest(store, vault, "modified.md")
                shutil.copyfile(vault / "modified-v2.md", vault / "modified.md")
                _ingest(store, vault, "modified.md")
            elif scenario == "source_deleted":
                revision = _ingest(store, vault, "deleted.md")
                assert store.delete_source(revision.source_uri)
            elif scenario == "retracted":
                revision = _ingest(store, vault, "retracted.md")
                assert store.retract(f"{revision.source_id}:{revision.revision}")
            elif scenario == "expired":
                _ingest(store, vault, "expired.md", expires_at=time.time() + 0.01)
                time.sleep(0.02)
            elif scenario == "effective_interval":
                _ingest(store, vault, "valid.md", effective_from=10, effective_until=20)
                _ingest(
                    store, vault, "future.md", effective_from=21, effective_until=30
                )
            elif scenario == "nested_private":
                _ingest(store, vault, "private.md")
            elif scenario == "compression":
                _ingest(store, vault, "compression.md")
                assert store.compression_enabled is False
                store.rebuild_fts()
            elif scenario == "citation":
                _ingest(store, vault, "citation.md")
            elif scenario == "replay":
                _ingest(store, vault, "replay.md")
            elif scenario == "injection_data_only":
                _ingest(store, vault, "injection.md")
            else:
                raise ValueError(f"unknown frozen scenario: {scenario}")

            bound = 15.0 if scenario == "effective_interval" else None
            retrieved = store.retrieve(_query(query_text, temporal_bound=bound))
            return {
                "results": [
                    {
                        "memory_id": item.memory_id,
                        "memory_revision": item.memory_revision,
                        "source_uri": item.source_uri,
                        "source_revision": item.source_revision,
                        "source_span": item.source_span,
                        "source_digest": item.source_digest,
                        "retrieval_tier": item.retrieval_tier,
                        "content_digest": hashlib.sha256(
                            item.content.encode()
                        ).hexdigest(),
                    }
                    for item in retrieved
                ],
                "citations_valid": all(
                    store.verify_citation(item, temporal_bound=bound)
                    for item in retrieved
                ),
                "citation_tamper_rejected": not retrieved
                or not store.verify_citation(
                    replace(retrieved[0], source_digest="0" * 64), temporal_bound=bound
                ),
                "contains_required": not case["must_include"]
                or any(case["must_include"] in item.content for item in retrieved),
                "contains_prohibited": bool(case["must_exclude"])
                and any(case["must_exclude"] in item.content for item in retrieved),
                "vector_status": store.vector_status,
                "tier4_invoked": False,
                "compression_enabled": store.compression_enabled,
            }


def _evaluate(case: dict[str, str], receipt: dict[str, Any]) -> dict[str, Any]:
    results = receipt["results"]
    expected_empty = not case["must_include"]
    passed = (not results) if expected_empty else bool(receipt["contains_required"])
    prohibited = int(receipt["contains_prohibited"])
    passed = (
        passed
        and not prohibited
        and receipt["citations_valid"]
        and receipt["citation_tamper_rejected"]
        and not receipt["tier4_invoked"]
    )
    return {
        "id": case["id"],
        "status": "passed" if passed else "failed",
        "result_count": len(results),
        "result_ids": [result["memory_id"] for result in results],
        "citation_verification": receipt["citations_valid"],
        "citation_tamper_rejected": receipt["citation_tamper_rejected"],
        "prohibited_result_count": prohibited,
        "timeout": False,
        "unsupported": False,
        "inconclusive": False,
        "error_class": None,
        "vector_status": receipt["vector_status"],
        "tier4_invoked": receipt["tier4_invoked"],
        "result_content_digests": [result["content_digest"] for result in results],
        "expected_status": case["status"],
    }


def _exception_report(
    case: dict[str, str], status: str, error_class: str
) -> dict[str, Any]:
    return {
        "id": case["id"],
        "status": status,
        "result_count": 0,
        "result_ids": [],
        "citation_verification": False,
        "citation_tamper_rejected": False,
        "prohibited_result_count": 0,
        "timeout": status == "timeout",
        "unsupported": status == "unsupported",
        "inconclusive": status == "inconclusive",
        "error_class": error_class,
        "vector_status": "disabled: no BM-0 ingestion path",
        "tier4_invoked": False,
        "result_content_digests": [],
        "expected_status": case["status"],
    }


def run(
    output_root: Path,
    *,
    exercise: Callable[[dict[str, str], Path], dict[str, Any]] = _exercise,
) -> tuple[Path, dict[str, Any]]:
    started_at, started = datetime.now(UTC).isoformat(), time.perf_counter()
    cases = _load_cases()
    case_reports: list[dict[str, Any]] = []
    for case in cases:
        try:
            with tempfile.TemporaryDirectory(prefix="supermem-bm0-") as temp_dir:
                case_reports.append(
                    _evaluate(case, exercise(case, Path(temp_dir) / "memory.sqlite"))
                )
        except RetrievalTimeoutError:
            case_reports.append(
                _exception_report(case, "timeout", "RetrievalTimeoutError")
            )
        except NotImplementedError:
            case_reports.append(
                _exception_report(case, "unsupported", "NotImplementedError")
            )
        except RuntimeError:
            case_reports.append(_exception_report(case, "inconclusive", "RuntimeError"))
        except Exception as exc:
            case_reports.append(_exception_report(case, "failed", type(exc).__name__))
    normalized = [
        {
            key: value
            for key, value in report.items()
            if key not in {"timeout", "unsupported", "inconclusive"}
        }
        for report in case_reports
    ]
    normalized_digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    artifact_dir = (
        output_root
        / f"bm0-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{normalized_digest[:12]}"
    )
    artifact_dir.mkdir(parents=True, exist_ok=False)
    counts = {
        status: sum(report["status"] == status for report in case_reports)
        for status in ("passed", "failed", "timeout", "unsupported", "inconclusive")
    }
    identity = candidate_identity()
    metrics = {
        "contract_version": "BM0-1",
        "case_count": len(case_reports),
        **counts,
        "timeouts": counts["timeout"],
        "prohibited_result_count": sum(
            report["prohibited_result_count"] for report in case_reports
        ),
        "normalized_run_digest": normalized_digest,
        "dataset_digest": _digest_path(DATASET_DIR / "dataset.jsonl"),
        "expected_results_digest": _digest_path(DATASET_DIR / "expected-results.json"),
        "harness_digest": _digest_path(Path(__file__)),
        "candidate_identity": identity,
        "baseline_head": _git_head(),
        "dependency_lock_digest": _digest_path(REPO_ROOT / "uv.lock"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "vector_status": "disabled: no BM-0 ingestion path",
        "enabled_projections": ["canonical SQLite", "SQLite FTS5"],
        "cache_state": "fresh temp SQLite per case",
        "concurrency": 1,
        "seed": "not applicable; deterministic SQLite FTS fixture",
        "start_time": started_at,
        "end_time": datetime.now(UTC).isoformat(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    (artifact_dir / "environment.json").write_text(
        json.dumps({"python": sys.version, "platform": platform.platform()}, indent=2)
        + "\n"
    )
    (artifact_dir / "configuration.json").write_text(
        json.dumps(
            {
                "dataset": str(DATASET_DIR),
                "projections": metrics["enabled_projections"],
                "vector": metrics["vector_status"],
            },
            indent=2,
        )
        + "\n"
    )
    (artifact_dir / "cases.jsonl").write_text(
        "".join(json.dumps(report, sort_keys=True) + "\n" for report in case_reports)
    )
    (artifact_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n"
    )
    (artifact_dir / "report.md").write_text(
        "# BM-0 local receipt\n\n"
        + "\n".join(f"- {key.title()}: {value}" for key, value in counts.items())
        + f"\n- Prohibited results: {metrics['prohibited_result_count']}\n- Normalized run digest: `{normalized_digest}`\n- Candidate digest: `{identity['digest']}`\n- Vector: {metrics['vector_status']}\n"
    )
    return artifact_dir, metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    artifact_dir, metrics = run(args.output_root)
    print(
        json.dumps(
            {
                "artifact_dir": str(artifact_dir),
                "normalized_run_digest": metrics["normalized_run_digest"],
                "passed": metrics["passed"],
                "failed": metrics["failed"],
            },
            sort_keys=True,
        )
    )
    return (
        0
        if metrics["failed"]
        == metrics["timeout"]
        == metrics["unsupported"]
        == metrics["inconclusive"]
        == 0
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
