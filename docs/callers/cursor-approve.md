# cursor-approve — per-axis approval after a passing cursor-review round

`cursor-approve.yml` approves a PR when independent per-axis reviewers agree it
is ready. Each axis is its own reusable workflow that declares only the secrets
it needs, so an axis never sees another axis's credentials. This guide covers
the two credential-free axes, `axis-correctness.yml` and `axis-conformance.yml`;
the context axes (business, design, completeness) are not available yet.

## What it does

1. `cursor-review` runs a round and exposes `approve_gate`, `round` and `max_rounds`.
   Call it with `defer_approval: true`: a passing round then reports
   `approve_gate == 'pass'` **without** posting an approval (and without
   resolving any thread), and withdraws the approving identity's own earlier
   approvals and requests for changes. Without it, cursor-review's `decide` APPROVES first, and that
   severity-only approval satisfies branch protection or auto-merge until the
   start phase withdraws it — and stays standing if the cursor-review run is
   cancelled after approving, or if its `approve_gate` comes out `untrusted`,
   because the start phase and the axes are then skipped. (A run that merely
   went red on an errored panel cell does NOT skip them while the gate reads
   `pass` or `capped`; see "A red panel cell must not skip the axes".) With it,
   this workflow's decide phase is the only approver.
2. `cursor-approve` with `phase: start` first withdraws the approving
   identity's own approvals — a backstop: with `defer_approval: true` there is
   none from this round, but a caller that does not set it has one standing
   from cursor-review's `decide`, which must not satisfy branch protection or
   auto-merge while the axes judge. It then
   writes the **status card** — one PR comment, found by
   `<!-- cursor-approve-card -->` and edited in place — with every axis
   pending. When cursor-review reports `approve_gate == 'capped'` it writes
   "Round limit reached, needs a human" instead. (cursor-review's decide has
   already written the card for this round; see
   [the status card contract](#the-status-card-contract).)
3. Each axis runs only when `approve_gate == 'pass'`, after the start phase: a
   read-only checkout of the PR head, one `cursor-agent` call over
   `.github/cursor-approve/`'s prompt, and one `axis-<name>` artifact holding
   `{"verdict", "confidence", "summary", "commit_sha"}`. A failed axis uploads
   nothing, and an axis refuses to run on a fork PR.
4. `cursor-approve` with `phase: decide` runs `aggregate.py decide` (any
   missing/malformed axis, one not stamped with `commit_sha`, any red, or more
   yellow than `max_yellow_axes` → no approval), reading each axis only from its
   own `axis-<name>` artifact, then `auto-approve.py approve-external`: it
   re-reads the head and base and withholds when either moved since the axes
   ran, never approves a PR labelled `needs-human-review` or
   `skip-cursor-review` (both read live from the PR at decide time, so a veto
   applied while the axes run still stops the approval — a caller's
   label-keyed concurrency group cannot cancel that in-flight decide),
   records the reviewed SHA in the review body, and withdraws this identity's
   own earlier approvals whenever it does not approve. An approval is one line
   linking the card. The card is rewritten with the verdicts, the result and
   the rule — or "Superseded by a newer commit" when the head moved.

Every model-supplied string on the card goes through post-review.py's
`neutralize_mentions` plus markdown/HTML escaping, so a summary cannot add a
heading, fire a mention or forge the card marker.

## The status card contract

The card is written on **every completed round** for a PR auto-approve applies
to (`approve_max_severity` set and the author passes `approve_authors`),
whatever the outcome, and edited in place each time, so it always shows the
latest round and the commit it reviewed. An author `approve_authors` does not
list gets no card.

cursor-review's own `decide` writes it first, every round. That job is the one
that has the round's gating findings, their threads and `decide_gate`'s
reasons in hand, and it runs on every outcome, while this workflow's phases run
only when the caller's `if:` lets a `pass` (or `capped`) through. Callers need
no new wiring for the not-approved outcomes. On a `pass` this workflow's start
phase then overwrites the card with the axes table, and its decide phase with
the verdicts. Both workflows write as the `APPROVER_TOKEN` identity, which is
how they find the same comment; pass the same `APPROVER_TOKEN` to both.

The three lines at the top of the card are a **stable contract** for agents.
Read them from the start of the comment body. A released value is not renamed (the one exception, `dismiss_then_relabel`, was withdrawn before any caller pinned it), and `next` is an open set — see **Forward compatibility** below:

```text
<!-- cursor-approve-card -->
<!-- cursor-approve-state: pass|changes_requested|no_decision|capped -->
<!-- cursor-approve-next: none|resolve_then_relabel|relabel|push_then_relabel|human -->
```

| State | Written by | Card says | `next` | Next-step line |
|---|---|---|---|---|
| `pass` | cursor-review decide, then this workflow's start phase | Approved (or passed the severity gate, axes pending), then the axes table | `none` | none |
| `changes_requested` | cursor-review decide | "Not approved: N finding(s) above `<threshold>` gate this round", one line per gating finding (severity, `file:line`, thread link, why it gated: inside this round's changes, a live re-raise, High/Critical anywhere, or `full` scope), or the earlier open threads that held it | `resolve_then_relabel` | Fix or reply to the gating threads, resolve them, then start a new round: remove and re-add the `cursor-review` label. |
| `no_decision` | cursor-review decide | "No decision this round", with `decide_gate`'s reasons verbatim (e.g. "2/6 panel reviewers did not complete") | `relabel` when the head moved (the one transient cause a relabel alone clears); `push_then_relabel` for a transient cause that left the head where it was (a reviewer or the judge errored, the PR could not be read, the base alone was retargeted); `human` for a structural one (the findings did not land as threads, the reviewed diff is empty) | Re-run the round: remove and re-add the `cursor-review` label. / Nothing — the round is being re-run automatically (see below). / Push a commit so the head moves, then re-run the round. / A human is needed: <cause>. |
| `capped` | cursor-review decide, or this workflow's start phase | Needs a human (`needs-human-review`, or the round limit) | `human` | A human is needed: the bot withdraws its own request for changes on this hand-off, so a human's review decides this PR — if one still shows, its withdrawal failed (see the run log) and it needs dismissing by hand. Removing the `needs-human-review` label resets the round count. |

The decide phase maps its outcome onto the same contract: `approved` →
`pass`/`none`; `not_approved` (an axis was red, missing, or too many yellow) →
`changes_requested`/`relabel`; `superseded` (the head moved) → `no_decision`/`relabel`;
`retargeted` (the base alone changed) and `error` → `no_decision`/`push_then_relabel`;
`needs_human` → `capped`/`human`; `vetoed` (`skip-cursor-review`) and `own_pr` →
`no_decision`/`human`. Every card also shows the reviewed commit SHA.

### Why a relabel alone is not always enough (BE-19527)

The gate's `dup` step skips any head that already carries a bot-posted
consolidated review — and a *degraded* round posts one too. So for a round the
head never moved under (a panel cell or the judge errored, the PR could not be
read, the base alone was retargeted), removing and re-adding the label starts a
run that no-ops, and the PR stays exactly where it was.

Those rounds report `push_then_relabel`, not `relabel`, and name the remedy that
actually works: **move the head** — an empty commit is enough — then re-run. The
distinction is on the contract marker, not only in the prose, because an agent
following `relabel` there would start nothing too.

It is deliberately not "dismiss the review first", which the `dup` step's
`state != "DISMISSED"` clause appears to offer: `post-review.py` submits the
consolidated review with `"event": "COMMENT"`, and GitHub dismisses only
`APPROVED` / `CHANGES_REQUESTED` reviews (422 otherwise, and the UI shows no
Dismiss control), so that clause is unreachable for the one review it is matched
against. The first cut of this change advised exactly that and was wrong.

**Forward compatibility.** `cursor-approve-next` is an open set, not the closed
four it began as — `push_then_relabel` is the second value added. Treat an
unrecognised value as `human`: it always means the round did not decide, and
handing it to a person is never the harmful answer. Do not switch exhaustively
and fall through to "do nothing", which strands exactly the degraded rounds that
need action.

### The round re-runs itself when the head outran it (BE-19526)

A panel takes tens of minutes. A push that lands while it runs leaves the round
judging a commit the PR has moved past, so `decide_gate` withholds with "the PR
head moved while the review ran" — a whole panel spent, and until now a human
had to notice the card and re-apply the label by hand.

The workflow now does it itself: it removes and re-adds the review label, **once
per PR**, and the card says so instead of asking you to. The next step stays
`relabel` on the contract marker, so an agent reading it waits for the new round
either way. decide only decides; the relabel is the run's LAST job
(`Re-run the round (head moved)`), after the Blocking gate, Panel integrity and
completion jobs, because the label events it fires cancel whatever of the run is
still going in the caller's `cancel-in-progress` group.

It fires only when a fresh round would actually decide differently — the head
moved (a base retarget alongside it rides along). Every other transient cause
leaves the head where it was, and the gate's same-SHA `dup` check would skip the
re-run, so those still ask for a human. The budget is counted off the PR's own
reviews — each retry's standing block carries `<!-- cursor-review-auto-retry -->`
and still counts once dismissed — so it holds across rounds and across runs; a
retry whose block did not post is never fired, since nothing would count it. An
unreadable review list or approver login, a round already at `max_rounds` (the
re-run would cap out), or a caller with neither `APPROVER_TOKEN` nor
`bot_app_id` (a `GITHUB_TOKEN`-applied label fires no run) all fall back to the
hand-recovery the card describes. A relabel that fails (or a push that cancels
the run first) turns that job red or never runs it; the card's "if no new round
starts, re-run it by hand" covers both.

Whichever token the caller configures needs **label write** on the repo.

A `no_decision` round (and a `changes_requested` one held only by an earlier
round's open thread) also leaves a **standing REQUEST_CHANGES** as the approver
identity, with the reasons and the next step, so resolving every thread does
not make an unapproved PR look done. The next no-decision round replaces it;
the next approval withdraws it — cursor-review's own, or, under
`defer_approval`, its passing decide and this workflow's decide phase. A
`capped` round posts none and withdraws any earlier one.

This workflow's **decide phase leaves the same block when it withholds** —
`not_approved`, `error`, `superseded` and `retargeted` — with the card's
reasons and next step and the reviewed-SHA marker, posted before the older
blocks are dismissed so they never stack. Under `defer_approval` the passing
cursor-review round has already dismissed every earlier block, so without this
a red axis would leave the PR with no approval and nothing blocking it.
`own_pr` posts none, nor does an approval. A `superseded` (or `retargeted`) decide
that finishes after a newer run approved the live head posts none either: as
the approver's latest review it would override that approval.

**A hand-off withdraws the block instead.** Once no round will approve the PR,
no approving round would ever withdraw a block either, and it would hold the
merge until someone dismissed it by hand. So the bot's own marked, unedited
request for changes is withdrawn — a human's, or one someone edited, never is —
when:

- the PR carries `needs-human-review` (the round limit, or a human): by
  cursor-review's `capped` decide, by this workflow's start phase on a `capped`
  gate (the round-cap path, where no decide runs) and its `needs_human`
  outcome, and by cursor-review's **Dismiss stale auto-approval** job on the
  next PR event;
- the PR carries `skip-cursor-review`: by this workflow's `vetoed` outcome and
  by that job;
- the caller clears cursor-review's `approve_max_severity`: by that job, on the
  next PR event, once none of the bot's approvals still stands.

The label is re-read after the reviews are listed, so a run that finishes late
never withdraws a block a newer round posted after the label came off. A
withdrawal that fails turns its step red; the card is still written.

The card then names what clears the PR: a human's review. Removing
`needs-human-review` only resets the round count for the next round.

The next-step line names cursor-review's `review_label` input (default
`cursor-review`) on the cards cursor-review writes; a value that is not a plain
label name is not echoed, and the line says "the review label" instead. This
workflow has no such input, so its own start and decide cards always say
`cursor-review`.

## Prerequisites

| Requirement | Why |
|---|---|
| `secrets.CURSOR_API_KEY` | The axes bill through it. The only secret an axis receives. |
| `secrets.APPROVER_TOKEN` | Token of the approving identity (needs `pull-requests: write` on the repo, and to be allowed to dismiss reviews if branch protection restricts that). Empty → both phases warn and do nothing; there is deliberately no fallback to an App or `GITHUB_TOKEN`. |
| cursor-review with `approve_max_severity` set | Without it `approve_gate` is `off` and no axis runs. |
| cursor-review with `defer_approval: true` | Without it cursor-review approves on severity alone before any axis has judged the PR (see step 1). |

## Caller

Pin every `uses:` and `workflows_ref` to the same full commit SHA. Keep the
per-PR `concurrency:` group cursor-review's caller already carries: it cancels an
older run when a newer one starts, so two runs never write the card or decide
on the same PR at once.

```yaml
concurrency:
  group: cursor-review-pr-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  cursor-review:
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@<sha>  # v1
    with:
      workflows_ref: <sha>
      approve_max_severity: low
      defer_approval: true  # only cursor-approve's decide approves
    secrets: inherit

  approve-start:
    needs: cursor-review
    # `!cancelled()`, not a bare condition: cursor-review's called job goes red
    # on any errored panel cell, which would otherwise skip this job. See
    # "A red panel cell must not skip the axes" below.
    if: >-
      !cancelled()
      && (needs.cursor-review.outputs.approve_gate == 'pass'
      || needs.cursor-review.outputs.approve_gate == 'capped')
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-approve.yml@<sha>  # v1
    with:
      workflows_ref: <sha>
      phase: start
      commit_sha: ${{ github.event.pull_request.head.sha }}
      axes: correctness,conformance
      round: ${{ needs.cursor-review.outputs.round }}
      max_rounds: ${{ needs.cursor-review.outputs.max_rounds }}
      approve_gate: ${{ needs.cursor-review.outputs.approve_gate }}
    secrets:
      APPROVER_TOKEN: ${{ secrets.APPROVER_TOKEN }}

  axis-correctness:
    needs: [cursor-review, approve-start]
    if: >-
      !cancelled()
      && needs.approve-start.result == 'success'
      && needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-correctness.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}

  axis-conformance:
    needs: [cursor-review, approve-start]
    if: >-
      !cancelled()
      && needs.approve-start.result == 'success'
      && needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-conformance.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}

  cursor-approve:
    needs: [cursor-review, approve-start, axis-correctness, axis-conformance]
    if: always() && needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-approve.yml@<sha>  # v1
    with:
      workflows_ref: <sha>
      phase: decide
      commit_sha: ${{ github.event.pull_request.head.sha }}
      axes: correctness,conformance
      max_yellow_axes: 0
      round: ${{ needs.cursor-review.outputs.round }}
      max_rounds: ${{ needs.cursor-review.outputs.max_rounds }}
      approve_max_severity: low
      poster_login: github-actions[bot]  # `<app-slug>[bot]` if cursor-review runs with bot_app_id
      approve_scope: ${{ needs.cursor-review.outputs.approve_scope_effective }}
    secrets:
      APPROVER_TOKEN: ${{ secrets.APPROVER_TOKEN }}
```

With `defer_approval: true`, thread auto-resolution moves here too: this
phase's decide resolves cursor-review's at-or-below-threshold threads only when
`approve_max_severity` and `poster_login` are both set, and `poster_login` must
be the login cursor-review posts findings under — `<app-slug>[bot]` when
cursor-review runs with `bot_app_id`, else `github-actions[bot]`. A wrong login
resolves nothing, so a "require conversation resolution" ruleset still blocks.
Pass `approve_scope` from cursor-review's `approve_scope_effective` output, not
a literal: under cursor-review's default `approve_scope: delta`, a round marks
findings outside its changes as non-gating, and only when that round really
gated under `delta` may those marked threads stop blocking the resolution of
the others. Omitted, it is `full` and any open above-threshold thread — marked
or not — leaves every thread open for a human.

Pass secrets explicitly to the axes — never `secrets: inherit` — so each axis
receives `CURSOR_API_KEY` and nothing else. `always()` on the decide job makes a
failed axis read as "no result" (withheld) instead of skipping the decision and
leaving an earlier approval standing. The axes `needs: approve-start` so they
never start while cursor-review's approval still stands; a start phase that
fails to withdraw it skips them, which decide reads as "no result".

### A red panel cell must not skip the axes

`!cancelled()` on the start phase and the axes is load-bearing. cursor-review's
called job goes **red whenever a panel cell errors** — the leg fails by design
and `Panel integrity` fails alongside it — and `approve_max_failed_reviewers`
does not change that. That input governs the `approve_gate` verdict, not the
job's conclusion, so a tolerated errored cell still leaves the called job in
`failure`.

Without `!cancelled()`, GitHub's default `needs:` rule skips the start phase on
that red job; the axes skip with it; and decide, which runs on `always()`,
reports every axis as ⚠️ no result and withholds. A caller that sets
`approve_max_failed_reviewers` above 0 and omits `!cancelled()` therefore never
gets the tolerance it configured — one errored cell out of six withholds every
round, and nothing on the card says why.

`approve_gate` stays the only authority on whether a round may be decided, and
reading it off a red cursor-review job cannot approve a round the gate itself
rejected: it is already `untrusted` when the judge was degraded, when more cells
errored than the caller tolerates, when the review did not land as threads or
when the head moved, and it falls back to `untrusted` whenever an upstream
decision job did not succeed — which is every Panel integrity failure that is
not simply a short panel. The axes keep an explicit
`needs.approve-start.result == 'success'` rather than a blanket `!cancelled()`
so the invariant above is unchanged: a start phase that failed to withdraw
cursor-review's standing approval still skips them.

What the guard does give up is an *incidental* backstop. The gate counts each
cell by the `status` that cell's own findings artifact reports, not by its
leg's conclusion — that is how a tolerated errored cell still passes — and
cursor-review documents that status as an availability signal, not an
attestation: a prompt-injected cell can write a clean `ok` record, and because
artifact names are run-global it can claim another cell's name first, so that
cell's real upload fails and its leg goes red while consolidate reads the
forgery. A bare `needs:` used to skip the axes on that red leg. With
`!cancelled()` the round goes on to the axes, which still review the change
independently, and decide still withholds on any red axis (or more yellow
than `max_yellow_axes`). The gate now reads that red leg too: when a reviewer
leg did not succeed although every counted cell artifact reads `ok`, the panel
is marked inconsistent and `approve_gate` is `untrusted` — not re-run
automatically, since the forged artifact would survive the retry — and Panel
integrity fails naming it. The residual is a forger that also errors its own
cell under `approve_max_failed_reviewers` > 0, so the honest red leg is
explained by a tolerated error: there the count stays tamper-evident only in
the rollup.

## Inputs

`cursor-approve.yml`:

| Input | Default | Meaning |
|---|---|---|
| `workflows_ref` | — (required) | Same commit SHA as the `uses:` pin; checked against `job.workflow_sha`. |
| `phase` | — (required) | `start` (card with pending axes) or `decide` (aggregate, approve, final card). |
| `commit_sha` | — (required) | The PR head the axes judged. |
| `axes` | — (required) | Comma-separated axes the caller ran, e.g. `correctness,conformance`. |
| `max_yellow_axes` | `0` | Yellow axes tolerated (0–3, strictly below the number of axes). |
| `round` | `''` | cursor-review's `round` output, for the card heading. |
| `max_rounds` | `''` | cursor-review's `max_rounds` output, for the card heading. |
| `approve_gate` | `''` | cursor-review's `approve_gate` output; `capped` makes the start card say a human is needed. |
| `approve_max_severity` | `''` | Decide phase: the same threshold cursor-review runs with. With `poster_login`, an approval that stands auto-resolves cursor-review's own at-or-below-threshold threads exactly as cursor-review's own approval does. Empty → no thread is resolved. |
| `poster_login` | `''` | Decide phase: the login cursor-review posts findings under — `<app-slug>[bot]` when it runs with `bot_app_id`, else `github-actions[bot]`. Empty → no thread is resolved. |
| `approve_scope` | `full` | Decide phase: cursor-review's `approve_scope_effective` output. `delta` → an open above-threshold thread that round marked non-gating (*Outside this round's changes*) does not block resolving the at-or-below-threshold threads, exactly as cursor-review's own decide treats it; the marked thread itself is never resolved. `full` (or empty) → any open above-threshold thread, marked or not, blocks all resolution. Any other value warns and resolves as `full`. |

## Axis inputs

Every axis wrapper — `axis-correctness.yml`, `axis-conformance.yml`,
`axis-business.yml`, `axis-design.yml`, `axis-completeness.yml` — takes the same
three inputs. Their secrets differ; see [Context axes](#context-axes) for the
last three. (Without a checkout, `commit_sha` is the head whose merge-base
diff the workflow fetches from the GitHub API.)

| Input | Default | Meaning |
|---|---|---|
| `commit_sha` | — (required) | The PR head to judge; checked out read-only with full history. |
| `model` | `claude-opus-5-5-xhigh` | Cursor model id (cursor-review's judge model). |
| `runs_on` | `"ubuntu-latest"` | JSON-encoded `runs-on`, as in cursor-review. |

## Base inputs

`cursor-axis-base.yml` is the shared body the axis wrappers call; consumers do
not call it directly. It loads its prompts from this repo at `job.workflow_sha`.

| Input | Default | Meaning |
|---|---|---|
| `axis` | — (required) | Which axis prompt to render. |
| `commit_sha` | — (required) | As above. |
| `model` | `claude-opus-5-5-xhigh` | As above. |
| `runs_on` | `"ubuntu-latest"` | As above. |
| `checkout` | `false` | Full read-only checkout of the PR head (`persist-credentials: false`). |
| `context_sources` | `''` | Comma list of `linear`, `notion`, `slack`: the context-proxy tools the agent gets. Business, design and completeness only; private repos only. |
| `no_shell` | `false` | Deny cursor-agent's shell, file-write and web-fetch tools; the job fails if the transcript shows a shell or file-write call, or no recognizable tool call at all. Required, with `checkout: false`, for business and design. |

Every axis also uploads `transcript-axis-<axis>` — the agent's stream-json
transcript, plus the proxy's call log for the context axes. On a context axis
each tool call in it is cut to the tool's name: no arguments and no results, so
no Linear, Notion or Slack content. Only the verdict
JSON (`verdict`, `confidence`, `summary` capped at 1200 characters) reaches
`cursor-approve.yml`; its `axis-*` download never matches a transcript.

## Context axes

`axis-business.yml`, `axis-design.yml` and `axis-completeness.yml` judge a PR
against company context, not just its code. They read Linear and Notion
through `.github/cursor-approve/context-proxy.py`, a read-only MCP server that
holds the tokens: the agent can search, but never holds a credential.

No axis reads Slack. Slack is where people debate before they decide, not a
record of what was decided: the decision lands in a ticket, a PRD or a TDD.
Treating a thread as evidence that a change was wanted rewards opening a thread
to get a PR approved, and anyone in a channel can post the text an approver
then reads. The proxy still implements `slack_search` / `slack_history`, but
`cursor-axis-base.yml` rejects `slack` in `context_sources`, so no caller can
enable them. `axis-business.yml` still declares an optional `SLACK_TOKEN` that it
never forwards, so a caller that passes one keeps working; drop the line when
convenient.

| Axis | Agent sees | Context tools | Secrets (all but `CURSOR_API_KEY` optional) |
|---|---|---|---|
| business | PR title, body, changed-file list with line counts — no checkout, no shell | `linear_search`, `linear_get_issue`, `notion_search`, `notion_get_page` | `CURSOR_API_KEY`, `LINEAR_KEY`, `NOTION_TOKEN` |
| design | PR title, body, merge-base diff cut at 200 KB — no checkout, no shell | `linear_search`, `linear_get_issue`, `notion_search`, `notion_get_page` | `CURSOR_API_KEY`, `LINEAR_KEY`, `NOTION_TOKEN` |
| completeness | Full read-only checkout (`persist-credentials: false`), with a shell | `linear_search`, `linear_get_issue` | `CURSOR_API_KEY`, `LINEAR_KEY` |

A missing token leaves that source unconfigured; the axis still runs. The tokens
are read-only bot identities (Linear and Notion as the tools bot).

How a token reaches the proxy without reaching the agent:

1. One step, the only one with the token secrets in its env, writes the enabled
   tokens to a `0600` file and passes on nothing but its path.
2. The axis step starts `context-proxy.py` FIRST, on two FIFOs and without
   `CURSOR_API_KEY`; the proxy reads the token file and deletes it, and the
   agent is not started until the file is gone. Tokens are never environment
   variables of the proxy or the agent.
3. cursor-agent's only MCP server is `mcp-relay.py`, which copies bytes between
   the agent and the already-running proxy. The axis step's env holds none of
   `LINEAR_KEY`, `NOTION_TOKEN`, `SLACK_TOKEN` (a test enforces this).

Each axis runs only on a same-repo PR in a **private** repository. On a fork PR
or a public repository the wrapper skips its axis with a notice; the base fails
closed on a public repo too. A skipped axis uploads no verdict, so leave it out
of `axes:` on a repo where it cannot run, or decide reads it as "no result".

Business and design fetch the change from the GitHub compare API, which
`contents: read` covers; no extra permission is needed.

```yaml
  axis-business:
    needs: [cursor-review, approve-start]
    if: >-
      !cancelled()
      && needs.approve-start.result == 'success'
      && needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-business.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
      LINEAR_KEY: ${{ secrets.LINEAR_KEY }}
      NOTION_TOKEN: ${{ secrets.NOTION_TOKEN }}

  axis-design:
    needs: [cursor-review, approve-start]
    if: >-
      !cancelled()
      && needs.approve-start.result == 'success'
      && needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-design.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
      LINEAR_KEY: ${{ secrets.LINEAR_KEY }}
      NOTION_TOKEN: ${{ secrets.NOTION_TOKEN }}

  axis-completeness:
    needs: [cursor-review, approve-start]
    if: >-
      !cancelled()
      && needs.approve-start.result == 'success'
      && needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-completeness.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
      LINEAR_KEY: ${{ secrets.LINEAR_KEY }}
```

**Accepted risk.** These axes read author-written text (the PR, and whatever
anyone wrote in an issue, page or channel) while holding read-only company
context. A prompt injection in any of it can make the agent repeat that context
into its verdict summary, which lands on the PR, or steer its verdict. That is
acceptable on private repositories only, where everyone who can read the PR can
already read the company, and is why the axes refuse to run on a public one.
The checkout axes (correctness, conformance, completeness) have a shell, granted
by an explicit `Shell(*)` allow rule: `--print` has no one to approve a command,
so without that rule every shell call is rejected and the axis reviews without
ever running `git`. The job fails if every shell call in a checkout axis's
transcript was rejected, and on any axis whose transcript shows no tool call.
That shell is the same unsandboxed exposure over PR code that the cursor-review
panel already accepts. For completeness it also means: on a hosted runner
(passwordless `sudo`) a hostile agent could read the running proxy's memory, so
its Linear token is protected by policy and prompt, not by the sandbox.

## Trust model

Every verdict is model output over the PR's own content, so a diff that
prompt-injects every axis can steer a round to an approval. Treat the approval
as an automated review signal, not a substitute for a human reviewer.
