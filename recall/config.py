"""Centralized configuration for Recall v2.

All environment variables consumed by Recall are defined here.
Other modules import from this file — they do NOT call os.getenv() directly.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── LLM provider ──────────────────────────────────────────────────────────────

RECALL_LLM_PROVIDER: str = os.getenv("RECALL_LLM_PROVIDER", "openrouter").lower()
RECALL_LLM_MODEL: str = os.getenv("RECALL_LLM_MODEL", "")

OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_DEFAULT_MODEL: str = "anthropic/claude-sonnet-4"

OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_DEFAULT_MODEL: str = os.getenv("OLLAMA_DEFAULT_MODEL", "llama3.2")

VLLM_HOST: str = os.getenv("VLLM_HOST", "0.0.0.0")
VLLM_PORT: int = int(os.getenv("VLLM_PORT", "8000"))
VLLM_DEFAULT_MODEL: str = os.getenv("VLLM_DEFAULT_MODEL", "driaforall/mem-agent")

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_DEFAULT_MODEL: str = os.getenv("ANTHROPIC_DEFAULT_MODEL", "claude-sonnet-4-5")

LMSTUDIO_HOST: str = os.getenv("LMSTUDIO_HOST", "http://localhost:1234")
LMSTUDIO_DEFAULT_MODEL: str = os.getenv("LMSTUDIO_DEFAULT_MODEL", "")

# ── Storage ───────────────────────────────────────────────────────────────────

_default_db = Path.home() / ".recall" / "recall.db"
RECALL_DB_PATH: Path = Path(os.getenv("RECALL_DB_PATH", str(_default_db)))

_repo_root = Path(__file__).resolve().parent.parent
_memory_path_file = _repo_root / ".memory_path"


def _read_vault_path() -> Path:
    default = _repo_root / "memory" / "mcp-server"
    try:
        if _memory_path_file.exists():
            raw = _memory_path_file.read_text().strip()
            raw = os.path.expanduser(os.path.expandvars(raw))
            p = Path(raw) if os.path.isabs(raw) else (_repo_root / raw).resolve()
            if p.is_dir():
                return p
    except Exception:
        pass
    return default


RECALL_VAULT_PATH: Path = Path(os.getenv("RECALL_VAULT_PATH", str(_read_vault_path())))

_default_kuzu = Path.home() / ".recall" / "graph"
RECALL_KUZU_PATH: Path = Path(os.getenv("RECALL_KUZU_PATH", str(_default_kuzu)))

_default_chroma = Path.home() / ".recall" / "chroma"
RECALL_CHROMA_PATH: Path = Path(os.getenv("RECALL_CHROMA_PATH", str(_default_chroma)))

# ── Feature flags ─────────────────────────────────────────────────────────────

RECALL_VECTOR: bool = os.getenv("RECALL_VECTOR", "false").lower() == "true"

# ── Agent / sandbox ───────────────────────────────────────────────────────────

RECALL_MAX_TOOL_TURNS: int = int(os.getenv("RECALL_MAX_TOOL_TURNS", "20"))
RECALL_SANDBOX_TIMEOUT: int = int(os.getenv("RECALL_SANDBOX_TIMEOUT", "20"))

# ── Memory limits ─────────────────────────────────────────────────────────────

RECALL_FILE_SIZE_LIMIT: int = 1 * 1024 * 1024
RECALL_DIR_SIZE_LIMIT: int = 10 * 1024 * 1024
RECALL_MEMORY_SIZE_LIMIT: int = 100 * 1024 * 1024

# ── Capture / compression ─────────────────────────────────────────────────────

RECALL_COMPRESS_EVERY: int = int(os.getenv("RECALL_COMPRESS_EVERY", "50"))

# ── Auth & rate limiting ──────────────────────────────────────────────────────

RECALL_API_KEY: str = os.getenv("RECALL_API_KEY", "")
RECALL_RATE_LIMIT: int = int(os.getenv("RECALL_RATE_LIMIT", "60"))

# ── Worker ────────────────────────────────────────────────────────────────────

RECALL_WORKER_PORT: int = int(os.getenv("RECALL_WORKER_PORT", "37777"))
RECALL_WORKER_HOST: str = os.getenv("RECALL_WORKER_HOST", "127.0.0.1")

# ── Retrieval ─────────────────────────────────────────────────────────────────

RECALL_MIN_RESULTS: int = int(os.getenv("RECALL_MIN_RESULTS", "3"))
RECALL_DEFAULT_TIER_LIMIT: int = int(os.getenv("RECALL_DEFAULT_TIER_LIMIT", "4"))
