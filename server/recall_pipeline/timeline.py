# -*- coding: utf-8 -*-
"""Timeline build — port of scripts/work-timeline.py.

Scans a tenant's uploaded transcripts ({data_root}/projects/**/*.jsonl) and
accumulates "what work was done" into {data_root}/work-timeline/YYYY-MM-DD.md,
grouped into completed N-minute buckets.

Differences from the local script:
  - All paths/settings come from TenantContext (no module globals).
  - Summaries call the Anthropic API (llm.complete).
  - Failure semantics fixed: if a bucket's summary fails, the cursor stops at
    the last successful bucket and the error propagates so the worker retries.
    (The local script fail-forwards: summarize_hour returns {} and the section
    is written with the low-quality ai-title fallback, never retried.)
  - No hook/debounce/flock plumbing (the server queue serializes per tenant;
    Phase 2 adds a tenant-scope lock).
"""
import os
import re
import json
import glob
from datetime import datetime, timedelta

from .context import TenantContext
from . import llm

PROMPT_TRUNC = 140            # max display length for a prompt
MAX_PROMPTS_PER_SESSION = 8   # number of prompts to show per session
MAX_ASSISTANT_SNIPPETS = 8    # number of assistant responses per session to feed into the summary input
ASSISTANT_TRUNC = 500         # max length of a single assistant response (for summary input)
ASSISTANT_TAIL_KEEP = 3       # always keep the last N assistant responses (conclusions land at the end)
DIGEST_PROMPT_TRUNC = 160     # user prompt length for summary input

# Marker identifying the LOCAL pipeline's own headless summary sessions.
# Uploaded transcripts can contain these (the client machine still runs the
# local pipeline), so they are still excluded as self-noise.
INTERNAL_MARKER = "[work-timeline-internal]"

# Meta/system/pasted text that is not an actual prompt
SKIP_PREFIXES = (
    "[work-timeline-internal]",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<system-reminder>",
    "<untrusted_tool_result",
    "<task-notification",
    "<bash-input",
    "<bash-stdout",
    "<bash-stderr",
    "<persisted-output",
    "[Request interrupted",
    "Caveat:",
    "Human:",
    "Assistant:",
    "User:",
)


