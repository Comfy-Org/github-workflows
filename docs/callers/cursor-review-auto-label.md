# `cursor-review-auto-label.yml` — apply the review label for opted-in reviewers

Read [the shared caller contract](README.md) first.

## What it does

Companion to [`cursor-review.yml`](cursor-review.md). When a person becomes
responsible for a PR, this applies the review label that `cursor-review.yml`
triggers on — keeping the "fire on label" contract intact instead of bolting a
second trigger onto the review itself.

The opt-in roster lives in **your** repo as
`vars.CURSOR_REVIEW_OPTED_IN_LOGINS` (whitespace-separated GitHub logins). No
roster is baked into the workflow.

## Prerequisites

| | |
|---|---|
| `vars.APP_ID` | **Required.** The CLOUD_CODE_BOT app id. |
| `secrets.CLOUD_CODE_BOT_PRIVATE_KEY` | **Required.** |
| `vars.CURSOR_REVIEW_OPTED_IN_LOGINS` | The opt-in roster. Empty means nobody gets auto-labeled. |

## Caller

`.github/workflows/cursor-review-auto-label.yml`:

```yaml
name: Cursor Review Auto-Label

on:
  pull_request:
    # `assigned` is the core behavior. Add `opened` / `ready_for_review` to also
    # label at PR creation time.
    types: [assigned, opened, ready_for_review]

jobs:
  auto-label:
    # Same-repo, human-authored PRs only. Both fork PRs and Dependabot PRs run
    # without Actions secrets, so the App-token mint fails on either. See the
    # gotcha.
    if: >-
      github.event.pull_request.head.repo.full_name == github.repository
      && github.actor != 'dependabot[bot]'
    permissions:
      contents: read
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review-auto-label.yml@<full-commit-sha>
    secrets:
      CLOUD_CODE_BOT_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}
```

Then ask a maintainer to add your repo to the `AUTO_LABEL_CALLERS` roster secret on `Comfy-Org/github-workflows`
— that roster is what keeps your pin current
(see [Staying current](README.md#staying-current)).

## Required permissions

```yaml
contents: read
```

Only that. The label write goes through the App token, not `GITHUB_TOKEN`.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `review_label` | `cursor-review` | Must match `cursor-review.yml`'s `review_label`. |
| `skip_label` | `skip-cursor-review` | Present on a PR ⇒ never auto-label it. |
| `runs_on` | `'"ubuntu-latest"'` | JSON. Set for self-hosted runners. |

## Why the App token is mandatory

A label applied with the default `GITHUB_TOKEN` **does not trigger workflow
runs** — GitHub suppresses events raised by that token to prevent recursion. The
label would appear on the PR and no review would start, with nothing in any log
to explain it. Hence `vars.APP_ID` and the App private key are required rather
than optional here.

## Gotchas

**Dependabot PRs fail the same way, and the fork guard alone misses them.**
Dependabot branches live in the base repo, so they pass the cross-repo test, but
Dependabot-triggered runs read the *Dependabot* secret store rather than Actions
secrets — the private key is empty and the mint fails. This workflow has no
bot-author skip of its own, so the caller-level `&& github.actor !=
'dependabot[bot]'` above is what keeps dependency PRs green.

**Guard the job against fork PRs yourself.** `pull_request` withholds secrets from
fork-originated runs, so `CLOUD_CODE_BOT_PRIVATE_KEY` arrives empty and the
App-token mint hard-fails — e.g. the moment a maintainer assigns a reviewer to an
external contribution, that PR picks up a red check. The reusable does **not**
carry a fork guard of its own, so add the `if:` shown in the caller above. Nothing
is lost: [`cursor-review.yml`](cursor-review.md) skips fork PRs anyway, so the
label would have had nothing to trigger.

**Which moments fire is the caller's choice.** This workflow reacts to whatever
`pull_request` event you pass through; it does not pick triggers for you.

**Keep `review_label` in sync** between this workflow and `cursor-review.yml`. A
mismatch labels PRs that nothing is listening for.

**An auto-applied label is not proof a review ran.** Check the run, not the
label.
