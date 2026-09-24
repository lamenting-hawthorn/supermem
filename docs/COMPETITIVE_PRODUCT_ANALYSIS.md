# Competitive product analysis — what a finished memory product looks like

Date: 2026-09-24
Method: full-repo analysis (shallow clones at `/tmp/membench-research/`) of
`mem0ai/mem0` (v2.2.0, post-v3 pipeline), `supermemoryai/supermemory` (API
contract via docs; engine is closed-source), `supermemoryai/memorybench`,
`getzep/graphiti`, `topoteretes/cognee`, `mem0ai/memory-benchmarks`, plus an
inventory of `supermem-p0` itself.

Companion doc: `benchmarks/EXTERNAL_RESEARCH_FINDINGS.md` (benchmark-harness
findings). This doc covers the **product** side: what competitors ship, what
we have, the gaps, and a recommended sequence.

---

## 0. The convergent architecture (what everyone ships)

Four independent codebases converged on the same skeleton:

```
raw input ──► async ingest (status machine) ──► LLM extraction ──► atomic
                                                          facts/memories
                                                              │
              ┌───────────────────────────────────────────────┤
              ▼                                               ▼
        document store                                fact/memory store
        (chunks, RAG)                        (versioned, bi-temporal,
              │                              deduped, entity-linked)
              └──────────► hybrid retrieval ◄─────────┘
                    vector + BM25 + entity boost, fused,
                    lifecycle-filtered, optionally reranked
                              │
                              ▼
              profile/context endpoint (static + dynamic
              + search results in ONE call — the hot path)
```

Key divergences worth noting:

- **mem0 v3 deleted the graph DBs and the UPDATE/DELETE LLM pass.** One
  additive extraction call + explicit-API mutations + entity co-occurrence
  side-index replaced ~4000 LOC of Neo4j/Memgraph/Kuzu code. Claimed effect:
  +20 pts LoCoMo, +26 pts LongMemEval, ~half extraction latency.
- **Graphiti went the other way** — full bi-temporal fact graph — but their
  own search never applies temporal predicates by default, and their bulk
  path drops within-batch invalidation. The machinery is real; the default
  read path doesn't use it.
- **Cognee's BEAM SOTA came from breadth, not depth**: same content embedded
  5 ways (chunk, summary, entity name, entity type, edge relation), merged
  lanes at top-k 20/20. Their graph machinery did NOT fix multi-hop /
  temporal / abstention (~0.5 each); session distillation is what scored.
- **Supermemory productized the lifecycle better than anyone**: version
  chains (`isLatest`, `parentMemoryId`), soft-forget with `forgetReason`,
  `forget-matching` with `dryRun`, and an `isInference` review queue.

Takeaway for supermem: **extraction + entity boosting + lifecycle-aware
reads are table stakes. Heavy graph machinery is optional and unproven on
the hardest question types.** Our Kuzu graph should carry topology and
provenance; facts belong in SQLite where FTS5 + sqlite-vec + temporal WHERE
clauses are free.

---

## 1. Gap analysis vs supermem today

Legend: ✅ have · ⚠️ partial · ❌ missing

