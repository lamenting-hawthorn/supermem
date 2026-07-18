"""
Recall v2 Worker HTTP API — optional service on port 37777.

If this service is NOT running, the MCP stdio server operates normally
with zero degradation. Start with: supermem serve --worker

Endpoints:
  GET  /              static/index.html — session viewer + search UI
  GET  /health        liveness + readiness
  GET  /.well-known/oauth-protected-resource  RFC 9728 metadata stub
  GET  /sessions      recent sessions with summaries (paginated)
  GET  /observations  paginated observations, filterable
  POST /search        FTS5 + graph + vector hybrid search
  POST /index/rebuild re-index entire vault
  GET  /backup        stream tar.gz of vault + SQLite snapshot
  GET  /stats         memory metrics
  GET  /open-tasks    local open-loop/task extraction
  GET  /followups     actionable follow-up suggestions
  GET  /day-summaries local daily summaries
  POST /observations/{id}/retract mark an observation retracted

Auth: Bearer token from SUPERMEM_API_KEY header. Disabled when env var unset.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from supermem.capture.observation import ObservationCapture
from supermem.capture.session import SessionManager
from supermem.config import (
    SUPERMEM_API_KEY,
    SUPERMEM_AUTHORIZATION_SERVERS,
    SUPERMEM_DB_PATH,
    SUPERMEM_DEFAULT_TIER_LIMIT,
    SUPERMEM_VAULT_PATH,
    SUPERMEM_WORKER_HOST,
    SUPERMEM_WORKER_PORT,
)
from supermem.indexer.vault import VaultIndexer
from supermem.logging import get_logger
from supermem.retrieval.hybrid import HybridRetriever
from supermem.storage.database import DatabaseManager
from supermem.storage.graph import KuzuGraphManager
from supermem.storage.vector import ChromaManager

log = get_logger(__name__)

# ── Shared state ──────────────────────────────────────────────────────────────

_db: DatabaseManager | None = None
_graph: KuzuGraphManager | None = None
_chroma: ChromaManager | None = None
_retriever: HybridRetriever | None = None

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Recall Worker", version="2.0.0", docs_url="/docs")

_STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
async def _startup() -> None:
    global _db, _graph, _chroma, _retriever
    _db = DatabaseManager()
    await _db.init()
    _graph = KuzuGraphManager()
    _graph.init()
    _chroma = ChromaManager()
    _chroma.init()
    _retriever = HybridRetriever(db=_db, graph=_graph, chroma=_chroma)
    log.info("worker_started", port=SUPERMEM_WORKER_PORT)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _db:
        await _db.close()
    log.info("worker_stopped")


# ── Auth dependency ───────────────────────────────────────────────────────────


async def _require_auth(request: Request) -> None:
    if not SUPERMEM_API_KEY:
        return  # auth disabled in personal mode
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != SUPERMEM_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    html_path = _STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Recall Worker</h1><p>static/index.html not found.</p>")


@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata(request: Request) -> JSONResponse:
    """Return RFC 9728-style protected resource metadata for remote MCP clients."""
    resource = str(request.base_url).rstrip("/")
    payload: dict[str, Any] = {
        "resource": resource,
        "scopes_supported": ["supermem.read", "supermem.write", "supermem.admin"],
        "bearer_methods_supported": ["header"],
    }
    if SUPERMEM_AUTHORIZATION_SERVERS:
        payload["authorization_servers"] = SUPERMEM_AUTHORIZATION_SERVERS
    else:
        payload["warning"] = (
            "Set SUPERMEM_AUTHORIZATION_SERVERS to advertise OAuth authorization "
            "servers for remote MCP deployments."
        )
    return JSONResponse(payload)


@app.get("/health")
async def health() -> JSONResponse:
    db_ok = await _db.health() if _db else False
    graph_ok = _graph.available if _graph else False
    vector_ok = _chroma.available if _chroma else False
    status = "ok" if db_ok else "degraded"
    return JSONResponse(
        {
            "status": status,
            "db": db_ok,
            "graph": graph_ok,
            "vector": vector_ok,
            "timestamp": time.time(),
        }
    )


@app.get("/sessions")
async def list_sessions(
    limit: int = Query(20, ge=1, le=200),
    _: None = Depends(_require_auth),
) -> JSONResponse:
    if not _db:
        raise HTTPException(503, "Database not available")
    try:
        async with _db._conn.execute(  # type: ignore[union-attr]
            "SELECT id, started_at, ended_at, summary FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        sessions = []
        for row in rows:
            sid = row[0]
            async with _db._conn.execute(  # type: ignore[union-attr]
                "SELECT COUNT(*) FROM observations WHERE session_id = ?", (sid,)
            ) as cur2:
                obs_count = (await cur2.fetchone())[0]
            sessions.append(
                {
                    "id": sid,
                    "started_at": row[1],
                    "ended_at": row[2],
                    "summary": row[3],
                    "obs_count": obs_count,
                }
            )
        return JSONResponse(sessions)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/observations")
async def list_observations(
    session_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    obs_type: str | None = Query(None),
    _: None = Depends(_require_auth),
) -> JSONResponse:
    if not _db:
        raise HTTPException(503, "Database not available")
    try:
        where_clauses = []
        params: list[Any] = []
        if session_id is not None:
            where_clauses.append("session_id = ?")
            params.append(session_id)
        if obs_type is not None:
            where_clauses.append("type = ?")
            params.append(obs_type)
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params += [limit, offset]
        async with _db._conn.execute(  # type: ignore[union-attr]
            f"SELECT id, session_id, created_at, content, tier_used, latency_ms, type "
            f"FROM observations {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:
        raise HTTPException(500, str(exc))


class SearchRequest(BaseModel):
    query: str
    tier_limit: int = SUPERMEM_DEFAULT_TIER_LIMIT


@app.post("/search")
async def search(
    body: SearchRequest,
    _: None = Depends(_require_auth),
) -> JSONResponse:
    if not _retriever:
        raise HTTPException(503, "Retriever not available")
    try:
        result = await _retriever.search(body.query, tier_limit=body.tier_limit)
        obs_list = (
            await _retriever.get_observations(result.obs_ids) if result.obs_ids else []
        )
        return JSONResponse(
            {
                "query": body.query,
                "source_tier": result.source_tier,
                "tier_label": {1: "FTS5", 2: "graph", 3: "vector", 4: "agent"}.get(
                    result.source_tier, "none"
                ),
                "latency_ms": round(result.latency_ms, 1),
                "obs_ids": result.obs_ids,
                "observations": [
                    {
                        "id": o.get("id"),
                        "content": o.get("content", ""),
                        "type": o.get("type"),
                    }
                    for o in obs_list
                ],
            }
        )
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/open-tasks")
async def open_tasks(
    days: int = Query(14, ge=1, le=90),
    limit: int = Query(20, ge=1, le=100),
    _: None = Depends(_require_auth),
) -> JSONResponse:
    if not _db:
        raise HTTPException(503, "Database not available")
    from supermem.capture.insights import extract_open_tasks

    observations = await _db.get_recent_observations_by_age(days=days, limit=1000)
    return JSONResponse(
        {"days": days, "tasks": extract_open_tasks(observations, limit=limit)}
    )


@app.get("/followups")
async def followups(
    days: int = Query(14, ge=1, le=90),
    limit: int = Query(10, ge=1, le=50),
    _: None = Depends(_require_auth),
) -> JSONResponse:
    if not _db:
        raise HTTPException(503, "Database not available")
    from supermem.capture.insights import extract_open_tasks, suggest_followups

    observations = await _db.get_recent_observations_by_age(days=days, limit=1000)
    tasks = extract_open_tasks(observations, limit=limit)
    return JSONResponse(
        {"days": days, "suggestions": suggest_followups(tasks, limit=limit)}
    )


@app.get("/day-summaries")
async def day_summaries(
    days: int = Query(7, ge=1, le=31),
    _: None = Depends(_require_auth),
) -> JSONResponse:
    if not _db:
        raise HTTPException(503, "Database not available")
    from supermem.capture.insights import summarize_days

    observations = await _db.get_recent_observations_by_age(days=days, limit=2000)
    return JSONResponse(
        {"days": days, "summaries": summarize_days(observations, days=days)}
    )


class RetractionRequest(BaseModel):
    reason: str = ""


@app.post("/observations/{obs_id}/retract")
async def retract_observation_endpoint(
    obs_id: int,
    body: RetractionRequest | None = None,
    _: None = Depends(_require_auth),
) -> JSONResponse:
    if not _db:
        raise HTTPException(503, "Database not available")
    retracted = await _db.retract_observation(
        obs_id, reason=(body.reason if body else None)
    )
    if not retracted:
        raise HTTPException(404, "Observation not found")
    return JSONResponse({"obs_id": obs_id, "retracted": True})


@app.post("/index/rebuild")
async def rebuild_index(_: None = Depends(_require_auth)) -> JSONResponse:
    if not _db or not _graph:
        raise HTTPException(503, "Storage not available")
    try:
        indexer = VaultIndexer(db=_db, graph=_graph, vault_path=SUPERMEM_VAULT_PATH)
        count = await asyncio.wait_for(indexer.walk(), timeout=300)
        return JSONResponse({"status": "ok", "files_indexed": count})
    except asyncio.TimeoutError:
        raise HTTPException(504, "Index rebuild timed out (>300s)")
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/backup")
async def backup(_: None = Depends(_require_auth)) -> StreamingResponse:
    """Stream a tar.gz of the vault markdown files + SQLite snapshot."""
    import datetime

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"supermem_backup_{timestamp}.tar.gz"

    def _generate():
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # Add vault markdown files
            if SUPERMEM_VAULT_PATH.exists():
                for md in SUPERMEM_VAULT_PATH.rglob("*.md"):
                    try:
                        tar.add(
                            str(md),
                            arcname=f"vault/{md.relative_to(SUPERMEM_VAULT_PATH)}",
                        )
                    except Exception:
                        pass
            # Add SQLite DB snapshot
            if SUPERMEM_DB_PATH.exists():
                tar.add(str(SUPERMEM_DB_PATH), arcname="supermem.db")
        buf.seek(0)
        yield buf.read()

    return StreamingResponse(
        _generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/stats")
async def stats(_: None = Depends(_require_auth)) -> JSONResponse:
    if not _db:
        raise HTTPException(503, "Database not available")
    try:
        s = await _db.get_stats()
        return JSONResponse(s)
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Run ───────────────────────────────────────────────────────────────────────


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=SUPERMEM_WORKER_HOST, port=SUPERMEM_WORKER_PORT)


if __name__ == "__main__":
    run()
