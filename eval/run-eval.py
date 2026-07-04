#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Retrieval-quality regression harness for the recall search.

Ground truth comes from YOUR OWN timeline: an LLM (claude -p) harvests real
events from each day file and phrases them as recall-style questions across
four difficulty tiers, plus negative controls (topics you never worked on).
Each question is then answered blindly by the recall tool itself (terms_of +
timeline search, no LLM in the answering path) and judged against the ground
truth by claude -p in batches.

Cases contain your private work data — they are stored in cases.local.json,
which is gitignored. Never commit them.

Usage:
  eval/run-eval.py --harvest        # (re)generate cases from the local timeline
  eval/run-eval.py                  # run the eval against cached cases
  eval/run-eval.py --harvest --run  # both
  eval/run-eval.py --cases PATH     # use a different case file

Typical numbers (measured 2026-07-04, 96 cases, bilingual Recall tags on):
hit@1 96%, top-3 99%, false positives 0/12.
"""
import os
import re
import sys
import json
import glob
import argparse
import importlib.util

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(EVAL_DIR)
DEFAULT_CASES = os.path.join(EVAL_DIR, "cases.local.json")

TIERS = ("easy", "natural", "semantic", "crosslingual")
CASES_PER_DAY = 4          # one per tier
NEGATIVE_CASES = 12
JUDGE_BATCH = 12           # cases per claude -p judging call
TOP_K = 3                  # hits shown to the judge


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wt = _load("work_timeline", os.path.join(REPO_DIR, "scripts", "work-timeline.py"))
rc = _load("recall", os.path.join(REPO_DIR, "skills", "recall", "recall.py"))


# ---------- harvest ----------

HARVEST_PROMPT = """[work-timeline-internal]
You are building an evaluation set for a lexical "recall" search tool over a work timeline.
Below is one day's timeline file. Extract exactly %(n)d DISTINCT real events and craft one
recall-style user question per event, one for EACH difficulty tier:
- "easy": includes the event's single most distinctive keyword (service/error/tool name).
- "natural": a normal sentence a user might ask, without rare exact tokens.
- "semantic": paraphrase HEAVILY with synonyms so the question shares MINIMAL vocabulary with the log.
- "crosslingual": ask in the OTHER language than the log mostly uses (English if the log is Korean, and vice versa).

Every case MUST be grounded in the file's real content — never invent events.
Output ONLY a JSON array (no markdown fences):
[{"date": "%(date)s", "difficulty": "easy", "query": "...", "expected_topic": "concise factual description of the true event"}, ...]

--- timeline file (%(date)s) ---
%(body)s
"""

NEGATIVE_PROMPT = """[work-timeline-internal]
Below is a work timeline (multiple days). Generate %(n)d recall-style questions about topics
that CLEARLY do NOT appear anywhere in it — negative controls for false-positive testing.
Pick well-known tech topics absent from the log (different frameworks, clouds, languages).
Output ONLY a JSON array (no markdown fences):
[{"date": "", "difficulty": "negative", "query": "...", "expected_topic": "(absent)"}, ...]

--- timeline digest ---
%(body)s
"""


def parse_json_array(text):
    a, b = text.find("["), text.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        out = json.loads(text[a:b + 1])
        return out if isinstance(out, list) else []
    except Exception:
        return []


def harvest(cases_path):
    files = sorted(glob.glob(os.path.join(rc.TIMELINE_DIR, "[0-9]" * 4 + "-*.md")))
    if not files:
        print("No timeline files under %s — nothing to harvest." % rc.TIMELINE_DIR)
        sys.exit(1)
    cases = []
    for path in files:
        date = os.path.splitext(os.path.basename(path))[0]
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
        if len(body) < 400:   # skip near-empty days
            continue
        print("harvesting %s …" % date)
        try:
            out = wt.run_claude(HARVEST_PROMPT % {
                "n": CASES_PER_DAY, "date": date, "body": body[:24000]})
        except Exception as e:
            print("  ! harvest failed: %s" % e)
            continue
        got = [c for c in parse_json_array(out)
               if isinstance(c, dict) and c.get("query") and c.get("difficulty") in TIERS]
        for c in got:
            c["date"] = date
        cases.extend(got)
        print("  +%d cases" % len(got))

    # negatives from a compact digest (headings only)
    digest = []
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(("- **", "## ")):
                    digest.append(line.rstrip())
    print("harvesting negatives …")
    try:
        out = wt.run_claude(NEGATIVE_PROMPT % {
            "n": NEGATIVE_CASES, "body": "\n".join(digest)[:24000]})
        negs = [c for c in parse_json_array(out)
                if isinstance(c, dict) and c.get("query")]
        for c in negs:
            c["difficulty"], c["date"], c["expected_topic"] = "negative", "", "(absent)"
        cases.extend(negs[:NEGATIVE_CASES])
        print("  +%d negatives" % min(len(negs), NEGATIVE_CASES))
    except Exception as e:
        print("  ! negative harvest failed: %s" % e)

    for i, c in enumerate(cases, 1):
        c["id"] = i
    os.makedirs(os.path.dirname(cases_path), exist_ok=True)
    with open(cases_path, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=1)
    print("saved %d cases → %s" % (len(cases), cases_path))
    return cases


# ---------- answer (deterministic: the tool under test, no LLM) ----------

def answer(case):
    terms = rc.terms_of(case["query"])
    hits = rc.search_timeline(terms, TOP_K)
    return {
        "terms": terms,
        "hits": [{"date": d8, "heading": heading, "score": "%d/%d" % (d, len(terms)),
                  "lines": lines[:2]}
                 for d, _t, d8, heading, lines in hits],
    }


# ---------- judge ----------

JUDGE_PROMPT = """[work-timeline-internal]
Judge recall-search results against ground truth. For each case decide:
- positive case: "hit@1" if hits[0] is the SAME event as the ground truth (matching date and
  meaning), "hit@3" if it appears in hits[1..], else "miss".
