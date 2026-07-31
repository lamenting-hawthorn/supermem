# SuperMem Current-State and Proof Handoff

## Document control

| Field | Value |
|---|---|
| Status | BM-0 reviewer VETO remediated in cycle 1; acceptance remains pending independent rerun |
| Assessment date | 2026-07-31 |
| Repository | `https://github.com/lamenting-hawthorn/supermem.git` |
| Audited branch | `main` |
| Audited commit | `eb1f3f6becde220ad346eb873b3bdd37d692dde4` |
| Product-code changes made by assessment | None |
| Readiness verdict | Benchmark: **REJECT**; remote production: **REJECT** |
| Controlling scope | Current-state claims and proof boundaries at the audited commit |

This handoff supersedes `docs/audit-2026-06-10.md` where the two conflict. The
older audit remains useful historical context, but it does not describe the exact
2026-07-31 checkout.

## BM-0 candidate progress (local proof only)

Done:

- [x] Frozen BM0-01 through BM0-12 Markdown fixture corpus, authoritative oracle, and fresh-temp
      harness with redacted receipts.
- [x] Narrow local `memory://` source-event/revision ledger, canonical memory record,
      authoritative SQLite FTS5 query, citation verification, and deterministic
      replay proof.
- [x] Central private stripping before existing SQLite observation/summary
      persistence and vault indexing; nested, unclosed, and stray-close blocks
      fail closed.
- [x] Source updates, deletion, retraction, live expiry, temporal validity, and
      FTS rebuild behavior are covered by focused local tests.
- [x] Compression no longer deletes the only raw observation authority.

Left / blocked:

- [ ] Independent reviewer rerun and veto decision.

The candidate is **not BM-0 accepted** and makes no installed-product, remote,
vector, graph, provider, staging, or production claim. The detailed receipt is
`docs/handoffs/04-bm0-implementation-receipt.md`.

## Handoff set

1. `00-current-state-and-proof.md` — frozen findings, receipts, limitations, and
   present maturity.
2. `01-bm0-acceptance-contract.md` — the next implementation slice and
   non-negotiable benchmark gates.
3. `02-target-architecture-and-reuse.md` — target boundaries and selective reuse
   from the three sibling projects.
4. `03-security-roadmap-and-release-gates.md` — P0 security directions,
   30/60/90 sequence, and claim/release controls.

## Executive state

SuperMem is a functional local developer prototype with working SQLite FTS
retrieval, sessions, observations, basic retraction, Markdown ingestion, MCP
stdio support, structured logs, and a broad automated test suite.

It is not yet a trustworthy benchmark candidate because vector ingestion,
source revision semantics, live expiry, compression recall, citations, and
reproducible evaluation are incomplete. It is not safe for untrusted remote or
multi-user deployment because packaged legacy HTTP surfaces bypass the primary
guard, remote identity is a shared optional token, retrieved content is not an
authority-safe boundary, and the restricted Python executor is not OS
containment.

## Maturity snapshot

| Area | Rating | Evidence-bounded interpretation |
|---|---:|---|
| Local single-user use | 3/5 | Useful developer prototype |
| Retrieval and lifecycle correctness | 1.5/5 | Important metadata exists but is not authoritative |
| Benchmark readiness | 1/5 | No frozen harness or public baseline |
| Remote/MCP production security | 1/5 | Local stdio only is the safe default |
| Observability and operations | 1/5 | Structured logs, no end-to-end telemetry proof |
| Overall | 2/5 | Local beta shape; benchmark and remote-production pre-alpha |

## Verified working capabilities

| Capability | Proof layer | Boundary |
|---|---|---|
| SQLite FTS5 write and keyword retrieval | Unit/local integration | Does not prove semantic recall |
| Sessions, observations, and active/retracted state | Unit/local integration | Does not prove full deletion |
| Markdown vault walking and wikilink extraction | Unit/static | Real long-running watcher not proved |
| Optional Kuzu graph implementation | Unit/static | Provider/runtime performance not proved |
| Primary FastMCP tools share a guard | Static/unit | Legacy packaged HTTP modules bypass it |
| Structured logging and correlation IDs | Static/unit | No OTel exporter or production backend proof |
| Python package and Docker build | Exact-head hosted CI | No deployment or installed-product E2E |

## Exact validation receipts

The assessment used an isolated temporary environment and did not install a
repository-local virtual environment.

