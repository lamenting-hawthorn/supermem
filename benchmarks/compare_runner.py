"""BM-0 benchmark runner.

Usage:
    uv run python -m benchmarks.compare_runner [--dataset competitive-local] \
        [--adapters no_memory raw_history supermem_fts] [--k 10] \
        [--repeats 2] [--out artifacts]

    uv run python -m benchmarks.runner compare artifacts/<old> artifacts/<new>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.adapters.no_memory import NoMemoryAdapter
from benchmarks.adapters.raw_history import RawHistoryAdapter
from benchmarks.harness_types import (
    BaseBenchmarkAdapter,
    BenchmarkCase,
    load_cases,
    load_manifest,
    load_mutations,
)
from benchmarks.oracle import CaseVerdict, judge_case
from benchmarks.scoring import (
    citation_coverage,
    citation_verification_rate,
    latency_percentiles,
    mrr,
    precision_at_k,
    prohibited_recall,
    recall_at_k,
    unknown_contamination,
    variance_rate,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from benchmarks.adapters.supermem_fts import SupermemFtsAdapter

    HAS_FTS = True
except Exception:  # pragma: no cover - defensive
    HAS_FTS = False

try:
    from benchmarks.adapters.supermem_hybrid import (
        SupermemHybridAdapter,
    )

    HAS_HYBRID = True
except Exception:  # pragma: no cover - defensive
    HAS_HYBRID = False


def build_adapter(name: str) -> BaseBenchmarkAdapter | None:
    if name == "no_memory":
        return NoMemoryAdapter()
    if name == "raw_history":
        return RawHistoryAdapter()
    if name == "supermem_fts" and HAS_FTS:
        return SupermemFtsAdapter()
    if name == "supermem_hybrid" and HAS_HYBRID:
        return SupermemHybridAdapter()
    return None


AVAILABLE_ADAPTERS = ["no_memory", "raw_history", "supermem_fts", "supermem_hybrid"]


def _git_identity() -> dict:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = (
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            != ""
        )
    except Exception:
        sha, dirty = "unknown", True
    return {"commit": sha, "dirty": dirty}


def _dataset_digests(dataset_dir: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(dataset_dir.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(dataset_dir))
            digests[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


async def _retrieve_for_case(
    adapter: BaseBenchmarkAdapter, case: BenchmarkCase, k: int
) -> list:
    if case.temporal_bound and isinstance(case.temporal_bound, dict):
        as_of = case.temporal_bound.get("as_of")
        retrieve_with_bound = getattr(adapter, "retrieve_with_bound", None)
        if as_of is not None and callable(retrieve_with_bound):
            return await retrieve_with_bound(case.query, k=k, as_of=float(as_of))
    return await adapter.retrieve(case.query, k=k)


async def run_adapter_suite(
    adapter_name: str,
    dataset_dir: Path,
    k: int,
    repeats: int,
) -> dict:
    adapter = build_adapter(adapter_name)
    if adapter is None:
        return {"adapter": adapter_name, "status": "unavailable"}

    cases = load_cases(dataset_dir)
    manifest = load_manifest(dataset_dir)
    mutations = load_mutations(manifest)

    started_wall = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()

    all_verdicts: list[dict] = []
    verdict_objects: list[CaseVerdict] = []
    repeat_outcomes: list[list[bool]] = []
    latencies: list[float] = []
    results_by_query_id: dict[str, list] = {}

    phase1 = [c for c in cases if c.expected.phase == 1]
    phase2 = [c for c in cases if c.expected.phase == 2]

    for rep in range(repeats):
        # Each repeat gets a fresh workspace + store so replay is like-for-like
        # (re-indexing into the same store would mint new observation ids and
        # shift rankings for reasons unrelated to retrieval quality).
        with tempfile.TemporaryDirectory(prefix=f"bm0-{adapter_name}-r{rep}-") as tmp:
            rep_workspace = Path(tmp)
            await adapter.setup(rep_workspace, dataset_dir)
            rep_outcomes: list[bool] = []
            results_by_query_id.clear()
            for case in phase1:
                t = time.perf_counter()
                results = await _retrieve_for_case(adapter, case, k)
                dt_ms = (time.perf_counter() - t) * 1000.0
                results_by_query_id[case.query_id] = results
                verdict = judge_case(case, results, workspace=rep_workspace)
                verdict_objects.append(verdict)
                passed = verdict.passed
                rep_outcomes.append(passed)
                latencies.append(dt_ms)
                all_verdicts.append(
                    {
                        "repeat": rep,
                        "query_id": case.query_id,
                        "case_type": case.expected.case_type,
                        "phase": 1,
                        "passed": passed,
                        "reasons": verdict.reasons,
                        "n_results": len(results),
                        "latency_ms": round(dt_ms, 3),
                    }
                )

            for mutation in mutations:
                await adapter.mutate(mutation)

            for case in phase2:
                t = time.perf_counter()
                results = await _retrieve_for_case(adapter, case, k)
                dt_ms = (time.perf_counter() - t) * 1000.0
                results_by_query_id[case.query_id] = results
                verdict = judge_case(case, results, workspace=rep_workspace)
                verdict_objects.append(verdict)
                passed = verdict.passed
                rep_outcomes.append(passed)
            latencies.append(dt_ms)
            all_verdicts.append(
                {
                    "repeat": rep,
                    "query_id": case.query_id,
                    "case_type": case.expected.case_type,
                    "phase": 2,
                    "passed": passed,
                    "reasons": verdict.reasons,
                    "n_results": len(results),
                    "latency_ms": round(dt_ms, 3),
                }
            )
        repeat_outcomes.append(rep_outcomes)
        await adapter.teardown()

    wall_seconds = time.perf_counter() - t0

    # Final-repeat results feed coverage metrics.
    last_rep = max(v["repeat"] for v in all_verdicts)
    last_ids = {v["query_id"] for v in all_verdicts if v["repeat"] == last_rep}
    last_results_flat = [
        r for qid in last_ids for r in results_by_query_id.get(qid, [])
    ]

    metrics = {
        "recall_at_k": recall_at_k(cases, results_by_query_id, k),
        "precision_at_k": precision_at_k(cases, results_by_query_id, k),
        "mrr": mrr(cases, results_by_query_id),
        "prohibited_recall": prohibited_recall(cases, results_by_query_id),
        "unknown_contamination": unknown_contamination(cases, results_by_query_id),
        "citation_coverage": citation_coverage(last_results_flat),
        "citation_verification_rate": citation_verification_rate(verdict_objects),
        "latency": latency_percentiles(latencies),
        "variance_rate": variance_rate(repeat_outcomes),
        "wall_seconds": round(wall_seconds, 3),
        "n_cases": len(cases),
        "n_repeats": repeats,
    }

    # Citation gates are vacuous when an adapter returns no rows at all
    # (e.g. the no_memory baseline) — there is nothing to cite.
    returned_anything = len(last_results_flat) > 0
    gate_checks = {
        "zero_stale_recall": metrics["prohibited_recall"].get("stale", 1.0) == 0.0,
        "zero_expired_recall": metrics["prohibited_recall"].get("expired", 1.0) == 0.0,
        "zero_retracted_recall": metrics["prohibited_recall"].get("retracted", 1.0)
        == 0.0,
        "zero_deleted_recall": metrics["prohibited_recall"].get("deleted", 1.0) == 0.0,
        "zero_private_recall": metrics["prohibited_recall"].get("private", 1.0) == 0.0,
        "zero_unknown_contamination": metrics["unknown_contamination"] == 0.0,
        "full_citation_coverage": (not returned_anything)
        or metrics["citation_coverage"] == 1.0,
        "citations_verify": (not returned_anything)
        or metrics["citation_verification_rate"] == 1.0,
        "deterministic_replay": metrics["variance_rate"] == 0.0,
    }

    ended_wall = datetime.now(timezone.utc).isoformat()

    failures = [v for v in all_verdicts if not v["passed"]]
    return {
        "adapter": adapter_name,
        "status": "ok",
        "metrics": metrics,
        "gates": gate_checks,
        "failures": failures[:50],
        "cases_jsonl": all_verdicts,
        "started": started_wall,
        "ended": ended_wall,
    }


def _write_report(path: Path, suite: dict) -> None:
    lines = [f"# BM-0 report — {suite['adapter']}", ""]
    if suite.get("status") != "ok":
        lines.append(f"Status: **{suite['status']}**")
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    m = suite["metrics"]
    lines += [
        f"Run window: {suite['started']} → {suite['ended']}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| recall@k | {m['recall_at_k']} |",
        f"| precision@k | {m['precision_at_k']} |",
        f"| MRR | {m['mrr']} |",
        f"| stale recall | {m['prohibited_recall'].get('stale')} |",
        f"| expired recall | {m['prohibited_recall'].get('expired')} |",
        f"| retracted recall | {m['prohibited_recall'].get('retracted')} |",
        f"| deleted-source recall | {m['prohibited_recall'].get('deleted')} |",
        f"| private recall | {m['prohibited_recall'].get('private')} |",
        f"| unknown contamination | {m['unknown_contamination']} |",
        f"| citation coverage | {m['citation_coverage']} |",
        f"| citation verification | {m['citation_verification_rate']} |",
        f"| latency p50/p95/p99 ms | {m['latency']['p50']:.2f} / {m['latency']['p95']:.2f} / {m['latency']['p99']:.2f} |",
        f"| replay variance rate | {m['variance_rate']} |",
        "",
        "## Gates",
        "",
    ]
    for gate, ok in suite["gates"].items():
        lines.append(f"- [{'x' if ok else ' '}] {gate}")
    failures = suite.get("failures", [])
    if failures:
        lines += ["", "## Failures", ""]
        for f in failures:
            lines.append(
                f"- {f['query_id']} ({f['case_type']}, phase {f['phase']}): {'; '.join(f['reasons']) or 'no results'}"
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def cmd_run(args: argparse.Namespace) -> int:
    identity = _git_identity()
    repo_sha, dirty = identity["commit"], identity["dirty"]
    dataset_dir = REPO_ROOT / "benchmarks" / "datasets" / args.dataset
    if not dataset_dir.exists():
        print(f"Dataset not found: {dataset_dir}", file=sys.stderr)
        return 2
    out_root = REPO_ROOT / args.out
    out_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + repo_sha[:8]
    run_dir = out_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    environment = {
        "run_id": run_id,
        "git_commit": repo_sha,
        "git_dirty": dirty,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "dataset": args.dataset,
        "dataset_digests": _dataset_digests(dataset_dir),
        "k": args.k,
        "repeats": args.repeats,
    }
    (run_dir / "environment.json").write_text(
        json.dumps(environment, indent=2), encoding="utf-8"
    )

    overall_exit = 0
    summary_lines = []
    for adapter_name in args.adapters:
        suite = asyncio.run(
            run_adapter_suite(adapter_name, dataset_dir, args.k, args.repeats)
        )
        adapter_dir = run_dir / adapter_name
        adapter_dir.mkdir(exist_ok=True)
        (adapter_dir / "configuration.json").write_text(
            json.dumps(
                {"adapter": adapter_name, "dataset": args.dataset, "k": args.k},
                indent=2,
            ),
            encoding="utf-8",
        )
        (adapter_dir / "cases.jsonl").write_text(
            "\n".join(json.dumps(v) for v in suite.get("cases_jsonl", [])),
            encoding="utf-8",
        )
        (adapter_dir / "metrics.json").write_text(
            json.dumps(suite.get("metrics", {"status": suite.get("status")}), indent=2),
            encoding="utf-8",
        )
        _write_report(adapter_dir / "report.md", suite)

        if suite.get("status") != "ok":
            summary_lines.append(f"{adapter_name:16s}  SKIPPED/UNAVAILABLE")
            continue
        m = suite["metrics"]
        gates_failed = [g for g, ok in suite["gates"].items() if not ok]
        summary_lines.append(
            f"{adapter_name:16s}  recall@{args.k}={m['recall_at_k']:.3f}  "
            f"MRR={m['mrr']:.3f}  citVerif={m['citation_verification_rate']:.3f}  "
            f"var={m['variance_rate']:.3f}  p50={m['latency']['p50']:.1f}ms  "
            f"gates_failed={len(gates_failed)}{' (' + ', '.join(gates_failed) + ')' if gates_failed else ''}"
        )
        # Baseline adapters (no lifecycle awareness by design) are informational;
        # gate failures only fail the run for product adapters.
        if gates_failed and adapter_name.startswith("supermem_"):
            overall_exit = 1

    print(f"\nRun id: {run_id}")
    print("\n".join(summary_lines))
    print(f"\nArtifacts: {run_dir.relative_to(REPO_ROOT)}")
    return overall_exit


def cmd_compare(old: str, new: str) -> int:
    from benchmarks.reporting import compare

    return compare(old, new)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.runner")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="run the benchmark suite")
    run_p.add_argument("--dataset", default="competitive-local")
    run_p.add_argument(
        "--adapters", nargs="+", default=["no_memory", "raw_history", "supermem_fts"]
    )
    run_p.add_argument("--k", type=int, default=10)
    run_p.add_argument("--repeats", type=int, default=2)
    run_p.add_argument("--out", default="artifacts")
    run_p.set_defaults(func=cmd_run)

    cmp_p = sub.add_parser("compare", help="compare two artifact runs")
    cmp_p.add_argument("old")
    cmp_p.add_argument("new")
    cmp_p.set_defaults(func=lambda a: cmd_compare(a.old, a.new))

    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] not in {"run", "compare"}:
        argv = ["run", *argv]
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
