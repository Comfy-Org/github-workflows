# `assign-prs-to-author.yml` — assign unassigned PRs to their authors

Read [the shared caller contract](README.md) first.

## What it does

Housekeeping. Sweeps every open PR with no assignees and assigns it to its
author. Bot-authored PRs are skipped by default.

Useful when a team tracks PR ownership through the assignee field — Comfy-Org
does — because a PR with no assignee is invisible to that view.

## Prerequisites

None. No secrets; it uses the caller's `GITHUB_TOKEN`.

## Caller

`.github/workflows/assign-prs-to-author.yml`:

```yaml
name: Housekeeping - Assign PRs to Author

on:
  schedule:
    - cron: '0 2 * * *'    # daily at 02:00 UTC
  workflow_dispatch:

jobs:
  assign:
    permissions:
      pull-requests: write
      issues: write        # assignees are set through the issues API
    uses: Comfy-Org/github-workflows/.github/workflows/assign-prs-to-author.yml@<full-commit-sha>
```

## Required permissions

```yaml
pull-requests: write
issues: write
```

**Both.** GitHub sets assignees through the *issues* API even for pull requests,
so `pull-requests: write` alone is not enough — and because the grant is validated
at startup, getting it wrong gives you a zero-job `startup_failure` with no logs.

## Inputs

None.

## Gotchas

**Run it on a schedule, never on `pull_request`.** It is a sweep over all open
PRs, not a per-PR reaction. Wiring it to PR events makes it do the same full scan
on every event.

**It only touches PRs with *no* assignee.** An existing assignee is never
replaced, so it is safe to run alongside
[`assign-reviewers.yml`](assign-reviewers.md).
