"""BM-0 benchmark runner.

Usage:
    uv run python -m benchmarks.compare_runner run [--dataset competitive-local] \
        [--adapters no_memory raw_history supermem_fts] [--k 10] \
        [--repeats 2] [--out artifacts] [--run-id RUN_ID] [--resume] \
        [--predict-only]

    uv run python -m benchmarks.compare_runner run \
        --evaluate-only artifacts/<run_id>   # re-score, no retrieval
    uv run python -m benchmarks.compare_runner failures artifacts/<run_id> \
        [--adapter NAME]
    uv run python -m benchmarks.compare_runner compare artifacts/<old> artifacts/<new>

Every retrieved case is appended to
``artifacts/<run_id>/<adapter>/predictions.jsonl`` as it completes — one
``{"case_id", "repeat", "latency_ms", "results"}`` object per line — so a
crash mid-run loses only the in-flight case:

- ``--predict-only`` stops after streaming predictions (plus
  configuration/environment manifests) — no scoring, metrics, or report.
- ``--evaluate-only <run_dir>`` re-scores an existing predictions directory
  without touching adapters, so scoring changes re-run without re-hitting
  retrieval APIs.
- ``--run-id <id> --resume`` continues a run in place: (case_id, repeat)
  pairs already present in predictions.jsonl are loaded, not re-retrieved.
  A partially-recorded repeat still re-runs adapter setup + mutations so
  the live workspace can verify citation digests; a fully-recorded repeat
  is skipped entirely.

Offline scoring (``--evaluate-only``, and fully-cached repeats under
``--resume``) has no live workspace, so byte-level source-digest
verification is skipped — citation field-presence checks still apply.
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
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from benchmarks.adapters.no_memory import NoMemoryAdapter
from benchmarks.adapters.raw_history import RawHistoryAdapter
from benchmarks.harness_types import (
    BaseBenchmarkAdapter,
    BenchmarkCase,
    CitedResult,
    Mutation,
    load_cases,
    load_manifest,
    load_mutations,
)
from benchmarks.oracle import CaseVerdict, judge_case
from benchmarks.scoring import (
    citation_coverage,
    citation_verification_rate,
    context_rot,
    context_stats,
    estimate_tokens,
    latency_percentiles,
    mrr,
    ndcg_at_k,
    precision_at_k,
    prohibited_recall,
    recall_at_k,
    recall_at_k_multi,
    recall_by_question_type,
    receipt_header,
    unknown_contamination,
    variance_rate,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from supermem.logging import get_logger

    log = get_logger(__name__)
except Exception:  # pragma: no cover - supermem deps unavailable
    import logging as _logging

    class _KwargLogger:
        """Stdlib stand-in accepting structlog-style kwargs."""

        def __init__(self, name: str):
            self._log = _logging.getLogger(name)

        def info(self, event: str, **kw: Any) -> None:
            self._log.info("%s %s", event, kw if kw else "")

        def warning(self, event: str, **kw: Any) -> None:
            self._log.warning("%s %s", event, kw if kw else "")

    _logging.basicConfig(level=_logging.INFO)
    log = _KwargLogger(__name__)

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

PREDICTIONS_FILENAME = "predictions.jsonl"

DEFAULT_ADAPTERS = ["no_memory", "raw_history", "supermem_fts"]
DEFAULT_DATASET = "competitive-local"


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


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _resolve_dir(raw: str) -> Path | None:
    """Resolve an artifact/run dir given as cwd-relative, absolute, or
    repo-root-relative."""
    path = Path(raw)
    if path.is_dir():
        return path
    alt = REPO_ROOT / raw
    if alt.is_dir():
        return alt
    return None


def _dataset_dir(dataset_name: str) -> Path:
    return REPO_ROOT / "benchmarks" / "datasets" / dataset_name


async def _retrieve_for_case(
    adapter: BaseBenchmarkAdapter, case: BenchmarkCase, k: int
) -> list:
    if case.temporal_bound and isinstance(case.temporal_bound, dict):
        as_of = case.temporal_bound.get("as_of")
        retrieve_with_bound = getattr(adapter, "retrieve_with_bound", None)
        if as_of is not None and callable(retrieve_with_bound):
            return await retrieve_with_bound(case.query, k=k, as_of=float(as_of))
    return await adapter.retrieve(case.query, k=k)


# ---------------------------------------------------------------------------
# predictions.jsonl — streaming per-case results
# ---------------------------------------------------------------------------


def _cited_result_from_dict(raw: dict) -> CitedResult:
    """Rebuild a CitedResult from its ``dataclasses.asdict()`` form,
    tolerating missing/extra keys."""
    return CitedResult(
        memory_id=str(raw.get("memory_id", "")),
        memory_revision=int(raw.get("memory_revision", 0)),
        content=str(raw.get("content", "")),
        source_uri=str(raw.get("source_uri", "")),
        source_revision=int(raw.get("source_revision", 0)),
        source_span=str(raw.get("source_span", "")),
        source_digest=str(raw.get("source_digest", "")),
        retrieval_tier=str(raw.get("retrieval_tier", "")),
        retrieval_score=float(raw.get("retrieval_score", 0.0)),
        latency_ms=float(raw.get("latency_ms", 0.0)),
    )


def _load_prediction_records(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """Load predictions.jsonl into ``{(case_id, repeat): entry}``.

    Malformed lines (e.g. a truncated tail left by a crash) are skipped with
    a warning; when a (case_id, repeat) pair appears twice the later line
    wins.
    """
    records: dict[tuple[str, int], dict[str, Any]] = {}
    if not path.is_file():
        return records
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.warning("predictions_line_skipped", path=str(path), line=lineno)
            continue
        case_id = obj.get("case_id") or obj.get("query_id")
        if case_id is None:
            log.warning("predictions_line_no_case_id", path=str(path), line=lineno)
            continue
        try:
            latency = obj.get("latency_ms")
            records[(str(case_id), int(obj.get("repeat", 0)))] = {
                "results": [_cited_result_from_dict(r) for r in obj.get("results", [])],
                "latency_ms": float(latency) if latency is not None else None,
            }
        except (TypeError, ValueError, AttributeError):
            log.warning("predictions_line_skipped", path=str(path), line=lineno)
            continue
    return records


def _entry_latency_ms(entry: dict[str, Any]) -> float:
    """Best latency for a loaded prediction: the runner-measured value when
    the line carried one, else the summed per-result adapter latency."""
    if entry.get("latency_ms") is not None:
        return float(entry["latency_ms"])
    return sum(r.latency_ms for r in entry["results"])


class _PredictionWriter:
    """Append-only JSONL sink for per-case predictions.

    Each line is flushed as it is written, so a crash loses at most the
    in-flight case.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fh: TextIO | None = None

    def open(self, append: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a" if append else "w", encoding="utf-8")
        if append:
            self._heal_truncated_tail()

    def _heal_truncated_tail(self) -> None:
        """A crash can leave a partial final line with no newline; appending
        directly would fuse the next record onto it. Terminate the partial
        line so it is skipped cleanly on the next load."""
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, 2)
                if fh.tell() == 0:
                    return
                fh.seek(-1, 2)
                tail = fh.read(1)
        except OSError:
            return
        if tail != b"\n":
            assert self._fh is not None
            self._fh.write("\n")
            self._fh.flush()

    def write(
        self, case_id: str, repeat: int, results: list, latency_ms: float
    ) -> None:
        assert self._fh is not None, "writer not opened"
        self._fh.write(
            json.dumps(
                {
                    "case_id": case_id,
                    "repeat": repeat,
                    "latency_ms": round(latency_ms, 3),
                    "results": [
                        asdict(r) if isinstance(r, CitedResult) else r for r in results
                    ],
                }
            )
            + "\n"
        )
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# Suite collection + scoring
# ---------------------------------------------------------------------------


