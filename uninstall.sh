#!/usr/bin/env bash
#
# claude-code-recall uninstaller (macOS).
#
# Removes the launchd jobs, the installed scripts/skill/hook, and the recall hook
# entry from settings.json. By default it KEEPS your timeline data.
#
# Usage:
#   ./uninstall.sh [--purge] [--yes]
#     --purge   Also delete the timeline data (~/.claude/work-timeline).
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

CLAUDE_DIR="$HOME/.claude"
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

for label in com.ccrecall.work-timeline com.ccrecall.work-timeline-rollup \
             com.ccrecall.work-timeline-threads com.ccrecall.work-timeline-consolidate; do
  launchctl unload "$LA_DIR/$label.plist" 2>/dev/null || true
  rm -f "$LA_DIR/$label.plist"
done
echo "launchd jobs removed."

rm -f "$SCRIPTS_DIR"/work-timeline.py "$SCRIPTS_DIR"/work-timeline-rollup.py \
      "$SCRIPTS_DIR"/work-timeline-threads.py "$SCRIPTS_DIR"/work-timeline-consolidate.py \
      "$SCRIPTS_DIR"/.work-timeline-state.json
rm -f "$HOOKS_DIR"/recall-gate.py
rm -rf "$SKILL_DIR"
echo "scripts / skill / hook removed."

# Remove the recall hook entry from settings.json (leave other hooks intact).
if [[ -f "$SETTINGS" ]]; then
  python3 - "$SETTINGS" <<'PYEOF' || true
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(0)
ups = data.get("hooks", {}).get("UserPromptSubmit", [])
new = []
for group in ups:
    hs = [h for h in group.get("hooks", []) if "recall-gate.py" not in (h.get("command") or "")]
    if hs:
        group["hooks"] = hs
        new.append(group)
if data.get("hooks", {}).get("UserPromptSubmit") is not None:
    data["hooks"]["UserPromptSubmit"] = new
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2); f.write("\n")
    print("  settings.json: recall hook removed.")
PYEOF
fi

if [[ $PURGE -eq 1 ]]; then
  rm -rf "$OUTPUT_DIR"
  echo "timeline data purged: $OUTPUT_DIR"
else
  echo "timeline data kept: $OUTPUT_DIR"
fi
echo "Done."
