# `cursor-review.yml` — label-triggered multi-model code review

Read [the shared caller contract](README.md) first.

## What it does

A 4-lab × 2-review-type `cursor-agent` panel runs adversarial and edge-case
passes over the PR diff. A judge model consolidates them into **one** PR review
with per-finding severity badges. The person who applied the label gets Slack
start/complete DMs.

Advisory by default. Opt into blocking to fail a required-status-check gate while
findings stay unresolved.

Prompts and scripts live in [`.github/cursor-review/`](../../.github/cursor-review)
— the single source of truth, so your repo carries only a thin caller.

## Prerequisites

| | |
|---|---|
| `secrets.CURSOR_API_KEY` | **Required.** Org-level in Comfy-Org. |
| `secrets.SLACK_BOT_TOKEN` | Optional. Without it the review still posts; only the DMs are skipped. |
| `vars.REVIEW_BOT_APP_ID` + `secrets.BOT_APP_PRIVATE_KEY` | Optional. Posts the review as your App instead of `github-actions[bot]`. |
| A review label | Default `cursor-review`. Create it in your repo. |

## Caller

`.github/workflows/cursor-review.yml`:

```yaml
name: Cursor Review

on:
  pull_request:
    types: [labeled, unlabeled]

concurrency:
  # cursor-review declares no group of its own, so a caller-level group is safe
  # and worth having — it stops label-toggling from stacking panels.
  group: cursor-review-pr-${{ github.event.pull_request.number }}-${{ github.event.label.name }}
  cancel-in-progress: true

jobs:
  review:
    permissions:
      contents: read
      pull-requests: write
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
      bot_app_id: ${{ vars.REVIEW_BOT_APP_ID }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
      SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
      BOT_APP_PRIVATE_KEY: ${{ secrets.BOT_APP_PRIVATE_KEY }}
```

Then add your repo to `vars.CURSOR_REVIEW_CALLERS`.

## Required permissions

```yaml
contents: read
pull-requests: write   # posting the consolidated review
```

## Inputs

| Input | Default | Notes |
|---|---|---|
| `judge_model` | `claude-opus-4-8-thinking-max` | Consolidates the panel into one review. |
| `diff_size_cap` | `5000` | Skip review above this diff size. |
| `review_label` | `cursor-review` | The label that triggers a run. |
| `diff_excludes` | *(none)* | Paths to keep out of the reviewed diff — generated code, fixtures, vendored trees. |
| `workflows_ref` | `main` | **Set to your `uses:` SHA** — prompts load from this ref at run time. |
| `bot_app_id` | `''` | Post as your App. |
| `run_without_label` | `false` | Run on every PR rather than waiting for the label. |

## Gotchas

**The label must be applied by a GitHub App token, not `GITHUB_TOKEN`.** Events
raised by `GITHUB_TOKEN` do not trigger workflow runs, so a label applied by
another workflow using the default token silently fails to start a review. That
is exactly what [`cursor-review-auto-label.yml`](cursor-review-auto-label.md)
exists to handle.

**Applying the label does not guarantee a run.** If the event was swallowed,
remove the label, confirm it is gone, then re-add it.

**`run_without_label: true` reviews every PR.** On a busy repo that is a large
step up in spend. Start label-gated.
