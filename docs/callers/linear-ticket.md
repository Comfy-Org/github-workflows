# `linear-ticket.yml` — require a linked Linear issue on every PR

Read [the shared caller contract](README.md) first.

## What it does

Gates a PR on a Linear issue that **Linear has linked to that exact PR** — not merely a
`TEAM-123`-shaped string in the branch, title, or body. The only thing that turns the check
green is an attachment Linear returns for the PR's canonical `html_url` (`attachmentsForURL`)
whose issue satisfies the configured team/state policy. Any supported linking method works:
identifier in the branch name, PR title, a `Closes`/`Fixes`/`Resolves` line in the body, or
a manual link pasted into the issue in Linear.

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
    secrets:
      LINEAR_API_TOKEN: ${{ secrets.LINEAR_API_TOKEN }}
```

Do **not** use `secrets: inherit` — pass only `LINEAR_API_TOKEN`.

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
| `enforce` | `true` | `false` is warn-only: every outcome publishes success, but the summary and marker comment show the verdict enforce mode would have produced. |

## Rollout (warn-only first)

1. Land both caller files with `enforce: false`. Observe for ~a week: attachment latency, URL
   matching, retry counts, team/state outcomes, fork/Dependabot behaviour, and rate-limit
   headroom (the validator logs `X-RateLimit-*` headers).
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
5. Flip `enforce: true`. Leave the check non-required for a short window.
6. Add the **`linear-ticket`** status context to your `main` ruleset as a required check.

Nothing here changes branch protection automatically — the last step is a manual repo-admin action.

## Gotchas

**The required check is a commit *status*, not the job.** Require the `linear-ticket` context in
your ruleset; do not require the `Linear Ticket / validate` job — it runs on the default branch
against the workflow_run event and is not tied to the PR's merge check.

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
