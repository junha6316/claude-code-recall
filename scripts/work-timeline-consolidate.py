#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Canonicalize work threads into a single "consolidated current truth" (run at index time, right after the threads job).

Two stages:
  1. Consolidation (coreference): use an LLM to cluster threads about the same topic and link them to one canonical thread.
     - Non-destructive: doesn't touch files/entries; only records alias_of (member→canonical)/aliases in the registry.
     - Sticky: once an alias_of is established it's never auto-removed (manual corrections go through editing _registry.json).
  2. Canonical synthesis: for each multi-session canonical, read every member's full record in chronological order and synthesize the "current state (canonical)".
     Make temporal supersession/contradictions explicit. Only re-synthesize clusters that changed.

recall reads this alias_of/current_state so that no matter which name you ask by, it resolves to the canonical's current truth.

Usage:
  work-timeline-consolidate.py            # consolidate + synthesize the changed clusters
  work-timeline-consolidate.py --no-synth # consolidate only (for testing)
  work-timeline-consolidate.py --dry-run  # don't modify the registry, just print proposals
"""
import os
import re
import argparse
import importlib.util

HOME = os.path.expanduser("~")
SCRIPTS_DIR = os.path.join(HOME, ".claude", "scripts")

SUMMARY_LANG = os.environ.get("CCRECALL_SUMMARY_LANG", "English")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS_DIR, filename))
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load %s" % filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wt = _load("work_timeline", "work-timeline.py")
th = _load("work_timeline_threads", "work-timeline-threads.py")

SYNTH_MIN_SESSIONS = 2     # for a single-session thread the entry itself is the state → skip synthesis
ENTRY_RE = re.compile(r"^- `(\d{2}:\d{2})` (.+?)\s*·([0-9a-f]{8})\s*$")
DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$")


def thread_file(slug):
    return os.path.join(th.THREADS_DIR, "%s.md" % slug)


def read_entries(slug):
    """Return a chronological list of (date, hm, text) from the thread's .md."""
    path = thread_file(slug)
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


# ---------- Stage 1: consolidation (coreference) ----------

CLUSTER_PROMPT = """[work-timeline-internal]
Below is a list of work threads. Group together threads that cover the same topic/same project (coreference resolution).

Rules:
- Only group them when they are clearly the same task/project. Don't group just because they're in the same area (both "DB", both "deployment").
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
    """Only threads not yet in a cluster (neither canonical nor member) are consolidation candidates."""
    return not e.get("alias_of") and not e.get("aliases")


def cluster(registry, dry_run):
    free = {slug: e for slug, e in registry.items() if is_free(e)}
    if len(free) < 2:
        return 0
    lines = []
    for slug, e in sorted(free.items(), key=lambda kv: kv[1].get("last_active", ""), reverse=True):
        recent = "; ".join(e.get("recent_titles", [])[-3:])
        lines.append("- slug=%s | name=%s | project=%s | recent work: %s"
                     % (slug, e["name"], ",".join(e.get("projects", [])) or "-",
                        th.flatten(recent, 160)))
    prompt = CLUSTER_PROMPT.format(threads="\n".join(lines))
    try:
        out = wt.run_claude(prompt)
    except Exception as ex:
        print("  ! consolidation LLM call failed: %s" % ex)
        return 0
    parsed = th.parse_llm_json(out)
    groups = parsed.get("groups", []) if isinstance(parsed, dict) else []

    linked = 0
    for g in groups:
        if not isinstance(g, dict):
            continue
        # The LLM only identifies "same topic". The canonical is chosen deterministically (predictable, close to user intent):
        # most sessions → earliest started → slug order.
        group_slugs = [g.get("canonical")] + (g.get("members") or [])
        members_all = list(dict.fromkeys(
            s for s in group_slugs if s in free and is_free(registry[s])))
        if len(members_all) < 2:
            continue
        canonical = sorted(members_all, key=lambda s: (
            -registry[s].get("count", 0), registry[s].get("first_active", ""), s))[0]
        for m in members_all:
            if m == canonical:
                continue
            if dry_run:
                print("  [dry] %s ← %s (%s)" % (canonical, m, registry[m]["name"]))
            else:
                registry[m]["alias_of"] = canonical
                registry[canonical].setdefault("aliases", []).append(
                    {"slug": m, "name": registry[m]["name"]})
            linked += 1
    return linked


# ---------- Stage 2: canonical synthesis ----------

SYNTH_PROMPT = """[work-timeline-internal]
Below is the chronological record of one work thread. Summarize the "current state (canonical)" of this work in {lang}.

Rules:
- Focus on what this work is now about and what the current decision/progress state is.
- If a past fact was changed by a later record, state it explicitly as "was ~ but is now ~" (don't write old facts as if they were current).
- If records contradict each other, make clear which one is the most recent.
- Within 5-8 lines, body only, no heading/title. Don't make up anything not in the log.

[Work: {name}]
[Record (chronological)]
{entries}
"""


def cluster_members(registry, canonical):
    return [canonical] + [s for s, e in registry.items() if e.get("alias_of") == canonical]


def synthesize(registry, dry_run):
    synth_count = 0
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
        for m in members:
            for date, hm, text in read_entries(m):
                entries.append("%s %s | %s" % (date, hm, text))
        if not entries:
            continue
        entries.sort()
        prompt = SYNTH_PROMPT.format(lang=SUMMARY_LANG, name=e["name"], entries="\n".join(entries))
        if dry_run:
            print("  [dry] synth %s (%d members, %d records)" % (slug, len(members), len(entries)))
            synth_count += 1
            continue
        try:
            state = wt.run_claude(prompt).strip()
        except Exception as ex:
            print("  ! synthesis failed %s: %s" % (slug, ex))
            continue
        if state:
            e["current_state"] = state
            e["state_synth_at"] = cluster_last
            e["state_members"] = members_key
            synth_count += 1
    return synth_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-synth", action="store_true", help="consolidate only, skip canonical synthesis")
    ap.add_argument("--dry-run", action="store_true", help="don't modify the registry, just print proposals")
    args = ap.parse_args()

    registry = th.load_json(th.REGISTRY_FILE, {})
    if not registry:
        print("no registry — run work-timeline-threads.py first")
        return

    linked = cluster(registry, args.dry_run)
    print("consolidation: linked %d threads to a canonical" % linked)

    synthed = 0
    if not args.no_synth:
        synthed = synthesize(registry, args.dry_run)
        print("synthesis: updated 'current state' of %d canonicals" % synthed)

    if not args.dry_run and (linked or synthed):
        th.save_json(th.REGISTRY_FILE, registry)
        print("registry saved")


if __name__ == "__main__":
    main()
