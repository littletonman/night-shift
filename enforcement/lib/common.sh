#!/usr/bin/env bash
# common.sh — shared helpers for night-shift enforcement hooks.
#
# SECURITY MODEL (read enforcement/../ARCHITECTURE.md for the full picture):
#   L1  GitHub server-side (branch protection)      — the only zero-bypass layer
#   L2  these hooks, run from root-owned /opt/...    — block even under
#                                                      --dangerously-skip-permissions
#   L3  SKILL.md prompt                              — guidance only
#
# Every gate that sources this file is FAIL CLOSED: the only way to allow a
# tool call is to reach `ns_allow` explicitly. Any error, unset variable, or
# unexpected exit results in exit 2 (block). See ns_finish below.
#
# This file is sourced, not executed. It must not `exit` at source time.

# --- strictness -------------------------------------------------------------
set -uo pipefail

# --- self-location ----------------------------------------------------------
# A sourcing hook sets NS_HOOK_SELF="$0" BEFORE sourcing so we can find siblings
# whether we run from the git repo or the installed /opt/night-shift copy.
: "${NS_HOOK_SELF:=${BASH_SOURCE[1]:-$0}}"
NS_HOOKS_DIR="$(cd "$(dirname "$NS_HOOK_SELF")" >/dev/null 2>&1 && pwd)"
NS_LIB_DIR="$(cd "$NS_HOOKS_DIR/../lib" >/dev/null 2>&1 && pwd || echo "$NS_HOOKS_DIR")"
NS_SCOPE_MATCH="$NS_LIB_DIR/scope_match.py"

# --- fail-closed exit machinery --------------------------------------------
# Gates set NS_DECISION=allow and call `ns_allow` to permit. Everything else
# (explicit ns_block, a failed command, an unset var, falling off the end)
# lands in ns_finish with NS_DECISION!=allow and blocks with exit 2.
NS_DECISION="block"
NS_BLOCK_MSG=""

ns_finish() {
  local rc=$?
  if [ "$NS_DECISION" = "allow" ]; then
    exit 0
  fi
  # Blocking path. Emit the reason to stderr (fed back to the model on exit 2).
  if [ -n "$NS_BLOCK_MSG" ]; then
    printf '%s\n' "$NS_BLOCK_MSG" >&2
  else
    printf 'night-shift gate: blocked (fail-closed; rc=%s, no explicit allow reached)\n' "$rc" >&2
  fi
  exit 2
}
trap ns_finish EXIT

# ns_allow — permit the tool call. Call this ONLY on the verified-safe path.
ns_allow() { NS_DECISION="allow"; exit 0; }

# ns_passthrough — enforcement does not apply here (e.g. not a night-shift repo,
# or an interactive session). Identical to allow but semantically distinct.
ns_passthrough() { NS_DECISION="allow"; exit 0; }

# ns_block "msg" — deny the tool call with a reason shown to the model.
ns_block() { NS_BLOCK_MSG="$1"; NS_DECISION="block"; exit 2; }

