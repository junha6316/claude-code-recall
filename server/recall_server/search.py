# -*- coding: utf-8 -*-
"""Recall search backend — port of skills/recall/recall.py.

The scorer/tokenizer (terms_of, score, matched_lines, split_blocks,
resolve_canonical) are copied unchanged — they are pure functions. The data
access layer is rewritten against TenantContext paths (the local script globs
~/.claude; Phase 1 serves from the tenant's server-local filesystem — object
storage is a later decision).

Search functions return structured dicts; the MCP layer formats them.
"""
from __future__ import annotations

import os
import re
import json
import glob
from datetime import datetime, timedelta

from recall_pipeline.context import TenantContext

LINE_TRUNC = 200          # max length when displaying a matched line
MAX_LINES_PER_HIT = 8     # number of matched lines to show per block
DEFAULT_LIMIT = 15


# --- query tokenization (verbatim from recall.py) -----------------------

MAX_TERMS = 6

STOP = {
    # Korean
    "기억해", "기억", "기억나", "전에", "예전", "지난번", "저번", "그때", "언제",
    "했지", "했어", "했던", "했었", "하던", "만들던", "만들던거", "만들", "하던거",
    "그거", "그게", "이거", "저거", "내가", "우리", "그", "좀", "해줘", "했나",
    "뭐", "뭐였지", "어떻게", "왜", "거", "것", "건", "때", "줘", "해", "나", "수",
    "있어", "있나", "없어", "적", "일", "관련",
    # English
    "the", "a", "an", "when", "did", "do", "how", "what", "was", "were", "is",
    "are", "i", "we", "you", "that", "this", "it", "there", "about", "for",
}

JOSA = re.compile(
    r"(을|를|이|가|은|는|에|의|로|으로|도|만|와|과|랑|이랑|에서|까지|부터"
    r"|던거|던|거|게|야|냐|니|네|좀|했|하)+$"
)


def terms_of(query):
    """Turn a query into search-term tokens (keyword extraction, max MAX_TERMS).
    Falls back to a plain whitespace split if extraction leaves nothing."""
    raw = re.split(r"[\s,.;:!?()\[\]{}<>'\"`~/\\|=&]+", query)
    out, seen = [], set()

    def add(tok):
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)

    for tok in raw:
        if not tok:
            continue
        low = tok.lower()
        if re.fullmatch(r"[a-z0-9_+#.-]{2,}", low):
            if low not in STOP:
                add(low)
            continue
        stem = JOSA.sub("", tok)
        if len(stem) < 2 or stem in STOP or tok in STOP:
            continue
        add(stem.lower())

    if not out:  # all tokens were stopwords/junk — fall back to the naive split
        return [t.lower() for t in query.split() if t.strip()][:MAX_TERMS]
    return out[:MAX_TERMS]


def score(text_lower, terms):
    """Return (number of distinct matches, total occurrences)."""
    distinct = 0
    total = 0
    for t in terms:
        c = text_lower.count(t)
        if c:
            distinct += 1
            total += c
    return distinct, total


def matched_lines(body, terms):
    out = []
    for line in body.splitlines():
        low = line.lower()
        if any(t in low for t in terms):
            s = " ".join(line.split())
            if not s or s.startswith("##"):
                continue
            if len(s) > LINE_TRUNC:
                s = s[:LINE_TRUNC] + "…"
            out.append(s)
            if len(out) >= MAX_LINES_PER_HIT:
                break
    return out


def split_blocks(content):
    """Split md into (heading, body) blocks by '## ' headings. H1 is ignored."""
    blocks = []
    cur, buf = None, []
    for line in content.splitlines():
        if line.startswith("## "):
            if cur is not None:
                blocks.append((cur, "\n".join(buf)))
            cur, buf = line[3:].strip(), [line]
        elif line.startswith("# "):
            continue
        else:
            if cur is not None:
                buf.append(line)
    if cur is not None:
        blocks.append((cur, "\n".join(buf)))
    return blocks


# ---------- Tier 1: timeline ----------

def search_timeline(ctx: TenantContext, terms, limit=DEFAULT_LIMIT):
    hits = []  # dicts
    for path in sorted(glob.glob(os.path.join(ctx.output_dir, "[0-9]" * 4 + "-*.md"))):
        date = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        for heading, body in split_blocks(content):
            d, t = score(body.lower(), terms)
            if d == 0:
                continue
            hits.append({"distinct": d, "total": t, "date": date,
                         "heading": heading, "lines": matched_lines(body, terms)})
    hits.sort(key=lambda h: (h["distinct"], h["total"], h["date"]), reverse=True)
    return hits[:limit]


# ---------- Tier 1.5: work threads ----------

THREAD_DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})", re.M)


