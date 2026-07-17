# Night Shift (enforced fork)

> Step away for the night.
>
> Claude Code keeps grinding — inside walls it can't talk its way past.
>
> Wake up to planned, committed, reviewed work on a `main` that never went red.

Night Shift turns a Claude Code session into an autonomous development agent. You
approve one thing — an **objective** — then walk away. The agent plans,
implements, and commits work on an `ns/*` branch.

This is the **enforced, resident fork**. In the original, every safety rule was an
instruction the agent was *trusted* to follow; the pre-commit "gate" only checked
that a review file existed — and the same agent wrote that file, so it could forge
it. Here the load-bearing rules are **enforced by root-owned Claude Code hooks the
agent cannot see, edit, or disable** — and by GitHub server-side protection it
never has credentials to touch. See [ARCHITECTURE.md](ARCHITECTURE.md) for the
full design.

```
═══════════════════════════════════════════════════════════════
  🌙  NIGHT SHIFT ENGAGED  🌙
═══════════════════════════════════════════════════════════════

  You can step away now. I'll take it from here.

  Run ID:      2026-04-19-2318
  Branch:      ns/harden-api-errors (from d9aad96)
  Objective:   Harden error handling across the API layer —
               standardize error responses, add retries to
               outbound HTTP, and cover the gaps with tests.
  Scope:       .night-shift/scope.yaml (root-owned)
  Handoff:     .night-shift/runs/2026-04-19-2318/handoff.md

  Sleep well. 🌙
═══════════════════════════════════════════════════════════════
```

<a href="https://www.loom.com/share/6bcfdd2579c74de5bdad595c686fa547" target="_blank" rel="noopener noreferrer"><img src="https://github.com/user-attachments/assets/1a95844b-51ca-4944-8918-2a49c3f3e83a" alt="Watch the Night Shift demo on Loom"></a>


## Why

Long-horizon autonomous coding agents fail in predictable ways: they hallucinate
progress, paper over broken tests, escalate scope into unrelated refactors,
sometimes push to `main`. Most "run it overnight" setups are one LLM grading its
own homework — and the guardrails are prompts the same model can rationalize past.

This fork makes the guarantees **structural**, pinned to three layers:

- **L1 — GitHub server-side.** `main` is protected: only a promote bot can push
  to it, and only by fast-forwarding to an already-CI-green commit. **`main` going
  red is not a behavior to prevent — it's structurally impossible.** The agent has
  no credential that can touch `main`.
- **L2 — root-owned local hooks.** A Claude Code `PreToolUse` hook gates every
  commit. It runs `codex review` **itself** on the staged diff and blocks on any
  finding. Exit-2 blocks **even under `--dangerously-skip-permissions`**, and the
  hooks live in root-owned files the agent can't modify — so this is a wall, not a
  request.
- **L3 — the prompt.** [SKILL.md](SKILL.md) / [INVARIANTS.md](INVARIANTS.md) still
  describe good work and the planning gates that have no tool-call to hook. But an
  L3 rule is a preference; the four that matter (commit review, scope, push,
  main-green) were moved down to L1/L2.

## How it works

Each task: Claude plans → Claude implements → Claude stages and commits. The
commit is where enforcement lives. When the agent runs `git commit`, the L2 hook
runs a gate that checks, cheapest-first so quota is spent last:

1. **clean-except-staged** — the whole working tree must be staged, so what Codex
   reviews is exactly what gets committed. (`git add x && git commit` in one
   command is denied — staging after the gate's snapshot would be a TOCTOU
   bypass.)
2. **scope** — every staged path must satisfy the root-owned `scope.yaml`
   (`allow_paths` / `deny_paths` globs; deny always wins). Out-of-scope files
   never enter history.
3. **secrets** — `gitleaks` scans the staged diff; any hit blocks.
4. **fast checks** — the project's lint/typecheck.
5. **Codex review** — `codex review` at high effort on the staged diff; any
   `[P1]`/`[P2]` blocks. **There is no review file to forge — the gate runs Codex
   live and never reads anything the agent wrote.**

The **full test suite runs in staging CI**, which gates promotion to `main` — so
the hook stays fast, and "every commit on `main` is green" is guaranteed by CI,
not by the agent. On a block, the reason lands on stderr; the agent fixes and
commits again. Each attempt re-runs Codex (no cached verdicts — a cache the agent
could write would be a forgery vector).

**Codex unavailable ⇒ commits stop.** The gate **fails closed**: a Codex crash,
timeout, or absence blocks the commit. There is no self-review fallback for code
review — that's what makes review *mandatory* rather than *degradable*. (The
planning gates below still fall back to a marked self-adversarial review; end-of-
shift consensus does not.)

Work is organized as:

- **Objective** — the one thing you approve.
- **Key Results** — deliverables the agent proposes iteratively, each Codex-gated
  for "does this serve the objective, or over-engineer it?"
- **Tasks** — independently committable units under each key result.

The shift ends only when the agent proposes "we're done" **and** Codex agrees on a
re-review of the full objective and key-result history.

## Requirements

- **No passwordless sudo for the operator.** L2 assumes the agent (running as your
  user) cannot become root to edit the root-owned hooks. `sudo -n true` must
  prompt for a password. This is step 0 of setup.
- **Claude Code** launched with `--dangerously-skip-permissions` in the target
  repo. The hooks enforce *because* of bypass mode, not despite it.
- **OpenAI [Codex CLI](https://github.com/openai/codex)** (`gpt-5.5` via ChatGPT
  auth) — **required**, not optional. Commits are blocked when Codex can't run.
- **Git**, plus `gitleaks`, `python3-yaml`, and `jq` — all installed for you by
  `enforcement/install.sh`.
