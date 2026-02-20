# Contributing to Recall

Thanks for your interest in contributing! This guide covers setup, code style, and the pull request process.

## Development Setup

**Requirements:** Python 3.11 exactly, `uv`

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/recall.git
cd recall

# 2. Install uv if you don't have it
make check-uv

# 3. Install all dependencies (including dev)
uv sync

# 4. Configure a test memory directory
make setup-cli
```

## Running Tests

```bash
# Run the full suite
uv run pytest tests/ -v

# Run a specific test file
uv run pytest tests/test_tools.py -v

# Run with coverage report
uv run pytest tests/ --cov=agent --cov-report=term-missing
```

All tests should pass before submitting a PR. If you add a new feature, add tests for it.

## Code Style

- **Formatter:** `black` (already a dependency — run `uv run black .` before committing)
- **Type hints:** Add them to any new public functions/methods
- **Docstrings:** Required for public functions and classes (Google style)

```python
def create_file(file_path: str, content: str = "") -> bool:
    """
    Create a new file in the memory vault.

    Args:
        file_path: Relative path within the memory directory.
        content: Initial file content.

    Returns:
        True if the file was created successfully.

    Raises:
        Exception: If size limits are exceeded or write fails.
    """
```

## Project Structure

```
recall/
├── agent/           # Core agent logic — be careful here, changes affect all integrations
├── mcp_server/      # MCP server wrappers — transport-specific code
├── memory_connectors/  # Data import plugins — safest place to add new connectors
├── tests/           # pytest test suite
└── examples/        # Demo scripts
```

## Adding a Memory Connector

Memory connectors live in `memory_connectors/`. Each connector:

1. Inherits from `BaseMemoryConnector` (`memory_connectors/base.py`)
2. Implements three methods: `extract_data()`, `organize_data()`, `generate_memory_files()`
3. Is registered in `memory_connectors/memory_connect.py`

```python
from memory_connectors.base import BaseMemoryConnector
from typing import Dict, Any

class MyServiceConnector(BaseMemoryConnector):
    @property
    def connector_name(self) -> str:
        return "My Service"

    @property
    def supported_formats(self) -> list:
        return ['.zip']

    def extract_data(self, source_path: str) -> Dict[str, Any]:
        # Parse the source into a raw data dict
        ...

    def organize_data(self, extracted_data: Dict[str, Any]) -> Dict[str, Any]:
        # Group/categorize into topics
        ...

    def generate_memory_files(self, organized_data: Dict[str, Any]) -> None:
        # Write Obsidian-style markdown to self.output_dir
        ...
```

Then register it:

```python
# memory_connectors/memory_connect.py
CONNECTORS = {
    ...
    "my-service": MyServiceConnector,
}
```

## Submitting a Pull Request

1. Fork the repo and create a branch: `git checkout -b feature/my-thing`
2. Make your changes with tests
3. Run `uv run black .` to format
4. Run `uv run pytest tests/ -v` — all tests must pass
5. Push and open a PR against `main`
6. Fill in the PR template

## Reporting Bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md). Include:
- OS and Python version
- Steps to reproduce
- Expected vs actual behaviour
- Relevant log output (`FASTMCP_LOG_LEVEL=DEBUG make serve-mcp`)

## Security

If you find a security issue in the sandbox implementation (`agent/engine.py`) or file path handling, please **do not** open a public issue. Email the maintainer directly.
