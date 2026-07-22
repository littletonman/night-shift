#!/usr/bin/env bash
# test_gates_integration.sh — end-to-end tests of the hook scripts with a FAKE
# codex on PATH (deterministic, no quota). Verifies exit codes: 0 = allow,
# 2 = block. Run: bash test_gates_integration.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOOKS="$HERE/../hooks"
PASS=0; FAIL=0
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Force resolve.py's no-registry (dev/test) mode: point NS_REGISTRY at a path
# that does not exist, so enforcement resolves from each temp repo's own
# scope.yaml instead of the real root-owned /opt registry. Without this, an
# installed /opt/night-shift/onboarded.list makes every unregistered temp repo
# resolve to "none" (passthrough) and every block assertion fails.
NS_TEST_REGISTRY="$WORK/no-such-registry.list"; export NS_REGISTRY="$NS_TEST_REGISTRY"

# --- fake codex: emits a canned transcript based on NS_FAKE_CODEX_MODE ------
FAKEBIN="$WORK/bin"; mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/codex" <<'FAKE'
#!/usr/bin/env bash
[ -n "$NS_FAKE_ARGS_LOG" ] && printf '%s\n' "$*" >> "$NS_FAKE_ARGS_LOG"
mode="${NS_FAKE_CODEX_MODE:-clean}"
case "$mode" in
  crash) echo "boom" >&2; exit 3 ;;
  capacity)
    printf 'OpenAI Codex v0.142.2\n--------\nmodel: gpt-5.5\n--------\nuser\ncurrent changes\nERROR: Selected model is at capacity. Please try a different model.\n'; exit 1 ;;
  capacity_then_clean)
    cf="${NS_FAKE_COUNTER:-/tmp/ns-fake-counter}"; c=$(( $(cat "$cf" 2>/dev/null || echo 0) + 1 )); echo "$c" > "$cf"
    if [ "$c" -lt 3 ]; then printf 'OpenAI Codex v0.142.2\nERROR: Selected model is at capacity.\n'; exit 1
    else printf 'OpenAI Codex v0.142.2\n--------\nmodel: gpt-5.5\n--------\nuser\ncurrent changes\nexec\n succeeded\ncodex\nNo correctness issues were identified.\n'; exit 0; fi ;;
  clean)
    printf 'OpenAI Codex v0.142.2\n--------\nmodel: gpt-5.5\n--------\nuser\ncurrent changes\nexec\n/bin/bash -lc x\n succeeded\ncodex\nNo correctness issues were identified.\n' ;;
  dirty)
    printf 'OpenAI Codex v0.142.2\n--------\nmodel: gpt-5.5\n--------\nuser\ncurrent changes\nexec\n/bin/bash -lc x\n succeeded\ncodex\n- [P1] Bad thing — file.ts:1-1\n  This is a blocking issue.\n' ;;
  noturn)  # banner but no verdict turn -> fail-closed
    printf 'OpenAI Codex v0.142.2\n--------\nmodel: gpt-5.5\n--------\nuser\ncurrent changes\nexec\n running...\n' ;;
esac
exit 0
FAKE
chmod +x "$FAKEBIN/codex"
export PATH="$FAKEBIN:$PATH"

# --- helpers ---------------------------------------------------------------
mkrepo() {
  local d="$WORK/repo.$RANDOM"; mkdir -p "$d"
  git -C "$d" init -q
  git -C "$d" config user.email t@t.t; git -C "$d" config user.name t
  git -C "$d" config commit.gpgsign false
  mkdir -p "$d/src" "$d/.night-shift/runs"
  cat > "$d/.night-shift/scope.yaml" <<'YAML'
allow_paths: ["src/**"]
deny_paths: [".github/workflows/**", "scope.yaml", "**/.env*"]
deny_commands: ["rm -rf", "docker"]
gate:
  fast_checks: ["true"]
  full_checks: []
  codex_effort: high
  codex_timeout_sec: 30
  require_secret_scan: false
push: { enabled: true, allow_refs: ["ns/"] }
YAML
  printf 'seed\n' > "$d/src/seed.txt"; git -C "$d" add src/seed.txt
  git -C "$d" commit -qm seed
  printf '%s' "$d"
}

