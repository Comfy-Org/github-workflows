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

**Panel integrity is its own check, and it is always on.** A reviewer cell that
never submits no longer exits green, and a run whose panel came up short, whose
findings could not be anchored, or whose review never landed publishes a red
`Panel integrity` context. It needs no input and blocks nothing by itself — see
[Panel integrity](#panel-integrity).

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
  # KEEP THIS. The reusable owns a group of its own (see "The reusable owns a
  # group of its own" below), but it refines yours rather than replacing it.
  # Never name a caller group `cursor-review-reusable-*`.
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
  # KEEP THIS — it is what supersedes a running panel on push; the reusable's
  # own group only does that under `run_without_label: true` (see below). Drop
  # `label.name` from the key, and never name it `cursor-review-reusable-*`.
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

**The reusable owns a group of its own — it ADDS to your caller group, it does not replace it.** `cursor-review.yml` declares a workflow-level `concurrency: cursor-review-reusable-<pr>-<slot>` with `cancel-in-progress: true`, which reaches you at your next pin bump with no caller edit and no permission change. Its slot rule: the trigger label and `skip-cursor-review` share one `trigger` slot, so **applying `skip-cursor-review` mid-panel cancels the running panel**; every other label gets its own `label-<name>` slot, so an unrelated label add never kills a running review; `pull_request_review_thread` events stay out of `trigger`, so resolving a finding thread on a blocking caller cannot cancel a panel; and under `run_without_label: true` the four plain PR actions the gate accepts (`opened` / `reopened` / `ready_for_review` / `synchronize`) join `trigger` as well, so a push supersedes a running panel and the veto label can still reach it.

What it does **not** do is make your own group redundant. Keep the caller group in every shape above, and know what each side costs:

- **Under the default `run_without_label: false`, the reusable's group does not supersede on push.** That arm of the `trigger` slot is gated on the input, so a `synchronize` event lands in `label-` while the label-triggered panel sits in `trigger`, and the push cancels nothing. `post-review`'s `!cancelled()` guard exists to stop a review pinned to a superseded head SHA and depends on that cancellation — so a widened or blocking caller that drops its PR-number-only group posts reviews against stale diffs, and under `blocking: true` gates red on threads for code that no longer exists.
- **A PR-number-only caller group is coarser than the reusable's, and that is the price of the line above.** It puts *every* event for the PR in one slot, so an unrelated label add — or, on the blocking caller, a `pull_request_review_thread: resolved` — cancels the caller run, and with it the `uses:` panel job, before the reusable's per-slot group can isolate anything. The reusable's slots only refine what your own group has not already cancelled.

**Two hard rules, both of them the ones [`pr-size`](pr-size.md) carries:**

- **Never name a caller group `cursor-review-reusable-*`.** A caller that declares the reusable's own group deadlocks its own run — the caller holds the group while its `uses:` job waits to acquire it.
- **Call cursor-review from a dedicated workflow file, not as one job of a larger `ci.yml`.** Cancellation is run-scoped, so the reusable's group cancels the whole caller *run* — including builds, tests and deploys that have nothing to do with the review. Unlike the deadlock rule this one arrives silently at your next pin bump, with no caller edit to warn you, so check it before you bump.

**`review_label` must match your label's case exactly.** The slot expression compares it with a GitHub expression `==`, which is case-**in**sensitive, while the gate's own decision is a case-**sensitive** shell comparison. A label differing from `review_label` only in case therefore reaches the shared `trigger` slot and cancels a running panel, then no-ops in the gate — a review destroyed with nothing replacing it. GitHub expressions have no case-sensitive string compare, so the caller has to get this right.

**Veto mid-flight: on a pin BELOW that change, `skip-cursor-review` does not stop a running panel.** With only a caller-level group, `labeled: skip-cursor-review` and `labeled: cursor-review` land in *different* groups (the group key carries `label.name`), so the veto starts a run that no-ops in the gate while the panel it was meant to stop keeps going — and still posts its review. Do not try to fix it caller-side by collapsing to a PR-number-only group: that does cancel on the veto, but it also puts *every* label event in one group, so adding an unrelated label kills a running review. Bump your pin past the reusable's own group instead — there is nothing to change in the caller.

**`run_without_label: true` reviews every PR.** On a busy repo that is a large
step up in spend. Start label-gated.

## Panel integrity

`<caller job id> / Panel integrity` (with the caller above, `review / Panel
integrity`) is the context that answers **"was the panel that reviewed this PR a
whole panel?"** — it is the one an automated merge gate should read for that
question, and it runs on every review, with no input to turn on. Read the two
bullets at the end of this section before you require it: it is red when the
review ran and came up short *and* when the decision that selects a review
failed, but it is **skipped — and therefore green — on the runs that
deliberately review nothing**.

It is **advisory**: red here fails no other job, and the consolidated review
still posts. Marking it a required status check in your branch-protection /
ruleset settings is what makes red block a merge — the same two-switch shape as
the Blocking gate, and independent of it. The two answer different questions:
Panel integrity asks whether the review was *complete*, the Blocking gate asks
whether its findings were *addressed*.

Red means at least one of these, each named on its own `::error::` annotation in
the job log:

| Cause | What it means |
|---|---|
| Panel incomplete | Fewer cells submitted findings than ran. The consolidated review was adjudicated over a short panel. The individual leg checks (`adversarial (<model>)` / `edge-case (<model>)`) are red for exactly the cells that did not submit — almost always the 15-minute agent cap. |
| Unanchored findings | Findings the review could not anchor to a line of the reviewed diff, so they were demoted to the review **body** and have no thread. The Blocking gate cannot see them; read the body. |
| Nothing delivered | No review carrying resolvable finding threads reached the PR — a read-only token, a rejected inline payload, or a post that could not be confirmed. The findings are in the `Post review` job summary. |
| Judge degraded | The judge model never adjudicated; the review is the raw union of the cells' findings, so duplicates and false positives were not filtered out. |
| Panel never adjudicated / Post review failed | `Consolidate panel` or `Post review` did not succeed, so the values the causes above are read from are absent. Reported as its own cause rather than inferred from the empty outputs, because an absent output is not a clean one — and because `delivered=true` is written the moment the POST returns, so it can survive a `Post review` job that dies in a later step. |
| Cell counts missing | `ok_count`/`total` came back empty. Counted as an incomplete panel: unset-vs-unset compares equal, so without this the check would print "whole panel" having counted nothing. |

**Your caller job goes red with a failing leg, and that is the point.** A
reusable workflow's caller job takes the aggregate conclusion of the jobs inside
it, so a cell that did not submit now turns `review` red as well as its own leg
— on a measured ~40% of runs, because a stalled `cursor-agent` is common. That
red is the signal: it is what makes an incomplete panel visible to anything
reading `statusCheckRollup`, which is exactly what used to be impossible. Read
it as "this review is partial", not as "the review failed": the consolidated
review still posts, the Blocking gate is unaffected, and re-running the failed
legs or re-triggering the review is what clears it. If you want a *merge* gate,
require `Panel integrity` — do **not** require the caller job itself, which is
red for every unrelated infrastructure failure too.

**"Re-run failed jobs" posts a second review.** GitHub's re-run-failed-jobs
re-runs every job that *depends* on a failed one, so re-running a red leg also
re-runs `Consolidate panel` and `Post review` — while the green `Gate` is not
re-run and its cached `already_reviewed=false` is reused. `post-review.py`'s
landed-review check only fires when the POST itself *errors*, so a clean re-run
POSTs, and the PR ends up with two consolidated reviews. Prefer **re-running the
whole workflow** (which re-runs `Gate`, whose dup-check sees the review that
already landed) or re-triggering by label. Use re-run-failed-jobs when you
actually want a second, fuller review on the same commit.

Three more shapes to expect before you require it:

* **A cancelled run reports red.** GitHub counts a *skipped* required check as
  passing, so this job runs on cancellation rather than handing a superseded run
  a free green — the same reasoning the Blocking gate documents. Under the
  `cancel-in-progress` caller above that red lands on the head SHA that was
  superseded, not on the new one.
* **Do not require a leg check instead.** A panel cell's context name carries
  the model id (`edge-case (kimi-k3-high)`), so it changes whenever the panel
  list does — and a required check whose name no longer exists blocks every PR
  in the repo. `Panel integrity` is stable by design.
* **It is red, not skipped, when the decision itself failed.** Panel integrity
  is gated on the same four conditions the panel is. Three of them read `Gate`'s
  and `Diff size check`'s job *outputs*, which are empty when those jobs
  **failed**; the fourth reads the review matrix's *result*, which is `skipped`
  whenever **any** job the matrix `needs:` did not succeed — `Preflight —
  validate model catalog` or `Prior-review ledger`. Gating on those alone would
  skip this check exactly when a dup-check API call errored, the diff could not
  be built, or a delisted model stopped the panel before a single cell started —
  and GitHub counts a skipped required check as **passing**. So a failed `Gate`,
  `Diff size check`, `Preflight` **or `Prior-review ledger`** runs this job and
  fails it: an undecided run is not a clean run. (`Prior-review ledger` is built
  never to fail — every step in it is `continue-on-error` — but a job timeout,
  a cancellation or a lost runner is not a step outcome, and "rare" is the wrong
  bar for something that would otherwise hand you a green merge gate.)
* **It still skips when no review was warranted, and a skip is green.** The
  deliberate no-panel branches — no trigger label, an already-reviewed commit, a
  PR over the diff-size cap, a fork the panel cannot run on — are the ones where
  `Gate` and `Diff size check` both *succeeded* and said no panel should run.
  This check stays skipped there, and a required skipped check passes. That is
  the intended shape: it answers **"was the panel that ran whole?"**, not "was
  this PR reviewed at all?" If you need the second question gated too — most
  relevantly, if you do not want an over-cap PR merging unreviewed — require the
  Blocking gate, which fails closed on over-cap fresh reviews, and keep your own
  label policy. Do not read a skipped Panel integrity as "the panel was fine".
* **It detects a cell that went missing, not a cell that lied.** "Did this cell
  submit" is the `status` field of the artifact the cell itself wrote, and that
  cell's agent runs `--trust` with shell access over attacker-authored diff
  text. A prompt-injected cell can write `{"status": "ok"}` with zero findings
  and green both its own leg and the panel count. That is a real limit, not a
  quibble: this check is an availability signal — it catches the stalls, crashes
  and caps that make up essentially all of the observed failures — and is **not**
  an attestation that six independent reviews happened. Making the count
  forgery-resistant needs the submission recorded outside the cell's own
  writable job; until then, do not treat a green `Panel integrity` as proof
  against an adversarial PR.
* **One listed cause is wired but inert.** The job also reads
  `diff-size`'s `incremental_subset` — "were the cells prioritized onto hunks
  that were actually in the reviewed diff?" — and fails on a literal `false`.
  `Diff size check` does not publish that output yet (it arrives with the
  incremental-diff fix), so today the expression is the empty string, which
  counts as "not measured", i.e. a pass. Nothing in the rollup changes when it
  starts being published; it is documented here so the cause table is not read
  as a scope check that is already running.

## Blocking-gate gotchas

Everything in this section applies only once you pass `blocking: true`.

**A mid-panel veto leaves the check's verdict racy.** Applying `skip-cursor-review`
while a panel is running cancels that run and starts a second one, both on the
same head SHA, and both publish a `Blocking gate` check. The cancelled run trips
the "a fresh review was triggered but did not land" guard (`post-review` is
`cancelled`) and reports **red**; the veto run has `should_run=false`, skips that
guard, falls through to the live thread query and reports **green** unless an
earlier round left unresolved threads. Which one sticks is whichever job finishes
last, and nothing orders them. Treat a red gate straight after a veto as "re-run
it", not as a finding: re-applying the trigger label, resolving the threads, or
re-running the gate job settles it. The `always()` on that job is deliberate and
is not the bug — a cancelled run that *skipped* the gate would mint a green
required check, which is the exact fail-open BE-4691 added the job to close. Which
verdict a vetoed PR *should* get is a policy question and is tracked separately;
until it is settled, do not require the check on a repo where mid-panel vetoes are
routine.

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
  # KEEP THIS — with `run_without_label: false` the reusable's own group does
  # not cancel a panel on push, and this gate reports on the head SHA (see
  # "The reusable owns a group of its own"); never name it
  # `cursor-review-reusable-*`. PR number only — label.name is empty on the
  # widened events, and split groups can't cancel each other (see the
  # run_without_label gotcha).
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
`skip-cursor-review` stops new panels from running and cancels a running one; it does not resolve the
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
