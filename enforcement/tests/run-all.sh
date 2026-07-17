#!/usr/bin/env bash
# run-all.sh — run the full enforcement test suite. No Codex quota is spent
# (the integration test uses a fake codex on PATH). Exit 0 iff everything passes.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FAIL=0

run() {
  printf '\n\033[1;34m=== %s ===\033[0m\n' "$1"
  if "${@:2}"; then printf '\033[1;32mPASS\033[0m: %s\n' "$1"
  else printf '\033[1;31mFAIL\033[0m: %s\n' "$1"; FAIL=1; fi
}

run "scope matcher selftest"  python3 "$HERE/../lib/scope_match.py" --selftest
run "scope matcher tests"     python3 "$HERE/test_scope_match.py"
run "command classifier tests" python3 "$HERE/test_classify.py"
run "gate integration tests"  bash "$HERE/test_gates_integration.sh"

# Shell + python syntax sanity on the shipped scripts.
run "shell syntax" bash -c '
  for f in '"$HERE"'/../hooks/*.sh '"$HERE"'/../lib/*.sh '"$HERE"'/../*.sh; do
    bash -n "$f" || exit 1
  done'
run "python syntax" bash -c '
  for f in '"$HERE"'/../lib/*.py; do python3 -m py_compile "$f" || exit 1; done'

printf '\n'
if [ "$FAIL" -eq 0 ]; then
  printf '\033[1;32mALL ENFORCEMENT TESTS PASSED\033[0m\n'; exit 0
else
  printf '\033[1;31mENFORCEMENT TESTS FAILED\033[0m\n'; exit 1
fi
