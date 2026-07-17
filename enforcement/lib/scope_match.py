#!/usr/bin/env python3
"""
scope_match.py — decide whether a set of repo-relative paths is within the
scope contract defined by a root-owned scope.yaml.

This is a SECURITY-CRITICAL, fail-closed component. It is the mechanical
enforcement point (night-shift design §5, point 3): whatever the agent wrote
to disk, a path only enters a commit if this matcher says it is in scope.

Contract (scope.yaml):
    allow_paths: [glob, ...]   # a path must match >=1 of these to be in scope
    deny_paths:  [glob, ...]   # a path matching any of these is ALWAYS denied

Decision for each path:
    in_scope  ==  (matches an allow glob)  AND  (matches no deny glob)
Deny always wins. A path matching nothing in allow_paths is OUT of scope.

Glob semantics (gitignore-flavored, path-aware — NOT shell fnmatch):
    *      matches any run of characters except '/'
    ?      matches a single character except '/'
    **     matches any number of path segments (including zero)
    **/    zero or more leading directory segments
    [..]   character class (with [!..] negation, like gitignore)
    trailing '/'   is treated as '/**' (the directory and everything under it)
    a pattern containing NO '/' matches its basename at ANY depth
    a pattern containing '/' is anchored at the repo root

Usage:
    printf '%s\0' path1 path2 | scope_match.py <scope.yaml> --paths0
    scope_match.py <scope.yaml> --path <single-path>
    scope_match.py <scope.yaml> --selftest

Exit codes:
    0  every path is in scope
    1  at least one path is out of scope (offenders + reason on stderr)
    2  internal error / unusable scope.yaml (FAIL CLOSED — caller must block)
"""

import sys
import re


def _die_config(msg):
    sys.stderr.write("scope: FATAL (fail-closed): %s\n" % msg)
    sys.exit(2)


def _translate_body(pat):
    """Translate the glob body (already stripped of anchoring concerns) into a
    regex fragment WITHOUT ^...$ anchors. Path-aware: '*' and '?' never cross
    '/'. '**' spans segments."""
    out = []
    i = 0
    n = len(pat)
    while i < n:
        c = pat[i]
        if c == '*':
            if i + 1 < n and pat[i + 1] == '*':
                # '**'
                j = i + 2
                if j < n and pat[j] == '/':
                    # '**/' -> zero or more leading segments
                    out.append(r'(?:[^/]*/)*')
                    i = j + 1
                    continue
                else:
                    # trailing '**' or '**' glued to non-slash -> anything, incl '/'
                    out.append(r'.*')
                    i = j
                    continue
            else:
                out.append(r'[^/]*')
                i += 1
                continue
        elif c == '?':
            out.append(r'[^/]')
            i += 1
        elif c == '/':
            out.append('/')
            i += 1
        elif c == '[':
            # character class: copy through to the closing ']'
            j = i + 1
            if j < n and pat[j] == '!':
                j += 1
            if j < n and pat[j] == ']':
                # a literal ']' as first class member
                j += 1
            while j < n and pat[j] != ']':
                j += 1
            if j >= n:
                # unterminated class -> treat '[' as a literal
                out.append(r'\[')
                i += 1
                continue
            body = pat[i + 1:j]
            if body.startswith('!'):
                body = '^' + body[1:]
            out.append('[' + body + ']')
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return ''.join(out)


def compile_glob(glob):
    """Compile a scope glob into an anchored, fullmatch regex. Returns None for
    an empty pattern (skipped)."""
    pat = glob.strip()
    if pat == '' or pat.startswith('#'):
        return None
    # A leading '/' anchors to root explicitly; strip it (we anchor anyway).
    explicit_root = pat.startswith('/')
    if explicit_root:
        pat = pat[1:]
    # Trailing slash => directory subtree.
    if pat.endswith('/'):
        pat = pat + '**'
    # Anchoring: gitignore rule — a '/' anywhere (other than a trailing one,
    # already expanded) anchors to root; no '/' => match basename at any depth.
    anchored = explicit_root or ('/' in pat)
    body = _translate_body(pat)
    if anchored:
        full = '^' + body + '$'
    else:
        full = '^(?:.*/)?' + body + '$'
    try:
        return re.compile(full)
    except re.error as e:
        _die_config("bad glob %r -> /%s/ : %s" % (glob, full, e))


def load_scope(path):
    try:
        import yaml
    except Exception as e:  # PyYAML must be present; absence is fail-closed
        _die_config("PyYAML not importable (%s) — install python3-yaml" % e)
    try:
        with open(path, 'r') as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        _die_config("scope.yaml not found at %s" % path)
    except Exception as e:
        _die_config("cannot parse %s: %s" % (path, e))
    if data is None:
        _die_config("scope.yaml is empty: %s" % path)
    if not isinstance(data, dict):
        _die_config("scope.yaml top level must be a mapping: %s" % path)

    def _globs(key):
        v = data.get(key, [])
        if v is None:
            return []
        if not isinstance(v, list):
            _die_config("%s must be a list of strings in %s" % (key, path))
        out = []
        for item in v:
            if not isinstance(item, str):
                _die_config("%s entries must be strings; got %r" % (key, item))
            out.append(item)
        return out

    allow = _globs('allow_paths')
    deny = _globs('deny_paths')
    allow_re = [r for r in (compile_glob(g) for g in allow) if r is not None]
    deny_re = [r for r in (compile_glob(g) for g in deny) if r is not None]
    return allow_re, deny_re


