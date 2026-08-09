"""supermem MCP server — FastMCP with lifecycle-aware three-tier retrieval.

Tools:
  use_memory_agent   — original tool, now routes through HybridRetriever first
  supermem_hybrid      — explicit tiered search with source_tier metadata
  get_timeline       — chronological context around an observation
  get_observations   — batch fetch full observation content by IDs
  list_open_tasks    — local open-loop/task extraction
  suggest_followups  — actionable follow-up suggestions
  list_day_summaries — local daily summaries
  retract_observation — mark a memory retracted/forgotten

Auth:    Trusted local stdio is unauthenticated; HTTP requires configured Bearer auth.
Rate:    SUPERMEM_RATE_LIMIT requests/min per client (default 60).
Session: Created on startup, closed with AI summary on shutdown.

Apache 2.0 — original implementation.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import hashlib
import ipaddress
import json
import os
import secrets
import signal
import socket
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastmcp import FastMCP, Context
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Ensure repository root is on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FILTERS_PATH = os.path.join(REPO_ROOT, ".filters")

from supermem.config import (  # noqa: E402
    SUPERMEM_API_KEY,
    SUPERMEM_DEFAULT_TIER_LIMIT,
    SUPERMEM_MAX_RETRIEVAL_TIER,
    SUPERMEM_MIN_RESULTS,
    SUPERMEM_RATE_LIMIT,
    SUPERMEM_VAULT_PATH,
)  # noqa: E402
from supermem.logging import get_logger, bind_request_id  # noqa: E402

log = get_logger(__name__)

# ── Shared state (initialised in lifespan) ────────────────────────────────────


@dataclasses.dataclass
class ServerContext:
    db: Any = None  # DatabaseManager
    graph: Any = None  # KuzuGraphManager
    chroma: Any = None  # ChromaManager
    retriever: Any = None  # HybridRetriever
    capture: Any = None  # ObservationCapture
    session_mgr: Any = None  # SessionManager
    session_id: int = -1
    model_client: Any = None  # BaseModelClient (None in personal mode)


_ctx = ServerContext()

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
    if len(bucket) >= SUPERMEM_RATE_LIMIT:
        return False
    bucket.append(now)
    return True


# ── Legacy helpers (preserved from v1) ───────────────────────────────────────


def _repo_root() -> str:
    return REPO_ROOT


def _read_filters() -> str:
    try:
        return open(FILTERS_PATH).read().strip()
    except Exception:
        return ""


def _transport_is_stdio() -> bool:
    return os.getenv("MCP_TRANSPORT", "stdio").strip().lower() == "stdio"


_TRUSTED_LOCAL_STDIO = object()


def _request_context(ctx: Context | object | None) -> tuple[Any | None, bool]:
    """Return ``(HTTP request, is trusted local stdio)`` for an MCP context."""
    if ctx is _TRUSTED_LOCAL_STDIO:
        return None, _transport_is_stdio()
    if type(ctx) is not Context:
        # Absent and compatibility-only objects are not transport identities.
        return None, False
    primary_context = cast(Context, ctx)
    try:
        if primary_context.fastmcp is not mcp:
            # Exact contexts owned by a different FastMCP instance are not ours.
            return None, False
    except Exception:
        return None, False
    try:
        request = primary_context.request_context.request
    except (LookupError, RuntimeError, ValueError):
        # A Context object outside an active FastMCP request is not stdio proof.
        return None, False
    except Exception as exc:
        log.warning("auth_context_error", error=str(exc))
        return None, False
    if request is None:
        return None, _transport_is_stdio()
    if type(request) is Request:
        return request, False
    return None, False


def _auth_ok(ctx: Context | object | None) -> bool:
    """Allow trusted local stdio or HTTP with the configured Bearer token."""
    request, is_stdio = _request_context(ctx)
    if is_stdio:
        return True
    if request is None or not SUPERMEM_API_KEY:
        return False
    auth_header = request.headers.get("authorization", "")
    return secrets.compare_digest(auth_header, f"Bearer {SUPERMEM_API_KEY}")


class PrimaryHTTPAuthMiddleware:
    """Reject unauthenticated HTTP before FastMCP allocates a session."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            auth_header = Request(scope).headers.get("authorization", "")
            expected = f"Bearer {SUPERMEM_API_KEY}"
            if not SUPERMEM_API_KEY or not secrets.compare_digest(
                auth_header, expected
            ):
                response = JSONResponse(
                    {"error": "unauthorized", "detail": "Bearer token required."},
                    status_code=401,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def primary_http_middleware() -> list[Middleware]:
    """Return primary-server ASGI middleware for authenticated HTTP only."""
    return [Middleware(PrimaryHTTPAuthMiddleware)]


def create_primary_http_app(
    path: str = "/mcp",
    *,
    json_response: bool | None = None,
):
    """Build the authenticated, stateless primary HTTP ASGI application.

    This helper is deliberately the same middleware configuration passed to
    ``mcp.run_async(transport="http", ...)`` below, so ASGI tests cover the
    primary server boundary rather than a tool-only approximation of it. The
    local HTTP profile deliberately does not retain MCP transport sessions.
    """
    return mcp.http_app(
        path=path,
        middleware=primary_http_middleware(),
        json_response=json_response,
        stateless_http=True,
    )


def _validated_loopback_host(value: str | None) -> str:
    """Return an allowed loopback bind host or reject it before socket setup."""
    host = (value or "127.0.0.1").strip()
    if not host:
        host = "127.0.0.1"
    if host.lower() == "localhost":
        return "localhost"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            "MCP_HOST must be localhost or a loopback IP literal."
        ) from exc
    if not address.is_loopback:
        raise ValueError("MCP_HOST must be localhost or a loopback IP literal.")
    return host


