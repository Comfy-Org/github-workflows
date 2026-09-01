# `pr-risk.yml` — PR risk grade (shadow check)

Read [the shared caller contract](README.md) first.

## What it does

Grades every PR into a tier `low` (safest) .. `xhigh` (riskiest) and syncs **one**
label (`risk:low`..`risk:xhigh`, or `risk:ungraded` when an input was unreadable).
The label is the entire product: nothing is gated, routed, commented, or
merged — a human looks at the label and agrees or disagrees (recorded with a
`risk-dispute` label this workflow never touches).

`R0`, `R1`, `R2`, and `R3` remain accepted in existing risk maps and
`label_map` keys as deprecated aliases for `low`, `medium`, `high`, and
`xhigh`. New maps and integrations should use only the canonical names; output
records and publish surfaces always do.

Deterministic, no LLM: `grade = worst(path_floor, provenance, reversibility)` —
a path-glob map, what process produced the diff (registered runbooks, forks
always `xhigh`), and revertability (persistent-state mutation, sensitive
deletions, whether green checks actually covered the changed lines). The
grader and its generic defaults live in
[`scripts/pr-risk/`](../../scripts/pr-risk) and load from **this repo** at the
pinned `workflows_ref` — never from the graded PR's checkout, so a PR cannot
edit the rules that judge it. A consumer sharpens the generic defaults with
`.github/risk.json` / `.github/risk-runbooks.json`, read from the PR's **base
ref** for the same reason.

## Prerequisites

None. No secrets — the only credential used is the automatic `GITHUB_TOKEN`.

## Caller

`.github/workflows/ci-pr-risk.yml`:

```yaml
name: CI - PR Risk Grade

on:
  # Public repo taking fork PRs? Use `pull_request_target:` here instead —
  # a fork run under plain `pull_request` cannot write the label. See the
  # fork gotcha below before you swap it.
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

concurrency:
  group: pr-risk-${{ github.event.pull_request.number }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  pr-risk:
    permissions:
      contents: read
      issues: write          # create the risk:* labels repo-side on first use
      pull-requests: write   # the label write itself — labeling a PR rides the
                             # pull-requests permission, not issues (the labels
                             # endpoint is dual-mapped by what the "issue" is)
      checks: write          # REQUIRED WHETHER OR NOT you set `check_run: true` —
                             # see "Grant the whole union" below
      actions: read          # the rollup's CheckRun -> checkSuite -> workflowRun hop
      statuses: read
    uses: Comfy-Org/github-workflows/.github/workflows/pr-risk.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
```

