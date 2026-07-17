# Night Shift Invariants (read before every task)

These rules are NON-NEGOTIABLE. Context compaction is not an excuse. "Time
efficiency" is not an excuse. "Simple change" is not an excuse.

In this fork, the code-review, scope, secret, and push rules are enforced by
root-owned hooks you cannot see or disable (see ARCHITECTURE.md). The invariants
below are still your responsibility to follow — but several of them are now
*walls*: if you try to skip them, the tool call is simply denied.

1. **Drift check before every write** (git mode). If it fails, stop the run.

2. **Key-result proposal requires Codex approval** (Outer B). Every key
   result must pass an adversarial review asking "is this worth implementing
   toward the objective, or would it over-engineer / over-optimize?" before
   decomposition begins. Output MUST be saved to
   `key-results/<KR>/codex-approval.txt`. (This planning gate has no automatic
   enforcement — run it honestly.)

3. **Decomposition requires Codex adversarial review** (Outer D). Saved to
   `key-results/<KR>/decomp-adversarial.txt`. (Also honor-system.)

4. **Task code review is enforced by the commit gate** (Inner 3). You do NOT run
   `codex review` yourself. `git commit` triggers a root-owned hook that runs
   Codex on the staged diff and blocks the commit on any `[P1]`/`[P2]`. Fix the
   findings on stderr and commit again; repeat until clean.

5. **The commit gate is a wall, not a file check.** A commit is allowed only when
   the staged tree is fully staged (clean-except-staged), every staged path is in
   `scope.yaml`, gitleaks finds no secret, fast checks pass, and Codex is clean.
   There is no review file to write and no `--no-verify` / chained-`git add` /
   forged-file bypass — they are all denied.

6. **End of shift requires dual consensus** — agent proposes "done" AND
   Codex agrees (or the shift's stop condition fires). Agent self-consensus is
   not valid consensus. The ONLY valid "done" rationale is that any
   further KR would *actively over-engineer or over-optimize the
   literal objective*. NEVER end early on "diminishing returns",
   "deferred to ROADMAP / future shift", "natural stopping point",
   "foundation in place", or "not enough time left" — estimates are
   unreliable. If Codex is unavailable, you cannot end on consensus.

7. **If Codex is unavailable for a PLANNING gate** (Outer B/D), write a rigorous
   self-adversarial review to the same file with header
   `CODEX UNAVAILABLE — SELF-ADVERSARIAL REVIEW`. For CODE review (commit) and
   end-consensus there is NO self-review fallback: the commit gate fails closed
   (Codex down ⇒ commits stop) and consensus needs real Codex.

8. **No questions to the user mid-shift.** Make judgment calls and record
   in `decisions_made` in state.json.

9. **Every commit must be green.** Fast checks run in the commit gate; the full
   suite runs in staging CI, which gates promotion to `main`. Never rely on a
   commit you know is red — revert (git mode) or mark blocked (degrade mode).

10. **Stay on the objective.** Every key result must serve it. Don't
    refactor unrelated code.

11. **Push only `ns/*`, never `main`.** When `push.enabled` is on, push at task
    boundaries to `ns/*` staging branches only. `main` is protected server-side.
    Never force-push. The gate denies both.

12. **Stay within scope.** You may only write and commit paths allowed by the
    root-owned `scope.yaml` (deny_paths always win). Out-of-scope writes and
    commits are blocked. If a task needs a path outside scope, mark it blocked
    and note it for the operator — do not try to route around the gate.
