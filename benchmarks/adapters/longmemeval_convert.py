"""LongMemEval → supermem competitive-harness dataset converter.

Converts a LongMemEval jsonl file (not vendored; download from
https://hqsiswiliam.github.io/longmemeval/) into a dataset directory the
BM-0 competitive harness can consume:

    uv run python -m benchmarks.adapters.longmemeval_convert \
        --input longmemeval_s.jsonl \
        --outdir benchmarks/datasets/longmemeval-subset [--max-cases N]

Each record's haystack sessions are rendered as Markdown under
``sources/session-<idx>.md`` with an ``observed_at: <epoch>`` YAML frontmatter
(where the date parses), and one ``dataset.jsonl`` case is emitted per record.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

QUESTION_TYPES_WITH_DEFAULT = {
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
}

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d (%a) %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_FRONTMATTER_RE = re.compile(r"^---\s*\nobserved_at:\s*([0-9.]+)\s*\n---\s*\n")


def parse_epoch(value: object) -> float | None:
    """Best-effort parse of a LongMemEval date string to a unix epoch."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def content_words(text: str) -> list[str]:
    """Distinctive candidate needles: lowercase content tokens, len > 3, deduped."""
    seen: set[str] = set()
    words: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        low = tok.lower()
        if len(low) <= 3 or low in seen:
            continue
        seen.add(low)
        words.append(low)
    return words


def extract_must_include(answer: str, haystack_text: str) -> list[str]:
    """Up to 5 answer content words that literally appear in the haystack text."""
    needles: list[str] = []
    for word in content_words(answer):
        if word not in haystack_text:
            continue
        if word not in needles:
            needles.append(word)
        if len(needles) == 5:
            break
    return needles


def render_session_md(sessions: list, observed_epoch: float | None) -> str:
    lines: list[str] = []
    if observed_epoch is not None:
        lines += ["---", f"observed_at: {observed_epoch}", "---", ""]
    for session in sessions:
        if not isinstance(session, list):
            continue
        for turn in session:
            if not isinstance(turn, dict):
                continue
            field = str(turn.get("field", "user"))
            value = str(turn.get("value", "")).replace("\r\n", "\n").replace("\n", " ")
            lines.append(f"- {field}: {value}")
    return "\n".join(lines) + "\n"


def map_case_type(question_type: str | None) -> tuple[str, bool]:
    """Returns (case_type, expect_empty)."""
    qt = question_type or ""
    if qt == "abstention":
        return "unknown_query", True
    if qt == "temporal_reasoning":
        return "effective_interval", False
    return "exact_positive", False


def convert(
    input_path: Path,
    outdir: Path,
    max_cases: int | None = None,
) -> dict:
    n_records_in = 0
    n_cases_out = 0
    n_skipped = 0
    type_counts: dict[str, int] = {}

    sources_dir = outdir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    cases_lines: list[str] = []

    with input_path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            n_records_in += 1
            try:
                rec = json.loads(line)
                valid = (
                    isinstance(rec, dict)
                    and isinstance(rec.get("question_id"), str)
                    and bool(rec["question_id"])
                    and isinstance(rec.get("question"), str)
                    and bool(rec["question"])
                )
            except json.JSONDecodeError:
                valid = False
            if not valid:
                n_skipped += 1
                n_records_in -= 1
                print(
                    f"warning: skipping malformed line {n_records_in + n_skipped}",
                    file=sys.stderr,
                )
                continue

            if max_cases is not None and n_cases_out >= max_cases:
                break

            question_type = rec.get("question_type")
            question_type_key = (
                question_type if isinstance(question_type, str) else "unknown"
            )
            type_counts[question_type_key] = type_counts.get(question_type_key, 0) + 1

            idx = n_cases_out
            sessions = rec.get("haystack_sessions")
            if not isinstance(sessions, list):
                sessions = []

            observed_epoch = parse_epoch(rec.get("haystack_date"))
            if observed_epoch is None:
                observed_epoch = parse_epoch(rec.get("question_date"))

            md_text = render_session_md(sessions, observed_epoch)
            (sources_dir / f"session-{idx}.md").write_text(md_text, encoding="utf-8")

            case_type, expect_empty = map_case_type(
                question_type if isinstance(question_type, str) else None
            )

            expected: dict = {
                "case_type": case_type,
                "must_include": [],
                "must_exclude": [],
                "expect_empty": expect_empty,
                "source_uri": f"entities/session-{idx}.md",
                "phase": 1,
            }
            note: str | None = None
            if not expect_empty:
                answer = rec.get("answer")
                haystack_text = "\n".join(
                    line
                    for line in md_text.splitlines()
                    if not line.startswith("observed_at:")
                )
                needles = (
                    extract_must_include(str(answer), haystack_text.lower())
                    if isinstance(answer, str)
                    else []
                )
                # must_include is matched case-sensitively by the oracle against
                # indexed observation content; verify literal presence in the file.
                file_text = (sources_dir / f"session-{idx}.md").read_text(
                    encoding="utf-8"
                )
                needles = [
                    n
                    for n in needles
                    if n in file_text
                    or any(n in w for w in _TOKEN_RE.findall(file_text))
                ]
                verified: list[str] = []
                for needle in needles:
                    if needle in file_text:
                        verified.append(needle)
                    elif re.search(
                        rf"\b{re.escape(needle)}\b", file_text, re.IGNORECASE
                    ):
                        verified.append(needle)
                expected["must_include"] = verified[:5]
                if not expected["must_include"]:
                    note = "no distinctive answer substring found in haystack; judged on retrieval presence only"

            if note is not None:
                expected["note"] = note

            temporal_bound = None
            if question_type == "temporal_reasoning":
                as_of = parse_epoch(rec.get("haystack_date"))
                temporal_bound = {"as_of": as_of} if as_of is not None else None

            case = {
                "query_id": rec["question_id"],
                "query": rec["question"],
                "scope": "local",
                "temporal_bound": temporal_bound,
                "max_records": 10,
                "timeout_ms": 5000,
                "correlation_id": "longmemeval",
                "expected": expected,
            }
            cases_lines.append(json.dumps(case, ensure_ascii=False))
            n_cases_out += 1

    manifest = {
        "name": "longmemeval-subset",
        "version": "0.1",
        "description": "LongMemEval questions converted to BM-0 retrieval cases; sources are rendered session transcripts.",
        "mutations": [],
        "source": "LongMemEval (https://hqsiswiliam.github.io/longmemeval/) converted",
        "n_cases": n_cases_out,
    }

    (outdir / "dataset.jsonl").write_text("\n".join(cases_lines), encoding="utf-8")
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    return {
        "n_records_in": n_records_in,
        "n_cases_out": n_cases_out,
        "n_skipped": n_skipped,
        "type_counts": type_counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmarks.adapters.longmemeval_convert",
        description="Convert LongMemEval jsonl into a BM-0 competitive dataset.",
    )
    parser.add_argument("--input", required=True, help="Path to LongMemEval .jsonl")
    parser.add_argument(
        "--outdir",
        default="benchmarks/datasets/longmemeval-subset",
        help="Output dataset directory",
    )
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"Input not found: {input_path}", file=sys.stderr)
        return 2

    stats = convert(input_path, Path(args.outdir), args.max_cases)

    print(f"records in : {stats['n_records_in']}")
    print(f"cases out  : {stats['n_cases_out']}")
    print(f"skipped    : {stats['n_skipped']}")
    print("by question_type:")
    for qt, count in sorted(stats["type_counts"].items()):
        print(f"  {qt}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
