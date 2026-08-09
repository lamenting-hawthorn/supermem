# Security posture and production-readiness roadmap

supermem is a **local-first memory system**. The default deployment target is a
trusted developer or personal workstation using MCP stdio. HTTP/worker mode is
useful for dashboards and automation, but should be treated as a private service
unless additional production controls are added.

## Current safeguards

- **Local-first storage:** memory is stored in a local Markdown vault and SQLite
  database; optional Kuzu/Chroma indexes can be rebuilt.
- **Private block stripping:** content wrapped in `<private>...</private>` is
  stripped before indexing or persistence.
- **Observation lifecycle:** memories carry provenance, confidence, sensitivity,
  validity, TTL, and `active`/`retracted` status metadata.
- **Retraction:** retracted observations are removed from FTS, filtered from
  retrieval/timelines/recent-session context, and derived summaries for the
  source session are invalidated. Retraction reasons are stored outside FTS.
- **Lifecycle-aware retrieval boundary:** every supported MCP and Worker
  retrieval request is capped at tiers 1–3 (FTS, graph, vector), whose results
  are filtered through observation lifecycle state.
- **Raw Agent boundary:** Tier 4 Agent vault navigation, raw content reads, and
  raw metadata probes are unavailable pending a source-aware lifecycle broker.
  `Agent.chat` fails closed before it can invoke a model, tool, or executor.
- **Restore containment:** archive restore validates paths before writing into
  the vault and rejects absolute or traversal paths.
- **Connector archive bounds:** Notion/Nuclino imports retain the 1 MiB parsed
  Markdown/CSV ceiling while allowing attachments only within the 100 MiB
  per-member and total-extraction ceilings; count, type, path, ratio, and
  central-directory checks remain fail closed.
- **MCP auth/rate guard:** primary MCP tools share the same auth guard and one
  per-client rate bucket.
- **Bounded local HTTP profile:** primary MCP HTTP binds only to loopback,
  authenticates before protocol handling, and is stateless, so initialize
  requests do not create retained MCP transport sessions. Worker observation
  listing and session observation counts return active rows only.
- **Retired transport boundary:** legacy HTTP/SSE adapters return their disabled
  response without reading or parsing request bodies.
- **Restricted executor hardening:** the retained internal utility uses an
  explicit tool allowlist, scrubbed environment, denied import roots,
  path-aware `open()` checks, wrappers for common lower-level `os` filesystem
  APIs, and denies direct `posix`/`nt` raw-I/O imports. It is not a supported
  MCP or Worker memory path.

## Important limitations

The restricted Python executor is **not a hostile-code sandbox**. It reduces
common local escape paths, but production deployments that execute untrusted code
should use an OS/container boundary with:

- no network by default;
- non-root user;
- read-only filesystem except an explicit work directory;
- scrubbed environment and secrets isolation;
- CPU, memory, process, and wall-clock limits;
- seccomp/AppArmor or equivalent syscall restrictions.

HTTP mode currently supports static `SUPERMEM_API_KEY` Bearer authentication. It
publishes protected-resource metadata for discovery, but it does **not** yet
validate OAuth issuers, JWKS, audiences, or scopes. Do not expose HTTP mode as a
multi-tenant internet service without an external auth gateway or future OAuth
support. The supported local HTTP profile is deliberately stateless; it does not
support resumable MCP transport sessions or long-lived session state.
The local Worker dashboard accepts the static bearer through a password field
and keeps it only in page memory. Imported memory content is escaped before UI
rendering so it cannot become an HTML credential-reading path. This remains a
local single-user convenience boundary, not a remote identity system.

Tier 4/raw-vault Agent navigation is intentionally unavailable, including for
trusted local stdio, because legacy vault files cannot reliably map to active,
retracted, or deleted observations. Do not treat the retained Agent or executor
code as a supported local-memory fallback. Re-enabling it requires a
source-to-observation lifecycle broker, regression proof for retraction and
deletion, and fresh security review.

## Recommended production roadmap

1. Build a source-aware lifecycle broker before any Tier 4/raw-vault Agent
   memory navigation is re-enabled.
2. Replace or wrap the restricted executor with container/OS isolation for
   untrusted remote use.
3. Add full OAuth 2.1/PKCE support for remote MCP transports, including issuer,
   JWKS, audience, and scope validation.
4. Expand lifecycle workflows with explicit supersede/forget APIs, deletion
   verification, and retention-policy reporting.
5. Add encrypted backup support and documented restore verification.
6. Add OpenTelemetry spans for MCP tool calls, retrieval tiers, model calls,
   compression, memory writes, retractions, and failures.
7. Build a reproducible memory-quality benchmark suite covering temporal
   updates, contradictory facts, retractions, abstention, prompt injection, and
   citation accuracy.
supermem is local-first memory software. The default deployment target is a
trusted local machine using MCP stdio. Remote HTTP deployments need additional
controls before being exposed to untrusted networks.

## Verified gaps and near-term priorities

- **MCP authorization:** the MCP authorization specification requires protected
  HTTP resources to publish OAuth 2.0 Protected Resource Metadata (RFC 9728) and
  use an external authorization server for OAuth-style flows. supermem currently
  supports bearer-token authentication, not a full OAuth 2.1/PKCE flow.
- **MCP tool consistency:** every MCP tool should pass through the same auth and
  rate-limit path. This is now enforced in the primary FastMCP server helpers.
- **Archive restore safety:** backup restore must validate archive member paths
  before writing into the vault. Restore now rejects absolute paths and `..`
  traversal that would escape the configured vault.
- **Executor boundary:** the current Python executor is a restricted internal
  utility, not a hostile-code sandbox. It is not reachable from a supported
  memory-retrieval route. Production deployments should still run any future
  untrusted code execution inside an OS/container boundary with a scrubbed
  environment, no network by default, a read-only filesystem, and CPU/memory
  limits.
- **Agent lifecycle boundary:** Tier 4/raw-vault Agent navigation is disabled
  until a source-to-observation broker can enforce active/retracted/deleted
  state. All supported retrieval is capped at Tier 3.
- **Memory lifecycle:** observations now have provenance/lifecycle columns such
  as source, observed/valid time, confidence, trust level, sensitivity, and
  status. Follow-up work should migrate retrieval and UI surfaces toward these
  fields.
- **Evaluation and observability:** production claims should be backed by a
  reproducible memory benchmark suite and OpenTelemetry-compatible traces for
  retrieval tiers, MCP tools, model calls, memory writes, and failures.

## Recommended implementation phases

1. Build the source-aware lifecycle broker before re-enabling Tier 4/Agent
   memory navigation.
2. Replace or disable the restricted Python executor for untrusted remote use.
3. Add RFC 9728 protected-resource metadata and OAuth 2.1/PKCE integration for
   remote MCP transports.
4. Build explicit forget/retract/supersede workflows around memory status and
   validity intervals.
5. Add retrieval evaluation fixtures covering temporal updates, contradictions,
   retractions, abstention, and prompt-injection documents.
6. Add OpenTelemetry spans for MCP tool execution, retrieval tiers, model calls,
   compression, and memory writes.
