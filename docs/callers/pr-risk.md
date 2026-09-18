# `pr-risk.yml` — PR risk grade (shadow check)

Read [the shared caller contract](README.md) first.

## What it does

Grades every PR into a tier `R0` (safest) .. `R3` (riskiest) and syncs **one**
visible label (`risk:R0`..`risk:R3`, or `risk:ungraded` when an input was
unreadable). A human can override it with `risk-dispute:low` through
`risk-dispute:xhigh`; the computed tier remains in the grade record.

Deterministic, no LLM: `grade = worst(path_floor, provenance, reversibility)` —
a path-glob map, what process produced the diff (registered runbooks, forks
always `R3`), and revertability (persistent-state mutation, sensitive
deletions, whether green checks actually covered the changed lines). The
grader and its generic defaults live in
[`scripts/pr-risk/`](../../scripts/pr-risk) and load from **this repo** at the
pinned `workflows_ref` — never from the graded PR's checkout, so a PR cannot
edit the rules that judge it. A consumer sharpens the generic defaults with
`.github/risk.json` / `.github/risk-runbooks.json`, read from the PR's **base
ref** for the same reason.

## Prerequisites

None. No secrets — the only credential used is the automatic `GITHUB_TOKEN`.

## Caller

`.github/workflows/ci-pr-risk.yml`:

```yaml
name: CI - PR Risk Grade

on:
  # Public repo taking fork PRs? Use `pull_request_target:` here instead —
  # a fork run under plain `pull_request` cannot write the label. See the
  # fork gotcha below before you swap it.
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review, labeled, unlabeled]

concurrency:
  group: pr-risk-${{ github.event.pull_request.number }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  pr-risk:
    if: >-
      github.event_name != 'pull_request' ||
      ((github.event.action != 'labeled' && github.event.action != 'unlabeled') ||
       startsWith(github.event.label.name, 'risk-dispute:'))
    permissions:
      contents: read
      issues: write          # create the risk:* labels repo-side on first use
      pull-requests: write   # the label write itself — labeling a PR rides the
                             # pull-requests permission, not issues (the labels
                             # endpoint is dual-mapped by what the "issue" is)
      checks: write          # REQUIRED WHETHER OR NOT you set `check_run: true` —
                             # see "Grant the whole union" below
      actions: read          # the rollup's CheckRun -> checkSuite -> workflowRun hop
      statuses: read
    uses: Comfy-Org/github-workflows/.github/workflows/pr-risk.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
```

