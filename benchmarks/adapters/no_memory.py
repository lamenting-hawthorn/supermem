"""no_memory adapter — always returns nothing (contamination baseline)."""

from __future__ import annotations

from pathlib import Path

from benchmarks.harness_types import (
    BaseBenchmarkAdapter,
    CitedResult,
    Mutation,
)


class NoMemoryAdapter(BaseBenchmarkAdapter):
    name = "no_memory"

    async def setup(self, workspace: Path, dataset_dir: Path) -> None:
        self._workspace = workspace

    async def mutate(self, mutation: Mutation) -> None:
        return None

    async def retrieve(self, query: str, k: int = 10) -> list[CitedResult]:
        return []

    async def teardown(self) -> None:
        return None
