# Target Architecture and Cross-Project Reuse Handoff

## Document control

| Field | Value |
|---|---|
| Status | Approved target direction; not an implementation claim |
| Baseline commit | `eb1f3f6becde220ad346eb873b3bdd37d692dde4` |
| Architecture principle | Local-first evidence authority with optional derived projections |
| Primary reuse sources | Governed Agent Harness, SkillLoop, Governing Agent Architecture |

## Product direction

The product should not compete on having four storage technologies. It should
compete on being the local, inspectable memory layer that can prove:

- where a memory came from;
- which source revision supports it;
- when it was observed and effective;
- why and under which scope it was retrieved;
- whether it was corrected, disputed, expired, retracted, or deleted;
- how much retrieval cost and latency it consumed.

Local-first Markdown remains useful. FTS, vectors, and graph are projections of
canonical evidence, not independent authorities.

## Target architecture

```text
Hostile inputs
  vault | conversations | connectors | MCP
             |
             v
Ingress boundary
  parse | normalize | privacy | source identity | scope | trust
             |
             v
Immutable SourceEvent / SourceRevision ledger
             |
             v
Candidate extraction
  raw source span + optional typed proposition
             |
             v
Lifecycle authority
  approve | revise | supersede | dispute | expire | retract | delete
             |
             v
Canonical MemoryRecordV1
             |
      +------+------+
      |             |
      v             v
FTS projection   Optional vector/graph projections
      |             |
      +------+------+
             |
             v
Trusted query boundary
  ActorContext | scope | temporal bound | result/latency budget
             |
             v
Authority filter + deterministic assembly
             |
             v
Cited retrieval result
  source URI | span | revision | digest | tier | score | latency
```

Two read-only side planes consume this path:

- benchmark/evaluation export with frozen digests and case receipts;
- redacted observability for ingest, retrieval, lifecycle, model, and error
  events.

## Architectural invariants

1. Raw source evidence is immutable; updates create revisions.
2. Canonical memory lifecycle is authoritative; indexes are disposable
   projections.
3. No projection may return a record that the canonical lifecycle query would
   reject.
4. Scope, temporal validity, expiry, sensitivity, and lifecycle are applied
   before results leave storage.
5. Every returned memory has a stable citation and digest.
6. Retrieved content is untrusted data and cannot authorize tools, writes,
   policy changes, or scope expansion.
7. Model-generated extraction begins as a candidate, not trusted active memory.
8. FTS is the deterministic default. Vector and graph are enabled only after
   measured benefit and full lifecycle parity.
9. Tier 4 code execution is not part of the trusted retrieval core.
10. Benchmark and telemetry consumers are read-only; they do not mutate runtime
    memory.
11. Local stdio and hosted remote access are separate trust profiles.
12. No hosted multi-user claim exists without a trusted identity boundary and
    database-enforced tenant isolation.

## Component directions

### Ingress and source ledger

- Assign stable source IDs independent of file paths where possible.
- Store immutable revision metadata and a content digest.
- Centralize private-block removal and sensitivity labelling before any
  persistence or derived indexing.
- Preserve the raw authority or a verifiable authorized reference; summaries
  never become the only silent authority.

### Lifecycle authority

Use explicit states:

```text
candidate
active
superseded
disputed
quarantined
expired
deleted
```

Transitions must name actor/process, reason, prior revision, evidence, time, and
idempotency binding. BM-0 may activate deterministic source-derived records
directly; model-derived records should later require review or policy approval.

### Retrieval

- Start with one FTS path that applies all authority predicates.
- Accept explicit query scope, temporal bound, maximum records, and timeout.
- Return scored cited records, not only observation IDs.
- Record candidate count, rejected count by reason, selected citations, and
  latency.
- Add vector retrieval only after embedding version, ingestion, update,
  retraction, deletion, and rebuild semantics exist.
- Add graph enrichment only when a named benchmark category shows improvement
  over FTS/vector under the same token and latency budget.

### Compression and context assembly

- Treat summaries as derived records with provenance back to source revisions.
- Keep raw evidence available under retention policy.
- Validate compaction against frozen must-include/must-exclude facts.
- Bound context by tokens and relevance, not by silent destructive deletion.

### MCP and product boundaries

- Default product surface: local stdio, read-only retrieval first.
- Writes, promotion, retraction, and deletion are distinct tools with clear
  confirmation semantics.
- Remote access is an optional gateway, not a flag on local assumptions.
- Result schemas mark retrieved text as data and retain source/trust labels.

