#!/usr/bin/env bash
# gate-bash.sh — PreToolUse hook for the Bash tool.
#
# Classifies the command (enforcement/lib/classify_cmd.py) and either allows it,
# denies it, or — for a gate-able `git commit` — runs the full commit gate
# (enforcement/lib/commit_gate.sh). Fail-closed via common.sh's EXIT trap.
NS_HOOK_SELF="$0"
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "$0")" && pwd)/../lib/common.sh"
NS_GATE_NAME="bash"

INPUT="$(cat)"

# Validity gate: if the hook input is not parseable JSON we cannot know the
# command. In a night-shift repo that is fail-closed (block); elsewhere it is
# none of our business (passthrough). This bounds the blast radius of any future
# input-format change to repos that have actually been onboarded.
if ! printf '%s' "$INPUT" | python3 -c 'import json,sys; json.load(sys.stdin)' >/dev/null 2>&1; then
  CWD_FALLBACK="$PWD"
  if ns_resolve_repo "$CWD_FALLBACK"; then
    ns_block "night-shift gate: could not parse hook input in an enforced repo (fail-closed)."
  fi
  ns_passthrough
fi

CMD="$(ns_json_get "$INPUT" tool_input.command)"
MODE="$(ns_json_get "$INPUT" permission_mode)"
CWD="$(ns_json_get "$INPUT" cwd)"; [ -n "$CWD" ] || CWD="$PWD"

# Passthrough (not onboarded / interactive), fail-closed (onboarded but scope
# missing), or proceed (enforced + autonomous). The command is passed so a
# `cd`/`-C`/absolute path INTO a registered repo is resolved even when cwd is
# outside it (closes the cwd-escape).
ns_gate_guard "$CWD" "$MODE" "$CMD"

# Enforced context from here on. An empty command is anomalous → fail-closed.
[ -n "$CMD" ] || ns_block "night-shift gate: empty command in an enforced repo (fail-closed)."

VERDICT="$(python3 "$NS_LIB_DIR/classify_cmd.py" "$CMD" "$NS_SCOPE_PATH" 2>&1)" \
  || ns_block "$(printf 'night-shift gate: classifier error (fail-closed):\n%s' "$VERDICT")"

KIND="$(printf '%s' "$VERDICT" | head -1 | cut -f1)"
REASON="$(printf '%s' "$VERDICT" | head -1 | cut -f2-)"

case "$KIND" in
  PASS)
    ns_allow ;;
  DENY)
    ns_log "deny: $REASON :: $CMD"
    ns_block "night-shift gate: $REASON" ;;
  COMMIT)
    # shellcheck source=../lib/commit_gate.sh
    source "$NS_LIB_DIR/commit_gate.sh"
    ns_run_commit_gate ;;
  *)
    ns_block "$(printf 'night-shift gate: unrecognized classifier verdict (fail-closed):\n%s' "$VERDICT")" ;;
esac

# Should be unreachable — every branch above exits.
ns_block "night-shift gate: fell through classifier (fail-closed)."
