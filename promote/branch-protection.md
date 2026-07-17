# L1 setup — server-side enforcement for the staging → promote model

This guide wires up the **only zero-bypass layer** of the night-shift harness: GitHub's
server-side rules. After this setup:

- the autonomous agent can push **only** `ns/*` branches;
- **nothing** can update `main` except the promote workflow's identity;
- `main` only ever advances by **fast-forward to a commit whose CI run passed**
  (`promote/promote.yml`, installed as `.github/workflows/promote.yml` on `main`).

All GitHub behavior below was verified against docs.github.com in July 2026; doc URLs
are cited inline where a claim is load-bearing.

**The identity model (read this first).** A fine-grained PAT is *not* an identity — it
authenticates **as the user who minted it**. Two fine-grained PATs from your one
account are the *same actor* to every branch rule, so "one PAT for the agent, one for
the bot" does nothing by itself on a solo account. Distinct actors available to a solo
user: your account, a **deploy key** (attached to the repo, not to any user account —
["GitHub attaches the public part of the key directly to your repository instead of a
personal account"](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)),
a **machine-user account**, or a **GitHub App**. This guide uses:

| Role        | Actor                                             | Credential                     | main        | ns/*  |
|-------------|---------------------------------------------------|--------------------------------|-------------|-------|
| Promote bot | **you** (repo admin) — via the ruleset bypass     | fine-grained PAT → `PROMOTE_TOKEN` | ✅ (bypass) | ✅    |
| Agent       | **write deploy key** (or a machine-user account)  | SSH key (or the machine user's PAT) | ❌ blocked | ✅    |

---

## 1. Prerequisite: GitHub Pro, and rulesets vs classic branch protection

**Plan requirement.** On a personal account, branch protection features on a **private**
repo require **GitHub Pro** (Free gets them on public repos only). The
[GitHub plans page](https://docs.github.com/en/get-started/learning-about-github/githubs-plans)
lists "Protected branches" under Pro's *"Advanced tools and insights in private
repositories"*. Rulesets have the same gating:
["Rulesets are available in public repositories with GitHub Free and GitHub Free for
organizations, and in public and private repositories with GitHub Pro, GitHub Team, and
GitHub Enterprise Cloud"](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets).
Both statements still hold as of July 2026. ✅ You have Pro; you're covered.

**Which mechanism to use: a repository ruleset, not a classic rule.** Two common
beliefs are wrong, in opposite directions:

- *"Classic branch protection can restrict who pushes."* Not on a personal repo. The
  classic **"Restrict who can push to matching branches"** setting is
  **organization-only**: ["You can enable branch restrictions in public repositories
  owned by a GitHub Free organization and in all repositories owned by an organization
  using GitHub Team or GitHub Enterprise Cloud"](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
  Likewise classic bypass lists: ["Actors may only be added to bypass lists when the
  repository belongs to an organization"](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule).
  The checkbox simply does not exist for you.
- *"Rulesets need Team/Enterprise."* Only **organization-level** rulesets (one ruleset
  across many repos) and **push rulesets** need Team/Enterprise. Plain **repository
  branch rulesets** — which is all we need — are available on a private personal repo
  with Pro (availability quote above).

So on a personal private Pro repo, a **repository ruleset with "Restrict updates"** is
the *only* server-side way to restrict who can push to `main` — and conveniently it's
also the better tool: unlike classic rules (where admins are exempt unless you check
"Do not allow bypassing the above settings"), **a ruleset applies to every actor not on
its bypass list, admins included, by default**. That is the "include administrators"
property this design wants.

---

## 2. The promote bot identity and `PROMOTE_TOKEN`

**Who the promote bot is:** *your own account*, exercised through a dedicated
fine-grained PAT, and allowed onto `main` by the ruleset's bypass entry for the
**Repository admin role** (that's you — you are the only admin).

**Create the token** (fine-grained PATs can be scoped to a single repo, unlike classic
PATs which cover every repo you can access —
[docs](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)):

1. github.com → your avatar → **Settings** → **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Name: `night-shift-promote`. Expiration: pick something finite (e.g. 90 days) and
   calendar the rotation — a leaked long-lived token is the worst failure mode here.
3. **Repository access** → *Only select repositories* → select **this one repo**.
4. **Repository permissions** → **Contents: Read and write** (this is the permission
   that allows `git push` over HTTPS; *Metadata: Read* is added automatically).
   Grant **nothing else** — in particular not *Administration* (rulesets/settings),
   not *Secrets*, not *Actions*.
5. Generate, copy the value. You'll store it in §5; never write it into the repo.

**The honest limitation:** a fine-grained PAT's Contents write is **repo-wide — there
is no branch-level scoping** for PATs. This token can push to *any* branch of the repo,
including `main`. What confines promotion to the one sanctioned path is the **ruleset**
(§4), not the token: `main` accepts updates only from bypass actors, and the bypass
list contains exactly the promote identity. The token scope limits *blast radius across
repos*; the ruleset limits *what happens inside this repo*. Both are needed; neither
substitutes for the other. Corollary: the push restriction on `main` must explicitly
admit this identity — that's the bypass entry you add in §4.

**Second honest limitation:** because the promote bot *is you*, every other credential
of yours (your laptop's SSH key, `gh`) also passes the bypass. Section 7 discusses why
that's a social hole, not a technical one.

**The more-scoped alternative: a GitHub App.** Create your own App, install it on this
one repo with only *Contents: read/write*, put **the App** (not the admin role) on the
ruleset bypass list (Apps are first-class bypass actors —
[REST: bypass actor types include `Integration`](https://docs.github.com/en/rest/repos/rules)),
and mint short-lived installation tokens in the workflow (e.g.
`actions/create-github-app-token`) instead of a stored PAT. You gain: (a) even *you*
can no longer push `main` directly — the bypass no longer includes any human; (b)
tokens live ~1 hour instead of months. Worth it once the harness runs unattended for
long stretches or you've ever caught yourself pushing to `main` "just this once".
Until then, the PAT route is fine and much less plumbing.

---

## 3. The agent's push credential

The agent needs a credential that (a) can push `ns/*`, (b) is a **different actor**
than the promote bot so the ruleset can block it on `main`, and (c) never touches the
repo's contents on disk.

**Recommended: a write deploy key.** A deploy key is ["an SSH key that grants access
to a single repository"](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys),
attached to the repo rather than to your account — a genuinely distinct actor, no
second GitHub account needed.

1. On the agent machine, generate a keypair **outside any repo**:

   ```bash
   mkdir -p ~/.ns-agent && ssh-keygen -t ed25519 -C "ns-agent" -f ~/.ns-agent/id_ed25519 -N ""
   ```

2. Repo → **Settings** → **Deploy keys** → **Add deploy key** → paste
   `~/.ns-agent/id_ed25519.pub`, title `ns-agent`, check **Allow write access**
   ("Deploy keys are read-only by default, but you can give them write access") → Add.

3. Wire it into the agent's clone **without leaking it into the repo** — put the key
   selection in the clone's *local* git config, which lives in `.git/config`. The
   `.git` directory is not part of the work tree: it is never staged, committed, or
   pushed, so nothing the agent commits can carry the configuration (and the key file
   itself sits in `~/.ns-agent/`, outside the repo entirely):

   ```bash
   cd /path/to/agent/clone
   git remote set-url origin git@github.com:OWNER/REPO.git
   git config core.sshCommand "ssh -i ~/.ns-agent/id_ed25519 -o IdentitiesOnly=yes"
   ```

*Alternative: a machine-user account.* Create a second (free) GitHub account, invite it
as a collaborator with **Write**, mint it a fine-grained PAT (this repo only,
Contents: read/write), and store that PAT for the agent's clone via a credential file
outside the repo: `git config credential.helper "store --file ~/.ns-agent/git-credentials"`
(then prime it with one push). Same properties, works over HTTPS; costs you a second
account to manage. If you use this, the machine user has Write — *not* admin — so it is
not covered by the §4 bypass. Good.

> **Do not** give the agent a deploy key *and* put "Deploy keys" on the ruleset bypass
> list (§4): deploy-key bypass is all-or-nothing for every deploy key on the repo.

**Actions secrets: why the agent's credential must not read them — and the one real
leak path.** It can't read them directly: secrets live in GitHub's encrypted store,
not in the git tree, so no Contents-scoped credential can fetch them (the secrets API
needs the separate *Secrets* permission, which you didn't grant; deploy keys speak only
git). This matters because `PROMOTE_TOKEN` **is** the promote bot — any actor holding
it can push `main`, and the entire L1 guarantee collapses. But there *is* an indirect
path to respect: the agent can commit a workflow file to `ns/staging`, and workflows
triggered by its pushes run **with access to repository-level secrets**. A malicious or
confused agent could commit a workflow that exfiltrates `secrets.PROMOTE_TOKEN`. That
is exactly why §5 stores the token as an **environment secret gated to `main`** instead
of a plain repo secret — the agent's branches can never satisfy the environment's
branch policy. Also set **Settings → Actions → General → Workflow permissions → "Read
repository contents and packages permissions"** so agent-authored workflows get a
read-only `GITHUB_TOKEN` (belt-and-braces: even a write `GITHUB_TOKEN` isn't a bypass
actor, so the ruleset would reject it on `main` anyway).

---

## 4. The rule on `main` — a repository ruleset

Classic "Restrict who can push" doesn't exist on personal repos (§1), so this section
is a ruleset. It delivers everything the classic recipe would have: required CI check,
push restricted to the promote bot only, no force pushes, no deletions, and admins
included (rulesets bind everyone off the bypass list by default).

Repo → **Settings** → in *Code and automation*, **Rules → Rulesets** →
**New ruleset → New branch ruleset**, then:

1. **Ruleset name:** `protect-main`.
2. **Enforcement status:** `Active` (not *Disabled* — and *Evaluate* is an
   Enterprise-only dry-run mode; you want Active).
3. **Bypass list:** **Add bypass** → **Repository admin role** → leave its mode as
   **Always** (not "For pull requests only"). This is the *only* entry. It admits the
   promote bot (§2). Do **not** add "Deploy keys" (that would admit the agent, §3).
   If you took the GitHub App route in §2, add **your App** here *instead of* the
   admin role.
4. **Target branches:** **Add target → Include default branch** (or *Include by
   pattern* → `main`). Do not target `ns/*` — those stay free for the agent.
5. **Branch rules** — set exactly these:
   - ☑ **Restrict updates** — the heart of the design: ["If selected, only users with
     bypass permissions can push to branches or tags whose name matches the pattern you
     specify"](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).
     Everyone and everything except the bypass actor — including the agent's deploy
     key, a machine user, `GITHUB_TOKEN`, and random collaborators — is rejected at
     the server.
   - ☑ **Restrict deletions** (on by default — confirm it's checked): only bypass
     actors could delete `main`.
   - ☑ **Block force pushes** (on by default — confirm): no history rewrites, so
     "fast-forward only" can't be subverted even by the bypass actor.
   - ☑ **Require status checks to pass** → **Add checks** → add your CI check by its
     *check name* — that's the **job** name your CI shows on commits (e.g. `test`),
     not the workflow file name. This is belt-and-braces: non-bypass actors are
     already fully blocked by *Restrict updates*, and the promote workflow only ships
     SHAs whose CI run succeeded; but this rule keeps a floor in place if you ever
     loosen the rules above. (Note: bypass actors skip this rule too — greenness of
     promoted commits is enforced by `promote.yml`'s `workflow_run` gate, not here.)
   - ☐ Leave **Require a pull request before merging** off. In this model `main`
     changes by fast-forward push, never by PR merge; the rule would bind nobody the
     other rules don't already bind, and would only add friction if you ever promote
     by hand.
6. **Create**.

If an old **classic** branch protection rule exists on `main`
(Settings → Branches), delete it — rules stack, and a stale classic rule with admin
exemptions just muddies the picture.

Also verify the agent's credential cannot *edit* this ruleset: ruleset management needs
repo admin (PAT *Administration* permission). Your agent has a deploy key (no settings
access at all) or a Write-role machine user — neither qualifies.

---

## 5. Store `PROMOTE_TOKEN`

**Recommended: as an environment secret gated to `main`** (environments in private
repos are Pro-gated too and you have Pro: ["Creation of an environment in a private
repository is available to organizations with GitHub Team and users with GitHub
Pro"](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)):

1. Repo → **Settings** → **Environments** → **New environment** → name it exactly
   `promote` (matching `environment: promote` in `promote.yml`) → **Configure
   environment**.
2. Under **Deployment branches and tags** → choose **Selected branches and tags** →
   **Add deployment branch or tag rule** → pattern `main`.
3. Under **Environment secrets** → **Add environment secret** →
   Name: `PROMOTE_TOKEN`, Value: the PAT from §2 → **Add secret**.

Why this shape: environment secrets are only handed to jobs that reference the
environment, and the branch policy only lets runs for `main` do so. The promote job
qualifies because `workflow_run`-triggered workflows execute against the default
branch; a CI run for an agent-pushed `ns/*` branch does not — closing the
workflow-exfiltration path from §3. (Optional: adding yourself under the environment's
**Required reviewers** turns every promotion into a one-click human approval — a
human-in-the-loop knob, at the cost of autonomy.)

**Simpler but weaker: a plain repository secret.** Settings → **Secrets and
variables → Actions** → *Repository secrets* → **New repository secret** →
`PROMOTE_TOKEN`. Works with the same workflow (the `environment: promote` line is
harmless either way), but any workflow the agent commits to `ns/*` could read it. Only
acceptable if you also gate what the agent may write under `.github/workflows/` some
other way. Prefer the environment.

---

## 6. Verification checklist — prove the guarantees before trusting them

Do these in order, from the **agent's** clone (deploy-key/machine-user credential)
unless stated otherwise. Prerequisite: `promote.yml` is on `main` (with the CI workflow
name filled in) — remember `workflow_run` only fires for workflow files on the default
branch.

**(a) Agent push to `main` is REJECTED.**

```bash
git fetch origin
git switch --detach origin/main
git commit --allow-empty -m "L1 test: this must be rejected"
git push origin HEAD:refs/heads/main
```

Expected: rejection citing repository rules, e.g.
`remote: error: GH013: Repository rule violations found for refs/heads/main` /
`Cannot update this protected ref` (wording may vary; anything but success is a fail
of the push and a pass of the test). Also confirm force-push and deletion are refused:

```bash
git push --force origin HEAD:refs/heads/main   # must be rejected
git push origin :refs/heads/main               # must be rejected
```

**(b) Agent push to `ns/staging` SUCCEEDS.**

```bash
git switch --detach origin/main   # reuse the empty test commit from (a), or make one
git push origin HEAD:refs/heads/ns/staging
```

Expected: accepted. (If `ns/staging` doesn't exist yet this creates it.)

**(c) Green `ns/staging` auto-promotes to `main`.**
The push in (b) triggers CI. Watch the repo's **Actions** tab: the CI run on
`ns/staging` goes green → a **Promote** run starts → it fast-forwards `main`. Then:

```bash
git ls-remote origin refs/heads/main refs/heads/ns/staging
```

Expected: both refs at the **same SHA**. Check the Promote run's summary shows
"fast-forwarded".

**(d) A red `ns/staging` does NOT promote.**
Push a commit that deliberately fails CI (e.g. a temporary failing assertion, or a
step with `exit 1`):

```bash
git commit --allow-empty -m "L1 test: break CI deliberately"   # or a real breaking change
git push origin HEAD:refs/heads/ns/staging
```

Expected: CI run is red. A Promote run *will still appear* (the trigger fires on every
completed CI run) but its `promote` job must show **Skipped** — that's the
`conclusion == 'success'` guard doing its job, not a bug. Confirm `main` did not move:

```bash
git ls-remote origin refs/heads/main   # same SHA as in (c)
```

Then push a green commit on top; it should promote and everything is clean again.

**(e) Bonus paranoia.** From the agent's environment, confirm the ruleset and secret
are out of reach: the deploy key can't call any settings API at all, and a machine-user
PAT without *Administration*/*Secrets* gets 403/404 on
`gh api repos/OWNER/REPO/rulesets` and the secrets endpoints. If you used the
environment in §5, note any agent-committed workflow on `ns/*` that declares
`environment: promote` will stall/fail the branch policy rather than receive the secret.

---

## 7. The honest boundary: what L1 does and does not guarantee

**L1 is the only zero-bypass layer.** Everything in this file is enforced by GitHub's
servers at ref-update time. It does not matter what runs on the agent's machine, what
model it is, or what flags it was started with: local hooks, wrappers, and permission
prompts (the L2 layer) all live inside the blast radius of
`--dangerously-skip-permissions` — an unattended agent can delete hooks, rewrite local
config, or shell out to raw `git`. None of that reaches `main`, because the reject
happens at the server. **"main stays green" is guaranteed server-side regardless of the
agent's behavior.**

Precisely what is guaranteed: `main` only ever changes by a fast-forward to a commit
whose CI run had already succeeded at promotion time; no force pushes; no deletion; no
identity but the promote bot can move it.

What is *not* guaranteed — know your residual risks:

1. **"Green" means only what CI tests.** Promotion is not review. An agent can write
   plausible-but-wrong code that passes; the bar for `main` is exactly as high as your
   CI is.
2. **`ns/*` is sacrificial.** The agent can force-push, trash, or rewrite its own
   branches. That's by design — recoverable workspace damage, never `main` damage.
3. **The human bypass is a social hole.** The ruleset's bypass admits *you* (repo
   admin), so your everyday credentials can push `main` directly. GitHub can't
   distinguish your PATs from you. Don't normalize "just this once" — and if you want
   the machine to stop even you, switch the bypass actor to a GitHub App (§2).
4. **The secret is the crown jewel.** `PROMOTE_TOKEN` *is* the promote bot. The §5
   environment gating closes the only in-repo read path (agent-authored workflows);
   rotation limits the damage window if it leaks anywhere else.
5. **CI runs the agent's code.** Even with secrets gated, workflows the agent pushes
   to `ns/*` execute with a (read-only, per §3) `GITHUB_TOKEN` and network access —
   standard CI supply-chain caution applies.

Layer summary: L1 (this file) = guarantees; L2 (local hooks) = fast feedback and
convenience, assumed bypassable; the promote workflow (`promote.yml`) = the single
sanctioned bridge between them.
