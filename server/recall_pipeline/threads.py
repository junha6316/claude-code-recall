# -*- coding: utf-8 -*-
"""Work-thread grouping — port of scripts/work-timeline-threads.py.

Groups a tenant's daily sessions into "work threads" and appends them to
{threads_dir}/<slug>.md, keyed by a registry (_registry.json).

- Classification: deterministic branch pre-matching, then LLM assignment for
  the rest.
- Processed incrementally in ascending date order (completed days only —
  today is excluded), so earlier threads become matching candidates later.
- Idempotent per sid: already-processed sessions are skipped.

Differences from the local script:
  - No importlib loading of work-timeline.py — plain module imports.
  - backfill_floor comes from ctx (the local BACKFILL_FLOOR hardcodes the
    author's own timeline start date, threads.py:41).
  - Failure semantics fixed: registry/cursor are saved after each COMPLETED
    day; an LLM failure propagates and the failed day is retried next run.
    (The local script falls back to branch/project slugs and marks the
    sessions processed, so a failed day is never re-classified.)
"""
import os
import re
import json
from datetime import datetime, timedelta
from collections import defaultdict

from .context import TenantContext
from . import llm, timeline

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
    """Atomic write — the registry is read-modify-write with no lock in the
    local script (review item D); atomicity plus per-tenant serialization in
    the worker covers Phase 0. Phase 2 adds a tenant-scope lock."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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


# Prompt signatures of the headless summary sessions the LOCAL timeline
# automation spawns — uploaded transcripts can contain them.
META_SIGNATURES = (
    "[work-timeline-internal]",
)


def is_meta_prompt(text):
    return any(sig in text for sig in META_SIGNATURES)


def collect_day(ctx: TenantContext, day_start):
    """Return one day's sessions as [(sid, session)], in ascending first_active order.
    Sessions with no real user prompts (headless automation) or that are timeline-summary runs are not tracked, so they are excluded."""
    sessions = timeline.collect_sessions(ctx, day_start, day_start + timedelta(days=1))
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


NAME_MATCH_MIN_KEY = 6   # normalized chars; below this, titles are too generic to be identity


def norm_title(text):
    """Comparison key for headline↔thread-name identity (case/space/punct-insensitive)."""
    return re.sub(r"[^0-9a-z가-힣]+", "", (text or "").lower())


def _canonical_of(slug, registry):
    seen = set()
    while slug in registry and registry[slug].get("alias_of") and slug not in seen:
        seen.add(slug)
        slug = registry[slug]["alias_of"]
    return slug


def name_match_slug(s, registry):
    """Deterministic prematch: a session whose headline equals an existing
    thread's name (or one of its recent titles) after normalization belongs to
    that thread. Exact equality only — paraphrases stay with the LLM; a false
    merge is worse than a duplicate because alias links are sticky."""
    key = norm_title(session_headline(s))
    if len(key) < NAME_MATCH_MIN_KEY:
        return None
    for slug, e in registry.items():
        if norm_title(e.get("name")) == key or \
                any(norm_title(t) == key for t in e.get("recent_titles", [])):
            return _canonical_of(slug, registry)
    return None


# ---------- LLM matching ----------

PROMPT = """You classify development work sessions into "work threads".
For each session, if it is the same as or a continuation of one of the [Existing threads] below, assign it to that slug;
if it doesn't fit any of them, create a new thread.

[Existing threads]
{threads}

[Sessions to classify]
{sessions}

