# `cursor-review.yml` — label-triggered multi-model code review

Read [the shared caller contract](README.md) first.

## What it does

A 4-lab × 2-review-type `cursor-agent` panel runs adversarial and edge-case
passes over the PR diff. A judge model consolidates them into **one** PR review
with per-finding severity badges. The person who applied the label gets Slack
start/complete DMs.

**Advisory only, and there is currently no supported way to make it blocking.**
The panel posts the review and succeeds regardless of what it found; no input
fails the run on findings.

Do **not** try to build a gate by marking `… / Consolidate panel` a required
check. GitHub counts a *skipped* required check as passing, and that job is
`if:`-gated on five conditions — it skips when the trigger label is absent, when a
review already exists for the head SHA (the dedupe below), when the diff is over
`diff_size_cap`, on fork PRs, and when the panel itself is skipped. So the check
goes green in exactly the cases where no review ran, which is the opposite of a
gate.

A real opt-in gate did exist — a `blocking:` input and a fail-closed **Blocking
gate** job — but both were dropped from `cursor-review.yml` in
[#31](https://github.com/Comfy-Org/github-workflows/pull/31), which was otherwise
a judge-extraction fix. Its script (`.github/cursor-review/gate-unresolved.py`)
is still in the tree with its CLI unwired — the module itself is still imported
by `build-ledger.py` for the shared review-thread query and helpers, so it is not
dead code. Restoring the gate is tracked separately; until then, treat this
review as advisory and gate on human approval.

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
    # Label-gated mode. If you set `run_without_label: true` below, this list
    # must also carry [opened, reopened, ready_for_review, synchronize] — see
    # the gotcha at the bottom.
    types: [labeled, unlabeled]

concurrency:
  # cursor-review declares no group of its own, so a caller-level group is safe
  # and worth having — it stops label-toggling from stacking panels.
  # NOTE: label.name is part of the key only because this caller is label-only.
  # Drop it if you widen `types:` — see the run_without_label gotcha.
  group: cursor-review-pr-${{ github.event.pull_request.number }}-${{ github.event.label.name }}
  cancel-in-progress: true

jobs:
  review:
    # A no-op while label-gated, but load-bearing the moment you widen `types:`
    # or set `run_without_label` — see the Dependabot gotcha.
    if: github.actor != 'dependabot[bot]'
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

Then ask a maintainer to add your repo to the `CURSOR_REVIEW_CALLERS` roster secret.

## Required permissions

```yaml
contents: read
pull-requests: write   # posting the consolidated review
```

## Inputs

| Input | Default | Notes |
|---|---|---|
| `judge_model` | `claude-opus-5-thinking-max` | Consolidates the panel into one review. |
| `diff_size_cap` | `5000` | Skip review above this diff size. |
| `ignore_comments` | `true` | Discount blank/comment-only lines from the size count (count-only — the panel still sees them). |
| `review_label` | `cursor-review` | The label that triggers a run. |
| `extra_generated_globs` | `**/node_modules/**`<br>`**/dist/**`<br>`**/vendor/**`<br>`**/*.generated.*`<br>`**/*.min.js`<br>`**/*.min.css` | Extra globs the shared `check-pr-size` classifier treats as generated — kept out of **both** the size-budget count and the reviewed diff. Passing your own value **replaces** the default list, so re-state the entries you still want — **copy them verbatim**, `**/…/**` and all: a pattern with no `/` matches only the *base name*, so a bare `node_modules` matches a file literally named `node_modules` and excludes nothing under the directory; and conversely a pattern that *does* contain a `/` is anchored to the whole repo-relative path unless it opens with `**/`, so `data/gen.json` matches only the root-level file and misses `packages/x/data/gen.json`. These are plain globs, **not** git pathspecs — never carry a `:!` prefix over from `diff_excludes` (see that row). `.claude` is deliberately **not** in the default: hand-authored agent instructions are prose worth reviewing. A repo whose `.claude/` tree is vendored/tool-installed output (a BMAD-method install, say) should pass the defaults above plus `**/.claude/**` — otherwise that tree now counts toward `diff_size_cap`, and a PR over the cap is skipped silently (no review comment, no Slack notice). |
| `extra_lockfiles` | `''` | Extra dependency-lockfile base names, on top of the classifier's built-ins. |
| `diff_excludes` | `''` | Pathspecs excluded from the reviewed diff **only** (not the size count) — back-compat escape hatch; prefer `extra_generated_globs`. Each entry must carry git pathspec-magic (`:!**/foo/**` or `:(exclude)**/foo/**`); the value is word-split into `git diff … -- . <entries>`, so a plain path is OR'd with `.` and excludes nothing. **Migrating:** this input used to exclude from *both* the count and the diff. If your caller lists generated paths here, move them to `extra_generated_globs` — left here they still leave the reviewed diff but are now counted, which can push the PR over `diff_size_cap`. **Strip the `:!` / `:(exclude)` prefix on the way over:** `extra_generated_globs` takes plain globs, not pathspecs, and the classifier compiles each token literally — a verbatim `:!**/vendor/**` becomes the anchored regexp `^:!(?:.*/)?vendor/.*$`, which matches no repo-relative path, so the exclusion silently vanishes from both the count and the diff (only `extra_lockfiles` validates its entries). Write `**/vendor/**`. |
| `workflows_ref` | **required** (no default) | **Set to your `uses:` SHA** — prompts load from this ref at run time. |
| `bot_app_id` | `''` | Post as your App. |
| `ledger_prior_review` | `true` | Give each round the prior rounds' findings + author replies, so a refuted or deferred finding is not re-litigated. |
| `run_without_label` | `false` | Run on every PR rather than waiting for the label. **Also requires widening your caller's `types:`** — see the gotcha. |

## Gotchas

**The label must be applied by a GitHub App token, not `GITHUB_TOKEN`.** Events
raised by `GITHUB_TOKEN` do not trigger workflow runs, so a label applied by
another workflow using the default token silently fails to start a review. That
is exactly what [`cursor-review-auto-label.yml`](cursor-review-auto-label.md)
exists to handle.

**Applying the label does not guarantee a run.** If the event was swallowed,
remove the label, confirm it is gone, then re-add it.

**One review per commit — re-labeling alone will not re-review.** The gate skips
the panel if a non-dismissed consolidated review already exists for the PR's HEAD
SHA, so a remove-and-re-add on unchanged content no-ops by design. To get a fresh
review of the same commit, **dismiss the existing review first**, then apply the
label.

Pushing a commit clears the dedupe (new head SHA), but with the label-gated caller
above it does **not** by itself start a run — `types: [labeled, unlabeled]` omits
`synchronize`, so a push delivers no event at all. After pushing you still toggle
the label. Add `synchronize` to `types:` if you want every push re-reviewed (and
see the spend warning below).

**Dependabot PRs are not covered by the fork skip.** Dependabot's branches live in
the base repo, so the gate's cross-repo check treats them as ordinary PRs — but
Dependabot-triggered runs read the *Dependabot* secret store, not Actions secrets.
`CURSOR_API_KEY` therefore arrives empty and the token is read-only, so under
`run_without_label: true` (or a `synchronize` trigger) every dependency PR burns
the matrix and fails red. The caller above already carries the guard that keeps
those PRs out — keep it when you widen `types:`:

```yaml
    if: github.actor != 'dependabot[bot]'
```

**Fork PRs are skipped, deliberately.** `pull_request` withholds secrets from
fork-originated runs, so `CURSOR_API_KEY` would be empty (every panel cell
produces nothing) and `GITHUB_TOKEN` read-only (posting the review 403s). The gate
detects the cross-repo head and skips cleanly rather than burning the matrix and
failing red on every external contribution. Do not "fix" this with
`pull_request_target` — that runs privileged against untrusted head code.

**`run_without_label: true` needs a wider trigger, or it never fires.** The input
only tells the reusable's gate to accept `opened` / `reopened` /
`ready_for_review` / `synchronize`; it cannot add those events to *your* `on:`
block. A caller left on `types: [labeled, unlabeled]` therefore still runs only
when a label is toggled — ordinary PRs are silently never reviewed, with nothing
in any log to explain it. Set both together:

```yaml
on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize, labeled, unlabeled]

concurrency:
  # Drop `label.name` from the key — see below.
  group: cursor-review-pr-${{ github.event.pull_request.number }}
  cancel-in-progress: true
# ...
    with:
      run_without_label: true
```

**Drop `github.event.label.name` from the concurrency group when you widen
`types:`.** That expression is empty on `opened`/`synchronize`/`reopened`, so
label events and plain PR events resolve to *different* groups and cannot cancel
each other. A push racing a label toggle then runs two 8-cell panels
concurrently — and because the head-SHA dedupe is evaluated before either posts,
both pass it and you get two panels and two reviews. Keying on the PR number
alone keeps every trigger in one group.

Keep `labeled`/`unlabeled` in the list even in label-free mode: the label path
stays live alongside it, which is how you force a re-review on an unchanged commit
(dismiss the existing review, then apply the label — see the dedupe gotcha below).

**`run_without_label: true` reviews every PR.** On a busy repo that is a large
step up in spend. Start label-gated.
