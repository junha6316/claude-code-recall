# -*- coding: utf-8 -*-
"""Thread consolidation + canonical synthesis — port of scripts/work-timeline-consolidate.py.

Two stages:
  1. Consolidation (coreference): use an LLM to cluster threads about the same topic and link them to one canonical thread.
     - Non-destructive: doesn't touch files/entries; only records alias_of (member→canonical)/aliases in the registry.
     - Sticky: once an alias_of is established it's never auto-removed (manual corrections go through editing _registry.json).
  2. Canonical synthesis: for each multi-session canonical, read every member's full record in chronological order and synthesize the "current state (canonical)".
     Make temporal supersession/contradictions explicit. Only re-synthesize clusters that changed.

Failure semantics: both stages are naturally retryable — a failed cluster pass
just links nothing (next run retries), and a failed synthesis leaves
state_synth_at stale so that slug is re-picked next run. Per-slug failures are
counted and re-raised at the end so the worker knows the run was incomplete.
"""
import os
import re
from datetime import datetime

from .context import TenantContext
from . import llm, threads as th

SYNTH_MIN_SESSIONS = 2     # for a single-session thread the entry itself is the state → skip synthesis
SYNTH_MAX_LINES = 150      # cap on record lines fed to the synthesis prompt (most recent kept)
DIGEST_TRUNC = 300         # length cap for one daily-log excerpt line
ENTRY_RE = re.compile(r"^- `(\d{2}:\d{2})` (.+?)\s*·([0-9a-f]{8})\s*$")
DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$")
ENTRY_PROJ_RE = re.compile(r"^\[([^\]]+)\]")
DAY_BLOCK_RE = re.compile(r"^## (\d{2}:\d{2})–\d{2}:\d{2}\s*$")
DAY_DIGEST_RE = re.compile(r"^- \*\*(.+?)\*\*(?: · `[^`]+`)? — (.+)$")


def thread_file(ctx: TenantContext, slug):
    return os.path.join(ctx.threads_dir, "%s.md" % slug)


def read_entries(ctx: TenantContext, slug):
    """Return a chronological list of (date, hm, text) from the thread's .md."""
    path = thread_file(ctx, slug)
    if not os.path.exists(path):
        return []
    out = []
    cur = None
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    for line in lines:
        m = DATE_RE.match(line)
        if m:
            cur = m.group(1)
            continue
        e = ENTRY_RE.match(line)
        if e and cur:
            out.append((cur, e.group(1), e.group(2).strip()))
    return sorted(out)


def day_digests(ctx: TenantContext, date, cache):
    """Return (hm, project, digest) list from the daily timeline md.

    A thread entry is a single line per session (headline from the first prompt),
    so for a session that starts in the morning and runs all day, everything after
    the first prompt is invisible to synthesis. The per-time-block daily-log
    digests fill that gap. `cache` is a per-run dict (no module-level cache —
    a long-lived worker serves many tenants)."""
    if date in cache:
        return cache[date]
    path = os.path.join(ctx.output_dir, "%s.md" % date)
    out = []
    if os.path.exists(path):
        cur_hm = None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = DAY_BLOCK_RE.match(line)
                if m:
                    cur_hm = m.group(1)
                    continue
                if cur_hm is None:
                    continue  # skip everything outside time blocks (e.g. the daily summary)
                d = DAY_DIGEST_RE.match(line.rstrip("\n"))
                if d:
                    proj = d.group(1).split(" · ")[0].strip()
                    out.append((cur_hm, proj, d.group(2).strip()))
    cache[date] = out
    return out


def entry_project(text):
    m = ENTRY_PROJ_RE.match(text)
    return m.group(1).split(" · ")[0].strip() if m else None


# ---------- Stage 1: consolidation (coreference) ----------