- **GitHub Pro** on the (private) repo for the L1 branch protection that makes
  `main`-stays-green real. See [promote/branch-protection.md](promote/branch-protection.md).

## Install

This is a personal fork — it is **not** installed from a marketplace, and it does
**not** auto-update from any upstream. Enforcement is set up with the installer:

```bash
sudo bash enforcement/install.sh system            # deps, root-owned hooks, managed settings
sudo /opt/night-shift/enforcement/install.sh onboard /path/to/repo --ntfy-topic <topic>
```

Full ordered runbook (including removing passwordless sudo, tailoring
`scope.yaml`, and the GitHub L1 setup): **[enforcement/SETUP.md](enforcement/SETUP.md)**.
The GitHub-side staging→promote and branch-protection steps are in
**[promote/branch-protection.md](promote/branch-protection.md)**.

## Quickstart

In the onboarded repo, launch Claude Code with `--dangerously-skip-permissions`,
on an `ns/*` branch (Night Shift switches you off `main` if needed). Then:

```
/night-shift
```

The skill asks, one question at a time: bypass-permissions confirmation, branch
choice, uncommitted-changes handling, and an **objective** (or type `propose`).
After you confirm, the `NIGHT SHIFT ENGAGED` banner fires. Enforcement is active
automatically because a root-owned `scope.yaml` is present and the session is in
bypass mode — your ordinary interactive sessions and any non-onboarded repo are
unaffected.

To stop early: say `stop night shift`. Retriggering during an active shift gives a
Stop/Resume/Abandon prompt.

## In the morning

The authoritative artifact is:

```
.night-shift/runs/<RUN_ID>/handoff.md
```

It summarizes what shipped per key result, per-task commit hashes, how many gate
rounds each took, decisions the agent made without you, and items needing
attention. Review the `ns/*` branch like any other PR; CI-green commits promote to
`main` on their own.

## Run folder

```
.night-shift/
├── scope.yaml            The scope contract (ROOT-OWNED; only you edit it, with sudo)
└── runs/<RUN_ID>/
    ├── state.json        Source of truth for the run
    ├── handoff.md        Human-facing summary
    └── key-results/<KR>/
        ├── codex-approval.txt      Planning gate: was this KR worth doing?
        └── decomp-adversarial.txt  Planning gate: are these the right tasks?
```

Code review is **not** a file here — the commit hook runs Codex live and ignores
anything the agent writes. The planning-gate artifacts above remain (they review
documents, which no tool-call can hook). Previous runs are never modified.

## Safety model

| Failure mode | Layer | How it's prevented |
| --- | --- | --- |
| `main` goes red | **L1** | Only the promote bot pushes `main`, only by ff-ing an already-CI-green commit — the agent has no credential for `main` |
| Agent pushes to `main` / force-pushes | **L1 + L2** | Branch-protection push allowlist; L2 also denies non-`ns/*` and force pushes |
| Agent forges a review to skip Codex | **L2** | The gate runs `codex review` itself and never reads any agent-written file |
| Agent skips review "to save time" | **L2** | No commit is possible without the gate; `--no-verify` and chained `git add && commit` are denied |
| Agent grades its own homework | **L2** | Codex is a separate model/process; Codex-down fails closed, it does not fall back to self-review |
| Agent commits out-of-scope files | **L2** | Staged-path scope check against root-owned `scope.yaml`; `.github/workflows`, secrets, migrations, infra are denied |
| Agent commits a secret | **L2** | `gitleaks` scans the staged diff; any hit blocks |
| Agent commits broken code | **L2 + CI** | Fast checks in the gate; the full suite runs in staging CI, which gates promotion |
| Agent decides it's done prematurely | **L3 + L2** | Dual consensus (Codex must agree); the Stop hook refuses to stop without the consensus artifact |
| Your own work gets clobbered | **L3** | Drift check before every write; an external HEAD change stops the run |
| Failed task leaves repo dirty | **L3** | Per-task scoped rollback to the task's `start_commit` |
| Scope creeps beyond the objective | **L3** | Every key result is Codex-gated on "does this serve the objective?" |

The full ruleset is in [INVARIANTS.md](INVARIANTS.md); the execution spec is in
[SKILL.md](SKILL.md); the enforcement design and its honest limits are in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Commands

| Trigger | Action |
| --- | --- |
| `/night-shift` | Start a shift, or Stop/Resume/Abandon an active one |
| `start night shift`, `going to sleep`, `take over for the night` | Same as `/night-shift` |
| `stop night shift`, `end night shift`, `wrap up` | Run the end procedure on the active shift |

## Limitations

- **The honest ceiling.** Only **L1 (branch protection) is truly zero-bypass** —
  it's the one guarantee that holds regardless of what the agent does. L2 raises
  the *cost* of a bypass from "convenient shortcut" to "deliberate, multi-step
  malice" (and depends on the operator having no passwordless sudo), but under
  `--dangerously-skip-permissions` it is not mathematically absolute. Put what you
  care about most behind L1.
- **Review depth is bounded by Codex.** The gate guarantees Codex *runs* and that
  a `[P1]`/`[P2]` blocks — it does not guarantee Codex catches everything.
  Attacker-controlled text in the diff is a residual prompt-injection surface;
  your test coverage is the backstop for anything Codex misses.
- **Time cap is a fallback, not the headline.** An 8-hour per-session cap still
  exists, but the resident supervisor (Phase 2 — nights 22:00–07:00, budget and
  circuit-breaker, session rotation) is what governs "keep working." Wall-clock,
  not compute: a suspended machine still counts against the cap.
- **No user questions mid-shift.** Ambiguities are resolved by the agent and
  logged in `state.json.decisions_made` — review them in the handoff.
- **Single session per repo (Phase 1).** External commits to the branch trigger
  drift protection and stop the run cleanly; worktree parallelism is Phase 3.