def bucket_floor(ctx: TenantContext, dt):
    minute = (dt.minute // ctx.bucket_minutes) * ctx.bucket_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def parse_ts(s):
    if not s:
        return None
    try:
        # "2026-06-23T01:02:09.010Z"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def extract_text(content):
    """Extract only the actual prompt text from a user message content (str or list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_result":
                return ""  # a tool result message is not a prompt
            if item.get("type") == "text":
                t = item.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts).strip()
    return ""


def extract_assistant_text(content):
    """Extract only text blocks from an assistant message content (excluding tool_use/thinking)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text", "")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
        return "\n".join(parts).strip()
    return ""


def is_real_prompt(text):
    if not text:
        return False
    for p in SKIP_PREFIXES:
        if text.startswith(p):
            return False
    return True


def project_label(cwd, fallback_dirname):
    if cwd:
        base = os.path.basename(cwd.rstrip("/"))
        if base:
            return base
    # dir name format: -Users-user-Projects-acme-acme-api
    seg = fallback_dirname.split("-")
    return seg[-1] if seg and seg[-1] else fallback_dirname


def subject_label(tool_paths, project):
    """Infer which repo the session actually worked on from the file paths its
    tools touched — sessions often run from ~ (or /tmp) while reading/editing
    ~/Projects/<repo>. Only <X> right after a "Projects" path segment counts, so
    unrelated absolute paths don't vote. Returns None when nothing was inferred
    or it adds nothing over the cwd-derived project label."""
    counts = {}
    for p in tool_paths:
        if "/.claude/" in p:      # transcript/config reads say nothing about the subject
            continue
        parts = p.split("/")
        for i, seg in enumerate(parts[:-1]):
            if seg.lower() == "projects" and parts[i + 1]:
                counts[parts[i + 1]] = counts.get(parts[i + 1], 0) + 1
                break
    if not counts:
        return None
    best = max(counts, key=lambda k: counts[k])
    return None if best == project else best


def collect_sessions(ctx: TenantContext, window_start, window_end):
    """Collect prompts where window_start <= ts < window_end, grouped by session."""
    # File mtime filter (if a prompt is in the window, the file was modified at or after that point). With a small margin.
    mtime_floor = (window_start - timedelta(minutes=10)).timestamp()
    sessions = {}  # sid -> dict

    for path in glob.glob(os.path.join(ctx.projects_dir, "*", "*.jsonl")):
        try:
            if os.path.getmtime(path) < mtime_floor:
                continue
        except OSError:
            continue
        dirname = os.path.basename(os.path.dirname(path))
        ai_title = None
        cwd = None
        git_branch = None
        prompts = []        # (local_dt, text) — actual prompts within the window
        assistant_texts = []  # (local_dt, text) — assistant responses within the window (for summary input)
        tool_paths = []      # file paths touched by tool calls (whole session — subject inference)
        active = False       # was there any activity (user/assistant) within the window
        first_active = None  # time of the first activity within the window
        internal = False     # exclude entirely if this is the local pipeline's own summary session
        saw_synthetic = False   # saw a synthetic message (session limit / interrupt)
        real_assistant = False  # had a real (non-synthetic) assistant reply in the window

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
                    t = rec.get("type")
                    if t == "ai-title":
                        at = rec.get("aiTitle")
                        if at:
                            ai_title = at
                        continue
                    if t == "user" and not rec.get("isMeta"):
                        if rec.get("cwd"):
                            cwd = rec.get("cwd")
                        if rec.get("gitBranch"):
                            git_branch = rec.get("gitBranch")
                        # Detect the marker regardless of window: identify the local pipeline's own summary session
                        if isinstance(rec.get("message"), dict):
                            if extract_text(rec["message"].get("content")).startswith(INTERNAL_MARKER):
                                internal = True
                    # Subject inference reads tool_use paths regardless of window:
                    # which repo a session is about doesn't depend on the time slice.
                    if t == "assistant" and isinstance(rec.get("message"), dict):
                        content = rec["message"].get("content")
                        if isinstance(content, list):
                            for blk in content:
                                if isinstance(blk, dict) and blk.get("type") == "tool_use" \
                                        and isinstance(blk.get("input"), dict):
                                    for key in ("file_path", "path", "notebook_path"):
                                        v = blk["input"].get(key)
                                        if isinstance(v, str) and v.startswith("/"):
                                            tool_paths.append(v)

                    # Determine window membership by activity time (user/assistant messages)
                    if t in ("user", "assistant"):
                        ts = parse_ts(rec.get("timestamp"))
                        if ts is None:
                            continue
                        lt = ts.astimezone(ctx.tz)
                        if not (window_start <= lt < window_end):
                            continue
                        msg = rec.get("message")
                        # Synthetic messages (model=<synthetic>, e.g. "session limit
                        # reached" / interrupts) carry no real work — don't count them
                        # as activity, so dead 0-token sessions don't pollute the timeline.
                        if t == "assistant" and isinstance(msg, dict) and msg.get("model") == "<synthetic>":
                            saw_synthetic = True
                            continue
                        active = True
                        if first_active is None or lt < first_active:
                            first_active = lt
                        if t == "user" and not rec.get("isMeta"):
                            if isinstance(msg, dict):
                                text = extract_text(msg.get("content"))
                                if is_real_prompt(text):
                                    prompts.append((lt, text))
                        elif t == "assistant":
                            real_assistant = True
                            if isinstance(msg, dict):
                                atext = extract_assistant_text(msg.get("content"))
                                if atext:
                                    assistant_texts.append((lt, atext))
        except OSError:
            continue

        if not active or internal:
            continue
        # Drop sessions that only got a synthetic message (limit/interrupt) with no real reply
        if saw_synthetic and not real_assistant:
            continue
        sid = os.path.splitext(os.path.basename(path))[0]
        proj = project_label(cwd, dirname)
        sessions[sid] = {
            "title": ai_title,
            "project": proj,
            "subject": subject_label(tool_paths, proj),
            "branch": git_branch,
            "first_active": first_active,
            "prompts": sorted(prompts, key=lambda x: x[0]),
            "assistant_texts": sorted(assistant_texts, key=lambda x: x[0]),
        }
    return sessions


HOURLY_SUMMARY_PROMPT = """Below are the work sessions that took place in Claude Code during the {window} time window.
For each session, write a one-line summary (at most 2 sentences) in {summary_lang}.

- Focus concretely on "what was done, and what the result/progress status is".
- Do not make up anything that is not in the logs. Keep simple lookups/checks brief.
- Output only one line per session number in the format "N. <summary>". No preamble, explanation, or blank lines.

{digest}
"""


def _flatten(text, limit):
    one = " ".join(text.split())
    return one[:limit] + "…" if len(one) > limit else one


# Assistant snippets containing these signals tend to carry the conclusion/decision
# rather than exploratory chatter, so they are prioritized for the summary input.
DECISION_SIGNALS = (
    "확정", "결론", "기각", "결정", "원인", "배포", "완료", "해결", "실패",
    "정리하면", "요약하면", "따라서",
    "decided", "confirmed", "ruled out", "root cause", "deployed",
    "conclusion", "in summary", "therefore", "fixed",
)


def select_assistant_snippets(assistant_texts, limit, tail_keep):
    """Pick the assistant responses most likely to hold conclusions.
    Always keep the last `tail_keep` (conclusions land at the end), then fill the
    remaining slots with earlier responses carrying a decision signal (most recent
    first). Chronological order is preserved in the returned list."""
    n = len(assistant_texts)
    if n <= limit:
        return list(assistant_texts)
    keep = set(range(max(0, n - tail_keep), n))
    for i in range(n - tail_keep - 1, -1, -1):
        if len(keep) >= limit:
            break
        if any(sig in assistant_texts[i][1].lower() for sig in DECISION_SIGNALS):
            keep.add(i)
    return [assistant_texts[i] for i in sorted(keep)]


def build_hour_digest(ordered):
    """ordered: [(sid, session)] (sorted by first_active). Builds the summary input text."""
    lines = []
    for i, (_sid, s) in enumerate(ordered, 1):
        branch = (" (branch: %s)" % s["branch"]) if s["branch"] and s["branch"] != "HEAD" else ""
        lines.append("[%d] project: %s%s" % (i, s["project"], branch))
        if s["title"]:
            lines.append("  title (auto): %s" % s["title"])
        ups = [_flatten(t, DIGEST_PROMPT_TRUNC) for _, t in s["prompts"][:MAX_PROMPTS_PER_SESSION]]
        if ups:
            lines.append("  user input: " + " / ".join(ups))
        ats = [_flatten(t, ASSISTANT_TRUNC)
               for _, t in select_assistant_snippets(
                   s["assistant_texts"], MAX_ASSISTANT_SNIPPETS, ASSISTANT_TAIL_KEEP)]
        if ats:
            lines.append("  assistant response gist: " + " / ".join(ats))
        lines.append("")
    return "\n".join(lines).strip()


def parse_numbered(text, n):
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s+(.+)", line)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= n:
                out[idx] = m.group(2).strip()
    return out


def summarize_hour(ctx: TenantContext, window_start, ordered):
    """For the session order (ordered), return a dict of sid -> one-line summary.

    Raises llm.LLMError on failure — the caller must NOT advance the cursor past
    this window (the fail-forward fallback of the local script is the bug this
    port fixes)."""
    digest = build_hour_digest(ordered)
    if not digest:
        return {}
    end = window_start + timedelta(minutes=ctx.bucket_minutes)
    window = "%02d:%02d–%02d:%02d" % (
        window_start.hour, window_start.minute, end.hour, end.minute)
    prompt = HOURLY_SUMMARY_PROMPT.format(
        window=window, digest=digest, summary_lang=ctx.summary_lang)
    out = llm.complete(prompt, model=ctx.model, timeout=ctx.llm_timeout)
    parsed = parse_numbered(out, len(ordered))
    return {ordered[i - 1][0]: summ for i, summ in parsed.items()}


def render_section(ctx: TenantContext, window_start, sessions, summaries=None):
    summaries = summaries or {}
    end = window_start + timedelta(minutes=ctx.bucket_minutes)
    heading = "## %02d:%02d–%02d:%02d" % (
        window_start.hour, window_start.minute, end.hour, end.minute)
    lines = [heading, ""]
    # Sort sessions by time of first activity
    ordered = sorted(sessions.items(), key=lambda kv: kv[1]["first_active"])
    for sid, s in ordered:
        headline = summaries.get(sid) or s["title"] or "(no title)"
        branch = (" · `%s`" % s["branch"]) if s["branch"] and s["branch"] != "HEAD" else ""
        lines.append("- **%s**%s — %s" % (s["project"], branch, headline))
        shown = s["prompts"][:MAX_PROMPTS_PER_SESSION]
        for lt, text in shown:
            one = " ".join(text.split())
            if len(one) > PROMPT_TRUNC:
                one = one[:PROMPT_TRUNC] + "…"
            lines.append("    - `%s` %s" % (lt.strftime("%H:%M"), one))
        extra = len(s["prompts"]) - len(shown)
        if extra > 0:
            lines.append("    - … +%d more" % extra)
    lines.append("")
    return heading, "\n".join(lines)


def date_file(ctx: TenantContext, d):
    return os.path.join(ctx.output_dir, "%s.md" % d.strftime("%Y-%m-%d"))


SECTION_RE = re.compile(r"^## (\d{2}):(\d{2})–\d{2}:\d{2}\s*$")


def parse_blocks(content):
    """Break the file into blocks by '## ' headings. Preserves (heading, body) order. H1 is discarded."""
    blocks = []
    cur = None
    buf = []
    for line in content.splitlines():
        if line.startswith("## "):
            if cur is not None:
                blocks.append((cur, "\n".join(buf).rstrip() + "\n"))
            cur = line.rstrip()
            buf = [line]
        elif line.startswith("# "):
            continue  # the existing H1 is regenerated
        else:
            if cur is not None:
                buf.append(line)
    if cur is not None:
        blocks.append((cur, "\n".join(buf).rstrip() + "\n"))
    return blocks


def write_day_file(path, day, blocks):
    """blocks: list of (heading, body). Saves non-time blocks (summaries, etc.) first, then time blocks sorted chronologically."""
    title_line = "# %s (Claude work log)" % day.strftime("%Y-%m-%d (%a)")

    def sort_key(item):
        idx, (heading, _body) = item
        m = SECTION_RE.match(heading)
        if m:
            return (1, int(m.group(1)) * 60 + int(m.group(2)))
        return (0, idx)  # non-time blocks keep their original order and go first

    ordered = [b for _, b in sorted(enumerate(blocks), key=sort_key)]
    out = [title_line, ""]
    for _heading, body in ordered:
        out.append(body.rstrip())
        out.append("")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip() + "\n")


