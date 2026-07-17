# Night Shift (resident fork) — enforcement architecture

This fork turns the original night-shift skill from a **pure-prompt spec** (every
rule was an instruction the agent was trusted to follow) into a **three-layer
enforced system**. The load-bearing invariants no longer depend on the agent
choosing to obey them.

> The original's own author acknowledged the gap: a night-shift agent could write
> a plausible-looking `code-review.txt` and walk past the "hard gate", because the
> gate only checked that a file existed — the same agent wrote the file. This fork
> closes that by having the *gate run the review itself*, on the commit path, from
> root-owned code the agent cannot edit.

## The three layers

```
L1  GitHub server-side (branch protection + required checks)   ← agent cannot touch it
L2  local hooks, run from root-owned /opt/night-shift           ← block even under
                                                                  --dangerously-skip-permissions
L3  SKILL.md / INVARIANTS.md prompt                             ← guidance only
```

Every enforced invariant is pinned to the **lowest layer that can actually hold it**:

| Invariant | Layer | Mechanism |
|---|---|---|
| `main` never goes red | **L1** | promote bot is the only pusher to `main`; it only ff's an already-CI-green SHA |
| Agent can't push to `main` | **L1** + L2 | branch protection push allowlist; L2 denies non-`ns/*` pushes as backup |
| Every commit is Codex-reviewed | **L2** | commit hook runs `codex review` itself; there is no agent-written file to forge |
| Writes stay in scope | **L2** | staged-file scope check in the commit hook (the hard wall) + file-tool scope hook (fast path) |
| Secrets never committed | **L2** | gitleaks on the staged diff, fail-closed |
| Codex down ⇒ commits stop | **L2** | fail-closed: any Codex crash/timeout/absence blocks the commit |
| KR approval / decomposition review | **L3** | prompt + on-disk artifact (no tool-call trigger exists to hook; forging it still can't pass the commit gate) |
| End-of-shift consensus | **L3** + L2 | Codex procedure in prompt; Stop hook refuses to stop without the artifact present |

The design principle: **an invariant that lives only in L3 is a preference, not a
guarantee.** The point of the fork is to move the four load-bearing ones (commit
review, scope, push, main-green) down to L1/L2.

## Why L2 can't be bypassed (and where it can)

Verified against Claude Code docs (v2.1.205) during the build:

- A `PreToolUse` hook returning **exit code 2 blocks the tool call even under
  `--dangerously-skip-permissions`**. This is the keystone: autonomous mode does
  not disable the gate.
- Hook config **hot-reloads** (it is not snapshotted at startup), so file
  ownership — not a startup snapshot — is the only real defense. Therefore the
  hooks live in **`/etc/claude-code/managed-settings.json`** (root-owned) with
  **`allowManagedHooksOnly: true`**, which makes Claude Code **ignore all
  user/project/local hooks**. The agent cannot register a competing hook or
  disable ours from any settings file it can write.
- The hook *scripts* live in **root-owned `/opt/night-shift/`**, copied there by
  the installer — the in-repo `enforcement/` tree is only source. Editing the
  repo copy does nothing; the running hooks are the `/opt` copy.
- The **scope contract** (`<repo>/.night-shift/scope.yaml`) is root-owned, and its
  directory carries the **sticky bit (1775)** so the agent can create run files
  under `runs/` but **cannot delete the root-owned `scope.yaml`** (sticky = you may
  only delete files you own).
- The authoritative **"is this repo enforced?" signal is a root-owned registry**
  (`/opt/night-shift/onboarded.list`), NOT the presence of `.night-shift/`. The
  agent owns the repo root, so it *can* rename the whole `.night-shift` directory
  — but that does not disable enforcement: the repo is still registered, its
  scope.yaml is now unreadable, and the gate **fails closed** (blocks every tool
  call + alerts) rather than passing through. Renaming `.night-shift` is also
  denied outright by the command classifier. (This closes a bypass an adversarial
  review found — see "Red-team" below.)

The one prerequisite that makes all of this real: **the operator must not have
passwordless sudo.** During the build we found this machine *did* (`sudo -n true`
succeeded), which would let the agent `sudo` its way past every root-owned file.
Removing `NOPASSWD` is step 0 of the install. Phase 2 replaces this with a
container where the agent has no sudo at all and only holds the `ns/*` push
credential.

**Honest ceiling (from the original's §8, still true):** under
`--dangerously-skip-permissions`, everything that is *not* root-owned or
server-side is theoretically reachable by a determined agent. The layering makes
bypass require deliberate, multi-step malice rather than a convenient shortcut —
and **L1 (branch protection) is the only layer with a zero-bypass surface**, which
is exactly why "main stays green" — the thing the operator cares about most — is
the one guarantee that is actually absolute.

**The subprocess ceiling (important).** PreToolUse hooks fire on the *Bash tool
call*, not on processes that call spawns. So a `git commit` **inside** an invoked
script or tool — `bash build.sh`, `npm run x`, `make`, a `python` script — is a
subprocess the commit gate never sees, and it can commit unreviewed, out-of-scope,
or secret-bearing content locally. The classifier denies the *transparent* evasions
(`sh -c`, stdin/heredoc-fed shells, `eval`, env/alias/retarget tricks) but cannot
see inside an opaque script file. The correct defense is **server-side
re-validation at the promotion boundary**: the promote workflow (which runs from
`main`, so the agent cannot alter it) should re-run the scope check and secret scan
on the `ns/staging → main` diff, using rules read from `main`, and refuse to
promote on any violation. That makes scope + secret enforcement zero-bypass for
anything that reaches `main`, regardless of how the commit was created locally.
`promote/branch-protection.md` describes wiring this in; until it is in place, the
local commit gate is best-effort against a deliberately-evasive agent (it fully
covers the ordinary path where the agent runs `git commit` as a tool call).

## The commit gate (L2 core)

On the commit path, `gate-bash.sh` classifies the command; a gate-able
`git commit` runs `commit_gate.sh`, cheap-to-expensive so quota is spent last:

1. **clean-except-staged** — the working tree must be fully staged, so the review
   target equals the commit target. (No `git add x && git commit` in one call —
   the classifier denies it, because staging after the gate's snapshot is a TOCTOU
   bypass.)
2. **staged scope check** — `git diff --cached --name-only --no-renames` (both
   rename endpoints, so nothing is smuggled across the boundary) piped through the
   glob matcher. This is the hard wall: whatever the agent wrote to disk, only
   in-scope paths enter history.
3. **secret scan** — gitleaks on the staged diff; any hit blocks.
4. **fast checks** — the project's lint/typecheck (from `scope.yaml`); fail here
   and Codex is never called.
5. **Codex review** — `codex review --uncommitted` at high effort. **Fail-closed
   parse**: require a completion banner and a verdict turn; isolate the verdict
   (text after the last `codex` turn label, so echoed diffs can't false-match);
   block on any `[P1]`/`[P2]`; block on any non-zero exit, timeout, or missing
   verdict. Codex unavailable ⇒ commits stop — that is what makes review
   *mandatory* rather than *degradable*.
6. **full checks** — usually empty; the full test suite runs in staging CI (which
   gates promotion), keeping the hook under its timeout.

There is deliberately **no verdict cache**: any agent-writable cache of "this diff
is clean" becomes a forgery vector. Each commit attempt re-runs Codex. The cheap
pre-checks (steps 1–4) are the only quota economy.

## File inventory

```
enforcement/
  lib/
    scope_match.py      gitignore-flavored path matcher (fail-closed). Tested: 39 cases.
    classify_cmd.py     shell-aware command classifier (shlex, operator-aware;
                        newline/subst/wrapper/runner/env/alias/basename-git/
                        .git-write/hooksPath hardened). Tested: 144 cases.
    resolve.py          which enforced repo a call belongs to (registry ancestor
                        match / outermost scope.yaml; cwd + cd/-C/abs-path aware).
    common.sh           fail-closed EXIT trap, self-location, JSON/YAML readers, ntfy, enforce-active
    commit_gate.sh      the gate pipeline above
  hooks/
    gate-bash.sh        PreToolUse:Bash        — classify + route to commit gate
    gate-write.sh       PreToolUse:Edit|Write|… — fast scope check on the target path
    gate-stop.sh        Stop                    — resident-mode continue-until-consensus (fail-OPEN, dormant in Phase 1)
    gate-config.sh      ConfigChange            — freeze settings edits during a shift (defense-in-depth)
    session-start.sh    SessionStart            — inject resume context (resident mode)
  scope.yaml.template   the machine-readable contract (per project)
  managed-settings.template.json   hook wiring + allowManagedHooksOnly
  install.sh            root installer: system / onboard / verify
  verify-integrity.sh   sha256 manifest check (supervisor pre-launch / manual)
  tests/                test_scope_match.py · test_classify.py · test_gates_integration.sh
promote/
  promote.yml           ns/staging green → ff main (L1)
  branch-protection.md   operator setup for the L1 guarantee
```

## Enforcement-active gate

The hooks are installed **globally** (managed settings), so they fire on every
Claude Code session on the machine. They enforce only when **both** hold:

- the repo (or an ancestor) is in the root-owned registry
  `/opt/night-shift/onboarded.list` — the installer adds it at `onboard` time
  (a registered repo whose scope.yaml is missing fails **closed**, not open), and
- the session is autonomous (`permission_mode == bypassPermissions`) or
  `NIGHT_SHIFT_ENFORCE=1`.

So the operator's ordinary interactive sessions — and every non-onboarded repo —
pass straight through. An agent cannot escape by leaving bypass mode: doing so
re-enables a permission prompt on every action, which stalls the autonomy it is
trying to preserve. The agent also cannot clear the env var on the hook process
(the hook inherits Claude Code's environment, not the agent's shell).

## Red-team

The gates were hardened across **four adversarial red-team rounds** that
empirically attacked each component; 14 bypasses were found and closed, each with
a regression test in `enforcement/tests/`:

- **Detection evasion** — commit/push smuggled via newline-separated statements
  (shlex doesn't split on `\n`); `git -c alias.X=commit`; `sh -c`/`bash -c`/`eval`
  and stdin/heredoc-fed shells; `env NAME=val` and runner prefixes
  (`nice`/`timeout`/`nohup`/`command`/`exec`/`xargs`/`find`) hiding the commit;
  `git` spelled as a path (`/usr/bin/git`, `./git`) or a pre-existing commit
  **alias** (`git ci`) — closed by basename-matching `git` and denying
  unknown/aliased subcommands.
- **TOCTOU** — command substitution in a commit (`git commit -m "$(git add evil)"`)
  staging after the gate's snapshot; and a **repo-local `.git/hooks/pre-commit`**
  that fires during the real commit (after the gate approves) and `git add -f`s
  out-of-scope content. The latter is closed by a gate-time check of the effective
  hooks dir (plus `.git/`-write and `core.hooksPath` denials, and onboard pointing
  `core.hooksPath` at a root-owned empty dir).
- **Retargeting** — `GIT_INDEX_FILE`/`--git-dir`/`-C` pointing the commit away
  from what the gate inspects.
- **Tamper / resolution** — `mv .night-shift` disabling enforcement (registry +
  fail-closed + classifier deny); nested `git init` shadowing the onboarded root
  (walk to outermost); cwd outside the registered repo (resolve from the
  command's `cd`/`-C`/absolute targets too); empty/unusable scope.yaml (fail
  closed in both the classifier and the matcher).
- **Scope / parser** — a symlinked directory redirecting a write out of the repo
  (realpath escape check); and a lone `codex` line echoed after real findings
  shifting the verdict window (anchor on the FIRST verdict turn, scan to end).

Residuals, documented and accepted: **subprocess-hidden commits** (see the
subprocess ceiling above — a `git commit` inside `bash script.sh` / `npm run` /
`make`, or an inline interpreter one-liner like `python3 -c "os.system('git
commit')"`; the classifier screens *shell* interpreters but not `python`/`perl`/
`node` one-liners, since those have heavy legitimate use — the promote-side
re-validation is the intended backstop). Also low: gate-write resolves
enforcement from cwd for the realpath escape check, so a symlinked-dir write from
a cwd *also* outside the repo is best-effort; and a linked-worktree has its own
index, but a commit from the registered root uses the root's index and `-C`/`cd`
variants are already caught — the commit gate's staged-scope check remains the
backstop for anything that reaches history.

## What changed from the original prompt (L3)

The voluntary "MUST run this bash to review / validate / gate" sections are
removed — the hooks do that now. The agent is told: *the gate will stop you; when
it does, read the reason on stderr and fix it.* The parts with no tool-call
trigger (KR approval, decomposition review, end-of-shift consensus) stay in the
prompt as procedure, with their artifacts checked where a hook can (the Stop hook
for consensus). The original's genuinely valuable, hard-won rules are kept
verbatim: append-only rejected proposals, the five forbidden "done" rationales,
`state.json` passed to Codex verbatim, targeted `git add`. The upstream
self-update (which pointed at `ppuliu/night-shift` and would silently revert this
fork) is removed.

## Verification evidence (from the build)

- Claude Code: exit-2 deny under bypass **confirmed**; `allowManagedHooksOnly`,
  `ConfigChange`, `SessionStart` context injection, `stop_hook_active` **confirmed**.
- Codex `0.142.2` (`gpt-5.5`, ChatGPT auth): `codex review --uncommitted` exits 0
  **even with `[P1]` findings** (⇒ parse markers, not exit code); a clean review
  ends with a `codex` verdict turn and no markers. A real clean in-scope change
  ran through the full gate in **17s → allow**.
- GitHub: classic branch protection on private repos **requires Pro**; rulesets
  require Team/Enterprise — so Pro + classic protection is the chosen L1.
- Tests: scope matcher 39/39, classifier 144/144, gate integration 42/42 (fake
  Codex: clean→allow, P1→block, crash→block, no-verdict→block, out-of-scope→block,
  unstaged→block, passthrough in interactive mode; plus symlink-escape, registry
  fail-closed, cwd-escape, empty-scope, pre-commit-hook, and verdict-window
  cases). Four adversarial red-team rounds found 14 bypasses; all are fixed with
  regression tests. Run `bash enforcement/tests/run-all.sh` (225 tests).
```