- negative case (expected_topic is "(absent)"): "correct_reject" if no hit is genuinely about
  the queried topic (weak unrelated matches are fine), else "false_positive".
Output ONLY a JSON object mapping id to verdict, e.g. {"7": "hit@1", "8": "miss"}.

--- cases ---
%s
"""


def judge_batch(batch):
    payload = []
    for c, a in batch:
        payload.append({
            "id": c["id"], "difficulty": c["difficulty"], "query": c["query"],
            "expected_date": c["date"], "expected_topic": c["expected_topic"],
            "search_terms": a["terms"], "hits": a["hits"],
        })
    try:
        out = wt.run_claude(JUDGE_PROMPT % json.dumps(payload, ensure_ascii=False, indent=1))
    except Exception as e:
        print("  ! judge failed: %s" % e)
        return {}
    a, b = out.find("{"), out.rfind("}")
    if a < 0 or b <= a:
        return {}
    try:
        return {str(k): v for k, v in json.loads(out[a:b + 1]).items()}
    except Exception:
        return {}


# ---------- scorecard ----------

def run_eval(cases):
    answered = []
    for c in cases:
        answered.append((c, answer(c)))
    verdicts = {}
    for i in range(0, len(answered), JUDGE_BATCH):
        batch = answered[i:i + JUDGE_BATCH]
        print("judging %d–%d / %d …" % (i + 1, i + len(batch), len(answered)))
        verdicts.update(judge_batch(batch))

    def vc(subset, v):
        return sum(1 for c, _ in subset if verdicts.get(str(c["id"])) == v)

    pos = [(c, a) for c, a in answered if c["difficulty"] != "negative"]
    neg = [(c, a) for c, a in answered if c["difficulty"] == "negative"]

    print("\n=== recall eval scorecard (%d cases) ===" % len(answered))
    if pos:
        h1, h3, miss = vc(pos, "hit@1"), vc(pos, "hit@3"), vc(pos, "miss")
        print("positives: hit@1 %d/%d (%.0f%%) · top-3 %d (%.0f%%) · miss %d"
              % (h1, len(pos), 100.0 * h1 / len(pos),
                 h1 + h3, 100.0 * (h1 + h3) / len(pos), miss))
        for tier in TIERS:
            sub = [(c, a) for c, a in pos if c["difficulty"] == tier]
            if sub:
                t1 = vc(sub, "hit@1")
                print("  %-12s hit@1 %d/%d · miss %d" % (tier, t1, len(sub), vc(sub, "miss")))
    if neg:
        fp = vc(neg, "false_positive")
        print("negatives: correct_reject %d/%d · false_positive %d"
              % (vc(neg, "correct_reject"), len(neg), fp))
    misses = [c for c, _ in pos if verdicts.get(str(c["id"])) == "miss"]
    if misses:
        print("\nmisses:")
        for c in misses:
            print("  #%d [%s] %s (expected %s)" % (c["id"], c["difficulty"], c["query"], c["date"]))
    unjudged = [c for c, _ in answered if str(c["id"]) not in verdicts]
    if unjudged:
        print("\n(unjudged: %d — judge call failed for these)" % len(unjudged))


def main():
    ap = argparse.ArgumentParser(description="recall retrieval-quality regression eval")
    ap.add_argument("--harvest", action="store_true", help="(re)generate cases from the local timeline")
    ap.add_argument("--run", action="store_true", help="run the eval (default unless --harvest is given alone)")
    ap.add_argument("--cases", default=DEFAULT_CASES, help="case file path (default: eval/cases.local.json)")
    args = ap.parse_args()

    cases = None
    if args.harvest:
        cases = harvest(args.cases)
        if not args.run:
            return
    if cases is None:
        try:
            with open(args.cases, "r", encoding="utf-8") as f:
                cases = json.load(f)
        except Exception:
            print("No case file at %s — run with --harvest first." % args.cases)
            sys.exit(1)
    run_eval(cases)


if __name__ == "__main__":
    main()
