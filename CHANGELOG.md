# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Observation provenance/lifecycle metadata, retraction audit storage, and active/retracted filtering across retrieval, timelines, and recent-session context.
- Local insight tools for open-task extraction, follow-up suggestions, and day summaries, exposed through MCP and the worker HTTP API.
- Worker endpoints for protected-resource metadata, local insights, and observation retraction.
- Security posture documentation covering current safeguards, limitations, and production hardening roadmap.

### Changed
- Hybrid retrieval now filters candidate IDs through lifecycle status before returning results.
- MCP rate limiting is keyed by client identity across tools rather than per tool.
- CI release jobs validate Docker/package builds on pull requests while pushing/publishing only on version tags.
- The agent executor is documented and hardened as a restricted local executor rather than a hostile-code sandbox.

### Fixed
- Archive restore rejects absolute and traversal paths before writing into the vault.
- Retraction invalidates derived session summaries and stores reasons outside FTS to avoid re-indexing sensitive values.
- Restricted executor wrappers cover common lower-level filesystem APIs reachable through import-hook globals.

## [0.3.1] - 2026-06-10

### Fixed
- Dependency cleanup: removed heavy vLLM/CUDA requirements from core dependencies; moved to `[local]` extra for local model users
- Docker image: explicit tag `ghcr.io/lamenting-hawthorn/supermem:0.3` for clarity
- CI: lightweight test pipeline, no GPU dependencies

## [0.3.0] - 2026-06-10

### Added
- Published to PyPI: `pip install supermem` now works
- Docker: `ghcr.io/lamenting-hawthorn/supermem:0.3` with multi-stage build, sub-200MB base image
- CI health checks: `pytest` coverage gate at 60%, ruff + black + mypy on every PR
- New MCP tool `supermem_hybrid`: token-efficient, returns observation IDs first, latency metadata second
- Progressive disclosure pattern documented for MCP tool usage

### Changed
- README: restructured Quick Start with pip as primary, Docker as production alternative
- Download badge: 140+ pulls from GHCR now displayed in README
- Four-tier retrieval short-circuit rule clarified in all docs

## [0.2.0] - 2026-04-07

### Added
- **Ambient memory injection** via Claude Code hook system — automatically surfaces relevant observations during user prompts without requiring explicit memory queries
- **TTL-based observation expiry** — configure retention via `SUPERMEM_OBS_TTL_DAYS` (default: 90 days); automatic cleanup on startup and via worker API
- **Parallel retrieval tiers** — hybrid retrieval (FTS5 → graph → vector → agent) now runs in parallel for sub-100ms queries
- **New hooks system** (`supermem/hooks/`) — `inject.py` reads user prompts from stdin, searches local SQLite, injects observations as system context
- **ServerContext extraction** — refactored MCP server to isolate initialization and startup failure handling
- **Comprehensive test coverage** — 522 new lines of MCP server tests, database tests, vault indexer tests; all observation expiry paths covered

### Changed
- **Rate limiter** — split per-user and per-endpoint limits for better control and clearer semantics
- **Vault indexer** — mtime-skip optimization to reduce redundant indexing; `get_entity_last_indexed` now cached
- **Startup robustness** — clear separation of startup failures (auth, db, config) vs. runtime errors

### Fixed
- Black formatting alignment across all Python files

## [0.1.0] - 2025-02-25

### Added
- Initial release: supermem v2 — four-tier retrieval architecture (FTS5 → graph → vector → agent)
- MCP server (stdio + HTTP transports) exposing agent as memory tool
- Hybrid storage: SQLite (FTS5), Kuzu (graph), Chroma (vector)
- Session tracking and observation recording
- Memory import connectors (ChatGPT, Notion, Nuclino, GitHub, Google Docs)
- `supermem` CLI with serve, chat, backup, restore, connect commands
- Worker HTTP API (`:37777`) for search, indexing, health checks