### Observability

Adopt a stable internal event vocabulary before binding to evolving external
conventions:

```text
memory.ingest
memory.candidate
memory.promote
memory.revise
memory.retract
memory.delete
memory.query
memory.projection.search
memory.result.select
memory.model.call
memory.error
```

Capture IDs/digests, counts, latency, model/token use, and error class. Prompt,
memory, and tool content remains off by default and redacted when explicitly
enabled.

## Reuse map

### SkillLoop: adapt now

Approved sources:

- `skillloop/benchmark.py`: baseline-versus-candidate replay, per-case deltas,
  regression counts, and JSON report structure.
- `skillloop/provenance.py`: callable/component and artifact hashing.
- `skillloop/dataset_readiness.py`: dataset checks, hashes, split readiness,
  warnings, and stats.
- `skillloop/review/queue.py`: later candidate review pattern with hash and path
  validation.

Reuse mode:

- Copy/adapt the small stdlib-oriented primitives with Apache-2.0 attribution.
- Replace SkillLoop traces/rubrics with retrieval fixtures and memory metrics.
- Keep SkillLoop an offline evaluation/review plane.

Do not:

- import the whole SkillLoop runtime;
- expose `training_ready_signal` as a memory-quality result;
- let SkillLoop write directly into active runtime memory.

### Governed Agent Harness: extract contracts and behavior

Approved sources:

- `contracts/v1/memory_record.schema.json`
- `contracts/v1/memory_query.schema.json`
- `contracts/v1/memory_proposal.schema.json`
- `contracts/v1/memory_decision.schema.json`
- `contracts/v1/evaluation_run.schema.json`
- `contracts/v1/evidence_envelope.schema.json`
- `contracts/v1/deletion_receipt.schema.json`
- retrieval tests proving temporal, tombstone, revision, and scope behavior;
- promotion tests proving revise, supersede, idempotency, and live expiry.

Reuse mode:

- Extract a deliberately narrowed and versioned local contract.
- Port behavioral acceptance cases before porting implementation.
- Map hosted tenant and UUID assumptions to the smallest local actor/scope shape
  needed by SuperMem.
- Retain Apache-2.0 notices for copied schema or test bytes.

Do not:

- add the Harness as a runtime dependency;
- copy its PostgreSQL migrations into SQLite;
- import its policy, evidence-chain, and authority stack before SuperMem has the
  corresponding trust boundary;
- claim Harness PostgreSQL proof applies to SuperMem.

### Governing Agent Architecture: reference and adapt small primitives

Approved sources:

- `src/guardrails.py`: fail-closed consecutive tool failure budget and
  deterministic context-compaction preservation rules.
- `src/tracing.py`: small trace-event shape and best-effort telemetry principle.
- `ARCHITECTURE.md`: raw event → canonical typed memory → retrieval/workflow
  separation and read-only SkillLoop export.

Reuse mode:

- Adapt small pure-Python patterns with MIT attribution.
- Use its PostgreSQL/RLS design as a future hosted reference.

Do not:

- copy its durable store, graph store, or full `init_schema.sql` into SuperMem;
- treat its dirty/behind inspected branch as a stable byte source without
  pinning a clean commit;
- swallow telemetry failures without an observable counter or warning budget.

## Reuse sequencing

| Milestone | Reuse |
|---|---|
| BM-0 | SkillLoop replay/provenance; narrowed Harness record/query/run contracts and lifecycle tests |
| BM-1 | SkillLoop dataset readiness and report comparisons |
| Lifecycle governance | SkillLoop review queue plus narrowed Harness proposal/decision states |
| Deletion | Harness preview/confirmed deletion receipt shape |
| Local agent hardening | Governing Agent Architecture failure budget and compaction rules |
| Hosted multi-user | Harness ActorContext/scope concepts and Governing Agent Architecture RLS reference, implemented as a separate hosted slice |

## Coupling vetoes

- No new runtime dependency on a sibling project.
- No wholesale database or migration copy.
- No graph/vector dependency added merely to match competitor architecture.
- No authority or policy contract without an enforcement point and tests.
- No copied implementation without source commit, license, attribution, and a
  SuperMem-owned regression test.

## Reuse receipt template

Every reused component records:

```text
Source project:
Source commit:
Source file and lines:
License:
Reuse mode: copied | adapted | contract-only | behavior-only
SuperMem owner:
SuperMem destination:
Modifications:
Acceptance tests:
Known coupling:
```
