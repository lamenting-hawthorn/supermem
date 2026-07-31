# BM-0 Acceptance Contract: Source Revision to Cited Retrieval

## Document control

| Field | Value |
|---|---|
| Status | Frozen implementation acceptance criteria |
| Baseline commit | `eb1f3f6becde220ad346eb873b3bdd37d692dde4` |
| Milestone | BM-0 |
| Primary objective | Make SuperMem trustworthy enough to establish a benchmark baseline |
| Product surface | Local, single-actor, SQLite FTS retrieval |
| Decision authority | These gates may not be weakened by the implementation owner |

## User-visible acceptance path

Given a local Markdown source:

1. SuperMem ingests an immutable source revision.
2. It creates or updates an active memory record with an exact citation.
3. A deterministic FTS query returns only the currently valid revision.
4. Modifying, deleting, retracting, or expiring the source makes the old content
   immediately ineligible across every enabled projection.
5. The returned result explains where it came from and preserves enough
   evidence to verify the citation.
6. A frozen harness can repeat the flow from a fresh temporary directory and
   produce the same normalized outcome.

## In scope

- Local Markdown source ingestion.
- SQLite source-event, source-revision, and canonical memory records.
- FTS5 projection and deterministic retrieval.
- Exact source URI/path, span, revision, and digest citations.
- Live effective-time, expiry, lifecycle, and sensitivity filtering.
- Revision, supersession, source deletion, and logical retraction.
- Centralized nested-private stripping before persistence.
- Deterministic fixtures, oracle, runner, metrics, and run receipts.
- Read-only local MCP result shape if needed to prove the same public retrieval
  path.

## Explicit non-goals

- Vector or graph quality improvements.
- LLM answer generation or Tier 4 execution.
- Remote HTTP, SSE, OAuth, hosted deployment, or multi-tenancy.
- Live connector/provider validation.
- UI/dashboard work.
- Physical deletion guarantees across backups.
- Headline competitor comparisons.

These non-goals prevent a benchmark substrate from becoming a broad platform
rewrite.

## Required contracts

### SourceRevisionV1

Minimum required fields:

```text
source_id
revision
source_uri
content_digest
captured_at
source_span
previous_revision
lifecycle_state
```

Source revisions are immutable. A change creates a new revision; it does not
overwrite the evidence used by an existing citation.

### MemoryRecordV1

Minimum required fields:

```text
memory_id
revision
kind
content
source_revision_ref
source_span
observed_at
effective_from
effective_until
expires_at
confidence
trust_level
sensitivity
lifecycle_state
supersedes_revision
record_digest
```

BM-0 may use a deliberately narrow subset of the Governed Agent Harness
contract. It must not import the Harness runtime or PostgreSQL persistence.

### RetrievalQueryV1

Minimum required fields:

```text
query_id
query
scope
temporal_bound
max_records
timeout_ms
correlation_id
```

### CitedRetrievalResultV1

Every returned item must include:

```text
memory_id
memory_revision
content
source_uri
source_revision
source_span
source_digest
retrieval_tier
retrieval_score
latency_ms
```

The result must not expose private source content outside the selected public
span.

## Deterministic fixture matrix

| ID | Scenario | Must include | Must exclude | Severity |
|---|---|---|---|---|
| BM0-01 | Exact positive retrieval | Current cited fact | Unrelated facts | High |
| BM0-02 | Unknown query | Nothing | Every stored fact | High |
| BM0-03 | Source modified | New revision | Old revision | Critical |
| BM0-04 | Source deleted | Nothing from source | All deleted-source content | Critical |
| BM0-05 | Logical retraction | Nothing retracted | Retracted content and derivatives | Critical |
| BM0-06 | Live expiry | Nothing after deadline | Expired content without restart | Critical |
| BM0-07 | Effective interval | Fact valid at query time | Out-of-bound facts | High |
| BM0-08 | Nested private block | Public surrounding text | Entire nested private region | Critical |
| BM0-09 | Summary/compression | Current retrievable representation | Silent recall loss | Critical |
| BM0-10 | Citation verification | Matching URI/span/digest | Wrong or stale source digest | Critical |
| BM0-11 | Deterministic replay | Same normalized IDs/order | Unexplained run variance | High |
| BM0-12 | Injection-shaped memory | Data-only retrieval | Permission/policy changes | Critical |

