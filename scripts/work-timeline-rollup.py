#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Read a day's worth of work timeline (.md), generate a "daily summary" via an
LLM (claude -p), and prepend it to the same file as a `## 🧠 Daily Summary`
section.

- Default target: the most recent dated file that does not yet have a summary
  section (the previous day is processed at 00:30 the next day)
- Idempotent: skips if a summary already exists (use --force to regenerate)
- The hourly sections are preserved by work-timeline.py, so this does not
  conflict with later hourly runs
"""
import os
import glob
import argparse
import subprocess
import shutil
import importlib.util
from datetime import datetime

HOME = os.path.expanduser("~")
SCRIPTS_DIR = os.path.join(HOME, ".claude", "scripts")
CLAUDE_BIN = (os.environ.get("CCRECALL_CLAUDE_BIN")
              or shutil.which("claude")
              or os.path.join(HOME, ".local", "bin", "claude"))
SUMMARY_HEADING = "## 🧠 Daily Summary"
CLAUDE_TIMEOUT = 240  # seconds
SUMMARY_LANG = os.environ.get("CCRECALL_SUMMARY_LANG", "English")

# Reuse the helpers from work-timeline.py (filename has a hyphen, so use importlib)
_spec = importlib.util.spec_from_file_location(
    "work_timeline", os.path.join(SCRIPTS_DIR, "work-timeline.py"))
if _spec is None or _spec.loader is None:
    raise RuntimeError("Failed to load work-timeline.py")
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

PROMPT_TEMPLATE = """[work-timeline-internal]
Below is the hour-by-hour log of work done with Claude Code on {date}.
Read it and write a "daily summary" in {lang}. Rules:

- Body only, no title or preamble. Use markdown.
- Start with a one-line overall takeaway (one sentence capturing today's core).
- Then "**Key work by project**", grouping by project (or area) into 3-6 bullets. Each bullet should be concrete, focused on "what was done and why, and what the result was".
- At the end, "**Remaining/follow-up work**" with 1-3 bullets if it can be inferred (omit if none).
- Then a final "**Recall tags**" section (heading verbatim in English, even when the summary language is not English): one bullet per project/event above, each a comma-separated list of search aliases so this day can be found later by lexical search regardless of how the question is phrased. For every distinctive term ALWAYS include both languages: English service/tool/error names get Korean paraphrases, Korean terms get English equivalents. Expand abbreviations and add likely synonyms (e.g. `GSC, 서치콘솔, 검색 노출 급감, search impressions drop` / `OpenNext 빌드, SSR 번들, 프로덕션 배포, production deploy, 릴리스`). Tags may paraphrase, but only for events that actually appear in the log — never invent events.
- Do not make up anything that is not in the log. Group simple lookups/checks together concisely.

--- Hour-by-hour log ---
{body}
"""


def find_target_file(date_str):
    files = sorted(glob.glob(os.path.join(wt.OUTPUT_DIR, "[0-9]" * 4 + "-*.md")))
    if date_str:
        p = os.path.join(wt.OUTPUT_DIR, "%s.md" % date_str)
        return p if os.path.exists(p) else None
    # Exclude today (still in progress); only summarize completed days
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    # The most recent (completed) file that has hour blocks but no summary
    for p in reversed(files):
        if os.path.splitext(os.path.basename(p))[0] == today:
            continue
        with open(p, "r", encoding="utf-8") as f:
            blocks = wt.parse_blocks(f.read())
        has_hours = any(wt.SECTION_RE.match(h) for h, _ in blocks)
        has_summary = any(h.strip() == SUMMARY_HEADING for h, _ in blocks)
        if has_hours and not has_summary:
            return p
    return None


def date_from_path(p):
    base = os.path.splitext(os.path.basename(p))[0]
    return datetime.strptime(base, "%Y-%m-%d")


def hour_body(blocks):
    parts = [b for h, b in blocks if wt.SECTION_RE.match(h)]
    return "\n".join(parts).strip()


def run_claude(prompt):
    env = dict(os.environ)
    env.setdefault("HOME", HOME)
    # claude needs USER/LOGNAME for keychain access (in case of launchd's minimal env)
    user = os.path.basename(HOME)
    env.setdefault("USER", user)
    env.setdefault("LOGNAME", user)
    # Add the mise node bin to PATH so the SessionEnd hook can find node (if present)
    node_bins = sorted(glob.glob(os.path.join(
        HOME, ".local", "share", "mise", "installs", "node", "*", "bin")))
    if node_bins:
        env["PATH"] = node_bins[-1] + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    proc = subprocess.run(
        [CLAUDE_BIN, "-p"],
        input=prompt, capture_output=True, text=True,
        timeout=CLAUDE_TIMEOUT, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError("claude -p failed (rc=%d): %s"
                           % (proc.returncode, (proc.stderr or "").strip()[:500]))
    return proc.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (if omitted, auto-selects the most recent file without a summary)")
    ap.add_argument("--force", action="store_true", help="regenerate even if a summary already exists")
    args = ap.parse_args()
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    path = find_target_file(args.date)
    if not path:
        print("[%s] No target file to summarize" % stamp)
        return

    with open(path, "r", encoding="utf-8") as f:
        blocks = wt.parse_blocks(f.read())
    if any(h.strip() == SUMMARY_HEADING for h, _ in blocks) and not args.force:
        print("[%s] Summary already exists, skipping: %s" % (stamp, os.path.basename(path)))
        return

    body = hour_body(blocks)
    if not body:
        print("[%s] Hour log is empty, skipping: %s" % (stamp, os.path.basename(path)))
        return

    day = date_from_path(path)
    prompt = PROMPT_TEMPLATE.format(date=day.strftime("%Y-%m-%d"), body=body, lang=SUMMARY_LANG)
    summary = run_claude(prompt)
    if not summary:
        print("[%s] LLM response is empty, aborting: %s" % (stamp, os.path.basename(path)))
        return

    block_body = SUMMARY_HEADING + "\n\n" + summary + "\n"
    wt.upsert_section(path, day, SUMMARY_HEADING, block_body)
    print("[%s] Summary written: %s (%d chars)"
          % (stamp, os.path.basename(path), len(summary)))


if __name__ == "__main__":
    main()
