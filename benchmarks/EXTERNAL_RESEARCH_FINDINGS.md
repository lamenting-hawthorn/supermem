# External memory-benchmark research — prioritized findings

Date: 2026-09-24
Sources read in full:

- Cognee, "AI Memory Benchmarks: The Complete Guide (2026)" — https://www.cognee.ai/ai-memory-benchmarks
- Mem0, "LoCoMo vs. LongMemEval vs. BEAM: The 2026 AI Memory Benchmark Guide" — https://mem0.ai/blog/ai-memory-benchmarks-in-2026
- Supermemory MemoryBench docs — /docs/memorybench/extend-benchmark, /extend-provider, /memscore
- mem0ai/memory-benchmarks repo — https://github.com/mem0ai/memory-benchmarks

Scope: what these sources imply for (a) our benchmark harness, (b) our metrics
and reporting, (c) the product, (d) which benchmarks to add. Ordered by
priority; each item maps to existing code where relevant.

---

## P0 — Harness integrity (do these first)

### P0.1 Checkpointing + resume + predict/evaluate split

`benchmarks/compare_runner.py` has no checkpoint, resume, or
predict-only/evaluate-only split (grepped — nothing). A 30–90 min API-bound
run is currently all-or-nothing; a crash at minute 80 loses everything.

The mem0 suite's design to copy:

- `--resume` — continue a run from checkpointed per-query results
- `--predict-only` — stop after the search stage, cache retrieval output
- `--evaluate-only` — score cached retrieval output without re-hitting
  embedding/rerank APIs

Implementation: stream each query's `list[CitedResult]` to disk as produced
(e.g. `predictions.jsonl` under the artifact dir), then let scoring/receipt
generation run over the file. This also makes judge experiments and scoring
changes re-runnable for free.

### P0.2 Multi-k evaluation from one retrieval pass

We currently run `--k 10` only. Mem0 retrieves deep once (`--top-k 200`) and
scores at cutoffs `10,20,50,200` from the same result list. Retrieve k=50
once and report recall@1/3/5/10/30/50.

Why it matters: it distinguishes *ranking* failures (hit at 30, miss at 10 →
reranker problem) from *retrieval* failures (miss at 50 → indexing problem).
Their published data shows the tradeoff is real — on LongMemEval
multi-session questions, top-50 scored **higher** than top-200 (93.2% vs
88.0%); deeper retrieval injects noise. `--k` should become `--k-cutoffs`.

### P0.3 Pin and record the dataset version

LongMemEval authors released `xiaowu0162/longmemeval-cleaned` (HF, Sept 2025)
fixing annotation issues; original vs cleaned scores are **not comparable**
and published evals often don't say which they used. Our
`datasets/longmemeval-full/manifest.json` only says "converted".

Actions:

- Confirm which source file `longmemeval-full` was converted from; re-convert
  from `longmemeval-cleaned` if needed.
- Record dataset name, version, source URL, and content hash in
  `manifest.json` so every artifact is self-describing.

### P0.4 Per-question-type breakdown (LongMemEval types, not just case types)

Our converter maps LongMemEval types onto BM-0 case types
(`README_LONGMEMEVAL.md`), losing the original vocabulary in the report.
Mem0's published per-type numbers show where the field's headroom actually
is:

| LongMemEval type | Mem0 top-200 | Field state |
|---|---|---|
| single-session-user | 98.6% | Saturated |
| single-session-assistant | 98.2% | Saturated |
| single-session-preference | 96.7% | Near-saturated |
| temporal-reasoning | 97.0% | Near-saturated |
| knowledge-update | 93.6% | Headroom — additive architectures surface stale facts |
| multi-session | 88.0% | The real weak spot |

Carry `question_type` through conversion into `ExpectedOutcome` metadata and
break the receipt down by it. If our per-type profile is inverted from this
table, the bug is localized for free.

### P0.5 Failure-inspection command

