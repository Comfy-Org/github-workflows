# `pr-risk.yml` — PR risk grade (shadow check)

Read [the shared caller contract](README.md) first.

## What it does

Grades every PR into a tier `R0` (safest) .. `R3` (riskiest) and syncs **one**
label (`risk:R0`..`risk:R3`, or `risk:ungraded` when an input was unreadable).
The label is the entire product: nothing is gated, routed, commented, or
merged — a human looks at the label and agrees or disagrees (recorded with a
`risk-dispute` label this workflow never touches).

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
      issues: write          # the risk label rides the issues API
      pull-requests: read
      checks: read           # the check rollup the reversibility axis reads
      statuses: read
    uses: Comfy-Org/github-workflows/.github/workflows/pr-risk.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
```

This workflow has no roster yet — it has no fleet of pinned callers to track
(see [Staying current](README.md#staying-current)), so there is no
`vars.*_CALLERS` entry to add.

## Required permissions

```yaml
contents: read
issues: write
pull-requests: read
checks: read
statuses: read
```

## Inputs

| Input | Default | Notes |
|---|---|---|
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. No default on purpose: a floating default let a caller SHA-pin `uses:` and still load the grader from HEAD of main. |
| `fleet_logins` | `mattmillerai` | Logins whose PRs grade provenance `agent-supervised` alongside `agent-coded`. Both are read for **human** authors only: an author GitHub types as a `Bot` is a runbook candidate regardless, so listing a bot here (or labelling its PR) buys it nothing — only a registry entry that asserts can promote it. |
| `bot_logins` | `github-actions,dependabot,renovate,coderabbitai,cursor,comfy-pr-bot,web-flow` | Extra logins treated as bots. Needed only for **machine USER accounts** — a real GitHub App is recognized from GitHub's own actor type, no list entry required. A bot with no runbook entry still grades as human — identity alone buys no trust. |
| `label_map` | `''` | Rename the five grader-owned labels as `tier=label` pairs. Tier keys are fixed; only the label text is yours. |
| `wait_for_checks_minutes` | `10` | How long to wait for the rest of the check rollup to settle before labeling (clamped to 25 — what a 30-minute job can spend waiting). `0` labels immediately, expect R2 floors from still-pending checks. |
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
