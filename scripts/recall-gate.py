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
import shutil
import subprocess

HOME = os.path.expanduser("~")
RECALL = os.path.join(HOME, ".claude", "skills", "recall", "recall.py")

# Fast model for keyword extraction (claude -p, single turn).
LLM_MODEL = "claude-haiku-4-5-20251001"
LLM_SYSTEM_PROMPT = (
    "You are a search-keyword extractor for a past-work recall system. "
    "Given the user's message, output ONLY comma-separated search terms on a "
    "single line - no sentences, no preamble, no tool calls. Prioritize ticket "
    "IDs (e.g. BE-1052), file paths, error messages, and proper nouns. Exclude "
    "URL parts (https, domain names like yplabs/atlassian), generic verbs, and "
    "pronouns. Also IGNORE meta-instructions about which tool or skill to use "
    "for searching (e.g. 'recall 스킬에서', 'using recall', 'search for') - "
    "extract only terms describing the actual past work or subject being "
    "recalled. Keep each term in the SAME language as it appears in the message "
    "- do NOT translate Korean terms into English. Output 3-6 terms."
)

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
JOSA = re.compile(
    r"(을|를|이|가|은|는|에|의|로|으로|도|만|와|과|랑|이랑|에서|까지|부터"
    r"|던거|던|거|게|야|냐|니|네|좀|했|하)+$"
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
)


def extract_keywords(prompt):
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


def _claude_bin():
    return shutil.which("claude") or next(
        (p for p in [os.path.join(HOME, ".local", "bin", "claude")]
         if os.path.exists(p)),
        None,
    )


def extract_keywords_llm(prompt):
    """Extract search terms via claude -p (Haiku, single turn). Returns None on
    failure -> caller falls back to the regex extractor.

    - --tools '' : block the agent tool loop (single turn).
    - --setting-sources '' : don't load hooks = recursion guard.
    - --system-prompt : replace the 'You are Claude Code' framing with the
      extractor role.
    """
    claude = _claude_bin()
    if not claude:
        return None
    try:
        r = subprocess.run(
            [claude, "-p",
             "--model", LLM_MODEL,
             "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
             "--setting-sources", "",
             "--tools", "",
             "--system-prompt", LLM_SYSTEM_PROMPT,
             "--output-format", "json",
             prompt],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "CLAUDE_RECALL_GATE": "1"},
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
        if data.get("is_error"):
            return None
        text = (data.get("result") or "").strip()
        line = next((ln for ln in text.splitlines() if ln.strip()), "")
        out, seen = [], set()
        for part in line.split(","):
            t = part.strip().strip("`'\"[]")
            # Sentences/cruft (long tokens or multi-word) => treat as failed
            # extraction and drop them.
            if not t or len(t) > 40 or len(t.split()) > 4:
                continue
            if len(t) >= 2 and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out[:6] or None
    except Exception:
        return None


def get_keywords(prompt):
    """Return (keywords, method). LLM first, regex fallback on failure."""
    kws = extract_keywords_llm(prompt)
    if kws:
        return kws, "model"
    return extract_keywords(prompt), "regex fallback"


def run(payload):
    """Return the JSON string to inject when a recall trigger matches, else None."""
    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    if not prompt.strip() or not TRIG_RE.search(prompt):
        return None
    if prompt.lstrip().startswith(SYSTEM_WRAPPER_PREFIXES):
        return None  # harness-injected content, not a user question
    kws, method = get_keywords(prompt)
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

    context = (
        "[recall enforcement hook] This prompt was detected as a recall question "
        "about past work. Per the CLAUDE.md recall rule, do not rely on memory or "
        "guessing; answer using the auto-run recall result below as your primary "
        "source. Auto-extracted keywords (%s): [%s]. If the keywords missed the "
        "mark or the result is sparse, re-run the recall skill yourself with more "
        "precise keywords before answering.\n\n--- recall result ---\n%s"
    ) % (method, ", ".join(kws), recall_out or "(no result)")

    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }, ensure_ascii=False)


def main():
    # Recursion guard: if the hook's own `claude -p` re-triggers this hook,
    # pass through immediately. (--setting-sources '' also blocks it; this is a
    # second safety net.)
    if os.environ.get("CLAUDE_RECALL_GATE"):
        return
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
