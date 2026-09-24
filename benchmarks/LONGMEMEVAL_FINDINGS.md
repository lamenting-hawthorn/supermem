# LongMemEval probe — deep findings (2026-09-24)

Why the LongMemEval-S run scored ~0, what we fixed, and what the external
research says the stack still needs. Receipts: `20260924T065328Z-b2a39106`
(pre-fix), paraphrase `20260924T021524Z`, competitive `20260924T021537Z`.

## Root-cause chain (in order of severity)

### 1. The corpus was empty — converter format mismatch (THE bug)

`longmemeval_convert.render_session_md` read `turn["field"]`/`turn["value"]`;
real LongMemEval turns are `{"role": ..., "content": ...}` — every file
contained only `- user: ` bullets. All 24,032 sources were empty.
**The ~0 result measured nothing.** Fixed: reads `role`/`content` with
`field`/`value` fallback.

### 2. The oracle measured the wrong thing

`must_include` required *verbatim* answer needles in returned content.
LongMemEval answers are abstractive ("Business Administration"), rarely
verbatim in transcripts. **Community convention (MemPalace, Zep, the paper's
retrieval protocol): session-level `recall_any@k` on `answer_session_ids`** —
a hit is a result citing an evidence session. Implemented as
`expected.source_uris` (primary oracle); `must_include` remains a diagnostic.
`has_answer` turns now drive needle extraction; `_abs` question_id suffix →
`expect_empty` (was missed entirely); numeric answers coerced via `str()`.

### 3. FTS coverage gate was stem-blind — NL questions returned literally []

`database.py::_term_coverage` compared *raw* tokens while the FTS5 index is
porter-stemmed: "graduate" never matched "graduated". Combined with
`ceil(n/2)` coverage on questions whose content terms are mostly stopwords,
FTS returned `[]` on nearly every question. Fixed: lite-stemmer (`_stem`)
normalizes both sides — deterministic, matches index semantics.

### 4. Missing BGE query asymmetry

`SqliteVecManager.search` embedded queries via `embed()` — same path as
documents. BGE models are trained asymmetric ("Represent this sentence for
searching relevant passages: " prefix). NOTE: fastembed's ONNX
`query_embed()` is a pass-through for bge-small — the prefix must be added
by the caller. `FastembedEmbeddingFunction.query_embed` added; on measured
data the prefix is a marginal improvement (recall@3 7→8/15) — correct to
keep, not transformative.

### 5. Fusion starved by per-leg k

Both legs fetched `limit=10` — evidence at rank 11–30 never reached RRF.
Fixed: legs fetch `max(3*limit, 30)`.

### 6. Distance floor cannot carry abstention on this domain

Measured: real evidence at 0.15–0.43; out-of-scope noise at 0.24–0.44 —
**completely overlapping**. No absolute threshold separates them. The 0.35
default remains correct for clean corpora; on conversational data it both
drops real hits and still leaks noise on `_abs` queries. Neither ms-marco
nor bge-reranker-base separate cleanly either (hits scored −10, noise +2).
**Abstention on conversational corpora is a known-hard open item** —
options: reranker-score gating (imperfect here), or LLM-judged relevance.

### 7. Bulk indexing throughput

Per-file `to_thread` embed calls → ~40min for 4.3k files, ~4h for 24k.
Fixed: `walk()` defers vector ingest; `upsert_many` embeds flattened chunks
in 64-batches then writes per-source. Still CPU-bound (~13k chunks ≈ 15–25
min) — the residual lever is chunk-count reduction or a stronger runtime.

## What external systems actually do (research summary)

- **Mem0** (94.4–94.8% LME-S QA): LLM extraction at ingest → declarative
  ~10-token facts (assistant turns included, +53.6pts that category);
  ADD-only + temporal metadata (+42.1 temporal); retrieval =
  semantic+BM25+entity-boost additive fusion, semantic threshold gate.
- **Supermemory**: documents vs *memories* (atomic facts, "dreaming"
  deferred extraction); `updates`/`extends`/`derives` edges + `isLatest`
  version chains; `forgetAfter` TTL + `forgetReason`; threshold 0.5–0.6;
  query rewriting + rerank. 95% Recall@15 claimed.
- **LongMemEval reality**: MemPalace reaches **96.6% R@5 / 98.2% R@10 with
  plain ChromaDB + MiniLM, zero LLM** — retrieval recall does NOT require
  an LLM. QA accuracy does. Session granularity is the eval unit; rounds
  (turn pairs) index better. Random R@10 ≈ 20% on ~50-session corpora.

## Issues log — remaining work

| # | Issue | Status | Next step |
|---|---|---|---|
| I1 | Empty corpus (converter format) | **fixed** | regenerated |
| I2 | Needle oracle ≠ session recall | **fixed** | `source_uris` primary |
| I3 | FTS stem-blind coverage | **fixed** | lite-stemmer |
| I4 | BGE query prefix | **fixed** | `query_embed` path |
| I5 | Per-leg k starves fusion | **fixed** | 3× over-fetch |
| I6 | Slow bulk embed | **fixed (partial)** | batched; CPU-bound remains |
| I7 | Distance floor ↔ abstention | **open** | needs reranker-score gate or per-domain floor |
| I8 | Reranker off by default in bench | **fixed** | adapter enables it |
| I9 | **No extraction layer** (raw transcript indexed; every winner extracts facts) | **open — biggest architectural gap** | ingest-time LLM fact extraction (optional, off by default) |
| I10 | No temporal query handling (`question_date` ↔ `haystack_dates`; +7–11% on 133 temporal cases) | **open** | resolve relative-time phrases / date-filter boost |
| I11 | Session granularity vs round granularity (rounds index better) | open | evaluate turn-pair files |
| I12 | `_abs` abstention leaks noise under any floor on this corpus | open | honest residual — report, don't tune away |

## Honest expected outcome after fixes

With the real corpus + session oracle: vector leg alone measured 10/15
recall@10 unfiltered on a 15-record slice (67%); hybrid (FTS fix + over-fetch
+ reranker) should land well above zero — the community range for this
protocol is ~70–97% R@5 on comparable stacks. `_abs` abstention cases will
partially fail (I7/I12) — that is the honest residual, not a bug to hide.
