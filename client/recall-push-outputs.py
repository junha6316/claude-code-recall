#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot migration: push the LOCAL pipeline's finished outputs to the
recall server, so a tenant starts with its history already built instead of
waiting for a server-side LLM backfill.

Uploads (full-file replace via PUT /v1/outputs/<relpath>):
  ~/.claude/work-timeline/*.md                  -> work-timeline/*.md
  ~/.claude/work-timeline/threads/*             -> work-timeline/threads/*
  ~/.claude/scripts/.work-timeline-state.json   -> state.json  (timeline cursor,
                                                   so server builds continue
                                                   where the local run stopped)

Usage:
  RECALL_SERVER_URL=... RECALL_TOKEN=... recall-push-outputs.py
"""
import os
import sys
import glob
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
TIMELINE_DIR = os.path.join(CONFIG_DIR, "work-timeline")
STATE_FILE = os.path.join(CONFIG_DIR, "scripts", ".work-timeline-state.json")

SERVER_URL = os.environ.get("RECALL_SERVER_URL", "").rstrip("/")
TOKEN = os.environ.get("RECALL_TOKEN", "")
WORKERS = int(os.environ.get("RECALL_UPLOAD_WORKERS", "8"))


def put(relpath, data):
    req = urllib.request.Request("%s/v1/outputs/%s" % (SERVER_URL, relpath),
                                 data=data, method="PUT")
    req.add_header("Authorization", "Bearer %s" % TOKEN)
    req.add_header("User-Agent", "recall-upload/1.0")
    req.add_header("Content-Type", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def main():
    if not SERVER_URL or not TOKEN:
        print("Set RECALL_SERVER_URL and RECALL_TOKEN.", file=sys.stderr)
        sys.exit(2)

    jobs = []   # (local_path, relpath)
    for p in sorted(glob.glob(os.path.join(TIMELINE_DIR, "*.md"))):
        jobs.append((p, "work-timeline/%s" % os.path.basename(p)))
    for p in sorted(glob.glob(os.path.join(TIMELINE_DIR, "threads", "*"))):
        if os.path.isfile(p):
            jobs.append((p, "work-timeline/threads/%s" % os.path.basename(p)))
    if os.path.exists(STATE_FILE):
        jobs.append((STATE_FILE, "state.json"))

    def work(job):
        path, relpath = job
        with open(path, "rb") as f:
            data = f.read()
        put(relpath, data)
        return relpath, len(data)

    done = failed = total = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for fut in as_completed([pool.submit(work, j) for j in jobs]):
            try:
                relpath, n = fut.result()
            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                print("  ! %s" % e, file=sys.stderr)
                failed += 1
                continue
            done += 1
            total += n
    print("outputs pushed: %d files (%d bytes), %d failed" % (done, total, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