def upsert_section(path, day, heading, body):
    """Add/update a single '## ' block in the date file (overwrites if the heading matches). Other blocks are preserved."""
    blocks = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            blocks = parse_blocks(f.read())
    heading = heading.rstrip()
    body = body.rstrip() + "\n"
    replaced = False
    for i, (h, _b) in enumerate(blocks):
        if h == heading:
            blocks[i] = (heading, body)
            replaced = True
            break
    if not replaced:
        blocks.append((heading, body))
    write_day_file(path, day, blocks)


def load_state(ctx: TenantContext):
    try:
        with open(ctx.state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(ctx: TenantContext, last_bucket_epoch):
    """Atomic write (tmp + os.replace). Phase 0 assumes one worker per tenant;
    Phase 2 adds a tenant-scope lock around the whole build."""
    os.makedirs(os.path.dirname(ctx.state_file), exist_ok=True)
    st = load_state(ctx)
    st["last_processed_epoch"] = last_bucket_epoch
    tmp = ctx.state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, ctx.state_file)


def compute_default_range(ctx: TenantContext, now):
    """Incremental range from saved state up to (but excluding) the in-progress
    bucket. The start is floored to max_backfill_hours so a stale state can't
    trigger a flood — history backfill goes through the explicit date paths."""
    bucket = timedelta(minutes=ctx.bucket_minutes)
    current_bucket = bucket_floor(ctx, now)
    st = load_state(ctx)
    last_epoch = st.get("last_processed_epoch")
    if last_epoch:
        start_bucket = datetime.fromtimestamp(last_epoch, now.tzinfo) + bucket
    else:
        start_bucket = current_bucket - bucket  # first run: only the previous bucket
    floor = current_bucket - timedelta(hours=ctx.max_backfill_hours)
    if start_bucket < floor:
        start_bucket = floor
    return start_bucket, current_bucket


