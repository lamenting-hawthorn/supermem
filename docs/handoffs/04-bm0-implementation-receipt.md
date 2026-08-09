# BM-0 implementation candidate receipt

## Status

Two reviewer **VETOs** were remediated within the two-cycle limit. The
independent reviewer reran the frozen candidate and returned **PASS**. BM-0 is
**accepted only for the local SQLite/FTS benchmark layer**. Proof is bounded to
static, unit, and local SQLite integration/simulator at baseline HEAD
`eb1f3f6becde220ad346eb873b3bdd37d692dde4` plus candidate-byte digest
`d8449d19fbac1253cbcf2acfc1138c8e930e2aeebccfdb3a08b33aa13b290858`.

## Reviewer VETO remediation cycle 1

Done:

- [x] Citation verification now compares memory ID/revision, source revision,
      URI, span, digest, content, FTS tier, finite score/latency, and current
      canonical lifecycle/scope/sensitivity/temporal eligibility.
- [x] Stray private closers now fail closed at filter, database, vault-indexer,
      and local-cited persistence boundaries; nested and unclosed blocks remain
      fail closed.
- [x] `timeout_ms` now controls a SQLite progress-handler deadline and has a
      deterministic injected-clock timeout test.
- [x] The harness converts per-case timeout, unsupported, inconclusive, and
      generic failures to redacted receipts and continues remaining cases.
- [x] `expected-results.json` is the validated authoritative oracle; dataset
      rows contain only ID/scenario/query and ID parity/duplicates are rejected.
- [x] Receipt metrics record baseline HEAD plus a deterministic manifest and
      candidate-byte digest over BM-0 runtime/benchmark/test files.
- [x] Immutable append-only source-event ledger records ingest/revise/supersede/
      retract/delete transitions and is covered by regression tests.
- [x] Harness now copies frozen Markdown source files to a fresh vault and uses
      contained `ingest_file`; traversal and symlink escapes are rejected.

Left:

- [x] Independent reviewer rerun and acceptance decision.

## Reviewer VETO remediation cycle 2 (final)

Done:

- [x] SQLite abort triggers now prohibit every direct update or deletion of an
      immutable source revision. Tests cover all evidence-critical fields and
      prove revision bytes/digests remain unchanged across revise, retract, and
      source deletion.
- [x] Citation verification now cross-checks canonical source-revision content
      and its digest against canonical memory content, as well as lifecycle
      eligibility. The earlier true-after-source-content-mutation behavior was
      caused by checking memory content but not revision content; source trigger
      enforcement and the added equality/digest checks close that route.
- [x] The oracle `status` field is consumed by validation: BM-0 accepts only
      frozen `passed` expectations and rejects non-passed statuses instead of
      silently reporting success.
- [x] Candidate byte manifest was computed only after final formatting; both
      independent fresh-run receipts contain the same candidate and normalized
      outcome digests.

Left:

- [x] Independent reviewer rerun and acceptance decision.

## Done

- `SourceRevisionV1`, `MemoryRecordV1`, `RetrievalQueryV1`, and
  `CitedRetrievalResultV1` are implemented in
  `supermem/local_cited_memory.py`.
- The only supported BM-0 public retrieval boundary is
  `LocalCitedMemory.retrieve(RetrievalQueryV1)`. It uses one FTS query joined to
  canonical source/revision/memory authority predicates; it has no graph,
  vector, model, or Tier 4 fallback.
- The local fixture adapter reads only contained vault-relative Markdown through
  `ingest_file`, canonicalizes it to `memory://vault/<relative-path>`, and
  rejects traversal plus symlink escapes. Arbitrary `file://` ingestion remains
  unsupported.
- Citation spans are `chars:<start>:<exclusive-end>` Unicode code-point ranges
  in the sanitized source revision. Citation verification checks URI, revision,
  span, digest, and returned content against immutable stored evidence.
- Vector is explicitly `disabled: no BM-0 ingestion path`; enabled projections
  are canonical SQLite and its SQLite FTS5 projection only.
- Privacy filtering is centralized before SQLite observation/summary writes and
  vault indexing. Nested or unclosed `<private>` blocks remove the entire
  private suffix before persistence.
- Compression retains raw observations; summaries are derived context and are
  never the only retrievable authority.

## Frozen harness evidence

Command, run twice from fresh temporary SQLite state:

```bash
.venv/bin/python -m benchmarks.runner --output-root /private/tmp/supermem-bm0-artifacts
```

| Run artifact | Normalized digest | Result |
|---|---|---|
| `/private/tmp/supermem-bm0-cycle2-a/bm0-20260731T010531304715Z-ed4faf36af00` | `ed4faf36af0092bcc0697d8c70391f98b9d5350c855d8cc7cdfbb1081998f289` | 12 passed, 0 failed |
| `/private/tmp/supermem-bm0-cycle2-b/bm0-20260731T010531445386Z-ed4faf36af00` | `ed4faf36af0092bcc0697d8c70391f98b9d5350c855d8cc7cdfbb1081998f289` | 12 passed, 0 failed |

