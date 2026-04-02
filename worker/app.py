"""
Recall v2 Worker HTTP API — optional service on port 37777.

If this service is NOT running, the MCP stdio server operates normally
with zero degradation. Start with: recall serve --worker

Endpoints:
  GET  /              static/index.html — session viewer + search UI
  GET  /health        liveness + readiness
  GET  /sessions      recent sessions with summaries (paginated)
  GET  /observations  paginated observations, filterable
  POST /search        FTS5 + graph + vector hybrid search
  POST /index/rebuild re-index entire vault
  GET  /backup        stream tar.gz of vault + SQLite snapshot
  GET  /stats         memory metrics

Auth: Bearer token from RECALL_API_KEY header. Disabled when env var unset.
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

from recall.capture.observation import ObservationCapture
from recall.capture.session import SessionManager
from recall.config import (
    RECALL_API_KEY,
    RECALL_DB_PATH,
    RECALL_DEFAULT_TIER_LIMIT,
    RECALL_VAULT_PATH,
    RECALL_WORKER_HOST,
    RECALL_WORKER_PORT,
)
from recall.indexer.vault import VaultIndexer
from recall.logging import get_logger
from recall.retrieval.hybrid import HybridRetriever
from recall.storage.database import DatabaseManager
from recall.storage.graph import KuzuGraphManager
from recall.storage.vector import ChromaManager

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
    log.info("worker_started", port=RECALL_WORKER_PORT)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _db:
        await _db.close()
    log.info("worker_stopped")


# ── Auth dependency ───────────────────────────────────────────────────────────

async def _require_auth(request: Request) -> None:
    if not RECALL_API_KEY:
        return  # auth disabled in personal mode
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != RECALL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    html_path = _STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Recall Worker</h1><p>static/index.html not found.</p>")


@app.get("/health")
async def health() -> JSONResponse:
    db_ok = await _db.health() if _db else False
    graph_ok = _graph.available if _graph else False
    vector_ok = _chroma.available if _chroma else False
    status = "ok" if db_ok else "degraded"
    return JSONResponse({
        "status": status,
        "db": db_ok,
        "graph": graph_ok,
        "vector": vector_ok,
        "timestamp": time.time(),
    })


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
            sessions.append({
                "id": sid,
                "started_at": row[1],
                "ended_at": row[2],
                "summary": row[3],
                "obs_count": obs_count,
            })
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
    tier_limit: int = RECALL_DEFAULT_TIER_LIMIT


@app.post("/search")
async def search(
    body: SearchRequest,
    _: None = Depends(_require_auth),
) -> JSONResponse:
    if not _retriever:
        raise HTTPException(503, "Retriever not available")
    try:
        result = await _retriever.search(body.query, tier_limit=body.tier_limit)
        obs_list = await _retriever.get_observations(result.obs_ids) if result.obs_ids else []
        return JSONResponse({
            "query": body.query,
            "source_tier": result.source_tier,
            "tier_label": {1: "FTS5", 2: "graph", 3: "vector", 4: "agent"}.get(result.source_tier, "none"),
            "latency_ms": round(result.latency_ms, 1),
            "obs_ids": result.obs_ids,
            "observations": [
                {"id": o.get("id"), "content": o.get("content", ""), "type": o.get("type")}
                for o in obs_list
            ],
        })
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/index/rebuild")
async def rebuild_index(_: None = Depends(_require_auth)) -> JSONResponse:
    if not _db or not _graph:
        raise HTTPException(503, "Storage not available")
    try:
        indexer = VaultIndexer(db=_db, graph=_graph, vault_path=RECALL_VAULT_PATH)
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
    filename = f"recall_backup_{timestamp}.tar.gz"

    def _generate():
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # Add vault markdown files
            if RECALL_VAULT_PATH.exists():
                for md in RECALL_VAULT_PATH.rglob("*.md"):
                    try:
                        tar.add(str(md), arcname=f"vault/{md.relative_to(RECALL_VAULT_PATH)}")
                    except Exception:
                        pass
            # Add SQLite DB snapshot
            if RECALL_DB_PATH.exists():
                tar.add(str(RECALL_DB_PATH), arcname="recall.db")
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
    uvicorn.run(app, host=RECALL_WORKER_HOST, port=RECALL_WORKER_PORT)


if __name__ == "__main__":
    run()