async def _collect_predictions(
    adapter: BaseBenchmarkAdapter,
    adapter_name: str,
    dataset_dir: Path,
    cases: list[BenchmarkCase],
    mutations: list[Mutation],
    k: int,
    repeats: int,
    cached: dict[tuple[str, int], dict[str, Any]],
    writer: _PredictionWriter | None,
) -> list[dict[str, Any]]:
    """Retrieve (or reuse cached) results for every (case, repeat) pair.

    Returns judged records — ``{"case", "repeat", "results", "verdict",
    "latency_ms"}`` — in run order. Judging happens inside the loop while a
    repeat's live workspace is still mounted, so citation digests verify
    against the same source bytes the adapter saw. Fully-cached repeats skip
    adapter setup entirely and are judged workspace-free (byte-level digest
    re-verification needs a live workspace).
    """
    phase1 = [c for c in cases if c.expected.phase == 1]
    phase2 = [c for c in cases if c.expected.phase == 2]
    records: list[dict[str, Any]] = []

    async def _get_results(case: BenchmarkCase, rep: int) -> tuple[list, float]:
        entry = cached.get((case.query_id, rep))
        if entry is not None:
            return entry["results"], _entry_latency_ms(entry)
        t = time.perf_counter()
        results = await _retrieve_for_case(adapter, case, k)
        dt_ms = (time.perf_counter() - t) * 1000.0
        if writer is not None:
            writer.write(case.query_id, rep, results, dt_ms)
        return results, dt_ms

    def _record(
        case: BenchmarkCase,
        rep: int,
        results: list,
        latency_ms: float,
        workspace: Path | None,
    ) -> None:
        records.append(
            {
                "case": case,
                "repeat": rep,
                "results": results,
                "verdict": judge_case(case, results, workspace=workspace),
                "latency_ms": latency_ms,
            }
        )

    for rep in range(repeats):
        if all((c.query_id, rep) in cached for c in cases):
            # Fully cached repeat — no setup, no mutations, no retrieval.
            log.info("repeat_fully_cached", adapter=adapter_name, repeat=rep)
            for case in (*phase1, *phase2):
                entry = cached[(case.query_id, rep)]
                _record(case, rep, entry["results"], _entry_latency_ms(entry), None)
            continue
        # Each repeat gets a fresh workspace + store so replay is
        # like-for-like (re-indexing into the same store would mint new
        # observation ids and shift rankings for reasons unrelated to
        # retrieval quality).
        with tempfile.TemporaryDirectory(prefix=f"bm0-{adapter_name}-r{rep}-") as tmp:
            rep_workspace = Path(tmp)
            await adapter.setup(rep_workspace, dataset_dir)
            try:
                for case in phase1:
                    results, dt_ms = await _get_results(case, rep)
                    _record(case, rep, results, dt_ms, rep_workspace)

                for mutation in mutations:
                    await adapter.mutate(mutation)

                for case in phase2:
                    results, dt_ms = await _get_results(case, rep)
                    _record(case, rep, results, dt_ms, rep_workspace)
            finally:
                await adapter.teardown()
    return records


