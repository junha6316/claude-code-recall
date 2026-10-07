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
import math
import argparse
from datetime import datetime, timedelta

HOME = os.path.expanduser("~")
# Honor CLAUDE_CONFIG_DIR (custom Claude Code config dirs); default ~/.claude.
CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
TIMELINE_DIR = os.path.join(CONFIG_DIR, "work-timeline")
THREADS_DIR = os.path.join(TIMELINE_DIR, "threads")
PROJECTS_DIR = os.path.join(CONFIG_DIR, "projects")

LINE_TRUNC = 200          # max length when displaying a matched line
MAX_LINES_PER_HIT = 8     # number of matched lines to show per block
DEFAULT_LIMIT = 15


# --- query tokenization -------------------------------------------------
# Full-sentence questions should degrade gracefully into keyword search, so
# terms_of() strips punctuation/markup, drops query-framing stopwords, and
# trims trailing Korean particles (josa). Stripped stems still match via the
# substring scorer (e.g. "게이트웨" ⊂ "게이트웨이").

MAX_TERMS = 6

# Query-framing words that carry no search signal (recall triggers, pronouns,
# common verb stems). Latin tokens are compared lowercase.
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
    "me", "my", "our", "us", "of", "to", "in", "on", "at", "with", "and", "or",
    "but", "so", "if", "can", "could", "would", "should", "will", "have", "has",
    "had", "be", "been", "does", "done", "didn", "don", "any", "some", "where",
    "which", "who", "why", "still", "just", "back", "ever", "again", "used",
    "remember", "before", "earlier", "previously", "ago", "while", "last",
    "time", "yesterday", "night", "day", "days", "week", "weeks", "month",
    "months", "year", "years", "few", "several", "couple", "happened", "from",
    # Particles of "set up", "figure out", "turn off", "look into".
    "up", "out", "off", "into",
    # What is left of "wasn't" or "we've" once the apostrophe splits it.
    "doesn", "isn", "wasn", "aren", "weren", "haven", "hasn", "hadn", "couldn",
    "wouldn", "shouldn", "ve", "ll", "re",
}

