# -*- coding: utf-8 -*-
"""FastAPI app: ingest API + recall MCP server (streamable HTTP) in one process.

Auth (Phase 1): every request carries `Authorization: Bearer <token>`; the
token maps to a tenant via tenants.json. The MCP mount shares the same tokens —
Claude Code registers it with
  claude mcp add recall --transport http <url>/mcp \
      --header "Authorization: Bearer <token>"

Run:
  RECALL_DATA_ROOT=... .venv/bin/uvicorn recall_server.app:app --port 8300
"""
from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from mcp.server.fastmcp import FastMCP

from . import build, search, storage
from .config import ServerConfig, Tenant, load_tenants

cfg = ServerConfig.from_env()
scheduler = build.BuildScheduler(cfg)

# Tenant for the in-flight MCP request (set by the auth middleware; contextvars
# propagate through the ASGI call into the tool handlers).
_mcp_tenant: contextvars.ContextVar[Tenant | None] = contextvars.ContextVar(
    "mcp_tenant", default=None)


def _resolve_token(authorization: str | None) -> Tenant | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return load_tenants(cfg).get(authorization[len("Bearer "):].strip())


def get_tenant(authorization: str | None = Header(default=None)) -> Tenant:
    tenant = _resolve_token(authorization)
    if tenant is None:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return tenant


# ---------- MCP server ----------

mcp = FastMCP(
    "recall",
    instructions=(
        "Search this user's past Claude Code work. Use recall_search first "
        "(curated timeline + work threads); use recall_raw only when you need "
        "an exact phrase/code/error, narrowed by date."
    ),
    stateless_http=True,
    streamable_http_path="/",   # mounted at /mcp below → endpoint is <url>/mcp
)


def _require_mcp_tenant() -> Tenant:
    tenant = _mcp_tenant.get()
    if tenant is None:  # middleware already 401s; defense in depth
        raise RuntimeError("unauthenticated MCP request")
    return tenant


@mcp.tool()
def recall_search(query: str, limit: int = 15) -> str:
    """Search the curated work timeline and work threads (when/which project/what
    was done + each thread's current state). Start here for any recall question."""
    tenant = _require_mcp_tenant()
    terms = search.terms_of(query)
    if not terms:
        return "Search query is empty."
    return search.render_results(
        terms,
        search.search_threads(tenant.ctx, terms, limit),
        search.search_timeline(tenant.ctx, terms, limit),
    )


@mcp.tool()
def recall_raw(query: str, since: str | None = None, until: str | None = None,
               project: str | None = None, limit: int = 15) -> str:
    """Search raw conversation transcripts for exact phrases/code/errors.
    Narrow with since/until (YYYY-MM-DD) — unbounded raw scans are slow."""
    tenant = _require_mcp_tenant()
    terms = search.terms_of(query)
    if not terms:
        return "Search query is empty."
    hits = search.search_raw(tenant.ctx, terms, since=since, until=until,
                             project=project, limit=limit)
    return search.render_raw(terms, hits, project)


mcp_asgi = mcp.streamable_http_app()


# ---------- FastAPI ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="recall server", lifespan=lifespan)


@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    """Bearer-gate the MCP mount and hand the tenant to tool handlers."""
    if request.url.path.startswith("/mcp"):
        tenant = _resolve_token(request.headers.get("authorization"))
        if tenant is None:
            return Response(status_code=401, content="invalid or missing bearer token")
        _mcp_tenant.set(tenant)
    return await call_next(request)


@app.get("/v1/files")
def list_files(tenant: Tenant = Depends(get_tenant)):
    """Current stored size per transcript file — the client syncs offsets here."""
    return storage.file_sizes(tenant.ctx)


@app.post("/v1/ingest/{project}/{session_file}")
async def ingest(project: str, session_file: str, request: Request,
                 tenant: Tenant = Depends(get_tenant),
                 x_base_offset: int = Header(...),
                 x_truncate: bool = Header(default=False)):
    data = await request.body()
    if len(data) > cfg.ingest_max_body_bytes:
        raise HTTPException(status_code=413, detail="delta too large")
    if not storage.check_ingest_budget(tenant.ctx, len(data), cfg.ingest_daily_byte_cap):
        raise HTTPException(status_code=429, detail="daily ingest byte cap reached")
    try:
        new_size = storage.append_delta(tenant.ctx, project, session_file,
                                        x_base_offset, data, truncate=x_truncate)
    except storage.BadSegment as e:
        raise HTTPException(status_code=400, detail=str(e))
    except storage.OffsetMismatch as e:
        # Client resyncs from this size (append-only ⇒ same prefix content).
        raise HTTPException(status_code=409, detail={"current_size": e.current_size})
    scheduler.schedule(tenant.tenant_id, tenant.ctx)
    return {"size": new_size}


@app.post("/v1/build")
async def build_now(tenant: Tenant = Depends(get_tenant)):
    """Manual build trigger (verification/ops; ingest normally debounces this)."""
    return await scheduler.build_now(tenant.tenant_id, tenant.ctx)


@app.get("/v1/status")
def status(tenant: Tenant = Depends(get_tenant)):
    return {
        "tenant": tenant.tenant_id,
        "files": len(storage.file_sizes(tenant.ctx)),
        "last_build": scheduler.last_status.get(tenant.tenant_id),
    }


app.mount("/mcp", mcp_asgi)
