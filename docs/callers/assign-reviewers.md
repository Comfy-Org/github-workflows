# `assign-reviewers.yml` — expertise-aware, load-balanced routing

Read [the shared caller contract](README.md) first.

## What it does

Matches a PR's changed paths against a **caller-repo** `.github/reviewers.yml`
(path-glob → reviewers, plus a `default_pool`), drops the author and anyone in
`vars.REVIEWER_EXCLUDE`, ranks the remaining candidates by current open load
(steering off anyone at or over `vars.REVIEWER_LOAD_CAP`), may swap one slot for a
`vars.REVIEWER_GROWTH_POOL` member for new-folk randomization, and assigns the top
`num_reviewers`.

> **Despite the name, it writes the ASSIGNEE field, not reviewer requests.**
> Comfy-Org routes and alerts people via assignees, so an entry under
> `reviewers:` means "gets assigned".

The assignee write goes through the CLOUD_CODE_BOT App token rather than
`GITHUB_TOKEN`, so the calling job needs only `contents: read`. **This does not
buy fork support** — see the fork gotcha below.

## Prerequisites

| | |
|---|---|
| `vars.APP_ID` | **Required.** CLOUD_CODE_BOT app id. |
| `secrets.CLOUD_CODE_BOT_PRIVATE_KEY` | **Required.** |
| `.github/reviewers.yml` in **your** repo | **Required.** The expertise map. |
| `vars.REVIEWER_GROWTH_POOL` | Optional. Logins for new-folk randomization. |
| `vars.REVIEWER_LOAD_CAP` | Optional. Max open reviews before steering off. |
| `vars.REVIEWER_EXCLUDE` | Optional. Logins to hard-exclude. |

## Caller

`.github/workflows/assign-reviewers.yml`:

```yaml
name: Assign Reviewers

on:
  pull_request:
    types: [opened, ready_for_review]

jobs:
  assign:
    # Same-repo, human-authored PRs only. On a fork PR — and on a Dependabot PR,
    # which is same-repo but reads the Dependabot secret store — the secret below
    # arrives empty, so the App-token step hard-fails and the PR carries a red X
    # for a routing decision that could never have been made. See the gotcha.
    if: >-
      github.event.pull_request.head.repo.full_name == github.repository
      && github.actor != 'dependabot[bot]'
    permissions:
      contents: read
    uses: Comfy-Org/github-workflows/.github/workflows/assign-reviewers.yml@<full-commit-sha>
    with:
      num_reviewers: 2
    secrets:
      CLOUD_CODE_BOT_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}
```

Then add your repo to `vars.ASSIGN_REVIEWERS_CALLERS`.

## Required permissions

```yaml
contents: read
```

The assignee write goes through the App token.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `reviewer_config_path` | `.github/reviewers.yml` | Where your expertise map lives. |
| `num_reviewers` | `2` | How many people to assign. |
| `skip_label` | `skip-auto-assign` | Present on a PR ⇒ skip routing. |

## Your `reviewers.yml`

```yaml
default_pool:            # fallback when no rule matches — see the warning below
  - octocat
  - hubot

rules:
  - paths:
      - services/api/**
      - proto/**
    reviewers: [alice, bob]

  - paths:
      - infra/**
    reviewers: [carol]
```

[This repo's own `reviewers.yml`](../../.github/reviewers.yml) is a worked example
with commentary on how the buckets were seeded.

## Gotchas

**Dependabot PRs need the same skip as forks, for a different reason.** They are
same-repo, so the fork guard alone lets them through — but Dependabot-triggered
runs read the *Dependabot* secret store rather than Actions secrets, so
`CLOUD_CODE_BOT_PRIVATE_KEY` is still empty. The reusable *does* skip bot authors
(`pr.user.type === 'Bot'`), but that check runs inside the job, **after** the
App-token mint has already failed — so the red X lands before the skip is reached.
Hence `&& github.actor != 'dependabot[bot]'` in the guard above.

**Fork PRs are not routed, and the caller must skip them explicitly.** The
`pull_request` event withholds repository secrets from fork-originated runs, so
`CLOUD_CODE_BOT_PRIVATE_KEY` arrives empty and the App-token mint hard-fails — a
red check and no routing either way. Hence the `if:` guard in the caller above; a
skipped job reports as neutral instead. Routing forks would mean
`pull_request_target`, which runs privileged against untrusted head code while
this workflow reads `.github/reviewers.yml` from the head SHA — that combination
turns the expertise map into a real escalation path, so it is not offered.
[`ci-assign-reviewers.yml`](../../.github/workflows/ci-assign-reviewers.yml) in
this repo is the worked example.

**A matched bucket does not fall back to `default_pool`.** `default_pool` is
consulted only when *no* rule matched the changed paths. The author is dropped
**after** that choice, so a rule whose reviewer list is just the PR author matches,
yields a one-person candidate set, loses that person to the author-drop, and ends
at `No eligible candidates after exclusions — nothing to assign`. It never reaches
`default_pool`. Write each bucket with at least one member who is not the usual
author of changes in those paths.

**`default_pool` must never be one person** — and must not be whoever opens most
PRs in the repo. When it *is* used (no rule matched), the same author-drop applies,
so a single-member pool that happens to be the author assigns nobody.

**Globs are the durable part; names are not.** People change teams. Write bucket
globs deliberately and expect the roster inside them to churn.

**Seeding from commit authorship is usually wrong.** On a repo where most commits
have one author, authorship carries no routing signal — seed from who has
**approved** merged PRs in that area instead.