CLUSTER_PROMPT = """Below is a list of work threads. Group together threads that cover the same topic/same project (coreference resolution).

Rules:
- Only group them when they are clearly the same task/project. Don't group just because they're in the same area (both "DB", both "deployment").
- A ★ marks an existing group (canonical). If a thread continues that work, use the ★ slug as the canonical and put the slugs to merge in members (prefer growing the existing group).
- Two ★ groups that are clearly the same work may also be grouped (one as canonical, the other in members).
- The canonical for each group is the single slug that best represents the work (favor the more inclusive name / most recent activity).
- Don't put the canonical itself in members. members are the other slugs that will be merged into the canonical.
- If there's nothing to group, return {{"groups": []}}.
- Output a single JSON object only. No explanation, no code fences.

[Threads]
{threads}

Output format:
{{"groups": [{{"canonical": "slug-a", "members": ["slug-b", "slug-c"]}}]}}
"""


def is_free(e):
    """A thread not yet in a cluster (neither canonical nor member)."""
    return not e.get("alias_of") and not e.get("aliases")


def is_canonical(e):
    """A canonical that already owns aliases. Becomes an absorption target for new free threads."""
    return not e.get("alias_of") and bool(e.get("aliases"))


def _elect(slugs, registry):
    """Pick the canonical deterministically: most sessions → earliest started → slug order."""
    return sorted(slugs, key=lambda s: (
        -registry[s].get("count", 0), registry[s].get("first_active", ""), s))[0]


def cluster(ctx: TenantContext, registry, dry_run):
    # Candidates = free (unclustered) + canonical (existing groups). Exposing canonicals lets a
    # new near-dup free thread get absorbed into the existing group instead of spawning a parallel one.
    cand = {slug: e for slug, e in registry.items()
            if is_free(e) or is_canonical(e)}
    if len(cand) < 2:
        return 0
    lines = []
    for slug, e in sorted(cand.items(), key=lambda kv: kv[1].get("last_active", ""), reverse=True):
        recent = "; ".join(e.get("recent_titles", [])[-3:])
        mark = " ★" if is_canonical(e) else ""
        lines.append("- slug=%s%s | name=%s | project=%s | recent work: %s"
                     % (slug, mark, e["name"], ",".join(e.get("projects", [])) or "-",
                        th.flatten(recent, 160)))
    prompt = CLUSTER_PROMPT.format(threads="\n".join(lines))
    try:
        out = llm.complete(prompt, model=ctx.model, timeout=ctx.llm_timeout)
    except llm.LLMError as ex:
        print("  ! consolidation LLM call failed: %s" % ex)
        return 0  # nothing linked; next run retries with the same candidates
    parsed = th.parse_llm_json(out)
    groups = parsed.get("groups", []) if isinstance(parsed, dict) else []

    linked = 0
    for g in groups:
        if not isinstance(g, dict):
            continue
        group_slugs = [g.get("canonical")] + (g.get("members") or [])
        # Re-check against live state so changes made earlier in this run are reflected.
        live = [s for s in dict.fromkeys(group_slugs)
                if s in registry and (is_free(registry[s]) or is_canonical(registry[s]))]
        canons = [s for s in live if is_canonical(registry[s])]
        frees = [s for s in live if is_free(registry[s])]
        if canons:
            winner = _elect(canons, registry)   # grow the (largest) existing group
            to_absorb = frees + [s for s in canons if s != winner]
        else:
            winner = _elect(frees, registry)
            to_absorb = [s for s in frees if s != winner]
        if not to_absorb:
            continue
        existing = {a.get("slug") for a in registry[winner].get("aliases", [])}
        for m in to_absorb:
            if dry_run:
                print("  [dry] %s ← %s (%s)" % (winner, m, registry[m]["name"]))
                linked += 1
                continue
            # Merging a losing canonical: keep the alias graph flat —
            # cluster_members() only follows direct alias_of pointers.
            for a in registry[m].pop("aliases", []):
                a_slug = a.get("slug")
                if a_slug in registry:
                    registry[a_slug]["alias_of"] = winner
                if a_slug not in existing:
                    registry[winner].setdefault("aliases", []).append(a)
                    existing.add(a_slug)
            registry[m]["alias_of"] = winner
            if m not in existing:
                registry[winner].setdefault("aliases", []).append(
                    {"slug": m, "name": registry[m]["name"]})
                existing.add(m)
            linked += 1
    return linked