Each artifact contains environment/configuration, redacted raw case receipts,
metrics, and a summary report. All report `0` failures, timeouts, unsupported,
inconclusive, and prohibited results. The harness records fixture, expected,
harness, lock, baseline-head, and candidate-byte manifest/digest plus the local
Python/platform details.

## Acceptance matrix

| Fixture | Local result |
|---|---|
| BM0-01 exact positive | Pass |
| BM0-02 unknown empty | Pass |
| BM0-03 update/current-only | Pass |
| BM0-04 source deletion | Pass |
| BM0-05 logical retraction | Pass |
| BM0-06 live expiry | Pass |
| BM0-07 effective interval | Pass |
| BM0-08 nested/unclosed private | Pass |
| BM0-09 compression authority | Pass |
| BM0-10 citation and tamper rejection | Pass |
| BM0-11 deterministic replay | Pass |
| BM0-12 injection-shaped data only | Pass |

Mandatory-gate status:

- [x] Critical fresh-temp fixture cases and exact-zero prohibited categories.
- [x] Full cited-result/lifecycle citation verification; only current source
      revisions return.
- [x] Atomic/idempotent source update/delete/retraction cleanup across canonical
      SQLite and enabled FTS projection; rebuild does not resurrect ineligible
      records.
- [x] Live expiry, non-destructive compression, empty unknown FTS behavior, no
      Tier 4 fallback, vector disabled, deterministic replay, and complete
      receipt status fields.
- [x] Focused/adversarial checks: 13 local authority/oracle tests passed.
- [x] Full pytest: 268 passed, 2 skipped, 1 warning.
- [x] Canonical CI Ruff: `supermem/ agent/ mcp_server/` passes.
- [x] Canonical CI mypy equivalent: `supermem/ --ignore-missing-imports
      --follow-imports=skip --no-error-summary` passes.
- [x] Full Black: 105 files unchanged.
- [x] Independent reviewer rerun: PASS for candidate digest
      `d8449d19fbac1253cbcf2acfc1138c8e930e2aeebccfdb3a08b33aa13b290858`.

## Exact validation commands

```text
.venv/bin/python -m pytest tests/unit/test_database.py tests/unit/test_vault_indexer.py tests/unit/test_local_cited_memory.py tests/unit/test_bm0_runner.py tests/unit/test_privacy.py tests/unit/test_compressor.py tests/integration/test_full_query.py -q
Independent reviewer result: 80 passed

.venv/bin/python -m pytest tests/ -q
Result: 268 passed, 2 skipped, 1 warning in 10.28s

UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --offline ruff check supermem/ agent/ mcp_server/
Result: All checks passed

UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --offline mypy supermem/ --ignore-missing-imports --follow-imports=skip --no-error-summary
Result: no errors

UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --offline black --check .
Result: 105 files would be left unchanged
```

## Proof boundary and residual risk

This proves only contained local fixture-vault/symlink behavior; it does not
prove installed MCP behavior, remote security, OS sandboxing, vector/graph
lifecycle parity, provider/device behavior, physical backup deletion, staging,
or production. The benchmark README still contains one stale sentence describing
the now-implemented contained vault adapter as future work; changing it would
alter the independently approved candidate digest, so it is recorded as
non-blocking follow-up rather than silently changing the reviewed bytes.

## Final independent and integrator receipt

Independent reviewer verdict: **PASS**.

Integrator command, run twice from distinct fresh roots:

```text
.venv/bin/python -m benchmarks.runner --output-root /private/tmp/supermem-bm0-integrator-a-019fb594
.venv/bin/python -m benchmarks.runner --output-root /private/tmp/supermem-bm0-integrator-b-019fb594
```

Both runs produced byte-identical `cases.jsonl`, 12 passed, zero failed,
timeout, unsupported, inconclusive, or prohibited results, candidate digest
`d8449d19fbac1253cbcf2acfc1138c8e930e2aeebccfdb3a08b33aa13b290858`,
and normalized outcome digest
`ed4faf36af0092bcc0697d8c70391f98b9d5350c855d8cc7cdfbb1081998f289`.

Integrator regression and static evidence:

```text
.venv/bin/python -m pytest tests/ -q
Result: 268 passed, 2 skipped, 1 warning

UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --offline ruff check supermem/ agent/ mcp_server/
Result: All checks passed

UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --offline mypy supermem/ --ignore-missing-imports --follow-imports=skip --no-error-summary
Result: passed

UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --offline black --check .
Result: 105 files would be left unchanged

git diff --check
Result: passed
```
