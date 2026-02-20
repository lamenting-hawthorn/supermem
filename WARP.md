# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

mem-agent-mcp is an MCP (Model Context Protocol) server that connects AI applications (Claude Desktop, LM Studio, ChatGPT) to a local memory agent. The agent manages an Obsidian-like memory system using a fine-tuned LLM (driaforall/mem-agent) that can read, write, and organize personal information in markdown files.

**Key Concept**: The memory agent uses structured markdown files with wikilink-style cross-references to maintain persistent memory across conversations. It executes sandboxed Python code to interact with the file system.

## Essential Commands

### Initial Setup
```bash
# 1. Check/install uv package manager
make check-uv

# 2. Install all dependencies (auto-installs LM Studio on macOS)
make install

# 3. Configure memory directory (GUI file picker)
make setup

# 3a. Configure memory directory (CLI alternative)
make setup-cli

# 4. Start the memory agent (will prompt for model precision on macOS)
make run-agent
# Options: 1) 4-bit (fast), 2) 8-bit (balanced), 3) bf16 (highest quality)

# 5. Generate MCP configuration file
make generate-mcp-json
```

### Development Workflow
```bash
# Run interactive chat CLI to test the agent
make chat-cli

# Start MCP server via stdio (for Claude Desktop)
make serve-mcp

# Start HTTP server for ChatGPT integration
make serve-mcp-http  # Then use ngrok: ngrok http 8081
```

### Memory Connectors
```bash
# Interactive wizard (recommended)
make memory-wizard

# Import ChatGPT conversations
make connect-memory CONNECTOR=chatgpt SOURCE=/path/to/export.zip

# Import Notion workspace
make connect-memory CONNECTOR=notion SOURCE=/path/to/export.zip

# Import Nuclino workspace
make connect-memory CONNECTOR=nuclino SOURCE=/path/to/export.zip

# Import GitHub repository (live)
make connect-memory CONNECTOR=github SOURCE="owner/repo" TOKEN=your_token

# Import Google Docs (live)
make connect-memory CONNECTOR=google-docs SOURCE="folder_id" TOKEN=your_token
```

### Filter Management
```bash
# Add filters (privacy controls for agent responses)
make add-filters

# Clear all filters
make reset-filters
```

## Architecture

### Three-Layer System

1. **Agent Layer** (`agent/`)
   - Core memory agent implementation
   - Sandboxed Python code execution engine
   - OpenAI-compatible client (supports vLLM, MLX, OpenRouter)
   - Tool functions for file/directory operations

2. **MCP Server Layer** (`mcp_server/`)
   - FastMCP server exposing `use_memory_agent` tool
   - Multiple transport modes: stdio, HTTP, SSE
   - Reads configuration from `.memory_path` and `.mlx_model_name` files
   - Manages filter injection from `.filters` file

3. **Memory Connectors** (`memory_connectors/`)
   - Plugin system for importing external data sources
   - Base class: `BaseMemoryConnector` with `extract_data()`, `organize_data()`, `generate_memory_files()`
   - Supports both export-based (zip files) and live API connectors

### Key Design Patterns

**Workspace Structure**: This is a uv workspace with three packages:
- Root: `mem-agent-mcp` (aggregator)
- `agent/` (standalone agent package)
- `mcp_server/` (MCP wrapper around agent)

**Sandboxed Execution**: The agent generates Python code which is executed in a restricted environment:
- File access limited to configured memory directory
- Timeout enforcement (20 seconds default)
- Blacklist for dangerous operations
- Results must be assigned to variables or they're lost

**Memory Format**: Obsidian-compatible markdown structure:
```
memory/
├── user.md                    # Main user profile
└── entities/
    ├── person_name.md         # Individual entity files
    ├── company_name.md
    └── ...
```

**Agent Response Format**: The agent MUST follow strict XML-like tags:
```
<think>reasoning</think>
<python>code or empty</python>
<reply>user response (only if python is empty)</reply>
```

### Important Implementation Details

**Platform-Specific Behavior**:
- macOS: Uses MLX models via LM Studio (`lms` CLI)
- Linux: Uses vLLM for GPU inference
- Model selection saved to `.mlx_model_name` at repo root

**Memory Path Resolution**:
- MCP server reads from `.memory_path` file (absolute or relative)
- Falls back to `memory/mcp-server/` if not configured
- Agent always resolves to absolute paths internally

**Filter System**:
- Filters stored in `.filters` file at repo root
- Automatically injected into queries as `<filter>...</filter>` tags
- Agent trained to respect filter constraints (privacy, obfuscation)

**Tool Function Patterns**:
- All tools in `agent/tools.py` MUST return values (not None)
- Size limits enforced: 1MB per file, 10MB per directory, 100MB total
- `update_file()` uses simple find-and-replace (no git-style diffs)

## Testing

```bash
# Test with sample memories (healthcare, client_success)
make run-agent
make serve-mcp-http
python examples/mem_agent_cli.py

# Run agent tests (if available)
cd agent && uv run pytest

# Run MCP server tests (if available)
cd mcp_server && uv run pytest
```

## Configuration Files

- `.memory_path` - Absolute path to memory directory (created by `make setup`)
- `.mlx_model_name` - MLX model name for macOS (created by `make run-agent`)
- `.filters` - Privacy filters applied to all queries (optional)
- `mcp.json` - Generated MCP configuration for Claude Desktop
- `pyproject.toml` - Root workspace configuration
- `agent/pyproject.toml` - Agent package dependencies
- `mcp_server/pyproject.toml` - MCP server dependencies

## Adding New Memory Connectors

1. Create new directory under `memory_connectors/`
2. Inherit from `BaseMemoryConnector` in `memory_connectors/base.py`
3. Implement required methods:
   - `connector_name` (property)
   - `supported_formats` (property)
   - `extract_data(source_path)` - Parse source data
   - `organize_data(extracted_data)` - Categorize into topics
   - `generate_memory_files(organized_data)` - Write markdown files
4. Register in `memory_connectors/memory_connect.py`
5. Add to `memory_connectors/__init__.py`

## Common Issues

**Agent returns generic responses**: Memory files may not exist or lack proper topic navigation in `user.md`. Run `make chat-cli` to test directly.

**MCP connection fails**: Check Claude Desktop config at `~/.config/claude/claude_desktop.json`. Verify `mcp.json` was copied correctly. Check logs at `~/Library/Logs/Claude/mcp-server-memory-agent-stdio.log`.

**Model not found on macOS**: Ensure LM Studio is running and model is loaded. Check `.mlx_model_name` contains correct model identifier. Try changing from `mem-agent-mlx-4bit` to `mem-agent-mlx@4bit` format.

**Import failures**: Verify export format matches connector expectations. Use `--max-items` to limit scope during debugging.

## Python Version Requirement

**Requires Python 3.11** (not 3.12+). This is enforced in all `pyproject.toml` files: `requires-python = ">=3.11,<3.12"`
