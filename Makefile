# Set default target
.DEFAULT_GOAL := help

# Repository root (absolute)
REPO_ROOT := $(shell git rev-parse --show-toplevel 2>/dev/null || pwd)

# MLX Agent Names
MLX_4BIT_MEMORY_AGENT_NAME := mem-agent-mlx-4bit
MLX_8BIT_MEMORY_AGENT_NAME := mem-agent-mlx-8bit
MLX_MEMORY_AGENT_NAME := driaforall/mem-agent-mlx-bf16 
BF16_MEMORY_AGENT_SEARCH_NAME := mem-agent-mlx-bf16

# Help command
help:
	@echo ""
	@echo "  supermem — Persistent AI memory without RAG"
	@echo ""
	@echo "  QUICK START (pick one path):"
	@echo "    make quickstart          — no GPU, uses OpenRouter API (set OPENROUTER_API_KEY in .env)"
	@echo "    make quickstart-local    — macOS/Linux with local model (requires GPU or Apple Silicon)"
	@echo ""
	@echo "  ALL TARGETS:"
	@echo "    check-uv          Install uv if missing"
	@echo "    install           Install dependencies (+ LM Studio on macOS)"
	@echo "    install-api       Install dependencies only (skips LM Studio)"
	@echo "    setup             Choose memory directory (GUI)"
	@echo "    setup-cli         Choose memory directory (CLI)"
	@echo "    run-agent         Start local model server (MLX or vLLM)"
	@echo "    generate-mcp-json Generate mcp.json for Claude Desktop / LM Studio"
	@echo "    serve-mcp         Start MCP server (stdio)"
	@echo "    serve-mcp-http    Start loopback MCP HTTP (API key required)"
	@echo "    chat-cli          Interactive terminal chat"
	@echo "    memory-wizard     Import memory from ChatGPT, Notion, GitHub, etc."
	@echo "    connect-memory    Direct connector CLI"
	@echo "    add-filters       Add privacy filters"
	@echo "    reset-filters     Clear privacy filters"
	@echo ""

# ─────────────────────────────────────────────────────────────
# QUICK START paths
# ─────────────────────────────────────────────────────────────

# Path A: API-only (no GPU needed — uses OpenRouter)
quickstart:
	@echo ""
	@echo "  supermem — Quick Start (API mode)"
	@echo ""
	@echo "  Step 1: Copy .env.example to .env and add your OPENROUTER_API_KEY"
	@echo "          Get a key at: https://openrouter.ai/keys"
	@echo ""
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "  Created .env from .env.example — open it and add your API key, then re-run: make quickstart"; \
		exit 0; \
	fi
	@if ! grep -q "OPENROUTER_API_KEY=." .env 2>/dev/null; then \
		echo "  OPENROUTER_API_KEY is not set in .env. Open .env and add your key."; \
		exit 1; \
	fi
	@echo "  Step 2: Installing dependencies..."
	$(MAKE) install-api
	@echo "  Step 3: Choosing memory directory..."
	$(MAKE) setup-cli
	@echo ""
	@echo "  Done! Start chatting:"
	@echo "    make chat-cli            — terminal REPL"
	@echo "    make generate-mcp-json   — then copy mcp.json to Claude Desktop"
	@echo ""

# Path B: Local model (macOS Apple Silicon or Linux GPU)
quickstart-local:
	@echo ""
	@echo "  supermem — Quick Start (local model)"
	@echo ""
	$(MAKE) install
	$(MAKE) setup-cli
	$(MAKE) run-agent
	@echo ""
	@echo "  Model server running. In a new terminal:"
	@echo "    make chat-cli            — terminal REPL"
	@echo "    make generate-mcp-json   — then copy mcp.json to Claude Desktop"
	@echo ""

# ─────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────

# Check if uv is installed and install if needed
check-uv:
	@echo "Checking if uv is installed..."
	@if ! command -v uv > /dev/null; then \
		echo "uv not found. Installing uv..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
		echo "Please restart your shell or run 'source ~/.bashrc' (or ~/.zshrc) to use uv"; \
	else \
		echo "uv is already installed"; \
		uv --version; \
	fi

