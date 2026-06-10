# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