Rules:
- Assign each session to exactly one thread.
- If a session is the same as or a continuation of an existing thread, you must use that slug. When in doubt, prefer an existing thread over creating a new one.
- `subject=` is the repo the session's tools actually touched; `project=` is only the folder it ran in. A session whose subject matches a thread's subject/project belongs there even if the folders differ.
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
            "- slug=%s | name=%s | project=%s | subject=%s | branch=%s | last_active=%s | recent work: %s"
            % (e["slug"], e["name"],
               ",".join(e.get("projects", [])) or "-",
               ",".join(e.get("subjects", [])) or "-",
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
        lines.append('%s) project=%s subject=%s branch=%s title="%s" input gist: %s'
                     % (key, s["project"], s.get("subject") or "-", br,
                        session_headline(s), ups or "-"))
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


def llm_assign(ctx: TenantContext, unmatched, registry, day_str):
    """unmatched: {Sk: (sid, s)} → {Sk: {slug, name?, new?}}.
    Raises llm.LLMError — the caller retries the whole day next run."""
    prompt = PROMPT.format(
        threads=render_threads_block(registry, day_str),
        sessions=render_sessions_block(unmatched),
        lang=ctx.summary_lang)
    out = llm.complete(prompt, model=ctx.model, timeout=ctx.llm_timeout)
    return parse_llm_json(out)


# ---------- thread update ----------

def new_entry(slug, name):
    return {"slug": slug, "name": name, "projects": [], "subjects": [], "branches": [],
            "sids": [], "recent_titles": [], "first_active": "", "last_active": "",
            "count": 0}


def resolve_decision(dec, s, registry):
    """Resolve an LLM decision (or None) into a slug. Creates a new registry entry if new."""
    if isinstance(dec, dict):
        slug = slugify(dec.get("slug"))      # normalize slug for both lookup and creation (absorbs case/formatting differences)
        if slug and slug in registry:        # prefer an existing thread (or one created earlier in the same batch)
            return slug
        if slug:                             # even without the new flag, create from the slug the LLM gave (avoids fragmentation within the same batch)
            # A thread with this exact name may exist outside the candidate
            # window (or was just created in this batch) — join it instead.
            existing = name_match_slug(s, registry)
            if existing:
                return existing
            slug = unique_slug(slug, registry)
            registry[slug] = new_entry(slug, dec.get("name") or session_headline(s))
            return slug
    # fallback: new thread based on branch -> project
    existing = name_match_slug(s, registry)
    if existing:
        return existing
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
    subj = s.get("subject")
    if subj and subj not in e.setdefault("subjects", []):   # setdefault: pre-subject registry entries
        e["subjects"].append(subj)
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
    head = flatten(session_headline(s), HEADLINE_TRUNC)
    # The headline comes from the first prompt, so the later part of a long session
    # is invisible. Append the last input as a hint of how far the session went.
    tail = ""
    prompts = s.get("prompts") or []
    if len(prompts) > 1:
        last = flatten(prompts[-1][1], 80)
        if last and last not in head:
            tail = " (last input: %s)" % last
    return "- `%s` [%s%s] %s%s  ·%s" % (hm, s["project"], br, head, tail, sid[:8])


THREAD_DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$")


def render_thread_header(e):
    span = e["first_active"]
    if e["last_active"] and e["last_active"] != e["first_active"]:
        span += " ~ " + e["last_active"]
    lines = [
        "# %s" % e["name"],
        "",
        "- slug: `%s`" % e["slug"],
        "- project: %s" % (", ".join(e.get("projects", [])) or "-"),
    ]
    if e.get("subjects"):
        lines.append("- subject: %s" % ", ".join(e["subjects"]))
    lines += [
        "- branch: %s" % (", ".join("`%s`" % b for b in e.get("branches", [])) or "-"),
        "- span: %s  · %d sessions" % (span, e["count"]),
        "",
    ]
    return "\n".join(lines)


def write_thread(ctx: TenantContext, slug, e, new_lines_by_date):
    """Regenerate the header (from the registry) every time, and append new lines to the date sections."""
    path = os.path.join(ctx.threads_dir, "%s.md" % slug)
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
    os.makedirs(ctx.threads_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip() + "\n")


# ---------- main ----------

def day_range(start_date, end_excl):
    d = start_date
    while d < end_excl:
        yield d
        d += timedelta(days=1)


def run_threads(ctx: TenantContext, since=None, dry_run=False):
    """Process completed days from the cursor (or `since`) up to yesterday.

    Registry/cursor/thread files are saved after each completed day, so an LLM
    failure mid-run keeps everything through the previous day and the failed
    day is retried on the next invocation."""
    now = datetime.now(ctx.tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")

    if since:
        start = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=ctx.tz)
    else:
        cur = load_json(ctx.threads_cursor_file, {})
        last = cur.get("last_date")
        if last:
            start = datetime.strptime(last, "%Y-%m-%d").replace(tzinfo=ctx.tz) + timedelta(days=1)
        else:
            start = today - timedelta(days=1)   # first run: yesterday only
    if ctx.backfill_floor:
        floor = datetime.strptime(ctx.backfill_floor, "%Y-%m-%d").replace(tzinfo=ctx.tz)
        if start < floor:
            start = floor

    if start >= today:
        print("[%s] no completed days to process (start=%s)" % (stamp, start.strftime("%Y-%m-%d")))
        return

    registry = load_json(ctx.registry_file, {})
    processed = set()
    for e in registry.values():
        processed.update(e.get("sids", []))

    total = 0
    days_done = 0

    for day in day_range(start, today):
        day_str = day.strftime("%Y-%m-%d")
        sessions = [(sid, s) for sid, s in collect_day(ctx, day) if sid not in processed]
        if sessions:
            # 1) deterministic pre-matching (branch, then exact-name) + separate out the unmatched
            assigned = {}
            unmatched = {}
            for i, (sid, s) in enumerate(sessions, 1):
                slug = deterministic_slug(s, registry) or name_match_slug(s, registry)
                if slug:
                    assigned[sid] = slug
                else:
                    unmatched["S%d" % i] = (sid, s)

            # 2) the unmatched are assigned by the LLM (LLMError propagates —
            #    nothing for this day has been saved yet, so it retries cleanly)
            decisions = llm_assign(ctx, unmatched, registry, day_str) if unmatched else {}
            for key, (sid, s) in unmatched.items():
                assigned[sid] = resolve_decision(decisions.get(key), s, registry)

            # 3) apply
            threads_new = defaultdict(lambda: defaultdict(list))  # slug -> date -> [line]
            for sid, s in sessions:
                slug = assigned[sid]
                update_entry(registry[slug], s, sid, day_str)
                processed.add(sid)
                threads_new[slug][day_str].append(format_line(s, sid))
                total += 1
            print("  %s: %d sessions → %d threads"
                  % (day_str, len(sessions), len(set(assigned.values()))))

            if dry_run:
                for slug, by_date in threads_new.items():
                    print("  - [dry] %s (%s): %s" % (slug, registry[slug]["name"],
                                                     sum(len(v) for v in by_date.values())))
            else:
                for slug, by_date in threads_new.items():
                    write_thread(ctx, slug, registry[slug], by_date)

        # Save per completed day — the cursor never passes a failed day.
        if not dry_run:
            save_json(ctx.registry_file, registry)
            save_json(ctx.threads_cursor_file, {"last_date": day_str})
        days_done += 1

    print("[%s] processing complete: %s ~ yesterday (%d days), %d sessions"
          % (stamp, start.strftime("%Y-%m-%d"), days_done, total))
