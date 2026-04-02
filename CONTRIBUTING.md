# Contributing to Recall

---

## Architecture Overview

Recall is a uv workspace monorepo. Three packages share a virtual environment:

```
mem-agent-mcp/
├── recall/          ← Core library (recall-core)
│   ├── core/        ABCs: BaseRetriever, BaseStorage, BaseModelClient, BaseConnector
│   ├── storage/     DatabaseManager (SQLite FTS5), KuzuGraphManager, ChromaManager
│   ├── retrieval/   FTSRetriever, GraphRetriever, VectorRetriever, AgentRetriever, HybridRetriever
│   ├── capture/     ObservationCapture, SessionManager, MemoryCompressor
│   ├── model/       OpenRouter, Ollama, vLLM, Claude, LMStudio clients
│   ├── indexer/     VaultIndexer (watchdog live re-indexing)
│   ├── privacy/     PrivacyFilter (<private> tag stripping)
│   ├── errors.py    RecallError hierarchy
│   ├── config.py    All env-var config (single source of truth)
│   └── logging.py   structlog JSON + correlation ID binding
├── agent/           Agent conversation loop + sandboxed executor (preserved from v1)
├── mcp_server/      FastMCP server, 4 MCP tools, lifespan hooks
├── worker/          Optional FastAPI HTTP dashboard on :37777
├── memory_connectors/  5 import connectors (ChatGPT, Notion, Nuclino, GitHub, Google Docs)
└── tests/
    ├── unit/        Fast, no network, mocked LLM clients
    └── integration/ Real temp vault → index → search
```

### Four-Tier Retrieval

```
HybridRetriever.search(query, tier_limit=4, min_results=3)
    │
    ├── FTSRetriever      tier=1  always available  wraps db.fts_search()
    ├── GraphRetriever    tier=2  requires kuzu     BFS from seed obs_ids → entity names → more obs_ids
    ├── VectorRetriever   tier=3  RECALL_VECTOR=true  cosine similarity via ChromaDB
    └── AgentRetriever    tier=4  always available  wraps agent.Agent.chat(), last resort
```

**Short-circuit**: stops when `len(obs_ids) >= min_results`. Unavailable tiers are logged at WARNING and skipped — no exception is raised.

### Cross-Layer Contract

**Rule: no direct imports between storage/retrieval/capture — only through ABCs in `recall/core/`.**

- `retrieval/` imports from `storage/` only via type hints (`TYPE_CHECKING`) or through constructor injection
- `capture/` does not import `retrieval/`; it writes directly to `storage/`
- `model/` is imported only by `capture/` and `retrieval/agent.py`
- `mcp_server/` is the only layer that wires everything together

Violating this creates circular import chains that are hard to debug.

---

## Development Setup

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all workspace packages + dev deps
uv sync

# Run tests
uv run pytest tests/ -v
uv run pytest tests/ --cov=recall --cov-fail-under=60

# Lint / format
uv run ruff check .
uv run black --check .
```

---

## Adding a New Connector

Connectors import external data (ChatGPT, Notion, etc.) into the vault as markdown, then index it.

1. **Create the connector directory**: `memory_connectors/myservice/`

2. **Implement `BaseMemoryConnector`**:

```python
# memory_connectors/myservice/connector.py
from ..base import BaseMemoryConnector

