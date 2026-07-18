#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scans Claude Code session transcripts (~/.claude/projects/**/*.jsonl) and
accumulates "what work was done" into an Obsidian vault, grouped into completed
N-minute buckets (BUCKET_MINUTES, default 10 minutes).

- No LLM: deterministically extracts only the ai-title (auto session title) and the actual user prompts
- Buckets: completed N-minute intervals (the in-progress bucket is excluded). If cron falls behind due to laptop sleep, it auto-catches-up (default max 24h)
- Idempotent: re-running the same time section overwrites it
- Timezone: the transcript's UTC (Z) timestamps are converted to local time for bucketing
"""
import os
import re
import sys
import json
import glob
import argparse
import subprocess
import shutil
from datetime import datetime, timedelta

try:
    import fcntl  # POSIX (macOS/Linux); absent on native Windows
except ImportError:
    fcntl = None

HOME = os.path.expanduser("~")
# Honor CLAUDE_CONFIG_DIR (custom Claude Code config dirs); default ~/.claude.
CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
PROJECTS_DIR = os.path.join(CONFIG_DIR, "projects")
# Actual storage lives under a non-TCC-protected path (the config dir). The Obsidian vault holds a symlink to this folder.
OUTPUT_DIR = os.path.join(CONFIG_DIR, "work-timeline")
STATE_FILE = os.path.join(CONFIG_DIR, "scripts", ".work-timeline-state.json")
LOCK_FILE = os.path.join(CONFIG_DIR, "scripts", ".work-timeline.lock")

MAX_BACKFILL_HOURS = 24       # prevent a flood on the first run when state is too stale
PROMPT_TRUNC = 140            # max display length for a prompt
MAX_PROMPTS_PER_SESSION = 8   # number of prompts to show per session
BUCKET_MINUTES = int(os.environ.get("CCRECALL_BUCKET_MINUTES", "15"))  # timeline bucket size (minutes); 60 = on-the-hour. A divisor of 60 is recommended.
# How long a hook-triggered scan suppresses the next one. The Stop hook fires every
# turn; only completed buckets produce new output, so debouncing avoids spawning a
# worker on every turn without losing data.
DEBOUNCE_MINUTES = int(os.environ.get("CCRECALL_DEBOUNCE_MINUTES", str(max(2, BUCKET_MINUTES // 3))))

SUMMARY_LANG = os.environ.get("CCRECALL_SUMMARY_LANG", "English")

# LLM per-time-window summary (claude -p). Resolve the claude binary: env override,
# then PATH lookup, then the default install location.
CLAUDE_BIN = (os.environ.get("CCRECALL_CLAUDE_BIN")
              or shutil.which("claude")
              or os.path.join(HOME, ".local", "bin", "claude"))
CLAUDE_TIMEOUT = 180          # seconds
# Model for the headless summary/classification calls. Sonnet-5 by default for
# better conclusion extraction; override with CCRECALL_SUMMARY_MODEL (empty = CLI default).
SUMMARY_MODEL = os.environ.get("CCRECALL_SUMMARY_MODEL", "claude-sonnet-5")
MAX_ASSISTANT_SNIPPETS = 8    # number of assistant responses per session to feed into the summary input
ASSISTANT_TRUNC = 500         # max length of a single assistant response (for summary input)
ASSISTANT_TAIL_KEEP = 3       # always keep the last N assistant responses (conclusions land at the end)
DIGEST_PROMPT_TRUNC = 160     # user prompt length for summary input

# Marker identifying cron's own headless claude -p summary sessions. Sessions containing this marker are excluded entirely (self-noise).
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


def local_tz():
    # Local timezone (KST, etc.) — astimezone() uses the system tz
    return datetime.now().astimezone().tzinfo


def bucket_floor(dt):
    minute = (dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES
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
        if os.path.normpath(cwd) == os.path.normpath(HOME):
            return "~ (home)"
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


def collect_sessions(window_start, window_end):
    """Collect prompts where window_start <= ts < window_end, grouped by session."""
    tz = local_tz()
    # File mtime filter (if a prompt is in the window, the file was modified at or after that point). With a small margin.
    mtime_floor = (window_start - timedelta(minutes=10)).timestamp()
    sessions = {}  # sid -> dict

    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
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
        internal = False     # exclude entirely if this is cron's own summary session
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
                        # Detect the marker regardless of window: identify cron's own summary session
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
                        lt = ts.astimezone(tz)
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


def run_claude(prompt, timeout=CLAUDE_TIMEOUT):
    """Headless claude -p invocation. Supplements USER/LOGNAME/PATH for launchd's minimal env."""
    env = dict(os.environ)
    env.setdefault("HOME", HOME)
    # Mark this headless session so its own Stop hook is a no-op (prevents the
    # summary call from re-triggering the ingestion worker — see cmd_hook).
    env["CCRECALL_INTERNAL"] = "1"
    user = os.path.basename(HOME)
    env.setdefault("USER", user)
    env.setdefault("LOGNAME", user)
    node_bins = sorted(glob.glob(os.path.join(
        HOME, ".local", "share", "mise", "installs", "node", "*", "bin")))
    if node_bins:
        env["PATH"] = node_bins[-1] + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    cmd = [CLAUDE_BIN, "-p"]
    if SUMMARY_MODEL:
        cmd += ["--model", SUMMARY_MODEL]
    proc = subprocess.run(
        cmd,
        input=prompt, capture_output=True, text=True,
        timeout=timeout, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError("claude -p failed (rc=%d): %s"
                           % (proc.returncode, (proc.stderr or "").strip()[:500]))
    return proc.stdout.strip()


HOURLY_SUMMARY_PROMPT = """[work-timeline-internal]
Below are the work sessions that took place in Claude Code during the {window} time window.
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


def summarize_hour(window_start, ordered):
    """For the session order (ordered), return a dict of sid -> one-line summary. Returns {} on failure."""
    digest = build_hour_digest(ordered)
    if not digest:
        return {}
    end = window_start + timedelta(minutes=BUCKET_MINUTES)
    window = "%02d:%02d–%02d:%02d" % (
        window_start.hour, window_start.minute, end.hour, end.minute)
    prompt = HOURLY_SUMMARY_PROMPT.format(window=window, digest=digest, summary_lang=SUMMARY_LANG)
    try:
        out = run_claude(prompt)
    except Exception as e:
        print("  [LLM summary failed, falling back to ai-title] %s" % str(e)[:200])
        return {}
    parsed = parse_numbered(out, len(ordered))
    return {ordered[i - 1][0]: summ for i, summ in parsed.items()}


def render_section(window_start, sessions, summaries=None):
    summaries = summaries or {}
    end = window_start + timedelta(minutes=BUCKET_MINUTES)
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


def date_file(d):
    return os.path.join(OUTPUT_DIR, "%s.md" % d.strftime("%Y-%m-%d"))


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


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state_updates(**kv):
    """Merge keys into the state file, preserving the others (e.g. last_tick_epoch
    must survive a last_processed_epoch write and vice versa).

    Several processes write this file concurrently (sibling Stop hooks + the detached
    worker), so the read-modify-write is serialized under a blocking lock and the write
    is atomic (temp + os.replace) — otherwise a truncated mid-write read or a stale
    read-back can silently drop last_processed_epoch and re-trigger a full backfill."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state_lock = None
    if fcntl is not None:
        try:
            state_lock = open(STATE_FILE + ".lock", "w")
            fcntl.flock(state_lock, fcntl.LOCK_EX)  # blocking: serialize writers
        except OSError:
            if state_lock is not None:
                state_lock.close()
                state_lock = None
    try:
        st = load_state()
        st.update(kv)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_FILE)
    finally:
        if state_lock is not None:
            try:
                fcntl.flock(state_lock, fcntl.LOCK_UN)
            except OSError:
                pass
            state_lock.close()


