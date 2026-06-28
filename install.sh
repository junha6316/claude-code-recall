#!/usr/bin/env bash
#
# claude-code-recall installer (macOS).
#
# Installs the recall skill, the work-timeline ingestion hooks (Stop + SessionStart),
# the UserPromptSubmit recall-gate hook, and the daily synthesis jobs (launchd).
# Re-running is safe (idempotent).
#
# Usage:
#   ./install.sh [--bucket-min N] [--debounce-min N] [--lang LANG] [--no-hook] [--yes]
#
#   --bucket-min N     Timeline bucket size in minutes (a divisor of 60). Default 15.
#   --debounce-min N   Min minutes between hook-triggered scans. Default = bucket/3 (>=2).
#   --lang LANG        Language for LLM summaries (e.g. English, Korean). Default English.
#   --no-hook          Do not register the recall-gate UserPromptSubmit hook in settings.json.
#   --yes, -y          Non-interactive: accept defaults, don't prompt.
#   --help, -h         Show this help.
#
set -euo pipefail

INTERVAL_MIN=""   # deprecated alias for --bucket-min (kept for back-compat)
BUCKET_MIN=""
DEBOUNCE_MIN=""
SUMMARY_LANG="English"
ENABLE_HOOK=1
ASSUME_YES=0

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval-min) INTERVAL_MIN="$2"; shift 2 ;;
    --bucket-min)   BUCKET_MIN="$2";   shift 2 ;;
    --debounce-min) DEBOUNCE_MIN="$2"; shift 2 ;;
    --lang)         SUMMARY_LANG="$2"; shift 2 ;;
    --no-hook)      ENABLE_HOOK=0;     shift ;;
    --yes|-y)       ASSUME_YES=1;      shift ;;
    --help|-h)      usage ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ "$(uname)" == "Darwin" ]] || { echo "Error: this installer supports macOS only (v1)." >&2; exit 1; }

# --- bucket size: flag, else --interval-min (back-compat), else prompt, else 15 ---
if [[ -z "$BUCKET_MIN" ]]; then
  BUCKET_MIN="$INTERVAL_MIN"
  if [[ -z "$BUCKET_MIN" && $ASSUME_YES -eq 0 && -t 0 ]]; then
    read -r -p "Timeline bucket size in minutes (divisor of 60) [15]: " BUCKET_MIN || true
  fi
  BUCKET_MIN="${BUCKET_MIN:-15}"
fi
[[ "$BUCKET_MIN" =~ ^[0-9]+$ && "$BUCKET_MIN" -ge 1 && "$BUCKET_MIN" -le 60 ]] || { echo "Error: --bucket-min must be 1..60." >&2; exit 2; }
if [[ $(( 60 % BUCKET_MIN )) -ne 0 ]]; then
  echo "Warning: bucket size $BUCKET_MIN is not a divisor of 60; buckets won't align to the clock." >&2
fi

# --- debounce: flag, else bucket/3 (min 2) ---
if [[ -z "$DEBOUNCE_MIN" ]]; then
  DEBOUNCE_MIN=$(( BUCKET_MIN / 3 ))
  [[ "$DEBOUNCE_MIN" -lt 2 ]] && DEBOUNCE_MIN=2
fi
[[ "$DEBOUNCE_MIN" =~ ^[0-9]+$ && "$DEBOUNCE_MIN" -ge 1 ]] || { echo "Error: --debounce-min must be a positive integer." >&2; exit 2; }

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
echo "  ingestion     : Stop + SessionStart hooks (event-driven, no daemon)"
echo "  bucket size   : ${BUCKET_MIN} min"
echo "  debounce      : ${DEBOUNCE_MIN} min"
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

# Ingestion no longer polls via launchd — it runs from the Stop/SessionStart hooks
# below. Remove the old polling job if a previous version installed it.
launchctl unload "$LA_DIR/com.ccrecall.work-timeline.plist" 2>/dev/null || true
rm -f "$LA_DIR/com.ccrecall.work-timeline.plist"

