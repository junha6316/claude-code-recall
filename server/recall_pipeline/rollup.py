# -*- coding: utf-8 -*-
"""Daily summary rollup — port of scripts/work-timeline-rollup.py.

Reads a day's timeline (.md), generates a "daily summary" via the Anthropic
API, and prepends it as a `## 🧠 Daily Summary` section.

- Default target: the most recent dated file that does not yet have a summary
  section (excluding today, which is still in progress)
- Idempotent: skips if a summary already exists (use force to regenerate)

The local script defines its OWN run_claude copy (rollup.py:85-105, using the
CLI default model) — this port routes through llm.complete like every other
stage, on ctx.model.
"""
import os
import glob
from datetime import datetime

from .context import TenantContext
from . import llm, timeline

SUMMARY_HEADING = "## 🧠 Daily Summary"
ROLLUP_TIMEOUT = 240  # seconds — daily summaries are longer than bucket summaries

PROMPT_TEMPLATE = """Below is the hour-by-hour log of work done with Claude Code on {date}.
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


def find_target_file(ctx: TenantContext, date_str):
    files = sorted(glob.glob(os.path.join(ctx.output_dir, "[0-9]" * 4 + "-*.md")))
    if date_str:
        p = os.path.join(ctx.output_dir, "%s.md" % date_str)
        return p if os.path.exists(p) else None
    # Exclude today (still in progress); only summarize completed days
    today = datetime.now(ctx.tz).strftime("%Y-%m-%d")
    # The most recent (completed) file that has hour blocks but no summary
    for p in reversed(files):
        if os.path.splitext(os.path.basename(p))[0] == today:
            continue
        with open(p, "r", encoding="utf-8") as f:
            blocks = timeline.parse_blocks(f.read())
        has_hours = any(timeline.SECTION_RE.match(h) for h, _ in blocks)
        has_summary = any(h.strip() == SUMMARY_HEADING for h, _ in blocks)
        if has_hours and not has_summary:
            return p
    return None


def date_from_path(p):
    base = os.path.splitext(os.path.basename(p))[0]
    return datetime.strptime(base, "%Y-%m-%d")


def hour_body(blocks):
    parts = [b for h, b in blocks if timeline.SECTION_RE.match(h)]
    return "\n".join(parts).strip()


def rollup_day(ctx: TenantContext, date_str=None, force=False):
    """Summarize one day file. Raises llm.LLMError on API failure so the worker
    retries (the day stays summary-less and is re-picked by find_target_file)."""
    stamp = datetime.now(ctx.tz).strftime("%Y-%m-%d %H:%M:%S")

    path = find_target_file(ctx, date_str)
    if not path:
        print("[%s] No target file to summarize" % stamp)
        return

    with open(path, "r", encoding="utf-8") as f:
        blocks = timeline.parse_blocks(f.read())
    if any(h.strip() == SUMMARY_HEADING for h, _ in blocks) and not force:
        print("[%s] Summary already exists, skipping: %s" % (stamp, os.path.basename(path)))
        return

    body = hour_body(blocks)
    if not body:
        print("[%s] Hour log is empty, skipping: %s" % (stamp, os.path.basename(path)))
        return

    day = date_from_path(path)
    prompt = PROMPT_TEMPLATE.format(
        date=day.strftime("%Y-%m-%d"), body=body, lang=ctx.summary_lang)
    # Daily summaries are the longest output in the pipeline (key-work bullets
    # + bilingual recall tags on a busy day). Raise the cap so a long day isn't
    # silently truncated at stop_reason=max_tokens (no error → no retry).
    summary = llm.complete(prompt, model=ctx.model, timeout=ROLLUP_TIMEOUT,
                           max_tokens=16384)

    block_body = SUMMARY_HEADING + "\n\n" + summary + "\n"
    timeline.upsert_section(path, day, SUMMARY_HEADING, block_body)
    print("[%s] Summary written: %s (%d chars)"
          % (stamp, os.path.basename(path), len(summary)))