def process_range(ctx: TenantContext, start_bucket, end_bucket,
                  no_llm=False, dry_run=False, advance_state=False):
    """Process completed buckets in [start_bucket, end_bucket).

    Fixed failure semantics: if the LLM summary for a bucket fails, the cursor
    is saved at the last SUCCESSFUL bucket and the error propagates — the
    worker retries and resumes exactly at the failed window."""
    bucket = timedelta(minutes=ctx.bucket_minutes)
    stamp = datetime.now(ctx.tz).strftime("%Y-%m-%d %H:%M:%S")
    if start_bucket >= end_bucket:
        print("[%s] no completed buckets to process (start=%s, end=%s)"
              % (stamp, start_bucket.strftime("%m-%d %H:%M"), end_bucket.strftime("%m-%d %H:%M")))
        return

    h = start_bucket
    total_sessions = 0
    last_done = None
    try:
        while h < end_bucket:
            sessions = collect_sessions(ctx, h, h + bucket)
            if sessions:
                summaries = {}
                if not no_llm:
                    ordered = sorted(sessions.items(), key=lambda kv: kv[1]["first_active"])
                    summaries = summarize_hour(ctx, h, ordered)
                heading, body = render_section(ctx, h, sessions, summaries)
                total_sessions += len(sessions)
                if dry_run:
                    print("----- %s -----" % date_file(ctx, h))
                    print(body)
                else:
                    upsert_section(date_file(ctx, h), h, heading, body)
            last_done = h
            h += bucket
    finally:
        # Cursor advances only through completed buckets — a failed window is
        # never skipped (contrast: work-timeline.py:572-573 advances regardless).
        if not dry_run and last_done is not None and advance_state:
            save_state(ctx, last_done.timestamp())

    print("[%s] processing complete: %s ~ %s, %d sessions"
          % (stamp,
             start_bucket.strftime("%m-%d %H:%M"),
             (end_bucket - bucket).strftime("%m-%d %H:%M"),
             total_sessions))


def run_incremental(ctx: TenantContext, no_llm=False, dry_run=False):
    """Default incremental build: from the saved cursor to the current bucket."""
    now = datetime.now(ctx.tz)
    start_bucket, end_bucket = compute_default_range(ctx, now)
    process_range(ctx, start_bucket, end_bucket,
                  no_llm=no_llm, dry_run=dry_run, advance_state=True)


def run_date(ctx: TenantContext, date_str, no_llm=False, dry_run=False):
    """Build one specific date (history backfill — state unchanged)."""
    now = datetime.now(ctx.tz)
    day0 = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ctx.tz)
    start_bucket = bucket_floor(ctx, day0)
    end_bucket = min(start_bucket + timedelta(hours=24), bucket_floor(ctx, now))
    process_range(ctx, start_bucket, end_bucket,
                  no_llm=no_llm, dry_run=dry_run, advance_state=False)