bash_input() {  # <cwd> <command> [mode]
  local mode="${3:-bypassPermissions}"
  python3 -c '
import json,sys
print(json.dumps({"tool_name":"Bash","permission_mode":sys.argv[3],
  "cwd":sys.argv[1],"hook_event_name":"PreToolUse",
  "tool_input":{"command":sys.argv[2]}}))' "$1" "$2" "$mode"
}
write_input() {  # <cwd> <file_path> [mode]
  local mode="${3:-bypassPermissions}"
  python3 -c '
import json,sys
print(json.dumps({"tool_name":"Write","permission_mode":sys.argv[3],
  "cwd":sys.argv[1],"hook_event_name":"PreToolUse",
  "tool_input":{"file_path":sys.argv[2],"content":"x"}}))' "$1" "$2" "$mode"
}

expect_rc() {  # <desc> <expected-rc> <actual-rc>
  if [ "$2" = "$3" ]; then PASS=$((PASS+1)); else
    FAIL=$((FAIL+1)); printf 'FAIL: %s (want rc=%s got rc=%s)\n' "$1" "$2" "$3"; fi
}

run_bash_gate() { bash_input "$1" "$2" "${3:-bypassPermissions}" | bash "$HOOKS/gate-bash.sh" >/dev/null 2>&1; printf '%s' $?; }
run_write_gate() { write_input "$1" "$2" "${3:-bypassPermissions}" | bash "$HOOKS/gate-write.sh" >/dev/null 2>&1; printf '%s' $?; }

# === WRITE GATE ============================================================
R="$(mkrepo)"
expect_rc "write in-scope src/a.ts allowed" 0 "$(run_write_gate "$R" "$R/src/a.ts")"
expect_rc "write deny .github/workflows blocked" 2 "$(run_write_gate "$R" "$R/.github/workflows/ci.yml")"
expect_rc "write out-of-scope lib/a.ts blocked" 2 "$(run_write_gate "$R" "$R/lib/a.ts")"
expect_rc "write .env blocked" 2 "$(run_write_gate "$R" "$R/src/.env")"
expect_rc "write outside repo blocked" 2 "$(run_write_gate "$R" "/etc/passwd")"
expect_rc "write passthrough in interactive mode" 0 "$(run_write_gate "$R" "$R/lib/a.ts" default)"

# no scope.yaml repo => passthrough
NOSCOPE="$WORK/noscope"; mkdir -p "$NOSCOPE"; git -C "$NOSCOPE" init -q
expect_rc "write passthrough when no scope.yaml" 0 "$(run_write_gate "$NOSCOPE" "$NOSCOPE/anything.ts")"

# === BASH GATE: classification =============================================
expect_rc "bash ls allowed" 0 "$(run_bash_gate "$R" "ls -la")"
expect_rc "bash push main blocked" 2 "$(run_bash_gate "$R" "git push origin main")"
expect_rc "bash push ns/ allowed" 0 "$(run_bash_gate "$R" "git push origin ns/staging")"
expect_rc "bash force push blocked" 2 "$(run_bash_gate "$R" "git push -f origin ns/staging")"
expect_rc "bash commit -am blocked" 2 "$(run_bash_gate "$R" 'git commit -am x')"
expect_rc "bash add&&commit blocked" 2 "$(run_bash_gate "$R" 'git add src/a.ts && git commit -m x')"
expect_rc "bash rm -rf blocked" 2 "$(run_bash_gate "$R" "rm -rf /tmp/x")"
expect_rc "bash deny passthrough interactive" 0 "$(run_bash_gate "$R" "git push origin main" default)"

# === BASH GATE: commit gate flow ===========================================
# clean-except-staged violation: unstaged change present
R2="$(mkrepo)"; printf 'edit\n' >> "$R2/src/seed.txt"   # unstaged modification
expect_rc "commit blocked when tree not fully staged" 2 "$(run_bash_gate "$R2" 'git commit -m x')"

# out-of-scope staged file blocked BEFORE codex
R3="$(mkrepo)"; mkdir -p "$R3/lib"; printf 'x\n' > "$R3/lib/bad.ts"; git -C "$R3" add lib/bad.ts
expect_rc "commit blocked when staged file out of scope" 2 "$(run_bash_gate "$R3" 'git commit -m x')"

