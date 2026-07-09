#!/usr/bin/env bash
#
# claude-code-recall update checker (notify-only).
#
# Two modes, both meant to run from the SessionStart hook:
#   --refresh   (default) Throttled to at most once per calendar day: query the
#               latest GitHub release tag and cache it to .ccrecall-latest.
#               This is the ONLY mode that touches the network; run it async.
#   --notify    Instant, no network. If the cached latest version is newer than
#               the installed version (.ccrecall-version), print a SessionStart
#               additionalContext notice so the user is told an update exists,
#               with an update command that preserves their install options.
#
# Notify-only by design: it never downloads or applies anything. Updating is a
# manual, user-invoked step (re-run install.sh) so remote code is never executed
# on the user's machine without their action.
#
# Honors CLAUDE_CONFIG_DIR (default ~/.claude). Fail-open: any error exits 0 so a
# session is never blocked.
set -uo pipefail

REPO="junha6316/claude-code-recall"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"

refresh() {
  local stamp today tag
  stamp="$SCRIPTS_DIR/.ccrecall-update-check"
  today="$(date +%Y-%m-%d 2>/dev/null)" || return 0
  [ "$(cat "$stamp" 2>/dev/null)" = "$today" ] && return 0
  command -v curl >/dev/null 2>&1 || return 0
  # Record the attempt BEFORE the network call so a failing/hanging check still
  # throttles to once a day instead of retrying every session.
  printf '%s\n' "$today" > "$stamp" 2>/dev/null || true
  tag="$(curl -fsSL --max-time 3 \
      "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
      | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      | head -1)"
  [ -n "$tag" ] && printf '%s\n' "$tag" > "$SCRIPTS_DIR/.ccrecall-latest" 2>/dev/null
  return 0
}

notify() {
  python3 - "$SCRIPTS_DIR" "$REPO" <<'PY' 2>/dev/null || true
import json, os, shlex, sys
scripts, repo = sys.argv[1], sys.argv[2]

def read(name):
    try:
        with open(os.path.join(scripts, name), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

installed, latest = read(".ccrecall-version"), read(".ccrecall-latest")
if not installed or not latest:
    sys.exit(0)

def parse(v):
    out = []
    for p in v.lstrip("v").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return out

if parse(latest) <= parse(installed):
    sys.exit(0)

# Rebuild the user's original install flags so the update command keeps their
# language / bucket / hook choices instead of silently resetting to defaults.
cfg = {}
try:
    with open(os.path.join(scripts, ".ccrecall-config"), encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                k, v = line.rstrip("\n").split("=", 1)
                cfg[k] = v
except Exception:
    pass
flags = ""
if cfg.get("SUMMARY_LANG"):
    # Quote so a multi-word language (e.g. "Brazilian Portuguese") stays a single
    # argument in the copy-pasted update command instead of splitting.
    flags += " --lang %s" % shlex.quote(cfg["SUMMARY_LANG"])
if cfg.get("BUCKET_MIN"):
    flags += " --bucket-min %s" % cfg["BUCKET_MIN"]
if cfg.get("ENABLE_HOOK") == "0":
    flags += " --no-hook"

tag = latest.lstrip("v")
msg = (
    "At the very start of your next reply, tell the user on one line (then "
    "continue normally): claude-code-recall %s is available (installed %s). "
    "To update, re-run the installer from an up-to-date checkout: "
    "`git -C ~/Projects/claude-code-recall pull && "
    "~/Projects/claude-code-recall/install.sh%s`. Without a local checkout: "
    "`curl -fsSL https://github.com/%s/archive/refs/tags/%s.tar.gz | tar xz && "
    "claude-code-recall-%s/install.sh%s`."
) % (latest, installed, flags, repo, latest, tag, flags)

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart", "additionalContext": msg}}, ensure_ascii=False))
PY
}

case "${1:-}" in
  --notify) notify ;;
  --refresh|"") refresh ;;
  *) ;;
esac
exit 0
