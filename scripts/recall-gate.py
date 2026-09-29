#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UserPromptSubmit hook — enforce the recall rule.

If a user prompt matches a "recall past work" pattern, automatically run
recall.py and inject its result into the context, enforcing the instruction:
"don't guess from memory, answer based on the recall result."
If no trigger matches, output nothing (no injection).
"""
import sys
import os
import re
import json
import shlex
import subprocess
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")


def _find_recall():
    """recall.py lives beside this script in the plugin layout
    (<root>/scripts/recall-gate.py + <root>/skills/recall/recall.py); the
    install.sh layout copies them to separate dirs under CLAUDE_CONFIG_DIR."""
    plugin = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "skills", "recall", "recall.py")
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    legacy = os.path.join(config_dir, "skills", "recall", "recall.py")
    for p in (os.path.normpath(plugin), legacy):
        if os.path.exists(p):
            return p
    return legacy


RECALL = _find_recall()
# Same directory recall.py searches; named in the footer as a full-text source.
TIMELINE_DIR = os.path.join(
    os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"), "work-timeline")

# Kept in sync with recall.py's marker (the gate reads recall's stdout, so it
# cannot import it — recall.py lives in a skills/ dir that is not a package).
AMBIGUOUS_MARKER = "[AMBIGUOUS]"

# Size cap for the whole additionalContext. Claude Code (checked in 2.1.284)
# moves hook output longer than 10,000 chars to a file and the model sees only
# a ~2KB preview. It counts JS string length (UTF-16 units), so ulen() below
# counts the same way; the cap keeps a small margin under that limit.
CONTEXT_BUDGET = 9500
# Rank-order policy, used only when the result is over the cap: how many
# leading blocks per section keep their full text; the rest collapse to their
# header line.
MAX_FULL_THREADS = 3
MAX_FULL_TIMELINE = 5

# Optional relevance filter: TypeSafe's Jev scores each block against the
# prompt. Opt-in only via this recall-specific key (a generic TYPESAFE_API_KEY
# is ignored), because it sends recall results off the machine.
JEV_KEY_ENV = "CCRECALL_TYPESAFE_API_KEY"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_TIMEOUT = 4  # seconds; recall itself gets 20s of the 25s hook timeout
# Low on purpose: Jev is less accurate on non-English/CJK text, and missing a
# relevant block costs more than keeping an extra one.
JEV_KEEP = 0.3
JEV_QUESTION = "Does `passage` contain information needed to answer `user_question`?"
JEV_CRITERIA = {
    "true": "The passage describes the work, decision, date, error or result the user is asking about",
    "false": "The passage is about a different topic and only shares a keyword",
}

# Recall-question triggers (per the CLAUDE.md recall rule).
# Compiled case-insensitively, so the English patterns below match regardless
# of case; the Korean patterns are unaffected by case folding.
TRIGGERS = [
    # Korean
    r"기억\s*(?:해|나|하|남|할|했)",
    r"전에",
    r"예전",
    r"지난\s*번",
    r"저번",
    r"그\s*때",
    r"언제\s*(?:했|만들|배포|고|작업|짰|썼|봤|구현|돌)",
    r"했던\s*(?:거|것|건)",
    r"했었",
    r"하던\s*거",
    r"만들던",
    r"\b전에\b",
    # English
    r"when did i",
    r"when did we",
    r"last time",
    r"remember when",
    r"did (?:i|we)",
    r"how did (?:i|we)",
    r"\bpreviously\b",
    r"\bearlier\b",
    r"used to",
    r"that .* (?:error|bug|issue)",
]
TRIG_RE = re.compile("|".join(TRIGGERS), re.IGNORECASE)

# Stopwords to exclude from keywords (triggers, pronouns, common verb stems).
STOP = {
    # Korean
    "기억해", "기억", "기억나", "전에", "예전", "지난번", "저번", "그때", "언제",
    "했지", "했어", "했던", "했었", "하던", "만들던", "만들던거", "만들", "하던거",
    "그거", "그게", "이거", "저거", "내가", "우리", "그", "좀", "해줘", "했나",
    "뭐", "뭐였지", "어떻게", "왜", "거", "것", "건", "때", "줘", "해", "나", "수",
    # English (extract_keywords lowercases latin tokens)
    "the", "a", "when", "did", "how", "what", "was",
}

# Strip trailing Korean particles/endings.
# Longer endings come first so "관련해서" strips to "관련" rather than stalling.
JOSA = re.compile(
    r"(을|를|이|가|은|는|에|의|로|으로|도|만|와|과|랑|이랑|에서|까지|부터"
    r"|해서|해야|하는|한|던거|던|거|게|야|냐|니|네|좀|했|하)+$"
)


# Prompts that are actually system/harness wrappers, not the user asking something
# (task notifications, command output, reminders). Never treat these as recall
# questions — their XML-ish tokens would otherwise become garbage keywords.
SYSTEM_WRAPPER_PREFIXES = (
    "<task-notification",
    "<system-reminder",
    "<local-command-caveat",
    "<command-name",
    "<bash-input",
    "<bash-stdout",
    "[Request interrupted",
    "Caveat:",
    # This plugin's own summary prompts (headless claude -p). They quote user
    # prompts, so recall questions inside them would trip the triggers.
    "[work-timeline-internal]",
    # A report handed back by a subagent or another session.
    "Another Claude session sent a message:",
    "<agent-message",
)

# Pasted text (logs, handoff notes, emails) is not the question itself. Its words
# would trip the triggers and fill the keyword slots, so it is dropped first.
# The closing tag carries the same id; an unclosed block runs to the end.
PASTE_RE = re.compile(
    r'<pasted_content id="([^"]*)">.*?(?:</pasted_content id="\1">|\Z)', re.S)

# A URL or file path is reduced to its last segment (PR number, doc id, file
# name). Left whole, its scheme, host and directories take every keyword slot.
LOCATOR_RE = re.compile(r"(?<!\S)(?:https?://|~/|/)\S+")


def _last_segment(m):
    seg = m.group(0).rstrip("/").rsplit("/", 1)[-1]
    return os.path.splitext(seg)[0]


def extract_keywords(prompt):
    prompt = LOCATOR_RE.sub(_last_segment, prompt)
    raw = re.split(r"[\s,.;:!?()\[\]{}<>'\"`~/\\|=&]+", prompt)
    kws = []
    for tok in raw:
        if not tok:
            continue
        low = tok.lower()
        # Keep Latin alphanumeric tokens as-is (ai, content, fargate, etc.).
        if re.fullmatch(r"[a-z0-9_.+-]{2,}", low):
            if low not in STOP:
                kws.append(low)
            continue
        # Korean token: adopt after stripping particles.
        stripped = JOSA.sub("", tok)
        if len(stripped) < 2:
            continue
        if stripped in STOP or tok in STOP:
            continue
        kws.append(stripped)
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out[:4]


def ulen(s):
    """Length as Claude Code measures it: UTF-16 units, so an emoji counts 2."""
    return len(s.encode("utf-16-le")) // 2


def ucut(s, n):
    """First n UTF-16 units of s (a split surrogate pair is dropped)."""
    return s.encode("utf-16-le")[:2 * n].decode("utf-16-le", "ignore")


def parse_items(recall_out):
    """Split recall.py's stdout into ordered (kind, lines) items.

    A '● ' result block runs to the next blank line (kept with it) and is a
    "thread" or "timeline" block depending on the '=== ' section it sits in.
    Everything else — section headers, the ambiguity note (also up to its blank
    line), stray lines — is "text" and is always rendered verbatim."""
    items, section = [], None
    lines = recall_out.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("=== "):
            section = ("thread" if line.startswith("=== Work threads") else
                       "timeline" if line.startswith("=== Timeline search results") else None)
        if line.startswith(("● ", AMBIGUOUS_MARKER)):
            j = i
            while j < len(lines) and lines[j].strip():
                j += 1
            kind = section if line.startswith("● ") and section else "text"
            items.append((kind, lines[i:j + 1]))
            i = j + 1
        else:
            items.append(("text", [line]))
            i += 1
    return items


def collapse(kind, lines):
    """Header line only; a thread block also keeps its '↳ <path>' line."""
    keep = [lines[0]]
    if kind == "thread":
        keep += [ln for ln in lines if ln.lstrip().startswith("↳ ")][-1:]
    if lines[-1] == "":
        keep.append("")  # blank separator before the next entry
    return keep


def jev_scores(prompt, passages):
    """Ask Jev, in one request, whether each passage helps answer the prompt.

    Returns (scores, None) on success and (None, reason) on any failure. With
    the key unset it returns (None, None) without touching the network."""
    key = os.environ.get(JEV_KEY_ENV)
    if not key:
        return None, None
    body = {
        "model": "jev-latest",
        "state": {"user_question": prompt[:2000]},
        "questions": {
            "b%d" % n: {"type": "noul",
                        "instructions": {"passage": p, "question": JEV_QUESTION},
                        "criteria": JEV_CRITERIA}
            for n, p in enumerate(passages)
        },
    }
    try:
        req = urllib.request.Request(
            JEV_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=JEV_TIMEOUT) as resp:
            answers = json.loads(resp.read().decode("utf-8"))["answers"]
        return [float(answers["b%d" % n]["noul"]) for n in range(len(passages))], None
    except urllib.error.HTTPError as e:
        e.close()
        return None, "HTTP %d" % e.code
    except (KeyError, TypeError, ValueError):  # bad JSON, missing answers/noul
        return None, "unexpected response"
    except Exception as e:  # timeout, connection refused, DNS, ...
        reason = getattr(e, "reason", None) or e
        return None, (str(reason) or type(e).__name__)[:120]


def footer(n_full, n_total, n_unfit, omitted, how, note, kws):
    """omitted: {"thread": n, "timeline": n} — blocks with not even a header shown."""
    lines = [
        "--- recall result shortened to fit the hook output limit ---",
        "%d of %d results are shown in full, picked by %s. Collapsed entries show "
        "only their header line (threads keep their ↳ file path)." % (n_full, n_total, how),
    ]
    if n_unfit:
        lines.append("%d picked results are collapsed because their full text did not fit."
                     % n_unfit)
    parts = ["%d %s" % (omitted[k], label) for k, label in
             (("thread", "threads"), ("timeline", "timeline hits")) if omitted[k]]
    if parts:
        lines.append("Omitted entirely (not even a header fit): %s." % ", ".join(parts))
    if note:
        lines.append(note)
    lines.append("For the full text run: python3 %s %s — or Read the ↳ thread "
                 "file, or %s/<date>.md for a timeline entry."
                 % (shlex.quote(RECALL), shlex.quote(" ".join(kws)), TIMELINE_DIR))
    return "\n".join(lines)


def shape(recall_out, prompt, kws):
    """Build the additionalContext for a recall result, at most CONTEXT_BUDGET
    UTF-16 units.

    A result that fits is injected exactly as printed, with no footer. Over the
    cap, blocks picked for full text (rank order, or Jev relevance when
    enabled) are shown whole while they fit; the others collapse to their
    header, and a footer says how to get the full text."""
    # recall marks a result whose leading threads are indistinguishable. There,
    # rank 1 is wrong about half the time, so asking beats guessing — and the
    # candidates are already named, so the question can offer real options
    # instead of telling the user to "be more specific".
    if AMBIGUOUS_MARKER in recall_out:
        directive = (
            "The search could not separate the leading candidates (see the "
            "%s line below). Do NOT answer from the top hit. Ask the user which "
            "thread they mean with AskUserQuestion, using the listed candidates "
            "as the options, then answer from the one they pick."
        ) % AMBIGUOUS_MARKER
    else:
        directive = (
            "If the keywords missed the mark or the result is sparse, re-run the "
            "recall skill yourself with more precise keywords before answering."
        )

    head = (
        "[recall enforcement hook] This prompt was detected as a recall question "
        "about past work. Per the CLAUDE.md recall rule, do not rely on memory or "
        "guessing; answer using the auto-run recall result below as your primary "
        "source. Auto-extracted keywords: [%s]. %s\n\n--- recall result ---\n"
    ) % (", ".join(kws), directive)
    whole = head + (recall_out or "(no result)")
    if ulen(whole) <= CONTEXT_BUDGET:
        return whole

    items = parse_items(recall_out)
    blocks = [i for i, (kind, _) in enumerate(items) if kind in ("thread", "timeline")]
    limit = {"thread": MAX_FULL_THREADS, "timeline": MAX_FULL_TIMELINE}
    # The ambiguity note names the leading threads as the options to offer, so
    # every one of them gets full text and can be compared.
    for kind, lines in items:
        if kind == "text" and lines[0].startswith(AMBIGUOUS_MARKER):
            n_tied = sum(1 for ln in lines if re.match(r"\s+\d+\. ", ln))
            limit["thread"] = max(limit["thread"], n_tied)
    rank, seen = set(), {"thread": 0, "timeline": 0}
    for i in blocks:
        kind = items[i][0]
        if seen[kind] < limit[kind]:
            rank.add(i)
        seen[kind] += 1

    # Only a result that needs cutting is sent to Jev.
    chosen, note = rank, None
    how = "rank order (up to the first %d threads and %d timeline hits)" % (
        limit["thread"], limit["timeline"])
    upgrade_order = blocks
    scores, err = jev_scores(prompt, ["\n".join(items[i][1]).rstrip("\n") for i in blocks])
    if scores is not None:
        relevant = {i for i, s in zip(blocks, scores) if s >= JEV_KEEP}
        if relevant:
            chosen, how = relevant, "Jev relevance (score >= %s)" % JEV_KEEP
            # Jev usually picks more than fits; the most relevant get full text first.
            score_of = dict(zip(blocks, scores))
            upgrade_order = sorted(blocks, key=lambda i: -score_of[i])
        else:
            note = "(Jev marked no result relevant; used rank order.)"
    elif err:
        note = "(Jev relevance check failed: %s; used rank order.)" % err

    # Budget: verbatim text and a worst-case footer are fixed. Chosen blocks get
    # their collapsed form first, then full text in order (rank, or Jev score
    # when Jev picked); the first one that does not fit ends the upgrades in its
    # section, so a lower-ranked (or lower-scored) block
    # never gets full text ahead of it. The remaining blocks then get their
    # collapsed form in order, and trailing ones that no longer fit are dropped.
    def cost(lines):
        return sum(ulen(ln) + 1 for ln in lines)

    n = len(blocks)
    tail_max = "\n\n" + footer(n, n, n, {"thread": n, "timeline": n}, how, note, kws)
    room = (CONTEXT_BUDGET - ulen(head) - ulen(tail_max)
            - sum(cost(lines) for kind, lines in items if kind == "text"))
    short = {i: collapse(*items[i]) for i in blocks}
    shown = {}
    for i in blocks:
        if i in chosen and cost(short[i]) <= room:
            shown[i] = short[i]
            room -= cost(short[i])
    stopped = set()
    for i in upgrade_order:
        kind, lines = items[i]
        if i not in shown or kind in stopped:
            continue
        extra = cost(lines) - cost(short[i])
        if extra > room:
            stopped.add(kind)
            continue
        shown[i] = lines
        room -= extra
    for i in blocks:
        if i in shown:
            continue
        if cost(short[i]) > room:
            break
        shown[i] = short[i]
        room -= cost(short[i])

    out = []
    for i, (kind, lines) in enumerate(items):
        out += lines if kind == "text" else shown.get(i, [])
    body = "\n".join(out).rstrip()
    full = {i for i in shown if shown[i] == items[i][1]}
    tail = ""
    if len(full) < n:
        # A picked block left out entirely counts as omitted, not also as collapsed.
        unfit = (chosen & set(shown)) - full
        omitted = {"thread": 0, "timeline": 0}
        for i in blocks:
            if i not in shown:
                omitted[items[i][0]] += 1
        tail = "\n\n" + footer(len(full), n, len(unfit), omitted, how, note, kws)
    # Last resort, only reachable if the verbatim text (or a pasted, very long
    # keyword echoed in head and footer) alone exceeds the budget. Dropping
    # `over` chars removes at least `over` UTF-16 units.
    over = ulen(head) + ulen(body) + ulen(tail) - CONTEXT_BUDGET
    if over > 0:
        body = body[:max(0, len(body) - over - 1)] + "…"
    return ucut(head + body + tail, CONTEXT_BUDGET)


def run(payload):
    """Return the JSON string to inject when a recall trigger matches, else None."""
    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    if prompt.lstrip().startswith(SYSTEM_WRAPPER_PREFIXES):
        return None  # harness-injected content, not a user question
    prompt = PASTE_RE.sub(" ", prompt)
    if not prompt.strip() or not TRIG_RE.search(prompt):
        return None
    kws = extract_keywords(prompt)
    if not kws:
        return None
    try:
        res = subprocess.run(
            ["python3", RECALL, " ".join(kws)],
            capture_output=True, text=True, timeout=20,
        )
        recall_out = res.stdout.strip()
    except Exception as e:
        recall_out = "(recall failed to run: %s)" % e

    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": shape(recall_out, prompt, kws),
        }
    }, ensure_ascii=False)


def main():
    # fail-open: swallow any exception and pass through silently. Even if recall
    # injection fails, the prompt/session is never blocked (output nothing = no
    # injection).
    try:
        payload = json.loads(sys.stdin.read())
        out = run(payload)
        if out:
            print(out)
    except Exception:
        pass


if __name__ == "__main__":
    main()