def _assemble_suite(
    adapter_name: str,
    cases: list[BenchmarkCase],
    records: list[dict[str, Any]],
    n_repeats: int,
    k: int,
    started_wall: str,
    wall_seconds: float,
) -> dict:
    """Fold judged per-case records into the suite result dict — metrics,
    gates, and the cases.jsonl payload.

    Retrieval metrics (recall@k, MRR, context stats) use the highest-numbered
    repeat present, matching the original last-repeat semantics.
    """
    phase1 = [c for c in cases if c.expected.phase == 1]
    phase2 = [c for c in cases if c.expected.phase == 2]
    ordered_cases = (*phase1, *phase2)

    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for r in records:
        by_key[(r["repeat"], r["case"].query_id)] = r  # later records win

    all_verdicts: list[dict] = []
    verdict_objects: list[CaseVerdict] = []
    repeat_outcomes: list[list[bool]] = []
    latencies: list[float] = []
    results_by_rep: dict[int, dict[str, list]] = {}

    for rep in sorted({rep for rep, _ in by_key}):
        rep_map = results_by_rep.setdefault(rep, {})
        outcomes: list[bool] = []
        for case in ordered_cases:
            r = by_key.get((rep, case.query_id))
            if r is None:
                continue
            results = r["results"]
            verdict = r["verdict"]
            rep_map[case.query_id] = results
            verdict_objects.append(verdict)
            outcomes.append(verdict.passed)
            latencies.append(r["latency_ms"])
            all_verdicts.append(
                {
                    "repeat": rep,
                    "query_id": case.query_id,
                    "case_type": case.expected.case_type,
                    "phase": case.expected.phase,
                    "passed": verdict.passed,
                    "reasons": verdict.reasons,
                    "n_results": len(results),
                    "context_chars": sum(len(x.content) for x in results),
                    "context_tokens_est": estimate_tokens(
                        sum(len(x.content) for x in results)
                    ),
                    "latency_ms": round(r["latency_ms"], 3),
                }
            )
        repeat_outcomes.append(outcomes)

    ended_wall = datetime.now(timezone.utc).isoformat()

    # Final-repeat results feed coverage metrics.
    last_rep = max(results_by_rep) if results_by_rep else -1
    last_results = results_by_rep.get(last_rep, {})
    last_results_flat = [res for res_list in last_results.values() for res in res_list]

    multi = recall_at_k_multi(cases, last_results)
    metrics = {
        "recall_at_k": multi.get("10", recall_at_k(cases, last_results, k)),
        "recall_at_k_multi": multi,
        "ndcg_at_k": ndcg_at_k(cases, last_results, 10),
        "by_question_type": recall_by_question_type(cases, last_results, 10),
        "precision_at_k": precision_at_k(cases, last_results, k),
        "mrr": mrr(cases, last_results),
        "prohibited_recall": prohibited_recall(cases, last_results),
        "unknown_contamination": unknown_contamination(cases, last_results),
        "citation_coverage": citation_coverage(last_results_flat),
        "citation_verification_rate": citation_verification_rate(verdict_objects),
        "context": context_stats(cases, last_results, k),
        "context_rot": context_rot(cases, last_results, k),
        "latency": latency_percentiles(latencies),
        "variance_rate": variance_rate(repeat_outcomes),
        "wall_seconds": round(wall_seconds, 3),
        "n_cases": len(cases),
        "n_repeats": n_repeats,
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


def _records_from_predictions(
    predictions_path: Path, cases: list[BenchmarkCase]
) -> list[dict[str, Any]]:
    """Rebuild judged records from a predictions.jsonl — no live workspace,
    so citation digests are not re-verified against source bytes."""
    by_id = {c.query_id: c for c in cases}
    records: list[dict[str, Any]] = []
    for (case_id, rep), entry in sorted(
        _load_prediction_records(predictions_path).items(),
        key=lambda kv: (kv[0][1], kv[0][0]),
    ):
        case = by_id.get(case_id)
        if case is None:
            log.warning("predictions_case_unknown", case_id=case_id, repeat=rep)
            continue
        records.append(
            {
                "case": case,
                "repeat": rep,
                "results": entry["results"],
                "verdict": judge_case(case, entry["results"], workspace=None),
                "latency_ms": _entry_latency_ms(entry),
            }
        )
    return records


async def run_adapter_suite(
    adapter_name: str,
    dataset_dir: Path,
    k: int,
    repeats: int,
    adapter_dir: Path | None = None,
    resume: bool = False,
) -> dict:
    """Run one adapter over the dataset.

    When ``adapter_dir`` is given, every case's results are appended to
    ``<adapter_dir>/predictions.jsonl`` as they are produced. With
    ``resume=True``, (case_id, repeat) pairs already present in that file
    are loaded instead of re-retrieved.
    """
    adapter = build_adapter(adapter_name)
    if adapter is None:
        return {"adapter": adapter_name, "status": "unavailable"}

    cases = load_cases(dataset_dir)
    manifest = load_manifest(dataset_dir)
    mutations = load_mutations(manifest)

    started_wall = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()

    cached: dict[tuple[str, int], dict[str, Any]] = {}
    writer: _PredictionWriter | None = None
    if adapter_dir is not None:
        predictions_path = adapter_dir / PREDICTIONS_FILENAME
        if resume and predictions_path.is_file():
            cached = _load_prediction_records(predictions_path)
            log.info(
                "predictions_resume",
                adapter=adapter_name,
                cached_cases=len(cached),
            )
        elif predictions_path.is_file() and predictions_path.stat().st_size:
            log.warning(
                "predictions_overwrite",
                adapter=adapter_name,
                path=str(predictions_path),
            )
        writer = _PredictionWriter(predictions_path)
        writer.open(append=bool(cached))

    try:
        records = await _collect_predictions(
            adapter,
            adapter_name,
            dataset_dir,
            cases,
            mutations,
            k,
            repeats,
            cached,
            writer,
        )
    finally:
        if writer is not None:
            writer.close()

    return _assemble_suite(
        adapter_name,
        cases,
        records,
        n_repeats=repeats,
        k=k,
        started_wall=started_wall,
        wall_seconds=time.perf_counter() - t0,
    )


def _write_report(path: Path, suite: dict) -> None:
    lines = [f"# BM-0 report — {suite['adapter']}", ""]
    if suite.get("status") != "ok":
        lines.append(f"Status: **{suite['status']}**")
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    m = suite["metrics"]
    lines += [
        f"**{receipt_header(m)}**",
        "",
        f"Run window: {suite['started']} → {suite['ended']}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| recall@k | {m['recall_at_k']} |",
        f"| recall@1/3/5/10/30/50 | {m.get('recall_at_k_multi', {})} |",
        f"| NDCG@10 | {m.get('ndcg_at_k')} |",
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
        f"| context avg chars / tokens(est) | {m['context']['avg_context_chars']} / {m['context']['avg_context_tokens_est']} |",
        f"| context tokens(est) per recall | {m['context']['avg_context_tokens_per_recall']} |",
        f"| context rot (small→large Δrecall, detected) | {m['context_rot']['recall_delta']} / {m['context_rot']['detected']} |",
        f"| latency p50/p95/p99 ms | {m['latency']['p50']:.2f} / {m['latency']['p95']:.2f} / {m['latency']['p99']:.2f} |",
        f"| replay variance rate | {m['variance_rate']} |",
        "",
        "## Gates",
        "",
    ]
    for gate, ok in suite["gates"].items():
        lines.append(f"- [{'x' if ok else ' '}] {gate}")
    by_qt = m.get("by_question_type") or {}
    if by_qt:
        lines += [
            "",
            "## Recall by question type",
            "",
            "| question_type | recall@k | n |",
            "|---|---|---|",
        ]
        for qt, r in sorted(by_qt.items()):
            lines.append(f"| {qt} | {r['recall_at_k']:.4f} | {r['n']} |")
    failures = suite.get("failures", [])
    if failures:
        lines += ["", "## Failures", ""]
        for f in failures:
            lines.append(
                f"- {f['query_id']} ({f['case_type']}, phase {f['phase']}): {'; '.join(f['reasons']) or 'no results'}"
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_adapter_configuration(
    adapter_dir: Path, adapter_name: str, dataset_name: str, k: int
) -> None:
    (adapter_dir / "configuration.json").write_text(
        json.dumps(
            {"adapter": adapter_name, "dataset": dataset_name, "k": k},
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_suite_artifacts(adapter_dir: Path, suite: dict) -> None:
    """Write cases.jsonl + metrics.json + report.md for a finished suite."""
    (adapter_dir / "cases.jsonl").write_text(
        "\n".join(json.dumps(v) for v in suite.get("cases_jsonl", [])),
        encoding="utf-8",
    )
    (adapter_dir / "metrics.json").write_text(
        json.dumps(suite.get("metrics", {"status": suite.get("status")}), indent=2),
        encoding="utf-8",
    )
    _write_report(adapter_dir / "report.md", suite)


def _suite_summary_line(
    adapter_name: str, suite: dict, k: int
) -> tuple[str, list[str]]:
    """One-line stdout summary plus the list of failed gates."""
    if suite.get("status") != "ok":
        return f"{adapter_name:16s}  SKIPPED/UNAVAILABLE", []
    m = suite["metrics"]
    gates_failed = [g for g, ok in suite["gates"].items() if not ok]
    line = (
        f"{adapter_name:16s}  recall@{k}={m['recall_at_k']:.3f}  "
        f"MRR={m['mrr']:.3f}  citVerif={m['citation_verification_rate']:.3f}  "
        f"var={m['variance_rate']:.3f}  p50={m['latency']['p50']:.1f}ms  "
        f"gates_failed={len(gates_failed)}{' (' + ', '.join(gates_failed) + ')' if gates_failed else ''}"
    )
    return line, gates_failed


def cmd_run(args: argparse.Namespace) -> int:
    if args.evaluate_only:
        return cmd_evaluate(args)

    if args.resume and not args.run_id:
        log.warning(
            "resume_without_run_id",
            hint="--resume only takes effect with --run-id on an existing dir",
        )

    identity = _git_identity()
    repo_sha, dirty = identity["commit"], identity["dirty"]
    dataset_name = args.dataset or DEFAULT_DATASET
    dataset_dir = _dataset_dir(dataset_name)
    if not dataset_dir.exists():
        print(f"Dataset not found: {dataset_dir}", file=sys.stderr)
        return 2
    out_root = REPO_ROOT / args.out
    out_root.mkdir(parents=True, exist_ok=True)
    if args.run_id:
        # Fixed run dir — reuse an existing one (e.g. with --resume) or
        # create it fresh.
        run_id = args.run_id
        run_dir = out_root / run_id
        if run_dir.exists() and not args.resume:
            log.warning("run_dir_reused", run_id=run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + repo_sha[:8]
        )
        run_dir = out_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)

    # Keep an existing environment.json on resume — it records the identity
    # and dataset digests of the run that produced the predictions.
    env_path = run_dir / "environment.json"
    if not env_path.exists():
        environment = {
            "run_id": run_id,
            "git_commit": repo_sha,
            "git_dirty": dirty,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "dataset": dataset_name,
            "dataset_digests": _dataset_digests(dataset_dir),
            "k": args.k,
            "repeats": args.repeats,
        }
        env_path.write_text(json.dumps(environment, indent=2), encoding="utf-8")

    overall_exit = 0
    summary_lines = []
    for adapter_name in args.adapters or DEFAULT_ADAPTERS:
        adapter_dir = run_dir / adapter_name
        adapter_dir.mkdir(exist_ok=True)
        _write_adapter_configuration(adapter_dir, adapter_name, dataset_name, args.k)
        suite = asyncio.run(
            run_adapter_suite(
                adapter_name,
                dataset_dir,
                args.k,
                args.repeats,
                adapter_dir=adapter_dir,
                resume=args.resume,
            )
        )
        if args.predict_only:
            n = len(suite.get("cases_jsonl", []))
            summary_lines.append(
                f"{adapter_name:16s}  predictions={n} (scoring skipped)"
                if suite.get("status") == "ok"
                else f"{adapter_name:16s}  SKIPPED/UNAVAILABLE"
            )
            continue
        _write_suite_artifacts(adapter_dir, suite)

        line, gates_failed = _suite_summary_line(adapter_name, suite, args.k)
        summary_lines.append(line)
        # Baseline adapters (no lifecycle awareness by design) are informational;
        # gate failures only fail the run for product adapters.
        if gates_failed and adapter_name.startswith("supermem_"):
            overall_exit = 1

    print(f"\nRun id: {run_id}")
    print("\n".join(summary_lines))
    print(f"\nArtifacts: {run_dir.relative_to(REPO_ROOT)}")
    if args.predict_only:
        print(
            "Predictions only — score later with "
            f"`--evaluate-only {run_dir.relative_to(REPO_ROOT)}`"
        )
    return overall_exit


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Score existing predictions — no adapter setup or retrieval."""
    run_dir = _resolve_dir(args.evaluate_only)
    if run_dir is None:
        print(f"Run dir not found: {args.evaluate_only}", file=sys.stderr)
        return 2
    env = _read_json(run_dir / "environment.json") or {}

    wanted = set(args.adapters) if args.adapters else None
    adapter_dirs = [
        d
        for d in sorted(run_dir.iterdir())
        if d.is_dir()
        and (d / PREDICTIONS_FILENAME).is_file()
        and (wanted is None or d.name in wanted)
    ]
    if not adapter_dirs:
        print(f"No {PREDICTIONS_FILENAME} files under {run_dir}", file=sys.stderr)
        return 2

    overall_exit = 0
    summary_lines = []
    for adapter_dir in adapter_dirs:
        adapter_name = adapter_dir.name
        conf = _read_json(adapter_dir / "configuration.json") or {}
        dataset_name = args.dataset or conf.get("dataset") or env.get("dataset")
        if not dataset_name:
            print(
                f"{adapter_name}: dataset unknown — pass --dataset",
                file=sys.stderr,
            )
            continue
        dataset_dir = _dataset_dir(dataset_name)
        if not dataset_dir.exists():
            print(
                f"{adapter_name}: dataset not found: {dataset_dir}",
                file=sys.stderr,
            )
            continue
        k = int(conf.get("k") or env.get("k") or args.k)
        cases = load_cases(dataset_dir)

        t0 = time.perf_counter()
        records = _records_from_predictions(adapter_dir / PREDICTIONS_FILENAME, cases)
        n_repeats = len({r["repeat"] for r in records})
        suite = _assemble_suite(
            adapter_name,
            cases,
            records,
            n_repeats=n_repeats,
            k=k,
            started_wall=datetime.now(timezone.utc).isoformat(),
            wall_seconds=time.perf_counter() - t0,
        )
        _write_adapter_configuration(adapter_dir, adapter_name, dataset_name, k)
        _write_suite_artifacts(adapter_dir, suite)

        line, gates_failed = _suite_summary_line(adapter_name, suite, k)
        summary_lines.append(line)
        if gates_failed and adapter_name.startswith("supermem_"):
            overall_exit = 1

    print(f"\nEvaluated: {run_dir}")
    print("\n".join(summary_lines))
    return overall_exit


def _expected_source_uris(case: BenchmarkCase) -> list[str]:
    uris = list(case.expected.source_uris)
    if case.expected.source_uri and case.expected.source_uri not in uris:
        uris.append(case.expected.source_uri)
    return uris


def _iter_misses(
    adapter_dir: Path, cases: list[BenchmarkCase]
) -> list[tuple[str, int, list[str]]]:
    """(case_id, repeat, reasons) for every missed case — from cases.jsonl
    when present, else by judging predictions.jsonl directly."""
    cases_path = adapter_dir / "cases.jsonl"
    if cases_path.is_file():
        misses: list[tuple[str, int, list[str]]] = []
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not v.get("passed", True):
                misses.append(
                    (
                        str(v.get("query_id") or v.get("case_id")),
                        int(v.get("repeat", 0)),
                        list(v.get("reasons", [])),
                    )
                )
        return misses
    misses = []
    for rec in _records_from_predictions(adapter_dir / PREDICTIONS_FILENAME, cases):
        if not rec["verdict"].passed:
            misses.append((rec["case"].query_id, rec["repeat"], rec["verdict"].reasons))
    return misses


def cmd_failures(args: argparse.Namespace) -> int:
    artifact_dir = _resolve_dir(args.artifact_dir)
    if artifact_dir is None:
        print(f"Artifact dir not found: {args.artifact_dir}", file=sys.stderr)
        return 2

    # Accept either a run dir (per-adapter subdirs) or a single adapter dir.
    if (artifact_dir / PREDICTIONS_FILENAME).is_file() or (
        artifact_dir / "cases.jsonl"
    ).is_file():
        adapter_dirs = [artifact_dir]
    else:
        adapter_dirs = [
            d
            for d in sorted(artifact_dir.iterdir())
            if d.is_dir()
            and ((d / PREDICTIONS_FILENAME).is_file() or (d / "cases.jsonl").is_file())
        ]
    if args.adapter:
        adapter_dirs = [d for d in adapter_dirs if d.name == args.adapter]
    if not adapter_dirs:
        print(f"No adapter artifacts under {artifact_dir}", file=sys.stderr)
        return 2

    env = (
        _read_json(artifact_dir / "environment.json")
        or _read_json(artifact_dir.parent / "environment.json")
        or {}
    )

    total_misses = 0
    for adapter_dir in adapter_dirs:
        adapter_name = adapter_dir.name
        conf = _read_json(adapter_dir / "configuration.json") or {}
        dataset_name = conf.get("dataset") or env.get("dataset")
        cases: list[BenchmarkCase] = []
        if dataset_name:
            dataset_dir = _dataset_dir(dataset_name)
            if dataset_dir.exists():
                cases = load_cases(dataset_dir)
        by_id = {c.query_id: c for c in cases}
        preds = _load_prediction_records(adapter_dir / PREDICTIONS_FILENAME)
        misses = _iter_misses(adapter_dir, cases)
        total_misses += len(misses)

        print(f"== {adapter_name} — {len(misses)} misses ==")
        for case_id, rep, reasons in misses:
            case = by_id.get(case_id)
            case_type = case.expected.case_type if case else "?"
            reason = "; ".join(reasons) if reasons else "miss"
            print(f"{case_id} r{rep} {case_type} — {reason}")
            if case is not None:
                print(f"  Q: {case.query}")
            expected = _expected_source_uris(case) if case else []
            print(f"  expected: {', '.join(expected) or '(none declared)'}")
            entry = preds.get((case_id, rep))
            top3 = [r.source_uri for r in entry["results"][:3]] if entry else []
            print(f"  top-3: {', '.join(top3) or '(no results)'}")
        print()

    if total_misses == 0:
        print("No misses found.")
    return 0


def cmd_compare(old: str, new: str) -> int:
    from benchmarks.reporting import compare

    return compare(old, new)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.compare_runner")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="run the benchmark suite")
    run_p.add_argument(
        "--dataset",
        default=None,
        help=f"dataset under benchmarks/datasets (default: {DEFAULT_DATASET}; "
        "for --evaluate-only, read from the run's configuration)",
    )
    run_p.add_argument(
        "--adapters",
        nargs="+",
        default=None,
        help=f"default: {' '.join(DEFAULT_ADAPTERS)}; for --evaluate-only, "
        "all adapters with predictions",
    )
    run_p.add_argument("--k", type=int, default=10)
    run_p.add_argument("--repeats", type=int, default=2)
    run_p.add_argument("--out", default="artifacts")
    run_p.add_argument(
        "--run-id",
        default=None,
        help="reuse/continue this artifact dir instead of minting a " "timestamped one",
    )
    run_p.add_argument(
        "--resume",
        action="store_true",
        help="skip (case_id, repeat) pairs already in predictions.jsonl",
    )
    run_p.add_argument(
        "--predict-only",
        action="store_true",
        help="stream predictions + manifests only; skip scoring/metrics/report",
    )
    run_p.add_argument(
        "--evaluate-only",
        metavar="RUN_DIR",
        default=None,
        help="score an existing predictions dir; no adapter setup/retrieval",
    )
    run_p.set_defaults(func=cmd_run)

    cmp_p = sub.add_parser("compare", help="compare two artifact runs")
    cmp_p.add_argument("old")
    cmp_p.add_argument("new")
    cmp_p.set_defaults(func=lambda a: cmd_compare(a.old, a.new))

    fail_p = sub.add_parser(
        "failures", help="print per-case misses from an artifact dir"
    )
    fail_p.add_argument("artifact_dir", help="run dir or single adapter dir")
    fail_p.add_argument("--adapter", default=None, help="only this adapter")
    fail_p.set_defaults(func=cmd_failures)

    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] not in {"run", "compare", "failures"}:
        argv = ["run", *argv]
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
