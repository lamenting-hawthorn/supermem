# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

supermem is an MCP (Model Context Protocol) server that gives AI assistants — Claude Desktop, LM Studio, ChatGPT — persistent, structured memory backed by SQLite + an embedded graph database. The LLM agent is tier 4, not the default path — most queries resolve in milliseconds via full-text search.

**Key Concept**: Four-tier retrieval that short-circuits as soon as enough results are found. Tiers 1–3 never call an LLM. The LLM agent only activates when deterministic retrieval falls short.

## Essential Commands

### Initial Setup
```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone https://github.com/lamenting-hawthorn/supermem
cd supermem
uv sync

# Configure (required)
export SUPERMEM_VAULT_PATH=~/notes
export SUPERMEM_LLM_PROVIDER=openrouter
export OPENROUTER_API_KEY=your_key_here
```

### Running the Server
```bash
# Start MCP server (stdio transport, for Claude Desktop)
uv run supermem serve

# Start MCP server + HTTP dashboard
uv run supermem serve --worker

# Dashboard at http://localhost:37777
```

### Development Workflow
```bash
# Interactive terminal REPL (no client required)
uv run supermem chat

# Run all tests with coverage
uv run pytest tests/ -v --cov=supermem --cov-report=term-missing

# Unit tests only (fast, no network)
uv run pytest tests/unit/ -v

# Integration tests (real storage)
uv run pytest tests/integration/ -v
```

### Memory Connectors
```bash
# ChatGPT export (Settings → Data controls → Export data → .zip)
uv run supermem connect chatgpt ~/Downloads/chatgpt_export.zip

# Notion workspace export (.zip)
uv run supermem connect notion ~/Downloads/notion_export.zip

# Nuclino workspace export (.zip)
uv run supermem connect nuclino ~/Downloads/nuclino_export.zip

# GitHub repositories (live via API)
uv run supermem connect github owner/repo1,owner/repo2 --token ghp_xxx

# Google Docs (OAuth, opens browser)
uv run supermem connect google_docs "My Doc Name"
```

### Backup & Restore
```bash
# Create timestamped archive (vault + SQLite)
uv run supermem backup

# Custom output path
uv run supermem backup --output /path/to/archive.tar.gz

# Restore from archive
uv run supermem restore archive.tar.gz
```

## Architecture

### Four-Tier Retrieval

Every query goes through tiers in order, short-circuiting when enough results are found. Tiers 1–3 never call an LLM.

```
Query
  │
  ├─ Tier 1: SQLite FTS5 full-text search          ~1ms    always available
  │          porter tokenizer, WAL mode
  │
  ├─ Tier 2: Kuzu embedded graph expansion         ~5ms    optional (install kuzu)
  │          BFS traversal via [[wikilink]] edges
  │
  ├─ Tier 3: ChromaDB vector similarity            ~50ms   optional (SUPERMEM_VECTOR=true)
  │          sentence-transformer embeddings
  │
  └─ Tier 4: LLM agent fallback                   ~5-30s  always available
             navigates vault via Python sandbox
```

**Short-circuit rule**: If Tier 1 returns ≥ `min_results` (default 3), Tiers 2–4 are skipped entirely. Unavailable tiers are skipped with a WARNING log — no errors raised.

### Workspace Structure

This is a uv workspace with four packages:

| Package | Purpose |
|---------|---------|
| `supermem` (root) | Core library, CLI entry points, package metadata |
| `agent/` | LLM agent with sandboxed Python code execution |
| `mcp_server/` | FastMCP server exposing memory tools over stdio/HTTP |
| `memory_connectors/` | Plugin system for importing external data sources |

### Key Design Patterns

**MCP Tool Reference**:

| Tool | Parameters | Returns | Notes |
|------|-----------|---------|-------|
| `use_memory_agent` | `query: str` | Formatted answer | Backward-compatible. Routes through all 4 tiers |
| `supermem_hybrid` | `query: str`, `tier_limit: int = 4` | JSON with `obs_ids`, `source_tier`, `latency_ms` | Preferred for programmatic use |
| `get_observations` | `ids: list[int]` | JSON array of observation dicts | Fetch full content for specific IDs |
| `get_timeline` | `obs_id: int`, `window: int = 5` | JSON array of chronological observations | Context around a specific observation |

**Progressive Disclosure Pattern**:
```python
# 1. Search — cheap, returns IDs only
result = await supermem_hybrid("Alice's project status", tier_limit=2)
# Returns: {"obs_ids": [42, 17, 88], "source_tier": 1, "latency_ms": 2.1}

# 2. Fetch — only for IDs you actually need
obs = await get_observations([42, 17])

# 3. Timeline — context around interesting observations
ctx = await get_timeline(42, window=3)
```

**Privacy**: Wrap sensitive content in `<private>...</private>` tags. It is stripped before writing to any storage layer (SQLite, Kuzu, ChromaDB). The content passes through to the agent sandbox only — it never persists.

