# `stale.yml` — stale-PR sweeper + Slack digest

Read [the shared caller contract](README.md) first.

## What it does

Runs [`actions/stale`](https://github.com/actions/stale) over PRs, then posts a
Slack digest of what it touched. PRs inactive for `days_before_pr_stale` get the
`stale` label; still-inactive PRs are closed after
`days_before_pr_close` more days.

The digest names the source repo in the header **and on every PR line**
(`your-org/your-repo#123`), so batches from different repos posted to the same
channel stay unambiguous line by line.

Issues are untouched — this is PR-only.

## Prerequisites

`secrets.SLACK_BOT_TOKEN` is optional. Without it the sweep still runs and only
the Slack heads-up is skipped.

## Caller

`.github/workflows/stale.yml`:

```yaml
name: Stale PRs

on:
  schedule:
    - cron: '0 3 * * *'
  workflow_dispatch:
    inputs:
      dry-run:
        description: Preview without applying
        type: boolean
        default: false

permissions:
  pull-requests: write
  issues: write            # required by actions/stale even for PR-only runs

concurrency:
  # stale.yml declares no group of its own, so a caller-level group is safe.
  group: stale
  cancel-in-progress: false

jobs:
  stale:
    uses: Comfy-Org/github-workflows/.github/workflows/stale.yml@<full-commit-sha>
    with:
      slack_channel: <YOUR_CHANNEL_ID>     # see the gotcha below
      dry_run: ${{ inputs.dry-run || false }}
    secrets:
      SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
```

## Required permissions

```yaml
pull-requests: write
issues: write
```

`issues: write` is required by `actions/stale` itself even on a PR-only run.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `slack_channel` | an internal Comfy-Org channel id | **Set this explicitly.** See below. |
| `days_before_pr_stale` | `14` | Inactivity before labeling. |
| `days_before_pr_close` | `14` | Further inactivity before closing. |
| `stale_pr_label` | `stale` | The label applied. |
| `exempt_pr_labels` | `pinned,security,work-in-progress,wip,dependencies` | Never swept. |
| `stale_pr_message` | *(see workflow)* | Comment posted when labeling. |
| `close_pr_message` | *(see workflow)* | Comment posted when closing. |
| `dry_run` | `false` | Report without labeling or closing. |

## Gotchas

**Always set `slack_channel` explicitly.** The default is Comfy-Org's internal
channel id. A caller outside that workspace inherits it, tries to post somewhere
it has no business posting, and gets a confusing Slack API error rather than a
clear misconfiguration.

**Dry-run first, on a repo with a backlog.** `dry_run: true` shows you the set it
would label and close. On a repo with old open PRs, the first live run can close a
lot at once — and closing someone's six-month-old PR without warning is a people
problem, not a CI problem.

**`exempt_pr_labels` is your safety valve.** Add whatever your repo uses for
long-lived work before the first live run, not after.