def normalize(path):
    """Normalize a repo-relative path; reject anything suspicious (fail-closed)."""
    p = path.strip()
    if p == '':
        return None
    p = p.replace('\\', '/')          # defensive: no backslash paths
    while p.startswith('./'):
        p = p[2:]
    p = p.lstrip('/')                 # treat as repo-relative
    # Reject traversal / absolute-ish components: these must never be in scope.
    parts = p.split('/')
    if any(seg in ('..',) for seg in parts):
        return '\x00TRAVERSAL\x00' + path   # sentinel -> will be denied
    return p


def classify(path, allow_re, deny_re):
    """Return (in_scope: bool, reason: str)."""
    norm = normalize(path)
    if norm is None:
        return True, 'empty (ignored)'
    if norm.startswith('\x00TRAVERSAL\x00'):
        return False, 'path traversal / unsafe path rejected'
    for r in deny_re:
        if r.match(norm):
            return False, 'matches deny_paths /%s/' % r.pattern
    for r in allow_re:
        if r.match(norm):
            return True, 'matches allow_paths /%s/' % r.pattern
    return False, 'not in any allow_paths (out of scope)'


def check_paths(scope_path, paths):
    allow_re, deny_re = load_scope(scope_path)
    if not allow_re:
        # An empty allow list denies everything. That is a real (if strict)
        # configuration, but almost always a mistake — surface it loudly.
        sys.stderr.write("scope: WARNING allow_paths is empty — everything is out of scope\n")
    offenders = []
    for p in paths:
        ok, reason = classify(p, allow_re, deny_re)
        if not ok:
            offenders.append((p, reason))
    if offenders:
        sys.stderr.write("scope: %d path(s) OUT OF SCOPE:\n" % len(offenders))
        for p, reason in offenders:
            sys.stderr.write("  - %s  (%s)\n" % (p, reason))
        return 1
    return 0


def _read_paths0():
    data = sys.stdin.buffer.read()
    return [p.decode('utf-8', 'surrogateescape')
            for p in data.split(b'\x00') if p != b'']


def _selftest():
    """Inline smoke test of the matcher semantics. Full tests live in
    tests/test_scope_match.py; this is a fast sanity gate."""
    cases = [
        # (allow, deny, path, expected_in_scope)
        (['apps/operator/**'], [], 'apps/operator/x.ts', True),
        (['apps/operator/**'], [], 'apps/operator/a/b/c.ts', True),
        (['apps/operator/**'], [], 'apps/operatorX/x.ts', False),
        (['apps/operator/**'], [], 'apps/other/x.ts', False),
        (['**'], ['.github/workflows/**'], '.github/workflows/ci.yml', False),
        (['**'], ['**/.env*'], 'apps/x/.env', False),
        (['**'], ['**/.env*'], 'apps/x/.env.local', False),
        (['**'], ['**/.env*'], 'apps/x/environment.ts', True),
        (['**'], ['scope.yaml'], '.night-shift/scope.yaml', False),
        (['**'], ['scope.yaml'], 'scope.yaml', False),
        (['**'], ['hooks/**'], 'hooks/gate.sh', False),
        (['**'], ['infra/'], 'infra/main.tf', False),
        (['packages/**/*.ts'], [], 'packages/ui/src/x.ts', True),
        (['packages/**/*.ts'], [], 'packages/ui/src/x.js', False),
        (['packages/**/*.ts'], [], 'packages/a.ts', True),
        (['**'], [], '../outside/x.ts', False),          # traversal denied
        (['src/**'], ['src/secrets/**'], 'src/secrets/key.ts', False),
        (['src/**'], ['src/secrets/**'], 'src/app/main.ts', True),
    ]
    failed = 0
    for allow, deny, path, expected in cases:
        allow_re = [compile_glob(g) for g in allow]
        deny_re = [compile_glob(g) for g in deny]
        ok, reason = classify(path, allow_re, deny_re)
        if ok != expected:
            failed += 1
            sys.stderr.write("SELFTEST FAIL: allow=%r deny=%r path=%r got=%s want=%s (%s)\n"
                             % (allow, deny, path, ok, expected, reason))
    if failed:
        sys.stderr.write("scope selftest: %d FAILED\n" % failed)
        sys.exit(1)
    sys.stdout.write("scope selftest: all %d passed\n" % len(cases))
    sys.exit(0)


def main(argv):
    if len(argv) >= 2 and argv[1] == '--selftest':
        _selftest()
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        sys.exit(2)
    scope_path = argv[1]
    mode = argv[2]
    if mode == '--paths0':
        paths = _read_paths0()
    elif mode == '--path' and len(argv) >= 4:
        paths = [argv[3]]
    else:
        sys.stderr.write("scope: bad invocation\n")
        sys.exit(2)
    if not paths:
        # Nothing to check -> vacuously in scope (an empty commit is caught elsewhere).
        sys.exit(0)
    sys.exit(check_paths(scope_path, paths))


if __name__ == '__main__':
    main(sys.argv)
