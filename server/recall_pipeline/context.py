# -*- coding: utf-8 -*-
"""Per-tenant context injected into every pipeline function.

Replaces the local scripts' module-level globals (HOME/CONFIG_DIR/PROJECTS_DIR/
OUTPUT_DIR/STATE_FILE in work-timeline.py:28-35, THREADS_DIR/REGISTRY_FILE/
CURSOR_FILE in work-timeline-threads.py:36-38). The importlib import-time
freezing problem the review flagged goes away because nothing is computed at
import time — paths derive from data_root per call.
"""
from __future__ import annotations  # keep the `str | None` annotation valid on 3.9 (pyproject floor)

import os
from dataclasses import dataclass, field
from datetime import tzinfo
from zoneinfo import ZoneInfo


@dataclass
class TenantContext:
    # Tenant data root. Layout:
    #   {data_root}/projects/*/*.jsonl     uploaded transcripts (same shape as ~/.claude/projects)
    #   {data_root}/work-timeline/         built timeline output (YYYY-MM-DD.md)
    #   {data_root}/work-timeline/threads/ thread files + _registry.json + _cursor.json
    #   {data_root}/state.json             timeline cursor (last_processed_epoch)
    data_root: str
    # Tenant timezone — bucketing and date filenames use this, NOT the server's
    # local tz (review item C: a UTC server bucketing a KST user's day is wrong).
    tz: tzinfo = field(default_factory=lambda: ZoneInfo("UTC"))
    summary_lang: str = "English"
    bucket_minutes: int = 15
    model: str = "claude-sonnet-5"
    llm_timeout: float = 180.0
    # Threads drops days before this date if set (YYYY-MM-DD). The local script
    # hardcodes the author's own start date (threads.py:41 BACKFILL_FLOOR) —
    # here it is per-tenant and defaults to no floor.
    backfill_floor: str | None = None
    # Incremental timeline runs won't reach further back than this (flood guard).
    # History backfill uses the explicit date/backfill paths instead.
    max_backfill_hours: int = 24

    @property
    def projects_dir(self) -> str:
        return os.path.join(self.data_root, "projects")

    @property
    def output_dir(self) -> str:
        return os.path.join(self.data_root, "work-timeline")

    @property
    def threads_dir(self) -> str:
        return os.path.join(self.output_dir, "threads")

    @property
    def registry_file(self) -> str:
        return os.path.join(self.threads_dir, "_registry.json")

    @property
    def threads_cursor_file(self) -> str:
        return os.path.join(self.threads_dir, "_cursor.json")

    @property
    def state_file(self) -> str:
        return os.path.join(self.data_root, "state.json")
