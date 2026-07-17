#!/usr/bin/env bash
# gate-config.sh — ConfigChange hook. Defense-in-depth: while an autonomous
# shift is enforced, block the agent from mutating Claude Code settings
# (hooks / permissions / disableAllHooks live there). The managed-settings
# allowManagedHooksOnly flag already neutralizes user/project/local hooks, so
# this is a redundant, belt-and-suspenders layer — but it turns a silent
# settings edit into a visible block. Fail-closed via common.sh's EXIT trap.
NS_HOOK_SELF="$0"
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "$0")" && pwd)/../lib/common.sh"
NS_GATE_NAME="config"

INPUT="$(cat)"

if ! printf '%s' "$INPUT" | python3 -c 'import json,sys; json.load(sys.stdin)' >/dev/null 2>&1; then
  ns_resolve_repo "$PWD" && ns_block "night-shift gate: unparseable ConfigChange input in an enforced repo (fail-closed)."
  ns_passthrough
fi

SOURCE="$(ns_json_get "$INPUT" source)"
FILE="$(ns_json_get "$INPUT" file_path)"
CWD="$(ns_json_get "$INPUT" cwd)"; [ -n "$CWD" ] || CWD="$PWD"

ns_resolve_repo "$CWD" || true
# ConfigChange has no permission_mode; enforce whenever the repo is onboarded
# with a present scope.yaml and we are not explicitly opted out.
[ "$NS_RESOLVE" = "enforced" ] || ns_passthrough
[ "${NIGHT_SHIFT_ENFORCE:-}" = "0" ] && ns_passthrough

case "$SOURCE" in
  policy_settings)
    # Managed/policy settings are root-owned and cannot be blocked here anyway.
    ns_passthrough ;;
  user_settings|project_settings|local_settings)
    ns_log "deny config change: $SOURCE $FILE"
    ns_block "night-shift gate: Claude Code settings changes are frozen during an enforced shift ($SOURCE: $FILE). Hooks and permissions are managed at the system level." ;;
  *)
    ns_passthrough ;;
esac
