#!/usr/bin/env bash
# gate-stop.sh — Stop hook. Prevents a resident autonomous shift from stopping
# before it has either reached end-of-shift consensus or been told to stop.
#
# IMPORTANT: this hook is FAIL-OPEN. A Stop hook that blocked on error would
# trap the session forever. It also stays DORMANT unless resident mode is
# active (env NIGHT_SHIFT_RESIDENT=1 or a root-owned .night-shift/RESIDENT
# marker), so ordinary/manual sessions and Phase-1 testing can always stop.
NS_HOOK_SELF="$0"
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "$0")" && pwd)/../lib/common.sh"
NS_GATE_NAME="stop"
NS_DECISION="allow"   # fail-open default

INPUT="$(cat)"

# If a Stop hook already fired once this turn, let the model stop (loop guard).
STOP_ACTIVE="$(ns_json_get "$INPUT" stop_hook_active 2>/dev/null || echo false)"
[ "$STOP_ACTIVE" = "true" ] && ns_allow

CWD="$(ns_json_get "$INPUT" cwd 2>/dev/null || echo "$PWD")"; [ -n "$CWD" ] || CWD="$PWD"
ns_resolve_repo "$CWD" || ns_allow

# Dormant unless resident mode is on.
RESIDENT="${NIGHT_SHIFT_RESIDENT:-}"
[ -f "$NS_REPO_ROOT/.night-shift/RESIDENT" ] && RESIDENT=1
[ "$RESIDENT" = "1" ] || ns_allow

# Human stop request overrides everything.
[ -f "$NS_REPO_ROOT/.night-shift/STOP-REQUEST" ] && ns_allow

# Frozen: staging CI is red — only permitted work is turning it green.
if [ -f "$NS_REPO_ROOT/.night-shift/FROZEN" ]; then
  ns_block "night-shift: staging CI is red (FROZEN). Do not stop — the only permitted work is making staging green (fix-forward, or revert the offending commit). Operator can clear this by removing .night-shift/FROZEN."
fi

# Most recent run state; if none or not running, allow stop.
STATE="$(ls -1t "$NS_REPO_ROOT"/.night-shift/runs/*/state.json 2>/dev/null | head -1)"
[ -n "$STATE" ] || ns_allow
RUN_STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$STATE" 2>/dev/null || echo "")"
[ "$RUN_STATUS" = "running" ] || ns_allow

# Active shift: allow stop only once an end-consensus artifact exists. (The Stop
# hook checks PRESENCE; the authenticity of the consensus rests on the L3 Codex
# procedure the agent must run — see ARCHITECTURE.md §consensus.)
RUN_DIR="$(dirname "$STATE")"
[ -s "$RUN_DIR/end-consensus.txt" ] && ns_allow

ns_block "night-shift: the shift is still active (status=running) and no end-consensus artifact exists at $RUN_DIR/end-consensus.txt. Continue with the next task, or run the end-of-shift dual-consensus procedure (write end-consensus-draft.md and have Codex review it). To stop manually, the operator touches .night-shift/STOP-REQUEST."
