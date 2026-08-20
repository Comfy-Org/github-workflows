# `linear-ticket.yml` — require a linked Linear issue for protected branches

Read [the shared caller contract](README.md) first.

## What it does

For a PR targeting a **protected base branch**, gates on a Linear issue that **Linear has linked
to that exact PR** — not merely a `TEAM-123`-shaped string in the branch, title, or body. The
validator reads GitHub's `protected` property for the current base branch, so repositories can
have any number of protected branches without duplicating a branch list in the caller. A PR
targeting an unprotected branch skips without querying Linear or publishing a `linear-ticket`
status.

For a protected branch, the only thing that turns the check green is an attachment Linear
returns for the PR's canonical `html_url` (`attachmentsForURL`) whose issue satisfies the
configured team/state policy. Any supported linking method works: identifier in the branch
name, PR title, a `Closes`/`Fixes`/`Resolves` line in the body, or a manual link pasted into the
issue in Linear.

Identifiers extracted from PR text are used **only** to explain a red check ("you referenced
`BE-1234` but Linear has not linked it") — they can never make it green. Automated checks buy
existence + linkage + traceability; **relevance is still a code-review job**.

### Why two workflows

The validator needs the Linear secret and a write token, which GitHub withholds from fork and
Dependabot PR runs. The fix is the standard **`pull_request` → `workflow_run`** split:

- an unprivileged **signal** workflow runs on the PR (`contents: read`, no secret, checks out
  nothing), and
- a privileged **validate** workflow runs on `workflow_run: completed` of the signal workflow
  and calls this reusable, which GitHub grants the secret and a write token even for a fork PR.

The validate job runs on your **default branch**, so its job-level check is *not* the thing you
require in branch protection. This reusable publishes a commit status named **`linear-ticket`**
on the PR head SHA; you require **that context** once warn-only observation is done.

## Prerequisites

| | |
|---|---|
| `secrets.LINEAR_API_TOKEN` | **Required.** The value placed verbatim into Linear's `Authorization` header — a personal API key **raw**, or an OAuth actor-auth access token prefixed with `Bearer` followed by one space. Missing/invalid fails the check *closed* as an infrastructure error (never green). |
| A Linear→GitHub integration | The workspace's GitHub integration must be connected so Linear creates the PR attachments this check reads. |

## Caller — two files

### 1. `.github/workflows/linear-ticket-signal.yml` (unprivileged, no-op)

```yaml
name: Linear Ticket Signal      # this exact name is referenced by the validate workflow

on:
  pull_request:
    types: [opened, edited, reopened, ready_for_review, synchronize, labeled, unlabeled]

permissions:
  contents: read

jobs:
  signal:
    runs-on: ubuntu-latest
    steps:
      - name: Signal
        run: echo "Signalling the Linear ticket validator (no PR content is checked out)."
```

The `labeled`/`unlabeled` types are required so adding or removing the exemption label reruns
the check. This workflow checks out nothing, runs no PR content, and emits no artifact — that
is what makes the `workflow_run` privilege boundary safe.

### 2. `.github/workflows/linear-ticket-validate.yml` (privileged, calls this reusable)

```yaml
name: Linear Ticket

on:
  workflow_run:
    workflows: ["Linear Ticket Signal"]   # must match the signal workflow's name exactly
    types: [completed]

jobs:
  validate:
    permissions:
      actions: read           # read the triggering workflow_run
      contents: read          # checkout the validator scripts; PR code is never checked out
      pull-requests: write    # the one marker diagnostic comment
      statuses: write         # publish the `linear-ticket` commit status
    uses: Comfy-Org/github-workflows/.github/workflows/linear-ticket.yml@<full-commit-sha>  # v1
    with:
      workflows_ref: <same-full-commit-sha>
      # team-keys: BE,ENG            # optional; LEAVE EMPTY (default) unless you truly want
      #                              # team scoping — a restricted list fails cross-team PRs
      # exempt-label: linear-exempt  # optional; empty disables exemption
      # exempt-actors: dependabot[bot],renovate[bot]  # optional; bot PRs skip the check
      # require-open-issue: true     # default
      enforce: false                 # WARN-ONLY during rollout; flip to true when ready
      # soft-fail: true              # default; warn-only still shows a RED (non-blocking)
      #                              # check. Set false for a silent always-green pilot.
    secrets:
      LINEAR_API_TOKEN: ${{ secrets.LINEAR_API_TOKEN }}
```

Do **not** use `secrets: inherit` — pass only `LINEAR_API_TOKEN`.

Then ask a maintainer to add your repo to the `LINEAR_TICKET_CALLERS` roster secret on
`Comfy-Org/github-workflows` — that roster is what keeps your pin current
(see [Staying current](README.md#staying-current)). Enrolment does not backfill your pin:
ask them to `workflow_dispatch` `bump-linear-ticket-callers.yml` once after adding you.

## Required permissions

```yaml
actions: read
contents: read
pull-requests: write
statuses: write
```

**Grant the whole union.** A reusable workflow can only narrow the caller's token, and GitHub
validates the nested job's declared `permissions:` against this block at **startup** — before
any `if:` runs. A short grant fails the whole run with an opaque "workflow file issue" and no
job-level detail.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. Validated to be a full 40-hex lowercase SHA **and an ancestor of this repo's main** before checkout — a branch, tag, short SHA, or fork-authored SHA fails the run. |
| `team-keys` | `''` | Comma-separated allow-list matched against the resolved issue's API `team.key`, never an identifier prefix. **Leave empty (recommended)** unless a repo genuinely wants team scoping — a restricted list fails a cross-team PR on first contact. Malformed or duplicate entries fail the run. |
| `exempt-label` | `''` | Single label that waives the requirement (recommended `linear-exempt`). Empty disables exemption. |
| `exempt-actors` | `''` | Comma-separated PR-author logins whose PRs skip the check without a label (e.g. `dependabot[bot],renovate[bot]`) — the non-manual hatch for bot PRs that never carry a ticket. Opt-in; empty means no bypass. Listing an actor means **all** of its PRs merge without a Linear ticket, so keep it to trusted automation accounts. |
| `require-open-issue` | `true` | Reject a linked issue whose Linear `state.type` is `completed`/`canceled`. `backlog`/`unstarted`/`started`/`triage` pass. |
| `enforce` | `true` | `false` is warn-only: a failing **verdict** never exits the job nonzero. `soft-fail` decides how loudly it is reported; the diagnosis is identical either way. Warn-only is not a promise the job always exits `0` — a broken *run* still does (a failed terminal status write, a missing token/repo, malformed `team-keys`, a non-`pull_request` trigger). |
| `soft-fail` | `true` | Warn-only only (ignored when `enforce: true`). `true` publishes a **red `failure`** status, so the PR's check list shows the check failing — loud, but non-blocking for as long as `linear-ticket` is not a required status in your ruleset. `false` restores the silent variant (warn-only publishes `success`; only the summary and comment carry the verdict). **Do not** require the `linear-ticket` context while `enforce: false` — required + soft-fail would block merges. |

## Upgrading an existing `enforce: false` caller

`soft-fail` is new, and it defaults to `true`. **If your caller already runs `enforce: false`,
taking a SHA bump past the commit that added this input changes what your PRs show:** warn-only
used to publish a green `linear-ticket` status in every outcome, and now publishes a **red**
one. Nothing about *blocking* changes — a status only blocks when your ruleset requires that
context — but the PR check list goes from green to red, and anything you have keyed off the
status (auto-merge bots, dashboards, other automation) sees `failure` where it saw `success`.

Two things to do before you take the bump:

1. **Check your ruleset.** If `linear-ticket` is already a required check while you are still on
   `enforce: false`, the red status *will* block merges. That combination was always a
   misconfiguration (rollout step 6 is the last step for a reason), but soft-fail is what makes
   it bite. Remove the required check, or flip `enforce: true` — do not sit in between.
2. **Decide which pilot you want.** Loud (the default) is recommended: authors fix links during
   the pilot instead of discovering the requirement on flip day. If you want the old behaviour,
   add one line to your caller:

   ```yaml
   enforce: false
   soft-fail: false   # keep the pre-soft-fail behaviour: warn-only stays green
   ```

An **absent** `SOFT_FAIL` — which only happens if your `uses:` pin and `workflows_ref` have
skewed apart, so an older `linear-ticket.yml` is running newer scripts — is treated as
`false`, so pin skew alone can never flip you to red. Keep both pins on the same SHA anyway.

## Rollout (warn-only first)

1. Land both caller files with `enforce: false`, and **leave `linear-ticket` out of your
   ruleset's required checks** — that omission, not the input, is what keeps the pilot
   non-blocking. Observe for ~a week: attachment latency, URL matching, retry counts,
   team/state outcomes, fork/Dependabot behaviour, and rate-limit headroom (the validator logs
   `X-RateLimit-*` headers).

   With the default `soft-fail: true`, a warn-only failure shows on the PR as a **red
   `linear-ticket` check** plus the marker comment, which is what makes authors notice and fix
   links during the pilot instead of discovering the requirement on flip day. Nobody is
   blocked: a non-required failing check leaves the merge button live. If you want the quieter
   first pass, set `soft-fail: false` and the status stays green.
2. Confirm the `linear-ticket` status publishes on **same-repo, fork, and Dependabot** PR head
   SHAs, and that automatic (branch/title/body) and manual links both resolve to the same
   canonical URL.
3. **Track the stale-link case.** Linking an issue *in Linear* after the check went red produces
   no GitHub event, so the status stays red until someone re-runs the workflow or edits the PR
   (the failure comment says this). Count how often that happens during the warn-only week. If
   it's rare, the re-run instruction is enough; if it's frequent, the fix is a
   `repository_dispatch` driven by a Linear webhook — not a larger retry budget — and is a
   deliberate v2 follow-up, out of scope here.
4. Decide bot-PR handling: set `exempt-actors` (e.g. `dependabot[bot],renovate[bot]`) so
   dependency PRs pass without hand-labelling each one.
5. Flip `enforce: true`. Leave the check non-required for a short window — the PR-visible
   result is unchanged from the soft-fail pilot; what changes is that the validator's own run
   now goes red too, so a systemic breakage is visible in the Actions tab.
6. Add the **`linear-ticket`** status context as a required check in every ruleset that protects
   a branch where tickets are required. Only now does anything block. The validator discovers
   every protected base branch dynamically; no branch list belongs in the workflow caller.

Nothing here changes branch protection automatically — the last step is a manual repo-admin action.

## Gotchas

**The required check is a commit *status*, not the job.** Require the `linear-ticket` context in
your ruleset; do not require the `Linear Ticket / validate` job — it runs on the default branch
against the workflow_run event and is not tied to the PR's merge check.

**Protection is evaluated for the PR's current base branch.** GitHub's branch response reports
protection from branch protection rules or rulesets. Retargeting a PR triggers a fresh run; an
unprotected target gets no `linear-ticket` status and no Linear API query. The status must be
absent rather than successful because GitHub scopes commit statuses to a SHA, not a PR: success
from an unprotected PR could otherwise overwrite failure on a protected PR sharing that commit.

**A red check is not the same as a blocked merge.** What blocks is your ruleset requiring the
`linear-ticket` context — nothing this workflow does. That is why warn-only can (and by default
does) publish a red status: it is a real, visible signal that costs nobody a merge. The
corollary is the footgun: do not add the context to your ruleset until you flip `enforce: true`,
or the warn-only pilot starts blocking.

**A manual link created after a red run does not emit a GitHub event.** After linking in Linear,
either re-run the failed validate workflow or edit the PR title/body to trigger a fresh
`edited` event. The failure comment says exactly this.

**Snapshot, not continuous.** A green check means the issue was linked and met policy *when that
commit's check ran*. Moving the issue to Done later does not retroactively fail an existing
check; the next PR event re-evaluates against current Linear state.

**Fails closed on infrastructure errors.** An auth/schema/timeout/rate-limit-exhaustion error is
reported as an infrastructure failure (red in enforce mode), not as an invalid ticket — a broken
credential must not silently disable the org-wide control. Availability is handled by bounded
retry, per-branch concurrency, the warn-only rollout, and the auditable exemption label.

**Store the token in the right form.** Personal API key → raw. OAuth actor-auth token → prefixed
with `Bearer` followed by one space. The value is dropped straight into the `Authorization` header.
