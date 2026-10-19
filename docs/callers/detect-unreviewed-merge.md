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
      # Optional. An APPROVED review counts, including from an automated
      # approver. List a login only when that approval must not count.
      # The match is exact: `name` does not match `name[bot]`.
      # ignore-approvers: some-bot[bot]
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
| `ignore-approvers` | `''` | Opt-out. Logins (whitespace- or comma-separated, case-insensitive; the `[bot]` suffix is part of the login and is not stripped) whose reviews the check ignores in both modes. Empty counts every `APPROVED` review, including an automated approver's. |

### Choosing `approval-mode`

- **`latest-per-reviewer`** — for OSS repos that have *"dismiss stale reviews on
  new commits"* enabled. A dismissed approval does **not** count, matching what
  branch protection actually enforced.
- **`any-approval`** — for private repos **without** stale-dismissal. Any
  historical `APPROVED` counts.

Getting this backwards produces audit noise in one direction or false confidence
in the other. Check the repo's branch-protection settings, then pick.

### Automated approvers: `ignore-approvers`

An `APPROVED` review satisfies the check in either mode, including one from an
automated approver. That is a machine user (the `APPROVER_TOKEN` account; login
has no `[bot]` suffix and `user.type` is `User`) or a GitHub App (`<slug>[bot]`,
`user.type` `Bot`). `user.type` is not a filter, and a `[bot]` suffix is not
inferred: listing `name` does not ignore `name[bot]`, or the reverse.

`ignore-approvers` is the opt-out for a login that must not satisfy the check.
The match is that exact login, case-insensitive. A listed review is dropped
before either mode runs, so it cannot count and cannot shadow another
reviewer's latest decision. Leave the input empty for an approver whose
approval should count. A human approval alongside a listed login still counts.

A review GitHub has dismissed is state `DISMISSED` and does not count in either
mode — dismissal replaces `APPROVED` on that review, so `any-approval` does not
keep a hidden earlier approval. `latest-per-reviewer` also drops an approval
that the same login later superseded with a non-comment review. A later
`COMMENTED` review does not.

Callers that already pass an approver's login here keep dropping that approval
until they remove the login. A pin bump does not edit the input.

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
