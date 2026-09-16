# `refresh-reviewers.yml` — reviewer-map drift detector

Read [the shared caller contract](README.md) first.

## What it does

Recomputes the caller repo's reviewer expertise map
(`.github/reviewers.yml`, the config [`assign-reviewers.yml`](assign-reviewers.md)
consumes) from git history and opens **one** idempotent single-file PR when the
committed map has drifted from reality. It is a drift **detector**, never a live
mutator: nothing is assigned and nothing merges — the whole deliverable is a
reviewable PR a human accepts or edits.

Scoring is recency-decayed commit touches per rule bucket: per commit, per rule
glob matched by at least one surviving changed file, `score += 0.5^(age_days /
half_life_days)` and `touches += 1`. Line counts are intentionally unused. Bot
authors, generated/churn paths, and non-collaborators are excluded (collaborators
rather than org members, because `addAssignees` silently drops non-collaborators).
The rewrite is surgical — only the `reviewers: [...]` / `default_pool: [...]`
lists change, every comment is preserved — and a rule that cannot reach its floor
of qualifiers is left unchanged.

Runs are idempotent: each re-run force-resets the same `pr_branch` from the
default branch and edits the one open drift PR in place, so duplicate PRs never
stack. A drift branch a human has pushed to is never force-reset, and a no-drift
run closes a stale still-bot-authored drift PR so an obsolete proposal cannot
linger mergeable.

## Prerequisites

| | |
|---|---|
| `vars.APP_ID` | **Required.** CLOUD_CODE_BOT app id (same app as `assign-reviewers.yml`). |
| `secrets.CLOUD_CODE_BOT_PRIVATE_KEY` | **Required.** The drift-PR branch push and PR open/edit are made by the app (`contents: write` + `pull-requests: write`), so the workflow's own token stays read-only. |
| `.github/reviewers.yml` in **your** repo | **Required.** The expertise map the generator rewrites. |

## Caller

`.github/workflows/refresh-reviewers.yml`, e.g. on a weekly schedule:

```yaml
name: CI - Refresh Reviewers

on:
  schedule:
    - cron: '0 6 * * 1'      # Mondays 06:00 UTC
  workflow_dispatch: {}

jobs:
  refresh:
    permissions:
      contents: read
    uses: Comfy-Org/github-workflows/.github/workflows/refresh-reviewers.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
      map_exclude: some-operator-login
    secrets:
      CLOUD_CODE_BOT_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}
```

## Required permissions

```yaml
contents: read
```

Every mutation (branch push, PR open/edit) goes through the App token, so the
calling job needs only `contents: read`.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `reviewer_config_path` | `.github/reviewers.yml` | Path in the caller repo to the expertise/path-glob reviewer config (same meaning as in `assign-reviewers.yml`). |
| `window_months` | `12` | How many months of git history to score. |
| `half_life_days` | `90` | Decay half-life in days for commit recency weighting. |
| `top_k` | `4` | Max experts per rule. |
| `floor` | `2` | Min experts per rule; below-threshold candidates backfill up to this, and a rule that still can't reach it is left unchanged. |
| `min_touches` | `5` | Raw commit-touches needed to qualify for a rule. |
| `min_score` | `1.5` | Decayed score needed to qualify for a rule. |
| `floor_min_touches` | `2` | Relaxed touch threshold used only for floor backfill. |
| `map_exclude` | `''` | Whitespace-separated logins never to place in the map (distinct from the runtime `vars.REVIEWER_EXCLUDE` — e.g. an operator login whose commits are largely agent-authored). |
| `extra_exclude_paths` | `''` | Newline-separated regexes appended to the built-in generated/churn path exclusion list. |
| `pr_branch` | `bot/refresh-reviewers` | Head branch for the drift PR (reset + reused across runs). Must never equal the default branch (hard error). |
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. No default on purpose: a floating default would let a caller SHA-pin `uses:` and still load the generator from a moving ref. |

## Gotchas

**`map_exclude` is not `vars.REVIEWER_EXCLUDE`.** `map_exclude` controls who may
appear in the **committed** map; `vars.REVIEWER_EXCLUDE` (read by
`assign-reviewers.yml`) controls who the assigner skips at PR time. Seed
`map_exclude` with operator logins whose commits are largely agent-authored —
their commit volume is not personal expertise.

**`workflows_ref` accepts any non-empty ref but warns on a non-SHA.** Like
`cursor-review`, `pr-size`, `agents-md-integrity` and
`coderabbit-config-validate`, this workflow `::warning::`s a `workflows_ref` that
is not a full 40-hex commit SHA (branches and tags are mutable and can skew
between jobs mid-run) and fails only on an empty or omitted value. Pin it to the
same SHA as `uses:` — the generator is loaded from that ref at run time, never
from your checkout, so a caller-side change can't rewrite the logic scoring it.

**No fork concern — it is not PR-triggered.** The workflow runs on a schedule or
manual dispatch, not on `pull_request`, so the fork/Dependabot empty-secret
pitfall that affects `assign-reviewers.yml` does not apply here.
