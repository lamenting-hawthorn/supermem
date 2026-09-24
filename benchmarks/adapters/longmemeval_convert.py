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
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Converter output-version note recorded in manifest.json so generated
# datasets are self-describing (bump when the emitted case/manifest schema
# or conversion semantics change).
CONVERTER_VERSION = "0.2"

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
            # Real LongMemEval turns are {"role","content"}; the legacy
            # {"field","value"} shape is kept for hand-written fixtures.
            field = str(turn.get("role") or turn.get("field", "user"))
            value = str(turn.get("content") or turn.get("value") or "")
            value = value.replace("\r\n", "\n").replace("\n", " ")
            lines.append(f"- {field}: {value}")
    return "\n".join(lines) + "\n"


def evidence_turn_text(session: list) -> str:
    """Content of the turns marked ``has_answer`` — the sharpest evidence."""
    parts: list[str] = []
    for turn in session if isinstance(session, list) else []:
        if isinstance(turn, dict) and turn.get("has_answer"):
            parts.append(str(turn.get("content") or turn.get("value") or ""))
    return "\n".join(parts)


def render_one_session_md(session: list, observed_epoch: float | None) -> str:
    """Render a single haystack session as its own source file.

    One file per session keeps sources small enough to be fully indexed and
    retrieved (vault observations truncate at 4096 chars), and lets the case
    point its citation at the evidence session specifically.
    """
    return render_session_md([session], observed_epoch)


def evidence_indices(rec: dict, n_sessions: int) -> set[int]:
    """Indices of the haystack sessions that contain the answer evidence."""
    ans_ids = rec.get("answer_session_ids")
    hs_ids = rec.get("haystack_session_ids")
    if not isinstance(ans_ids, list) or not isinstance(hs_ids, list):
        return set(range(n_sessions))
    wanted = {str(a) for a in ans_ids}
    idxs = {i for i, sid in enumerate(hs_ids) if str(sid) in wanted}
    return idxs or set(range(n_sessions))


def evidence_needles(
    answer: str, evidence_text: str, answer_turn_text: str = ""
) -> list[str]:
    """Needles for retrieval recall: answer terms first, then distinctive
    evidence-session terms.

    LongMemEval answers are often paraphrastic — verbatim answer words verify
    rarely. When too few answer words appear literally in the evidence file,
    fall back to the evidence session's own distinctive tokens (preferring
    ``has_answer`` turn content), so the case still measures 'did the evidence
    surface' rather than being vacuous.
    """
    needles = extract_must_include(answer, evidence_text.lower())
    if len(needles) >= 2:
        return needles[:5]
    seen = set(needles)
    # Prefer distinctive tokens from has_answer turns, then the session text.
    for source in (answer_turn_text, evidence_text):
        for word in content_words(source):
            if len(word) >= 6 and word not in seen:
                seen.add(word)
                needles.append(word)
                if len(needles) >= 5:
                    return needles[:5]
    return needles[:5]


def haystack_epoch(rec: dict) -> float | None:
    """Latest haystack timestamp: `haystack_date` (scalar) or the max of
    `haystack_dates` (cleaned format's list)."""
    epoch = parse_epoch(rec.get("haystack_date"))
    if epoch is not None:
        return epoch
    dates = rec.get("haystack_dates")
    if isinstance(dates, list) and dates:
        epochs = [e for e in (parse_epoch(d) for d in dates) if e is not None]
        if epochs:
            return max(epochs)
    return None


def map_case_type(question_type: str | None) -> tuple[str, bool]:
    """Returns (case_type, expect_empty)."""
    qt = (question_type or "").replace("-", "_")
    if qt == "abstention":
        return "unknown_query", True
    if qt == "temporal_reasoning":
        return "effective_interval", False
    return "exact_positive", False


