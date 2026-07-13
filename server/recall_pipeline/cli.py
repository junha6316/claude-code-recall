# -*- coding: utf-8 -*-
"""Pipeline CLI — run the ported stages against one tenant's data root.

Phase 0 verification entrypoint; Phase 1's build worker calls the same
run_* functions from the queue instead.

Usage:
  python -m recall_pipeline.cli --data-root PATH [--tz Asia/Seoul] timeline [--date YYYY-MM-DD | --dry-run | --no-llm]
  python -m recall_pipeline.cli --data-root PATH rollup [--date YYYY-MM-DD] [--force]
  python -m recall_pipeline.cli --data-root PATH threads [--since YYYY-MM-DD] [--dry-run]
  python -m recall_pipeline.cli --data-root PATH consolidate [--no-synth] [--dry-run]
"""
import argparse
from zoneinfo import ZoneInfo

from .context import TenantContext
from . import timeline, rollup, threads, consolidate


def build_ctx(args) -> TenantContext:
    ctx = TenantContext(data_root=args.data_root, tz=ZoneInfo(args.tz))
    if args.model:
        ctx.model = args.model
    if args.lang:
        ctx.summary_lang = args.lang
    if args.backfill_floor:
        ctx.backfill_floor = args.backfill_floor
    return ctx


def main():
    ap = argparse.ArgumentParser(prog="recall_pipeline")
    ap.add_argument("--data-root", required=True, help="tenant data root (contains projects/)")
    ap.add_argument("--tz", default="UTC", help="tenant IANA timezone (e.g. Asia/Seoul)")
    ap.add_argument("--model", help="override summary model (default: claude-sonnet-5)")
    ap.add_argument("--lang", help="summary language (default: English)")
    ap.add_argument("--backfill-floor", help="threads: drop days before YYYY-MM-DD")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("timeline", help="build the bucketed timeline")
    p.add_argument("--date", help="process only this date (YYYY-MM-DD), state unchanged")
    p.add_argument("--dry-run", action="store_true", help="print to stdout without writing files")
    p.add_argument("--no-llm", action="store_true", help="skip the LLM summary (use ai-title)")

    p = sub.add_parser("rollup", help="write the daily summary section")
    p.add_argument("--date", help="YYYY-MM-DD (default: most recent day without a summary)")
    p.add_argument("--force", action="store_true", help="regenerate even if a summary exists")

    p = sub.add_parser("threads", help="group sessions into work threads")
    p.add_argument("--since", help="process from YYYY-MM-DD (backfill)")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("consolidate", help="cluster threads + synthesize current state")
    p.add_argument("--no-synth", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()
    ctx = build_ctx(args)

    if args.command == "timeline":
        if args.date:
            timeline.run_date(ctx, args.date, no_llm=args.no_llm, dry_run=args.dry_run)
        else:
            timeline.run_incremental(ctx, no_llm=args.no_llm, dry_run=args.dry_run)
    elif args.command == "rollup":
        rollup.rollup_day(ctx, date_str=args.date, force=args.force)
    elif args.command == "threads":
        threads.run_threads(ctx, since=args.since, dry_run=args.dry_run)
    elif args.command == "consolidate":
        consolidate.run_consolidate(ctx, no_synth=args.no_synth, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
