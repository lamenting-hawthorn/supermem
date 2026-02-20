# Recall

> **Persistent AI memory without RAG** — an agent that navigates your knowledge base like a filesystem, not a vector index.

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://python.org)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple.svg)](https://modelcontextprotocol.io)

An MCP (Model Context Protocol) server that gives AI assistants — Claude Desktop, LM Studio, ChatGPT — a **persistent, structured memory** backed by local markdown files. Powered by [driaforall/mem-agent](https://huggingface.co/driaforall/mem-agent), a fine-tuned model purpose-built for memory navigation.

---

## Why Not RAG?

Most AI memory systems use **RAG (Retrieval-Augmented Generation)**: embed your documents as vectors, then retrieve the "most similar" chunks at query time. This works well for large corpora but has a hard ceiling — if the embedding similarity doesn't surface the right chunk, that information is effectively invisible to the model.

**mem-agent takes a different approach:**

| | RAG | mem-agent |
|---|---|---|
| **Retrieval** | Embedding similarity (top-k chunks) | Agent navigates the file graph directly |
| **Memory limit** | Bounded by embedding quality | Technically unlimited — agent can explore any path |
| **Multi-hop reasoning** | Limited (single retrieval step) | Native (follows [[wikilinks]] across files) |
| **Write support** | Rarely | First-class — agent reads and writes memory |
| **Speed** | Fast (one vector lookup) | Slow (multiple LLM calls + file I/O per query) |
| **Retrieval failures** | Silent (wrong embedding = missed info) | Explicit (agent reports what it couldn't find) |

**The tradeoff is real:** this is slower than RAG. Each query may require several LLM inference calls as the agent reasons about, navigates, and reads your memory. If you need sub-second retrieval over millions of documents, use RAG. If you want an assistant that can genuinely reason about your personal knowledge base and never miss information due to embedding drift, this is for you.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        AI Client                            │
│           (Claude Desktop / LM Studio / ChatGPT)           │
└───────────────────────┬─────────────────────────────────────┘
                        │ MCP (stdio / HTTP / SSE)
┌───────────────────────▼─────────────────────────────────────┐
│                    mcp_server/                               │
│         FastMCP server — exposes memory tool to client      │
└───────────────────────┬─────────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────────┐
│                      agent/                                  │
│                                                             │
│  User query                                                 │
│      │                                                      │
│      ▼                                                      │
│  Agent LLM (mem-agent, MLX/vLLM)                           │
│      │  generates <think> + <python> blocks                 │
│      ▼                                                      │
│  Sandboxed subprocess  ◄──── tools.py (file API)           │
│      │  path-restricted, 20s timeout                       │
│      ▼                                                      │
│  Results fed back → up to 20 tool turns                    │
│      │                                                      │
│      ▼                                                      │
│  Final <reply> returned to client                           │
└───────────────────────┬─────────────────────────────────────┘
                        │ read / write
┌───────────────────────▼─────────────────────────────────────┐
│                 Memory (local markdown vault)                │
│                                                             │
│   memory/                                                   │
│   ├── user.md              ← root profile + nav links       │
│   └── entities/                                             │
│       ├── jane_doe.md      ← [[linked]] from user.md        │
│       └── acme_corp.md     ← [[linked]] from user.md        │
└─────────────────────────────────────────────────────────────┘
```

**Memory connectors** (separate package) import data from ChatGPT, Notion, Nuclino, GitHub, and Google Docs into the markdown vault before the agent ever runs.

---

## Supported Platforms

| Platform | Backend | Requirement |
|---|---|---|
| macOS | MLX (Apple Silicon) | LM Studio |
| Linux | vLLM (GPU) | CUDA GPU |
| Any OS | OpenRouter API | `OPENROUTER_API_KEY` |

---

## Quick Start

> **Requires Python 3.11.** If you don't have it, `uv` will install it automatically.

### Path A — No GPU (OpenRouter API)

The fastest way to get running. No local model server needed.

```bash
# 1. Get a free API key at https://openrouter.ai/keys
# 2. Run:
make quickstart
# Copies .env.example → .env, prompts you to add your API key,
# installs deps, and sets up your memory directory.

# 3. Start chatting:
make chat-cli
```

### Path B — Local model (Apple Silicon / Linux GPU)

Fully private — no data leaves your machine.

```bash
make quickstart-local
# Installs deps + LM Studio, sets up memory, starts the model server.
# Prompts for model precision (4-bit recommended to start).
```

### Connect to your AI client

**Claude Desktop:**
```bash
make generate-mcp-json
# Copy mcp.json → ~/.config/claude/claude_desktop.json
# Restart Claude Desktop
```

**LM Studio:**
```bash
make generate-mcp-json
# Copy mcp.json to LM Studio's mcp.json location
# See: https://lmstudio.ai/docs/app/plugins/mcp
```

**Claude Code (terminal):**
```bash
claude mcp add mem-agent \
  --env MEMORY_DIR="/path/to/your/memory" \
  -- python "/path/to/mcp_server/server.py"

claude mcp list  # verify it appears
```

**ChatGPT:**
```bash
make serve-mcp-http          # FastAPI on :8081
# In another terminal:
ngrok http 8081
# Add https://your-ngrok-url.ngrok.io/mcp to ChatGPT → Settings → Connectors
```

---

## Memory Structure

The agent navigates an **Obsidian-style** markdown vault with wikilinks:

```
memory/
├── user.md              # Root — your profile and all entity links
└── entities/
    ├── jane_doe.md      # Linked from user.md as [[entities/jane_doe]]
    └── acme_corp.md
```

**user.md** example:
```markdown
# User Information
- user_name: John Doe
- birth_date: 1990-01-01
- living_location: Enschede, Netherlands

## User Relationships
- company: [[entities/acme_corp.md]]
- mother: [[entities/jane_doe.md]]
```

**Entity file** example:
```markdown
# Jane Doe
- relationship: Mother
- birth_date: 1965-01-01
- birth_location: New York, USA
```

The agent can follow `[[wikilinks]]` across files, enabling multi-hop reasoning: *"What city does John's mother live in?"* → reads `user.md` → follows link → reads `jane_doe.md` → answers.

> Modifying memory files manually does not require restarting the MCP server.

---

## Importing Existing Data

The fastest way to populate memory from real data:

```bash
make memory-wizard   # interactive setup for any connector
```

### Available Connectors

| Connector | Source | Method |
|---|---|---|
| `chatgpt` | ChatGPT export `.zip` | Offline export |
| `notion` | Notion export `.zip` | Offline export |
| `nuclino` | Nuclino export `.zip` | Offline export |
| `github` | GitHub API | Live (token) |
| `google-docs` | Google Drive | Live (OAuth 2.0) |

### ChatGPT History

1. Go to [ChatGPT Settings → Data Controls](https://chatgpt.com/settings/data-controls) → Export data
2. Wait for the email, download the ZIP
3. Run:

```bash
make connect-memory CONNECTOR=chatgpt SOURCE=/path/to/export.zip

# With AI-powered categorization (semantic, requires LM Studio):
python memory_connectors/memory_connect.py chatgpt /path/to/export.zip \
  --method ai --embedding-model lmstudio

# Limit scope for testing:
make connect-memory CONNECTOR=chatgpt SOURCE=/path/to/export.zip MAX_ITEMS=100
```

### Notion

1. Notion workspace settings → Export content → Markdown & CSV → Export all
2. Run:

```bash
make connect-memory CONNECTOR=notion SOURCE=/path/to/notion-export.zip
```

### Nuclino

1. Workspace menu (☰) → ⋮ → Workspace settings → Export Workspace
2. Run:

```bash
make connect-memory CONNECTOR=nuclino SOURCE=/path/to/nuclino-export.zip
```

### GitHub (live)

```bash
# Single repo
make connect-memory CONNECTOR=github SOURCE="owner/repo" TOKEN=your_github_token

# Multiple repos
make connect-memory CONNECTOR=github SOURCE="owner/repo1,owner/repo2" TOKEN=your_token

# With content type control
python memory_connectors/memory_connect.py github "owner/repo" \
  --include-issues --include-prs --include-wiki --token your_token
```

Get a token: [GitHub Settings → Tokens](https://github.com/settings/tokens) → `public_repo` scope for public repos, `repo` for private.

### Google Docs (live)

```bash
make connect-memory CONNECTOR=google-docs SOURCE="folder_id" TOKEN=your_access_token
```

Get an access token via [Google OAuth 2.0 Playground](https://developers.google.com/oauthplayground/) (select `Drive API v3 → drive.readonly`).

Find folder ID from URL: `https://drive.google.com/drive/folders/`**`1ABC123DEF456`** ← that's the ID.

---

## Privacy Filters

The agent accepts `<filter>` tags to redact specific information from responses:

```
What's my mother's age? <filter> 1. Do not reveal explicit age information </filter>
```

Manage filters via:
```bash
make add-filters    # add filters interactively
make reset-filters  # clear all filters
```

Changes take effect immediately without restarting the server.

---

## Running Tests

```bash
# Install dev dependencies
uv sync

# Run all tests
uv run pytest tests/ -v

# Run a specific test file
uv run pytest tests/test_tools.py -v

# Run with coverage
uv run pytest tests/ --cov=agent --cov-report=term-missing
```

---

## Development

### Project Structure

```
mem-agent-mcp/
├── agent/                   # Core agent package
│   ├── agent.py             # Agent class + conversation loop
│   ├── engine.py            # Sandboxed subprocess executor
│   ├── model.py             # LLM client (OpenRouter/vLLM)
│   ├── tools.py             # File operation API for the agent
│   ├── schemas.py           # Pydantic models
│   ├── settings.py          # Environment config
│   ├── utils.py             # Helpers (prompt loading, parsing)
│   └── system_prompt.txt    # Agent behavior spec
├── mcp_server/              # FastMCP server wrappers
│   ├── server.py            # stdio transport (Claude Desktop)
│   ├── mcp_http_server.py   # HTTP transport (ChatGPT)
│   ├── mcp_sse_server.py    # SSE transport
│   └── scripts/             # Setup utilities
├── memory_connectors/       # Data import plugin system
│   ├── base.py              # Abstract connector interface
│   ├── chatgpt_history/
│   ├── notion/
│   ├── nuclino/
│   ├── github_live/
│   ├── google_docs_live/
│   ├── memory_connect.py    # CLI router
│   └── memory_wizard.py     # Interactive wizard
├── memories/                # Sample memory packs (healthcare, client_success)
├── examples/
│   └── mem_agent_cli.py     # Interactive CLI demo
├── tests/                   # pytest test suite
└── chat_cli.py              # Rich-formatted terminal REPL
```

### Adding a New Memory Connector

1. Create a directory under `memory_connectors/`
2. Implement a class inheriting `BaseMemoryConnector`:

```python
from memory_connectors.base import BaseMemoryConnector
from typing import Dict, Any

class MyServiceConnector(BaseMemoryConnector):
    @property
    def connector_name(self) -> str:
        return "My Service"

    @property
    def supported_formats(self) -> list:
        return ['.zip', '.json']

    def extract_data(self, source_path: str) -> Dict[str, Any]:
        # Parse the source data into a flat dict
        ...

    def organize_data(self, extracted_data: Dict[str, Any]) -> Dict[str, Any]:
        # Group/categorize by topic
        ...

    def generate_memory_files(self, organized_data: Dict[str, Any]) -> None:
        # Write Obsidian-style markdown files
        ...
```

3. Register it in `memory_connectors/memory_connect.py`
4. Add usage examples to this README

### Sandbox Security

All agent-generated Python code runs in a **separate subprocess** (`agent/engine.py`) with:
- Working directory locked to the configured memory path
- `open()`, `os.remove()`, `os.rename()` wrapped to deny paths outside memory root
- Configurable builtin blacklist (exec, eval, etc.)
- 20-second hard timeout
- Only picklable results returned to the main process

### Environment Variables

Copy `.env.example` to `.env` and fill in what you need:

```bash
cp .env.example .env
```

| Variable | Purpose | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | Cloud LLM (Path A) | — |
| `MEMORY_DIR` | Override memory directory | from `.memory_path` |
| `VLLM_HOST` / `VLLM_PORT` | vLLM server (Linux) | `0.0.0.0:8000` |
| `FASTMCP_LOG_LEVEL` | Log verbosity | `INFO` |
| `MCP_TRANSPORT` | `stdio` / `http` / `sse` | `stdio` |

`.env` is gitignored — it will never be committed.

---

## Troubleshooting

**Agent gives generic responses instead of using memory:**
- Confirm memory files exist at the configured path
- Check that `user.md` has proper topic links
- Enable debug logging: `FASTMCP_LOG_LEVEL=DEBUG make serve-mcp`
- Check logs at `~/Library/Logs/Claude/mcp-server-memory-agent-stdio.log` (macOS)

**MCP connection issues:**
- Verify `~/.config/claude/claude_desktop.json` has the correct paths
- Ensure the LM Studio / vLLM server is running before starting the MCP server
- On LM Studio, try changing model name in `.mlx_model_name` from `mem-agent-mlx-4bit` to `mem-agent-mlx@4bit`

**Memory import failures:**
- Check that the export format is supported
- Try `--max-items 10` to limit scope and confirm the connector works
- Verify file permissions on the output directory

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions, code style, and how to submit pull requests.

---

## License

[Apache 2.0](LICENSE) — © Recall contributors
