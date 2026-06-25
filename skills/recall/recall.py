#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Search past Claude Code work. Two-tier structure:

  Tier 1 (default): search ~/.claude/work-timeline/*.md (curated daily/hourly summaries).
                    Quickly find "when / in which project / what was done".
  Tier 2 (--raw):   search ~/.claude/projects/**/*.jsonl (raw conversations).
                    When you need an exact phrase/code/error message, narrow to that date and dig.

Usage:
  recall.py "fargate cost"                       # search the timeline
  recall.py "ssl regression" --raw --since 2026-06-12  # exact phrase from raw conversations
  recall.py "alert" --raw --since 2026-06-22 --until 2026-06-23 --project my-api
"""
import os
import re
import json
import glob
import argparse
from datetime import datetime, timedelta

HOME = os.path.expanduser("~")
TIMELINE_DIR = os.path.join(HOME, ".claude", "work-timeline")
THREADS_DIR = os.path.join(TIMELINE_DIR, "threads")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")

LINE_TRUNC = 200          # max length when displaying a matched line
MAX_LINES_PER_HIT = 8     # number of matched lines to show per block
DEFAULT_LIMIT = 15


def terms_of(query):
    """Turn a query into search-term tokens. Split on whitespace, lowercase (Latin), drop empty tokens."""
    return [t.lower() for t in query.split() if t.strip()]


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


def search_timeline(terms, limit):
    hits = []  # (distinct, total, date, heading, lines)
    for path in sorted(glob.glob(os.path.join(TIMELINE_DIR, "[0-9]" * 4 + "-*.md"))):
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
            hits.append((d, t, date, heading, matched_lines(body, terms)))
    hits.sort(key=lambda h: (h[0], h[1], h[2]), reverse=True)
    return hits[:limit]


def print_timeline_hits(hits, terms):
    if not hits:
        print("No matches in the timeline. Try --raw to search raw conversations, or change your keywords.")
        return
    print("=== Timeline search results (%d hits, query: %s) ===\n" % (len(hits), " ".join(terms)))
    for d, t, date, heading, lines in hits:
        print("● [%s] %s   (%d/%d terms matched, %d occurrences)" % (date, heading, d, len(terms), t))
        for ln in lines:
            print("    %s" % ln)
        print()


# ---------- Tier 1.5: work threads ----------

THREAD_DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})", re.M)
REGISTRY_FILE = os.path.join(THREADS_DIR, "_registry.json")


def load_registry():
    try:
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
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


def search_threads(terms, limit, registry):
    """Search threads/<slug>.md, grouping aliases under their canonical and returning the canonical's current state.
    Searching by an old name (alias) resolves to the merged canonical truth."""
    clusters = {}  # canonical -> aggregation dict
    for path in sorted(glob.glob(os.path.join(THREADS_DIR, "*.md"))):
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
            c = {"d": 0, "t": 0, "lines": [], "path": path, "last_date": "",
                 "name": e.get("name") or h1_of(content) or canonical,
                 "current_state": e.get("current_state"), "via": set()}
            clusters[canonical] = c
        if slug != canonical:                       # matched on an alias file
            c["via"].add(registry.get(slug, {}).get("name") or slug)
        if (d, t) > (c["d"], c["t"]):               # take matched lines from the highest-scoring file
            c["d"], c["t"] = d, t
            c["lines"] = matched_lines(content, terms)
            dates = THREAD_DATE_RE.findall(content)
            c["last_date"] = dates[-1] if dates else ""
        if slug == canonical:                        # display path points to the canonical file
            c["path"] = path
    ranked = sorted(clusters.values(),
                    key=lambda c: (c["d"], c["t"], c["last_date"]), reverse=True)
    return ranked[:limit]


def print_thread_hits(hits, terms):
    if not hits:
        return
    print("=== Work threads (%d hits, query: %s) ===\n" % (len(hits), " ".join(terms)))
    for c in hits:
        via = ("  ← merged from: %s" % ", ".join(sorted(c["via"]))) if c["via"] else ""
        print("● %s   (%d/%d terms matched, %d occurrences, latest %s)%s"
              % (c["name"], c["d"], len(terms), c["t"], c["last_date"] or "-", via))
        if c.get("current_state"):
            print("  [current state]")
            for ln in c["current_state"].splitlines():
                if ln.strip():
                    print("    %s" % ln.strip())
        for ln in c["lines"]:
            print("    %s" % ln)
        print("    ↳ %s" % c["path"])
        print()


# ---------- Tier 2: raw transcript ----------

def extract_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            typ = item.get("type")
            if typ == "text":
                tx = item.get("text", "")
                if isinstance(tx, str):
                    parts.append(tx)
        return "\n".join(parts).strip()
    return ""


def parse_ts(s):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def project_of(path, cwd):
    if cwd:
        base = os.path.basename(cwd.rstrip("/"))
        if base:
            return base
        return "~"
    seg = os.path.basename(os.path.dirname(path)).split("-")
    return seg[-1] if seg and seg[-1] else "?"


def search_raw(terms, since, until, project, limit):
    tz = datetime.now().astimezone().tzinfo
    s_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=tz) if since else None
    u_dt = (datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)) if until else None
    mtime_floor = (s_dt - timedelta(days=1)).timestamp() if s_dt else None

    hits = []  # (local_dt, role, project, path, snippet)
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
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
                    ts = parse_ts(rec.get("timestamp"))
                    if ts is None:
                        continue
                    lt = ts.astimezone(tz)
                    if s_dt and lt < s_dt:
                        continue
                    if u_dt and lt >= u_dt:
                        continue
                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    text = extract_text(msg.get("content"))
                    if not text:
                        continue
                    low = text.lower()
                    if not all(t in low for t in terms):
                        continue
                    proj = project_of(path, cwd)
                    if project and project.lower() not in proj.lower():
                        continue
                    snippet = " ".join(text.split())
                    if len(snippet) > 400:
                        snippet = snippet[:400] + "…"
                    hits.append((lt, typ, proj, path, snippet))
        except OSError:
            continue
    hits.sort(key=lambda h: h[0])
    return hits[:limit]


def print_raw_hits(hits, terms, project):
    if not hits:
        print("No matches in raw conversations (query: %s%s). Try adjusting the date range or keywords."
              % (" ".join(terms), (", project=" + project) if project else ""))
        return
    print("=== Raw conversation search results (%d hits, query: %s) ===\n" % (len(hits), " ".join(terms)))
    for lt, role, proj, path, snippet in hits:
        sid = os.path.splitext(os.path.basename(path))[0]
        print("● %s · %s · [%s] %s" % (lt.strftime("%Y-%m-%d %H:%M"), proj, role, sid[:8]))
        print("    %s" % snippet)
        print("    ↳ %s" % path)
        print()


def main():
    ap = argparse.ArgumentParser(description="Search past Claude Code work")
    ap.add_argument("query", help="search terms (multiple keywords separated by spaces)")
    ap.add_argument("--raw", action="store_true", help="search raw transcripts (Tier 2)")
    ap.add_argument("--since", help="only on or after YYYY-MM-DD (raw)")
    ap.add_argument("--until", help="only on or before YYYY-MM-DD (raw, inclusive)")
    ap.add_argument("--project", help="filter by partial project-name match (raw)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="maximum number of results")
    args = ap.parse_args()

    terms = terms_of(args.query)
    if not terms:
        print("Search query is empty.")
        return

    if args.raw:
        hits = search_raw(terms, args.since, args.until, args.project, args.limit)
        print_raw_hits(hits, terms, args.project)
    else:
        registry = load_registry()
        print_thread_hits(search_threads(terms, args.limit, registry), terms)
        print_timeline_hits(search_timeline(terms, args.limit), terms)


if __name__ == "__main__":
    main()