def load_registry(ctx: TenantContext):
    try:
        with open(ctx.registry_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def resolve_canonical(slug, registry):
    """Follow the alias_of chain to resolve to the canonical slug."""
    seen = set()
    while slug in registry and registry[slug].get("alias_of") and slug not in seen:
        seen.add(slug)
        slug = registry[slug]["alias_of"]
    return slug


def h1_of(content):
    return next((ln[2:].strip() for ln in content.splitlines() if ln.startswith("# ")), "")


def search_threads(ctx: TenantContext, terms, limit=DEFAULT_LIMIT):
    """Search threads/<slug>.md, grouping aliases under their canonical and returning the canonical's current state."""
    registry = load_registry(ctx)
    clusters = {}  # canonical -> aggregation dict
    for path in sorted(glob.glob(os.path.join(ctx.threads_dir, "*.md"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        d, t = score(content.lower(), terms)
        if d == 0:
            continue
        slug = os.path.splitext(os.path.basename(path))[0]
        canonical = resolve_canonical(slug, registry)
        e = registry.get(canonical, {})
        c = clusters.get(canonical)
        if c is None:
            c = {"distinct": 0, "total": 0, "lines": [], "slug": canonical, "last_date": "",
                 "name": e.get("name") or h1_of(content) or canonical,
                 "current_state": e.get("current_state"), "via": set()}
            clusters[canonical] = c
        if slug != canonical:                       # matched on an alias file
            c["via"].add(registry.get(slug, {}).get("name") or slug)
        if (d, t) > (c["distinct"], c["total"]):    # take matched lines from the highest-scoring file
            c["distinct"], c["total"] = d, t
            c["lines"] = matched_lines(content, terms)
            dates = THREAD_DATE_RE.findall(content)
            c["last_date"] = dates[-1] if dates else ""
    ranked = sorted(clusters.values(),
                    key=lambda c: (c["distinct"], c["total"], c["last_date"]), reverse=True)
    for c in ranked:
        c["via"] = sorted(c["via"])
    return ranked[:limit]


# ---------- Tier 2: raw transcript ----------

def _extract_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                tx = item.get("text", "")
                if isinstance(tx, str):
                    parts.append(tx)
        return "\n".join(parts).strip()
    return ""


def _parse_ts(s):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _project_of(path, cwd):
    if cwd:
        base = os.path.basename(cwd.rstrip("/"))
        if base:
            return base
        return "~"
    seg = os.path.basename(os.path.dirname(path)).split("-")
    return seg[-1] if seg and seg[-1] else "?"


def search_raw(ctx: TenantContext, terms, since=None, until=None,
               project=None, limit=DEFAULT_LIMIT):
    s_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=ctx.tz) if since else None
    u_dt = (datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=ctx.tz) + timedelta(days=1)) if until else None
    mtime_floor = (s_dt - timedelta(days=1)).timestamp() if s_dt else None

    hits = []
    for path in glob.glob(os.path.join(ctx.projects_dir, "*", "*.jsonl")):
        try:
            if mtime_floor and os.path.getmtime(path) < mtime_floor:
                continue
        except OSError:
            continue
        cwd = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    typ = rec.get("type")
                    if typ == "user" and rec.get("cwd"):
                        cwd = rec.get("cwd")
                    if typ not in ("user", "assistant"):
                        continue
                    ts = _parse_ts(rec.get("timestamp"))
                    if ts is None:
                        continue
                    lt = ts.astimezone(ctx.tz)
                    if s_dt and lt < s_dt:
                        continue
                    if u_dt and lt >= u_dt:
                        continue
                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    text = _extract_text(msg.get("content"))
                    if not text:
                        continue
                    low = text.lower()
                    if not all(t in low for t in terms):
                        continue
                    proj = _project_of(path, cwd)
                    if project and project.lower() not in proj.lower():
                        continue
                    snippet = " ".join(text.split())
                    if len(snippet) > 400:
                        snippet = snippet[:400] + "…"
                    sid = os.path.splitext(os.path.basename(path))[0]
                    hits.append({"time": lt.strftime("%Y-%m-%d %H:%M"), "role": typ,
                                 "project": proj, "session": sid[:8], "snippet": snippet})
        except OSError:
            continue
    hits.sort(key=lambda h: h["time"])
    return hits[:limit]


# ---------- text rendering (shared by MCP tools) ----------

def render_results(terms, thread_hits, timeline_hits):
    out = []
    if thread_hits:
        out.append("=== Work threads (%d hits, query: %s) ===\n" % (len(thread_hits), " ".join(terms)))
        for c in thread_hits:
            via = ("  ← merged from: %s" % ", ".join(c["via"])) if c["via"] else ""
            out.append("● %s   (%d/%d terms matched, %d occurrences, latest %s)%s"
                       % (c["name"], c["distinct"], len(terms), c["total"], c["last_date"] or "-", via))
            if c.get("current_state"):
                out.append("  [current state]")
                for ln in c["current_state"].splitlines():
                    if ln.strip():
                        out.append("    %s" % ln.strip())
            for ln in c["lines"]:
                out.append("    %s" % ln)
            out.append("")
    if timeline_hits:
        out.append("=== Timeline search results (%d hits, query: %s) ===\n" % (len(timeline_hits), " ".join(terms)))
        for h in timeline_hits:
            out.append("● [%s] %s   (%d/%d terms matched, %d occurrences)"
                       % (h["date"], h["heading"], h["distinct"], len(terms), h["total"]))
            for ln in h["lines"]:
                out.append("    %s" % ln)
            out.append("")
    if not thread_hits and not timeline_hits:
        out.append("No matches in the timeline. Try recall_raw for raw conversations, or change your keywords.")
    return "\n".join(out)


def render_raw(terms, hits, project=None):
    if not hits:
        return ("No matches in raw conversations (query: %s%s). Try adjusting the date range or keywords."
                % (" ".join(terms), (", project=" + project) if project else ""))
    out = ["=== Raw conversation search results (%d hits, query: %s) ===\n" % (len(hits), " ".join(terms))]
    for h in hits:
        out.append("● %s · %s · [%s] %s" % (h["time"], h["project"], h["role"], h["session"]))
        out.append("    %s" % h["snippet"])
        out.append("")
    return "\n".join(out)