Enrolling is **two steps** — merging the caller above is only the first. Ask a
maintainer to add this repo to the `PR_RISK_CALLERS` roster secret
(see [Staying current](README.md#staying-current)); until they do, the
[`bump-pr-risk-callers.yml`](../../.github/workflows/bump-pr-risk-callers.yml)
fleet does not know the caller exists, so both of its pins sit frozen and the
caller silently drifts behind the grader it runs. Skipping this half is the most
repeated mistake in this repo.

One thing to know about that second step:

- **Enrolment does not backfill your pin.** The fleet only runs on a push to
  `main` touching `pr-risk.yml` or `scripts/pr-risk/**`, so a repo added to the
  roster after the fact stays on whatever SHA it merged with until the grader
  next changes. Ask the maintainer to `workflow_dispatch`
  `bump-pr-risk-callers.yml` once after adding you — every bump entrypoint
  carries `workflow_dispatch` for exactly this.

Enrolling a private repo used to publish its name in this public repo's run log:
the roster was an Actions **variable** bound through `env:`, and Actions dumps
the step env before `bump-callers.sh` can mask it. BE-6472 moved every roster to
a **secret**, which the runner masks in that dump too, so that caveat no longer
applies to new enrolments — but a name already printed in an old public log
cannot be unpublished.

## Required permissions

```yaml
contents: read
issues: write
pull-requests: write
checks: write
actions: read
statuses: read
```

**Grant the whole union, including `checks: write`.** A reusable workflow can
only narrow the caller's token, never elevate it, so GitHub validates *every*
nested job's declared `permissions:` against this block at **startup** — before
any job is scheduled. The `publish-check` job declares `checks: write`, and a
job-level `if:` is a runtime condition, so leaving `check_run` at its default
`false` does not exempt you: a short grant fails the whole run with an opaque
"workflow file issue" and no job-level detail. Grading itself only needs
`checks: read` (the rollup the reversibility axis reads); the write is the
grant, not the behaviour. **Moving an existing pin onto a commit that has this
job? Add `checks: write` to the caller in the same PR** — a pin bump alone will
fail the caller's next run at startup.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. No default on purpose: a floating default let a caller SHA-pin `uses:` and still load the grader from HEAD of main. Checked before the tool checkout on two axes: it must be a full 40-hex lowercase SHA, **and** that commit must be an ancestor of `main` of this repo. So a branch, a tag, a `refs/pull/N/head` and any **not-yet-merged** SHA all fail the run — **merge the change here first, then bump the pin.** There is no opt-out. |
| `fleet_logins` | `mattmillerai` | Logins whose PRs grade provenance `agent-supervised` alongside `agent-coded`. Both are read for **human** authors only: an author GitHub types as a `Bot` is a runbook candidate regardless, so listing a bot here (or labelling its PR) buys it nothing — only a registry entry that asserts can promote it. |
| `bot_logins` | `github-actions,dependabot,renovate,coderabbitai,cursor,comfy-pr-bot,web-flow` | Extra logins treated as bots. Needed only for **machine USER accounts** — a real GitHub App is recognized from GitHub's own actor type, no list entry required. A bot with no runbook entry still grades as human — identity alone buys no trust. **This list is load-bearing, not a hint:** a listed login skips the first-time-contributor test, so it moves a non-fork `NONE`/`FIRST_TIME_CONTRIBUTOR` PR from `external` (xhigh) to `human` (medium). Nothing validates that a listed login is really a machine account, so add one only for an account you control, and remove it when it is retired. |
| `label_map` | `''` | Rename the five grader-owned labels as `tier=label` pairs. Canonical keys are `low`, `medium`, `high`, `xhigh`, and `unknown`; deprecated `R0`–`R3` keys remain accepted. |
| `wait_for_checks_minutes` | `10` | How long to wait for the rest of the check rollup to settle before labeling (clamped to 25 — what a 30-minute job can spend waiting). `0` labels immediately, expect high floors from still-pending checks. |
| `repo_map_path` | `.github/risk.json` | Consumer risk-map override, read from the PR **base ref**. |
| `repo_runbooks_path` | `.github/risk-runbooks.json` | Consumer runbook-registry override, read from the PR **base ref**. |

## Gotchas

**Fork PRs need `pull_request_target`, not `pull_request`.** A fork PR under a
plain `pull_request` trigger gets a read-only token and the label write fails.
This is safe to do here by construction — the workflow never checks out or
executes PR code. That property is the entire safety argument, so keep the
caller a bare `uses:` job: `pull_request_target` runs privileged, and adding
your own steps (or jobs) that check out or run PR head code under it reopens
the classic pwn-request hole. Same-repo-only callers (private org repos that
take no fork PRs) should stay on plain `pull_request`.

That is why the caller block above carries **no `if:` at all**, and why the two
paragraphs below talk about a clause it does not show. Neither shape this page
recommends needs one: under `pull_request_target` the token is writable for fork
PRs too, and a same-repo-only repo has no fork PRs to guard against. The third
shape — a public repo staying on plain `pull_request` and accepting that its fork
PRs go **ungraded** rather than red — is the one that needs
`github.event.pull_request.head.repo.full_name == github.repository`, scoped
behind an event test so `workflow_dispatch` still works. Copy that variant from
the caller pattern in
[`pr-risk.yml`'s header](../../.github/workflows/pr-risk.yml) rather than writing
it yourself; an unscoped clause makes the dispatch button a silent no-op.

**Bot PRs are graded — do not add a `github.actor != 'dependabot[bot]'` skip.**
Dependabot's `pull_request` runs do start from a read-only `GITHUB_TOKEN`, but
the caller's `permissions:` block elevates it — that is GitHub's documented
behaviour ([Changing `GITHUB_TOKEN`
permissions](https://docs.github.com/en/code-security/dependabot/troubleshooting-dependabot/troubleshooting-dependabot-on-github-actions#changing-github_token-permissions))
— and this workflow declares no secrets, so the label write succeeds. A
dependabot skip therefore buys almost nothing and leaves most repos'
highest-volume automated PR producer ungraded. (Almost: the clause did skip
Dependabot's own github-actions PRs bumping *this* caller's pins, which now run
the newly pinned reusable inside their own run under the elevated token. The
exposure is narrow — Dependabot only proposes SHAs behind this repo's published
tags, and the tool checkout is separately gated on `workflows_ref` being an
ancestor of `main` — and the fleet's own bump PRs always behaved this way, since
the clause keyed on `github.actor`, the run's **trigger**, not the PR's author. Do
review such a PR before merging: Dependabot rewrites `uses:` and leaves
`workflows_ref` stale, so fix the input in the same PR.) The reasoning is
specific to *this* workflow: a caller that needs an **Actions secret** (an app
private key, an API token —
[`cursor-review-auto-label.yml`](cursor-review-auto-label.md) is the org's
example) still needs the skip, because a Dependabot-triggered run sees Dependabot
secrets only and no `permissions:` block can change that. Fork PRs are the
opposite case: the `permissions:` elevation does not extend to them, which is why
the fork gotcha above is still a real token guard.

**Already carrying that skip? Removing it is yours to do, not the fleet's** —
[`bump-pr-risk-callers.yml`](../../.github/workflows/bump-pr-risk-callers.yml)
moves SHA pins only and will never edit your `if:`, so drop the clause in your
own repo, and update in the same PR anything repo-side that mirrors the caller's
`if:`/`concurrency` shape (a unit test asserting on the caller's YAML is the
usual one).

**Enroll it as its own workflow, not a job inside an existing CI workflow.**
The grading job excludes its own *run* from the check rollup it reads; a job
sharing a run with the rest of CI excludes its siblings too and lands on the
honest high floor instead of grading off the full rollup.

**Pair the caller with a per-PR `cancel-in-progress` concurrency group** (shown
above). The reversibility axis waits for other checks to settle, so a stale run
from an earlier push should not stack behind a newer one.

**The label is applied with the plain `GITHUB_TOKEN` on purpose** — a
`GITHUB_TOKEN`-applied label cannot fire `labeled` triggers, so this shadow
check is structurally unable to start a workflow cascade. Do not "upgrade" the
label write to an App token; that would reopen the cascade risk this design
avoids.
