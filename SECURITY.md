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
- **Restore containment:** archive restore validates paths before writing into
  the vault and rejects absolute or traversal paths.
- **MCP auth/rate guard:** primary MCP tools share the same auth guard and one
  per-client rate bucket.
- **Restricted executor hardening:** generated Python runs in a subprocess with
  a scrubbed environment, denied import roots, path-aware `open()` checks, and
  wrappers for common lower-level `os` filesystem APIs.

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
support.

## Recommended production roadmap

1. Replace or wrap the restricted executor with container/OS isolation for
   untrusted remote use.
2. Add full OAuth 2.1/PKCE support for remote MCP transports, including issuer,
   JWKS, audience, and scope validation.
3. Expand lifecycle workflows with explicit supersede/forget APIs, deletion
   verification, and retention-policy reporting.
4. Add encrypted backup support and documented restore verification.
5. Add OpenTelemetry spans for MCP tool calls, retrieval tiers, model calls,
   compression, memory writes, retractions, and failures.
6. Build a reproducible memory-quality benchmark suite covering temporal
   updates, contradictory facts, retractions, abstention, prompt injection, and
   citation accuracy.
