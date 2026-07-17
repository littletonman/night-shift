#!/usr/bin/env python3
"""Adversarial tests for classify_cmd.py. Run: python3 test_classify.py

The classifier decides PASS / COMMIT / DENY for a Bash command. The most
important properties under test:
  - `git add x && git commit` is DENY (anti-TOCTOU: index changes after the
    gate would snapshot the tree).
  - `git commit -m "msg with && inside"` is COMMIT (quoted operators are not
    real operators).
  - force pushes, history plumbing, and pushes to non-ns/* are DENY.
"""
import os
import sys
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CLASSIFY = os.path.join(HERE, "..", "lib", "classify_cmd.py")

SCOPE = """
allow_paths: ["src/**"]
deny_paths: [".github/workflows/**"]
deny_commands:
  - "rm -rf"
  - "docker"
  - "supabase db push"
push:
  enabled: true
  allow_refs: ["ns/"]
"""

PASS = 0
FAIL = 0


def classify(cmd, scope_text=SCOPE):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(scope_text)
        sp = f.name
    try:
        p = subprocess.run([sys.executable, CLASSIFY, cmd, sp],
                           capture_output=True, text=True)
        out = p.stdout.strip()
        return out.split("\t")[0]
    finally:
        os.unlink(sp)


def expect(cmd, want, scope_text=SCOPE):
    global PASS, FAIL
    got = classify(cmd, scope_text)
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL: %-55r want=%s got=%s" % (cmd, want, got))


# --- commit happy path ------------------------------------------------------
expect('git commit -m "add feature"', "COMMIT")
expect("git commit -m 'fix bug'", "COMMIT")
expect('git commit -m "fix: handle a && b in parser"', "COMMIT")  # quoted && is NOT compound
expect('git commit -m "use | pipe operator"', "COMMIT")
expect('git commit --message "x" --author "a <a@b.c>"', "COMMIT")

# --- anti-TOCTOU: staging combined with commit is DENY ---------------------
expect('git add src/x.ts && git commit -m "x"', "DENY")
expect('git add . && git commit -m "x"', "DENY")
expect('git commit -m "x"; git add y', "DENY")
expect('git add x || git commit -m "y"', "DENY")
expect('git stash && git commit -m "x"', "DENY")
expect('git commit -am "x"', "DENY")       # auto-stage
expect('git commit -a -m "x"', "DENY")
expect('git commit --all -m "x"', "DENY")
expect('git commit --amend -m "x"', "DENY")  # history rewrite
expect('git commit --no-verify -m "x"', "DENY")
expect('git commit -n -m "x"', "DENY")
expect('echo hi && git commit -m "x"', "DENY")  # any compound with commit

# --- pathspec commit is allowed (clean-except-staged makes it safe) --------
expect('git commit -m "x" src/a.ts', "COMMIT")

# --- force push / history plumbing always DENY -----------------------------
expect("git push --force origin ns/staging", "DENY")
expect("git push -f origin ns/staging", "DENY")
expect("git push --force-with-lease origin ns/staging", "DENY")
expect("git push origin +ns/staging:main", "DENY")   # force refspec
expect("git push origin --mirror", "DENY")
expect("git commit-tree HEAD^{tree}", "DENY")
expect("git update-ref refs/heads/main deadbeef", "DENY")
expect("git filter-branch --tree-filter x", "DENY")
expect("git reset --hard origin/main && git commit -m x", "DENY")  # reset present w/ commit compound

# --- push targeting ---------------------------------------------------------
expect("git push origin ns/staging", "PASS")
expect("git push origin ns/staging:ns/staging", "PASS")
expect("git push origin main", "DENY")
expect("git push origin feature/x", "DENY")
expect("git push origin ns/staging:main", "DENY")  # remote side is main
expect("git push", "DENY")  # no explicit refspec

# --- push disabled ----------------------------------------------------------
DISABLED = SCOPE.replace("enabled: true", "enabled: false")
expect("git push origin ns/staging", "DENY", DISABLED)

# --- deny_commands ----------------------------------------------------------
expect("rm -rf /tmp/x", "DENY")
expect("docker build .", "DENY")
expect("supabase db push", "DENY")
expect("rm -r /tmp/x", "PASS")   # not the -rf token sequence
expect("cat docker-compose.yml", "PASS")  # 'docker' only as substring, not a token