# Install dependencies using uv (skips LM Studio — use for API-only / CI)
install-api: check-uv
	@echo "Installing dependencies with uv (API mode — skipping LM Studio)..."
	uv sync

# Install dependencies using uv
install: check-uv
	@echo "Installing top-level workspace with uv..."
	uv sync
	@if [ "$$(uname -s)" = "Darwin" ]; then \
		if ! command -v lms > /dev/null; then \
			echo "lms not found. Installing lms..."; \
			chmod +x mcp_server/scripts/install_lms.sh; \
			./mcp_server/scripts/install_lms.sh; \
		else \
			echo "lms is already installed"; \
		fi; \
	fi

setup:
	uv run python mcp_server/scripts/memory_setup.py && uv run python mcp_server/scripts/setup_scripts_and_json.py && chmod +x mcp_server/scripts/start_server.sh

setup-cli:
	uv run python mcp_server/scripts/memory_setup_cli.py && uv run python mcp_server/scripts/setup_scripts_and_json.py && chmod +x mcp_server/scripts/start_server.sh

add-filters:
	uv run python mcp_server/scripts/filters.py --add

reset-filters:
	uv run python mcp_server/scripts/filters.py --reset

run-agent:
	@if [ "$$(uname -s)" = "Darwin" ]; then \
		echo "Detected macOS (Darwin). Starting MLX server via lms..."; \
		echo "Select MLX model precision:"; \
		echo "  1) 4-bit ($(MLX_4BIT_MEMORY_AGENT_NAME))"; \
		echo "  2) 8-bit ($(MLX_8BIT_MEMORY_AGENT_NAME))"; \
		echo "  3) bf16 ($(MLX_MEMORY_AGENT_NAME))"; \
		printf "Enter choice [1-3]: "; read choice; \
		case $$choice in \
			1) model=$(MLX_4BIT_MEMORY_AGENT_NAME); search_name=$(MLX_4BIT_MEMORY_AGENT_NAME);; \
			2) model=$(MLX_8BIT_MEMORY_AGENT_NAME); search_name=$(MLX_8BIT_MEMORY_AGENT_NAME);; \
			3) model=$(MLX_MEMORY_AGENT_NAME); search_name=$(BF16_MEMORY_AGENT_SEARCH_NAME);; \
			*) echo "Invalid choice. Defaulting to 4-bit."; model=$(MLX_4BIT_MEMORY_AGENT_NAME); search_name=$(MLX_4BIT_MEMORY_AGENT_NAME);; \
		esac; \
		printf "%s\n" "$$model" > $(REPO_ROOT)/.mlx_model_name; \
		echo "Saved model to $(REPO_ROOT)/.mlx_model_name: $$(cat $(REPO_ROOT)/.mlx_model_name)"; \
		lms get $$search_name --mlx --always-show-all-results; \
		lms load $$model; \
		lms server start --port 8000; \
	else \
		echo "Non-macOS detected. Starting vLLM server..."; \
		uv run vllm serve driaforall/mem-agent; \
	fi

generate-mcp-json:
	@echo "Generating mcp.json..."
	@echo '{"mcpServers": {"memory-agent-stdio": {"command": "bash", "args": ["-lc", "cd $(REPO_ROOT) && uv run python mcp_server/server.py"], "env": {"FASTMCP_LOG_LEVEL": "INFO", "MCP_TRANSPORT": "stdio"}, "timeout": 600000}}}' > mcp.json
	@echo "Wrote mcp.json the following contents:"
	@cat mcp.json

serve-mcp:
	@echo "Starting MCP server over STDIO"
	FASTMCP_LOG_LEVEL=INFO MCP_TRANSPORT=stdio uv run python -m mcp_server.server

chat-cli:
	uv run python chat_cli.py

# Interactive Memory Wizard
memory-wizard:
	@echo "🧙‍♂️ Starting Memory Connector Wizard..."
	uv run python -m memory_connectors.memory_wizard