**Agent Response Format**: The agent uses structured tags:
```
<think>reasoning</think>
<python>code or empty</python>
<reply>user response (only if python is empty)</reply>
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SUPERMEM_LLM_PROVIDER` | `openrouter` | `openrouter` \| `ollama` \| `vllm` \| `claude` \| `lmstudio` |
| `SUPERMEM_LLM_MODEL` | provider default | Model string (e.g. `openai/gpt-4o-mini`, `llama3`) |
| `SUPERMEM_DB_PATH` | `~/.supermem/supermem.db` | SQLite database path |
| `SUPERMEM_VAULT_PATH` | `.memory_path` file | Markdown vault directory |
| `SUPERMEM_VECTOR` | `false` | Set `true` to enable ChromaDB tier |
| `SUPERMEM_API_KEY` | _(none)_ | Bearer token for HTTP API auth (disabled if unset) |
| `SUPERMEM_RATE_LIMIT` | `60` | Requests/minute limit |
| `SUPERMEM_WORKER_PORT` | `37777` | HTTP dashboard port |
| `SUPERMEM_COMPRESS_EVERY` | `50` | Observations written before LLM compression |
| `OPENROUTER_API_KEY` | _(required for openrouter)_ | OpenRouter API key |
| `ANTHROPIC_API_KEY` | _(required for claude)_ | Anthropic API key |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `VLLM_HOST` / `VLLM_PORT` | `localhost` / `8000` | vLLM server address |
| `LMSTUDIO_HOST` | `http://localhost:1234` | LM Studio server URL |

## Testing

```bash
# All tests
uv run pytest tests/ -v

# Unit tests only (fast, no network)
uv run pytest tests/unit/ -v

# Integration tests (real storage)
uv run pytest tests/integration/ -v

# With coverage (CI gate: 60%)
uv run pytest tests/ --cov=supermem --cov-report=term-missing
```

Kuzu and Anthropic tests are auto-skipped if packages are not installed.

## CI Pipeline

```bash
# Linting and formatting
uv run ruff check .
uv run black --check .
uv run mypy supermem/

# Tests with coverage gate (60%)
uv run pytest tests/ --cov=supermem --cov-report=term-missing
```

## Docker

```bash
# Clone and configure
git clone https://github.com/lamenting-hawthorn/supermem
cp .env.example .env
# Edit .env: set SUPERMEM_VAULT_PATH, SUPERMEM_LLM_PROVIDER, API keys

# MCP server only (stdio, for Claude Desktop)
docker compose up supermem-mcp

# MCP server + HTTP dashboard
docker compose --profile worker up

# Dashboard at http://localhost:37777
```

Image is distributed on GHCR (`ghcr.io/lamenting-hawthorn/supermem`).

## Claude Desktop Configuration

Add to `mcp.json`:
```json
{
  "mcpServers": {
    "supermem": {
      "command": "supermem",
      "args": ["serve"]
    }
  }
}
```

When running from source, use `uv run supermem serve` as the command.

## Adding New Memory Connectors

1. Create new directory under `memory_connectors/`
2. Inherit from `BaseMemoryConnector` in `memory_connectors/base.py`
3. Implement required methods:
   - `connector_name` (property)
   - `supported_formats` (property)
   - `extract_data(source_path)` — Parse source data
   - `organize_data(extracted_data)` — Categorize into topics
   - `generate_memory_files(organized_data)` — Write markdown files
4. Register in `memory_connectors/` discovery system
5. All connectors write markdown to the vault, then automatically index into SQLite + graph

## Common Issues

**MCP connection fails**: Verify `supermem` is on PATH. If using `uv run`, ensure the cwd is the repo root. Check Claude Desktop logs at `~/Library/Logs/Claude/`.

**Tier 4 (LLM agent) kicks in too often**: Your vault may be sparse. Import more content via `supermem connect` or increase `min_results` threshold to force Tiers 1–3 to work harder.

**SQLite database locked**: Only one process should access the DB at a time. If using Docker, ensure you're not also running `supermem serve` locally against the same `SUPERMEM_DB_PATH`.

**Kuzu graph tier skipped**: Kuzu is an optional dependency. Install with `pip install kuzu` or `uv add kuzu` in the root workspace.

**ChromaDB tier skipped**: Set `SUPERMEM_VECTOR=true` and ensure `chromadb` and `sentence-transformers` are installed.

**Import failures**: Verify export format matches connector expectations. Use `--max-items N` to limit scope during debugging.

**Privacy tags not stripped**: Ensure tags are exactly `<private>` and `</private>` (case-sensitive). Content between these tags is preserved in memory but excluded from all storage indices.

## Python Version Requirement

**Requires Python 3.11+**. This is enforced in `pyproject.toml`: `requires-python = ">=3.11"`.

## Package Manager

This project uses **uv** (not pip directly). Key commands:

```bash
uv sync                  # Install all workspace dependencies
uv add <package>         # Add a dependency to the current package
uv run <command>         # Run a command within the virtual environment
uv run supermem <args>   # Run the supermem CLI
```
