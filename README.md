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
| `work-timeline.py` | Every *N* minutes, scans transcripts, groups sessions into fixed time buckets, and writes one short LLM summary per bucket to `~/.claude/work-timeline/YYYY-MM-DD.md`. Incremental & idempotent; catches up after sleep. |
| `work-timeline-rollup.py` | Once a day, prepends a 🧠 *Daily Summary* to that day's file. |
| `work-timeline-threads.py` | Stitches related work across days into "threads". |
| `work-timeline-consolidate.py` | Consolidates/cleans the accumulated logs. |
| `skills/recall` | A Claude Code **skill**: searches the timeline first, drills into raw transcripts only when an exact phrase/error is needed. |
| `recall-gate.py` | A `UserPromptSubmit` hook that auto-runs `recall` when your prompt looks like a recall question (English + Korean triggers) and injects the result into context. |

The timeline and the recall skill use the **`claude` CLI** (`claude -p`) for summaries,
so summarization runs on your own Claude account.

## Requirements

- **macOS** (scheduling uses `launchd`; this is the only platform supported in v1)
- `python3` (the macOS Command Line Tools python is used if present)
- The **`claude` CLI** on your `PATH` (needed for summaries) and `node` (the CLI is a Node app)

## Install

```bash
git clone https://github.com/junha6316/claude-code-recall
cd claude-code-recall
./install.sh
```

The installer auto-detects your `python3`, `claude`, and `node` paths, copies the
files into `~/.claude/`, registers four `launchd` jobs, and merges the recall hook
into `~/.claude/settings.json` (existing hooks are preserved).

You can set the cadence at install time:

```bash
./install.sh --interval-min 15 --bucket-min 15 --lang English
```

| Flag | Meaning | Default |
|---|---|---|
| `--interval-min N` | How often the scanner runs (minutes) | `15` |
| `--bucket-min N` | Timeline bucket size (a divisor of 60) | = `--interval-min` |
| `--lang LANG` | Language for summaries (`English`, `Korean`, …) | `English` |
| `--no-hook` | Don't register the recall-gate hook | hook on |
| `--yes` | Non-interactive (accept defaults) | prompts |

These map to env vars (`CCRECALL_BUCKET_MINUTES`, `CCRECALL_SUMMARY_LANG`,
`CCRECALL_CLAUDE_BIN`) baked into the launchd jobs, so you can also tweak them later
by editing the plists in `~/Library/LaunchAgents/com.ccrecall.*`.

### Backfill past days

```bash
python3 ~/.claude/scripts/work-timeline.py --backfill 12   # last 12 hours
python3 ~/.claude/scripts/work-timeline.py --date 2026-06-25
```

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

## Uninstall

```bash
./uninstall.sh           # removes jobs/scripts/hook; keeps your timeline data
./uninstall.sh --purge   # also deletes ~/.claude/work-timeline
```

## Notes

- **Cost:** each time bucket with activity costs one `claude -p` call. With 15-minute
  buckets that's up to ~4 calls per active hour. Increase `--bucket-min`/`--interval-min`
  to reduce calls.
- **TCC:** data is written under `~/.claude` (not a TCC-protected folder). To surface
  it in a notes app like Obsidian, symlink the folder into your vault rather than
  changing the output path.

## License

[MIT](./LICENSE)