def save_state(last_hour_epoch):
    save_state_updates(last_processed_epoch=last_hour_epoch)


def compute_default_range(now):
    """Incremental range from saved state up to (but excluding) the in-progress
    bucket. After sleep/offline the start is floored to MAX_BACKFILL_HOURS so a
    stale state can't trigger a flood."""
    bucket = timedelta(minutes=BUCKET_MINUTES)
    current_bucket = bucket_floor(now)
    st = load_state()
    last_epoch = st.get("last_processed_epoch")
    if last_epoch:
        start_bucket = datetime.fromtimestamp(last_epoch, now.tzinfo) + bucket
    else:
        start_bucket = current_bucket - bucket  # first run: only the previous bucket
    floor = current_bucket - timedelta(hours=MAX_BACKFILL_HOURS)
    if start_bucket < floor:
        start_bucket = floor
    return start_bucket, current_bucket


def process_range(start_bucket, end_bucket, no_llm=False, dry_run=False, advance_state=False):
    """Process completed buckets in [start_bucket, end_bucket). Optionally advances
    the saved state (only the default incremental run should)."""
    bucket = timedelta(minutes=BUCKET_MINUTES)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if start_bucket >= end_bucket:
        print("[%s] no completed buckets to process (start=%s, end=%s)"
              % (stamp, start_bucket.strftime("%m-%d %H:%M"), end_bucket.strftime("%m-%d %H:%M")))
        return

    h = start_bucket
    total_sessions = 0
    last_done = None
    while h < end_bucket:
        sessions = collect_sessions(h, h + bucket)
        if sessions:
            summaries = {}
            if not no_llm:
                ordered = sorted(sessions.items(), key=lambda kv: kv[1]["first_active"])
                summaries = summarize_hour(h, ordered)
            heading, body = render_section(h, sessions, summaries)
            total_sessions += len(sessions)
            if dry_run:
                print("----- %s -----" % date_file(h))
                print(body)
            else:
                upsert_section(date_file(h), h, heading, body)
        last_done = h
        h += bucket

    if not dry_run and last_done is not None and advance_state:
        save_state(last_done.timestamp())

    print("[%s] processing complete: %s ~ %s, %d sessions"
          % (stamp,
             start_bucket.strftime("%m-%d %H:%M"),
             (end_bucket - bucket).strftime("%m-%d %H:%M"),
             total_sessions))


