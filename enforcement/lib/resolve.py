#!/usr/bin/env python3
"""
resolve.py — decide which enforced night-shift repo a tool call belongs to.

Usage:  resolve.py <registry> <command> <path> [more paths...]
Prints: "<root>\t<state>"  where state is one of enforced | missing | none.

Candidates considered: every <path> given, PLUS any repo-referencing paths
extracted from <command> (targets of `cd`, `git -C`, `--git-dir=`,
`--work-tree=`, and absolute-path arguments). This closes the cwd-escape: even
if the session cwd is outside a registered repo, a command that `cd`s or `-C`s
into one is still resolved to that repo.

Registry mode (registry file exists): a candidate resolves to the longest
registry entry E such that realpath(candidate) is E or under E. A registered
root whose scope.yaml is unreadable yields 'missing' (fail closed).

No-registry mode (dev/test): a candidate resolves to the OUTERMOST ancestor dir
containing .night-shift/scope.yaml (so a nested `git init` cannot shadow the
onboarded root).
"""
import os
import re
import sys
import shlex

ENV_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def candidates_from_command(cmd):
    out = []
    if not cmd:
        return out
    try:
        toks = shlex.split(cmd)
    except Exception:
        return out
    for i, t in enumerate(toks):
        if t in ("cd", "-C") and i + 1 < len(toks):
            out.append(toks[i + 1])
        elif t.startswith("--git-dir="):
            out.append(t.split("=", 1)[1])
        elif t.startswith("--work-tree="):
            out.append(t.split("=", 1)[1])
        elif t.startswith("/"):
            out.append(t)
    return out


def registered_root(path, entries):
    try:
        rp = os.path.realpath(path)
    except Exception:
        return None
    best = None
    for e in entries:
        er = os.path.realpath(e)
        if rp == er or rp.startswith(er + os.sep):
            if best is None or len(er) > len(best):
                best = er
    return best


def outermost_scope_root(path):
    cur = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path))
    found = None
    while True:
        if os.path.isfile(os.path.join(cur, ".night-shift", "scope.yaml")):
            found = cur                      # keep climbing → outermost wins
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return found


def main():
    if len(sys.argv) < 3:
        sys.stdout.write("\tnone")
        return
    registry = sys.argv[1]
    command = sys.argv[2]
    cands = list(sys.argv[3:]) + candidates_from_command(command)

    use_registry = bool(registry) and os.path.isfile(registry)
    entries = []
    if use_registry:
        try:
            entries = [l.strip() for l in open(registry) if l.strip()]
        except Exception:
            entries = []

    best_root, best_state = None, "none"
    for p in cands:
        if not p:
            continue
        if use_registry:
            root = registered_root(p, entries)
            if root:
                scope = os.path.join(root, ".night-shift", "scope.yaml")
                state = "enforced" if os.access(scope, os.R_OK) else "missing"
                # prefer enforced; otherwise take the first registered root found
                if best_state != "enforced" and (best_root is None or state == "enforced"):
                    best_root, best_state = root, state
        else:
            root = outermost_scope_root(p)
            if root:
                best_root, best_state = root, "enforced"

    sys.stdout.write("%s\t%s" % (best_root or "", best_state))


if __name__ == "__main__":
    main()