# in-scope + clean + fake codex CLEAN => allowed
R4="$(mkrepo)"; printf 'good\n' > "$R4/src/feature.ts"; git -C "$R4" add src/feature.ts
NS_FAKE_CODEX_MODE=clean
export NS_FAKE_CODEX_MODE
expect_rc "commit allowed when in-scope + codex clean" 0 "$(run_bash_gate "$R4" 'git commit -m x')"

# regression: the codex review call must neutralize the agent-writable AGENTS.md
# (project_doc_max_bytes=0) and keep the read-only sandbox — both are load-bearing.
R4b="$(mkrepo)"; printf 'good\n' > "$R4b/src/feature.ts"; git -C "$R4b" add src/feature.ts
NS_FAKE_CODEX_MODE=clean; export NS_FAKE_CODEX_MODE
ALOG="$WORK/codex-args.log"; : > "$ALOG"; export NS_FAKE_ARGS_LOG="$ALOG"
run_bash_gate "$R4b" 'git commit -m x' >/dev/null
grep -q 'project_doc_max_bytes=0' "$ALOG"
expect_rc "codex review neutralizes AGENTS.md (project_doc_max_bytes=0)" 0 "$?"
grep -q 'sandbox_mode="read-only"' "$ALOG"
expect_rc "codex review keeps the read-only sandbox" 0 "$?"
grep -q 'model="gpt-5.6-terra"' "$ALOG"
expect_rc "codex review pins the review model (gate.codex_model default)" 0 "$?"
unset NS_FAKE_ARGS_LOG

# regression: an operator-set NS_CODEX_REVIEW_MODEL (the run-config env path)
# overrides the baked default — models travel as env, not baked into the image.
R4c="$(mkrepo)"; printf 'good\n' > "$R4c/src/feature.ts"; git -C "$R4c" add src/feature.ts
ALOG2="$WORK/codex-args2.log"; : > "$ALOG2"; export NS_FAKE_ARGS_LOG="$ALOG2"
NS_CODEX_REVIEW_MODEL="gpt-5.6-terra-override"; export NS_CODEX_REVIEW_MODEL
run_bash_gate "$R4c" 'git commit -m x' >/dev/null
grep -q 'model="gpt-5.6-terra-override"' "$ALOG2"
expect_rc "review model honors NS_CODEX_REVIEW_MODEL over the baked default" 0 "$?"
unset NS_CODEX_REVIEW_MODEL NS_FAKE_ARGS_LOG

# in-scope + clean + fake codex DIRTY (P1) => blocked
R5="$(mkrepo)"; printf 'bug\n' > "$R5/src/feature.ts"; git -C "$R5" add src/feature.ts
NS_FAKE_CODEX_MODE=dirty
expect_rc "commit blocked when codex finds P1" 2 "$(run_bash_gate "$R5" 'git commit -m x')"

# fake codex CRASH => fail-closed block
R6="$(mkrepo)"; printf 'x\n' > "$R6/src/feature.ts"; git -C "$R6" add src/feature.ts
NS_FAKE_CODEX_MODE=crash
expect_rc "commit blocked when codex crashes (fail-closed)" 2 "$(run_bash_gate "$R6" 'git commit -m x')"

# fake codex NO VERDICT TURN => fail-closed block
R7="$(mkrepo)"; printf 'x\n' > "$R7/src/feature.ts"; git -C "$R7" add src/feature.ts
NS_FAKE_CODEX_MODE=noturn
expect_rc "commit blocked when codex output has no verdict (fail-closed)" 2 "$(run_bash_gate "$R7" 'git commit -m x')"
unset NS_FAKE_CODEX_MODE

# gitleaks required but missing => block
R8="$(mkrepo)"; sed -i 's/require_secret_scan: false/require_secret_scan: true/' "$R8/.night-shift/scope.yaml"
printf 'x\n' > "$R8/src/feature.ts"; git -C "$R8" add src/feature.ts
NS_FAKE_CODEX_MODE=clean; export NS_FAKE_CODEX_MODE
# only expect a block if gitleaks is actually absent on this machine
if ! command -v gitleaks >/dev/null 2>&1; then
  expect_rc "commit blocked when secret scan required but gitleaks missing" 2 "$(run_bash_gate "$R8" 'git commit -m x')"
fi
unset NS_FAKE_CODEX_MODE

