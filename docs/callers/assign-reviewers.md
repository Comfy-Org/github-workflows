# `assign-reviewers.yml` — expertise-aware, load-balanced routing

Read [the shared caller contract](README.md) first.

## What it does

Routes a PR using configured path expertise and recent human approvals on the
same code. Each chosen owner must add coverage of the changed files. Workload
breaks relevance ties; a one-subsystem PR normally gets one owner even with the
default maximum of two. There are no random substitutions.

Each run logs its evidence and writes an Actions summary: additional files
covered, configured rules, supporting historical PR numbers, and known/unknown
open review load. It does not post a PR comment.

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
| `.github/reviewers.yml` in **your** repo | **Required for assignment.** The expertise map. |
| App permission `Actions: read` | Required for cached-history downloads; otherwise assignment uses live history. |
| `vars.REVIEWER_GROWTH_POOL` | Deprecated and ignored. No random assignments. |
| `vars.REVIEWER_LOAD_CAP` | Optional. Prefer below-cap owners among equally relevant candidates. |
| `vars.REVIEWER_EXCLUDE` | Optional. Logins to hard-exclude. |
| `vars.REVIEWER_AUTHOR_ALLOWLIST` | Optional. Whitespace-separated logins. When non-empty, only these **authors'** PRs are routed; everyone else's is skipped. Unset ⇒ every eligible author is routed. |

## Caller

`.github/workflows/assign-reviewers.yml`:

```yaml
name: Assign Reviewers

on:
  pull_request:
    types: [opened, ready_for_review]
  schedule:
    - cron: '17 */6 * * *'
  workflow_dispatch: {}

jobs:
  assign:
    # Same-repo, human-authored PRs only. On a fork PR — and on a Dependabot PR,
    # which is same-repo but reads the Dependabot secret store — the secret below
    # arrives empty, so the App-token step hard-fails and the PR carries a red X
    # for a routing decision that could never have been made. See the gotcha.
    if: >-
      github.event_name == 'pull_request'
      && github.event.pull_request.head.repo.full_name == github.repository
      && github.actor != 'dependabot[bot]'
    permissions:
      contents: read
    uses: Comfy-Org/github-workflows/.github/workflows/assign-reviewers.yml@<full-commit-sha>
    with:
      num_reviewers: 2
      history_workflow: assign-reviewers.yml
    secrets:
      CLOUD_CODE_BOT_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}

  refresh-history:
    if: >-
      (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')
      && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)
    permissions:
      contents: read
    uses: Comfy-Org/github-workflows/.github/workflows/assign-reviewers.yml@<full-commit-sha>
    with:
      generate_history: true
    secrets:
      CLOUD_CODE_BOT_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}
```

Pin both jobs to the same reviewed SHA. The shared caller bumper updates both
references in this file. To keep live-only routing, omit `history_workflow`, the
refresh job, and the schedule/manual triggers.

Then ask a maintainer to add your repo to the `ASSIGN_REVIEWERS_CALLERS` roster secret.

## Required permissions

```yaml
contents: read
```

The assignee write goes through the App token.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `generate_history` | `false` | Generate a manifest instead of assigning; only schedule/manual dispatch on the default branch is accepted. |
| `history_workflow` | `''` | Caller workflow filename that publishes the manifest, e.g. `assign-reviewers.yml`. Empty uses live history. |
| `reviewer_config_path` | `.github/reviewers.yml` | Where your expertise map lives. |
| `num_reviewers` | `2` | Maximum owners (clamped to 1–10). Extra owners must add file coverage. |
| `skip_label` | `skip-auto-assign` | Present on a PR ⇒ skip routing. |

## Shared history manifest

Generation is a second **job calling this same reusable workflow**, not a second
implementation. It uses the same collector as live routing, independent of the PR
author, author allowlist, exclusions, or reviewer map. It gathers up to 50 recent
merged PRs on the default branch and publishes `reviewer-history-v1`, a ZIP with
one `manifest.json` member. The JSON contains schema version, repository, base
branch, producing run ID, refresh timestamp, and records of PR number, changed
paths and final human approvers. Review bodies and credentials are never stored.

On a cache hit, the PR job reads the manifest instead of repeating the history
search and review/file lookups. Current changed files, ownership config, author
and exclusions, workload, assignability, and manual assignments are still live.
History can be up to **12 hours old**; cached evidence can include an approval
withdrawn since refresh. This is advisory owner routing, not approval enforcement.
The six-hour schedule tolerates a missed refresh. Artifacts expire after two days,
so four runs daily retain about eight small snapshots, with a 2 MiB payload limit.