| Capability | Competitors | supermem |
|---|---|---|
| LLM fact extraction at ingest | All (1–N calls/episode) | ❌ raw indexing only |
| Atomic self-contained memories | All ("15–80 word facts") | ❌ chunks/files only |
| Memory versioning chains | supermemory, mem0 (history table) | ⚠️ `superseded` status, no chains |
| Bi-temporal facts (valid/invalid/expired/reference) | graphiti | ⚠️ `valid_from/until`, `expires_at` — close, no `as_of` reads |
| Entity index beyond wikilinks | mem0 (side-collection), graphiti (LLM) | ❌ `[[wikilinks]]` only |
| Contradiction/supersession arithmetic | graphiti, supermemory (`updates` edges) | ⚠️ per-source supersede only |
| Semantic-gated hybrid fusion | mem0 (threshold gates semantic, BM25 boosts) | ⚠️ RRF k=60, both legs expand recall |
| Multi-view embeddings (summary/entity/edge lanes) | cognee | ❌ chunk text only |
| Async ingest + status machine | supermemory (queued→…→done), graphiti (per-group queue) | ❌ synchronous indexing |
| `customId` upsert / re-ingest diffing | supermemory, mem0 | ⚠️ content-hash dedup only |
| Soft-forget w/ dryRun preview | supermemory (`forget-matching`) | ❌ retract is immediate |
| Inference review queue | supermemory (`isInference`) | ❌ |
| Profile/context fused endpoint | supermemory `/v4/profile` | ❌ closest: `use_memory_agent` |
| Memory middleware (retrieve→inject→write-back) | supermemory `withSupermemory`, mem0 proxy | ⚠️ Claude Code hooks only |
| Per-user/container isolation | supermemory containerTags, cognee per-dataset engines | ❌ single-tenant vault |
| Scoped/expiring API keys | supermemory, mem0 | ❌ single static Bearer |
| Backup covers all stores | — | ⚠️ vault+supermem.db only; vectors.db & Kuzu excluded |
| MCP output schemas + annotations | supermemory (zod in+out, readOnly/destructive hints) | ❌ |
| Retrieval `explain`/score breakdown | mem0 `explain=true`, supermemory `similarity` labels | ⚠️ `source_tier` only |

Plus one correctness item found in our own inventory: `CLAUDE.md` says
`Agent.chat` "fails closed," but `agent/agent.py:104-165` still runs the
full model+executor loop — the denial only lives in `agent/tools.py`. Either
wire `AGENT_MEMORY_NAVIGATION_UNAVAILABLE` into `Agent.chat` or fix the doc.
Also: `supermem/__init__.py` says 0.3.1, `pyproject.toml` says 0.4.0.

---

## 2. What to build, in order

### P0 — Extraction layer (the product-defining gap)

Everyone's scores come from here; our write path has zero LLM. Design to
copy is **mem0's v3 additive extraction** — one call, not the old two-call
ADD/UPDATE/DELETE (which they measured and deleted):

- One call sees: top-10 semantically similar existing memories + last-10 raw
  messages (coreference) + recently extracted texts → emits self-contained
  15–80-word facts + `linked_memory_ids`.
- **Observation-Date vs Current-Date trick** (steal verbatim): relative
  dates resolve against when the conversation happened, not today.
  *"'User went to Paris last week' is useless 6 months later. 'User went to
  Paris the week of May 15, 2023' is meaningful forever."*
- Dedup: MD5/exact-hash vs top-10 existing AND within-batch (semantic dedup
  delegated to the prompt, not a similarity threshold).
- Anti-hallucination plumbing: map UUIDs→sequential ints before feeding
  existing memories to the LLM; `json_object` response format + code-block
  strip + brace-scan fallback; re-raise LLM errors (don't return [] — the
  caller must distinguish "LLM down" from "no facts").
- Session ring buffer: `messages(session_scope, role, content, created_at)`
  table evicted to last-10 per scope — enables pronoun resolution.
- Gate it like supermemory's `taskType`: `index-only` (cheap, deterministic,
  current behavior) vs `extract` (LLM). Default off locally; remote-model
  path already exists via `BaseModelClient`.
- Keep mem0's boundary: **the LLM never mutates existing state** — lifecycle
  ops are explicit API calls only. Our retract/expire/modify model is
  already stricter and better; extraction should only ADD.

