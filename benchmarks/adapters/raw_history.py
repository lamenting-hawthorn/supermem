"""raw_history adapter — naive keyword-overlap baseline (RAG-ish)."""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from pathlib import Path

from benchmarks.harness_types import (
    BaseBenchmarkAdapter,
    CitedResult,
    Mutation,
)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


class RawHistoryAdapter(BaseBenchmarkAdapter):
    """Scores whole files by token overlap with the query. No lifecycle, no
    citations freshness guarantees beyond current-file digests — the baseline
    that shows why structured memory matters."""

    name = "raw_history"

    def __init__(self) -> None:
        self._files: dict[str, Path] = {}

    async def setup(self, workspace: Path, dataset_dir: Path) -> None:
        self._workspace = workspace
        sources = dataset_dir / "sources"
        for md in sorted(sources.glob("*.md")):
            dest = workspace / "entities" / md.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(md.read_bytes())
            rel = f"entities/{md.name}"
            self._files[rel] = dest

    async def mutate(self, mutation: Mutation) -> None:
        p = mutation.payload
        if mutation.type == "modify_file":
            path = self._workspace / p["path"]
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace(p["old_substring"], p["new_substring"]),
                encoding="utf-8",
            )
        elif mutation.type == "delete_file":
            path = self._workspace / p["path"]
            rel = str(path.relative_to(self._workspace)).replace("\\", "/")
            path.unlink(missing_ok=True)
            self._files.pop(rel, None)

    async def retrieve(self, query: str, k: int = 10) -> list[CitedResult]:
        started = time.perf_counter()
        q_tokens = Counter(_tokens(query))
        scored: list[tuple[float, str]] = []
        for uri, path in sorted(self._files.items()):
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            c_tokens = Counter(_tokens(content))
            overlap = sum(min(q_tokens[t], c_tokens.get(t, 0)) for t in q_tokens)
            denom = math.sqrt(sum(q_tokens.values())) or 1.0
            score = overlap / denom
            if score > 0:
                scored.append((score, uri))
        scored.sort(key=lambda x: (-x[0], x[1]))
        latency_ms = (time.perf_counter() - started) * 1000.0
        results: list[CitedResult] = []
        for rank, (score, uri) in enumerate(scored[:k], start=1):
            path = self._files[uri]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            results.append(
                CitedResult(
                    memory_id=f"raw:{uri}",
                    memory_revision=1,
                    content=path.read_text(encoding="utf-8", errors="replace"),
                    source_uri=uri,
                    source_revision=1,
                    source_span=f"{uri}#whole",
                    source_digest=digest,
                    retrieval_tier=self.name,
                    retrieval_score=round(score, 6),
                    latency_ms=latency_ms / max(len(scored[:k]), 1),
                )
            )
        return results

    async def teardown(self) -> None:
        self._files.clear()
