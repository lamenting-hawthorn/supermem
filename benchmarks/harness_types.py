"""Frozen contracts for the supermem benchmark harness (BM-0).

These types implement the contract layer specified in
docs/handoffs/01-bm0-acceptance-contract.md. Adapters, the runner, and the
oracle all speak exclusively through these types.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Case types map 1:1 onto the BM0 fixture matrix in handoff 01.
CASE_TYPES = (
    "exact_positive",
    "unknown_query",
    "source_modified",
    "source_deleted",
    "retracted",
    "expired",
    "effective_interval",
    "private_canary",
    "compression_preservation",
    "citation_verification",
    "deterministic_replay",
    "injection_shaped",
)

# Cases whose expected outcome must hold after mutations have been applied.
MUTATION_PHASE = 2


@dataclass
class CitedResult:
    """CitedRetrievalResultV1 — every retrieval answer must carry evidence."""

    memory_id: str
    memory_revision: int
    content: str
    source_uri: str
    source_revision: int
    source_span: str
    source_digest: str
    retrieval_tier: str
    retrieval_score: float
    latency_ms: float


@dataclass
class ExpectedOutcome:
    case_type: str
    must_include: list[str] = field(default_factory=list)
    must_exclude: list[str] = field(default_factory=list)
    expect_empty: bool = False
    source_uri: str | None = None
    phase: int = 1


@dataclass
class BenchmarkCase:
    """RetrievalQueryV1 + expected outcome."""

    query_id: str
    query: str
    scope: str = "local"
    temporal_bound: dict | None = None  # {"as_of": epoch_float} | None
    max_records: int = 10
    timeout_ms: int = 5000
    correlation_id: str = "run"
    expected: ExpectedOutcome = field(default_factory=ExpectedOutcome)

    @classmethod
    def from_json(cls, raw: dict) -> "BenchmarkCase":
        exp_raw = raw.get("expected", {})
        expected = ExpectedOutcome(
            case_type=exp_raw.get("case_type", "exact_positive"),
            must_include=list(exp_raw.get("must_include", [])),
            must_exclude=list(exp_raw.get("must_exclude", [])),
            expect_empty=bool(exp_raw.get("expect_empty", False)),
            source_uri=exp_raw.get("source_uri"),
            phase=int(exp_raw.get("phase", 1)),
        )
        tb = raw.get("temporal_bound")
        return cls(
            query_id=raw["query_id"],
            query=raw["query"],
            scope=raw.get("scope", "local"),
            temporal_bound=tb,
            max_records=int(raw.get("max_records", 10)),
            timeout_ms=int(raw.get("timeout_ms", 5000)),
            correlation_id=str(raw.get("correlation_id", "run")),
            expected=expected,
        )


@dataclass
class Mutation:
    """A declarative state change applied between phase 1 and phase 2."""

    type: str  # modify_file | delete_file | retract_obs | expire_obs | archive_obs
    payload: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: dict) -> "Mutation":
        return cls(type=raw["type"], payload=dict(raw.get("payload", {})))


def load_cases(dataset_dir: Path) -> list[BenchmarkCase]:
    """Load ordered benchmark cases from <dataset_dir>/dataset.jsonl."""
    path = dataset_dir / "dataset.jsonl"
    cases: list[BenchmarkCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(BenchmarkCase.from_json(json.loads(line)))
    return cases


def load_manifest(dataset_dir: Path) -> dict[str, Any]:
    path = dataset_dir / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_mutations(manifest: dict[str, Any]) -> list[Mutation]:
    return [Mutation.from_json(m) for m in manifest.get("mutations", [])]


class BaseBenchmarkAdapter(ABC):
    """Contract every retrieval backend must satisfy to be benchmarked.

    Adapters ingest the corpus into fresh state under ``workspace``, apply
    declarative mutations, and answer queries with fully cited results.
    """

    name: str = "base"

    @abstractmethod
    async def setup(self, workspace: Path, dataset_dir: Path) -> None: ...

    @abstractmethod
    async def mutate(self, mutation: Mutation) -> None: ...

    @abstractmethod
    async def retrieve(self, query: str, k: int = 10) -> list[CitedResult]: ...

    @abstractmethod
    async def teardown(self) -> None: ...
