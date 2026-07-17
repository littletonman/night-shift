#!/usr/bin/env bash
# session-start.sh — SessionStart hook. Injects a short resume banner when a
# resident shift is active so a freshly (re)started session immediately knows
# which run it is continuing. Never blocks: always exits 0, emitting at most a
# JSON additionalContext payload. (Primarily a Phase-2 / supervisor concern.)
#
# Deliberately self-contained (does not source common.sh's fail-closed trap) —
# a SessionStart hook must never interfere with starting a session.
set -uo pipefail

INPUT="$(cat 2>/dev/null || true)"

emit_nothing() { printf '{}\n'; exit 0; }

CWD="$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("cwd",""))
except Exception: print("")' 2>/dev/null || true)"
[ -n "$CWD" ] || CWD="$PWD"

ROOT="$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$ROOT" ] || emit_nothing
[ -r "$ROOT/.night-shift/scope.yaml" ] || emit_nothing

RESIDENT="${NIGHT_SHIFT_RESIDENT:-}"
[ -f "$ROOT/.night-shift/RESIDENT" ] && RESIDENT=1
[ "$RESIDENT" = "1" ] || emit_nothing

STATE="$(ls -1t "$ROOT"/.night-shift/runs/*/state.json 2>/dev/null | head -1)"
[ -n "$STATE" ] || emit_nothing

CONTEXT="$(python3 -c '
import json,sys
try:
    s=json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
if s.get("status")!="running":
    sys.exit(0)
krs=s.get("key_results",[])
done=sum(1 for k in krs if k.get("status")=="completed")
obj=s.get("objective") or "(propose)"
sys.stdout.write(
  "You are resuming resident night-shift run %s. Objective (verbatim): %r. "
  "Key results: %d completed of %d. Re-read INVARIANTS.md and state.json before "
  "acting. Enforcement hooks are active: commits are gated by Codex review and "
  "the scope contract; you do not run codex review yourself."
  % (s.get("run_id",""), obj, done, len(krs))
)
' "$STATE" 2>/dev/null || true)"

[ -n "$CONTEXT" ] || emit_nothing

python3 -c '
import json,sys
print(json.dumps({"hookSpecificOutput":{"hookEventName":"SessionStart",
      "additionalContext":sys.argv[1]}}))
' "$CONTEXT" 2>/dev/null || emit_nothing
