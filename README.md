# claude-code-recall

**A searchable memory for your [Claude Code](https://claude.com/claude-code) work.**

`claude-code-recall` quietly scans your Claude Code sessions in the background and
builds a clean, time-bucketed work log — *what you did, in which project, when* —
then gives Claude a **`recall`** skill to search it. So when you ask *"when did I
fix that OpenSSL thing?"* or *"how did I handle the Fargate scaling last time?"*,
Claude actually looks it up instead of guessing.

```
## 14:00–14:15
- **my-api** · `main` — Reviewed the async-logging change (0 issues), added a
  regression test keeping the webhook synchronous (41 passed), opened a draft PR.
- **infra** — Diagnosed the object-storage upload auth failure: the OAuth token
  was fine; the real fix was setting the API token in the profile env.
```

---

## How it works

Claude Code already records every session as a transcript (`~/.claude/projects/**/*.jsonl`).
This tool turns those raw transcripts into something you can actually recall:

| Component | What it does |
|---|---|
| `work-timeline.py` | Runs from the **`Stop` hook** (after every turn) and **`SessionStart`** (catch-up). Scans transcripts, groups sessions into fixed time buckets, and writes one short LLM summary per bucket to `~/.claude/work-timeline/YYYY-MM-DD.md`. Debounced, non-blocking (detached worker), concurrency-safe, incremental & idempotent; catches up after sleep. |
| `work-timeline-rollup.py` | Once a day, prepends a 🧠 *Daily Summary* to that day's file. |
| `work-timeline-threads.py` | Stitches related work across days into "threads". |
| `work-timeline-consolidate.py` | Consolidates/cleans the accumulated logs. |
| `skills/recall` | A Claude Code **skill**: searches the timeline first, drills into raw transcripts only when an exact phrase/error is needed. |
| `recall-gate.py` | A `UserPromptSubmit` hook that auto-runs `recall` when your prompt looks like a recall question (English + Korean triggers) and injects the result into context. |

The timeline and the recall skill use the **`claude` CLI** (`claude -p`) for summaries,
so summarization runs on your own Claude account.

## Requirements

- **macOS or Linux** — daily-summary jobs run via `launchd` (macOS) or `cron` (Linux).
  Ingestion itself is event-driven (Claude Code hooks) and platform-agnostic.
- `python3` (stdlib only — no packages to install; on macOS the Command Line Tools python is used if present)
- The **`claude` CLI** on your `PATH` (needed for summaries) and `node` (the CLI is a Node app)

## Install

```bash
git clone https://github.com/junha6316/claude-code-recall
cd claude-code-recall
./install.sh
```

The installer auto-detects your `python3`, `claude`, and `node` paths, copies the
files into your Claude config dir (`$CLAUDE_CONFIG_DIR` if set, else `~/.claude`),
registers the ingestion hooks (`Stop` + `SessionStart`) and the recall hook into
`settings.json` (existing hooks are preserved), sets up the daily-summary jobs
(`launchd` on macOS, tagged `crontab` entries on Linux), and backfills the last
12h so the timeline isn't empty.

You can tune it at install time:

```bash
./install.sh --bucket-min 15 --debounce-min 5 --lang English
```

| Flag | Meaning | Default |
|---|---|---|
| `--bucket-min N` | Timeline bucket size (a divisor of 60) | `15` |
| `--debounce-min N` | Min minutes between hook-triggered scans | `bucket/3` (≥2) |
| `--lang LANG` | Language for summaries (`English`, `Korean`, …) | `English` |
| `--no-hook` | Don't register the recall-gate hook | hook on |
| `--yes` | Non-interactive (accept defaults) | prompts |

These are baked as env vars (`CCRECALL_BUCKET_MINUTES`, `CCRECALL_DEBOUNCE_MINUTES`,
`CCRECALL_SUMMARY_LANG`, `CCRECALL_CLAUDE_BIN`) into the hook command in
`settings.json`, so you can tweak them later by editing that entry. `--interval-min`
is still accepted as a deprecated alias for `--bucket-min`.

### Backfill past days

```bash
python3 ~/.claude/scripts/work-timeline.py --backfill 12   # last 12 hours
python3 ~/.claude/scripts/work-timeline.py --date 2026-06-25
```

## Updating

The installer registers a lightweight update checker on `SessionStart`. Once a day
it checks the latest GitHub **release** tag; when a newer one exists it tells you at
the start of a session, with a ready-to-run update command that keeps your original
install options (language, bucket size, etc.):

```bash
git -C ~/Projects/claude-code-recall pull && ~/Projects/claude-code-recall/install.sh
```

It **only notifies** — it never downloads or applies anything on its own, so remote
code is never executed without your action. Re-running `install.sh` is the update
(it's idempotent and backs up `settings.json` first). The check is network-only once
per day, fails open (a failed check never blocks a session), and stays quiet until
you publish a release, so it does nothing on `main`-only checkouts.

### Releasing (maintainers)

The version in `.claude-plugin/plugin.json` is the source of truth. To ship an update
to everyone: bump that `version`, commit, then tag and publish a matching release.

```bash
gh release create v0.2.0 --title v0.2.0 --generate-notes
```

Only tagged releases reach installs — pushing to `main` alone does not notify anyone.

## Using recall

Just ask Claude naturally — *"when did I work on X?"*, *"what was that error last
time?"* — and the hook runs `recall` for you. Or run it directly:

```bash
python3 ~/.claude/skills/recall/recall.py "fargate scaling"
python3 ~/.claude/skills/recall/recall.py "openssl" --raw --since 2026-06-01
```

## ⚠️ Privacy & secrets

The timeline contains **raw prompt text**, which can include tokens, passwords, and
other secrets/PII that appeared in your conversations. `~/.claude/work-timeline/` is
private to your machine. **Do not commit or sync it anywhere public.** This repo's
`.gitignore` already excludes timeline data, logs, and state files.

## Measuring search quality

`eval/run-eval.py` is a retrieval-quality regression harness: it harvests grounded
question/answer cases **from your own timeline** (via `claude -p`, four difficulty
tiers + negative controls), answers each blindly with the recall tool itself, and
judges the results against ground truth.

```bash
python3 eval/run-eval.py --harvest --run   # first time
python3 eval/run-eval.py                   # re-run after changes (regression)
```

Measured 2026-07-04 (96 cases, bilingual *Recall tags* in daily summaries):
**hit@1 96% · top-3 99% · false positives 0/12**. Cases live in
`eval/cases.local.json` (gitignored — they contain your private work data).

## Uninstall

```bash
./uninstall.sh           # removes jobs/scripts/hook; keeps your timeline data
./uninstall.sh --purge   # also deletes ~/.claude/work-timeline
```

## Roadmap

- **Multi-device support.** Today ingestion and recall are per-machine: each device
  builds its own timeline, threads, and registry from its local
  `~/.claude/projects`, so recall on one machine can't find work done on another.
  The goal is to merge timelines across a user's devices — stitching threads and
  reconciling the per-device cursor/registry — so recall covers all of them.
  Constraint: the timeline holds raw prompt text (secrets/PII), so any sync must be
  **private and user-controlled** (never a public remote); see *Privacy & secrets*.

## Notes

- **No daemon / zero idle cost:** ingestion runs from the `Stop` hook, so it only does
  work right after you actually use Claude Code — nothing runs when you're idle.
- **No added latency:** the hook returns immediately and processes in a detached worker,
  so it never blocks your session. Concurrent sessions are serialized by a file lock.
- **Cost:** each time bucket with activity costs one `claude -p` call. With 15-minute
  buckets that's up to ~4 calls per active hour. Increase `--bucket-min` to reduce calls.
  Note these calls run on **your Claude account** and count toward its rolling usage
  limits like any other session — if you routinely brush against your plan's limits,
  prefer a larger `--bucket-min` (e.g. 30).
- **TCC:** data is written under `~/.claude` (not a TCC-protected folder). To surface
  it in a notes app like Obsidian, symlink the folder into your vault rather than
  changing the output path.

## License

[MIT](./LICENSE)
