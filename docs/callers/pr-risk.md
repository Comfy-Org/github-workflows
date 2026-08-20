# `pr-risk.yml` — PR risk grade (shadow check)

Read [the shared caller contract](README.md) first.

## What it does

Grades every PR into a tier `R0` (safest) .. `R3` (riskiest) and syncs **one**
label (`risk:R0`..`risk:R3`, or `risk:ungraded` when an input was unreadable).
Nothing is gated, routed, or merged. A human can record a different assessment
beside the computed label with `risk-dispute:R0` through `risk-dispute:R3`.
The legacy plain `risk-dispute` marker remains valid with no human tier; neither
form changes the computed `risk:R*`.

Deterministic, no LLM: `grade = worst(path_floor, provenance, reversibility)` —
a path-glob map, what process produced the diff (registered runbooks, forks
always `R3`), and revertability (persistent-state mutation, sensitive
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
    types: [opened, synchronize, reopened, ready_for_review, labeled, unlabeled]
  issue_comment:
    types: [created]

concurrency:
  group: pr-risk-${{ github.event.pull_request.number || github.event.issue.number }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  pr-risk:
    if: >-
      github.event_name != 'issue_comment' &&
      (github.event_name != 'pull_request' ||
       (((github.event.action != 'labeled' && github.event.action != 'unlabeled') ||
         github.event.label.name == 'risk-dispute' ||
         startsWith(github.event.label.name, 'risk-dispute:')) &&
        github.actor != 'dependabot[bot]' &&
        github.event.pull_request.head.repo.full_name == github.repository))
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
      enabled: true

  risk-dispute-comment:
    if: >-
      github.event.issue.pull_request &&
      (github.event.comment.body == '/risk-dispute' ||
       startsWith(github.event.comment.body, '/risk-dispute '))
    permissions:
      contents: read
      issues: write
      pull-requests: write
      checks: write
      actions: read
      statuses: read
    uses: Comfy-Org/github-workflows/.github/workflows/pr-risk.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
      enabled: true
      pr_number: ${{ github.event.issue.number }}
      wait_for_checks_minutes: 1
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
| `bot_logins` | `github-actions,dependabot,renovate,coderabbitai,cursor,comfy-pr-bot,web-flow` | Extra logins treated as bots. Needed only for **machine USER accounts** — a real GitHub App is recognized from GitHub's own actor type, no list entry required. A bot with no runbook entry still grades as human — identity alone buys no trust. **This list is load-bearing, not a hint:** a listed login skips the first-time-contributor test, so it moves a non-fork `NONE`/`FIRST_TIME_CONTRIBUTOR` PR from `external` (R3) to `human` (R1). Nothing validates that a listed login is really a machine account, so add one only for an account you control, and remove it when it is retired. |
| `label_map` | `''` | Rename the five grader-owned labels as `tier=label` pairs. Tier keys are fixed; only the label text is yours. |
| `allowed_dispute_associations` | `OWNER,MEMBER,COLLABORATOR` | Comment authors allowed to use `/risk-dispute`. Comma-separated with no spaces. Direct label changes already require repository label permission. |
| `wait_for_checks_minutes` | `10` | How long to wait for the rest of the check rollup to settle before labeling (clamped to 25 — what a 30-minute job can spend waiting). `0` labels immediately, expect R2 floors from still-pending checks. |
| `repo_map_path` | `.github/risk.json` | Consumer risk-map override, read from the PR **base ref**. |
| `repo_runbooks_path` | `.github/risk-runbooks.json` | Consumer runbook-registry override, read from the PR **base ref**. |

## Risk disputes

A human assessment sits beside the computed grade; it never replaces it:

```text
risk:R1
risk-dispute:R2
```

Apply `risk-dispute:R0` through `risk-dispute:R3` directly, or comment:

```text
/risk-dispute R2 Optional reason
```

The legacy forms remain supported as a disagreement with no human-assessed tier:

```text
risk-dispute
/risk-dispute Optional reason
```

The reason may be empty or continue on later lines. A tiered dispute replaces
the legacy label and any previous tier. `/risk-dispute clear` clears both forms;
removing a label clears that form, and a new push expires both. Each change posts
a bot-authored audit record with the computed tier, nullable human tier, head
SHA, source, actor and optional reason.

## Gotchas

**Fork PRs need `pull_request_target`, not `pull_request`.** A fork PR under a
plain `pull_request` trigger gets a read-only token and the label write fails.
This is safe to do here by construction — the workflow never checks out or
executes PR code. That property is the entire safety argument, so keep the
caller a bare `uses:` job: `pull_request_target` runs privileged, and adding
your own steps (or jobs) that check out or run PR head code under it reopens
the classic pwn-request hole. Same-repo-only callers (private org repos that
take no fork PRs) should stay on plain `pull_request`.

**Enroll it as its own workflow, not a job inside an existing CI workflow.**
The grading job excludes its own *run* from the check rollup it reads; a job
sharing a run with the rest of CI excludes its siblings too and lands on the
honest R2 floor instead of grading off the full rollup.

**Pair the caller with a per-PR `cancel-in-progress` concurrency group** (shown
above). The reversibility axis waits for other checks to settle, so a stale run
from an earlier push should not stack behind a newer one.

**The label is applied with the plain `GITHUB_TOKEN` on purpose** — a
`GITHUB_TOKEN`-applied label cannot fire `labeled` triggers, so this shadow
check is structurally unable to start a workflow cascade. Do not "upgrade" the
label write to an App token; that would reopen the cascade risk this design
avoids.
