# -*- coding: utf-8 -*-
"""Debounced per-tenant build worker.

Each ingest schedules a build after a quiet period (new ingests reset the
timer). The build runs the four pipeline stages via recall_pipeline's run_*
functions — the same code the Phase 0 CLI drives. Failures propagate from the
pipeline with its fixed cursor semantics, so the next trigger retries exactly
where the last build stopped.

Phase 1 concurrency model: one asyncio event loop, one build at a time
(global lock). rollup/threads/consolidate are day-granular, so recall answers
for *today* come from the timeline tier; the daily tiers catch up on the first
build after midnight (documented in the plan).
"""
from __future__ import annotations

import os
import json
import asyncio
from datetime import datetime

from recall_pipeline import llm, timeline, rollup, threads, consolidate
from recall_pipeline.context import TenantContext

from .config import ServerConfig


class LLMBudget:
    """Daily Anthropic call cap, persisted per tenant. Installed as
    llm.before_call for the duration of one build (builds are serialized)."""

    def __init__(self, ctx: TenantContext, daily_cap: int):
        self.ctx = ctx
        self.daily_cap = daily_cap
        self.path = os.path.join(ctx.data_root, "llm_budget.json")

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def check_and_count(self):
        today = datetime.now(self.ctx.tz).strftime("%Y-%m-%d")
        st = self._load()
        calls = st.get("calls", 0) if st.get("date") == today else 0
        if calls >= self.daily_cap:
            raise llm.BudgetExceeded(
                "daily LLM call cap reached (%d) — build resumes tomorrow" % self.daily_cap)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": today, "calls": calls + 1}, f)
        os.replace(tmp, self.path)


def run_build(ctx: TenantContext, daily_cap: int) -> dict:
    """One full build pass (blocking — call via asyncio.to_thread)."""
    budget = LLMBudget(ctx, daily_cap)
    llm.before_call = budget.check_and_count
    status = {"timeline": "ok", "rollup": "ok", "threads": "ok", "consolidate": "ok"}
    try:
        # Stage order mirrors the local cron: timeline first, day-granular after.
        # Each stage's failure is isolated so a later stage still runs on the
        # data the earlier stages already committed... except timeline, whose
        # output everything else reads — its failure skips the rest.
        try:
            timeline.run_incremental(ctx)
        except llm.LLMError as e:
            status["timeline"] = "failed: %s" % e
            status["rollup"] = status["threads"] = status["consolidate"] = "skipped"
            return status
        for name, fn in (("rollup", lambda: rollup.rollup_day(ctx)),
                         ("threads", lambda: threads.run_threads(ctx)),
                         ("consolidate", lambda: consolidate.run_consolidate(ctx))):
            try:
                fn()
            except llm.LLMError as e:
                status[name] = "failed: %s" % e
        return status
    finally:
        llm.before_call = None


class BuildScheduler:
    """Debounce ingests into builds. One pending timer per tenant; one global
    build lock (Phase 1: single process, builds serialized)."""

    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self._timers: dict[str, asyncio.Task] = {}
        self._build_lock = asyncio.Lock()
        self.last_status: dict[str, dict] = {}

    def schedule(self, tenant_id: str, ctx: TenantContext):
        prev = self._timers.get(tenant_id)
        if prev and not prev.done():
            prev.cancel()  # reset the quiet period
        self._timers[tenant_id] = asyncio.get_running_loop().create_task(
            self._debounced(tenant_id, ctx))

    async def _debounced(self, tenant_id: str, ctx: TenantContext):
        try:
            await asyncio.sleep(self.cfg.build_debounce_seconds)
        except asyncio.CancelledError:
            return
        # Quiet period elapsed — detach from _timers so a later ingest's
        # schedule() cancels only *pending* timers, never this now-running
        # build. (Cancelling a task already inside build_now unwinds the
        # `async with _build_lock`, releasing the lock while the orphaned
        # to_thread build keeps running → two concurrent builds.) No await
        # runs between the sleep returning and this pop, so the detach is
        # atomic w.r.t. other coroutines.
        if self._timers.get(tenant_id) is asyncio.current_task():
            del self._timers[tenant_id]
        await self.build_now(tenant_id, ctx)

    async def build_now(self, tenant_id: str, ctx: TenantContext) -> dict:
        async with self._build_lock:
            status = await asyncio.to_thread(run_build, ctx, self.cfg.llm_daily_call_cap)
        status["at"] = datetime.now(ctx.tz).isoformat(timespec="seconds")
        self.last_status[tenant_id] = status
        print("[build] %s: %s" % (tenant_id, status))
        return status
