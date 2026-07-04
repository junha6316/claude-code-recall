#!/usr/bin/env bash
#
# claude-code-recall uninstaller (macOS & Linux).
#
# Removes the daily jobs (launchd on macOS, cron on Linux), the installed
# scripts/skill/hook, and the recall hook entry from settings.json.
# Honors CLAUDE_CONFIG_DIR (default ~/.claude). By default it KEEPS your timeline data.
#
# Usage:
#   ./uninstall.sh [--purge] [--yes]
#     --purge   Also delete the timeline data ($CONFIG/work-timeline).
#     --yes,-y  Don't prompt for confirmation.
#
set -euo pipefail

PURGE=0; ASSUME_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
SKILL_DIR="$CLAUDE_DIR/skills/recall"
HOOKS_DIR="$CLAUDE_DIR/hooks"
OUTPUT_DIR="$CLAUDE_DIR/work-timeline"
LA_DIR="$HOME/Library/LaunchAgents"
SETTINGS="$CLAUDE_DIR/settings.json"

if [[ $ASSUME_YES -eq 0 && -t 0 ]]; then
  read -r -p "Remove claude-code-recall? (timeline data is kept unless --purge) [y/N] " ans || true
  [[ "${ans:-}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

if [[ "$(uname)" == "Darwin" ]]; then
  for label in com.ccrecall.work-timeline com.ccrecall.work-timeline-rollup \
               com.ccrecall.work-timeline-threads com.ccrecall.work-timeline-consolidate; do
    launchctl unload "$LA_DIR/$label.plist" 2>/dev/null || true
    rm -f "$LA_DIR/$label.plist"
  done
  echo "launchd jobs removed."
elif command -v crontab >/dev/null; then
  CRON_TMP="$(mktemp)"
  ( crontab -l 2>/dev/null | grep -v '# ccrecall$' ) > "$CRON_TMP" || true
  crontab "$CRON_TMP" 2>/dev/null || true
  rm -f "$CRON_TMP"
  echo "cron jobs removed."
fi

rm -f "$SCRIPTS_DIR"/work-timeline.py "$SCRIPTS_DIR"/work-timeline-rollup.py \
      "$SCRIPTS_DIR"/work-timeline-threads.py "$SCRIPTS_DIR"/work-timeline-consolidate.py \
      "$SCRIPTS_DIR"/.work-timeline-state.json "$SCRIPTS_DIR"/.work-timeline.lock \
      "$SCRIPTS_DIR"/.work-timeline-state.json.lock "$SCRIPTS_DIR"/.work-timeline-state.json.tmp
rm -f "$HOOKS_DIR"/recall-gate.py
rm -rf "$SKILL_DIR"
echo "scripts / skill / hook removed."

# Remove our hook entries from settings.json (recall-gate + ingestion ticks), leave
# other hooks intact. A hook group is dropped only if it has no hooks left.
if [[ -f "$SETTINGS" ]]; then
  python3 - "$SETTINGS" <<'PYEOF' || true
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(0)
hooks = data.get("hooks")
if not isinstance(hooks, dict):
    sys.exit(0)
OURS = ("recall-gate.py", "work-timeline.py --hook")
changed = False
for event in ("UserPromptSubmit", "Stop", "SessionStart"):
    groups = hooks.get(event)
    if groups is None:
        continue
    new = []
    for group in groups:
        hs = [h for h in group.get("hooks", []) if not any(o in (h.get("command") or "") for o in OURS)]
        if hs:
            group["hooks"] = hs
            new.append(group)
    hooks[event] = new
    changed = True
if changed:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2); f.write("\n")
    print("  settings.json: claude-code-recall hooks removed.")
PYEOF
fi

if [[ $PURGE -eq 1 ]]; then
  rm -rf "$OUTPUT_DIR"
  echo "timeline data purged: $OUTPUT_DIR"
else
  echo "timeline data kept: $OUTPUT_DIR"
fi
echo "Done."
