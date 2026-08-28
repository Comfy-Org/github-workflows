# `cursor-review.yml` — label-triggered multi-model code review

Read [the shared caller contract](README.md) first.

## What it does

A 3-lab × 2-review-type `cursor-agent` panel runs adversarial and edge-case
passes over the PR diff. A judge model consolidates them into **one** PR review
with per-finding severity badges. The person who applied the label gets Slack
start/complete DMs.

**Advisory by default, blocking on opt-in.** Out of the box the panel posts the
review and succeeds regardless of what it found. Passing `blocking: true` adds a
fail-closed **Blocking gate** job that goes red while the PR has unresolved,
non-outdated cursor-review finding threads: resolve every thread — or push a fix
and re-review, since a thread whose hunk changed is outdated and stops counting
— and it goes green. Blocking the merge takes **two switches**, because a
workflow cannot set branch protection: the input turns the check on, and marking
`<caller job id> / Blocking gate` (with the caller below, `review / Blocking
gate`) a required status check in your branch-protection / ruleset settings is
what makes red actually block. Until you flip the second switch the red check is
visible but advisory — a deliberate rollout state. Read [the blocking-gate
gotchas](#blocking-gate-gotchas) before requiring the check. (The gate shipped
in [#16](https://github.com/Comfy-Org/github-workflows/pull/16), was dropped by
accident in [#31](https://github.com/Comfy-Org/github-workflows/pull/31), and
was restored by BE-4691.)

Require the **Blocking gate** check and no other. Do **not** try to build a gate
by marking `… / Consolidate panel` a required check. GitHub counts a *skipped*
required check as passing, and that job is `if:`-gated on five conditions — it
skips when the trigger label is absent, when a review already exists for the
head SHA (the dedupe below), when the diff is over `diff_size_cap`, on fork PRs,
and when the panel itself is skipped. So the check goes green in exactly the
cases where no review ran, which is the opposite of a gate. The `… / Post
review` job is no better a candidate: it `needs:` Consolidate panel and runs
only when that job SUCCEEDS, so it skips in every one of those cases and in the
failure cases besides. The Blocking gate does not have this hole: with
`blocking: true` it runs on every event the caller delivers, so its verdict is
always a live query of the PR's thread state, never a skip.

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
| `judge_model` | `claude-opus-5-thinking-xhigh` | Consolidates the panel into one review. |
| `panel_models` | `''` | JSON array of model ids replacing the built-in panel list (each runs both review types; preflight validates them against the live catalog). Use for per-repo experiments such as a reasoning-tier A/B. |
| `skip_bot_branch_prefixes` | `ci/bump- chore/refresh- auto/refresh-` | Skip the panel for Bot-authored PRs on these branch prefixes (machine pin bumps / refreshes). `''` to review every bot PR. |
| `diff_size_cap` | `5000` | Skip review above this diff size. An over-cap PR is not silent — see the gotcha below. Under `blocking: true` it is also not green: an unreviewed PR cannot pass the gate. |
| `ignore_comments` | `true` | Discount blank/comment-only lines from the size count (count-only — the panel still sees them). |
| `review_label` | `cursor-review` | The label that triggers a run. |
| `extra_generated_globs` | `**/node_modules/**`<br>`**/dist/**`<br>`**/vendor/**`<br>`**/*.generated.*`<br>`**/*.min.js`<br>`**/*.min.css` | Extra globs the shared `check-pr-size` classifier treats as generated — kept out of **both** the size-budget count and the reviewed diff. Passing your own value **replaces** the default list, so re-state the entries you still want — **copy them verbatim**, `**/…/**` and all: a pattern with no `/` matches only the *base name*, so a bare `node_modules` matches a file literally named `node_modules` and excludes nothing under the directory; and conversely a pattern that *does* contain a `/` is anchored to the whole repo-relative path unless it opens with `**/`, so `data/gen.json` matches only the root-level file and misses `packages/x/data/gen.json`. These are plain globs, **not** git pathspecs — never carry a `:!` prefix over from `diff_excludes` (see that row). `.claude` is deliberately **not** in the default: hand-authored agent instructions are prose worth reviewing. A repo whose `.claude/` tree is vendored/tool-installed output (a BMAD-method install, say) should pass the defaults above plus `**/.claude/**` — otherwise that tree now counts toward `diff_size_cap`, and a PR over the cap is skipped silently (no review comment, no Slack notice). |
| `extra_lockfiles` | `''` | Extra dependency-lockfile base names, on top of the classifier's built-ins. |
| `diff_excludes` | `''` | Pathspecs excluded from the reviewed diff **only** (not the size count) — back-compat escape hatch; prefer `extra_generated_globs`. Each entry must carry git pathspec-magic (`:!**/foo/**` or `:(exclude)**/foo/**`); the value is word-split into `git diff … -- . <entries>`, so a plain path is OR'd with `.` and excludes nothing. **Migrating:** this input used to exclude from *both* the count and the diff. If your caller lists generated paths here, move them to `extra_generated_globs` — left here they still leave the reviewed diff but are now counted, which can push the PR over `diff_size_cap`. **Strip the `:!` / `:(exclude)` prefix on the way over:** `extra_generated_globs` takes plain globs, not pathspecs, and the classifier compiles each token literally — a verbatim `:!**/vendor/**` becomes the anchored regexp `^:!(?:.*/)?vendor/.*$`, which matches no repo-relative path, so the exclusion silently vanishes from both the count and the diff (only `extra_lockfiles` validates its entries). Write `**/vendor/**`. |
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. No default on purpose. The review prompts and scripts load from this ref at run time. Each job that checks them out carries its own `Require a pinned workflows_ref` step and fails fast on an empty or omitted value — but treat that as a backstop, not a guarantee: a job the label gate skips never evaluates it, and the `Prior-review ledger` job is deliberately exempt (it must never fail the run, since the review matrix `needs:` it) and falls back instead of erroring. |
| `bot_app_id` | `''` | Post as your App. |
| `ledger_prior_review` | `true` | Give each round the prior rounds' findings + author replies, so a refuted or deferred finding is not re-litigated. |
| `run_without_label` | `false` | Run on every PR rather than waiting for the label. **Also requires widening your caller's `types:`** — see the gotcha. |
| `blocking` | `false` | Adds the fail-closed **Blocking gate** check: red while any cursor-review finding thread is unresolved and non-outdated, and red when the round that should have produced those threads did not land (including an over-cap skip). Turning red into a merge block is a second, separate switch — see [the blocking-gate gotchas](#blocking-gate-gotchas). |

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

**An over-cap PR gets no review, and now says so.** When the counted diff exceeds `diff_size_cap` the panel is skipped and the run still goes green — nothing about it is a failure. So the skip announces itself in three places instead: a `::warning::` annotation and a step-summary block on the *Diff size check* job (both credential-free, so they still show on Dependabot PRs, whose runs can't read Actions secrets), plus a sticky PR comment naming the counted total and the cap. Get the PR under the cap and **re-apply the label** — with the label-gated caller above a push alone starts no run — and that comment flips to ✅. The comment posts as your bot app when `bot_app_id` + `BOT_APP_PRIVATE_KEY` are set and as `github-actions[bot]` otherwise, so it works out of the box; if the write fails it degrades to the annotation and the summary and the job log says why. The comment path is best-effort throughout — it never reddens the run. Note that **fork PRs get neither half**: the gate skips a cross-repo head before the size check runs, so a fork PR is skipped for being a fork, whatever its size. **Under `blocking: true` an over-cap PR does not go green** — the Blocking gate holds it red, because diff size is author-controlled and "too big to review" is not evidence a PR is clean; see [the blocking-gate gotchas](#blocking-gate-gotchas).

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
each other. A push racing a label toggle then runs two full panels
concurrently — and because the head-SHA dedupe is evaluated before either posts,
both pass it and you get two panels and two reviews. Keying on the PR number
alone keeps every trigger in one group.

Keep `labeled`/`unlabeled` in the list even in label-free mode: the label path
stays live alongside it, which is how you force a re-review on an unchanged commit
(dismiss the existing review, then apply the label — see the dedupe gotcha below).

**`run_without_label: true` reviews every PR.** On a busy repo that is a large
step up in spend. Start label-gated.

## Blocking-gate gotchas

Everything in this section applies only once you pass `blocking: true`.

**Widen your triggers before you require the check, or pushes brick the PR.**
A required check that never *reports* on the head SHA blocks merge as
"Expected", and the label-only caller above delivers no event on push — so
after any push the PR stays blocked until someone toggles the label. The
blocking caller shape is:

```yaml
on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize, labeled, unlabeled]
  pull_request_review_thread:
    types: [resolved, unresolved]

concurrency:
  # PR number only — label.name is empty on the widened events, and split
  # groups can't cancel each other (see the run_without_label gotcha).
  group: cursor-review-pr-${{ github.event.pull_request.number }}
  cancel-in-progress: true
```

These extra events are cheap: unless you also set `run_without_label: true`,
the gate's trigger step says "don't review" on all of them, so the panel, the
DMs and the over-cap comment all stay skipped — the only thing that runs is the
Blocking gate itself, re-querying live thread state and reporting on the new
SHA. The `pull_request_review_thread` events are what flip the check green the
moment the last thread is resolved, with no label dance.

**The gate enforces "no unresolved findings", not "a review happened".** A PR
that never gets the trigger label has no finding threads, so its gate reports
green. If you want every PR reviewed, pair `blocking: true` with
[`cursor-review-auto-label.yml`](cursor-review-auto-label.md) or
`run_without_label: true` — the gate then binds what those trigger. Fork PRs
are the same story: they can't run the panel (see above), so they never gate
red.

**Neither the skip label nor removing the trigger label waives the gate.**
`skip-cursor-review` stops new panels from running; it does not resolve the
threads an earlier panel already posted, and neither does taking the trigger
label off. Once findings exist, the ways out are resolving each thread, pushing
a fix that outdates them, or a ruleset bypass. Dismissing the review does not
clear it either — dismissal changes the review's state, not its threads'.

**Anyone with write access — including the PR author — can resolve threads.**
GitHub's thread-resolution permission is what it is: this gate guarantees every
finding was explicitly looked at and closed out, not that a second person
approved the closure. It complements a required human approval; it does not
replace one.

**The fresh-review path is fail-closed.** When a run was supposed to produce a
review and the pipeline broke, the gate refuses to pass rather than reporting
green over a review that never landed. Re-run the failed jobs or re-trigger the
review to clear it. It holds the check red on all of:

* the trigger job itself failing, so whether the PR should have been reviewed is
  unknown;
* the diff-size or post-review job failing;
* a post-review job that *succeeded* without delivering a review to the PR. A
  zero exit is not proof: post-review writes the review to the job summary
  instead when its token is read-only (403), posts a body-only "Review failed"
  review when the judge crashed, and posts a no-findings review when every panel
  cell errored. It reports which of those happened as a job output, and the gate
  requires the positive statement rather than inferring it from the exit code;
* a review that landed but whose findings all went into the review *body* with no
  inline thread (the anchors missed the reviewed diff, or GitHub rejected the
  inline payload) — findings a thread query cannot see;
* **the diff being over `diff_size_cap`.** No panel ran, so nothing was reviewed.
  Get the PR under the cap, or raise the cap on the caller;
* being triggered by an event with no pull request in its payload (`merge_group`,
  `push`, `workflow_dispatch`), which the gate cannot judge — use the trigger
  shape above.

Cancelling a run does not waive it either: the gate runs on cancellation too,
because GitHub counts a *skipped* required check as passing.

**Two things the gate still cannot promise.** Both are limits of what review
threads can express, not bugs, and it is worth knowing them before you require
the check:

* **It does not prove the current head SHA was reviewed.** A thread stops
  counting once its hunk changes (`isOutdated`), and with a label-gated caller a
  push runs no new panel — so a cosmetic edit to the flagged lines can outdate
  every finding and turn the check green with no re-review. Pair `blocking: true`
  with [`cursor-review-auto-label.yml`](cursor-review-auto-label.md) or
  `run_without_label: true` if you need every head SHA actually reviewed. (The
  `isOutdated` waiver is deliberate: without it, findings a fix already
  superseded would have to be resolved by hand before the check could ever go
  green.)
* **It gates the findings that got a thread, not every finding.** In a round
  where *some* findings were demoted to the review body, the gate holds red on
  the inline half and is silent about the demoted half — deliberately, since a
  body-only finding has no thread to resolve and failing on it would be a check
  nothing could ever clear. Read the review body, not just the threads. The
  fully-demoted case, where *no* finding got a thread, is caught by the
  fail-closed list above.