Only successful, completed **schedule or workflow_dispatch** runs of the configured
caller on the same repository's default branch can supply a snapshot. PR and
workflow_run artifacts are never accepted. The reader checks repository, base,
run identity, schema, record structure and freshness, and reads exactly one bounded
JSON member in memory, without extracting or executing archive contents. Historical
file lists stop at three pages; 300+ file sweeps provide no routing evidence.

Absent, expired, malformed, incompatible, or inaccessible snapshots fall back to
the live collector. Failed refreshes publish nothing; partial history is never
saved. The run log reports cache hit/run ID/age or why live lookup was needed.
Non-default-base PRs use live history because the shared snapshot covers only the
default branch. Change the schema version when changing the evidence contract.

After merging the caller, open its Actions page and choose **Run workflow** on the
default branch to seed the snapshot immediately. Subsequent refreshes run on the
schedule. Check the `Refresh reviewer history` job for the record/byte count and
its `reviewer-history-v1` artifact, then verify that a qualifying PR reports
`History cache hit`. Before the first refresh, assignments still work via live
lookup. The App needs Actions read permission to download artifacts; the caller's
ambient token remains `contents: read` and is not widened.

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
skipped job reports as neutral instead. The base-SHA configuration read does not
change this caller contract; fork routing through `pull_request_target` is not offered.
[`ci-assign-reviewers.yml`](../../.github/workflows/ci-assign-reviewers.yml) in
this repo is the worked example.

**`REVIEWER_AUTHOR_ALLOWLIST` fails closed — a typo silently switches routing
off.** Every login that is not in the list is skipped, so a misspelled entry (or a
list that names nobody who actually opens PRs here) looks exactly like the
automation being disabled: green checks, no assignees, no error. Entries are
matched case-insensitively and a leading `@` is tolerated, but nothing else is
guessed at. After setting it, confirm on a real PR that the run logs
`is in REVIEWER_AUTHOR_ALLOWLIST — routing`. To turn scoping back off, clear the
variable rather than emptying it to whitespace — both work, but an unset variable
is the unambiguous "no scoping" state.

## How selection works

1. Read the expertise map at the PR's **base SHA**. Routing changes in a PR apply
   only after merge. Match current paths and original paths of renamed files.
   A matching rule defines the eligible roster for that file.
2. Look at up to **50 recently updated merged PRs in the last 90 days**, targeting
   the same base branch. Only human approvals from members, owners, or
   collaborators count. The last decisive review state must be APPROVED;
   dismissed approvals and later change requests do not count. One approval of
   the exact file is evidence; directory-only inference needs two separate PRs
   and a shared directory at least two levels deep. History refines the roster
   for mapped files and discovers eligible owners for unmapped files.
3. Rank by newly covered files. Each file has equal weight except recognizable
   generated files, vendored/build output, and lockfiles, which count one tenth.
   Among equal coverage, prefer owners below the optional load cap, then stronger
   approval evidence, then lower load, then login for a stable tie. Load counts
   open PRs assigned to the person in **this repository**, excluding their own
   PRs. The repository-scoped app cannot reliably measure the whole organization.
4. Confirm the chosen user can be assigned, then repeat only for uncovered files
   up to `num_reviewers`. A second person must cover something new.

If no rule and no credible historical evidence match, assign **one** member of
`default_pool`, clearly identified as a low-confidence fallback. A matched rule
whose entire roster is excluded does **not** fall back to unrelated people.
Keep at least two people in each rule and fallback pool so authorship/exclusions
leave an eligible owner. The PR author and `REVIEWER_EXCLUDE` are always removed,
case-insensitively, tolerating a leading `@`.

History is a bounded sample, not a complete ownership database. Huge historical
PRs (300+ files) are ignored as weak routing evidence. Directory proximity is a
heuristic, not proof of semantic expertise; maintain explicit rules for areas
where that distinction matters. Older or less frequently reviewed areas may
still use the configured fallback. Use the summary to improve those rules.

If history fails, discard partial history and use the configured map. Failed or
incomplete load queries remain **unknown**, never zero. An incomplete current
file list skips routing. An unavailable assignability check skips that candidate.

Existing non-author assignees or requested reviewers/teams suppress routing.
The reusable serializes assignment per PR and rechecks live assignments, draft,
closed/skip-label state, head SHA and base target immediately before writing. Manual changes
made during the analysis are respected; a push during analysis skips the stale
selection. There is still a small race with unrelated external writers after the
last read because GitHub's assignment API has no conditional-write operation.

**Globs are the durable part; names are not.** People change teams. Write bucket
globs deliberately and expect the roster inside them to churn.

**Seeding from commit authorship is usually wrong.** On a repo where most commits
have one author, authorship carries no routing signal — seed from who has
**approved** merged PRs in that area instead.
