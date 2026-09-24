# supermem benchmarks

Two complementary harnesses live here:

1. **BM-0 contract harness** (`python -m benchmarks.runner`, `make bm0`) — the
   frozen acceptance proof from `docs/handoffs/01-bm0-acceptance-contract.md`.
   Exercises `supermem.local_cited_memory.LocalCitedMemory` end to end:
   immutable source revisions → canonical SQLite state → authoritative FTS5
   retrieval with digest-verifiable citations. Fresh temp state per fixture;
   run twice and compare `normalized_run_digest`.

2. **Competitive benchmark** (`python -m benchmarks.compare_runner`,
   `make bench`) — multi-adapter retrieval-quality comparison so we can know
   our place and track regressions forever:

   ```bash
   uv run python -m benchmarks.compare_runner                       # default adapters
   uv run python -m benchmarks.compare_runner compare artifacts/<old> artifacts/<new>
   ```

   Artifacts land in `artifacts/<run-id>/<adapter>/…` (gitignored). Exit code 1
   if any product-adapter gate regressed.

## Competitive-benchmark adapters

| Adapter | What it proves |
|---|---|
| `no_memory` | Contamination baseline: must return nothing for every query |
| `raw_history` | Naive keyword-overlap RAG baseline (no lifecycle awareness) |
| `supermem_fts` | The real SQLite+FTS5 pipeline: supersession, deletion, retraction, live expiry, privacy stripping, citations |
| `supermem_hybrid` | FTS + vector fusion via the production path (`create_vector_manager` → sqlite-vec + `HybridRetriever` RRF, post-fusion lifecycle filter) |

Baseline adapters are informational; gate failures fail the run only for
`supermem_*` adapters.

### Gates

- stale / expired / retracted / deleted-source / private recall rates exactly zero
- unknown queries contaminate nothing
- every positive result carries a citation whose digest matches the current source file
- two repeats produce identical normalized outcomes (variance rate 0)
- compression does not destroy the only retrievable authority

Failures are reported honestly in `report.md` and `cases.jsonl` — never tuned
away. The first honest baseline establishes the comparison.

## Datasets

- `datasets/bm0-local/` — BM-0 contract fixtures (upstream harness).
- `datasets/competitive-local/` — the 12-case competitive matrix (17 cases
  incl. phase-2 post-mutation cases); schema frozen in
  `benchmarks/harness_types.py`; mutations ordered in `manifest.json`.
- `datasets/paraphrase-local/` — paraphrase-recall probe: 8 positive cases
  whose queries share zero salient tokens with the target source (lexical
  retrieval cannot match), plus 2 abstention cases stressing the vector
  distance floor.
- `datasets/longmemeval-subset/` — generated (not committed) via
  `longmemeval_convert`: per-session source files + evidence needles from
  `answer_session_ids`.

### Measured receipts (run id, artifact)

| Corpus | `supermem_fts` | `supermem_hybrid` | Reading |
|---|---|---|---|
| competitive-local | recall@10=1.000 | recall@10=1.000 | `20260924T021537Z` — all gates pass; fused tiers incl. vector hits |
| paraphrase-local | recall@10=0.000 | recall@10=0.750 | `20260924T021524Z` — the semantic-recall delta: 6/8 paraphrase hits under the 0.35 floor; 2 abstentions correctly empty |
| longmemeval-subset (90, per-session) | recall@10=0.000 | recall@10=0.000 | `20260924T023105Z` — honest negative: short formal questions vs informal chat evidence defeats bge-small bi-encoding; targets rarely rank top-10 even unfiltered. Retrieval on conversational memory needs query expansion / stronger embeddings — not the distance floor |

### Adding a competitive dataset

Create `datasets/<name>/{manifest.json, dataset.jsonl, sources/}` and run with
`--dataset <name>`.

## Layout

```
benchmarks/
  runner.py           # BM-0 contract harness (upstream)
  compare_runner.py   # competitive multi-adapter harness
  harness_types.py    # competitive contracts (CitedResult, adapter protocol)
  oracle.py scoring.py reporting.py
  adapters/           # competitive retrieval backends under test
  datasets/
    bm0-local/        # contract fixtures
    competitive-local/# competitive fixtures
  tests/              # competitive-harness unit tests
```
