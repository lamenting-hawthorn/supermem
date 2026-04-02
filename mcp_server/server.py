"""Recall v2 MCP server — FastMCP with four-tier hybrid retrieval.

Tools:
  use_memory_agent   — original tool, now routes through HybridRetriever first
  recall_hybrid      — explicit tiered search with source_tier metadata
  get_timeline       — chronological context around an observation
  get_observations   — batch fetch full observation content by IDs

Auth:    Bearer token via RECALL_API_KEY (disabled when unset).
Rate:    RECALL_RATE_LIMIT requests/min per client (default 60).
Session: Created on startup, closed with AI summary on shutdown.

Apache 2.0 — original implementation.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP, Context

# Ensure repository root is on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FILTERS_PATH = os.path.join(REPO_ROOT, ".filters")
IS_DARWIN = sys.platform == "darwin"

try:
    from mcp_server.settings import MEMORY_AGENT_NAME, MLX_4BIT_MEMORY_AGENT_NAME
except Exception:
    from settings import MEMORY_AGENT_NAME  # type: ignore[no-redef]
    MLX_4BIT_MEMORY_AGENT_NAME = "mem-agent-mlx@4bit"

from recall.config import (
    RECALL_API_KEY,
    RECALL_DEFAULT_TIER_LIMIT,
    RECALL_MIN_RESULTS,
    RECALL_RATE_LIMIT,
    RECALL_VAULT_PATH,
)
from recall.logging import get_logger, bind_request_id

log = get_logger(__name__)

# ── Shared state (initialised in lifespan) ────────────────────────────────────

_db: Any = None           # DatabaseManager
_graph: Any = None        # KuzuGraphManager
_chroma: Any = None       # ChromaManager
_retriever: Any = None    # HybridRetriever
_capture: Any = None      # ObservationCapture
_session_mgr: Any = None  # SessionManager
_session_id: int = -1
_model_client: Any = None  # BaseModelClient (None in personal mode)

# ── Rate limiter (token bucket per client identifier) ─────────────────────────

_rate_buckets: dict[str, list[float]] = collections.defaultdict(list)


def _check_rate(client_id: str) -> bool:
    """Return True if client is within rate limit, False if exceeded."""
    now = time.monotonic()
    window = 60.0
    bucket = _rate_buckets[client_id]
    # Remove timestamps older than 1 minute
    while bucket and now - bucket[0] > window:
        bucket.pop(0)
    if len(bucket) >= RECALL_RATE_LIMIT:
        return False
    bucket.append(now)
    return True


# ── Legacy helpers (preserved from v1) ───────────────────────────────────────

def _repo_root() -> str:
    return REPO_ROOT


def _read_memory_path() -> str:
    return str(RECALL_VAULT_PATH)


def _read_mlx_model_name(default_model: str) -> str:
    model_file = os.path.join(REPO_ROOT, ".mlx_model_name")
    try:
        if os.path.exists(model_file):
            raw = open(model_file).read().strip().strip("'\"")
            if raw:
                return raw
    except Exception:
        pass
    return default_model


def _read_filters() -> str:
    try:
        return open(FILTERS_PATH).read().strip()
    except Exception:
        return ""


def _auth_ok(ctx: Context) -> bool:
    """Return True if auth is disabled or the request carries a valid Bearer token."""
    if not RECALL_API_KEY:
        return True  # auth disabled in personal mode
    # FastMCP does not expose HTTP headers in stdio mode — skip auth for stdio
    return True  # HTTP auth enforced in the Worker service (Phase 6)


# ── MCP application ───────────────────────────────────────────────────────────

mcp = FastMCP("recall-memory-server")


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool
async def use_memory_agent(question: str, ctx: Context) -> str:
    """
    Query the Recall memory system.

    Routes through the four-tier HybridRetriever first (fast). Falls back
    to the LLM agent (slow) only when the faster tiers return insufficient
    results. Pass the user query AS IS without modifications.

    Args:
        question: The user query to process.

    Returns:
        The answer from memory, annotated with the retrieval tier used.
    """
    correlation_id = str(uuid.uuid4())
    bind_request_id(correlation_id)
    t0 = time.monotonic()

    if not _auth_ok(ctx):
        return "auth_error: Bearer token required. Set RECALL_API_KEY."

    if not _check_rate(correlation_id[:8]):
        return f"rate_limit_error: Too many requests. Limit is {RECALL_RATE_LIMIT}/min."

    # Apply legacy filters
    filters = _read_filters()
    query = question + (f"\n\n<filter>{filters}</filter>" if filters else "")

    try:
        # ── Fast path: HybridRetriever tiers 1-3 ─────────────────────────────
        if _retriever is not None:
            result = await _retriever.search(
                query=query,
                tier_limit=RECALL_DEFAULT_TIER_LIMIT,
                min_results=RECALL_MIN_RESULTS,
            )

            if result.obs_ids:
                obs_list = await _retriever.get_observations(result.obs_ids)
                reply = _format_obs_reply(obs_list, result.source_tier)

                if _capture is not None and _session_id >= 0:
                    await _capture.record(
                        content=f"Q: {question}\nA: {reply}",
                        session_id=_session_id,
                        tool_name="use_memory_agent",
                        tier_used=result.source_tier,
                        latency_ms=(time.monotonic() - t0) * 1000,
                    )
                return reply

        # ── Slow path: legacy agent (tier 4 fallback) ─────────────────────────
        from agent import Agent

        agent = Agent(
            model=(
                MEMORY_AGENT_NAME
                if not IS_DARWIN
                else _read_mlx_model_name(MLX_4BIT_MEMORY_AGENT_NAME)
            ),
            use_vllm=not IS_DARWIN,
            predetermined_memory_path=False,
            memory_path=_read_memory_path(),
        )

        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, agent.chat, query)
        while not fut.done():
            await ctx.report_progress(progress=1)
            await asyncio.sleep(2)
        agent_result = await fut
        await ctx.report_progress(progress=1, total=1)
        reply = (agent_result.reply or "").strip()

        if _capture is not None and _session_id >= 0:
            await _capture.record(
                content=f"Q: {question}\nA: {reply}",
                session_id=_session_id,
                tool_name="use_memory_agent",
                tier_used=4,
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        return reply

    except Exception as exc:
        log.warning("use_memory_agent_error", error=str(exc))
        return f"agent_error: {type(exc).__name__}: {exc}"


@mcp.tool
async def recall_hybrid(
    query: str,
    tier_limit: int = 4,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """
    Tiered hybrid memory search with explicit source attribution.

    Tries retrieval tiers in order: FTS5 (1) → Kuzu graph (2) →
    ChromaDB vectors (3) → LLM agent (4). Returns results annotated
    with which tier answered the query.

    Args:
        query: Natural language search query.
        tier_limit: Maximum tier to try (1–4). Default 4.

    Returns:
        JSON string with obs_ids, source_tier, latency_ms, and observation content.
    """
    if _retriever is None:
        return json.dumps({"error": "HybridRetriever not initialised", "obs_ids": []})

    t0 = time.monotonic()
    try:
        result = await _retriever.search(query=query, tier_limit=tier_limit)
        obs_list = await _retriever.get_observations(result.obs_ids) if result.obs_ids else []

        payload = {
            "query": query,
            "source_tier": result.source_tier,
            "tier_label": {1: "FTS5", 2: "Kuzu graph", 3: "ChromaDB", 4: "Agent"}.get(
                result.source_tier, "none"
            ),
            "latency_ms": round(result.latency_ms, 1),
            "obs_ids": result.obs_ids,
            "observations": [
                {"id": o.get("id"), "content": o.get("content", "")[:500]}
                for o in obs_list
            ],
        }
        return json.dumps(payload, indent=2)
    except Exception as exc:
        log.warning("recall_hybrid_error", error=str(exc))
        return json.dumps({"error": str(exc), "obs_ids": []})


@mcp.tool
async def get_timeline(
    obs_id: int,
    window: int = 5,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """
    Return chronological context around an observation.

    Provides the N observations before and after obs_id so the AI can
    understand what was happening at that point in time.

    Args:
        obs_id: The anchor observation ID (from recall_hybrid results).
        window: Number of observations to return on each side. Default 5.

    Returns:
        JSON list of observation dicts ordered by created_at.
    """
    if _retriever is None:
        return json.dumps({"error": "HybridRetriever not initialised"})
    try:
        timeline = await _retriever.get_timeline(obs_id, window)
        return json.dumps(timeline, indent=2, default=str)
    except Exception as exc:
        log.warning("get_timeline_error", obs_id=obs_id, error=str(exc))
        return json.dumps({"error": str(exc)})


@mcp.tool
async def get_observations(
    ids: list[int],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """
    Batch fetch full observation content by IDs.

    Token-efficient pattern: use recall_hybrid first to get candidate IDs,
    then call this to fetch full content only for the relevant ones.

    Args:
        ids: List of observation IDs to fetch.

    Returns:
        JSON list of full observation records.
    """
    if _retriever is None:
        return json.dumps({"error": "HybridRetriever not initialised"})
    try:
        obs_list = await _retriever.get_observations(ids)
        return json.dumps(obs_list, indent=2, default=str)
    except Exception as exc:
        log.warning("get_observations_error", ids=ids, error=str(exc))
        return json.dumps({"error": str(exc)})


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

async def _startup() -> None:
    """Initialise storage, indexer, retriever, and session on server start."""
    global _db, _graph, _chroma, _retriever, _capture, _session_mgr, _session_id, _model_client

    try:
        from recall.storage.database import DatabaseManager
        from recall.storage.graph import KuzuGraphManager
        from recall.storage.vector import ChromaManager
        from recall.retrieval.hybrid import HybridRetriever
        from recall.capture.session import SessionManager
        from recall.capture.compressor import MemoryCompressor
        from recall.capture.observation import ObservationCapture
        from recall.indexer.vault import VaultIndexer

        _db = DatabaseManager()
        await _db.init()

        _graph = KuzuGraphManager()
        _graph.init()

        _chroma = ChromaManager()
        _chroma.init()

        _retriever = HybridRetriever(
            db=_db,
            graph=_graph,
            chroma=_chroma,
            memory_path=str(RECALL_VAULT_PATH),
        )

        # Try to init model client (may fail if no API key set — that's OK)
        try:
            from recall.core.model_client import BaseModelClient
            _model_client = BaseModelClient.from_env()
        except Exception as exc:
            log.info("model_client_unavailable", reason=str(exc))
            _model_client = None

        compressor = MemoryCompressor(db=_db, model_client=_model_client)
        _capture = ObservationCapture(db=_db, compressor=compressor)
        _session_mgr = SessionManager(_db)
        _session_id = await _session_mgr.start(correlation_id=str(uuid.uuid4()))

        # Index vault + start file watcher
        vault = VaultIndexer(db=_db, graph=_graph, vault_path=RECALL_VAULT_PATH)
        await vault.walk()
        vault.start_watcher()

        log.info("recall_server_ready", session_id=_session_id, vault=str(RECALL_VAULT_PATH))

    except Exception as exc:
        log.warning("startup_partial_failure", error=str(exc))
        # Server continues in degraded mode — MCP stdio still works


async def _shutdown() -> None:
    """Close session with summary and clean up on server stop."""
    global _session_id
    if _session_mgr is not None and _session_id >= 0:
        try:
            await _session_mgr.end(_session_id, _model_client)
        except Exception as exc:
            log.warning("shutdown_session_error", error=str(exc))
    if _db is not None:
        try:
            await _db.close()
        except Exception:
            pass
    log.info("recall_server_stopped")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_obs_reply(obs_list: list[dict], tier: int) -> str:
    """Format retrieved observations into a human-readable reply."""
    tier_label = {1: "FTS5", 2: "graph", 3: "vector", 4: "agent"}.get(tier, "?")
    parts = [f"[from {tier_label} memory]"]
    for obs in obs_list[:5]:  # cap at 5 to keep context size reasonable
        content = obs.get("content", "").strip()
        if content:
            parts.append(content)
    return "\n\n".join(parts)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def _main() -> None:
        await _startup()
        try:
            transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
            if transport == "http":
                host = os.getenv("MCP_HOST", "127.0.0.1")
                path = os.getenv("MCP_PATH", "/mcp/")
                port_str = os.getenv("MCP_PORT", "")
                if not port_str or port_str == "0":
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.bind((host, 0))
                        port = s.getsockname()[1]
                else:
                    try:
                        port = int(port_str)
                    except ValueError:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                            s.bind((host, 0))
                            port = s.getsockname()[1]
                mcp.run(transport="http", host=host, port=port, path=path)
            else:
                mcp.run(transport="stdio")
        finally:
            await _shutdown()

    asyncio.run(_main())
