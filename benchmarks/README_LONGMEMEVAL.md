# LongMemEval external benchmark

[LongMemEval](https://hqsiswiliam.github.io/longmemeval/) is the public
long-term-memory benchmark (used by Zep and others). This converter turns it
into a supermem competitive-harness dataset so we can compare our retrieval
quality against published baselines over time.

## What IS and IS NOT comparable

Honest framing: LongMemEval's headline numbers measure **LLM answer accuracy**
over retrieved context. Our competitive harness measures **retrieval quality
only** — recall@k / MRR / prohibited-content leakage / citation verification,
with no LLM in the loop. So:

- Comparable over time: any change to supermem's retrieval (FTS ranking, RRF
  fusion, vector tier) measured on the same converted questions.
- Not directly comparable: our recall@k vs published LongMemEval accuracy
  percentages. Use this to track *our* trajectory, not to claim parity with
  Mem0/Zep headline numbers. For headline claims, an answer-generation layer
  with a blinded judge would have to be added later (handoff 01 §Later
  end-to-end answer evaluation).

## Run

```bash
# 1. Download LongMemEval-S (smallest split) — data is NOT vendored here:
#    https://github.com/HappyClipper/LongMemEval or the HuggingFace mirror.
#    You want longmemeval_s.jsonl.

# 2. Convert (deterministic; sessions render as markdown sources):
uv run python -m benchmarks.adapters.longmemeval_convert \
    --input ~/Downloads/longmemeval_s.jsonl \
    --outdir benchmarks/datasets/longmemeval-subset

# 3. Benchmark (offline, no LLM needed):
make bench-with DATASET=longmemeval-subset
# or directly:
uv run python -m benchmarks.compare_runner --dataset longmemeval-subset \
    --adapters no_memory raw_history supermem_fts

# 4. Compare against a previous run:
uv run python -m benchmarks.compare_runner compare artifacts/<old> artifacts/<new>
```

## Conversion mapping

| LongMemEval question_type | Competitive case_type | Notes |
|---|---|---|
| abstention | `unknown_query` (`expect_empty`) | Nothing in haystack ⇒ nothing retrievable |
| temporal_reasoning | `effective_interval` | `temporal_bound.as_of` = haystack date epoch |
| everything else | `exact_positive` | `must_include` = up to 5 gold-answer words verified present in the rendered source |

Malformed records are skipped with a warning and counted in the summary.