def _loopback_socket_family(host: str) -> socket.AddressFamily:
    """Choose the matching address family for an already validated host."""
    if host.lower() != "localhost" and ipaddress.ip_address(host).version == 6:
        return socket.AF_INET6
    return socket.AF_INET


def _ephemeral_loopback_port(host: str) -> int:
    """Reserve an available loopback port long enough to select it for FastMCP."""
    with socket.socket(_loopback_socket_family(host), socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _client_id(ctx: Context | object | None) -> str:
    """Best-effort client identity for rate limiting, shared across all tools."""
    request, is_stdio = _request_context(ctx)
    if is_stdio:
        return "local"
    if request is not None:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            digest = hashlib.sha256(auth_header[7:].encode()).hexdigest()[:16]
            return f"bearer:{digest}"
        host = getattr(request.client, "host", "unknown")
        return f"http:{host}"
    return "unknown"


def _retrieval_tier_limit(ctx: Context | object | None, requested: int) -> int:
    """Cap every caller at the lifecycle-aware Tier 3 retrieval boundary."""
    del ctx
    return min(requested, SUPERMEM_MAX_RETRIEVAL_TIER)


def _guard_tool(ctx: Context | object | None, tool_name: str) -> str | None:
    """Apply common MCP auth and rate limiting; return an error string on denial."""
    if not _auth_ok(ctx):
        return "auth_error: Bearer token required. Set SUPERMEM_API_KEY."
    if not _check_rate(_client_id(ctx)):
        return (
            f"rate_limit_error: Too many requests. Limit is {SUPERMEM_RATE_LIMIT}/min."
        )
    return None


def _validate_int_bounds(
    value: int, name: str, minimum: int, maximum: int
) -> str | None:
    if value < minimum or value > maximum:
        return f"{name} must be between {minimum} and {maximum}."
    return None


# ── MCP application ───────────────────────────────────────────────────────────


@asynccontextmanager
async def _supermem_lifespan(_: FastMCP) -> AsyncIterator[dict[str, object]]:
    """Let FastMCP own startup and shutdown for every supported transport."""
    try:
        await _startup()
        yield {}
    finally:
        await _shutdown()


mcp = FastMCP("supermem-server", lifespan=_supermem_lifespan)


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool
async def use_memory_agent(question: str, ctx: Context) -> str:
    """
    Query the supermem memory system.

    Routes through lifecycle-aware HybridRetriever tiers 1-3. Tier 4 / raw
    Agent vault navigation is unavailable until a source-aware broker exists.
    Pass the user query AS IS.

    Args:
        question: The user query to process.

    Returns:
        The answer from memory, annotated with the retrieval tier used.
    """
    correlation_id = str(uuid.uuid4())
    bind_request_id(correlation_id)
    t0 = time.monotonic()

    denial = _guard_tool(ctx, "use_memory_agent")
    if denial:
        return denial

    # Apply legacy filters
    filters = _read_filters()
    query = question + (f"\n\n<filter>{filters}</filter>" if filters else "")

    try:
        # ── Supported path: lifecycle-aware HybridRetriever tiers 1-3 ───────
        if _ctx.retriever is not None:
            result = await _ctx.retriever.search(
                query=query,
                tier_limit=_retrieval_tier_limit(ctx, SUPERMEM_DEFAULT_TIER_LIMIT),
                min_results=SUPERMEM_MIN_RESULTS,
            )

            if result.obs_ids:
                obs_list = await _ctx.retriever.get_observations(result.obs_ids)
                reply = _format_obs_reply(obs_list, result.source_tier)

                if _ctx.capture is not None and _ctx.session_id >= 0:
                    await _ctx.capture.record(
                        content=f"Q: {question}\nA: {reply}",
                        session_id=_ctx.session_id,
                        tool_name="use_memory_agent",
                        tier_used=result.source_tier,
                        latency_ms=(time.monotonic() - t0) * 1000,
                    )
                return reply

        return "No eligible memory results found."

    except Exception as exc:
        log.warning("use_memory_agent_error", error=str(exc))
        return f"agent_error: {type(exc).__name__}: {exc}"


@mcp.tool
async def supermem_hybrid(
    query: str,
    tier_limit: int = SUPERMEM_MAX_RETRIEVAL_TIER,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """
    Tiered hybrid memory search with explicit source attribution.

    Tries lifecycle-aware retrieval tiers in order: FTS5 (1) → Kuzu graph (2)
    → ChromaDB vectors (3). Raw Agent vault navigation (Tier 4) is unavailable
    until a source-aware lifecycle broker exists.

    Args:
        query: Natural language search query.
        tier_limit: Maximum tier to try (1–3). Requests above 3 are capped.

    Returns:
        JSON string with obs_ids, source_tier, latency_ms, and observation content.
    """
    denial = _guard_tool(ctx, "supermem_hybrid")
    if denial:
        return json.dumps({"error": denial, "obs_ids": []})
    if _ctx.retriever is None:
        return json.dumps({"error": "HybridRetriever not initialised", "obs_ids": []})

    try:
        result = await _ctx.retriever.search(
            query=query, tier_limit=_retrieval_tier_limit(ctx, tier_limit)
        )
        obs_list = (
            await _ctx.retriever.get_observations(result.obs_ids)
            if result.obs_ids
            else []
        )

        payload = {
            "query": query,
            "source_tier": result.source_tier,
            "tier_label": {1: "FTS5", 2: "Kuzu graph", 3: "ChromaDB"}.get(
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
        log.warning("supermem_hybrid_error", error=str(exc))
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
        obs_id: The anchor observation ID (from supermem_hybrid results).
        window: Number of observations to return on each side. Default 5.

    Returns:
        JSON list of observation dicts ordered by created_at.
    """
    denial = _guard_tool(ctx, "get_timeline")
    if denial:
        return json.dumps({"error": denial})
    if _ctx.retriever is None:
        return json.dumps({"error": "HybridRetriever not initialised"})
    try:
        timeline = await _ctx.retriever.get_timeline(obs_id, window)
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

    Token-efficient pattern: use supermem_hybrid first to get candidate IDs,
    then call this to fetch full content only for the relevant ones.

    Args:
        ids: List of observation IDs to fetch.

    Returns:
        JSON list of full observation records.
    """
    denial = _guard_tool(ctx, "get_observations")
    if denial:
        return json.dumps({"error": denial})
    if _ctx.retriever is None:
        return json.dumps({"error": "HybridRetriever not initialised"})
    try:
        obs_list = await _ctx.retriever.get_observations(ids)
        return json.dumps(obs_list, indent=2, default=str)
    except Exception as exc:
        log.warning("get_observations_error", ids=ids, error=str(exc))
        return json.dumps({"error": str(exc)})


@mcp.tool
async def list_open_tasks(
    days: int = 14,
    limit: int = 20,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """List likely unresolved tasks/open loops from recent local memory."""
    denial = _guard_tool(ctx, "list_open_tasks")
    if denial:
        return json.dumps({"error": denial, "tasks": []})
    for error in (
        _validate_int_bounds(days, "days", 1, 90),
        _validate_int_bounds(limit, "limit", 1, 100),
    ):
        if error:
            return json.dumps({"error": error, "tasks": []})
    if _ctx.db is None:
        return json.dumps({"error": "Database not initialised", "tasks": []})
    try:
        from supermem.capture.insights import extract_open_tasks

        observations = await _ctx.db.get_recent_observations_by_age(
            days=days, limit=1000
        )
        tasks = extract_open_tasks(observations, limit=limit)
        return json.dumps({"days": days, "tasks": tasks}, indent=2, default=str)
    except Exception as exc:
        log.warning("list_open_tasks_error", error=str(exc))
        return json.dumps({"error": str(exc), "tasks": []})


@mcp.tool
async def suggest_followups(
    days: int = 14,
    limit: int = 10,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Suggest next actions for likely unresolved tasks in recent memory."""
    denial = _guard_tool(ctx, "suggest_followups")
    if denial:
        return json.dumps({"error": denial, "suggestions": []})
    for error in (
        _validate_int_bounds(days, "days", 1, 90),
        _validate_int_bounds(limit, "limit", 1, 50),
    ):
        if error:
            return json.dumps({"error": error, "suggestions": []})
    if _ctx.db is None:
        return json.dumps({"error": "Database not initialised", "suggestions": []})
    try:
        from supermem.capture.insights import (
            extract_open_tasks,
            suggest_followups as build,
        )

        observations = await _ctx.db.get_recent_observations_by_age(
            days=days, limit=1000
        )
        tasks = extract_open_tasks(observations, limit=limit)
        suggestions = build(tasks, limit=limit)
        return json.dumps(
            {"days": days, "suggestions": suggestions}, indent=2, default=str
        )
    except Exception as exc:
        log.warning("suggest_followups_error", error=str(exc))
        return json.dumps({"error": str(exc), "suggestions": []})


@mcp.tool
async def list_day_summaries(
    days: int = 7,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Return local day summaries with keywords, highlights, and open-loop counts."""
    denial = _guard_tool(ctx, "list_day_summaries")
    if denial:
        return json.dumps({"error": denial, "summaries": []})
    error = _validate_int_bounds(days, "days", 1, 31)
    if error:
        return json.dumps({"error": error, "summaries": []})
    if _ctx.db is None:
        return json.dumps({"error": "Database not initialised", "summaries": []})
    try:
        from supermem.capture.insights import summarize_days

        observations = await _ctx.db.get_recent_observations_by_age(
            days=days, limit=2000
        )
        summaries = summarize_days(observations, days=days)
        return json.dumps({"days": days, "summaries": summaries}, indent=2, default=str)
    except Exception as exc:
        log.warning("list_day_summaries_error", error=str(exc))
        return json.dumps({"error": str(exc), "summaries": []})


@mcp.tool
async def retract_observation(
    obs_id: int,
    reason: str = "",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Retract an observation so it no longer appears in retrieval results."""
    denial = _guard_tool(ctx, "retract_observation")
    if denial:
        return json.dumps({"error": denial, "retracted": False})
    if _ctx.db is None:
        return json.dumps({"error": "Database not initialised", "retracted": False})
    try:
        retracted = await _ctx.db.retract_observation(obs_id, reason=reason or None)
        if retracted and _ctx.chroma is not None:
            try:
                await _ctx.chroma.delete_obs(obs_id)
            except Exception as exc:
                log.warning(
                    "retract_vector_delete_failed", obs_id=obs_id, error=str(exc)
                )
        return json.dumps({"obs_id": obs_id, "retracted": retracted}, indent=2)
    except Exception as exc:
        log.warning("retract_observation_error", obs_id=obs_id, error=str(exc))
        return json.dumps({"error": str(exc), "retracted": False})


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────


async def _startup() -> None:
    """Initialise storage, indexer, retriever, and session on server start.

    Critical failures (database unavailable) are re-raised so the caller knows
    the server is non-functional. Non-critical failures (graph, vector, vault
    indexer) are logged and the server continues in degraded mode.
    """
    from supermem.storage.database import DatabaseManager
    from supermem.storage.graph import KuzuGraphManager
    from supermem.storage.vector import ChromaManager
    from supermem.retrieval.hybrid import HybridRetriever
    from supermem.capture.session import SessionManager
    from supermem.capture.compressor import MemoryCompressor
    from supermem.capture.observation import ObservationCapture
    from supermem.indexer.vault import VaultIndexer

    # ── Critical: database must succeed ──────────────────────────────────────
    _ctx.db = DatabaseManager()
    await _ctx.db.init()  # raises StorageError on failure

    # ── Non-critical: graph + vector (degrade gracefully if unavailable) ─────
    # KuzuGraphManager.init() handles "kuzu not installed" internally (available=False).
    # We only catch unexpected init errors so the server keeps running.
    _ctx.graph = KuzuGraphManager()
    try:
        _ctx.graph.init()
    except Exception as exc:
        log.warning("graph_init_failed", error=str(exc))

    _ctx.chroma = ChromaManager()
    try:
        _ctx.chroma.init()
    except Exception as exc:
        log.warning("chroma_init_failed", error=str(exc))
        _ctx.chroma = None

    _ctx.retriever = HybridRetriever(
        db=_ctx.db,
        graph=_ctx.graph,
        chroma=_ctx.chroma,
        memory_path=str(SUPERMEM_VAULT_PATH),
    )

    # ── Non-critical: model client (optional, for compression/summaries) ─────
    try:
        from supermem.core.model_client import BaseModelClient

        _ctx.model_client = BaseModelClient.from_env()
    except Exception as exc:
        log.info("model_client_unavailable", reason=str(exc))
        _ctx.model_client = None

    compressor = MemoryCompressor(db=_ctx.db, model_client=_ctx.model_client)
    _ctx.capture = ObservationCapture(db=_ctx.db, compressor=compressor)
    _ctx.session_mgr = SessionManager(_ctx.db)
    _ctx.session_id = await _ctx.session_mgr.start(correlation_id=str(uuid.uuid4()))

    # ── Non-critical: vault indexer ───────────────────────────────────────────
    try:
        vault = VaultIndexer(
            db=_ctx.db, graph=_ctx.graph, vault_path=SUPERMEM_VAULT_PATH
        )
        await vault.walk()
        vault.start_watcher()
    except Exception as exc:
        log.warning("vault_indexer_unavailable", error=str(exc))

    log.info(
        "supermem_server_ready",
        session_id=_ctx.session_id,
        vault=str(SUPERMEM_VAULT_PATH),
    )


async def _shutdown() -> None:
    """Close session with summary and clean up on server stop."""
    if _ctx.session_mgr is not None and _ctx.session_id >= 0:
        try:
            await _ctx.session_mgr.end(_ctx.session_id, _ctx.model_client)
        except Exception as exc:
            log.warning("shutdown_session_error", error=str(exc))
    if _ctx.db is not None:
        try:
            await _ctx.db.close()
        except Exception:
            pass
    log.info("supermem_server_stopped")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _format_obs_reply(obs_list: list[dict], tier: int) -> str:
    """Format retrieved observations into a human-readable reply."""
    tier_label = {1: "FTS5", 2: "graph", 3: "vector"}.get(tier, "?")
    parts = [f"[from {tier_label} memory]"]
    for obs in obs_list[:5]:  # cap at 5 to keep context size reasonable
        content = obs.get("content", "").strip()
        if content:
            parts.append(content)
    return "\n\n".join(parts)


# ── Entry point ───────────────────────────────────────────────────────────────


async def _main() -> None:
    """Run one supported FastMCP transport inside its own async lifecycle."""
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "http":
        if not SUPERMEM_API_KEY:
            raise ValueError(
                "SUPERMEM_API_KEY is required for the primary MCP HTTP transport."
            )
        host = _validated_loopback_host(os.getenv("MCP_HOST"))
        path = os.getenv("MCP_PATH", "/mcp/")
        port_str = os.getenv("MCP_PORT", "")
        if not port_str or port_str == "0":
            port = _ephemeral_loopback_port(host)
        else:
            try:
                port = int(port_str)
            except ValueError:
                port = _ephemeral_loopback_port(host)
        await mcp.run_async(
            transport="http",
            host=host,
            port=port,
            path=path,
            middleware=primary_http_middleware(),
            stateless_http=True,
        )
    elif transport == "stdio":
        await mcp.run_async(transport="stdio")
    else:
        raise ValueError("Unsupported MCP_TRANSPORT; expected 'stdio' or 'http'.")


def _run_entrypoint() -> None:
    """Bridge Uvicorn's restored SIGTERM into FastMCP lifespan cleanup."""
    termination_signal: int | None = None

    def _sigterm_bridge(signum: int, _frame: object) -> None:
        nonlocal termination_signal
        termination_signal = signum
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, _sigterm_bridge)
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        # Preserve ordinary Ctrl-C behavior; consume only the SIGTERM bridge.
        if termination_signal is None:
            raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)

    if termination_signal is not None:
        raise SystemExit(128 + termination_signal)


if __name__ == "__main__":
    _run_entrypoint()
