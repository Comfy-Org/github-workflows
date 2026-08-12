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

### Choosing `approval-mode`

- **`latest-per-reviewer`** — for OSS repos that have *"dismiss stale reviews on
  new commits"* enabled. A dismissed approval does **not** count, matching what
  branch protection actually enforced.
- **`any-approval`** — for private repos **without** stale-dismissal. Any
  historical `APPROVED` counts.

Getting this backwards produces audit noise in one direction or false confidence
in the other. Check the repo's branch-protection settings, then pick.

## Gotchas

**Use this repo's path, not the old one.** Some older examples reference
`Comfy-Org/unreviewed-merges/.github/workflows/detector.yml` — that path **does
not exist** and will not resolve. The workflow lives here, in
`Comfy-Org/github-workflows`, which is what the ~11 live callers use.

**The `DETECT_UNREVIEWED_MERGE_CALLERS` roster is not seeded yet**, so pin bumps
are still manual for now — the `bump-detect-unreviewed-merge-callers.yml` fleet
exists but has no roster to read. Check periodically that your pin is not far
behind `main`. Seeding that roster is tracked as a follow-on; once it lands,
enrollment is the usual two steps.
