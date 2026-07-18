#!/usr/bin/env bash
# Daily synthesis (rollup -> threads -> consolidate), hook-driven.
#
# Replaces the launchd/cron daily jobs for the plugin install: the SessionStart
# hook calls this on every session, and a date stamp makes it run at most once
# per calendar day (first session of the day). The three scripts are cursor-
# based and process completed days, so running at 09:00 instead of 00:30 only
# delays, never skips.
#
# The stamp is written BEFORE running (at-most-once-per-day, like launchd's
# calendar trigger): a failed run is retried tomorrow, not on every session.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
STAMP="$CLAUDE_DIR/scripts/.ccrecall-daily-stamp"
LOG="$CLAUDE_DIR/scripts/work-timeline.log"
TODAY="$(date +%Y-%m-%d)"

[ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$TODAY" ] && exit 0
mkdir -p "$CLAUDE_DIR/scripts"
echo "$TODAY" > "$STAMP"

# macOS: prefer the Command Line Tools python to avoid a TCC re-exec prompt.
PY="/Library/Developer/CommandLineTools/usr/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)" || exit 0

{
  echo "[daily-synthesis] $TODAY start"
  "$PY" "$ROOT/scripts/work-timeline-rollup.py"
  "$PY" "$ROOT/scripts/work-timeline-threads.py"
  "$PY" "$ROOT/scripts/work-timeline-consolidate.py"
  echo "[daily-synthesis] $TODAY done"
} >> "$LOG" 2>&1
