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

import os
import asyncio
import contextvars
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import build, search, storage
from .config import ServerConfig, Tenant
from .r2 import R2Config, R2Sync
from .tenants import TenantStore

cfg = ServerConfig.from_env()
scheduler = build.BuildScheduler(cfg)

# Tenant store: SQLite locally, D1-compatible schema in production. On first
# boot, silently import a Phase 1 tenants.json if one sits at the legacy path.
store = TenantStore(os.environ.get("RECALL_TENANTS_DB")
                    or os.path.join(cfg.data_root, "tenants.db"))
store.import_json(cfg.tenants_file)

# Personal-deploy bridge for ephemeral disks (Cloudflare Containers): the DB
# is rebuilt empty on every cold start, so seed one tenant from env. Format:
#   RECALL_SEED_TENANT="tenant_id:tz:lang:token"
# Multi-tenant production replaces this with a D1 lookup — do not grow it.
_seed = os.environ.get("RECALL_SEED_TENANT")
if _seed:
    _tid, _tz, _lang, _token = _seed.split(":", 3)
    if store.resolve(_token, cfg.data_root) is None:
        try:
            store.add_with_token(_tid, _token, tz=_tz, lang=_lang)
        except Exception as e:   # already exists with another token, etc.
            print("[seed] skipped: %s" % e)

# R2 persistence (None in local mode — everything below degrades to Phase 1).
_r2cfg = R2Config.from_env()
r2 = R2Sync(_r2cfg) if _r2cfg else None
_pull_locks: dict[str, asyncio.Lock] = {}   # tenant_id -> cold-pull guard

# Tenant for the in-flight MCP request (set by the auth middleware; contextvars
# propagate through the ASGI call into the tool handlers).
_mcp_tenant: contextvars.ContextVar[Tenant | None] = contextvars.ContextVar(
    "mcp_tenant", default=None)


def _resolve_token(authorization: str | None) -> Tenant | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    resolved = store.resolve(authorization[len("Bearer "):].strip(), cfg.data_root)
    if resolved is None:
        return None
    tenant_id, ctx = resolved
    return Tenant(tenant_id=tenant_id, ctx=ctx)


async def _ensure_warm(tenant: Tenant):
    """Cold container disk (fresh instance after scale-to-zero) → restore the
    tenant's data from R2 before serving. One pull per tenant at a time."""
    if r2 is None or not r2.is_cold(tenant.ctx):
        return
    lock = _pull_locks.setdefault(tenant.tenant_id, asyncio.Lock())
    async with lock:
        if r2.is_cold(tenant.ctx):   # re-check after waiting on the lock
            n = await asyncio.to_thread(r2.pull_tenant, tenant.tenant_id, tenant.ctx)
            print("[r2] cold start: restored %d object(s) for %s" % (n, tenant.tenant_id))


async def get_tenant(authorization: str | None = Header(default=None)) -> Tenant:
    tenant = _resolve_token(authorization)
    if tenant is None:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    await _ensure_warm(tenant)
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
    # The SDK's DNS-rebinding Host check defaults to localhost-only, which
    # 421s every request behind the workers.dev proxy. Rebinding is already
    # blocked here: our middleware rejects any request without a bearer token,
    # and a rebound browser origin cannot attach that header.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
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

if r2 is not None:
    async def _push_after_build(tenant_id, ctx):
        n = await asyncio.to_thread(r2.push_outputs, tenant_id, ctx)
        print("[r2] pushed %d output object(s) for %s" % (n, tenant_id))
    scheduler.after_build = _push_after_build


@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    """Bearer-gate the MCP mount and hand the tenant to tool handlers."""
    if request.url.path.startswith("/mcp"):
        tenant = _resolve_token(request.headers.get("authorization"))
        if tenant is None:
            return Response(status_code=401, content="invalid or missing bearer token")
        await _ensure_warm(tenant)   # recall must see R2-restored data post-sleep
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
    if r2 is not None:
        # Durable before ack — a post-ack sleep must not lose the delta.
        relpath = "projects/%s/%s" % (project, session_file)
        await asyncio.to_thread(r2.push_file, tenant.tenant_id, tenant.ctx, relpath)
    scheduler.schedule(tenant.tenant_id, tenant.ctx)
    return {"size": new_size}


@app.post("/v1/build")
async def build_now(tenant: Tenant = Depends(get_tenant)):
    """Manual build trigger (verification/ops; ingest normally debounces this)."""
    return await scheduler.build_now(tenant.tenant_id, tenant.ctx)


@app.delete("/v1/tenant")
async def delete_tenant(tenant: Tenant = Depends(get_tenant)):
    """Self-serve deletion: wipe the R2 prefix and local data, then revoke.

    Wipe-before-revoke keeps the operation retryable: if the R2 delete fails
    partway, the token stays valid so the client can call DELETE again and
    finish the wipe. Revoking first would 401 that retry and strand the
    half-deleted data. (The local rmtree ignores errors — local is just the
    ephemeral cache of the already-wiped R2 state.) mark_deleted runs last
    and is idempotent."""
    import shutil
    removed_r2 = 0
    if r2 is not None:
        removed_r2 = await asyncio.to_thread(r2.delete_tenant, tenant.tenant_id)
    await asyncio.to_thread(shutil.rmtree, tenant.ctx.data_root, True)
    store.mark_deleted(tenant.tenant_id)
    return {"deleted": tenant.tenant_id, "r2_objects_removed": removed_r2}


@app.get("/v1/status")
def status(tenant: Tenant = Depends(get_tenant)):
    return {
        "tenant": tenant.tenant_id,
        "files": len(storage.file_sizes(tenant.ctx)),
        "last_build": scheduler.last_status.get(tenant.tenant_id),
    }


app.mount("/mcp", mcp_asgi)