# Trailing Korean particles/endings to strip from Korean tokens.
# Longer endings come first so "관련해서" strips to "관련" rather than stalling.
JOSA = re.compile(
    r"(을|를|이|가|은|는|에|의|로|으로|도|만|와|과|랑|이랑|에서|까지|부터"
    r"|해서|해야|하는|한|던거|던|거|게|야|냐|니|네|좀|했|하)+$"
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
        # Latin/alphanumeric tokens (service names, error strings) pass as-is.
        if re.fullmatch(r"[a-z0-9_+#.-]{2,}", low):
            if low not in STOP:
                add(low)
            continue
        # Korean tokens: strip trailing particles, keep 2+ char stems.
        stem = JOSA.sub("", tok)
        if len(stem) < 2 or stem in STOP or tok in STOP:
            continue
        add(stem.lower())

    if not out:  # all tokens were stopwords/junk — fall back to the naive split
        return [t.lower() for t in query.split() if t.strip()][:MAX_TERMS]
    return out[:MAX_TERMS]


# Weight floor, so a query made only of corpus-common words still ranks by
# occurrence count instead of collapsing to an all-zero tie.
WEIGHT_FLOOR = 0.01

# A short Latin term ("set", "up", "ai", "rds") as a plain substring hits
# unrelated words (setup, update, detail, records), and in a mostly non-English
# corpus those hits even get a high weight. Such terms match whole words only,
# with an optional plural "s"; Korean stems and longer terms stay substrings.
SHORT_LATIN = re.compile(r"[a-z0-9]{1,3}")


def count_term(text_lower, t):
    """Occurrences of term t in text_lower, by the matching rule above."""
    if t not in text_lower:  # most documents lack the term; skip the slower regex scan
        return 0
    if SHORT_LATIN.fullmatch(t):
        return len(re.findall(r"(?<![a-z0-9])%ss?(?![a-z0-9])" % re.escape(t), text_lower))
    return text_lower.count(t)


def term_weights(terms, docs_lower):
    """Inverse-document-frequency weight per term over the corpus being searched.

    Without this every term counts the same, so a question like
    "memory-invalidation 관련해서 작업하던거" ranks threads matching the generic
    "작업" above the one thread that actually contains "memory-invalidation".
    Words present in most documents collapse toward WEIGHT_FLOOR; rare ones
    dominate.
    """
    n = len(docs_lower)
    weights = {}
    for t in terms:
        df = sum(1 for d in docs_lower if count_term(d, t))
        weights[t] = math.log((n + 1.0) / (df + 1.0)) + WEIGHT_FLOOR
    return weights


def score_weighted(text_lower, terms, weights):
    """Return (weighted score, distinct matches, total occurrences)."""
    weighted = 0.0
    distinct = 0
    total = 0
    for t in terms:
        c = count_term(text_lower, t)
        if c:
            distinct += 1
            total += c
            # log1p damps repetition so one long document cannot outrank a
            # genuinely rarer match by sheer term count.
            weighted += weights.get(t, WEIGHT_FLOOR) * (1.0 + math.log1p(c))
    return weighted, distinct, total


def matched_lines(body, terms):
    out = []
    for line in body.splitlines():
        low = line.lower()
        if any(count_term(low, t) for t in terms):
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


def block_spans(content):
    """1-based (first, last) file line of each block split_blocks() returns, in
    the same order, or None when the two disagree. Counted on "\\n" so the
    numbers match Read and sed; splitlines() also breaks on characters such as
    U+2028, which can start an extra block."""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the file's final newline does not start a line
    starts = [i + 1 for i, ln in enumerate(lines) if ln.startswith("## ")]
    if len(starts) != len(split_blocks(content)):
        return None
    ends = [s - 1 for s in starts[1:]] + [len(lines)]
    return list(zip(starts, ends))


def search_timeline(terms, limit):
    # Collect blocks first: term weights need the whole corpus before scoring.
    blocks = []  # (date, heading, body, body_lower, loc)
    for path in sorted(glob.glob(os.path.join(TIMELINE_DIR, "[0-9]" * 4 + "-*.md"))):
        date = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        parts = split_blocks(content)
        spans = block_spans(content) or [None] * len(parts)
        for (heading, body), span in zip(parts, spans):
            blocks.append((date, heading, body, body.lower(), (path, span)))

    weights = term_weights(terms, [b[3] for b in blocks])
    hits = []  # (weighted, distinct, total, date, heading, lines, (path, span))
    for date, heading, body, body_lower, loc in blocks:
        w, d, t = score_weighted(body_lower, terms, weights)
        if d == 0:
            continue
        hits.append((w, d, t, date, heading, matched_lines(body, terms), loc))
    hits.sort(key=lambda h: (h[0], h[2], h[3]), reverse=True)
    return hits[:limit]


def print_timeline_hits(hits, terms):
    if not hits:
        print("No matches in the timeline. Try --raw to search raw conversations, or change your keywords.")
        return
    print("=== Timeline search results (%d hits, query: %s) ===\n" % (len(hits), " ".join(terms)))
    for _w, d, t, date, heading, lines, (path, span) in hits:
        print("● [%s] %s   (%d/%d terms matched, %d occurrences)" % (date, heading, d, len(terms), t))
        for ln in lines:
            print("    %s" % ln)
        print("    ↳ %s%s" % (path, " (lines %d-%d)" % span if span else ""))
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
    # Read every thread first: term weights need the whole corpus before scoring.
    docs = []  # (path, content, content_lower)
    for path in sorted(glob.glob(os.path.join(THREADS_DIR, "*.md"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        docs.append((path, content, content.lower()))
    weights = term_weights(terms, [d[2] for d in docs])

    clusters = {}  # canonical -> aggregation dict
    for path, content, content_lower in docs:
        w, d, t = score_weighted(content_lower, terms, weights)
        if d == 0:
            continue
        slug = os.path.splitext(os.path.basename(path))[0]
        canonical = resolve_canonical(slug, registry)
        e = registry.get(canonical, {})
        c = clusters.get(canonical)
        if c is None:
            c = {"w": 0.0, "d": 0, "t": 0, "lines": [], "path": path, "last_date": "",
                 "name": e.get("name") or h1_of(content) or canonical,
                 "current_state": e.get("current_state"), "via": set()}
            clusters[canonical] = c
        if slug != canonical:                       # matched on an alias file
            c["via"].add(registry.get(slug, {}).get("name") or slug)
        if (w, t) > (c["w"], c["t"]):               # take matched lines from the highest-scoring file
            c["w"], c["d"], c["t"] = w, d, t
            c["lines"] = matched_lines(content, terms)
            dates = THREAD_DATE_RE.findall(content)
            c["last_date"] = dates[-1] if dates else ""
        if slug == canonical:                        # display path points to the canonical file
            c["path"] = path
    ranked = sorted(clusters.values(),
                    key=lambda c: (c["w"], c["t"], c["last_date"]), reverse=True)
    return ranked[:limit]


# Ambiguity gate. When the leading threads score within this fraction of the
# top hit, the ranking is not actually deciding anything and rank 1 should not
# be presented as the answer. Measured over 1572 realistic recall questions:
# inside this band the top hit is wrong 51% of the time, outside it 2.8%.
AMBIGUITY_MARGIN = 0.20
MAX_CANDIDATES = 4          # AskUserQuestion takes at most 4 options
AMBIGUOUS_MARKER = "[AMBIGUOUS]"


def ambiguous_candidates(hits, margin=AMBIGUITY_MARGIN):
    """Thread hits effectively tied with the leader. Empty when rank 1 is clear."""
    if len(hits) < 2:
        return []
    top = hits[0]["w"]
    if top <= 0:
        return []
    tied = [h for h in hits if h["w"] >= top * (1.0 - margin)]
    return tied[:MAX_CANDIDATES] if len(tied) >= 2 else []


def print_ambiguity_note(hits):
    """Warn when the top threads are indistinguishable, and name the candidates
    so the caller can ask the user instead of guessing."""
    tied = ambiguous_candidates(hits)
    if not tied:
        return
    print("%s The top %d threads score within %d%% of each other — ranking is not "
          "deciding between them. Ask which one the user means (offer these as "
          "options) before answering; do not present the first as the answer."
          % (AMBIGUOUS_MARKER, len(tied), int(AMBIGUITY_MARGIN * 100)))
    for i, c in enumerate(tied, 1):
        print("  %d. %s   (latest %s)" % (i, c["name"], c["last_date"] or "-"))
    print()


def print_thread_hits(hits, terms):
    if not hits:
        return
    print("=== Work threads (%d hits, query: %s) ===\n" % (len(hits), " ".join(terms)))
    print_ambiguity_note(hits)
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
                    if not all(count_term(low, t) for t in terms):
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
