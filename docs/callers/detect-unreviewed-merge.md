# `detect-unreviewed-merge.yml` — SOC 2 unreviewed-merge detection

Read [the shared caller contract](README.md) first.

## What it does

Detects a PR merged **without prior approval** and opens a tracking issue in
[`Comfy-Org/unreviewed-merges`](https://github.com/Comfy-Org/unreviewed-merges).
This is SOC 2 compliance evidence: the control is "changes are reviewed", and this
is the detective control that catches exceptions.

It reports; it does not block. Blocking is branch protection's job.

## Prerequisites

| | |
|---|---|
| `secrets.UNREVIEWED_MERGES_TOKEN` | **Required.** Fine-grained PAT with `issues: write` on `Comfy-Org/unreviewed-merges`. |

The token needs write on the *tracking* repo, not on yours — findings are
centralized so auditors read one place.

## Caller

`.github/workflows/detect-unreviewed-merge.yml`:

```yaml
name: Detect Unreviewed Merge

on:
  push:
    branches: [main]        # or [master] — your default branch

concurrency:
  group: detect-unreviewed-merge-${{ github.sha }}
  cancel-in-progress: false

permissions:
  contents: read
  pull-requests: read

jobs:
  detect:
    uses: Comfy-Org/github-workflows/.github/workflows/detect-unreviewed-merge.yml@<full-commit-sha>
    with:
      approval-mode: latest-per-reviewer   # 'any-approval' for private repos
      # Only if this repo auto-approves (cursor-review `approve_max_severity`):
      # the identity that approves, so its approval is not counted as a review.
      # ignore-approvers: my-review-app[bot]
    secrets:
      UNREVIEWED_MERGES_TOKEN: ${{ secrets.UNREVIEWED_MERGES_TOKEN }}
```

It triggers on **push to the default branch**, not on a `pull_request` event —
the merge commit is the thing being audited, so the check runs after the merge
lands. `concurrency` is keyed by `github.sha` for the same reason.

## Required permissions

```yaml
contents: read
pull-requests: read
```

Read-only on your repo. The issue write happens on the tracking repo via the PAT.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `approval-mode` | `latest-per-reviewer` | Which historical approvals count. Pick deliberately — see below. |
| `ignore-approvers` | `''` | Logins (whitespace- or comma-separated, case-insensitive, `[bot]` suffix included) whose reviews the check ignores in both modes. **Required in practice for any repo that auto-approves** — see below. |

### Choosing `approval-mode`

- **`latest-per-reviewer`** — for OSS repos that have *"dismiss stale reviews on
  new commits"* enabled. A dismissed approval does **not** count, matching what
  branch protection actually enforced.
- **`any-approval`** — for private repos **without** stale-dismissal. Any
  historical `APPROVED` counts.

Getting this backwards produces audit noise in one direction or false confidence
in the other. Check the repo's branch-protection settings, then pick.

### Automated approvers: `ignore-approvers`

An approval by a bot is not a human review. If the repo enables cursor-review's
`approve_max_severity`, the bot's `APPROVED` would otherwise satisfy this check
in either mode — a PR could be labelled, auto-approved and merged with nobody
looking, and no tracking issue filed. Pass the identity that approves: the
`APPROVER_TOKEN` account's login, else your `bot_app_id` App's `<slug>[bot]`,
else `github-actions[bot]`. A human approval alongside the bot's still counts.

## Gotchas

**Use this repo's path, not the old one.** Some older examples reference
`Comfy-Org/unreviewed-merges/.github/workflows/detector.yml` — that path **does
not exist** and will not resolve. The workflow lives here, in
`Comfy-Org/github-workflows`, which is what the ~11 live callers use.

**The `DETECT_UNREVIEWED_MERGE_CALLERS` roster is seeded**, so
`bump-detect-unreviewed-merge-callers.yml` moves your pin for you and enrollment
is the usual two steps: merge the caller, *and* add the repo to that roster
secret. Skipping the second half is the common mistake — your pin then never
moves and the caller quietly drifts behind the reusable.