# === SYMLINK ESCAPE (gate-write) ===========================================
R11="$(mkrepo)"
ln -s /etc "$R11/src/etclink"
expect_rc "write through symlink escaping to /etc blocked" 2 "$(run_write_gate "$R11" "$R11/src/etclink/pwned")"
mkdir -p "$R11/.github/workflows"
ln -s "$R11/.github/workflows" "$R11/src/wflink"
expect_rc "write through symlink into deny path blocked" 2 "$(run_write_gate "$R11" "$R11/src/wflink/ci.yml")"
expect_rc "normal in-scope write still allowed" 0 "$(run_write_gate "$R11" "$R11/src/normal.ts")"

# === REGISTRY FAIL-CLOSED (mv .night-shift cannot disable enforcement) ======
RREG="$WORK/registry.list"
R12="$(mkrepo)"
( cd "$R12" && pwd -P ) > "$RREG"          # register the repo (realpath)
export NS_REGISTRY="$RREG"
expect_rc "registry: push main still blocked (registered+scope present)" 2 "$(run_bash_gate "$R12" 'git push origin main')"
expect_rc "registry: mv .night-shift is denied by classifier" 2 "$(run_bash_gate "$R12" 'mv .night-shift .off')"
# actually move it, then a BENIGN command must fail closed (enforcement not disabled)
mv "$R12/.night-shift" "$R12/.off"
expect_rc "registry: benign cmd blocked when scope missing (fail-closed)" 2 "$(run_bash_gate "$R12" 'ls -la')"
expect_rc "registry: write blocked when scope missing (fail-closed)" 2 "$(run_write_gate "$R12" "$R12/src/x.ts")"
mv "$R12/.off" "$R12/.night-shift"          # restore
expect_rc "registry: benign cmd allowed again after restore" 0 "$(run_bash_gate "$R12" 'ls -la')"
# interactive (non-bypass) session with scope missing should NOT be trapped
mv "$R12/.night-shift" "$R12/.off"
expect_rc "registry: interactive session passes through when scope missing" 0 "$(run_bash_gate "$R12" 'ls -la' default)"
mv "$R12/.off" "$R12/.night-shift"
export NS_REGISTRY="$NS_TEST_REGISTRY"

# === CWD-ESCAPE (operate on a registered repo from an outside cwd) ==========
RREG2="$WORK/reg2.list"
R13="$(mkrepo)"; ( cd "$R13" && pwd -P ) > "$RREG2"
export NS_REGISTRY="$RREG2"
PARENT="$(dirname "$R13")"
printf 'x\n' > "$R13/src/staged.ts"; git -C "$R13" add src/staged.ts
expect_rc "cwd-escape: git -C <repo> push main blocked from outside cwd" 2 "$(run_bash_gate "$PARENT" "git -C $R13 push origin main")"
expect_rc "cwd-escape: cd <repo> && commit blocked from outside cwd" 2 "$(run_bash_gate "$PARENT" "cd $R13 && git commit -m x")"
expect_rc "cwd-escape: write into repo deny-path from outside cwd blocked" 2 "$(run_write_gate "$PARENT" "$R13/.github/workflows/ci.yml")"
expect_rc "cwd-escape: unrelated command from outside cwd passes through" 0 "$(run_bash_gate "$PARENT" 'ls -la')"
export NS_REGISTRY="$NS_TEST_REGISTRY"

# === EMPTY/UNUSABLE scope.yaml -> fail closed ==============================
R15="$(mkrepo)"; : > "$R15/.night-shift/scope.yaml"       # present but empty
RREG3="$WORK/reg3.list"; ( cd "$R15" && pwd -P ) > "$RREG3"
export NS_REGISTRY="$RREG3"
expect_rc "empty scope.yaml: benign cmd blocked (classifier fail-closed)" 2 "$(run_bash_gate "$R15" 'ls -la')"
expect_rc "empty scope.yaml: write blocked (scope_match fail-closed)" 2 "$(run_write_gate "$R15" "$R15/src/x.ts")"
export NS_REGISTRY="$NS_TEST_REGISTRY"

