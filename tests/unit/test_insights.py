from __future__ import annotations

import time

from supermem.capture.insights import (
    extract_open_tasks,
    summarize_days,
    suggest_followups,
)


def test_extract_open_tasks_skips_completed_items() -> None:
    observations = [
        {
            "id": 1,
            "created_at": time.time(),
            "content": "TODO: email Alice about launch",
        },
        {"id": 2, "created_at": time.time(), "content": "Done: email Bob about launch"},
    ]

    tasks = extract_open_tasks(observations)

    assert len(tasks) == 1
    assert tasks[0]["obs_id"] == 1
    assert "email Alice" in tasks[0]["content"]


def test_suggest_followups_turns_tasks_into_actions() -> None:
    tasks = [{"obs_id": 7, "content": "Need to review the PR", "confidence": 0.8}]

    suggestions = suggest_followups(tasks)

    assert suggestions == [
        {
            "obs_id": 7,
            "suggestion": "Follow up about: Need to review the PR",
            "confidence": 0.8,
        }
    ]


def test_summarize_days_groups_recent_observations() -> None:
    observations = [
        {"id": 1, "created_at": time.time(), "content": "TODO: ship #release notes"},
        {"id": 2, "created_at": time.time(), "content": "Discussed release plan"},
    ]

    summaries = summarize_days(observations, days=1)

    assert len(summaries) == 1
    assert summaries[0]["observation_count"] == 2
    assert summaries[0]["open_task_count"] == 1
    assert "release" in summaries[0]["keywords"]
