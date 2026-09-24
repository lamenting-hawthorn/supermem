"""Centralized configuration for supermem.

All environment variables consumed by supermem are defined here.
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

# ── Embedding (tier 3 vector store) ──────────────────────────────────────────
# SUPERMEM_EMBEDDING_PROVIDER: "" → auto: fastembed when importable (declared
#   dependency; the default embedder for the sqlite-vec backend), else Chroma's
#   built-in ONNX MiniLM for the legacy chroma backend, else unavailable;
# "fastembed" → require fastembed's TextEmbedding (bge-small-en-v1.5 default;
#   a missing/failing install reports unavailable rather than falling back);
# "local-endpoint" → POST to an OpenAI-compatible /embeddings endpoint
#   (LM Studio / Ollama serve bge/nomic-embed models) configured via
#   SUPERMEM_EMBEDDING_BASE_URL + SUPERMEM_EMBEDDING_MODEL.
# Unknown values normalize to "".
_EMBEDDING_PROVIDERS = {"", "fastembed", "local-endpoint"}
DEFAULT_FASTEMBED_MODEL: str = "BAAI/bge-small-en-v1.5"
DEFAULT_LOCAL_ENDPOINT_BASE_URL: str = "http://localhost:1234/v1"


def _parse_embedding_provider(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    return val if val in _EMBEDDING_PROVIDERS else ""


def _parse_embedding_model(raw: str | None) -> str:
    return (raw or "").strip()


def _parse_embedding_base_url(raw: str | None) -> str:
    return (raw or "").strip()


def embedding_provider_from_env() -> str:
    return _parse_embedding_provider(os.getenv("SUPERMEM_EMBEDDING_PROVIDER"))


def embedding_model_from_env() -> str:
    return _parse_embedding_model(os.getenv("SUPERMEM_EMBEDDING_MODEL"))


def embedding_base_url_from_env() -> str:
    return _parse_embedding_base_url(os.getenv("SUPERMEM_EMBEDDING_BASE_URL"))


SUPERMEM_EMBEDDING_PROVIDER: str = embedding_provider_from_env()
SUPERMEM_EMBEDDING_MODEL: str = embedding_model_from_env()
SUPERMEM_EMBEDDING_BASE_URL: str = (
    embedding_base_url_from_env() or DEFAULT_LOCAL_ENDPOINT_BASE_URL
)

# ── Vector backend ────────────────────────────────────────────────────────────
# SUPERMEM_VECTOR_BACKEND: "" → auto-select (sqlite backend when sqlite-vec is
# importable, else legacy chroma backend when chromadb is importable, else an
# always-unavailable manager); explicit "sqlite" / "chroma" / "none" overrides.
_VECTOR_BACKENDS = {"", "sqlite", "chroma", "none"}


def _parse_vector_backend(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    return val if val in _VECTOR_BACKENDS else ""


def vector_backend_from_env() -> str:
    return _parse_vector_backend(os.getenv("SUPERMEM_VECTOR_BACKEND"))


SUPERMEM_VECTOR_BACKEND: str = vector_backend_from_env()


# ── Vector relevance floor ──────────────────────────────────────────────────
# SUPERMEM_VECTOR_MAX_DISTANCE: cosine-distance cutoff for the sqlite-vec
# tier. KNN otherwise always returns top-k nearest rows — including
# irrelevant hits for out-of-scope queries — so hits worse than this are
# dropped. Calibrated on the frozen bench corpus for the default embedder
# (BAAI/bge-small-en-v1.5): true hits ≲0.31, noise ≳0.36. Retune per
# embedding model/corpus; "off"/"none" disables the floor.
def _parse_vector_max_distance(raw: str | None) -> float | None:
    val = (raw or "").strip().lower()
    if val in {"off", "none", "disabled", "false"}:
        return None
    try:
        return float(val) if val else 0.35
    except ValueError:
        return 0.35


def vector_max_distance_from_env() -> float | None:
    return _parse_vector_max_distance(os.getenv("SUPERMEM_VECTOR_MAX_DISTANCE"))


SUPERMEM_VECTOR_MAX_DISTANCE: float | None = vector_max_distance_from_env()

# Path of the sqlite-vec store (default: vectors.db beside the main DB so a
# single backup of ~/.supermem covers everything).
SUPERMEM_VECTORS_PATH: Path = Path(
    os.getenv("SUPERMEM_VECTORS_PATH", str(SUPERMEM_DB_PATH.parent / "vectors.db"))
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
SUPERMEM_COMPRESS_BUDGET_CHARS: int = int(
    os.getenv("SUPERMEM_COMPRESS_BUDGET_CHARS", "2000")
)
SUPERMEM_COMPRESS_MIN_COVERAGE: float = float(
    os.getenv("SUPERMEM_COMPRESS_MIN_COVERAGE", "0.6")
)
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
# Tier 4 (LLM agent retrieval) is capped out of the default retrieval ladder;
# enable explicitly via SUPERMEM_MAX_RETRIEVAL_TIER if ever desired.
SUPERMEM_MAX_RETRIEVAL_TIER: int = int(os.getenv("SUPERMEM_MAX_RETRIEVAL_TIER", "3"))
SUPERMEM_DEFAULT_TIER_LIMIT: int = min(
    int(os.getenv("SUPERMEM_DEFAULT_TIER_LIMIT", "3")),
    SUPERMEM_MAX_RETRIEVAL_TIER,
)
# Whether Tier 4 (AgentRetriever) persists its unverified reply as an observation.
# Off by default — agent replies are returned to the caller without being written.
SUPERMEM_TIER4_PERSIST: bool = os.getenv("SUPERMEM_TIER4_PERSIST", "false").lower() in (
    "1",
    "true",
    "yes",
)