def _recently_ticked(now):
    t = load_state().get("last_tick_epoch")
    if not t:
        return False
    try:
        return (now.timestamp() - float(t)) < DEBOUNCE_MINUTES * 60
    except (TypeError, ValueError):
        return False


def cmd_hook():
    """Hook entrypoint (Stop / SessionStart). Debounced and non-blocking: it spawns a
    detached worker and returns immediately, so it never adds latency to the session
    even if Claude Code runs the hook synchronously."""
    # The worker's `claude -p` summary spawns a headless session whose own Stop hook
    # would re-enter here. CCRECALL_INTERNAL (set in run_claude) marks that case.
    if os.environ.get("CCRECALL_INTERNAL"):
        return
    now = datetime.now(local_tz())
    if _recently_ticked(now):
        return
    # Claim the debounce window before spawning so sibling hooks (other live sessions
    # firing Stop at the same moment) skip instead of piling up workers.
    save_state_updates(last_tick_epoch=now.timestamp())
    try:
        out = open(os.path.join(os.path.dirname(STATE_FILE), "work-timeline.log"), "a")
    except OSError:
        out = subprocess.DEVNULL
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--hook-run"],
        stdin=subprocess.DEVNULL, stdout=out, stderr=out,
        start_new_session=True, env=dict(os.environ),
    )


def cmd_hook_run():
    """Detached worker spawned by --hook. A non-blocking file lock guards against
    concurrent runs (multiple sessions can fire Stop at once); the loser just exits
    because the winner already covers this window."""
    lock = None
    if fcntl is not None:
        try:
            os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
            lock = open(LOCK_FILE, "w")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if lock is not None:
                lock.close()
            return
    try:
        now = datetime.now(local_tz())
        save_state_updates(last_tick_epoch=now.timestamp())
        start_bucket, end_bucket = compute_default_range(now)
        process_range(start_bucket, end_bucket, no_llm=False, dry_run=False, advance_state=True)
    finally:
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0,
                    help="ignore state and process the previous N completed time windows (manual catch-up/testing)")
    ap.add_argument("--date", help="process only the completed time windows of a specific date (YYYY-MM-DD) (state unchanged, for backfill)")
    ap.add_argument("--dry-run", action="store_true", help="print to stdout without writing files")
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM per-time-window summary (use ai-title)")
    ap.add_argument("--hook", action="store_true",
                    help="hook entrypoint (Stop/SessionStart): debounced, spawns a detached worker, returns immediately")
    ap.add_argument("--hook-run", action="store_true",
                    help="internal: the detached worker spawned by --hook (lock-guarded incremental scan)")
    args = ap.parse_args()

    if args.hook:
        cmd_hook()
        return
    if args.hook_run:
        cmd_hook_run()
        return

    tz = local_tz()
    now = datetime.now(tz)

    if args.date:
        day0 = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=tz)
        start_bucket = bucket_floor(day0)
        end_bucket = min(start_bucket + timedelta(hours=24), bucket_floor(now))
        advance = False
    elif args.backfill > 0:
        start_bucket = bucket_floor(now) - timedelta(hours=args.backfill)
        end_bucket = bucket_floor(now)
        advance = False
    else:
        start_bucket, end_bucket = compute_default_range(now)
        advance = True

    process_range(start_bucket, end_bucket, no_llm=args.no_llm, dry_run=args.dry_run, advance_state=advance)


if __name__ == "__main__":
    main()
