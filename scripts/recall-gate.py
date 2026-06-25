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
import subprocess

HOME = os.path.expanduser("~")
RECALL = os.path.join(HOME, ".claude", "skills", "recall", "recall.py")

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


def extract_keywords(prompt):
    raw = re.split(r"[\s,.;:!?()\[\]{}'\"`~/\\|]+", prompt)
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


def run(payload):
    """Return the JSON string to inject when a recall trigger matches, else None."""
    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
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

    context = (
        "[recall enforcement hook] This prompt was detected as a recall question "
        "about past work. Per the CLAUDE.md recall rule, do not rely on memory or "
        "guessing; answer using the auto-run recall result below as your primary "
        "source. Auto-extracted keywords: [%s]. If the keywords missed the mark or "
        "the result is sparse, re-run the recall skill yourself with more precise "
        "keywords before answering.\n\n--- recall result ---\n%s"
    ) % (", ".join(kws), recall_out or "(no result)")

    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
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
