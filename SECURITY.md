# Security posture and production-readiness roadmap

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
- **Executor boundary:** the current Python executor is a restricted local
  subprocess, not a hostile-code sandbox. It now scrubs inherited environment
  variables, blocks common process/network imports by default, and uses
  path-aware containment checks, but production deployments should still run any
  agent code execution inside an OS/container boundary with a scrubbed
  environment, no network by default, a read-only filesystem, and CPU/memory
  limits.
- **Memory lifecycle:** observations now have provenance/lifecycle columns such
  as source, observed/valid time, confidence, trust level, sensitivity, and
  status. Follow-up work should migrate retrieval and UI surfaces toward these
  fields.
- **Evaluation and observability:** production claims should be backed by a
  reproducible memory benchmark suite and OpenTelemetry-compatible traces for
  retrieval tiers, MCP tools, model calls, memory writes, and failures.

## Recommended implementation phases

1. Replace or disable the restricted Python executor for untrusted remote use.
2. Add RFC 9728 protected-resource metadata and OAuth 2.1/PKCE integration for
   remote MCP transports.
3. Build explicit forget/retract/supersede workflows around memory status and
   validity intervals.
4. Add retrieval evaluation fixtures covering temporal updates, contradictions,
   retractions, abstention, and prompt-injection documents.
5. Add OpenTelemetry spans for MCP tool execution, retrieval tiers, model calls,
   compression, and memory writes.
