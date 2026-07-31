# Security, Roadmap, and Release-Gate Handoff

## Document control

| Field | Value |
|---|---|
| Status | Frozen risk priorities and staged direction |
| Baseline commit | `eb1f3f6becde220ad346eb873b3bdd37d692dde4` |
| Current safe deployment profile | Trusted local user, MCP stdio |
| Current remote-production verdict | REJECT |
| Current benchmark verdict | REJECT until BM-0 passes |

## Security model

Treat these inputs as hostile:

- Markdown and imported conversations;
- connector payloads and metadata;
- MCP tool descriptions, parameters, and return values;
- model-generated propositions, summaries, code, and tool calls;
- archive contents and file paths;
- remote identity and scope assertions supplied by clients.

Only application-controlled code may establish actor identity, authorize scope,
change lifecycle state, or approve an effect.

## Immediate P0 directions

### Remote surfaces

- Disable or remove legacy `mcp_server/http_server.py`,
  `mcp_server/mcp_http_server.py`, and legacy SSE exposure from supported
  product paths.
- Bind local HTTP only to `127.0.0.1`.
- Validate `Origin` and reject invalid origins.
- Make authentication fail closed for any remote mode.
- Stop logging raw request, response, prompt, memory, token, and tool payloads.
- Do not advertise remote production support until the remote gate below
  passes.

### Executor

- Disable Tier 4 Python execution in benchmark and remote profiles.
- Rename remaining "sandbox" claims to "restricted executor" until an
  OS/container boundary exists.
- If retained for trusted local use, make it explicit opt-in with no inherited
  secrets and a narrowly selected working directory.
- For untrusted execution, require a non-root isolated runtime, no network by
  default, read-only filesystem, explicit scratch directory, scrubbed
  environment, process/CPU/memory/time limits, and syscall restrictions.
- Retrieved content may never enable installs, expand paths, or authorize
  execution.

### Memory poisoning and authority

- Mark imported and retrieved content as data, never instructions.
- Keep it outside system/developer instruction channels.
- Prohibit retrieved text from changing tool permissions, actor scope,
  lifecycle policy, or write approval.
- Add malicious stored-instruction fixtures to BM-0 and later Write → Execute →
  Forget tests.
- Model-derived memory begins as candidate/quarantined unless an explicit local
  policy permits deterministic activation.

### Dependencies and supply chain

- Triage every 2026-07-31 `pip-audit` advisory for reachability and fixed
  version.
- Refresh and commit the lock only with regression proof.
- Pin CI tools and third-party actions deliberately; avoid mutable `latest`
  where release evidence depends on it.
- Generate an SBOM and vulnerability receipt for release candidates.
- Do not expose credentials in logs, benchmark artifacts, or telemetry.

## Remote MCP acceptance gate

Remote support remains rejected until all items pass through the real HTTP
surface:

- [ ] A single current Streamable HTTP endpoint replaces legacy wrappers.
- [ ] Local binding defaults to loopback.
- [ ] `Origin` is validated and invalid origins receive HTTP 403.
- [ ] Every protected request fails closed without identity.
- [ ] Protected Resource Metadata names authorization servers and supported
      scopes.
- [ ] OAuth flow validates issuer, signature/JWKS, expiry, audience/resource,
      and scopes.
- [ ] Tokens are never accepted for another resource or passed through to
      downstream systems.
- [ ] Read, write, promote, retract, and delete use separate least-privilege
      scopes.
- [ ] Actor and memory scope are derived from verified identity, not request
      JSON.
- [ ] Cross-user/session/project access and forged scope tests fail closed.
- [ ] Rate limits are identity-bound and tested under concurrency.
- [ ] Logs redact tokens, private content, prompts, tool arguments, and PII.
- [ ] Installed-wheel and container E2E tests exercise authorization and
      shutdown behavior.
- [ ] Independent security review has direct access to the candidate and may
      veto release.

Static API-key tests do not satisfy this gate.

## Deletion and retention gate

- [ ] Preview identifies canonical records, FTS rows, vector chunks, graph
      edges, summaries, caches, and backup obligations.
- [ ] Confirmed deletion/retraction is idempotent.
- [ ] Content becomes immediately non-retrievable from every enabled
      projection.
