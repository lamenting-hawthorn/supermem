# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

supermem — persistent AI memory without RAG. The supported MCP memory surface
uses lifecycle-aware SQLite FTS, graph, and optional vector retrieval (tiers
1–3). Raw-vault Agent navigation (Tier 4) is disabled pending a source-aware
lifecycle broker, so direct `Agent.chat` fails closed before model, tool, or
executor use. The retained restricted executor is not a hostile-code sandbox
and is not a supported MCP memory path.

## Build & Run Commands

All commands use `uv` (not pip) and are orchestrated via `Makefile`.

```bash
# Install
make install              # deps + LM Studio (macOS)
make install-api          # deps only (API mode / CI)

# Setup
make setup-cli            # choose memory directory (CLI)
make setup                # choose memory directory (GUI)

# Run
make chat-cli             # interactive terminal REPL
make run-agent            # start local model server (MLX on macOS, vLLM on Linux)
make serve-mcp            # MCP server (stdio, for Claude Desktop)
make serve-mcp-http       # Loopback MCP HTTP; SUPERMEM_API_KEY required
make generate-mcp-json    # generate mcp.json config for Claude Desktop

# Quick start
make quickstart           # API mode (OpenRouter, no GPU)
make quickstart-local     # local model (Apple Silicon / CUDA GPU)

# Data import
make memory-wizard        # interactive import wizard
make connect-memory CONNECTOR=chatgpt SOURCE=/path/to/export.zip

# Privacy
make add-filters          # add privacy filter rules
make reset-filters        # clear filters
```

### Testing

```bash
uv run pytest tests/ -v                                    # run all tests
uv run pytest tests/test_engine.py -v                      # run single test file
uv run pytest tests/ --cov=agent --cov-report=term-missing # with coverage
```

CI runs pytest + black formatting checks (`.github/workflows/ci.yml`).

## Architecture

### Workspace Structure

uv workspace monorepo with four packages:

| Package | Path | Purpose |
|---------|------|---------|
| `agent` | `agent/` | Disabled raw-vault Agent compatibility shell and restricted local executor |
| `mcp-server` | `mcp_server/` | FastMCP wrapper exposing lifecycle-aware memory tools over stdio + HTTP |
| `supermem-core` | `supermem/` | v2 layer — hybrid retrieval, graph/vector/SQLite storage, session tracking, Worker HTTP API |
| `memory_connectors` | `memory_connectors/` | Plugin system for importing data (ChatGPT, Notion, Nuclino, GitHub, Google Docs) |

### Data Flow

```
AI Client (Claude Desktop / ChatGPT)
  │
  ├─ trusted local stdio ── HybridRetriever tiers 1-3
  └─ authenticated HTTP ─── HybridRetriever tiers 1-3
  ▼
mcp_server/server.py — FastMCP, exposes lifecycle-aware retrieval tools
  │
  ▼
supermem/ — HybridRetriever (FTS5 → graph → vector, 3 tiers)
  ├── supermem/storage/database.py  — SQLite via aiosqlite
  ├── supermem/storage/graph.py     — Kuzu graph DB
  ├── supermem/storage/vector.py    — Chroma vector store
  └── supermem/indexer/vault.py     — walks vault, populates stores

Tier 4 raw-vault Agent navigation is unavailable until it can enforce source
lifecycle policy. `agent/agent.py`, `agent/tools.py`, and `agent/engine.py`
remain compatibility/internal surfaces; no supported MCP or Worker path invokes
them for memory retrieval.
```

### supermem CLI (entry point)

The `supermem` package installs a CLI (`uv run supermem` or just `supermem` after install):

```bash
supermem serve               # Start MCP server (stdio)
supermem serve --worker      # MCP server + Worker HTTP API on :37777
supermem chat                # Interactive terminal REPL
supermem backup              # Archive vault + SQLite → timestamped .tar.gz
supermem restore <file>      # Restore from archive
supermem connect chatgpt ~/Downloads/export.zip
supermem connect github owner/repo --token ghp_xxx
```

### Worker HTTP API (`:37777`)

Optional service started via `supermem serve --worker`. Provides:

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Liveness + DB/graph/vector readiness |
| `GET /sessions` | Recent sessions with summaries |
| `GET /observations` | Active observations only, filterable by type/session |
| `POST /search` | Hybrid search (FTS5 → graph → vector; all requests cap at Tier 3) |
| `POST /index/rebuild` | Re-index entire vault |
| `GET /backup` | Stream tar.gz backup |
| `GET /stats` | Memory metrics |
| `GET /` | Static session viewer UI (`worker/static/index.html`) |

Auth: protected endpoints require `Authorization: Bearer <SUPERMEM_API_KEY>`;
they fail closed when the key is unset. All supported retrieval is capped at
Tier 3. Primary MCP HTTP is a separate authenticated loopback-only, stateless
profile: it does not issue or resume MCP transport session IDs.

### Key Design Decisions

- **Agent boundary**: raw-vault Agent navigation is disabled until a
  source-aware lifecycle broker exists. Do not re-enable it for stdio, HTTP, or
  Worker paths without that broker and fresh security review.
- **Restricted executor**: a retained internal utility with an explicit tool
  allowlist and denied platform raw-I/O imports; it is not hostile-code
  isolation and is not reachable from the supported memory surface.
- **Size limits**: 1MB per file, 10MB per directory, 100MB total memory — enforced in `agent/tools.py`
- **Agent settings**: `agent/settings.py` — MAX_TOOL_TURNS=20, executor timeout=20s, LLM backend URLs
- **System prompt**: `agent/system_prompt.txt` — behavioral spec, available APIs, file naming conventions

### Key Files

| File | Role |
|------|------|
| `agent/agent.py` | Tier-4 compatibility shell; `chat` fails closed |
| `agent/engine.py` | Restricted subprocess executor (not hostile-code isolation) |
| `agent/tools.py` | Raw-vault content and metadata routes denied pending lifecycle broker |
| `agent/model.py` | Legacy LLM client factory; not invoked by disabled Agent navigation |
| `agent/schemas.py` | Pydantic models (ChatMessage, AgentResponse, etc.) |
| `agent/settings.py` | All constants and backend config |
| `agent/system_prompt.txt` | Agent behavioral instructions |
| `mcp_server/server.py` | FastMCP server entry point |
| `chat_cli.py` | Rich terminal REPL |
| `memory_connectors/base.py` | BaseMemoryConnector abstract class |

## Environment

- **Python**: 3.11 (exact, enforced in pyproject.toml)
- **Config files**: `.memory_path` (memory dir), `.mlx_model_name` (model), `.filters` (privacy rules)
- **Env vars**: see `.env.example` — OPENROUTER_API_KEY, VLLM_HOST/PORT, LOG_LEVEL, MCP_TRANSPORT; v2 adds SUPERMEM_VAULT_PATH, SUPERMEM_DB_PATH, SUPERMEM_WORKER_PORT (default 37777), SUPERMEM_API_KEY
- **Remotes**: `origin` = fork (`lamenting-hawthorn/supermem`), `upstream` = `firstbatchxyz/mem-agent-mcp`
- **Docker**: `docker-compose.yml` + `Dockerfile` available for containerized deployment

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
