"""Local memory insights inspired by ambient-memory workflows.

This module keeps productivity helpers deterministic and local: it extracts
open loops, follow-up suggestions, and day summaries from stored observations
without requiring an LLM call.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

_TASK_RE = re.compile(
    r"\b(todo|to-do|task|action item|follow up|follow-up|need to|needs to|should|must|reply to|send|schedule|prepare|review|fix|ship|email|call)\b",
    re.IGNORECASE,
)
_DONE_RE = re.compile(
    r"\b(done|completed|closed|resolved|shipped|sent|fixed|cancelled|canceled)\b",
    re.IGNORECASE,
)
_OWNER_RE = re.compile(
    r"\b(?:owner|assignee|assigned to)\s*[:=]\s*([^\n,;]+)", re.IGNORECASE
)
_HASHTAG_RE = re.compile(r"(?<!\w)#([A-Za-z][\w-]+)")
_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{2,}\b")

_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "but",
    "can",
    "for",
    "from",
    "has",
    "have",
    "into",
    "need",
    "needs",
    "not",
    "now",
    "our",
    "out",
    "should",
    "that",
    "the",
    "this",
    "todo",
    "with",
    "will",
    "you",
}


@dataclass(frozen=True)
class OpenTask:
    """A locally inferred unresolved task/open loop."""

    obs_id: int
    created_at: float
    content: str
    confidence: float
    reason: str
    owner: str | None = None


def _snippet(text: str, max_chars: int = 280) -> str:
    cleaned = " ".join(text.split())
    return (
        cleaned
        if len(cleaned) <= max_chars
        else cleaned[: max_chars - 1].rstrip() + "…"
    )


def extract_open_tasks(observations: Iterable[dict], limit: int = 20) -> list[dict]:
    """Extract likely open tasks from observations using conservative heuristics."""
    candidates: list[OpenTask] = []
    for obs in observations:
        content = str(obs.get("content", ""))
        if not content or _DONE_RE.search(content):
            continue
        match = _TASK_RE.search(content)
        if not match:
            continue
        owner_match = _OWNER_RE.search(content)
        confidence = 0.65
        lowered = content.lower()
        if "?" in content or "follow" in lowered:
            confidence += 0.1
        if any(
            marker in lowered for marker in ("todo", "action item", "need to", "must")
        ):
            confidence += 0.15
        candidates.append(
            OpenTask(
                obs_id=int(obs.get("id", 0)),
                created_at=float(obs.get("created_at", 0.0)),
                content=_snippet(content),
                confidence=min(confidence, 0.95),
                reason=f"matched '{match.group(0)}'",
                owner=owner_match.group(1).strip() if owner_match else None,
            )
        )
    candidates.sort(key=lambda task: (task.confidence, task.created_at), reverse=True)
    return [task.__dict__ for task in candidates[:limit]]


def suggest_followups(open_tasks: Iterable[dict], limit: int = 10) -> list[dict]:
    """Turn open tasks into short, actionable follow-up suggestions."""
    suggestions = []
    for task in list(open_tasks)[:limit]:
        owner = task.get("owner")
        prefix = f"Ask {owner}" if owner else "Follow up"
        suggestions.append(
            {
                "obs_id": task.get("obs_id"),
                "suggestion": f"{prefix} about: {task.get('content')}",
                "confidence": task.get("confidence", 0.0),
            }
        )
    return suggestions


def summarize_days(observations: Iterable[dict], days: int = 7) -> list[dict]:
    """Build lightweight day summaries from recent observations."""
    buckets: dict[str, list[dict]] = {}
    for obs in observations:
        created_at = float(obs.get("created_at", 0.0))
        day = dt.datetime.fromtimestamp(created_at, tz=dt.UTC).date().isoformat()
        buckets.setdefault(day, []).append(obs)

    summaries = []
    for day in sorted(buckets.keys(), reverse=True)[:days]:
        rows = buckets[day]
        text = "\n".join(str(row.get("content", "")) for row in rows)
        tags = _HASHTAG_RE.findall(text)
        words = [
            w.lower() for w in _WORD_RE.findall(text) if w.lower() not in _STOPWORDS
        ]
        keywords = [word for word, _ in Counter(tags + words).most_common(8)]
        tasks = extract_open_tasks(rows, limit=5)
        summaries.append(
            {
                "date": day,
                "observation_count": len(rows),
                "keywords": keywords,
                "open_task_count": len(tasks),
                "highlights": [
                    _snippet(str(row.get("content", "")), 180) for row in rows[:5]
                ],
            }
        )
    return summaries
