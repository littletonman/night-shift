#!/usr/bin/env bash
# gate-write.sh — PreToolUse hook for file-writing tools (Edit|Write|MultiEdit|
# NotebookEdit). Fast, exact scope enforcement on the target path. This is
# execution point 1 of the scope contract; the commit gate's staged-scope check
# is the hard backstop. Fail-closed via common.sh's EXIT trap.
#
# The enforced repo is resolved from the TARGET FILE (not the session cwd), and
# the path is realpath-resolved, so neither a cwd outside the repo nor a
# symlinked directory inside an allowed path can escape the scope check.
NS_HOOK_SELF="$0"
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "$0")" && pwd)/../lib/common.sh"
NS_GATE_NAME="write"

INPUT="$(cat)"

if ! printf '%s' "$INPUT" | python3 -c 'import json,sys; json.load(sys.stdin)' >/dev/null 2>&1; then
  if ns_resolve_repo "$PWD" && [ "$NS_RESOLVE" != "none" ]; then
    ns_block "night-shift gate: could not parse hook input in an enforced repo (fail-closed)."
  fi
  ns_passthrough
fi

FILE="$(ns_json_get "$INPUT" tool_input.file_path)"
[ -n "$FILE" ] || FILE="$(ns_json_get "$INPUT" tool_input.notebook_path)"
MODE="$(ns_json_get "$INPUT" permission_mode)"
CWD="$(ns_json_get "$INPUT" cwd)"; [ -n "$CWD" ] || CWD="$PWD"

# Absolute (lexical) target path.
ABS="$(python3 -c '
import os,sys
f,cwd=sys.argv[1],sys.argv[2]
sys.stdout.write(f if os.path.isabs(f) else os.path.normpath(os.path.join(cwd,f)))
' "$FILE" "$CWD" 2>/dev/null)" || ABS=""
[ -n "$ABS" ] || { ns_gate_guard "$CWD" "$MODE"; ns_block "night-shift gate: file tool with no resolvable path in an enforced repo (fail-closed)."; }

# Resolve enforcement from BOTH the session cwd AND the target path, so a write
# into a registered repo is gated even when cwd is elsewhere. Then require
# autonomy. The realpath escape check below catches symlink redirection.
ns_gate_guard "$CWD" "$MODE" "" "$ABS"

# Realpath-resolve BOTH the repo root and the target (resolving symlinks in the
# target's existing ancestors) so a symlinked directory cannot redirect the
# write out of the repo or into a deny path. Then compute the repo-relative path.
REL="$(python3 -c '
import os,sys
root=os.path.realpath(sys.argv[1])
target=os.path.realpath(sys.argv[2])   # resolves symlinks in the existing prefix
sys.stdout.write(os.path.relpath(target, root))
' "$NS_REPO_ROOT" "$ABS" 2>/dev/null)" \
  || ns_block "night-shift gate: could not resolve real path of $FILE (fail-closed)."

case "$REL" in
  ../*|..) ns_block "night-shift gate: the write resolves outside the repo root ($FILE -> $REL); a symlink may be redirecting it. Denied." ;;
esac

SCOPE_ERR="$(python3 "$NS_SCOPE_MATCH" "$NS_SCOPE_PATH" --path "$REL" 2>&1)" || {
  ns_log "deny write: $REL"
  ns_block "$(printf 'night-shift gate: %s is out of scope.\n%s\nThe operator controls scope.yaml (allow_paths / deny_paths).' "$REL" "$SCOPE_ERR")"
}

ns_allow
