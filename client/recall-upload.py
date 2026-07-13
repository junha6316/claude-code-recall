#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Incremental transcript uploader (stdlib only, like the local scripts).

Scans ~/.claude/projects/**/*.jsonl and uploads the appended bytes since the
last run to the recall server. Per-file state guards against the (rare) cases
where a transcript is not a pure append:

  - (inode, size) tracking: a replaced file (tmp+rename) or a shrunk file
    resets the offset and re-uploads from 0 with X-Truncate.
  - boundary anchor: SHA-256 of the last 256 bytes before the committed offset;
    if those bytes changed, the prefix diverged → re-upload from 0.
  - complete lines only: deltas are cut at the last newline, so a record being
    written mid-flush is never half-sent.

Server protocol: POST /v1/ingest/{project}/{file} with X-Base-Offset; a 409
carries the server's current size and we retry from there (append-only ⇒ the
prefix content matches).

Usage:
  RECALL_SERVER_URL=http://127.0.0.1:8300 RECALL_TOKEN=... recall-upload.py [--full]
"""
import os
import sys
import json
import glob
import hashlib
import argparse
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
PROJECTS_DIR = os.path.join(CONFIG_DIR, "projects")
STATE_FILE = os.path.join(CONFIG_DIR, "scripts", ".recall-upload-state.json")

SERVER_URL = os.environ.get("RECALL_SERVER_URL", "").rstrip("/")
TOKEN = os.environ.get("RECALL_TOKEN", "")
ANCHOR_BYTES = 256
MAX_DELTA = 4 * 1024 * 1024   # upload in chunks the server accepts comfortably


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def anchor_of(f, offset):
    """SHA-256 hex of the ANCHOR_BYTES before `offset` (or fewer at file head)."""
    start = max(0, offset - ANCHOR_BYTES)
    f.seek(start)
    return hashlib.sha256(f.read(offset - start)).hexdigest()


def http(method, path, data=None, headers=None):
    req = urllib.request.Request(SERVER_URL + path, data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % TOKEN)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=60)


def post_delta(relpath, base_offset, data, truncate=False):
    """Returns the server's new size, or the server's current size on 409."""
    proj, name = relpath.split("/", 1)
    headers = {"X-Base-Offset": str(base_offset), "Content-Type": "application/octet-stream"}
    if truncate:
        headers["X-Truncate"] = "1"
    try:
        with http("POST", "/v1/ingest/%s/%s" % (proj, name), data=data, headers=headers) as r:
            return json.load(r)["size"], False
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return json.load(e)["detail"]["current_size"], True
        raise


def upload_file(path, relpath, entry, full=False):
    """Upload the delta for one file. Mutates and returns `entry`."""
    st = os.stat(path)
    size, inode = st.st_size, st.st_ino

    offset = entry.get("offset", 0)
    diverged = full
    if entry.get("inode") != inode or size < offset:
        diverged = True   # replaced or shrunk file
    if not diverged and offset > 0 and entry.get("anchor"):
        with open(path, "rb") as f:
            if anchor_of(f, offset) != entry["anchor"]:
                diverged = True   # prefix bytes changed under us

    if diverged:
        offset = 0

    with open(path, "rb") as f:
        f.seek(offset)
        pending = f.read()
        # complete lines only — hold back a trailing partial record
        cut = pending.rfind(b"\n")
        if cut < 0:
            pending = b""
        else:
            pending = pending[:cut + 1]

        sent = 0
        base = offset
        truncate = diverged
        while sent < len(pending):
            chunk = pending[sent:sent + MAX_DELTA]
            new_size, mismatch = post_delta(relpath, base, chunk, truncate=truncate)
            truncate = False
            if mismatch:
                # Server is at a different offset. Append-only ⇒ same prefix:
                # adopt the server's size and re-cut our delta from there.
                if new_size > size:
                    # server has MORE than local (shouldn't happen) — start over
                    new_size, _ = post_delta(relpath, 0, b"", truncate=True)
                base = new_size
                f.seek(base)
                pending = f.read()
                cut = pending.rfind(b"\n")
                pending = b"" if cut < 0 else pending[:cut + 1]
                sent = 0
                continue
            sent += len(chunk)
            base = new_size

        entry.update({"inode": inode, "offset": base, "mtime": st.st_mtime})
        entry["anchor"] = anchor_of(f, base)
    return entry, base - (0 if diverged else offset)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="ignore state and re-upload everything from 0")
    ap.add_argument("--project", help="only project dirs containing this substring")
    args = ap.parse_args()

    if not SERVER_URL or not TOKEN:
        print("Set RECALL_SERVER_URL and RECALL_TOKEN.", file=sys.stderr)
        sys.exit(2)

    state = load_state()
    files = state.setdefault("files", {})
    uploaded = skipped = 0
    total_bytes = 0

    for path in sorted(glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))):
        relpath = "%s/%s" % (os.path.basename(os.path.dirname(path)), os.path.basename(path))
        if args.project and args.project not in relpath:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        entry = files.get(relpath, {})
        # cheap skip: unchanged since last committed state
        if (not args.full and entry.get("inode") == st.st_ino
                and entry.get("offset") == st.st_size
                and entry.get("mtime") == st.st_mtime):
            skipped += 1
            continue
        try:
            files[relpath], delta = upload_file(path, relpath, entry, full=args.full)
        except urllib.error.HTTPError as e:
            print("  ! %s: HTTP %d %s" % (relpath, e.code, e.reason), file=sys.stderr)
            continue
        except OSError as e:
            print("  ! %s: %s" % (relpath, e), file=sys.stderr)
            continue
        save_state(state)   # per-file commit — a crash resumes cleanly
        if delta:
            uploaded += 1
            total_bytes += delta
            print("  ↑ %s (+%d bytes)" % (relpath, delta))

    print("upload complete: %d files uploaded (%d bytes), %d unchanged"
          % (uploaded, total_bytes, skipped))


if __name__ == "__main__":
    main()
