#!/usr/bin/env bash
#
# claude-code-recall installer (macOS).
#
# Installs the work-timeline background jobs (launchd), the recall skill, and the
# UserPromptSubmit recall-gate hook. Re-running is safe (idempotent).
#
# Usage:
#   ./install.sh [--interval-min N] [--bucket-min N] [--lang LANG] [--no-hook] [--yes]
#
#   --interval-min N   How often the scanner runs, in minutes (launchd StartInterval). Default 15.
#   --bucket-min N     Timeline bucket size in minutes (a divisor of 60). Default = --interval-min.
#   --lang LANG        Language for LLM summaries (e.g. English, Korean). Default English.
#   --no-hook          Do not register the recall-gate UserPromptSubmit hook in settings.json.
#   --yes, -y          Non-interactive: accept defaults, don't prompt.
#   --help, -h         Show this help.
#
set -euo pipefail

INTERVAL_MIN=""
BUCKET_MIN=""
SUMMARY_LANG="English"
ENABLE_HOOK=1
ASSUME_YES=0

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval-min) INTERVAL_MIN="$2"; shift 2 ;;
    --bucket-min)   BUCKET_MIN="$2";   shift 2 ;;
    --lang)         SUMMARY_LANG="$2"; shift 2 ;;
    --no-hook)      ENABLE_HOOK=0;     shift ;;
    --yes|-y)       ASSUME_YES=1;      shift ;;
    --help|-h)      usage ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ "$(uname)" == "Darwin" ]] || { echo "Error: this installer supports macOS only (v1)." >&2; exit 1; }

# --- scan interval: flag, else prompt (if TTY), else default 15 ---
if [[ -z "$INTERVAL_MIN" ]]; then
  if [[ $ASSUME_YES -eq 0 && -t 0 ]]; then
    read -r -p "Scan interval in minutes [15]: " INTERVAL_MIN || true
  fi
  INTERVAL_MIN="${INTERVAL_MIN:-15}"
fi
[[ "$INTERVAL_MIN" =~ ^[0-9]+$ && "$INTERVAL_MIN" -ge 1 ]] || { echo "Error: --interval-min must be a positive integer." >&2; exit 2; }

# --- bucket size: flag, else prompt (if TTY), else = interval ---
if [[ -z "$BUCKET_MIN" ]]; then
  if [[ $ASSUME_YES -eq 0 && -t 0 ]]; then
    read -r -p "Timeline bucket size in minutes (divisor of 60) [$INTERVAL_MIN]: " BUCKET_MIN || true
  fi
  BUCKET_MIN="${BUCKET_MIN:-$INTERVAL_MIN}"
fi
[[ "$BUCKET_MIN" =~ ^[0-9]+$ && "$BUCKET_MIN" -ge 1 && "$BUCKET_MIN" -le 60 ]] || { echo "Error: --bucket-min must be 1..60." >&2; exit 2; }
if [[ $(( 60 % BUCKET_MIN )) -ne 0 ]]; then
  echo "Warning: bucket size $BUCKET_MIN is not a divisor of 60; buckets won't align to the clock." >&2
fi

INTERVAL_SEC=$(( INTERVAL_MIN * 60 ))

# --- paths ---
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
SKILL_DIR="$CLAUDE_DIR/skills/recall"
HOOKS_DIR="$CLAUDE_DIR/hooks"
OUTPUT_DIR="$CLAUDE_DIR/work-timeline"
LOG="$SCRIPTS_DIR/work-timeline.log"
LA_DIR="$HOME/Library/LaunchAgents"
SETTINGS="$CLAUDE_DIR/settings.json"

# --- detect python (prefer Command Line Tools python to avoid a TCC re-exec) ---
if [[ -x /Library/Developer/CommandLineTools/usr/bin/python3 ]]; then
  PY="/Library/Developer/CommandLineTools/usr/bin/python3"
else
  PY="$(command -v python3 || true)"
fi
[[ -n "$PY" ]] || { echo "Error: python3 not found." >&2; exit 1; }

# --- detect claude CLI (summaries need it) and node (claude is a node CLI) ---
CLAUDE_BIN="$(command -v claude || true)"
[[ -z "$CLAUDE_BIN" && -x "$HOME/.local/bin/claude" ]] && CLAUDE_BIN="$HOME/.local/bin/claude"
if [[ -z "$CLAUDE_BIN" ]]; then
  echo "Warning: 'claude' CLI not found. The timeline will still record prompts, but LLM summaries"
  echo "         will fail until claude is on PATH. Set CCRECALL_CLAUDE_BIN in the plists if needed." >&2
fi
NODE_BIN=""
NODE="$(command -v node || true)"; [[ -n "$NODE" ]] && NODE_BIN="$(dirname "$NODE")"
CLAUDE_DIRBIN=""; [[ -n "$CLAUDE_BIN" ]] && CLAUDE_DIRBIN="$(dirname "$CLAUDE_BIN")"
PATH_ENV="${NODE_BIN:+$NODE_BIN:}${CLAUDE_DIRBIN:+$CLAUDE_DIRBIN:}/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

