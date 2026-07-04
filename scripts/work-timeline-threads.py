#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Group the daily work-timeline sessions into "work threads" and append them to threads/<slug>.md.

- Session source: reuses collect_sessions (jsonl parsing) from work-timeline.py.
- Classification: match against the registry (threads/_registry.json) -> if found, append to that thread,
  otherwise the LLM creates a new name (slug+name) and adds it. (Branch matches are pre-matched deterministically, without the LLM.)
- Processed incrementally in ascending date order, so threads created on earlier days become matching candidates for later days.
- Idempotent per sid: already-processed sessions are skipped, so re-running a backfill is safe.

Usage:
  work-timeline-threads.py                       # from after the cursor up to yesterday (unprocessed)
  work-timeline-threads.py --since 2026-05-22    # backfill
  work-timeline-threads.py --dry-run             # no file/registry changes, prints assignments only
"""
import os
import re
import json
import argparse
import importlib.util
from datetime import datetime, timedelta
from collections import defaultdict

HOME = os.path.expanduser("~")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))  # sibling scripts (repo checkout or installed dir alike)

# Reuse work-timeline.py helpers (hyphenated filename -> importlib)
_spec = importlib.util.spec_from_file_location(
    "work_timeline", os.path.join(SCRIPTS_DIR, "work-timeline.py"))
if _spec is None or _spec.loader is None:
    raise RuntimeError("failed to load work-timeline.py")
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

THREADS_DIR = os.path.join(wt.OUTPUT_DIR, "threads")
REGISTRY_FILE = os.path.join(THREADS_DIR, "_registry.json")
CURSOR_FILE = os.path.join(THREADS_DIR, "_cursor.json")

SUMMARY_LANG = os.environ.get("CCRECALL_SUMMARY_LANG", "English")
BACKFILL_FLOOR = "2026-05-22"   # same as the start date of the cleaned-up timeline backfill
HEADLINE_TRUNC = 120
DIGEST_PROMPT_TRUNC = 140
MAX_SESSION_PROMPTS = 3          # number of prompts per session to include in the LLM digest
RECENT_TITLES_KEEP = 5           # number of recent work titles to keep in the registry
REGISTRY_ACTIVE_DAYS = 28        # threshold for "active" threads to include in the LLM prompt
REGISTRY_MAX_IN_PROMPT = 50      # if still too many, cut to the most recent ones


# ---------- storage ----------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- slug ----------

def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s


def unique_slug(base, registry):
    base = base or "thread"
    if base not in registry:
        return base
    i = 2
    while "%s-%d" % (base, i) in registry:
        i += 1
    return "%s-%d" % (base, i)


# ---------- session collection ----------

def flatten(text, limit):
    one = " ".join((text or "").split())
    return one[:limit] + "…" if len(one) > limit else one


# Prompt signatures of the headless summary sessions that the timeline automation itself spawns.
# Matched as substrings against prompt text so these sessions are filtered out of threads.
META_SIGNATURES = (
    "[work-timeline-internal]",
)


def is_meta_prompt(text):
    return any(sig in text for sig in META_SIGNATURES)


def collect_day(day_start):
    """Return one day's sessions as [(sid, session)], in ascending first_active order.
    Sessions with no real user prompts (headless automation) or that are timeline-summary runs are not tracked, so they are excluded."""
    sessions = wt.collect_sessions(day_start, day_start + timedelta(days=1))
    items = []
    for sid, s in sessions.items():
        real = [(lt, t) for lt, t in s.get("prompts", []) if not is_meta_prompt(t)]
        if not real:
            continue
        s["prompts"] = real
        items.append((sid, s))
    return sorted(items, key=lambda kv: kv[1]["first_active"])


def session_headline(s):
    if s.get("title"):
        return s["title"]
    if s.get("prompts"):
        return flatten(s["prompts"][0][1], HEADLINE_TRUNC)
    return "(no title)"


# ---------- deterministic matching ----------

# Generic branches that don't identify a task -- excluded from deterministic matching/identifiers (avoids catch-all).
GENERIC_BRANCHES = {"HEAD", "main", "master", "develop", "dev", "release", "staging"}


def trackable_branch(s):
    br = s.get("branch")
    return br if br and br not in GENERIC_BRANCHES else None


def deterministic_slug(s, registry):
    br = trackable_branch(s)
    if not br:
        return None
    for slug, e in registry.items():
        if br in e.get("branches", []):
            return slug
    return None


# ---------- LLM matching ----------

PROMPT = """[work-timeline-internal]
You classify development work sessions into "work threads".
For each session, if it is the same as or a continuation of one of the [Existing threads] below, assign it to that slug;
if it doesn't fit any of them, create a new thread.

[Existing threads]
{threads}

[Sessions to classify]
{sessions}

