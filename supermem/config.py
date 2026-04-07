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

SUPERMEM_LLM_PROVIDER: str = os.getenv("SUPERMEM_LLM_PROVIDER", "openrouter").lower()
SUPERMEM_LLM_MODEL: str = os.getenv("SUPERMEM_LLM_MODEL", "")

OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL: str = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
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

_default_db = Path.home() / ".supermem" / "supermem.db"
SUPERMEM_DB_PATH: Path = Path(os.getenv("SUPERMEM_DB_PATH", str(_default_db)))

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


SUPERMEM_VAULT_PATH: Path = Path(
    os.getenv("SUPERMEM_VAULT_PATH", str(_read_vault_path()))
)

_default_kuzu = Path.home() / ".supermem" / "graph"
SUPERMEM_KUZU_PATH: Path = Path(os.getenv("SUPERMEM_KUZU_PATH", str(_default_kuzu)))

_default_chroma = Path.home() / ".supermem" / "chroma"
SUPERMEM_CHROMA_PATH: Path = Path(
    os.getenv("SUPERMEM_CHROMA_PATH", str(_default_chroma))
)

# ── Feature flags ─────────────────────────────────────────────────────────────

SUPERMEM_VECTOR: bool = os.getenv("SUPERMEM_VECTOR", "false").lower() == "true"

# ── Agent / sandbox ───────────────────────────────────────────────────────────

SUPERMEM_MAX_TOOL_TURNS: int = int(os.getenv("SUPERMEM_MAX_TOOL_TURNS", "20"))
SUPERMEM_SANDBOX_TIMEOUT: int = int(os.getenv("SUPERMEM_SANDBOX_TIMEOUT", "20"))

# ── Memory limits ─────────────────────────────────────────────────────────────

SUPERMEM_FILE_SIZE_LIMIT: int = 1 * 1024 * 1024
SUPERMEM_DIR_SIZE_LIMIT: int = 10 * 1024 * 1024
SUPERMEM_MEMORY_SIZE_LIMIT: int = 100 * 1024 * 1024

# ── Capture / compression ─────────────────────────────────────────────────────

SUPERMEM_COMPRESS_EVERY: int = int(os.getenv("SUPERMEM_COMPRESS_EVERY", "50"))
# TTL for regular observations in days (0 = no expiry)
SUPERMEM_OBS_TTL_DAYS: int = int(os.getenv("SUPERMEM_OBS_TTL_DAYS", "90"))

# ── Auth & rate limiting ──────────────────────────────────────────────────────

SUPERMEM_API_KEY: str = os.getenv("SUPERMEM_API_KEY", "")
SUPERMEM_RATE_LIMIT: int = int(os.getenv("SUPERMEM_RATE_LIMIT", "60"))

# ── Worker ────────────────────────────────────────────────────────────────────

SUPERMEM_WORKER_PORT: int = int(os.getenv("SUPERMEM_WORKER_PORT", "37777"))
SUPERMEM_WORKER_HOST: str = os.getenv("SUPERMEM_WORKER_HOST", "127.0.0.1")

# ── Retrieval ─────────────────────────────────────────────────────────────────

SUPERMEM_MIN_RESULTS: int = int(os.getenv("SUPERMEM_MIN_RESULTS", "3"))
SUPERMEM_DEFAULT_TIER_LIMIT: int = int(os.getenv("SUPERMEM_DEFAULT_TIER_LIMIT", "4"))
