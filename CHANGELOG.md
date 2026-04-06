# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-04-07

### Added
- **Ambient memory injection** via Claude Code hook system — automatically surfaces relevant observations during user prompts without requiring explicit memory queries
- **TTL-based observation expiry** — configure retention via `RECALL_OBS_TTL_DAYS` (default: 90 days); automatic cleanup on startup and via worker API
- **Parallel retrieval tiers** — hybrid retrieval (FTS5 → graph → vector → agent) now runs in parallel for sub-100ms queries
- **New hooks system** (`recall/hooks/`) — `inject.py` reads user prompts from stdin, searches local SQLite, injects observations as system context
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
- Initial release: Recall v2 — four-tier retrieval architecture (FTS5 → graph → vector → agent)
- MCP server (stdio + HTTP transports) exposing agent as memory tool
- Hybrid storage: SQLite (FTS5), Kuzu (graph), Chroma (vector)
- Session tracking and observation recording
- Memory import connectors (ChatGPT, Notion, Nuclino, GitHub, Google Docs)
- `recall` CLI with serve, chat, backup, restore, connect commands
- Worker HTTP API (`:37777`) for search, indexing, health checks