MemoryBench has `show-failures -r <run>` plus a `serve` UI; mem0 ships a
Next.js results browser. We have timestamped artifacts — add a
`compare_runner failures <artifact_dir>` subcommand dumping each missed case
with its top-k results and which oracle leg (source_uris vs must_include)
failed. Aggregates say *where*; per-case dumps say *why*.

---

## P1 — Metrics and reporting

### P1.1 NDCG@k

The LongMemEval repo reports Recall@k **and** NDCG@k. Our `recall_any` in
`scoring.py` is binary session-level; NDCG captures graded rank quality
(evidence at rank 1 vs rank 9 matters once a reader has a token budget).
`CitedResult` is already rank-ordered — straightforward add.

### P1.2 Oracle-split run (retrieval ceiling)

`longmemeval_oracle` contains only evidence sessions — the official way to
separate retriever error from reader error. For our retrieval-only harness it
is a ceiling check: anything under ~100% session recall on the oracle corpus
means a converter or indexing bug remains (like the empty-render bug already
fixed).

### P1.3 MemScore-style triple as the headline

MemoryBench's `MemScore: 86% / 145ms / 1823tok` — accuracy, search latency,
context tokens — never accuracy alone. We already collect all three
(`latency_percentiles`, `context_stats`); reformat the receipt to lead with
the triple. "Accuracy without a token budget is a half-finished score."

### P1.4 Accuracy-per-token vs `raw_history` is the real comparison

