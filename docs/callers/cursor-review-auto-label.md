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
| `vars.APP_ID` | **Required unless `bot_app_id` is set.** The CLOUD_CODE_BOT app id. |
| `secrets.CLOUD_CODE_BOT_PRIVATE_KEY` | **Required unless `bot_app_id` is set** — and must NOT be passed when it is. |
| `bot_app_id` (input) | Optional. App id to mint the label token from instead of `vars.APP_ID` — for a caller that would rather not hand this workflow the write-broad app's key. |
| `secrets.BOT_APP_PRIVATE_KEY` | Required **when** `bot_app_id` is set — and must NOT be passed when it isn't. |
| `vars.CURSOR_REVIEW_OPTED_IN_LOGINS` | The opt-in roster. Empty means nobody gets auto-labeled. |

Supply **exactly one complete pair**. A preflight step ahead of the mint fails
the run with an explicit `::error::` — never a silent fall back to the other
app — on every other shape:

| What you passed | Why it fails |
|---|---|
| `bot_app_id`, no `BOT_APP_PRIVATE_KEY` | Half a pair. |
| `BOT_APP_PRIVATE_KEY`, no `bot_app_id` | Half a pair — and usually a `bot_app_id:` expression that resolved empty (variable unset or misspelled). Falling back here would mint from the write-broad app you were trying to avoid, on a green run. |
| `bot_app_id` **and** `CLOUD_CODE_BOT_PRIVATE_KEY` | A mix. Migrating means *replacing* the `CLOUD_CODE_BOT_PRIVATE_KEY:` mapping, not adding beside it, so the write-broad key never enters the run. `secrets: inherit` trips this too — map the one secret explicitly. |
| Neither key | Nothing to mint with. |
| No `bot_app_id` and an empty `vars.APP_ID` | The default pair needs the app id too, not just the key. |

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

To mint from a narrower App instead, replace that caller's `uses:`/`secrets:`
block (its last three lines) with:

```yaml
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review-auto-label.yml@<full-commit-sha>
    with:
      bot_app_id: ${{ vars.APP_ID_PR }}
    secrets:
      BOT_APP_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PR_PRIVATE_KEY }}
```

Note the `CLOUD_CODE_BOT_PRIVATE_KEY:` line is *gone*, not kept alongside — a
caller that passes both is rejected at the preflight rather than quietly
carrying the write-broad key into the run.

That App still needs `pull-requests: write` on the installation — it is what
applies the label, and it is the only permission the minted token requests
(`permission-pull-requests: write`, on both credential paths) — and it must be a
real GitHub App, for the same GITHUB_TOKEN reason below.

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
| `bot_app_id` | `''` (empty) | App id to mint the label token from. Empty ⇒ `vars.APP_ID` + `secrets.CLOUD_CODE_BOT_PRIVATE_KEY`. Set ⇒ pair it with `secrets.BOT_APP_PRIVATE_KEY` and drop the CLOUD_CODE_BOT mapping. |

## Why the App token is mandatory

A label applied with the default `GITHUB_TOKEN` **does not trigger workflow
runs** — GitHub suppresses events raised by that token to prevent recursion. The
label would appear on the PR and no review would start, with nothing in any log
to explain it. Hence an App mint is required rather than optional here — what is
optional is only *which* App: `vars.APP_ID` + `CLOUD_CODE_BOT_PRIVATE_KEY` by
default, or `bot_app_id` + `BOT_APP_PRIVATE_KEY`.

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