Paraphrase recall should be measured as a baseline but is not an FTS-only
quality gate. It becomes a candidate-improvement gate when real vector
ingestion is introduced.

## Mandatory acceptance gates

BM-0 is accepted only when all conditions are true:

- [ ] All critical fixture cases pass from a fresh temporary directory.
- [ ] Stale-fact, expired-fact, retracted-fact, deleted-source, and private-data
      retrieval rates are exactly zero.
- [ ] Every positive result has a verifiable citation URI, revision, span, and
      digest.
- [ ] Source modification returns only the current eligible revision.
- [ ] Source deletion/retraction clears every enabled projection atomically or
      fails closed.
- [ ] Expiry is enforced during retrieval without process restart.
- [ ] Compression does not destroy the only retrievable authority.
- [ ] Unknown FTS queries remain empty and do not escalate into code execution.
- [ ] The vector tier is reported disabled until a real ingestion path exists.
- [ ] Two clean runs have identical normalized case outcomes and result order.
- [ ] Raw and summarized reports include all failures, timeouts, unsupported,
      and inconclusive cases.
- [ ] The existing test suite, Ruff, Black, and mypy still pass.
- [ ] An independent reviewer reruns the frozen harness and can veto acceptance.

## Harness layout

Recommended repository shape:

```text
benchmarks/
  README.md
  manifest.json
  environment.schema.json
  datasets/
    bm0-local/
      dataset.jsonl
      expected-results.json
      sources/
  adapters/
    no_memory.py
    raw_history.py
    supermem_fts.py
  runner.py
  oracle.py
  scoring.py
  reporting.py
  tests/
```

Generated outputs must live outside committed fixtures:

```text
artifacts/<run-id>/
  environment.json
  configuration.json
  cases.jsonl
  metrics.json
  report.md
```

## Frozen run identity

Every run records:

- dataset and expected-results digests;
- harness and SuperMem commit;
- dependency lock digest;
- Python and operating-system versions;
- model/provider/prompt identity, if a later mode uses a model;
- retrieval configuration and budget;
- seed, hardware, cache state, and concurrency;
- start/end times and normalized run digest.

## Metrics

### Deterministic retrieval

- recall@k and precision@k;
- MRR or nDCG where ranking matters;
- stale, prohibited, expired, and retracted recall rate;
- unknown-answer contamination;
- citation coverage and citation verification rate;
- ingestion-to-retrieval delay;
- p50/p95/p99 latency;
- error, timeout, and variance rates;
- storage growth.

### Later end-to-end answer evaluation

Keep answer generation separate from retrieval correctness:

- answer accuracy by category;
- abstention precision/recall;
- temporal and contradiction correctness;
- model input/output tokens and cost;
- retrieved-context tokens;
- fixed blinded semantic judge only where exact assertions are insufficient.

An LLM judge may not override an exact citation, scope, deletion, expiry, or
private-canary failure.

## Public benchmark sequence

1. Pass BM-0 local fixtures.
2. Run all cleaned LongMemEval v1 questions in no-memory, raw-history/RAG, and
   SuperMem FTS modes.
3. Add LoCoMo only as a secondary conversational regression.
4. Add SuperMem hybrid mode only after vector/graph projections have real write,
   update, deletion, and populated-store tests.
5. Run LongMemEval-V2 small only after the earlier gates are reproducible.

No minimum public accuracy score is frozen before the first honest baseline.
The first run establishes the comparison; it must not be tuned away or deleted.

## Implementation order

1. Freeze fixtures, oracle, and result contracts.
2. Add source revision and citation contracts.
3. Make current-revision, temporal, expiry, and lifecycle predicates
   authoritative in one retrieval query path.
4. Centralize privacy filtering before persistence.
5. Make update/delete/retract projection cleanup atomic and idempotent.
6. Repair or disable compression.
7. Expose cited results through the local public retrieval boundary.
8. Run the frozen harness twice from clean temporary state.
9. Run the broader regression suite.
10. Obtain independent review before declaring BM-0 accepted.

## Receipt template

Append one block per candidate:

```text
Candidate commit:
Dirty state:
Environment:
Commands:
Focused result:
Full regression result:
Harness run IDs:
Artifacts:
Acceptance gates passed:
Acceptance gates blocked:
Reviewer verdict:
Proof boundary:
```