class MyServiceConnector(BaseMemoryConnector):
    @property
    def connector_name(self) -> str:
        return "MyService"

    @property
    def supported_formats(self) -> list:
        return [".zip", ".json"]  # accepted source file types

    def extract_data(self, source_path: str) -> dict:
        # Validate format with helpful error messages:
        from pathlib import Path
        p = Path(source_path)
        if not p.exists():
            raise FileNotFoundError(
                f"Not found: {source_path}\nHint: Export from MyService → Settings → Export"
            )
        if p.suffix not in (".zip", ".json"):
            raise ValueError(
                f"Unrecognised format '{p.suffix}'.\n"
                "Supported: .zip (full export), .json (raw data).\n"
                "Hint: Download from MyService → Settings → Export data."
            )
        # ... parse and return data dict

    def organize_data(self, extracted_data: dict) -> dict:
        # ... group/structure data
        return organized

    def generate_memory_files(self, organized_data: dict) -> None:
        # ... write .md files to self.output_path
        entities_dir = self.output_path / "entities" / "myservice"
        entities_dir.mkdir(parents=True, exist_ok=True)
        # ... write files ...

        # REQUIRED: index all generated files into Recall storage
        try:
            from recall.indexer.vault import VaultIndexer
            md_paths = list(entities_dir.rglob("*.md"))
            if md_paths:
                VaultIndexer.index_paths(md_paths)
        except Exception as e:
            print(f"⚠️ Could not index files: {e}")
```

3. **Register in `memory_connectors/memory_connect.py`**:

```python
CONNECTORS = {
    ...
    "myservice": ("myservice.connector", "MyServiceConnector"),
}
```

4. **Add tests** in `tests/integration/test_connectors.py`.

### Connector Guidelines

- Always validate the source format early with a helpful error message that tells the user where to download the correct file
- Strip `<private>` content before writing to markdown (use `recall.privacy.filter.PrivacyFilter.strip()`)
- Call `VaultIndexer.index_paths()` at the end of `generate_memory_files()` — this is what populates SQLite for FTS search
- For HTTP connectors: add retry with exponential backoff (use `tenacity`), respect `Retry-After` headers, add 1 req/s rate limiting for unauthenticated APIs
- For large imports: use `rich.progress.Progress` for progress bars

---

## Adding a New LLM Provider

1. Add a new class in `recall/model/base.py` extending `BaseModelClient`:

```python
class MyProviderClient(BaseModelClient):
    def __init__(self) -> None:
        from recall.config import MY_PROVIDER_API_KEY
        if not MY_PROVIDER_API_KEY:
            raise ProviderNotConfiguredError("MY_PROVIDER_API_KEY is not set.")
        self._api_key = MY_PROVIDER_API_KEY

    async def chat_completion(self, messages: list[dict], model: str = "", **kw) -> str:
        # ... call the API, return string
```

2. Register in `get_client_for_provider()` mapping dict.

3. Add `MY_PROVIDER_API_KEY` and any host/port vars to `recall/config.py`.

4. Add a mocked test in `tests/unit/test_model_clients.py`.

---

## Test Instructions

```bash
# Unit tests — fast, no network, no LLM
uv run pytest tests/unit/ -v

# Integration tests — real SQLite + VaultIndexer, no network
uv run pytest tests/integration/ -v

# Full suite with coverage
uv run pytest tests/ --cov=recall --cov-fail-under=60

# Run a single test file
uv run pytest tests/unit/test_database.py -v
```

**Test conventions:**
- Unit tests mock the LLM client with `unittest.mock.AsyncMock`
- Integration tests spin up a real `DatabaseManager` in `tmp_path` — no mocking storage
- Kuzu tests are auto-skipped when `kuzu` is not installed (`pytest.skip`)
- Anthropic/Claude tests are auto-skipped when `anthropic` is not installed (`pytest.importorskip`)
- Use `pytest_asyncio.fixture` for async fixtures, `pytest.mark.asyncio` for async tests

---

## CI Pipeline

`.github/workflows/ci.yml` runs on every pull request:

1. `ruff check .` — linting
2. `black --check .` — formatting
3. `mypy recall/ agent/ mcp_server/` — type checking (continue-on-error)
4. `pytest --cov=recall --cov-fail-under=60` — tests + coverage gate

On tag push (`v*`): builds Docker image + pushes to GHCR, publishes to PyPI.

---

## Reporting Issues

Open an issue at [firstbatchxyz/mem-agent-mcp](https://github.com/firstbatchxyz/mem-agent-mcp/issues).

Please include:
- Recall version (`recall --version`)
- `RECALL_LLM_PROVIDER` value
- Full error traceback
- Whether you're using MCP stdio or HTTP transport