# Memory Connectors (unified system)
connect-memory:
	@echo "Memory Connectors - Convert various sources to mem-agent format"
	@echo "Usage: make connect-memory CONNECTOR=<type> SOURCE=<path> [OUTPUT=<path>] [MAX_ITEMS=<num>] [TOKEN=<token>]"
	@echo ""
	@echo "Available connectors:"
	@uv run python -m memory_connectors.memory_connect --list
	@echo ""
	@if [ -z "$(CONNECTOR)" ] || [ -z "$(SOURCE)" ]; then \
		echo "Examples:"; \
		echo "  Export-based: make connect-memory CONNECTOR=chatgpt SOURCE=/path/to/export.zip"; \
		echo "  Live GitHub:  make connect-memory CONNECTOR=github SOURCE='owner/repo' TOKEN=github_token"; \
		echo "  Live Google:  make connect-memory CONNECTOR=google-docs SOURCE='folder_id' TOKEN=access_token"; \
		exit 1; \
	fi
	@if [ "$(CONNECTOR)" != "github" ] && [ "$(CONNECTOR)" != "google-docs" ] && [ ! -e "$(SOURCE)" ]; then \
		echo "Error: Source path does not exist: $(SOURCE)"; \
		exit 1; \
	fi
	@cmd="uv run python -m memory_connectors.memory_connect $(CONNECTOR) $(SOURCE)"; \
	if [ -n "$(OUTPUT)" ]; then cmd="$$cmd --output $(OUTPUT)"; fi; \
	if [ -n "$(MAX_ITEMS)" ]; then cmd="$$cmd --max-items $(MAX_ITEMS)"; fi; \
	if [ -n "$(TOKEN)" ]; then cmd="$$cmd --token $(TOKEN)"; fi; \
	echo "Running: $$cmd"; \
	$$cmd

# Legacy ChatGPT converter (kept for backwards compatibility)
convert-chatgpt:
	@echo "⚠️  Legacy ChatGPT converter (use 'make connect-memory CONNECTOR=chatgpt' instead)"
	@echo "Usage: make convert-chatgpt EXPORT_PATH=/path/to/chatgpt-export [MAX_CONVERSATIONS=100]"
	@echo ""
	@if [ -z "$(EXPORT_PATH)" ]; then \
		echo "Error: EXPORT_PATH is required"; \
		echo "Example: make convert-chatgpt EXPORT_PATH=/Users/username/Downloads/chatgpt-export"; \
		exit 1; \
	fi
	@if [ ! -e "$(EXPORT_PATH)" ]; then \
		echo "Error: Export path does not exist: $(EXPORT_PATH)"; \
		exit 1; \
	fi
	@cmd="uv run python -m memory_connectors.memory_connect chatgpt $(EXPORT_PATH)"; \
	if [ -n "$(MAX_CONVERSATIONS)" ]; then cmd="$$cmd --max-items $(MAX_CONVERSATIONS)"; fi; \
	echo "🔄 Redirecting to new connector system..."; \
	echo "Running: $$cmd"; \
	$$cmd

# Retired legacy HTTP wrapper
serve-http:
	@echo "ERROR: serve-http used a retired unauthenticated compatibility wrapper."
	@echo "Use 'make serve-mcp-http' for the primary loopback HTTP transport."
	@false

# Primary loopback MCP HTTP server. Remote production remains unsupported.
serve-mcp-http:
	FASTMCP_LOG_LEVEL=INFO MCP_TRANSPORT=http MCP_HOST=127.0.0.1 uv run python -m mcp_server.server

# Competitive multi-adapter benchmark (recall@k / MRR / prohibited-recall / citations)
bench:
	uv run python -m benchmarks.compare_runner

# Compare two runs: make bench-compare OLD=artifacts/<run-old> NEW=artifacts/<run-new>
bench-compare:
	uv run python -m benchmarks.compare_runner compare $(OLD) $(NEW)

# Frozen BM-0 contract harness (upstream acceptance proof)
bm0:
	uv run python -m benchmarks.runner --output-root artifacts/bm0
