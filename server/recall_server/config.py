# -*- coding: utf-8 -*-
"""Server configuration + tenant resolution.

Phase 1 keeps this deliberately small: tenants live in a JSON file mapping
bearer tokens to tenant ids, and every tenant gets a directory under a common
data root. Accounts/DB arrive in Phase 2.

tenants.json format (path from RECALL_TENANTS_FILE):
  { "<bearer-token>": {"tenant_id": "junha", "tz": "Asia/Seoul", "lang": "Korean"} }
"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from recall_pipeline.context import TenantContext


@dataclass
class ServerConfig:
    data_root: str                      # {data_root}/{tenant_id}/... per tenant
    tenants_file: str                   # token -> tenant settings JSON
    build_debounce_seconds: float = 20.0   # quiet period after the last ingest before building
    llm_daily_call_cap: int = 500          # per-tenant Anthropic calls per day (cost cap)
    ingest_max_body_bytes: int = 8 * 1024 * 1024   # one delta upload cap
    ingest_daily_byte_cap: int = 256 * 1024 * 1024  # per-tenant upload cap per day

    @classmethod
    def from_env(cls) -> "ServerConfig":
        data_root = os.environ.get("RECALL_DATA_ROOT") or os.path.expanduser("~/recall-data")
        return cls(
            data_root=data_root,
            tenants_file=os.environ.get("RECALL_TENANTS_FILE")
                         or os.path.join(data_root, "tenants.json"),
            build_debounce_seconds=float(os.environ.get("RECALL_BUILD_DEBOUNCE", "20")),
            llm_daily_call_cap=int(os.environ.get("RECALL_LLM_DAILY_CAP", "500")),
        )


@dataclass
class Tenant:
    tenant_id: str
    ctx: TenantContext


def load_tenants(cfg: ServerConfig) -> dict:
    """token -> Tenant. Reloaded per request (file is tiny; hot-add tenants
    without restart)."""
    try:
        with open(cfg.tenants_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    out = {}
    for token, t in raw.items():
        tid = t["tenant_id"]
        ctx = TenantContext(
            data_root=os.path.join(cfg.data_root, tid),
            tz=ZoneInfo(t.get("tz", "UTC")),
            summary_lang=t.get("lang", "English"),
        )
        if t.get("model"):
            ctx.model = t["model"]
        if t.get("backfill_floor"):
            ctx.backfill_floor = t["backfill_floor"]
        out[token] = Tenant(tenant_id=tid, ctx=ctx)
    return out