- [ ] Receipt retains only authorized non-content audit material.
- [ ] Backup retention and delayed physical erasure are disclosed separately.
- [ ] Rebuild cannot resurrect deleted or superseded content.
- [ ] A private canary stays absent after restart, projection rebuild, and
      restore within the claimed deletion boundary.

## Observability gate

- [ ] Correlation ID spans ingest through retrieval and result selection.
- [ ] Per-tier candidate/result counts and latency are recorded.
- [ ] Lifecycle transitions name state, actor/process, reason class, and record
      digest.
- [ ] Model/provider/token/cost fields are recorded when a model is used.
- [ ] Content capture is off by default.
- [ ] Redaction tests cover secrets, private blocks, authorization headers, and
      memory content.
- [ ] Telemetry backend failure does not fail the user query and increments an
      observable failure counter.
- [ ] Internal event schema is versioned and mapped to the current
      OpenTelemetry GenAI/MCP conventions.

## 30/60/90 direction

### Days 0–30: benchmark truth and local safety

Deliver:

- BM-0 immutable source revision to cited FTS retrieval;
- live expiry and effective-time filtering;
- update/supersession, source deletion, and retraction parity;
- centralized nested-private filtering;
- compression disabled or made provenance-preserving and retrievable;
- legacy remote and Tier 4 execution disabled in supported benchmark/product
  profiles;
- dependency advisory triage and refreshed lock;
- frozen local harness and first full cleaned LongMemEval v1 baseline;
- README corrections based only on measured behavior.

Exit gate:

- BM-0 acceptance contract passes twice from clean state;
- full regression suite passes;
- independent reviewer accepts the receipts;
- no remote or vector claim is introduced.

### Days 31–60: measured retrieval improvement

Deliver:

- vector ingestion with embedding-space version, rebuild, revision, retraction,
  deletion, and populated-store tests;
- baseline-versus-candidate evaluation under the same corpus, model, token, and
  latency budgets;
- optional graph enrichment only for categories with measured benefit;
- provenance-preserving summary/compaction validation;
- prompt-injection, malicious connector, backup/restore, concurrency,
  cold/warm-cache, installed-wheel, and Docker tests;
- redacted internal telemetry and OpenTelemetry mapping.

Exit gate:

- hybrid mode has no lifecycle parity failures;
- every quality improvement is paired with latency, token/cost, storage, and
  false-memory counter-metrics;
- FTS remains available as a deterministic fallback;
- benchmark artifacts include regressions and failures.

### Days 61–90: bounded productization

Deliver:

- trusted ActorContext and scoped query/write contracts;
- candidate → active promotion and quarantine/review;
- deletion preview and confirmed receipt;
- secure remote MCP as a separate vertical slice;
- PostgreSQL/RLS backend only if multi-user hosting is an explicit product goal;
- LongMemEval-V2 small and memory-poisoning evaluation;
- installed-product operational runbook and rollback plan.

Exit gate:

- remote MCP gate passes against the installed/container artifact;
- database-enforced tenant isolation is proved with a real database if hosted
  multi-user support is claimed;
- no critical advisory remains untriaged;
- independent production-readiness review accepts, limits, or vetoes release.

## Claim ladder

Use only the highest claim supported by direct evidence:

```text
static inspection
  < unit
  < local integration/simulator
  < installed product
  < real provider/database/device
  < staging
  < production
```

Examples:

- A SQLite fixture does not prove PostgreSQL RLS.
- A TestClient call does not prove deployed HTTP behavior.
- CI Docker build does not prove a running container's auth or shutdown.
- LongMemEval accuracy does not prove prompt-injection resistance.
- Logical retraction does not prove physical backup erasure.

## Release decision record

Every release candidate records:

```text
Candidate commit and artifact digest:
Target deployment profile:
Accepted claims:
Explicitly unsupported claims:
BM-0 receipt:
Public benchmark receipt:
Dependency/SBOM receipt:
Installed-product receipt:
Remote MCP receipt:
Deletion receipt:
Observability receipt:
Independent reviewer:
Reviewer verdict:
Known residual risks:
Rollback owner and procedure:
Human release authority:
```

No push, tag, package publication, deployment, or maturity-claim change is
authorized by this handoff alone.

