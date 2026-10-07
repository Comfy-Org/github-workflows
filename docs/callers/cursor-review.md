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
pull-requests: write   # posting the consolidated review (and, at the round cap, the label + comment)
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
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. No default on purpose. The review prompts and scripts load from this ref at run time. Each job that checks them out carries its own `Require a pinned workflows_ref` step and fails fast on an empty value **and on a value that differs from the commit `uses:` resolved to** (`job.workflow_sha`, which the runner computes from the `uses:` pin, so a caller cannot set it); a runner too old to supply `job.workflow_sha` warns and skips that comparison rather than failing. But treat the whole step as a backstop, not a guarantee: a job the label gate skips never evaluates it, and the `Prior-review ledger` job is deliberately exempt (it must never fail the run, since the review matrix `needs:` it) — it falls back instead of erroring, and downgrades the same mismatch to a `::warning::`. |
| `bot_app_id` | `''` | Post as your App. |
| `ledger_prior_review` | `true` | Give each round the prior rounds' findings + author replies, so a refuted or deferred finding is not re-litigated. |
| `run_without_label` | `false` | Run on every PR rather than waiting for the label. **Also requires widening your caller's `types:`** — see the gotcha. |
| `blocking` | `false` | Adds the fail-closed **Blocking gate** check: red while any cursor-review finding thread is unresolved and non-outdated, and red when the round that should have produced those threads did not land (including an over-cap skip). Turning red into a merge block is a second, separate switch — see [the blocking-gate gotchas](#blocking-gate-gotchas). |
| `approve_max_severity` | `''` (off) | `medium`, `low` or `nit`: after each round the bot **approves** (pinned to the reviewed commit) when every finding is at or below that severity, and **requests changes** when any is above it. See [auto-approve](#auto-approve). |
| `approve_authors` | `''` (everyone) | Per-author opt-in for auto-approve: a comma- or space-separated list of GitHub logins (case-insensitive, leading `@` ignored). A PR whose author is not listed gets auto-approve **off** — `approve_gate` is `off`, no APPROVE or REQUEST_CHANGES is posted, the decision note says *auto-approve not enabled for author `<login>`* — while the panel still reviews and posts its threads and the approver withdraws its own earlier approvals and dismisses its own REQUEST_CHANGES, since narrowing the list moves no head SHA. Empty means every author; a non-empty list that names nobody (`,`, `@`) fails closed and auto-approves no one. See [auto-approve](#auto-approve). No effect without `approve_max_severity`. |
| `approve_scope` | `delta` | What an auto-approve round after the first gates on. `delta`: a finding above `approve_max_severity` blocks only when it sits inside a hunk of the verified incremental block (the changes since the last reviewed commit), re-raises an earlier finding whose thread is still unresolved (the judge names that thread in `repeat_of`, or the finding sits on the path and line of an earlier round's thread that is still open — so a re-raise of an unanswered thread gates without a `repeat_of`), or is High/Critical anywhere in the reviewed diff. Every other finding is still posted, as a thread marked *Outside this round's changes: not blocking auto-approve*, and is never auto-resolved — it stays open for a human (and still counts for the `blocking` gate). Round 1, an incremental block that was unavailable or discarded, an empty one (a pure rebase, or no new-side hunk), one naming a path that cannot be parsed, an unknown prior-review ledger, and a list of earlier open threads that could not be read all fail closed to `full`; so does an explicitly empty value (only the input's own default picks `delta`). The decision note states the scope used, the gating vs non-gating counts, and why each gating finding gated. `full`: every finding counts wherever it sits (the behaviour before this input). The panel reviews the full diff either way. No effect without `approve_max_severity`. |
| `defer_approval` | `false` | Only for a caller that runs [cursor-approve](cursor-approve.md) after this workflow: a round that would approve still reports `approve_gate` = `pass`, but posts **no** approval and resolves no thread, and withdraws the approver's own earlier auto-approvals and requests for changes instead — so cursor-approve's decide is the only approver. Requests changes as usual. No effect without `approve_max_severity`. |
| `approve_max_failed_reviewers` | `0` | How many panel reviewers may **error** (the cell ran but failed, timed out, or never uploaded) before auto-approve withholds its decision. `0` keeps the strict rule: any reviewer that did not complete means no decision. `N` tolerates up to N errored reviewers and names them in the decision (e.g. "approved with 1/6 reviewers errored: `<model>:edge-case`"). See [auto-approve](#auto-approve). No effect without `approve_max_severity`. |
| `max_rounds` | `5` | Cap on review rounds per PR (`0` → no cap). At or over it, no panel runs: the PR is labelled `needs-human-review`, one comment lists the latest round's open findings above the threshold, and the `approve_gate` output is `capped`. Removing the label resets the count. See [round cap](#round-cap-and-the-approve_gate-output). |
| `runs_on` | `'"ubuntu-latest"'` | JSON-encoded `runs-on` for `diff-size`, `preflight`, the `review` panel and `consolidate` only — the jobs that hold no write credential. Every other job stays on GitHub-hosted `ubuntu-latest`. A self-hosted pool is fine only if it is one-job-per-fresh-pod, identity-free and private-network-isolated (those jobs run models with shell over PR code), on linux/x64 with bash, git, curl, jq, python3, gh, tar and GNU coreutils. Empty falls back to the default, so `${{ vars.CURSOR_REVIEW_RUNS_ON }}` is safe while the variable is unset; e.g. `'["self-hosted", "linux", "x64"]'`. |

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

**The "hunks new since round N" block is always a subset of the diff being reviewed.** From round 2 onward the panel prompt carries a second block — the hunks new since the last reviewed commit — introduced as "the subset of the diff above". It is derived from two PR patches — the diff the previous round actually reviewed, taken against the merge base *that* round recorded (see the next note), versus the reviewed diff this round is running on — each of which is a diff against a merge base and so contains only your branch's own changes, and every section it shows is copied verbatim out of the reviewed diff. It can therefore never contain a hunk your PR does not carry — in particular, merging the base branch into your branch no longer drags that branch's commits into the block (BE-15558; the old `git diff LAST_REVIEWED...HEAD` formulation did, because with a merge commit at HEAD the merge base of those two commits *is* `LAST_REVIEWED`). A pure rebase, which shifts line numbers without changing a hunk, produces no block at all rather than re-flagging the whole PR.

**A retargeted PR gets one round with no block, on purpose.** Each consolidated review that actually reviewed something now carries a hidden *round sentinel* recording the commit it reviewed and the merge base it was diffed against (a round that failed outright, or in which every reviewer errored, records none — so the round after it takes the fail-closed path below), and the next round rebuilds the "already reviewed" side against **that recorded merge base** rather than recomputing one from your PR's current base (BE-15598). It has to: change the PR's base branch — or rewrite that branch — and the recomputed merge base moves, so everything your branch inherited from the old base appears on both sides and hunks the panel has never seen are quietly subtracted as already reviewed. That loss is invisible to the subset check below, since a block that is merely too small is still a subset. So the step **fails closed** instead: when there is no usable recorded merge base — the previous round predates this change, its sentinel did not parse, or the recorded commit is unreachable or no longer an ancestor of the reviewed commit — the *Diff size check* job logs which case it hit (`No recorded merge base for round N …` / `Recorded merge base … is unreachable or not an ancestor …`) and the panel simply reviews the full diff with no prioritization block. Nothing is skipped and no finding is suppressed. Every open PR sees exactly one such round after this rolls out, because the round it is comparing against was posted before the sentinel existed; the block comes back on the round after that.

The block is verified after it is built, and **discarded whole if the check fails**. If you see `::warning::Incremental diff discarded: it was not a subset of the reviewed diff (<n> foreign file(s), <a> vs <b> lines).` on the *Diff size check* job, it means some section of the block was not carried **byte for byte** by the reviewed diff — a file it does not have, or a hunk that did not match verbatim — or the block came out longer than it, and the whole thing was thrown away: the panel reviewed the **full diff alone**, which is always correct — it just lost the hint about where to spend budget first. Nothing was skipped and no finding was suppressed. The job also reports this as its `incremental_subset` output, which is `false` only in that discard case.

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

## Auto-approve

Opt in with `approve_max_severity` (`medium`, `low` or `nit`; anything else fails
the run). Off by default. Pass a repo variable so an admin can flip or kill it
without a PR:

```yaml
on:
  pull_request:
    # `synchronize`, `reopened` and `edited` are REQUIRED with auto-approve:
    # they dismiss the bot's earlier approval when new commits land (`reopened`
    # carries a push made while the PR was closed) or the base is retargeted.
    types: [labeled, unlabeled, synchronize, reopened, edited]
jobs:
  cursor-review:
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@<sha>  # v1
    with:
      workflows_ref: <same-sha-as-uses>
      approve_max_severity: ${{ vars.CURSOR_APPROVE_MAX_SEVERITY }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
      # Optional: approve as a user account (see below).
      APPROVER_TOKEN: ${{ secrets.CURSOR_APPROVER_TOKEN }}
```

**Trying it on a few authors first.** `approve_authors` limits auto-approve to
the PR authors it lists (comma- or space-separated logins; empty, the default,
means everyone). Pass it from a repo variable too, e.g.
`approve_authors: ${{ vars.CURSOR_APPROVE_AUTHORS }}`. For anyone else the round
runs and posts its threads exactly as before, but no review event is submitted,
`approve_gate` reports `off`, and the step summary says *auto-approve not enabled
for author `<login>`*. It is read from the caller at the PR's head like
`approve_max_severity`, so it narrows who is approved; it is not a security
boundary (see the trust model below).

After `Post review` lands, `auto-approve.py decide` submits one of:

- **APPROVE**, pinned to the reviewed commit, when every finding is at or below
  the threshold;
- **REQUEST_CHANGES** when any finding is above it, or has an unrecognised
  severity;
- **no decision** when the round can't be trusted: the judge did not adjudicate, a
  panel reviewer did not complete (beyond what `approve_max_failed_reviewers`
  tolerates — see below), the review did not land as threads (or some
  finding reached the review body only), the head moved or the base was
  retargeted mid-run, the PR state
  could not be read, an earlier round's thread above the threshold is still
  open, or the **reviewed diff is empty** — every changed path was stripped by
  `diff_excludes` or the generated-file classifier (or the change is a pure
  rename / mode / binary change with no content hunk), so zero findings means
  nobody looked, not that the change is clean. A no-decision round also **withdraws** the bot's own earlier approvals, so
  a round-1 approval does not keep counting through a degraded re-run, and
  posts one **standing REQUEST_CHANGES** carrying the reasons verbatim and the
  next step (re-run the round by removing and re-adding the `cursor-review`
  label, or — when the cause would recur, e.g. the findings did not land as
  threads or the reviewed diff is empty — a human is needed). Without it, a PR
  whose threads all get resolved would look done although nothing approved it.
  The next no-decision round replaces it (it never stacks); the next round that
  approves — or, under `defer_approval`, that passes, and cursor-approve's
  approval after it — dismisses it. A `capped` round (`needs-human-review`)
  posts none: the label already hands the PR to a human.

Whatever the outcome, `decide` then writes the **cursor-approve status card**
(one PR comment, edited in place each round; see
[cursor-approve.md](cursor-approve.md#the-status-card-contract)) — except for
an author `approve_authors` does not list, who gets no card. It is written as
the decide identity (`APPROVER_TOKEN`'s user when set), so pass the same
`APPROVER_TOKEN` to cursor-approve or its phases will start a second card.

**Tolerating an errored reviewer.** By default a single panel cell that errors
(one model having a bad minute) withholds the whole decision, and since a push
does not start a new round on a label-triggered caller, the PR then waits for a
human. `approve_max_failed_reviewers: N` lets the round be decided with up to N
cells whose status is `error`. It never tolerates more than that, and a round is
still withheld when:

- more than N reviewers errored;
- a cell has any status other than `ok` or `error` (missing, unknown), or is
  not an object at all;
- the panel metadata is missing or empty;
- a review type — `adversarial` or `edge-case` — has **no** reviewer that
  completed (checked whenever N > 0, errors or not). Some cells of one type may
  error within N, but never all of them, so nothing is approved with a whole
  pass missing.

`error` is not purely an infrastructure signal: a cell counts as `error` until
the reviewer reports finishing, so one that the PR's own content stalled or
derailed lands there too. N is therefore also how many reviewers a PR could
silence and still be decided; keep it small relative to the panel.

A decision taken over tolerated errors says so, e.g. ``Approved: every finding is
at or below `low` (approved with 1/6 reviewers errored: gpt-x:edge-case)``. The
same gate feeds `defer_approval`, so cursor-approve's axes see it too.

**Resolving the bot's own nits on approval.** A ruleset that requires every
conversation resolved would otherwise hold an approval hostage to the Low / Nit
threads it approved over. So after an APPROVE is posted **and** the post-write
re-read confirms the head and base did not move (and no `needs-human-review`
label landed), the approver identity resolves a thread when all of these hold:

1. it is unresolved;
2. its first comment was posted by the identity that posts the findings — the
   `bot_app_id` App's `<slug>[bot]`, else `github-actions[bot]` (passed in by the
   workflow, never read from the thread);
3. that comment's severity badge is at or below the threshold — unbadged
   threads are never resolved;
4. no other account has commented in it — any human reply, or another bot's,
   leaves it for a person.

Each thread is re-read just before it is touched (a reply that landed since the
snapshot leaves it for a person), then resolved with `resolveReviewThread`, then
given a reply — ``Resolved by auto-approve: Low finding, at or below the `low`
threshold, on commit abc1234.`` (hidden marker `<!-- cursor-review-auto-resolve -->`).
Resolve comes first so a failed resolve leaves no approver reply behind to make
the thread look human-touched on every later round.
If **any** live thread is above the threshold or unbadged, nothing is resolved,
not even the eligible ones; a REQUEST_CHANGES or "nothing" round resolves nothing.
A failure on one thread is logged and skipped: it never undoes the approval. A
round resolves at most 30 threads and stops after 3 failures in a row (a missing
permission or a rate limit); the rest wait for the next approving round. The step
summary logs the resolved / skipped-human / skipped-unbadged /
skipped-above-threshold / failed / deferred counts. Resolving a thread needs the
approver to have **write access** to the repo (e.g. `APPROVER_TOKEN` from a
member of a team with write); without it the approval still lands and each
resolve logs a warning. Withdrawing an approval later does not unresolve anything.

**On the blocking caller, give thread events their own concurrency slot.** A
resolve made with `APPROVER_TOKEN` or the bot App's token (not `GITHUB_TOKEN`)
fires `pull_request_review_thread: resolved`, and under the PR-number-only group
shown above that new run cancels the very run doing the resolving — mid-loop.
Key thread events apart:

```yaml
concurrency:
  group: cursor-review-pr-${{ github.event.pull_request.number }}${{ github.event_name == 'pull_request_review_thread' && format('-thread-{0}', github.run_id) || '' }}
  cancel-in-progress: true
```

Those runs only re-run the Blocking gate against live thread state, so letting
them overlap costs nothing.

How later rounds see such a thread: the blocking gate counts it as resolved, and
the prior-review ledger carries it with `resolved=true` plus the auto-resolve
reply. The ledger never counts that reply as an *answer*, even when the approver
is an OWNER / MEMBER / COLLABORATOR account: it gives no technical reason, so it
neither lets the judge drop the finding nor spends a `repeat_of` slot. In effect
a finding at or below the threshold is treated as addressed, which is the point
of the threshold.

The decision step is `continue-on-error`: a refused approval shows as a red
step with an `::error::`, not as a failed `Post review` job (which the blocking
gate would read as "the review did not land").

The **Dismiss stale auto-approval** job withdraws the bot's own marked
**approvals** when what was reviewed changes. It runs on every event of an open
PR and reads the PR's **live** head and base, not the event, so whichever event
runs next redoes a dismissal that a cancelled run left undone:

- an approval not on the current head — a push (`synchronize`, or `reopened` for
  a push made while the PR was closed);
- an approval recorded against a base other than the current one — a retarget
  (`edited`). The head did not move but the diff did, so the on-head approval
  goes too. Each approval records the base it was reviewed against; one posted
  before that record is reached only by the retarget's own `edited` run;
- the PR carries `skip-cursor-review` — every marked approval, same head or not;
  re-apply the trigger label after removing the veto to earn a fresh one.

If a stale marked approval belongs to a login this run cannot act as — the
approver identity changed, or the approver's secrets are not available to the
run (`APPROVER_TOKEN` / `BOT_APP_PRIVATE_KEY` on a Dependabot PR) — the job goes
**red** rather than passing unchecked; dismiss it by hand.

A request-changes is left in place — a push does not start a new panel under the
label-triggered caller, so only the next round (re-apply the label) supersedes it.

**The dismissal is not gated on `approve_max_severity`.** Unsetting the variable
is the kill switch for *new* approvals; the next push still withdraws any
approval already on a PR. On a repo that never approved, the job lists the
reviews, finds none of its own, and does nothing. It does key on the approver
identity, though: change `APPROVER_TOKEN` / `bot_app_id` while approvals are
live and the old identity's approvals can no longer be dismissed — the job goes
red on the next stale one until you dismiss it by hand.

**Widened callers: keep `edited` from cancelling a panel.** The label-only
caller above keys its group on `github.event.label.name`, so `edited` never
shares a group with the labelled run. If you dropped the label from the group
(the `run_without_label` / blocking shape), a title edit now cancels a running
panel. Give `edited` its own group:

```yaml
concurrency:
  group: cursor-review-pr-${{ github.event.pull_request.number }}${{ github.event.action == 'edited' && '-edited' || '' }}
  cancel-in-progress: true
```

**Known residual: the caller can drop the trigger.** For `pull_request` events
GitHub runs the caller workflow from the PR's head, so a commit can remove
`synchronize` (or the whole caller) and no dismissal runs for it. Closing that
needs dismissal from base-controlled code — e.g. a separate
`pull_request_target` dismiss-only job that checks out nothing. Until then,
treat this the same way as the trust model below.

**Pair it with `detect-unreviewed-merge`'s `ignore-approvers`.** Pass the
approver identity there, or the bot's approval satisfies that SOC 2 audit and a
PR merged with no human review files nothing. See
[`detect-unreviewed-merge.md`](detect-unreviewed-merge.md#automated-approvers-ignore-approvers).

**Trust model — read before letting the approval count.** The approval is
only as strong as two things a PR author controls:

- **Prompt injection.** Every signal `decide` gates on — the findings, the
  panel's status, the judge's status — is model output over the PR's own diff.
  The panel and judge run with the PR checked out, so a diff written to steer
  them can produce a clean round, and that round approves. No check computed
  outside those jobs can tell a genuinely clean diff from a convincing one.
- **The switch lives in the PR.** For `pull_request` events GitHub runs the
  caller workflow from the PR's head, so a same-repo PR can add
  `approve_max_severity` (and drop `synchronize`) in the very commit it wants
  approved. A repo variable keeps an *admin's* kill switch out of PRs; it does
  not stop a PR from editing the caller to ignore it.

So enabling auto-approve makes this approval no stronger than **push access**
to the repo. Do not let it be the only review that satisfies a ruleset for code
that needs a human's eyes — in particular, think twice before giving it an
`APPROVER_TOKEN` that is a code owner. Fork PRs never reach it (the `gate` job
skips them).

**Who approves.** `APPROVER_TOKEN` if set, else the `bot_app_id` App, else
`github-actions[bot]`. A GitHub App cannot be a CODE OWNER, so on a ruleset with
`require_code_owner_review` an App's approval is posted but does not count — use
`APPROVER_TOKEN` from a user account that is a code owner there. GitHub refuses
an approval of the approver's own PR; that is logged and skipped, not failed.

- **`github-actions[bot]` fallback** (no `APPROVER_TOKEN`, no `bot_app_id`) can
  approve only when the repo or org setting *Allow GitHub Actions to create and
  approve pull requests* is on. It is off by default; with it off the approve
  step goes red (the job does not).
- **Dismissal permission.** Where branch protection restricts who may dismiss
  reviews, add the approver identity to the allowed dismissers. Otherwise the
  dismiss job goes red and the stale review stays — `pull-requests: write` alone
  is not enough.

## Round cap and the `approve_gate` output

**`max_rounds`** (default `5`, `0` disables) stops a PR from cycling through
review rounds forever. Before the panel starts, the checkout-free **Round cap**
job counts the consolidated reviews (`## 🔍 Cursor Review — Consolidated panel`)
the posting identity — the `bot_app_id` App, else `github-actions[bot]` — has
already left on the PR. Reviews by anyone else carrying the same heading are not
counted, and neither are rounds that reviewed nothing (a "Review failed" error
review, or one where every panel cell failed). At or over the cap it:

- runs **no panel** (`Diff size check`, and everything after it, is skipped);
- adds the `needs-human-review` label, creating it if the repo lacks it.
  Creating a repo label needs `issues: write`, which the caller's
  `pull-requests: write` does not grant, so **create `needs-human-review` once
  by hand** unless the `bot_app_id` App has Issues write. If the label cannot be
  applied, the cap fails open for that run (the panel runs) with a warning,
  rather than capping a PR it gave no way to reset;
- posts **one** comment listing the latest round's open findings above
  `approve_max_severity` (every open finding when that is empty). A hidden
  `<!-- cursor-review-round-cap -->` marker keeps a re-trigger from posting it
  again for the same cap;
- sets `approve_gate` to `capped`.

While the label is on, auto-approve never approves the PR. **Removing the label
resets the cap**: only rounds after its most recent removal (read from the issue
timeline) count, so a human who has looked at the PR can hand it back to the bot
for another `max_rounds` rounds. A count that cannot be read fails open — the
panel runs, as it did before the cap existed. Under `blocking: true` a capped
head holds **Blocking gate** red, the same as an over-cap diff: no panel looked
at it. With `max_rounds` above 0 the gate reads the live label on every event,
so resolving the old threads cannot turn it green while `needs-human-review` is
on the PR — whoever applied it.

**`approve_gate`** is a workflow-level output for a downstream job that should
run only after a round passed the severity gate:

```yaml
jobs:
  cursor-review:
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@<sha>  # v1
    with: { workflows_ref: <same-sha-as-uses>, approve_max_severity: low }
    secrets: { CURSOR_API_KEY: "${{ secrets.CURSOR_API_KEY }}" }
  next:
    needs: cursor-review
    if: needs.cursor-review.outputs.approve_gate == 'pass'
```

| Value | Meaning |
|---|---|
| `pass` | The auto-approve decision was APPROVE (posted, unless `defer_approval` left it to cursor-approve). |
| `fail` | REQUEST_CHANGES, or an earlier round's open thread above the threshold withheld approval. |
| `untrusted` | The judge was degraded, a panel cell failed (beyond what `approve_max_failed_reviewers` tolerates), the review was not delivered, or the head moved — and also any run that delivered no round at all (an unrelated event, an already-reviewed head, an over-cap diff). |
| `capped` | The round cap was hit by this run, or the PR carries `needs-human-review` (while `max_rounds` or `approve_max_severity` is set). Wins over `off`. |
| `off` | `approve_max_severity` is empty, or `approve_authors` does not list the PR's author (and the cap was not hit). |

Two more outputs carry the count, for a "round R of M" display:

| Output | Value |
|---|---|
| `round` | The 1-based number of the round this run delivered, counted the way the cap counts (the posting identity's consolidated reviews since `needs-human-review` was last removed; a round that reviewed nothing is not counted). If this run hit the cap, it is the number of the last round. It is **empty** when the run delivered no round, the count could not be read, or `max_rounds` is `0`, because nothing is counted then. |
| `max_rounds` | The effective cap: the `max_rounds` input as applied, or `0` for no cap. A value the cap step rejects as not a whole number reports `0`, because it is not applied. |

One more, for [cursor-approve](cursor-approve.md) under `defer_approval: true`:

| Output | Value |
|---|---|
| `approve_scope_effective` | The scope this run's auto-approve decision actually gated under: `delta` only when `approve_scope` is `delta` and the round did not fail closed to `full` (round 1, an unavailable, discarded or empty incremental block, an unparseable path, an unknown ledger, an unreadable list of earlier open threads); `full` otherwise, including when no decision ran or the round withdrew it (`untrusted`/`capped`). Pass it as cursor-approve's `approve_scope`, so its deferred approval auto-resolves threads under the same non-gating rule this round's decide used. |
