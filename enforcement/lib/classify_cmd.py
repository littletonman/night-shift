#!/usr/bin/env python3
"""
classify_cmd.py — classify a Bash command for the night-shift PreToolUse gate.

Reads the command from argv[1] and a scope.yaml path from argv[2]. Prints a
verdict to stdout:

    PASS                 allow without the commit gate
    COMMIT               a gate-able `git commit` — run the commit gate
    DENY\t<reason>       block, with a human reason (tab-separated)

The job is to make sure `git commit` and `git push` can neither be HIDDEN from
the classifier nor REDIRECTED to operate on state the commit gate does not
inspect. The gate's staged-scope + Codex review only mean something if:
  - a commit cannot be smuggled past detection (env prefix, shell wrapper,
    alias, eval, pipe-into-shell), and
  - a commit cannot be pointed at a different index/repo than the gate reads
    (GIT_INDEX_FILE / GIT_DIR / -C / --git-dir / --work-tree), and
  - the index cannot change between the gate's snapshot and the commit
    (chaining `git add` with `git commit`).

Correct shell tokenization via `shlex` (punctuation_chars=True) so quoted
operators are not mistaken for real ones. FAIL CLOSED: any parse error -> DENY.
"""

import sys
import re
import shlex

OPERATORS = {"&&", "||", ";", "|", "&", "\n"}
REDIRECTS = {">", ">>", "<", "<<", ">|", "&>", ">&"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash"}
INTERPRETERS = SHELLS  # things we refuse to pipe an opaque script into

INDEX_MUTATORS = {"add", "stage", "rm", "mv", "restore", "reset", "checkout",
                  "apply", "stash", "clean"}
GIT_PLUMBING = {"commit-tree", "update-ref", "update-index", "fast-import",
                "filter-branch", "symbolic-ref", "replace", "mktree",
                "hash-object", "write-tree", "read-tree"}
GIT_NONLANE_COMMIT = {"merge", "cherry-pick", "revert", "am", "rebase"}
# git global options that retarget the repo/index/tree the command operates on.
GIT_RETARGET_OPTS = {"-C", "--git-dir", "--work-tree", "--namespace"}
# runners that LOOP/DEFER/hide a wrapped command — deny when they wrap git.
OPAQUE_RUNNERS = {"xargs", "find", "parallel", "watch", "entr", "flock"}
# Every real git subcommand. Anything else after `git` is (almost certainly) a
# user alias, which could map to commit/push/rebase — so unknown => DENY.
KNOWN_GIT_SUBCOMMANDS = {
    "add", "am", "annotate", "apply", "archive", "bisect", "blame", "branch",
    "bundle", "cat-file", "check-attr", "check-ignore", "check-mailmap",
    "check-ref-format", "checkout", "checkout-index", "cherry", "cherry-pick",
    "citool", "clean", "clone", "column", "commit", "commit-graph", "commit-tree",
    "config", "count-objects", "credential", "credential-cache", "credential-store",
    "describe", "diff", "diff-files", "diff-index", "diff-tree", "difftool",
    "fast-export", "fast-import", "fetch", "filter-branch", "fmt-merge-msg",
    "for-each-ref", "for-each-repo", "format-patch", "fsck", "gc",
    "get-tar-commit-id", "grep", "gui", "hash-object", "help", "http-backend",
    "init", "instaweb", "interpret-trailers", "log", "ls-files", "ls-remote",
    "ls-tree", "maintenance", "merge", "merge-base", "merge-file", "merge-index",
    "merge-one-file", "merge-tree", "mktag", "mktree", "multi-pack-index", "mv",
    "name-rev", "notes", "pack-objects", "pack-redundant", "pack-refs", "patch-id",
    "prune", "prune-packed", "pull", "push", "quiltimport", "range-diff",
    "read-tree", "rebase", "receive-pack", "reflog", "remote", "repack", "replace",
    "request-pull", "rerere", "reset", "restore", "rev-list", "rev-parse", "revert",
    "rm", "send-email", "send-pack", "shortlog", "show", "show-branch", "show-index",
    "show-ref", "sparse-checkout", "stage", "stash", "status", "stripspace",
    "submodule", "subtree", "switch", "symbolic-ref", "tag", "unpack-file",
    "unpack-objects", "update-index", "update-ref", "update-server-info",
    "upload-archive", "upload-pack", "var", "verify-commit", "verify-pack",
    "verify-tag", "version", "whatchanged", "worktree", "write-tree", "lfs",
    "svn", "p4", "daemon", "shell",
}


def is_git(token):
    """Match git by BASENAME so /usr/bin/git and ./git are recognized (not only
    the bare token `git`)."""
    return token.rsplit("/", 1)[-1] == "git"

ENFORCEMENT_MARKERS = ("/opt/night-shift", "/etc/claude-code",
                       ".night-shift/scope.yaml", "managed-settings.json")
DANGER_TOOLS = {"sudo", "chattr", "setfacl", "doas", "pkexec"}
# tools that could rename/delete the root-owned .night-shift control directory
FS_MUTATORS = {"mv", "rm", "rmdir", "cp", "ln", "install", "rsync", "shred",
               "truncate", "unlink", "rename"}
# tools that can WRITE a file (used to screen writes into a repo's .git/ dir)
WRITE_TOOLS = FS_MUTATORS | {"tee", "dd", "sed"}
ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
# env vars that can subvert git's view of the index/repo/config.
DANGEROUS_ENV_NAMES = {"HOME", "XDG_CONFIG_HOME", "GIT_CONFIG", "GIT_CONFIG_GLOBAL"}
MAX_DEPTH = 4


def out(verdict, reason=""):
    if reason:
        sys.stdout.write(verdict + "\t" + reason + "\n")
    else:
        sys.stdout.write(verdict + "\n")
    sys.exit(0)


def die_deny(reason):
    out("DENY", reason)


def normalize_newlines(cmd):
    """Convert UNQUOTED newlines to ';' so multi-line commands split into
    statements. shlex's punctuation_chars handles ; | & ( ) < > but NOT newline,
    so without this `git add x\\ngit commit` collapses into a single mis-read
    statement (a real bypass). Quotes and line-continuations are respected so a
    newline inside a commit message is preserved."""
    res = []
    i, n, q = 0, len(cmd), None
    while i < n:
        c = cmd[i]
        if q:
            res.append(c)
            if c == '\\' and q == '"' and i + 1 < n:
                res.append(cmd[i + 1]); i += 2; continue
            if c == q:
                q = None
            i += 1; continue
        if c in ('"', "'"):
            q = c; res.append(c); i += 1; continue
        if c == '\\' and i + 1 < n:
            if cmd[i + 1] == '\n':          # line continuation: drop both
                i += 2; continue
            res.append(c); res.append(cmd[i + 1]); i += 2; continue
        if c == '\n':
            res.append(';'); i += 1; continue
        res.append(c); i += 1
    return ''.join(res)


def tokenize(cmd):
    lex = shlex.shlex(normalize_newlines(cmd), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def split_statements(tokens):
    stmts, cur, had_op = [], [], False
    for t in tokens:
        if t in OPERATORS:
            had_op = True
            if cur:
                stmts.append(cur); cur = []
        else:
            cur.append(t)
    if cur:
        stmts.append(cur)
    return stmts, had_op


def contiguous_subseq(hay, needle):
    if not needle:
        return False
    for i in range(len(hay) - len(needle) + 1):
        if hay[i:i + len(needle)] == needle:
            return True
    return False


def strip_env_prefix(argv):
    """Strip a leading `env` command and any NAME=VALUE assignment tokens.
    Returns (assignments, rest_argv)."""
    assigns = []
    i = 0
    if i < len(argv) and argv[i] == "env":
        i += 1
    while i < len(argv) and ENV_ASSIGN_RE.match(argv[i]):
        assigns.append(argv[i]); i += 1
    return assigns, argv[i:]


def dangerous_env(assigns):
    for a in assigns:
        name = a.split("=", 1)[0]
        if name.startswith("GIT_") or name in DANGEROUS_ENV_NAMES:
            return name
    return None


def git_walk(argv):
    """If argv (already env-stripped) is a git invocation, return
    (subcommand, rest_tokens, retarget_opts, bad_config). Else None."""
    if not argv or not is_git(argv[0]):
        return None
    i = 1
    retarget = []
    bad_c = None
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "--git-dir", "--work-tree", "--namespace"):
            retarget.append(a); i += 2; continue
        if a.split("=", 1)[0] in ("--git-dir", "--work-tree", "--namespace"):
            retarget.append(a.split("=", 1)[0]); i += 1; continue
        if a == "-c":
            if i + 1 < len(argv) and is_dangerous_c(argv[i + 1]):
                bad_c = argv[i + 1].split("=", 1)[0]
            i += 2; continue
        if a.startswith("-c") and len(a) > 2:
            if is_dangerous_c(a[2:]):
                bad_c = a[2:].split("=", 1)[0]
            i += 1; continue
        if a.startswith("-"):
            i += 1; continue
        return argv[i], argv[i + 1:], retarget, bad_c
    return None


def shell_c_script(argv):
    """If argv is `<shell> [opts] -c <script> ...`, return <script>; else None."""
    if not argv or argv[0] not in SHELLS:
        return None
    for i in range(1, len(argv)):
        if argv[i] == "-c" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def has_cmd_subst(cmd):
    """True if the raw command contains an ACTIVE command/process substitution
    ($( … ), backticks, <( ), >( )). Single-quoted regions are literal in bash
    and exempt; double quotes do NOT disable $( … ), so they are not exempt."""
    i, n, in_single = 0, len(cmd), False
    while i < n:
        c = cmd[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1; continue
        if c == "'":
            in_single = True; i += 1; continue
        if c == '\\' and i + 1 < n:
            i += 2; continue
        if c == '`':
            return True
        if c == '$' and i + 1 < n and cmd[i + 1] == '(':
            return True
        if c in '<>' and i + 1 < n and cmd[i + 1] == '(':
            return True
        i += 1
    return False


def targets_nightshift_dir(argv):
    """True if any argument refers to the .night-shift control directory itself
    (not paths under it, which the agent legitimately reads/writes)."""
    for a in argv[1:]:
        base = a.rstrip('/')
        if base == '.night-shift' or base == './.night-shift' \
                or base.endswith('/.night-shift'):
            return True
    return False


def path_in_git_dir(p):
    """True if a path writes INTO a repo's .git directory (hooks, config, refs).
    Direct writes there are never legitimate for the agent and are the vector
    for planting a pre-commit hook that injects content past the gate."""
    q = p.replace('\\', '/')
    return q == '.git' or q.endswith('/.git') \
        or q.startswith('.git/') or q.startswith('./.git/') or '/.git/' in q


def is_dangerous_c(val):
    """A `-c key=value` / `git config key` that can redirect commits or run a
    program during commits (aliases, hooks path, fsmonitor)."""
    v = val.lower()
    return (v.startswith('alias.') or v.startswith('core.hookspath')
            or v.startswith('core.fsmonitor'))


def load_scope(scope_path):
    # FAIL CLOSED: a present-but-unusable scope.yaml must not silently drop
    # deny_commands or default push to enabled (consistency with scope_match.py).
    import yaml
    with open(scope_path) as f:
        d = yaml.safe_load(f)
    if not isinstance(d, dict):
        raise ValueError("scope.yaml is empty or not a mapping")
    deny = []
    for item in (d.get("deny_commands", []) or []):
        if isinstance(item, str):
            try:
                deny.append(shlex.split(item))
            except Exception:
                deny.append(item.split())
    push = d.get("push", {}) or {}
    return {
        "deny_commands": deny,
        "push_enabled": push.get("enabled", True),
        "push_allow": push.get("allow_refs", ["ns/"]) or ["ns/"],
    }


def analyze_push(rest, scope):
    if not scope["push_enabled"]:
        die_deny("push is disabled in scope.yaml (local-only until the "
                 "staging/promote path is configured).")
    for t in rest:
        if t in ("-f", "--force") or t.startswith("--force-with-lease") \
                or t in ("--mirror", "--delete", "-d") or t.startswith("+"):
            die_deny("force/mirror/delete push (or force refspec) is never allowed.")
    args = [t for t in rest if not t.startswith("-")]
    refspecs = args[1:] if len(args) >= 1 else []
    allow = scope["push_allow"]

    def ref_ok(spec):
        remote_ref = spec.split(":")[-1].replace("refs/heads/", "")
        return any(remote_ref.startswith(p) or remote_ref == p.rstrip("/") for p in allow)

    if not refspecs:
        die_deny("`git push` with no explicit refspec is not allowed; push an "
                 "allowed branch explicitly, e.g. `git push origin ns/staging`.")
    for spec in refspecs:
        if not ref_ok(spec):
            die_deny("push target %r is not an allowed ref (allowed: %s). main is "
                     "protected; push to ns/*." % (spec, ", ".join(allow)))
    return "PASS"


def classify(cmd, scope, depth=0):
    """Return ('PASS'|'COMMIT'|'DENY', reason). Never exits (except fail-closed
    parse errors), so it can recurse for shell wrappers."""
    if depth > MAX_DEPTH:
        return ("DENY", "command nests shell interpreters too deeply to analyze.")
    for marker in ENFORCEMENT_MARKERS:
        if marker in cmd:
            return ("DENY", "command references a protected enforcement path (%s)." % marker)
    try:
        tokens = tokenize(cmd)
    except Exception as e:
        return ("DENY", "could not parse command (fail-closed): %s" % e)

    statements, had_operator = split_statements(tokens)

    # redirect targets to protected paths (enforcement paths OR any repo .git/,
    # which is how a pre-commit hook gets planted to inject content past the gate)
    for i, t in enumerate(tokens):
        if t in REDIRECTS and i + 1 < len(tokens):
            tgt = tokens[i + 1]
            if path_in_git_dir(tgt):
                return ("DENY", "redirect writes into a repo's .git/ directory (%s) "
                                "— hooks/config there run during the commit, past "
                                "the gate." % tgt)
            for marker in ENFORCEMENT_MARKERS:
                if marker in tgt:
                    return ("DENY", "redirect writes to a protected path (%s)." % tgt)

    has_commit = False
    commit_argv = None
    other_mutates_index = False
    push_rest = None

    for idx, raw_argv in enumerate(statements):
        if not raw_argv:
            continue
        # A bare shell interpreter reading its script from stdin / a heredoc /
        # a pipe / an input redirect is opaque to the gate — deny it. (Running
        # a script FILE, `bash foo.sh`, is allowed but is a documented
        # limitation: git commands inside that file are subprocesses the hook
        # does not see; the CI-side re-validation is the backstop.)
        assigns, argv = strip_env_prefix(raw_argv)
        if not argv:
            continue
        if argv[0] in INTERPRETERS and shell_c_script(argv) is None:
            has_input_redirect = any(t in ("<", "<<", "<<<") for t in raw_argv)
            non_opt_args = [a for a in argv[1:] if not a.startswith("-") and a not in REDIRECTS]
            if idx > 0 or has_input_redirect or not non_opt_args:
                return ("DENY", "a shell interpreter (%s) reading its script from "
                                "stdin/heredoc/pipe/redirect is opaque to the gate "
                                "and is not allowed." % argv[0])
        if argv[0] == "eval":
            return ("DENY", "`eval` executes dynamically-built commands the gate "
                            "cannot inspect and is not allowed.")
        if argv[0] in DANGER_TOOLS:
            return ("DENY", "privilege/ACL tool '%s' is not allowed." % argv[0])
        if argv[0] in FS_MUTATORS and targets_nightshift_dir(argv):
            return ("DENY", "renaming/removing the .night-shift control directory "
                            "is not allowed (it holds the root-owned scope contract).")
        if argv[0] in WRITE_TOOLS and any(path_in_git_dir(a.split("=", 1)[-1]) for a in argv[1:]):
            return ("DENY", "%s into a repo's .git/ directory is not allowed — a "
                            "hook planted there runs during the commit, past the "
                            "gate." % argv[0])

        # shell wrapper: recurse into a static -c script; deny a dynamic one.
        script = shell_c_script(argv)
        if script is not None:
            if "$(" in script or "`" in script:
                return ("DENY", "shell -c with command substitution is opaque to "
                                "the gate and is not allowed.")
            inner_v, inner_r = classify(script, scope, depth + 1)
            if inner_v == "DENY":
                return ("DENY", "inside `%s -c`: %s" % (argv[0], inner_r))
            if inner_v == "COMMIT":
                if had_operator:
                    return ("DENY", "a commit inside a compound command is not allowed.")
                has_commit = True
                commit_argv = commit_argv or []
            continue

        # scope.yaml deny_commands (token-subsequence match, on the env-stripped argv)
        for needle in scope["deny_commands"]:
            if contiguous_subseq(argv, needle):
                return ("DENY", "matches scope.yaml deny_commands: %s" % " ".join(needle))

        # Find a git command ANYWHERE in the statement. Runners (nice, timeout,
        # nohup, command, exec, stdbuf, setsid, …) just prepend tokens before
        # `git`, so keying on argv[0] alone lets them hide the commit.
        gj = None
        for k in range(len(argv)):
            if is_git(argv[k]) and git_walk(argv[k:]) is not None:
                gj = k
                break
        if gj is None:
            continue
        pre = argv[:gj]
        for r in pre:
            if r in OPAQUE_RUNNERS:
                return ("DENY", "running git through '%s' hides the invocation from "
                                "the gate and is not allowed." % r)
        sub, rest, retarget, bad_c = git_walk(argv[gj:])

        if bad_c:
            return ("DENY", "`git -c %s=…` is not allowed — it can redirect commits "
                            "or hooks past the gate." % bad_c)
        if sub in GIT_PLUMBING:
            return ("DENY", "git history-plumbing '%s' is not allowed." % sub)
        if sub == "config" and any(is_dangerous_c(t) for t in rest):
            return ("DENY", "`git config` of alias.* or core.hooksPath is not "
                            "allowed (it can redirect commits/hooks past the gate).")

        if sub in ("commit", "push"):
            # dangerous env anywhere in the statement (leading + any NAME=VALUE token)
            all_assigns = assigns + [t for t in argv if ENV_ASSIGN_RE.match(t)]
            bad_env = dangerous_env(all_assigns)
            if bad_env:
                return ("DENY", "env var %s can retarget git's index/repo/config; "
                                "not allowed with a git commit/push." % bad_env)
            if retarget:
                return ("DENY", "git %s cannot use %s (it would operate on a repo/"
                                "index the gate does not inspect)." % (sub, "/".join(sorted(set(retarget)))))

        if sub == "commit":
            has_commit = True
            commit_argv = rest
        elif sub == "push":
            push_rest = rest
        elif sub in GIT_NONLANE_COMMIT:
            return ("DENY", "`git %s` is not enabled in this phase (single-lane "
                            "main: stage, then `git commit`)." % sub)
        elif sub in INDEX_MUTATORS:
            other_mutates_index = True
        elif sub not in KNOWN_GIT_SUBCOMMANDS:
            return ("DENY", "unknown/aliased git subcommand '%s' is not allowed — "
                            "an alias could map it to commit/push/rebase. Run the "
                            "underlying git command directly." % sub)

    # --- decide -----------------------------------------------------------
    if has_commit:
        if had_operator:
            return ("DENY", "a `git commit` must be its own Bash call — chaining it "
                            "with && / ; / | lets the index change after the gate "
                            "inspects the tree. Stage first, commit next.")
        if other_mutates_index:
            return ("DENY", "do not combine staging and committing in one command.")
        if has_cmd_subst(cmd):
            return ("DENY", "a `git commit` command may not contain command "
                            "substitution ($(...) / backticks / <(...)) — it can "
                            "stage content AFTER the gate has inspected the tree.")
        ca = commit_argv or []
        if "--amend" in ca:
            return ("DENY", "`git commit --amend` rewrites history; not allowed.")
        if "--no-verify" in ca or "-n" in ca:
            return ("DENY", "`git commit --no-verify` is not allowed.")
        if "-a" in ca or "--all" in ca or "-am" in ca:
            return ("DENY", "`git commit -a/--all` is not allowed; stage explicitly.")
        return ("COMMIT", "")

    if push_rest is not None:
        return (analyze_push(push_rest, scope), "")

    return ("PASS", "")


def main():
    if len(sys.argv) < 3:
        die_deny("classifier invoked without command/scope (fail-closed).")
    try:
        scope = load_scope(sys.argv[2])
    except Exception as e:
        die_deny("scope.yaml is unusable (fail-closed): %s" % e)
    verdict, reason = classify(sys.argv[1], scope, 0)
    out(verdict, reason)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        die_deny("classifier crashed (fail-closed): %s" % e)