Rules:
- Assign each session to exactly one thread.
- If a session is the same as or a continuation of an existing thread, you must use that slug. When in doubt, prefer an existing thread over creating a new one.
- A new thread's slug must be lowercase English with hyphens (kebab-case); name must be a short noun phrase summarizing the work, written in {lang}.
- Output a single JSON object only. No explanation/markdown/code fences.

Output format (example):
{{"S1": {{"slug": "ecs-fargate-cost"}}, "S2": {{"slug": "new-slug", "name": "new task name", "new": true}}}}
"""


def active_threads_for_prompt(registry, day_str):
    items = list(registry.values())
    cutoff = (datetime.strptime(day_str, "%Y-%m-%d")
              - timedelta(days=REGISTRY_ACTIVE_DAYS)).strftime("%Y-%m-%d")
    items = [e for e in items if e.get("last_active", "") >= cutoff]
    items.sort(key=lambda e: e.get("last_active", ""), reverse=True)
    return items[:REGISTRY_MAX_IN_PROMPT]


def render_threads_block(registry, day_str):
    items = active_threads_for_prompt(registry, day_str)
    if not items:
        return "(none)"
    lines = []
    for e in items:
        recent = "; ".join(e.get("recent_titles", [])[-2:])
        lines.append(
            "- slug=%s | name=%s | project=%s | branch=%s | last_active=%s | recent work: %s"
            % (e["slug"], e["name"],
               ",".join(e.get("projects", [])) or "-",
               ",".join(e.get("branches", [])) or "-",
               e.get("last_active", "-"),
               flatten(recent, DIGEST_PROMPT_TRUNC) or "-"))
    return "\n".join(lines)


def render_sessions_block(unmatched):
    lines = []
    for key, (_, s) in unmatched.items():
        ups = " / ".join(flatten(t, DIGEST_PROMPT_TRUNC)
                         for _, t in s.get("prompts", [])[:MAX_SESSION_PROMPTS])
        br = s.get("branch") if s.get("branch") and s["branch"] != "HEAD" else "-"
        lines.append('%s) project=%s branch=%s title="%s" input gist: %s'
                     % (key, s["project"], br, session_headline(s), ups or "-"))
    return "\n".join(lines)


def parse_llm_json(text):
    if not text:
        return {}
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return {}
    try:
        return json.loads(text[a:b + 1])
    except Exception:
        return {}


def llm_assign(unmatched, registry, day_str):
    """unmatched: {Sk: (sid, s)} → {Sk: {slug, name?, new?}}"""
    prompt = PROMPT.format(
        threads=render_threads_block(registry, day_str),
        sessions=render_sessions_block(unmatched),
        lang=SUMMARY_LANG)
    try:
        out = wt.run_claude(prompt)
    except Exception as ex:
        print("  ! LLM call failed, falling back for all: %s" % ex)
        return {}
    return parse_llm_json(out)


# ---------- thread update ----------

def new_entry(slug, name):
    return {"slug": slug, "name": name, "projects": [], "branches": [],
            "sids": [], "recent_titles": [], "first_active": "", "last_active": "",
            "count": 0}


def resolve_decision(dec, s, registry):
    """Resolve an LLM decision (or None) into (slug, entry). Creates a new registry entry if new."""
    if isinstance(dec, dict):
        slug = slugify(dec.get("slug"))      # normalize slug for both lookup and creation (absorbs case/formatting differences)
        if slug and slug in registry:        # prefer an existing thread (or one created earlier in the same batch)
            return slug
        if slug:                             # even without the new flag, create from the slug the LLM gave (avoids fragmentation within the same batch)
            slug = unique_slug(slug, registry)
            registry[slug] = new_entry(slug, dec.get("name") or session_headline(s))
            return slug
    # fallback: new thread based on branch -> project
    base = slugify(trackable_branch(s)) or slugify(s.get("project"))
    slug = unique_slug(base, registry)
    registry[slug] = new_entry(slug, session_headline(s))
    return slug


def update_entry(e, s, sid, day_str):
    if sid not in e["sids"]:
        e["sids"].append(sid)
    proj = s.get("project")
    if proj and proj not in e["projects"]:
        e["projects"].append(proj)
    br = trackable_branch(s)
    if br and br not in e["branches"]:
        e["branches"].append(br)
    title = session_headline(s)
    e["recent_titles"].append(title)
    e["recent_titles"] = e["recent_titles"][-RECENT_TITLES_KEEP:]
    if not e["first_active"] or day_str < e["first_active"]:
        e["first_active"] = day_str
    if day_str > e["last_active"]:
        e["last_active"] = day_str
    e["count"] += 1


def format_line(s, sid):
    hm = s["first_active"].strftime("%H:%M")
    br = (" · `%s`" % s["branch"]) if s.get("branch") and s["branch"] != "HEAD" else ""
    return "- `%s` [%s%s] %s  ·%s" % (
        hm, s["project"], br, flatten(session_headline(s), HEADLINE_TRUNC), sid[:8])


THREAD_DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$")


def render_thread_header(e):
    span = e["first_active"]
    if e["last_active"] and e["last_active"] != e["first_active"]:
        span += " ~ " + e["last_active"]
    return "\n".join([
        "# %s" % e["name"],
        "",
        "- slug: `%s`" % e["slug"],
        "- project: %s" % (", ".join(e.get("projects", [])) or "-"),
        "- branch: %s" % (", ".join("`%s`" % b for b in e.get("branches", [])) or "-"),
        "- span: %s  · %d sessions" % (span, e["count"]),
        "",
    ])


def write_thread(slug, e, new_lines_by_date):
    """Regenerate the header (from the registry) every time, and append new lines to the date sections."""
    path = os.path.join(THREADS_DIR, "%s.md" % slug)
    sections = {}     # date -> [lines]
    order = []        # preserve the order in which dates appear
    if os.path.exists(path):
        cur = None
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read().splitlines()
        for line in existing:
            m = THREAD_DATE_RE.match(line)
            if m:
                cur = m.group(1)
                if cur not in sections:
                    sections[cur] = []
                    order.append(cur)
            elif cur is not None and line.startswith("- `"):
                sections[cur].append(line)
    for date, lines in sorted(new_lines_by_date.items()):
        if date not in sections:
            sections[date] = []
            order.append(date)
        sections[date].extend(lines)
    order = sorted(order)
    out = [render_thread_header(e)]
    for date in order:
        out.append("## %s\n" % date)
        out.append("\n".join(sections[date]) + "\n")
    os.makedirs(THREADS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip() + "\n")


# ---------- main ----------

def day_range(start_date, end_excl):
    d = start_date
    while d < end_excl:
        yield d
        d += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="process from YYYY-MM-DD (backfill). If omitted, from after the cursor.")
    ap.add_argument("--dry-run", action="store_true", help="no file/registry changes, prints assignments only")
    args = ap.parse_args()

    tz = wt.local_tz()
    now = datetime.now(tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")

    if args.since:
        start = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=tz)
    else:
        cur = load_json(CURSOR_FILE, {})
        last = cur.get("last_date")
        if last:
            start = datetime.strptime(last, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)
        else:
            start = today - timedelta(days=1)   # first run: yesterday only
    floor = datetime.strptime(BACKFILL_FLOOR, "%Y-%m-%d").replace(tzinfo=tz)
    if start < floor:
        start = floor

    if start >= today:
        print("[%s] no completed days to process (start=%s)" % (stamp, start.strftime("%Y-%m-%d")))
        return

    registry = load_json(REGISTRY_FILE, {})
    processed = set()
    for e in registry.values():
        processed.update(e.get("sids", []))

    threads_new = defaultdict(lambda: defaultdict(list))  # slug -> date -> [line]
    last_done = None
    total = 0

    for day in day_range(start, today):
        day_str = day.strftime("%Y-%m-%d")
        sessions = [(sid, s) for sid, s in collect_day(day) if sid not in processed]
        last_done = day_str
        if not sessions:
            continue

        # 1) deterministic pre-matching (branch) + separate out the unmatched
        assigned = {}
        unmatched = {}
        for i, (sid, s) in enumerate(sessions, 1):
            slug = deterministic_slug(s, registry)
            if slug:
                assigned[sid] = slug
            else:
                unmatched["S%d" % i] = (sid, s)

        # 2) the unmatched are assigned by the LLM
        decisions = llm_assign(unmatched, registry, day_str) if unmatched else {}
        for key, (sid, s) in unmatched.items():
            assigned[sid] = resolve_decision(decisions.get(key), s, registry)

        # 3) apply
        for sid, s in sessions:
            slug = assigned[sid]
            update_entry(registry[slug], s, sid, day_str)
            processed.add(sid)
            threads_new[slug][day_str].append(format_line(s, sid))
            total += 1
        print("  %s: %d sessions → %d threads"
              % (day_str, len(sessions), len(set(assigned.values()))))

    if args.dry_run:
        print("[%s] (dry-run) %s ~ yesterday, %d sessions, %d threads (not saved)"
              % (stamp, start.strftime("%Y-%m-%d"), total, len(threads_new)))
        for slug, by_date in threads_new.items():
            print("  - %s (%s): %s" % (slug, registry[slug]["name"],
                                       sum(len(v) for v in by_date.values())))
        return

    for slug, by_date in threads_new.items():
        write_thread(slug, registry[slug], by_date)
    save_json(REGISTRY_FILE, registry)
    if last_done:
        save_json(CURSOR_FILE, {"last_date": last_done})
    print("[%s] processing complete: %s ~ yesterday, %d sessions, %d thread files updated"
          % (stamp, start.strftime("%Y-%m-%d"), total, len(threads_new)))


if __name__ == "__main__":
    main()
