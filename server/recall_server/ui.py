# -*- coding: utf-8 -*-
"""Read-only web UI over the built recall data (timeline + threads + search).

GET /ui serves a single self-contained page (no auth — it holds no data);
the page calls the /v1/ui/* JSON endpoints below with the tenant's bearer
token, which the browser keeps in localStorage. Same auth dependency as the
rest of the API.
"""
from __future__ import annotations

import os
import re
import glob

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from . import search
from .config import Tenant

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")   # also blocks path tricks


def make_router(get_tenant) -> APIRouter:
    """get_tenant is injected by app.py to avoid a circular import."""
    router = APIRouter()

    @router.get("/ui", response_class=HTMLResponse)
    def ui_page():
        return PAGE

    @router.get("/v1/ui/timeline")
    def timeline_dates(tenant: Tenant = Depends(get_tenant)):
        dates = []
        for p in glob.glob(os.path.join(tenant.ctx.output_dir, "*.md")):
            d = os.path.splitext(os.path.basename(p))[0]
            if DATE_RE.match(d):
                dates.append(d)
        return {"dates": sorted(dates, reverse=True)}

    @router.get("/v1/ui/timeline/{date}")
    def timeline_day(date: str, tenant: Tenant = Depends(get_tenant)):
        if not DATE_RE.match(date):
            raise HTTPException(status_code=400, detail="bad date")
        try:
            with open(os.path.join(tenant.ctx.output_dir, date + ".md"),
                      "r", encoding="utf-8") as f:
                return {"date": date, "content": f.read()}
        except OSError:
            raise HTTPException(status_code=404, detail="no timeline for that date")

    @router.get("/v1/ui/threads")
    def threads_list(tenant: Tenant = Depends(get_tenant)):
        registry = search.load_registry(tenant.ctx)
        out = []
        for slug, e in registry.items():
            if not isinstance(e, dict) or e.get("alias_of"):
                continue
            out.append({
                "slug": slug,
                "name": e.get("name") or slug,
                "first_active": e.get("first_active") or "",
                "last_active": e.get("last_active") or "",
                "count": e.get("count") or 0,
                "has_state": bool(e.get("current_state")),
            })
        registered = set(registry.keys())
        for path in glob.glob(os.path.join(tenant.ctx.threads_dir, "*.md")):
            slug = os.path.splitext(os.path.basename(path))[0]
            if slug in registered:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            dates = search.THREAD_DATE_RE.findall(content)
            out.append({"slug": slug, "name": search.h1_of(content) or slug,
                        "first_active": dates[0] if dates else "",
                        "last_active": dates[-1] if dates else "",
                        "count": None, "has_state": False})
        out.sort(key=lambda t: (t["last_active"], t["first_active"]), reverse=True)
        return {"threads": out}

    @router.get("/v1/ui/threads/{slug}")
    def thread_detail(slug: str, tenant: Tenant = Depends(get_tenant)):
        if not SLUG_RE.match(slug):
            raise HTTPException(status_code=400, detail="bad slug")
        registry = search.load_registry(tenant.ctx)
        canonical = search.resolve_canonical(slug, registry)
        e = registry.get(canonical, {})
        content = ""
        try:
            with open(os.path.join(tenant.ctx.threads_dir, canonical + ".md"),
                      "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            if not e:
                raise HTTPException(status_code=404, detail="no such thread")
        return {"slug": canonical,
                "name": e.get("name") or search.h1_of(content) or canonical,
                "current_state": e.get("current_state") or "",
                "content": content}

    @router.get("/v1/ui/search")
    def ui_search(q: str, limit: int = 15, tenant: Tenant = Depends(get_tenant)):
        terms = search.terms_of(q)
        if not terms:
            return {"terms": [], "threads": [], "timeline": []}
        return {"terms": terms,
                "threads": search.search_threads(tenant.ctx, terms, limit),
                "timeline": search.search_timeline(tenant.ctx, terms, limit)}

    return router


# The page itself lives in ui_page.html — a single source shared with the
# Cloudflare Worker, which serves it at the edge (unauthenticated requests
# cannot be routed to a tenant container; see cloudflare/src/index.js).
with open(os.path.join(os.path.dirname(__file__), "ui_page.html"), encoding="utf-8") as _f:
    PAGE = _f.read()
