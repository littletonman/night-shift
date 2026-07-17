# Night Shift (resident fork) — Phase 1 operator runbook

Phase 1 stands up the enforcement layers (L2 local hooks + L1 GitHub-side) so an
autonomous run is gated whether or not the agent cooperates. Do these in order.
Steps marked **[you]** need your hands (password, billing, a login); the rest is
mechanical.

## 0. [you] Remove passwordless sudo — do this FIRST

The whole L2 guarantee assumes the agent (running as your user) cannot become
root. During setup we found this machine has passwordless sudo, which would let
the agent edit the "root-owned" hooks. Fix it:

```bash
sudo -n true 2>&1 && echo "PASSWORDLESS SUDO IS ON — fix before continuing"
sudo visudo        # remove the NOPASSWD entry for your user, then save
sudo -n true       # must now PROMPT for a password (or fail)
```

Until `sudo -n true` fails, the local enforcement is theater. (Phase 2 replaces
this with a container where the agent has no sudo at all.)

## 1. Install the system enforcement  (root)

From this repo:

```bash
sudo bash enforcement/install.sh system
```

This installs `jq` + `python3-yaml` + `gitleaks`, copies the hooks to root-owned
`/opt/night-shift/`, writes `/etc/claude-code/managed-settings.json` (with
`allowManagedHooksOnly` — the agent cannot register competing hooks), and records
a sha256 integrity manifest.

## 2. Onboard the target repo  (root)

```bash
sudo /opt/night-shift/enforcement/install.sh onboard /path/to/repo --ntfy-topic night-shift-<random>
```

Creates a **root-owned** `<repo>/.night-shift/scope.yaml` in a sticky-bit
directory (the agent can create run files but cannot delete the contract),
and gitignores `.night-shift/`.

## 3. [you] Tailor the scope contract  (root edit)

```bash
sudo $EDITOR /path/to/repo/.night-shift/scope.yaml
```

Set at minimum:
- **allow_paths / deny_paths** — what the agent may touch. Keep
  `.github/workflows/**`, `.claude/**`, `.night-shift/**`, `**/.env*`, infra, and
  DB migrations in deny_paths.
- **gate.fast_checks** — the project's REAL lint/typecheck commands (these run in
  the commit gate before Codex; e.g. `npm run lint`, `mypy src`, `make lint`).
- **notify.ntfy_topic** — already set if you passed `--ntfy-topic`.
- **push.enabled** — leave `false` until step 6 (Mode B) is done.

## 4. [you] Fix the GitHub CLI token

Setup found the `gh` keyring token invalid:

```bash
gh auth status      # if it complains, refresh:
gh auth refresh
```

## 5. Verify the install

```bash
bash enforcement/tests/run-all.sh          # full test suite (no Codex quota)
sudo /opt/night-shift/enforcement/install.sh verify   # integrity manifest check
```

## 6. [you] Stand up the L1 guarantee (Mode B: staging → promote)

This is the only zero-bypass layer — it makes "main goes red" structurally
impossible. Full steps in **`promote/branch-protection.md`**. In short:
GitHub Pro on the private repo → branch protection on `main` (required CI check +
push restricted to the promote bot + block force-push + include admins) →
`PROMOTE_TOKEN` secret → install `promote/promote.yml`. Then set
`push.enabled: true` and configure the agent's `ns/*`-only push credential.

## 7. [you] Set up the phone alert channel

Install the **ntfy** app (iOS/Android), subscribe to the topic you chose in step
2. Test it: `curl -d "night-shift test" ntfy.sh/<your-topic>`.

## 8. Acceptance smoke tests (the guarantees, end to end)

In an onboarded repo, with `--dangerously-skip-permissions`:

- **Out-of-scope commit is rejected.** Stage a file under a deny path → the gate
  blocks the commit.
- **Codex-down blocks commits.** Temporarily break Codex (e.g. `PATH` without it,
  or log out) → a commit is blocked fail-closed (do NOT leave it broken).
- **Push to main is refused.** `git push origin main` → denied by L2; and after
  step 6, refused by GitHub server-side even if L2 were bypassed.
- A hand-written `.night-shift/.../code-review.txt` has **no effect** — the gate
  runs Codex itself and never reads it.

## 9. Launch a shift

```bash
cd /path/to/repo
claude --dangerously-skip-permissions
# then: /night-shift
```

The hooks enforce automatically because (a) a root-owned `scope.yaml` is present
and (b) the session is in bypass mode. Your ordinary interactive sessions and any
non-onboarded repo are unaffected.

---

**What's Phase 2** (not in this runbook): the resident supervisor (systemd,
22:00–07:00 window, crash/quota/circuit-breaker handling, session rotation), the
container that removes the agent's sudo entirely, and worktree parallelism. The
Stop hook and `session-start.sh` are already installed but dormant until a
`.night-shift/RESIDENT` marker (which the supervisor will manage) exists.