Enrolling is **two steps** — merging the caller above is only the first. Ask a
maintainer to add this repo to the `PR_RISK_CALLERS` roster secret
(see [Staying current](README.md#staying-current)); until they do, the
[`bump-pr-risk-callers.yml`](../../.github/workflows/bump-pr-risk-callers.yml)
fleet does not know the caller exists, so both of its pins sit frozen and the
caller silently drifts behind the grader it runs. Skipping this half is the most
repeated mistake in this repo.

One thing to know about that second step:

- **Enrolment does not backfill your pin.** The fleet only runs on a push to
  `main` touching `pr-risk.yml` or `scripts/pr-risk/**`, so a repo added to the
  roster after the fact stays on whatever SHA it merged with until the grader
  next changes. Ask the maintainer to `workflow_dispatch`
  `bump-pr-risk-callers.yml` once after adding you — every bump entrypoint
  carries `workflow_dispatch` for exactly this.

Enrolling a private repo used to publish its name in this public repo's run log:
the roster was an Actions **variable** bound through `env:`, and Actions dumps
the step env before `bump-callers.sh` can mask it. BE-6472 moved every roster to
a **secret**, which the runner masks in that dump too, so that caveat no longer
applies to new enrolments — but a name already printed in an old public log
cannot be unpublished.

## Required permissions

```yaml
contents: read
issues: write
pull-requests: write
checks: write
actions: read
statuses: read
```

**Grant the whole union, including `checks: write`.** A reusable workflow can
only narrow the caller's token, never elevate it, so GitHub validates *every*
nested job's declared `permissions:` against this block at **startup** — before
any job is scheduled. The `publish-check` job declares `checks: write`, and a
job-level `if:` is a runtime condition, so leaving `check_run` at its default
`false` does not exempt you: a short grant fails the whole run with an opaque
"workflow file issue" and no job-level detail. Grading itself only needs
`checks: read` (the rollup the reversibility axis reads); the write is the
grant, not the behaviour. **Moving an existing pin onto a commit that has this
job? Add `checks: write` to the caller in the same PR** — a pin bump alone will
fail the caller's next run at startup.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. No default on purpose: a floating default let a caller SHA-pin `uses:` and still load the grader from HEAD of main. Checked before the tool checkout on two axes: it must be a full 40-hex lowercase SHA, **and** that commit must be an ancestor of `main` of this repo. So a branch, a tag, a `refs/pull/N/head` and any **not-yet-merged** SHA all fail the run — **merge the change here first, then bump the pin.** There is no opt-out. |
| `enabled` | `false` | **The master switch, and grading is OFF until you set it.** Enrolling and switching on are two decisions, so you can land the caller, get it reviewed, and start grading later. While false a `pull_request` run grades nothing and touches no existing `risk:*` label. A manual `workflow_dispatch` grades regardless — that is what makes "try it here before turning it on" possible. `vars.RISK_CONFIG` on the calling repo overrides this in both directions, so `{"enabled": false}` is a kill switch that needs no PR. |
| `pr_number` | `''` | Grade **one** PR by number instead of the event's. Leave it empty on a `pull_request` run — with no number supplied, the target, the base ref and every emitted label are exactly what they were before this input existed. Supplying it is what makes the backfill and the manual re-grade below possible, and bot-authored and fork PRs **are** graded on that path. Typed `string`, because `workflow_dispatch` inputs arrive as strings. |
| `pr_numbers` | `''` | Grade **several** PRs by number, comma-separated (`12,15,20`). Takes precedence over `pr_number` when both are set. Targets are graded one at a time and one unreadable PR is reported without abandoning the rest. Pair a long list with a low `wait_for_checks_minutes` — the per-target waits are additive, and the run stops starting new targets once the job's budget is spent, reporting the un-attempted ones by number. There is deliberately no `all_open: true`. |
| `fleet_logins` | `mattmillerai` | Logins whose PRs grade provenance `agent-supervised` alongside `agent-coded`. Both are read for **human** authors only: an author GitHub types as a `Bot` is a runbook candidate regardless, so listing a bot here (or labelling its PR) buys it nothing — only a registry entry that asserts can promote it. |
| `bot_logins` | `github-actions,dependabot,renovate,coderabbitai,cursor,comfy-pr-bot,web-flow` | Extra logins treated as bots. Needed only for **machine USER accounts** — a real GitHub App is recognized from GitHub's own actor type, no list entry required. A bot with no runbook entry still grades as human — identity alone buys no trust. **This list is load-bearing, not a hint:** a listed login skips the first-time-contributor test, so it moves a non-fork `NONE`/`FIRST_TIME_CONTRIBUTOR` PR from `external` (R3) to `human` (R1). Nothing validates that a listed login is really a machine account, so add one only for an account you control, and remove it when it is retired. |
| `label_map` | `''` | Rename the five grader-owned labels as `tier=label` pairs. Tier keys are fixed; only the label text is yours. |
| `wait_for_checks_minutes` | `10` | How long to wait for the rest of the check rollup to settle before labeling (clamped to 25 — what a 30-minute job can spend waiting). `0` labels immediately, expect R2 floors from still-pending checks. |
| `repo_map_path` | `.github/risk.json` | Consumer risk-map override, read from the PR **base ref**. Repo-relative — a leading `/` or a `..` segment is refused, not resolved. |
| `repo_runbooks_path` | `.github/risk-runbooks.json` | Consumer runbook-registry override, read from the PR **base ref**. |
| `sticky_comment` | `false` | Publish ONE sticky PR comment — a single visible line (the tier, the axis that decided it and that axis's reason), with the formula, the per-axis reasons, the map versions and the caveats folded into a collapsed `<details>` and a "this grade is wrong" checkbox below it. Created once and updated in place, so N pushes leave one comment. **Defaults to false on purpose:** a bot comment on every PR is a visible behaviour change, so it is a per-consumer decision, and a caller that does not opt in behaves byte-identically to before this input existed. Ticking the checkbox applies `risk-grade-disputed` on the next grade — a machine-maintained mirror, distinct from the human-owned `risk-dispute:*` overrides — and needs no permission beyond the `pull-requests: write` already granted. |
| `check_run` | `false` | Publish a Check Run on the PR's head commit carrying the tier and the reason: the immutable, timestamped, commit-attached record a mutable label cannot be. Its conclusion is hardcoded `neutral`, so it can never fail a PR even if a repo later marks it required. **Leaving it false does not excuse the `checks: write` grant** — see "Grant the whole union" above; the grant is validated at startup, the input only decides whether the job runs. |
| `check_name` | `PR risk (advisory)` | Name of the published Check Run. Only used when `check_run` is true. |

## Gotchas

**Fork PRs need `pull_request_target`, not `pull_request`.** A fork PR under a
plain `pull_request` trigger gets a read-only token and the label write fails.
This is safe to do here by construction — the workflow never checks out or
executes PR code. That property is the entire safety argument, so keep the
caller a bare `uses:` job: `pull_request_target` runs privileged, and adding
your own steps (or jobs) that check out or run PR head code under it reopens
the classic pwn-request hole. Same-repo-only callers (private org repos that
take no fork PRs) should stay on plain `pull_request`.

The caller's `if:` limits label events to `risk-dispute:*`; other labels do not
need a re-grade. A public repo staying on plain `pull_request` also needs the
fork guard from [`pr-risk.yml`'s header](../../.github/workflows/pr-risk.yml).
Keep it behind the event test so `workflow_dispatch` still works.

## Risk overrides

`risk-dispute:low`, `risk-dispute:medium`, `risk-dispute:high`, and
`risk-dispute:xhigh` override the visible risk label. For example,
`risk-dispute:medium` changes a computed `risk:high` label to `risk:medium`.
Removing the dispute restores the computed label. If multiple override labels
are present, the highest risk wins.

**Bot PRs are graded — do not add a `github.actor != 'dependabot[bot]'` skip.**
Dependabot's `pull_request` runs do start from a read-only `GITHUB_TOKEN`, but
the caller's `permissions:` block elevates it — that is GitHub's documented
behaviour ([Changing `GITHUB_TOKEN`
permissions](https://docs.github.com/en/code-security/dependabot/troubleshooting-dependabot/troubleshooting-dependabot-on-github-actions#changing-github_token-permissions))
— and this workflow declares no secrets, so the label write succeeds. A
dependabot skip therefore buys almost nothing and leaves most repos'
highest-volume automated PR producer ungraded. (Almost: the clause did skip
Dependabot's own github-actions PRs bumping *this* caller's pins, which now run
the newly pinned reusable inside their own run under the elevated token. The
exposure is narrow — Dependabot only proposes SHAs behind this repo's published
tags, and the tool checkout is separately gated on `workflows_ref` being an
ancestor of `main` — and the fleet's own bump PRs always behaved this way, since
the clause keyed on `github.actor`, the run's **trigger**, not the PR's author. Do
review such a PR before merging: Dependabot rewrites `uses:` and leaves
`workflows_ref` stale, so fix the input in the same PR.) The reasoning is
specific to *this* workflow: a caller that needs an **Actions secret** (an app
private key, an API token —
[`cursor-review-auto-label.yml`](cursor-review-auto-label.md) is the org's
example) still needs the skip, because a Dependabot-triggered run sees Dependabot
secrets only and no `permissions:` block can change that. Fork PRs are the
opposite case: the `permissions:` elevation does not extend to them, which is why
the fork gotcha above is still a real token guard.

**Already carrying that skip? Removing it is yours to do, not the fleet's** —
[`bump-pr-risk-callers.yml`](../../.github/workflows/bump-pr-risk-callers.yml)
moves SHA pins only and will never edit your `if:`, so drop the clause in your
own repo, and update in the same PR anything repo-side that mirrors the caller's
`if:`/`concurrency` shape (a unit test asserting on the caller's YAML is the
usual one).

**Enroll it as its own workflow, not a job inside an existing CI workflow.**
The grading job excludes its own *run* from the check rollup it reads; a job
sharing a run with the rest of CI excludes its siblings too and lands on the
honest R2 floor instead of grading off the full rollup.

**Pair the caller with a per-PR `cancel-in-progress` concurrency group** (shown
above). The reversibility axis waits for other checks to settle, so a stale run
from an earlier push should not stack behind a newer one.

**The label is applied with the plain `GITHUB_TOKEN` on purpose** — a
`GITHUB_TOKEN`-applied label cannot fire `labeled` triggers, so this shadow
check is structurally unable to start a workflow cascade. Do not "upgrade" the
label write to an App token; that would reopen the cascade risk this design
avoids.