# --- enforcement path protection -------------------------------------------
expect("echo x > /opt/night-shift/enforcement/hooks/gate-bash.sh", "DENY")
expect("cat /etc/claude-code/managed-settings.json", "DENY")
expect("vim .night-shift/scope.yaml", "DENY")
expect("sudo rm -rf /", "DENY")

# --- ordinary commands pass -------------------------------------------------
expect("npm test", "PASS")
expect("ls -la src/", "PASS")
expect('echo "hello world"', "PASS")
expect("git status", "PASS")
expect("git add src/x.ts", "PASS")   # staging alone is fine
expect("git diff --cached", "PASS")
expect("git log --oneline", "PASS")

# --- non-lane commit verbs (this phase) ------------------------------------
expect("git merge feature/x", "DENY")
expect("git cherry-pick abc123", "DENY")
expect("git revert abc123", "DENY")
expect("git rebase main", "DENY")

# --- DETECTION-EVASION bypasses (the class found by self-review) ------------
# env-assignment prefix must not hide the commit, and index-retargeting env is denied
expect('GIT_INDEX_FILE=/tmp/x git commit -m x', "DENY")   # would review a different index
expect('GIT_DIR=/tmp/x git commit -m x', "DENY")
expect('HOME=/tmp git commit -m x', "DENY")               # HOME redirects gitconfig
expect('env GIT_INDEX_FILE=/tmp/x git commit -m x', "DENY")
expect('NODE_ENV=test git commit -m x', "COMMIT")         # benign env still routes to gate
expect('NODE_ENV=test npm run build', "PASS")             # benign env on non-git passes

# git global retarget options on commit/push are denied
expect('git -C /other commit -m x', "DENY")
expect('git --git-dir=/other/.git commit -m x', "DENY")
expect('git --work-tree=/other commit -m x', "DENY")
expect('git -C /other push origin ns/staging', "DENY")
expect('git -C src status', "PASS")                       # retarget on a non-commit is fine

# shell wrappers: recurse to detect a hidden commit
expect('bash -c "git commit -m x"', "COMMIT")             # detected, routed to gate
expect("sh -c 'git commit -m x'", "COMMIT")
expect('bash -c "git add . && git commit -m x"', "DENY")  # inner compound denied
expect('bash -c "GIT_INDEX_FILE=/tmp/x git commit -m x"', "DENY")
expect('bash -c "git push origin main"', "DENY")
expect('sh -c "echo hi"', "PASS")
expect('bash -c "$(cat payload)"', "DENY")                # dynamic -c is opaque

# eval and pipe-into-shell are opaque -> denied
expect('eval "git commit -m x"', "DENY")
expect('echo Z2l0 | base64 -d | bash', "DENY")
expect('cat script.sh | sh', "DENY")
expect('git log | grep fix', "PASS")                      # piping to a non-shell is fine

# stdin/heredoc/redirect-fed shell is opaque -> denied; a script FILE is allowed
expect('bash < script.sh', "DENY")
expect('bash <<EOF\ngit commit -m x\nEOF', "DENY")
expect('bash', "DENY")                                    # bare shell reads stdin
expect('sh -s', "DENY")                                   # -s reads stdin, no file
expect('bash build.sh', "PASS")                           # running a script file (documented limitation)
expect('bash -c "echo ok"', "PASS")                       # inline -c still analyzed/recursed

# git aliases (a name-based evasion) cannot be defined
expect('git config alias.ci commit', "DENY")
expect('git config --global alias.ci commit', "DENY")
expect('git -c alias.ci=commit ci -m x', "DENY")

# --- red-team confirmed bypasses (must now be closed) ----------------------
# newline-separated statements: shlex doesn't split on \n, so these once
# collapsed into one mis-read statement and smuggled a commit/push through.
expect('git add src/x.ts\ngit commit -m x', "DENY")
expect('true\ngit push origin main', "DENY")
expect('git status\ngit commit -m pwned', "DENY")
expect('git commit -m "line one\nline two"', "COMMIT")   # newline INSIDE quotes is a literal message
expect('echo a\necho b', "PASS")                          # benign multiline still passes