```text
uv sync --frozen
Result: 93 locked packages installed

pytest tests/ --cov=supermem --cov=agent --cov-report=term-missing --cov-fail-under=60
Result: 250 passed, 2 skipped, 1 warning
Coverage: 66.89%
Elapsed: 14.67 seconds

ruff 0.15.12
Result: passed

mypy 1.20.2
Result: passed

black --check .
Result: 100 files would be left unchanged
```

Exact-head hosted receipt:

- GitHub Actions run:
  `https://github.com/lamenting-hawthorn/supermem/actions/runs/29630878923`
- Commit: `eb1f3f6becde220ad346eb873b3bdd37d692dde4`
- Jobs: lint/type/test, Docker build, and package build succeeded.
- Hosted environment: Python 3.11.

These results prove the checked-in tests and build at that commit. They do not
prove populated vector retrieval, real connectors, installed MCP behavior,
performance, adversarial safety, staging, or production.

## Confirmed blockers

### P0: benchmark-invalidating correctness

1. **Vector ingestion is absent.**
   `VectorStore.upsert_chunks()` exists, but no production path calls it.
   Tier 3 must not be described as working semantic retrieval until a populated
   vector-store test passes.

2. **Compression causes recall loss.**
   Compression writes a summary to the non-FTS `summaries` table and deletes the
   retrievable source observations. Retrieval never queries the replacement
   summary.

3. **Expiry is startup-only.**
   `expires_at` is purged during database initialization. Live retrieval checks
   `status = 'active'` but not the current time, allowing a running process to
   return expired observations.

4. **Source updates retain stale content.**
   A modified file adds a new `entity_content` observation without superseding
   the prior source revision. File deletion is not handled by the watcher.

5. **Citations are absent.**
   Retrieval results expose observation IDs and content but not stable source
   revision, span, digest, or citation verification.

### P0: security and authority

1. Packaged legacy HTTP/MCP/SSE modules allow unauthenticated requests, use
   permissive CORS, bind to all interfaces, and may log complete payloads.
2. The restricted Python executor can be bypassed through native modules and is
   not a hostile-code sandbox.
3. Retrieved/imported content is not consistently treated as untrusted data.
4. There is no trusted actor/scope model or tenant isolation.
5. Static bearer authentication is disabled when the key is unset and does not
   implement OAuth issuer, audience, or scope validation.

### P1: release and operational evidence

1. No committed benchmark manifest, oracle, runner, or raw result receipts.
2. README tier order and approximate latency numbers are not reproducibly
   measured and conflict with concurrent FTS/vector code.
3. No OpenTelemetry traces or metrics for ingestion, retrieval, lifecycle,
   model calls, or deletion.
4. A 2026-07-31 `pip-audit` produced 60 advisory records across 15 installed
   packages. Reachability was not established; every record requires triage.
5. Package version remains `0.3.1` while `main` contains unreleased changes.

## Current benchmark baseline

There is no public SuperMem accuracy or latency baseline.

The only honest baseline at this commit is:

| Property | Current result |
|---|---|
| Checked-in test suite | 250 passed, 2 skipped |
| Total coverage | 66.89% |
| Public memory benchmark | Not run |
| Populated vector retrieval | Not implemented/proved |
| Source revision replacement | Fails; stale content remains |
| Live TTL exclusion | Fails without restart |
| Compression preservation | Fails; source recall is lost |
| Citation accuracy | Not measurable; citations absent |
| Remote production readiness | Rejected |

## Claim policy for future work

- Never publish benchmark numbers without a frozen dataset, harness commit,
  environment, model/prompt, retrieval budget, and preserved case outputs.
- Never use passing unit tests to claim installed product, provider, staging, or
  production behavior.
- Never call the Python executor a secure sandbox without an OS/container
  boundary.
- Never claim deletion from non-retrievability alone; distinguish logical
  retraction, projection cleanup, backup retention, and physical erasure.
- Vendor scores, including Maximem's published scores, are comparison inputs,
  not SuperMem evidence.

## Evidence update protocol

Future work must append a receipt to the relevant handoff containing:

1. date, branch, commit, and dirty state;
2. exact command and environment;
3. result and artifact location;
4. proof layer;
5. limitations and unresolved risks;
6. acceptance criterion advanced or blocked.

Do not silently replace this baseline. If a new assessment supersedes it, add a
dated successor and link both documents.