# Daily synthesis jobs stay on launchd (run once a day, off the hot path).
emit_plist "com.ccrecall.work-timeline-rollup"      "work-timeline-rollup.py"      "$(cal_xml 30)"
emit_plist "com.ccrecall.work-timeline-threads"     "work-timeline-threads.py"     "$(cal_xml 40)"
emit_plist "com.ccrecall.work-timeline-consolidate" "work-timeline-consolidate.py" "$(cal_xml 50)"

# --- (re)load launchd jobs ---
for label in com.ccrecall.work-timeline-rollup \
             com.ccrecall.work-timeline-threads com.ccrecall.work-timeline-consolidate; do
  launchctl unload "$LA_DIR/$label.plist" 2>/dev/null || true
  launchctl load   "$LA_DIR/$label.plist"
done
echo "launchd daily jobs loaded."

# --- merge hooks into settings.json (preserves existing hooks) ---
# Ingestion runs from Stop + SessionStart (async = non-blocking). The recall-gate
# UserPromptSubmit hook is added unless --no-hook. Config is baked into the command
# (hooks don't inherit the launchd plist env).
# Single-quote the free-text --lang value so a multi-word language (e.g. "Brazilian
# Portuguese") doesn't split into a stray command when Claude Code runs the hook.
# (PY / SCRIPTS_DIR live under fixed space-free paths and are left unquoted so the
# "work-timeline.py --hook" idempotency/uninstall matcher keeps matching.)
HOOK_ENV="CCRECALL_BUCKET_MINUTES=$BUCKET_MIN CCRECALL_SUMMARY_LANG='$SUMMARY_LANG' CCRECALL_DEBOUNCE_MINUTES=$DEBOUNCE_MIN"
[[ -n "$CLAUDE_BIN" ]] && HOOK_ENV="$HOOK_ENV CCRECALL_CLAUDE_BIN='$CLAUDE_BIN'"
TICK_CMD="$HOOK_ENV $PY $SCRIPTS_DIR/work-timeline.py --hook"
RECALL_CMD=""
[[ $ENABLE_HOOK -eq 1 ]] && RECALL_CMD="python3 ~/.claude/hooks/recall-gate.py 2>/dev/null || true"

[[ -f "$SETTINGS" ]] && cp "$SETTINGS" "$SETTINGS.bak.$(date +%s 2>/dev/null || echo bak)" 2>/dev/null || true
"$PY" - "$SETTINGS" "$TICK_CMD" "$RECALL_CMD" <<'PYEOF'
import json, sys
path, tick_cmd, recall_cmd = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    data = {}
hooks = data.setdefault("hooks", {})

def has(event, needle):
    for group in hooks.get(event, []):
        if isinstance(group, dict):
            for h in group.get("hooks", []):
                if needle in (h.get("command") or ""):
                    return True
    return False

changed = False
for event in ("Stop", "SessionStart"):
    arr = hooks.setdefault(event, [])
    if not has(event, "work-timeline.py --hook"):
        arr.append({"hooks": [{"type": "command", "command": tick_cmd, "async": True}]})
        changed = True

if recall_cmd:
    ups = hooks.setdefault("UserPromptSubmit", [])
    if not has("UserPromptSubmit", "recall-gate.py"):
        ups.append({"hooks": [{"type": "command", "command": recall_cmd, "timeout": 25,
                               "statusMessage": "recall: checking past work..."}]})
        changed = True

if changed:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("  settings.json: hooks registered (Stop, SessionStart%s)."
          % (", UserPromptSubmit" if recall_cmd else ""))
else:
    print("  settings.json: hooks already present (skipped).")
PYEOF

# --- backfill recent activity so the timeline isn't empty on first use (no LLM = fast/free) ---
echo "Backfilling the last 12h (no LLM)…"
CCRECALL_BUCKET_MINUTES="$BUCKET_MIN" "$PY" "$SCRIPTS_DIR/work-timeline.py" --backfill 12 --no-llm || true

cat <<EOF

Done.

  • Timeline output : $OUTPUT_DIR/YYYY-MM-DD.md
  • Ingestion       : runs automatically on every turn (Stop hook) + session start
  • Recall manually : $PY $SKILL_DIR/recall.py "<terms>"
  • Logs            : $LOG

⚠️  Your timeline records raw prompt text, which may contain secrets (tokens,
    passwords, PII). $OUTPUT_DIR is private to your machine — do NOT commit it
    or sync it anywhere public.

EOF