# command substitution in a commit stages content AFTER the gate snapshots
expect('git commit -m "$(git add evil)"', "DENY")
expect('git commit -m "note `git add evil`"', "DENY")
expect('git commit -m "ts $(date)"', "DENY")              # any active subst in a commit is denied
expect("git commit -m 'literal $(not run)'", "COMMIT")    # single-quoted -> inert -> allowed
expect('echo $(date)', "PASS")                            # subst outside a commit is fine

# runner/wrapper prefixes must not hide the commit/push (round-2 finding)
expect('nice git commit -m x', "COMMIT")
expect('command git commit -m x', "COMMIT")
expect('nohup git commit -m x', "COMMIT")
expect('timeout 10 git commit -m x', "COMMIT")
expect('exec git commit -m x', "COMMIT")
expect('stdbuf -oL git commit -m x', "COMMIT")
expect('nohup git commit --amend -m x', "DENY")           # amend caught behind a runner
expect('command git push origin main', "DENY")            # push-to-main caught behind a runner
expect('nice env GIT_INDEX_FILE=/tmp/x git commit -m x', "DENY")  # nested env caught
expect('find . -exec git commit -m x \\;', "DENY")        # opaque runner
expect('echo x | xargs git commit -m x', "DENY")          # opaque runner (xargs)
expect('flock /tmp/l git commit -m x', "DENY")            # opaque runner (flock)
expect('nice ls -la', "PASS")                             # runner on a non-git command is fine
expect('echo git', "PASS")                                # bare 'git' with no subcommand is not a commit

# git recognized by BASENAME, not the literal token (round-final finding)
expect('/usr/bin/git commit -m x', "COMMIT")
expect('./git commit -m x', "COMMIT")
expect('command /usr/bin/git commit -m x', "COMMIT")
expect('/usr/bin/git push origin main', "DENY")
expect('/usr/local/bin/git commit --amend -m x', "DENY")
expect('/usr/bin/git status', "PASS")

# unknown / aliased git subcommand denied (a pre-existing alias could map to commit)
expect('git ci -m x', "DENY")                             # ci -> commit alias
expect('git lg', "DENY")
expect('git st', "DENY")
expect('git cm -m x', "DENY")
expect('git status', "PASS")                              # known-safe verb
expect('git whatchanged', "PASS")                         # known (uncommon but real)
expect('git worktree list', "PASS")

# .git/ writes (pre-commit-hook injection vector, round-3 finding) are denied
expect('echo x > .git/hooks/pre-commit', "DENY")
expect('printf "#!/bin/sh\\ngit add -f evil" > .git/hooks/pre-commit', "DENY")
expect('cp payload .git/hooks/pre-commit', "DENY")
expect('install -m 755 payload .git/hooks/pre-commit', "DENY")
expect('tee .git/hooks/pre-commit', "DENY")
expect('dd of=.git/hooks/pre-commit', "DENY")
expect('mv payload sub/.git/hooks/pre-commit', "DENY")   # nested repo .git too
expect('cat .git/config', "PASS")                        # reading .git is fine
expect('ls .git/hooks', "PASS")                          # listing is fine
expect('echo x > .gitignore', "PASS")                    # .gitignore is not .git/
expect('git config core.hooksPath /tmp/evil', "DENY")
expect('git config core.hooksPath', "DENY")              # deny get too (safe/minor friction)
expect('git -c core.hooksPath=/tmp/evil commit -m x', "DENY")
expect('git -c core.hooksPath=/tmp/evil status', "DENY")
expect('git config core.fsmonitor /tmp/evil', "DENY")    # fsmonitor also runs a program
expect('git -c core.fsmonitor=/tmp/evil commit -m x', "DENY")

# .night-shift control dir cannot be renamed/removed (would disable enforcement)
expect('mv .night-shift .off', "DENY")
expect('rm -rf .night-shift', "DENY")
expect('mv ./.night-shift /tmp/x', "DENY")
expect('rmdir .night-shift', "DENY")
expect('rm .night-shift/runs/old/state.json', "PASS")     # under runs/ (agent's own) is fine
expect('cat .night-shift/runs/x/state.json', "PASS")      # reading is fine

print("\nclassify tests: %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
