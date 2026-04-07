# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

supermem — persistent AI memory without RAG. An MCP server that lets AI assistants (Claude Desktop, ChatGPT, LM Studio) navigate a local markdown knowledge base using a fine-tuned agent model, not vector search. The agent reads/writes memory files via sandboxed Python execution with Obsidian-style `[[wikilinks]]` for multi-hop reasoning.

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
make serve-mcp-http       # MCP server (HTTP, for ChatGPT)
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
| `agent` | `agent/` | Core agent logic — conversation loop, sandboxed execution, file tools, LLM clients |
| `mcp-server` | `mcp_server/` | FastMCP wrapper exposing agent as MCP tool, stdio + HTTP transports |
| `supermem-core` | `supermem/` | v2 layer — hybrid retrieval, graph/vector/SQLite storage, session tracking, Worker HTTP API |
| `memory_connectors` | `memory_connectors/` | Plugin system for importing data (ChatGPT, Notion, Nuclino, GitHub, Google Docs) |

### Data Flow

```
AI Client (Claude Desktop / ChatGPT)
  │ MCP (stdio or HTTP)
  ▼
mcp_server/server.py — FastMCP, exposes "use_memory_agent" tool
  │
  ▼
agent/agent.py — conversation loop, up to 20 tool turns
  │
  ├── agent/model.py — OpenAI SDK client (OpenRouter, vLLM, or LM Studio)
  ├── agent/engine.py — subprocess sandbox (path-restricted, builtins blacklisted, 20s timeout)
  └── agent/tools.py — file/dir operations on memory vault
        │
        ▼
  Memory Vault (local markdown files with [[wikilinks]])
        │
        ▼ (v2 indexing — when Worker is running)
  supermem/ — HybridRetriever (FTS5 → graph → vector → agent, 4 tiers)
  ├── supermem/storage/database.py  — SQLite via aiosqlite
  ├── supermem/storage/graph.py     — Kuzu graph DB
  ├── supermem/storage/vector.py    — Chroma vector store
  └── supermem/indexer/vault.py     — walks vault, populates stores
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
| `GET /observations` | Paginated observations, filterable by type |
| `POST /search` | Hybrid search (FTS5 → graph → vector → agent) |
| `POST /index/rebuild` | Re-index entire vault |
| `GET /backup` | Stream tar.gz backup |
| `GET /stats` | Memory metrics |
| `GET /` | Static session viewer UI (`worker/static/index.html`) |

Auth: `Authorization: Bearer <SUPERMEM_API_KEY>` header; disabled when env var unset.

### Key Design Decisions

- **XML response format**: Agent responses use strict `<think>`, `<python>`, `<reply>` tags — parsed in `agent/utils.py`
- **Sandbox isolation**: Code runs in a subprocess with `builtins.open`, `os.remove`, `os.rename` patched to restrict to memory path; dangerous builtins (exec, eval, `__import__`) are blacklisted
- **Size limits**: 1MB per file, 10MB per directory, 100MB total memory — enforced in `agent/tools.py`
- **Agent settings**: `agent/settings.py` — MAX_TOOL_TURNS=20, sandbox timeout=20s, LLM backend URLs
- **System prompt**: `agent/system_prompt.txt` — behavioral spec, available APIs, file naming conventions

### Key Files

| File | Role |
|------|------|
| `agent/agent.py` | Agent class — conversation management, tool-turn loop |
| `agent/engine.py` | Sandboxed subprocess executor |
| `agent/tools.py` | Memory file/dir CRUD operations |
| `agent/model.py` | LLM client factory (OpenRouter, vLLM) |
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