echo "Installing claude-code-recall:"
echo "  scan interval : ${INTERVAL_MIN} min (StartInterval=${INTERVAL_SEC}s)"
echo "  bucket size   : ${BUCKET_MIN} min"
echo "  summary lang  : ${SUMMARY_LANG}"
echo "  python        : ${PY}"
echo "  claude        : ${CLAUDE_BIN:-<not found>}"
echo "  recall hook   : $([[ $ENABLE_HOOK -eq 1 ]] && echo enabled || echo disabled)"

# --- copy files into ~/.claude ---
mkdir -p "$SCRIPTS_DIR" "$SKILL_DIR" "$HOOKS_DIR" "$OUTPUT_DIR" "$LA_DIR"
cp "$REPO_DIR"/scripts/work-timeline.py \
   "$REPO_DIR"/scripts/work-timeline-rollup.py \
   "$REPO_DIR"/scripts/work-timeline-threads.py \
   "$REPO_DIR"/scripts/work-timeline-consolidate.py "$SCRIPTS_DIR"/
cp "$REPO_DIR"/scripts/recall-gate.py "$HOOKS_DIR"/
cp "$REPO_DIR"/skills/recall/recall.py "$REPO_DIR"/skills/recall/SKILL.md "$SKILL_DIR"/

# --- generate launchd plists ---
emit_plist() {  # $1=label  $2=script  $3=schedule-xml
  cat > "$LA_DIR/$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$1</string>
	<key>ProgramArguments</key>
	<array>
		<string>$PY</string>
		<string>$SCRIPTS_DIR/$2</string>
	</array>
	<key>RunAtLoad</key>
	<false/>
	<key>StandardOutPath</key>
	<string>$LOG</string>
	<key>StandardErrorPath</key>
	<string>$LOG</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>HOME</key><string>$HOME</string>
		<key>USER</key><string>$USER</string>
		<key>LOGNAME</key><string>$USER</string>
		<key>PATH</key><string>$PATH_ENV</string>
		<key>CCRECALL_CLAUDE_BIN</key><string>${CLAUDE_BIN:-}</string>
		<key>CCRECALL_SUMMARY_LANG</key><string>$SUMMARY_LANG</string>
		<key>CCRECALL_BUCKET_MINUTES</key><string>$BUCKET_MIN</string>
	</dict>
$3
</dict>
</plist>
EOF
}

cal_xml() { printf '\t<key>StartCalendarInterval</key>\n\t<dict>\n\t\t<key>Hour</key><integer>0</integer>\n\t\t<key>Minute</key><integer>%s</integer>\n\t</dict>' "$1"; }

emit_plist "com.ccrecall.work-timeline"             "work-timeline.py"             "$(printf '\t<key>StartInterval</key>\n\t<integer>%s</integer>' "$INTERVAL_SEC")"
emit_plist "com.ccrecall.work-timeline-rollup"      "work-timeline-rollup.py"      "$(cal_xml 30)"
emit_plist "com.ccrecall.work-timeline-threads"     "work-timeline-threads.py"     "$(cal_xml 40)"
emit_plist "com.ccrecall.work-timeline-consolidate" "work-timeline-consolidate.py" "$(cal_xml 50)"

# --- (re)load launchd jobs ---
for label in com.ccrecall.work-timeline com.ccrecall.work-timeline-rollup \
             com.ccrecall.work-timeline-threads com.ccrecall.work-timeline-consolidate; do
  launchctl unload "$LA_DIR/$label.plist" 2>/dev/null || true
  launchctl load   "$LA_DIR/$label.plist"
done
echo "launchd jobs loaded."

# --- merge the recall hook into settings.json (preserves existing hooks) ---
if [[ $ENABLE_HOOK -eq 1 ]]; then
  [[ -f "$SETTINGS" ]] && cp "$SETTINGS" "$SETTINGS.bak.$(date +%s 2>/dev/null || echo bak)" 2>/dev/null || true
  "$PY" - "$SETTINGS" <<'PYEOF'
import json, os, sys
path = sys.argv[1]
cmd = "python3 ~/.claude/hooks/recall-gate.py 2>/dev/null || true"
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    data = {}
hooks = data.setdefault("hooks", {})
ups = hooks.setdefault("UserPromptSubmit", [])
def has_recall(ups):
    for group in ups:
        for h in group.get("hooks", []) if isinstance(group, dict) else []:
            if "recall-gate.py" in (h.get("command") or ""):
                return True
    return False
if not has_recall(ups):
    ups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 25,
                           "statusMessage": "recall: checking past work..."}]})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("  settings.json: recall hook added.")
else:
    print("  settings.json: recall hook already present (skipped).")
PYEOF
fi

cat <<EOF

Done.

  • Timeline output : $OUTPUT_DIR/YYYY-MM-DD.md
  • Backfill now    : $PY $SCRIPTS_DIR/work-timeline.py --backfill 12
  • Recall manually : $PY $SKILL_DIR/recall.py "<terms>"
  • Logs            : $LOG

⚠️  Your timeline records raw prompt text, which may contain secrets (tokens,
    passwords, PII). $OUTPUT_DIR is private to your machine — do NOT commit it
    or sync it anywhere public.

EOF