# ---------- Stage 2: canonical synthesis ----------

SYNTH_PROMPT = """Below is the chronological record of one work thread. Summarize the "current state (canonical)" of this work in {lang}.

Rules:
- Focus on what this work is now about and what the current decision/progress state is.
- If a past fact was changed by a later record, state it explicitly as "was ~ but is now ~" (don't write old facts as if they were current).
- If records contradict each other, make clear which one is the most recent.
- Lines marked "(daily-log)" are excerpts from the same day's work journal for the same project and may include unrelated work. Only use what is relevant to this thread.
- Within 5-8 lines, body only, no heading/title. Don't make up anything not in the log.

[Work: {name}]
[Record (chronological)]
{entries}
"""


def cluster_members(registry, canonical):
    return [canonical] + [s for s, e in registry.items() if e.get("alias_of") == canonical]


def synthesize(ctx: TenantContext, registry, dry_run):
    synth_count = 0
    failed = 0
    digest_cache = {}
    for slug, e in registry.items():
        if e.get("alias_of"):
            continue  # members are synthesized from the canonical
        members = cluster_members(registry, slug)
        total = sum(registry[m].get("count", 0) for m in members)
        if total < SYNTH_MIN_SESSIONS:
            continue
        cluster_last = max((registry[m].get("last_active", "") for m in members), default="")
        members_key = sorted(members)
        if e.get("state_synth_at") == cluster_last and e.get("state_members") == members_key:
            continue  # no change

        entries = []
        date_projects = {}   # date -> {project}, for matching daily-log excerpts
        for m in members:
            for date, hm, text in read_entries(ctx, m):
                entries.append("%s %s | %s" % (date, hm, text))
                proj = entry_project(text)
                if proj:
                    date_projects.setdefault(date, set()).add(proj)
        if not entries:
            continue
        # Thread entries alone (one first-prompt line per session) lose everything a
        # long-running session did later in the day; add the matching daily-log digests.
        for date, projs in date_projects.items():
            for hm, proj, dig in day_digests(ctx, date, digest_cache):
                if proj in projs:
                    entries.append("%s %s | (daily-log/%s) %s"
                                   % (date, hm, proj, th.flatten(dig, DIGEST_TRUNC)))
        entries = sorted(set(entries))[-SYNTH_MAX_LINES:]
        prompt = SYNTH_PROMPT.format(lang=ctx.summary_lang, name=e["name"], entries="\n".join(entries))
        if dry_run:
            print("  [dry] synth %s (%d members, %d records)" % (slug, len(members), len(entries)))
            synth_count += 1
            continue
        try:
            state = llm.complete(prompt, model=ctx.model, timeout=ctx.llm_timeout).strip()
        except llm.LLMError as ex:
            # state_synth_at stays stale → this slug is re-picked next run.
            print("  ! synthesis failed %s: %s" % (slug, ex))
            failed += 1
            continue
        if state:
            e["current_state"] = state
            e["state_synth_at"] = cluster_last
            e["state_members"] = members_key
            synth_count += 1
    return synth_count, failed


def run_consolidate(ctx: TenantContext, no_synth=False, dry_run=False):
    """Consolidate + synthesize. Raises llm.LLMError at the end if any
    synthesis failed, AFTER saving the successful ones — the worker retries and
    only the stale slugs are re-synthesized."""
    stamp = datetime.now(ctx.tz).strftime("%Y-%m-%d %H:%M:%S")
    registry = th.load_json(ctx.registry_file, {})
    if not registry:
        print("[%s] no registry — run threads first" % stamp)
        return

    linked = cluster(ctx, registry, dry_run)
    print("consolidation: linked %d threads to a canonical" % linked)

    synthed = failed = 0
    if not no_synth:
        synthed, failed = synthesize(ctx, registry, dry_run)
        print("synthesis: updated 'current state' of %d canonicals (%d failed)"
              % (synthed, failed))

    if not dry_run and (linked or synthed):
        th.save_json(ctx.registry_file, registry)
        print("registry saved")

    if failed:
        raise llm.LLMError("%d canonical syntheses failed (will retry next run)" % failed)
