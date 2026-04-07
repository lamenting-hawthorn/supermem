# supermem

> **Persistent AI memory without RAG** — four-tier retrieval that uses an LLM agent only as a last resort, backed by SQLite FTS5, an embedded graph database, and your local markdown vault.

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://python.org)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple.svg)](https://modelcontextprotocol.io)

An MCP (Model Context Protocol) server that gives AI assistants — Claude Desktop, LM Studio, ChatGPT — **persistent, structured memory** backed by SQLite + an optional graph database. The LLM agent is tier 4, not the default path — most queries resolve in milliseconds via full-text search.

---

## Quick Start (Personal, No GPU)

```bash
pip install supermem

# Point Recall at a directory of markdown files
export SUPERMEM_VAULT_PATH=~/notes
export SUPERMEM_LLM_PROVIDER=openrouter
export OPENROUTER_API_KEY=your_key_here

# Start the MCP server (add to Claude Desktop's mcp.json)
supermem serve
```

Add to Claude Desktop `mcp.json`:
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

---

## Quick Start (Production with Docker)

```bash
# Clone and configure
git clone https://github.com/firstbatchxyz/mem-agent-mcp
cp .env.example .env
# Edit .env: set SUPERMEM_VAULT_PATH, SUPERMEM_LLM_PROVIDER, API keys

# MCP server only (stdio, for Claude Desktop)
docker compose up supermem-mcp

# MCP server + HTTP dashboard
docker compose --profile worker up

# Dashboard at http://localhost:37777
```

---

## Architecture: Four-Tier Retrieval

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

**Short-circuit rule**: if tier 1 returns ≥ `min_results` (default 3), tiers 2–4 are skipped entirely. Unavailable tiers are skipped with a WARNING log — no errors raised.

---

## MCP Tool Reference

| Tool | Parameters | Returns | Notes |
|------|-----------|---------|-------|
| `use_memory_agent` | `query: str` | Formatted answer | Backward-compatible. Routes through all 4 tiers; falls back to full agent only if tiers 1–3 insufficient |
| `supermem_hybrid` | `query: str`, `tier_limit: int = 4` | JSON with `obs_ids`, `source_tier`, `latency_ms` | Preferred for programmatic use. Token-efficient — returns IDs first |
| `get_observations` | `ids: list[int]` | JSON array of observation dicts | Fetch full content for specific IDs |
| `get_timeline` | `obs_id: int`, `window: int = 5` | JSON array of chronological observations | Context around a specific observation |

### Progressive Disclosure Pattern

```python
# 1. Search — cheap, returns IDs only
result = await supermem_hybrid("Alice's project status", tier_limit=2)
# {"obs_ids": [42, 17, 88], "source_tier": 1, "latency_ms": 2.1}

# 2. Fetch — only for IDs you actually need
obs = await get_observations([42, 17])
# [{"id": 42, "content": "...", "tier_used": 1}, ...]

# 3. Timeline — context around interesting observations  
ctx = await get_timeline(42, window=3)
```

---

## Environment Variables

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

---

## Connector Guide

Import external data into your vault with one command:

```bash
# ChatGPT export (Settings → Data controls → Export data → .zip)
supermem connect chatgpt ~/Downloads/chatgpt_export.zip

# Notion workspace export (.zip)
supermem connect notion ~/Downloads/notion_export.zip

# Nuclino workspace export (.zip)
supermem connect nuclino ~/Downloads/nuclino_export.zip

# GitHub repositories (live via API)
supermem connect github owner/repo1,owner/repo2 --token ghp_xxx

# Google Docs (OAuth, opens browser)
supermem connect google_docs "My Doc Name"
```

All connectors write markdown to your vault, then automatically index the files into SQLite + graph. Private content wrapped in `<private>...</private>` tags is stripped before indexing.

---

## CLI Reference

```bash
supermem serve            # Start MCP server (stdio transport, for Claude Desktop)
supermem serve --worker   # Start MCP server + HTTP dashboard on :37777
supermem chat             # Interactive terminal REPL (no client required)
supermem backup           # Create timestamped .tar.gz (vault + SQLite)
supermem backup --output /path/to/archive.tar.gz
supermem restore <archive.tar.gz>
supermem connect <type> <source> [--token TOKEN] [--max-items N]
```

---

## HTTP Dashboard (Optional)

Start with `supermem serve --worker` or `docker compose --profile worker up`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | `{"status":"ok","db":true,"graph":false,"vector":false}` |
| `/sessions` | GET | Paginated session list with summaries |
| `/observations` | GET | Filter by session/date/type |
| `/search` | POST | `{"query": "...", "tier_limit": 4}` |
| `/index/rebuild` | POST | Reindex entire vault |
| `/backup` | GET | Streams vault + DB as `.tar.gz` |
| `/stats` | GET | `{obs_count, entity_count, session_count, db_size_mb}` |

Auth: `Authorization: Bearer <SUPERMEM_API_KEY>`. Disabled when env var is unset.

---

## Privacy

Wrap sensitive content in `<private>...</private>` tags. It is stripped before writing to any storage layer (SQLite, Kuzu, ChromaDB). The content passes through to the agent sandbox only — it never persists.

```markdown
# Meeting Notes

Alice discussed the roadmap.
<private>Budget: $2.4M approved for Q3</private>
Next steps: ship v2 by June.
```

---

## Running Tests

```bash
uv run pytest tests/ -v                          # all tests
uv run pytest tests/unit/ -v                     # unit only (fast, no network)
uv run pytest tests/integration/ -v              # integration (real storage)
uv run pytest tests/ --cov=supermem --cov-report=term-missing  # with coverage
```

Coverage gate: 60% (CI enforced). Kuzu and Anthropic tests are auto-skipped if packages are not installed.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