LongMemEval-S ≈ 115k tokens — inside modern context windows — so
`raw_history` is a *legitimately strong* baseline, not a strawman. Our pitch
isn't beating it on recall; it's matching recall at ~7K tokens/query vs 25K+
(mem0's published ratio ≈ 3–4×). Make `avg_context_tokens_per_recall`
(already computed in `scoring.py`) a headline number. Chroma's context-rot
research supports memory even under 128k: focused ~300-token prompts
outperformed full 113k prompts.

### P1.5 Methodology block on every published number

The mem0 post documents why scores disagree: ByteRover measured Zep at 75.1%
vs Zep's claimed 94.7%; Mem0 at 66.9% vs self-reported 92.5%. The spread is
judge model + answerer model + rerank on/off — not fraud. Every number we
publish must carry: dataset version, embedder, reranker + model, k, judge,
answerer. Our "remote-model capability benchmark" labeling on the current
run is correct — formalize it inside the receipt, not just in prose.

### P1.6 Latency positioning

Reported bands: <100ms excellent (vector-search level), 100–300ms good,
300–500 adequate, >500 slow. Our local SQLite path should sit far below
API-based memory products — p50 on the receipt is a sales number, not only
an engineering one.

---

## P2 — Product improvements

### P2.1 Entity linking as a third retrieval leg

Mem0's April-2026 pipeline: semantic similarity + BM25 + entity matching
scored in parallel and fused. We have FTS5 + sqlite-vec + Kuzu already — an
entity-extraction pass at ingest with entity-match boost in RRF is the
natural third leg. This is what multi-hop and cross-session questions reward.

### P2.2 Knowledge-update is our architectural differentiator — test it harder

Mem0's ADD-only design scores 93.6% on knowledge-update *because* superseded
facts surface alongside new ones. Our lifecycle model (retract/expire/modify)
should win structurally — but the converter maps knowledge-update questions
to plain `exact_positive`. Strengthen those cases: `must_exclude` terms from
the *superseded* answer, or require the newest evidence session specifically.
This is the category where we can claim a genuinely better architecture.

### P2.3 Abstention as a product feature

`_abs` questions grade whether the system declines. `unknown_contamination`
measures it at retrieval level — good — but add a product-side confidence
floor: below threshold, return empty rather than junk. For an MCP memory
tool, confidently-wrong is worse than no-results.

### P2.4 Per-user isolation — uncontested territory

Both articles flag that **no public benchmark tests multi-tenant
isolation**. Our `private_canary` case type already exceeds published evals.
Extend it into a real multi-user scenario (two tenants, interleaved
ingestion, cross-leak detection) — a claim competitors cannot currently
answer with a benchmark number.

### P2.5 Extraction quality is worth ~2–3 points

Mem0 measured extraction-model spread on identical stores: GPT-5 91.0% →
Gemma-4 88.6% → GPT-OSS-120B 89.8% → Llama-4-Maverick 88.6%. We index raw
sources; a fact-extraction layer at ingest is a real product decision, and
this is the evidence for its size of effect.

### P2.6 Write-selectivity is unmeasured by everyone

Benchmarks grade the read side; nobody measures what was stored (duplicate
rate, store size growth, contradiction pairs left unresolved). Add internal
metrics to the run receipt — store size, dedup ratio, stale-fact surface
rate — because "stores everything" and "stores what matters" look identical
on recall until token budgets bite.

---

## P3 — End-to-end eval and new benchmarks

### P3.1 Fastest path to a headline number: MemoryBench provider adapter

`supermemoryai/memorybench` is open source; a provider is five methods
(`initialize/ingest/awaitIndexing/search/clear`) — a thin wrapper over our
Worker API on :37777. That buys LoCoMo + LongMemEval + ConvoMem runs with a
judge LLM, latency, and token reporting, scored on identical footing as
Supermemory/Mem0/Zep. Days of work, and produces market-legible numbers
before we build our own judge layer. There's also a `benchmark-context`
skill that generates the adapter.

### P3.2 If we build our own judge layer, copy these design choices

- Judge-agnostic (GPT-4o / Sonnet / Flash selectable); score the same run
  with two judges to rule out grading bias.
- Per-question-type judge prompts — abstention graded differently than
  temporal.
- Graded scoring, not binary: BEAM scores rubric "nuggets" 0/0.5/1; a
  partial answer reads as 0.25, not a miss.
- Decide whether harness errors count against accuracy (MemoryBench
  excludes them, which inflates; counting is more honest).

### P3.3 Datasets to add, in order

| Priority | Benchmark | Why for us |
|---|---|---|
| 1 | **ConvoMem** | Ships inside MemoryBench; single-conversation reference resolution — cheapest new coverage |
| 2 | **LoCoMo** | Temporal + multi-hop + adversarial abstention; evidence turn IDs allow direct retrieval scoring. NB: 446/1986 questions are adversarial and often excluded — decide and document |
| 3 | **MemoryAgentBench** | Its "selective forgetting" / conflict-resolution competency is literally our mutation-phase design |
| 4 | **PersonaMem** | Preference drift / current-state tracking; v2 implicit personalization breaks frontier models (37–48%) — differentiating if we do well |
| 5 | **LongMemEval-M or BEAM-128k** | The scale tier where context-stuffing actually breaks; BEAM adds contradiction-resolution, event-ordering, instruction-following — nobody saturates it (SOTA ≈ 0.64–0.79) |
| 6 | **LongMemEval-V2** | Agent trajectories instead of chat — where the field is heading |

Also noted: **DolphinBench** (mem0) measures whether memory improves
tool-task completion — the agentic direction if we want it later.

---

## Rules for publishing numbers (from the methodology literature)

1. Always publish the protocol: dataset version, embedder, reranker, k,
   judge, answerer model.
2. Never compare our recall@k to someone's answer-accuracy headline.
3. Report the triple (accuracy / latency / tokens), not accuracy alone.
4. Keep `raw_history` in every run — it is the honest baseline at
   LongMemEval-S scale.
5. Label remote-model runs vs default local product config separately.
6. Include `variance_rate` (we run `--repeats 2`) — nondeterminism is part
   of the number.

## Suggested execution order

1. P0.1 checkpointing → P0.2 multi-k → P0.3 dataset pin → P0.4 per-type
   breakdown → P0.5 failures command
2. P1 metrics (NDCG, oracle run, MemScore receipt)
3. P3.1 MemoryBench provider adapter (headline comparability)
4. P2 product items (entity leg, knowledge-update tests, abstention floor,
   isolation bench)
5. P3.3 dataset adds
