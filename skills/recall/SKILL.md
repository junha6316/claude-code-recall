---
name: recall
description: |
  Search past Claude Code work and conversations to recall "when did I do what."
  Use for recall-style questions — "when did I do X", "remember when we did X",
  "what was that error", "how did I handle X last time", "the thing I worked on
  earlier". Search the curated daily timeline first; drill down into the raw
  conversations only when you need an exact phrase, code snippet, or error message.
allowed-tools:
  - Bash
  - Read
---

# recall — search past work

Find "when / in which project / what you did" across past Claude Code sessions.
Two layers are searched:

- **Timeline** (`~/.claude/work-timeline/*.md`) — curated daily summaries + per-bucket prompts. Clean and small. **Default search target.**
- **Raw conversations** (`~/.claude/projects/**/*.jsonl`) — the exact messages/code/errors exchanged that day. Only when you need the detail.

## Procedure

### Step 1 — search the timeline (the answer is almost always here)

Pull **1–3 key terms** from the user's question. Drop particles and pronouns; keep
the distinctive tokens (service / project / error / tool names, e.g. `fargate`,
`openssl`, `alert`).

```
python3 ~/.claude/skills/recall/recall.py "<terms>"
```

- Separate multiple terms with spaces. Blocks that match more of the terms sort to the top (partial matches still show).
- Use the result's `[date] section` + matched prompts to tell the user **when / which project / what happened**.
- If nothing lands, retry with different terms (synonyms, or the other language).

### Step 2 — raw drill-down (only when an exact phrase/code/error is needed)

After Step 1 gives you a date, narrow to that range and search the raw transcripts:

```
python3 ~/.claude/skills/recall/recall.py "<terms>" --raw --since YYYY-MM-DD [--until YYYY-MM-DD] [--project NAME]
```

- `--raw` returns only messages that contain **all** of the terms (AND). Don't pass too many terms.
- Narrowing with `--since` is fast (avoids scanning the entire raw history).
- For deeper context, open the `.jsonl` path printed in the results with **Read** to see the surrounding session.

## Answering principles

- Report only what you found (date / project / what was done); don't invent anything that isn't in the logs.
- If you can't find it, say so plainly ("not in the timeline"), and suggest the terms you tried and a next step (raw search / different terms).
- The timeline only covers dates that have been backfilled or accumulated since install. For earlier dates, search the raw transcripts with `--raw`. New sessions are appended automatically on a schedule (default every 15 minutes) via launchd.