def _sha256_of(path: Path) -> str:
    """Streaming SHA256 of the input file for manifest pinning."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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

            observed_epoch = haystack_epoch(rec)
            if observed_epoch is None:
                observed_epoch = parse_epoch(rec.get("question_date"))

            # One source file per haystack session — fat per-record files get
            # truncated at the observation layer (4096 chars) which makes the
            # recall check vacuous and destroys per-session citation fidelity.
            evidence_idx = evidence_indices(rec, len(sessions))
            session_files: list[tuple[int, str, str]] = []
            for j, session in enumerate(sessions):
                text = render_one_session_md(session, observed_epoch)
                fname = f"session-{idx}-s{j}.md"
                (sources_dir / fname).write_text(text, encoding="utf-8")
                session_files.append((j, fname, text))
            if not session_files:
                text = render_one_session_md([], observed_epoch)
                fname = f"session-{idx}-s0.md"
                (sources_dir / fname).write_text(text, encoding="utf-8")
                session_files.append((0, fname, text))
            first_evidence = next(
                (f for j, f, _ in session_files if j in evidence_idx),
                session_files[0][1],
            )

            case_type, expect_empty = map_case_type(
                question_type if isinstance(question_type, str) else None
            )
            # Cleaned LongMemEval marks abstention via the question_id suffix
            # (`*_abs`) rather than a distinct question_type.
            if not expect_empty and str(rec.get("question_id", "")).endswith("_abs"):
                case_type, expect_empty = "unknown_query", True

            expected: dict = {
                "case_type": case_type,
                # Original LongMemEval taxonomy, kept for per-type metric
                # breakdowns (case_type is the coarser BM-0 mapping).
                "question_type": question_type_key,
                "must_include": [],
                "must_exclude": [],
                "expect_empty": expect_empty,
                "source_uri": f"entities/{first_evidence}",
                # Session-level recall oracle (the LongMemEval community
                # convention): a hit is any result whose source_uri is one of
                # the answer-bearing sessions. Verbatim needles stay as a
                # secondary diagnostic only.
                "source_uris": [
                    f"entities/{f}" for j, f, _ in session_files if j in evidence_idx
                ],
                "phase": 1,
            }
            note: str | None = None
            if not expect_empty:
                answer = rec.get("answer")
                # Strip the observed_at frontmatter so needles come from real
                # content, not generated metadata.
                evidence_text = "\n".join(
                    "\n".join(
                        line
                        for line in text.splitlines()
                        if not line.startswith(("observed_at:", "---"))
                    )
                    for j, _f, text in session_files
                    if j in evidence_idx
                )
                answer_turn_text = "\n".join(
                    evidence_turn_text(sessions[j])
                    for j in evidence_idx
                    if j < len(sessions)
                )
                needles = (
                    evidence_needles(str(answer), evidence_text, answer_turn_text)
                    if answer is not None
                    else []
                )
                # must_include is matched case-sensitively by the oracle against
                # indexed observation content; verify literal presence in files.
                file_text = "\n".join(t for _j, _f, t in session_files)
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
            if (question_type or "").replace("-", "_") == "temporal_reasoning":
                as_of = haystack_epoch(rec)
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

    # Pin the conversion inputs so every generated dataset is
    # self-describing: which source file, its content hash, when, and with
    # which converter version (original vs cleaned LongMemEval scores are
    # not comparable — the source identity is part of the number).
    manifest = {
        "name": outdir.name,
        "version": "0.2",
        "description": "LongMemEval questions converted to BM-0 retrieval cases; sources are rendered session transcripts.",
        "mutations": [],
        "source": "LongMemEval (https://hqsiswiliam.github.io/longmemeval/) converted",
        "source_dataset": input_path.name,
        "source_sha256": _sha256_of(input_path),
        "n_records_in": n_records_in,
        "n_cases": n_cases_out,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "converter": "benchmarks.adapters.longmemeval_convert",
        "converter_version": CONVERTER_VERSION,
        "question_type_counts": type_counts,
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