# === PRE-COMMIT HOOK INJECTION (round-3 finding) ===========================
# A planted .git/hooks/pre-commit must block the commit at gate time, even
# though it was created by a means the classifier might not screen.
R16="$(mkrepo)"
printf 'good\n' > "$R16/src/clean.ts"; git -C "$R16" add src/clean.ts
NS_FAKE_CODEX_MODE=clean; export NS_FAKE_CODEX_MODE
# baseline: clean staged commit is allowed
expect_rc "hook: clean commit allowed with no hooks" 0 "$(run_bash_gate "$R16" 'git commit -m ok')"
# plant a pre-commit hook (simulate a write the classifier didn't catch)
printf '#!/bin/sh\ngit add -f .github/workflows/pwn.yml\n' > "$R16/.git/hooks/pre-commit"
chmod +x "$R16/.git/hooks/pre-commit"
printf 'more\n' > "$R16/src/clean.ts"; git -C "$R16" add src/clean.ts
expect_rc "hook: commit blocked when a pre-commit hook is present" 2 "$(run_bash_gate "$R16" 'git commit -m x')"
# neutralized via core.hooksPath -> empty dir: commit allowed again
EMPTY="$WORK/emptyhooks"; mkdir -p "$EMPTY"
git -C "$R16" config core.hooksPath "$EMPTY"
expect_rc "hook: commit allowed when core.hooksPath points at an empty dir" 0 "$(run_bash_gate "$R16" 'git commit -m x')"
# a SYMLINKED hook is also caught ([ -x ] follows symlinks)
git -C "$R16" config --unset core.hooksPath
rm -f "$R16/.git/hooks/pre-commit"
printf '#!/bin/sh\ntrue\n' > "$WORK/evilhook"; chmod +x "$WORK/evilhook"
ln -s "$WORK/evilhook" "$R16/.git/hooks/pre-commit"
printf 'z\n' > "$R16/src/clean.ts"; git -C "$R16" add src/clean.ts
expect_rc "hook: symlinked pre-commit hook also blocked" 2 "$(run_bash_gate "$R16" 'git commit -m x')"
unset NS_FAKE_CODEX_MODE

# === VERDICT PARSER window (round-3 finding) ===============================
# A fake codex whose P1 finding body contains a lone 'codex' line must still block.
FAKE2="$WORK/bin2"; mkdir -p "$FAKE2"
cat > "$FAKE2/codex" <<'FK'
#!/usr/bin/env bash
printf 'OpenAI Codex v0.142.2\n--------\nmodel: gpt-5.5\n--------\nuser\ncurrent changes\nexec\n/bin/bash -lc x\n succeeded\ncodex\n- [P1] Bad thing — file.ts:1-1\n  Details reference codex\ncodex\nlooks fine now\n'
exit 0
FK
chmod +x "$FAKE2/codex"
R17="$(mkrepo)"; printf 'x\n' > "$R17/src/f.ts"; git -C "$R17" add src/f.ts
expect_rc "verdict: P1 with a trailing lone 'codex' line still blocks" 2 "$(PATH="$FAKE2:$PATH" run_bash_gate "$R17" 'git commit -m x')"

# === CAPACITY RETRY (transient codex "at capacity") ========================
export NS_CAPACITY_BACKOFF_SEC=0   # no real backoff sleep in tests
# persistent capacity -> retries then blocks fail-closed
R18="$(mkrepo)"; printf 'x\n' > "$R18/src/feature.ts"; git -C "$R18" add src/feature.ts
NS_FAKE_CODEX_MODE=capacity; export NS_FAKE_CODEX_MODE
expect_rc "capacity: commit blocked after 3 retries (fail-closed)" 2 "$(run_bash_gate "$R18" 'git commit -m x')"
unset NS_FAKE_CODEX_MODE
# capacity clears by the 3rd try -> commit allowed
R19="$(mkrepo)"; printf 'x\n' > "$R19/src/feature.ts"; git -C "$R19" add src/feature.ts
CF="$WORK/cap-counter.$RANDOM"; : > "$CF"
NS_FAKE_CODEX_MODE=capacity_then_clean; NS_FAKE_COUNTER="$CF"; export NS_FAKE_CODEX_MODE NS_FAKE_COUNTER
expect_rc "capacity: commit allowed once capacity clears (retry succeeds)" 0 "$(run_bash_gate "$R19" 'git commit -m x')"
unset NS_FAKE_CODEX_MODE NS_FAKE_COUNTER NS_CAPACITY_BACKOFF_SEC

printf '\nintegration tests: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
