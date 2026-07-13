# -*- coding: utf-8 -*-
"""Transcript ingest storage — byte-offset append protocol.

Design (from the plan's verified append-only finding): Claude Code transcripts
are pure append (even compaction appends), so the server only needs an
offset-match append:

  1. Client asks GET /v1/files → {relpath: size} to sync its send offsets.
  2. Client POSTs a delta with X-Base-Offset: N (complete lines only).
  3. Server appends iff current size == N; otherwise 409 + current size and
     the client resyncs. Content divergence (client-side file replacement) is
     detected client-side via (inode,size,mtime)+anchor and re-uploaded from 0
     with X-Base-Offset: 0 + X-Truncate: 1.
"""
from __future__ import annotations

import os
import re
import json
from datetime import datetime

from recall_pipeline.context import TenantContext

# project dir + session file names come from the client path — allow only the
# flat names Claude Code actually produces (defense against path traversal).
# Note: project dirs are slugged cwd paths with a LEADING hyphen
# ("-Users-junha-park-..."), so the first char can't be restricted to alnum.
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,301}$")


def _safe_segment(name: str) -> bool:
    return bool(SAFE_SEGMENT.match(name)) and name not in (".", "..")


class BadSegment(ValueError):
    pass


class OffsetMismatch(Exception):
    def __init__(self, current_size: int):
        self.current_size = current_size
        super().__init__("offset mismatch; current size %d" % current_size)


def _target(ctx: TenantContext, project: str, session_file: str) -> str:
    if not _safe_segment(project) or not _safe_segment(session_file):
        raise BadSegment("invalid project/session name")
    if not session_file.endswith(".jsonl"):
        raise BadSegment("session file must be .jsonl")
    return os.path.join(ctx.projects_dir, project, session_file)


def file_sizes(ctx: TenantContext) -> dict:
    """{ "<project>/<session>.jsonl": size } for the tenant's stored files."""
    out = {}
    root = ctx.projects_dir
    if not os.path.isdir(root):
        return out
    for proj in os.listdir(root):
        pdir = os.path.join(root, proj)
        if not os.path.isdir(pdir):
            continue
        for name in os.listdir(pdir):
            if name.endswith(".jsonl"):
                try:
                    out["%s/%s" % (proj, name)] = os.path.getsize(os.path.join(pdir, name))
                except OSError:
                    pass
    return out


def append_delta(ctx: TenantContext, project: str, session_file: str,
                 base_offset: int, data: bytes, truncate: bool = False) -> int:
    """Append `data` at `base_offset`. Returns the new size.

    truncate=True means the client detected a replaced/diverged local file and
    is re-uploading from scratch (base_offset must be 0)."""
    path = _target(ctx, project, session_file)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cur = os.path.getsize(path) if os.path.exists(path) else 0
    if truncate:
        if base_offset != 0:
            raise OffsetMismatch(cur)
        mode = "wb"
    else:
        if cur != base_offset:
            raise OffsetMismatch(cur)
        mode = "ab"
    with open(path, mode) as f:
        f.write(data)
    return base_offset + len(data)


# ---- daily ingest byte cap (rate limit) ----

def check_ingest_budget(ctx: TenantContext, incoming: int, daily_cap: int) -> bool:
    """True if `incoming` bytes fit today's cap; records usage when allowed."""
    path = os.path.join(ctx.data_root, "ingest_budget.json")
    today = datetime.now(ctx.tz).strftime("%Y-%m-%d")
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    used = st.get("bytes", 0) if st.get("date") == today else 0
    if used + incoming > daily_cap:
        return False
    os.makedirs(ctx.data_root, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"date": today, "bytes": used + incoming}, f)
    os.replace(tmp, path)
    return True