Prompt sources to port: `mem0/configs/prompts.py` (`ADDITIVE_EXTRACTION_PROMPT`,
~470 lines), `graphiti_core/prompts/extract_nodes_and_edges.py` ("the original
conversation will NOT be available at retrieval time — only what you extract
survives"), `extract_edges.py` rules (SCREAMING_SNAKE_CASE relations, "NEVER
generalize 'Gamecube' to 'gaming console'").

### P1 — Fact store: versioned, bi-temporal, provenance-stamped

New SQLite table (facts live in SQLite, topology stays in Kuzu):

```sql
facts(id, text, text_hash, embedding_id→vec, 
      valid_at, invalid_at, expired_at, reference_time, created_at,
      status(active|superseded|forgotten|inferred),
      version, parent_id, root_id, forget_reason,
      entity_ids→json, source_ids→json, confidence)
```

Copy graphiti's timestamps verbatim — `invalid_at` = when the world changed,
`expired_at` = when we learned it — and their contradiction arithmetic:
skip if either edge already invalid before the other's `valid_at`;
invalidate candidate only if `candidate.valid_at < new.valid_at`; a
contradicted candidate *newer* than the incoming fact births the new fact
already superseded (handles out-of-order ingest). **Unlike graphiti, apply
the temporal predicate at query time by default** — `invalid_at IS NULL AND
expired_at IS NULL` — with an `as_of` opt-in. That's their gap; make it our
feature (it already is our lifecycle claim).

Provenance: every fact carries `source_ids` (cognee's `source_ref` pattern)
— this powers surgical delete, per-run rollback, and our citations feature.

### P2 — Entity side-index + boost (cheap version of graph memory)

Do mem0's version first, not graphiti's: `entities(name, embedding)` +
`fact_entities(entity_id, fact_id)` tables. At query time: extract ≤8 query
entities (heuristic/spaCy-style, not LLM — mem0 converged on this),
entity-search at similarity ≥0.5, boost linked facts by
`sim * 0.5 * (1/(1 + 0.001*(n_linked-1)²))`. No Cypher needed — two SQLite
tables. Kuzu stays for `[[wikilink]]` topology and provenance; later, if
typed relations prove out, add the `RelatesToNode_` fact-node pattern
(graphiti's Kuzu workaround) with fact UUIDs as join keys into SQLite.

### P3 — Lifecycle API polish

- `forget-matching` with `dryRun` + id-bound apply + `maxForget` cap +
  `forgetBatchId` (supermemory's pattern — copy the semantics verbatim).
- `PATCH`-style update → new version, old gets `isLatest=false`; never
  in-place mutation.
- `isInference` flag + review queue: inferred facts down-weighted until
  approved. This is a complete, cheap answer to "LLM extraction is noisy" —
  and a differentiator no local product has.
- Soft-delete granularity: deleting a source document hard-deletes the doc
  but *soft-forgets* derived facts.

### P4 — Retrieval tuning (small diffs, real points)

- **Semantic gate before fusion** (mem0): vector score threshold gates
  candidates; FTS/entity signals *reorder* but can't inject weak candidates.
  Currently our RRF lets either leg expand recall — measure both ways on the
  harness.
- **Sigmoid-normalized BM25**: query-length-adaptive sigmoid over raw FTS5
  rank before fusion (mem0's `scoring.py`).
- **Over-fetch `max(k*4, 60)`** then fuse (we do `limit*3` per leg — close;
  formalize).
- **Multi-view embeddings** (cognee): add a *retrieval-shaped summary* lane
  per chunk ("This chunk is about: <categories> / Facts: <standalone
  facts>") — embed it alongside chunk text. Their BEAM config was literally
  chunk-20 + entity-20 lanes.
- Expose `threshold`, `rerank`, `rewriteQuery` (multi-query expansion),
  `include:{forgotten,sessions,chunks}` and return `timing`/`total` on
  `POST /search` — supermemory's minimal search contract.
- Graph leg: make BFS a *ranked* contributor seeded from lexical/vector hits
  (graphiti seeds BFS from bm25/cosine hits), not only post-fusion appends.
- Standard RRF k=60 is fine; graphiti's `rank_const=1` over-weights rank-1 —
  don't copy that.

### P5 — Profile/context endpoint + middleware

The single hottest call shape: `POST /context {query?, scope}` →
`{static_facts[], dynamic_recent[], search_results[]}` in one round trip,
designed to be spliced into a system prompt every turn (~50ms target).
supermemory marks entries `[Recent]`/`[YYYY-MM-DD]`/`[Summary]`. Then ship a
thin middleware: retrieve → inject → generate → write-back under stable
`customId` — the pattern that makes memory "automatic" (their
`withSupermemory`, mem0's chat-completions proxy). Our Claude Code hooks
already prove the loop; package it for non-Claude clients.

### P6 — Async ingest + status machine

`add` returns `{id, status:"queued"}` immediately; statuses
`queued→extracting→embedding→indexing→done|failed` (+ separate
`extractionStatus` so "searchable" ≠ "memories extracted"); a `/processing`
queue endpoint. Keep ingest off the request path; **search must never block
on ingest** (supermemory's self-hosted guarantee). Sequential ingestion per
scope — graphiti's one-worker-per-group_id queue exists because edge
resolution races if episodes interleave; our fact resolution will have the
same invariant.

### P7 — MCP/DX polish

- Output schemas + `structuredContent` on every tool; annotations
  (`readOnlyHint`, `destructiveHint`, `idempotentHint`).
- Similarity-labeled markdown results (`- [87%] text`) alongside structured
  data.
- Error text with remediation hints (cognee's `_tool_error_text`).
- Deliberately pinned tool surface; hide advanced tools behind a
  `search_tools`/`call_tool` discovery pair (cognee's measured k=10 choice).
- `remember(background=True)` + status polling so ingestion outruns MCP
  deadlines.
- Validation bounds even local-first: scope/tag regex, metadata flat-values,
  content caps, filter depth limits.
- Active-scope state (supermemory's SpaceState) — a local SQLite row works.

### P8 — Ops completeness

- Backup must include `vectors.db` + Kuzu graph (today: vault+main DB only).
- Request-log table powering `/stats` (method/path/status/latency per call);
  opt-in telemetry only — local-first shouldn't phone home.
- `explain=true` score breakdowns for relevance debugging.
- Per-scope keys can wait; multi-tenant `containerTag`-style scoping is the
  real feature when we go there (cognee isolates engines per user+dataset —
  hard error, no silent fallback).
- Fix version skew (0.3.1 vs 0.4.0) and the `Agent.chat` boundary above.

---

## 3. Harness cross-references (feeds doc 1)

From the two eval-harness repos:

- **mem0 memory-benchmarks**: resume = one `{qid}.json` written per
  question, glob+skip on restart (~30 lines); ingestion checkpoints per
  chunk (`_progress_*.json`); `--predict-only`/`--evaluate-only`/`--rejudge`
  split; multi-cutoff = retrieve 200 once, judge at `[:10][:20][:50][:200]`;
  `GracefulShutdown` SIGINT/SIGINT flag; per-question JSON carries
  `retrieval.search_results` for re-judging. Their **retrieval-sufficiency
  judge** (`RETRIEVAL_JUDGE_PROMPT`, `run.py:103-148`) judges "do the
  memories suffice?" without an answerer — direct fit for our retrieval-only
  harness.
- **memorybench**: checkpoint = per-question phase state machine
  (`questions[qid].phases.{ingest,indexing,search,answer,evaluate}`), atomic
  tmp→rename writes, `dataSourceRunId` keyed container tags so re-judging
  doesn't re-ingest; 4 judge prompt variants (default / abstention /
  temporal off-by-one / knowledge-update / preference-rubric); MemScore
  triple; concurrency 5-level priority + batched fail-fast. **Bug not to
  copy**: their `recallAtK` is degenerate (clamped binary hit rate) — use
  real gold-evidence recall.

## 4. What NOT to build

- 25 vector-store backends (mem0) or 19 search types (cognee) — ship
  SQLite+vec+Kuzu done well.
- LLM triplet graph extraction as the primary path (mem0 abandoned it;
  cognee's graph didn't fix multi-hop anyway).
- mem0's notices/upsell machinery, PostHog defaults-on telemetry.
- graphiti's 47-method driver abstraction, `property_filters`/`episode_metadata`
  dead fields, their `rank_const=1` RRF.
- Per-cutoff LLM re-answering at benchmark time (expensive; our deterministic
  metrics make multi-k free).

## 5. Suggested sequence

1. Benchmark P0s from `EXTERNAL_RESEARCH_FINDINGS.md` (checkpointing first —
   it de-risks everything else).
2. P0 extraction layer behind a flag; P1 fact store + bi-temporal columns.
3. P2 entity side-index + boost; P4 retrieval tuning — each validated by the
   per-type LongMemEval breakdown (knowledge-update and multi-session are
   where the wins will show).
4. P3 lifecycle API polish + P5 context endpoint + P6 async ingest.
5. P7/P8 DX and ops.
6. MemoryBench provider adapter (P3.1 in the other doc) whenever we want a
   market-legible headline number.
