# BM-0 local benchmark

BM-0 is the frozen, local-only proof that Markdown can move through an immutable
source revision into canonical SQLite state and authoritative FTS5 retrieval with
a verifiable citation. It enables only canonical SQLite and its FTS5 projection.
Vector, graph, and Tier 4 agent retrieval are disabled and unproved here.

The only supported BM-0 public retrieval boundary is
`supermem.local_cited_memory.LocalCitedMemory.retrieve(RetrievalQueryV1)`.
`LocalCitedMemory.ingest_markdown()` accepts stable non-file `memory://` URIs for
the fixture. A future vault adapter must canonicalize a vault-root-relative URI,
reject traversal and symlink escapes before calling this boundary, and use the
same `chars:<start>:<exclusive-end>` Unicode code-point source-span convention.
BM-0 makes no
claim for arbitrary file URI ingestion.

Run from a clean checkout with fresh temporary state (run this exact command
twice and compare `normalized_run_digest`):

```bash
.venv/bin/python -m benchmarks.runner --output-root /private/tmp/supermem-bm0-artifacts
```

Each invocation creates fresh SQLite state per fixture. Artifacts include raw
case receipts without raw memory text, metrics, environment/configuration, and a
summary report that explicitly records failures, timeouts, unsupported, and
inconclusive cases.
