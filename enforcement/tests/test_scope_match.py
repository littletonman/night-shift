#!/usr/bin/env python3
"""Comprehensive tests for scope_match.py. Run: python3 test_scope_match.py"""
import os
import sys
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MATCH = os.path.join(HERE, "..", "lib", "scope_match.py")

sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import scope_match as sm  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", msg)


def classify(allow, deny, path):
    allow_re = [sm.compile_glob(g) for g in allow]
    deny_re = [sm.compile_glob(g) for g in deny]
    ok, _ = sm.classify(path, allow_re, deny_re)
    return ok


# --- basic allow / deny -----------------------------------------------------
check(classify(["src/**"], [], "src/a.ts"), "src/** allows src/a.ts")
check(classify(["src/**"], [], "src/a/b/c.ts"), "src/** allows nested")
check(not classify(["src/**"], [], "lib/a.ts"), "src/** denies lib/a.ts")
check(not classify(["src/**"], [], "srcx/a.ts"), "src/** no prefix bleed to srcx")
check(not classify([], [], "anything"), "empty allow denies all")

# --- deny wins over allow ---------------------------------------------------
check(not classify(["**"], ["src/secrets/**"], "src/secrets/k.ts"), "deny beats allow")
check(classify(["**"], ["src/secrets/**"], "src/app.ts"), "allow when not denied")

# --- gitignore anchoring: no slash => basename anywhere ---------------------
check(not classify(["**"], ["scope.yaml"], "scope.yaml"), "bare deny top-level")
check(not classify(["**"], ["scope.yaml"], ".night-shift/scope.yaml"), "bare deny nested")
check(not classify(["**"], ["scope.yaml"], "a/b/c/scope.yaml"), "bare deny deep")
check(classify(["**"], ["scope.yaml"], "scope.yaml.bak"), "bare deny exact basename only")

# --- ** / **/ semantics -----------------------------------------------------
check(not classify(["**"], ["**/.env*"], ".env"), "**/.env* matches top .env")
check(not classify(["**"], ["**/.env*"], "a/.env"), "**/.env* matches nested")
check(not classify(["**"], ["**/.env*"], "a/b/.env.local"), "**/.env* matches deep .env.local")
check(classify(["**"], ["**/.env*"], "a/environment.ts"), "**/.env* not env-prefixed dir file")
check(classify(["packages/**/*.ts"], [], "packages/a.ts"), "**/ can be zero segments")
check(classify(["packages/**/*.ts"], [], "packages/ui/src/a.ts"), "**/ multiple segments")
check(not classify(["packages/**/*.ts"], [], "packages/ui/src/a.js"), "extension enforced")
check(not classify(["packages/**/*.ts"], [], "other/a.ts"), "root anchor enforced")

# --- trailing slash => subtree ---------------------------------------------
check(not classify(["**"], ["infra/"], "infra/main.tf"), "infra/ denies subtree")
check(not classify(["**"], ["infra/"], "infra/a/b.tf"), "infra/ denies deep")
check(classify(["**"], ["infra/"], "infrastructure.md"), "infra/ no prefix bleed")

# --- single char and ? ------------------------------------------------------
check(classify(["src/?.ts"], [], "src/a.ts"), "? matches one char")
check(not classify(["src/?.ts"], [], "src/ab.ts"), "? matches exactly one")
check(not classify(["src/*.ts"], [], "src/a/b.ts"), "* does not cross /")

# --- traversal / unsafe paths always denied --------------------------------
check(not classify(["**"], [], "../outside/x.ts"), "traversal denied")
check(not classify(["**"], [], "a/../../etc/passwd"), "embedded traversal denied")
check(classify(["**"], [], "./src/a.ts"), "leading ./ normalized and allowed")

# --- .github/workflows protection (the L1-defense path) --------------------
check(not classify(["**"], [".github/workflows/**"], ".github/workflows/ci.yml"),
      "workflows denied")
check(classify(["**"], [".github/workflows/**"], ".github/CODEOWNERS"),
      "other .github allowed")

# --- character classes ------------------------------------------------------
check(classify(["src/[abc].ts"], [], "src/a.ts"), "char class match")
check(not classify(["src/[abc].ts"], [], "src/d.ts"), "char class non-match")
check(classify(["src/[!x].ts"], [], "src/a.ts"), "negated class match")
check(not classify(["src/[!x].ts"], [], "src/x.ts"), "negated class excludes")

# --- CLI end-to-end (subprocess) -------------------------------------------
def run_cli(scope_yaml, paths):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(scope_yaml)
        sp = f.name
    try:
        p = subprocess.run([sys.executable, MATCH, sp, "--paths0"],
                           input="\x00".join(paths).encode(),
                           capture_output=True)
        return p.returncode
    finally:
        os.unlink(sp)


check(run_cli("allow_paths: ['src/**']\ndeny_paths: []\n", ["src/a.ts", "src/b.ts"]) == 0,
      "CLI all in scope -> 0")
check(run_cli("allow_paths: ['src/**']\ndeny_paths: []\n", ["src/a.ts", "lib/x.ts"]) == 1,
      "CLI one out of scope -> 1")
check(run_cli("allow_paths: ['**']\ndeny_paths: ['.github/workflows/**']\n",
              [".github/workflows/ci.yml"]) == 1, "CLI deny workflow -> 1")

# --- fail-closed: unusable scope.yaml -> exit 2 ----------------------------
def run_cli_raw(scope_text, paths):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(scope_text)
        sp = f.name
    try:
        p = subprocess.run([sys.executable, MATCH, sp, "--paths0"],
                           input="\x00".join(paths).encode(), capture_output=True)
        return p.returncode
    finally:
        os.unlink(sp)


check(run_cli_raw("allow_paths: 'not a list'\n", ["src/a.ts"]) == 2,
      "malformed allow_paths -> fail-closed 2")
check(run_cli_raw(": : : not yaml : :\n", ["src/a.ts"]) == 2,
      "unparseable yaml -> fail-closed 2")

print("\nscope_match tests: %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