# --- logging (diagnostic, NOT a security boundary) --------------------------
ns_log() {
  # Best-effort append to the repo's run log. Never fails the gate.
  local root="${NS_REPO_ROOT:-}"
  [ -n "$root" ] || return 0
  local logdir="$root/.night-shift"
  [ -d "$logdir" ] || return 0
  { printf '%s [%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || echo now)" \
      "${NS_GATE_NAME:-gate}" "$*" >> "$logdir/enforce.log"; } 2>/dev/null || true
}

# --- notifications (ntfy.sh push; best-effort) ------------------------------
# Topic comes from scope.yaml notify.ntfy_topic or env NIGHT_SHIFT_NTFY_TOPIC.
ns_ntfy() {
  # ns_ntfy <priority> <title> <message>
  local prio="$1" title="$2" msg="$3"
  local topic="${NIGHT_SHIFT_NTFY_TOPIC:-${NS_NTFY_TOPIC:-}}"
  [ -n "$topic" ] || return 0
  command -v curl >/dev/null 2>&1 || return 0
  { curl -fsS --max-time 8 \
      -H "Title: $title" -H "Priority: $prio" -H "Tags: warning" \
      -d "$msg" "https://ntfy.sh/$topic" >/dev/null 2>&1; } || true
}

# --- JSON field extraction (python3, so hooks need no jq) --------------------
# ns_json_get <json-string> <dotted.path>  -> prints value ("" if absent)
ns_json_get() {
  printf '%s' "$1" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(3)
cur=d
for k in sys.argv[1].split("."):
    if isinstance(cur,dict) and k in cur:
        cur=cur[k]
    else:
        cur=None; break
if cur is None:
    pass
elif isinstance(cur,str):
    sys.stdout.write(cur)
else:
    sys.stdout.write(json.dumps(cur))
' "$2"
}

# --- scope.yaml scalar reader (python3 + PyYAML) ----------------------------
# ns_scope_get <scope.yaml> <dotted.path> <default>
ns_scope_get() {
  local out
  out="$(python3 -c '
import sys
try:
    import yaml
except Exception:
    sys.exit(3)
try:
    d=yaml.safe_load(open(sys.argv[1])) or {}
except Exception:
    sys.exit(3)
cur=d
for k in sys.argv[2].split("."):
    if isinstance(cur,dict) and k in cur:
        cur=cur[k]
    else:
        cur=None; break
if cur is None:
    sys.exit(4)
if isinstance(cur,bool):
    sys.stdout.write("true" if cur else "false")
elif isinstance(cur,(list,dict)):
    import json; sys.stdout.write(json.dumps(cur))
else:
    sys.stdout.write(str(cur))
' "$1" "$2" 2>/dev/null)" || { printf '%s' "$3"; return 0; }
  printf '%s' "$out"
}

# ns_scope_list <scope.yaml> <dotted.path>  -> prints list items one per line
ns_scope_list() {
  python3 -c '
import sys
try:
    import yaml
    d=yaml.safe_load(open(sys.argv[1])) or {}
except Exception:
    sys.exit(0)
cur=d
for k in sys.argv[2].split("."):
    if isinstance(cur,dict) and k in cur:
        cur=cur[k]
    else:
        cur=[]; break
if isinstance(cur,list):
    for x in cur:
        sys.stdout.write(str(x)+"\n")
' "$1" "$2" 2>/dev/null || true
}

# --- repo + enforcement-active detection ------------------------------------
# The authoritative "is this repo enforced?" signal is a ROOT-OWNED registry,
# NOT a file inside the agent-writable repo. Otherwise the agent could rename
# .night-shift and make the repo look un-onboarded (fail-open). With the
# registry, a registered repo whose scope.yaml is missing FAILS CLOSED.
: "${NS_REGISTRY:=/opt/night-shift/onboarded.list}"

# ns_resolve_repo <startdir> [command] [extra-candidate-paths...] :
# sets NS_REPO_ROOT, NS_SCOPE_PATH, NS_RESOLVE (enforced|missing|none) and
# returns 0/2/1. Resolution (registry ancestor match, or outermost scope.yaml
# in the no-registry fallback, plus paths extracted from <command>) lives in
# lib/resolve.py so gate-bash and gate-write share it.
ns_resolve_repo() {
  local start="${1:-$PWD}"; shift || true
  local command="${1:-}"; shift || true
  local res root state
  res="$(python3 "$NS_LIB_DIR/resolve.py" "$NS_REGISTRY" "$command" "$start" "$@" 2>/dev/null)" || res=""
  root="${res%%$'\t'*}"
  state="${res#*$'\t'}"
  [ "$state" = "$res" ] && state=""     # no tab present -> treat as none
  [ -n "$state" ] || state="none"
  NS_REPO_ROOT="$root"; NS_RESOLVE="$state"
  if [ -n "$root" ]; then NS_SCOPE_PATH="$root/.night-shift/scope.yaml"; else NS_SCOPE_PATH=""; fi
  case "$state" in
    enforced)
      NS_NTFY_TOPIC="$(ns_scope_get "$NS_SCOPE_PATH" notify.ntfy_topic "" 2>/dev/null || true)"
      return 0 ;;
    missing) return 2 ;;
    *) return 1 ;;
  esac
}

# ns_gate_guard <startdir> <permission_mode> [command] [extra-candidate-paths...]
# The standard PreToolUse preamble. Exits via ns_passthrough / ns_block, or
# returns 0 to proceed (repo enforced AND session autonomous).
ns_gate_guard() {
  local start="$1" mode="$2"; shift 2
  ns_resolve_repo "$start" "$@" || true
  case "$NS_RESOLVE" in
    none)
      ns_passthrough ;;
    missing)
      if ns_enforcement_active "$mode"; then
        ns_ntfy high "night-shift: scope.yaml missing" \
          "onboarded repo $NS_REPO_ROOT has no readable scope.yaml (moved/renamed?) — failing closed."
        ns_block "night-shift gate: this repo is onboarded but its scope.yaml is missing/unreadable at $NS_SCOPE_PATH (was .night-shift moved or renamed?). Failing closed — the operator must restore it."
      fi
      ns_passthrough ;;
    enforced)
      ns_enforcement_active "$mode" || ns_passthrough
      return 0 ;;
  esac
  ns_block "night-shift gate: resolve fell through (fail-closed)."
}

# ns_enforcement_active <permission_mode> : decide whether to enforce.
# Enforce when a night-shift scope.yaml is present AND the session is autonomous
# (bypassPermissions) OR explicitly opted in via NIGHT_SHIFT_ENFORCE=1. This
# spares ordinary interactive sessions in the same repo while making autonomous
# runs non-bypassable. An agent cannot flip itself out of bypass without
# re-enabling per-action permission prompts (which stalls autonomy), so gating
# on bypass mode is not an escape hatch.
ns_enforcement_active() {
  local mode="${1:-}"
  # Hard opt-out only via env the agent cannot set on the hook process.
  if [ "${NIGHT_SHIFT_ENFORCE:-}" = "0" ]; then return 1; fi
  if [ "${NIGHT_SHIFT_ENFORCE:-}" = "1" ]; then return 0; fi
  case "$mode" in
    bypassPermissions) return 0 ;;
    *) return 1 ;;
  esac
}

# ns_require_tool <name> — block if a required tool is missing (fail-closed).
ns_require_tool() {
  command -v "$1" >/dev/null 2>&1 || ns_block "night-shift gate: required tool '$1' not found on PATH — install it (fail-closed)."
}
