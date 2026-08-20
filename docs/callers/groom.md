# `groom.yml` — scheduled code-cleanup sweep

Read [the shared caller contract](README.md) first — pinning, permissions, and the
roster step apply here and are not repeated below.

## What it does

A **read-only FINDER** agent scans a clean default-branch checkout — the whole
repo, not a diff — for high-value refactors. An **independent VERIFIER** agent in
a fresh session re-checks each finding as CONFIRM / DOWNGRADE / REJECT and
attaches a stable dedup signature. Survivors are deduped against a durable
GitHub-issue-state ledger and filed as `groom`-labeled issues. Security-adjacent
findings get `groom-security` instead — investigate, don't auto-implement.

Default mode is **finds-only: no commits, no PRs, and it never merges.**

The model steps hold **no write credentials.** The `audit` jobs are
`contents: read`; filing happens in a separate job as a GitHub App you nominate.
Preserve that split — see [SECURITY.md](../../SECURITY.md).

## Prerequisites

| | |
|---|---|
| `secrets.ANTHROPIC_API_KEY` | **Required.** The finder and verifier bill through it. Available org-wide in Comfy-Org. |
| `vars.APP_ID` + `secrets.CLOUD_CODE_BOT_PRIVATE_KEY` | Optional. Files issues as cloud-code-bot instead of `github-actions[bot]`, so groom's output is a distinct, queryable actor. |

## Minimal caller — finds-only

Put this at `.github/workflows/groom.yml` in your repo:

```yaml
name: Groom

on:
  schedule:
    # Frequent BASE cron (daily). Effective cadence is the runtime `interval_days`
    # gate below, not this cron — GitHub Actions cron is static in the file, so a
    # daily tick + a runtime gate is how you get a tunable cadence with no
    # workflow-file edit. Pick a NON-round minute and stagger it against other
    # repos — top-of-hour is the most congested slot on GitHub's scheduler.
    - cron: '17 9 * * *'
  workflow_dispatch:          # bypasses the interval gate (not the volume gate)
    inputs:
      dry_run:
        description: Run the full audit but do NOT open issues — print what it would file.
        type: boolean
        default: false

# NOTE: deliberately no caller-level `concurrency:` — see "Footguns" below.

jobs:
  groom:
    permissions:
      contents: read
      issues: write
      pull-requests: read
      actions: read   # the interval gate reads run history for the last real run
    uses: Comfy-Org/github-workflows/.github/workflows/groom.yml@<full-commit-sha>
    with:
      # Optional since BE-8077 — omit it and the briefs + ledger auto-load from
      # the commit the uses: pin above resolved to. Kept explicit here because
      # `bump-callers.sh` moves this line and the uses: pin in ONE pass, so a
      # roster-enrolled caller costs nothing to double-pin. See its row.
      workflows_ref: <same-full-commit-sha>   # keep byte-identical to the uses: SHA
      bot_app_id: ${{ vars.APP_ID }}
      # Cadence knob + the matching volume-gate window (BE-4004). Wire both to
      # one repo Actions variable so they can't drift: retune weekly ->
      # every-3-days -> daily by editing `GROOM_INTERVAL_DAYS`, no workflow edit.
      interval_days: ${{ vars.GROOM_INTERVAL_DAYS || '7' }}
      cadence: ${{ vars.GROOM_INTERVAL_DAYS || '7' }}
      # `github.event.inputs` is null on a schedule event, so '' != 'true' -> false.
      # Scheduled runs always file live; only a manual dispatch can dry-run.
      dry_run: ${{ github.event.inputs.dry_run == 'true' }}
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      BOT_APP_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}
```

Then ask a maintainer to add your repo to the `GROOM_CALLERS` roster secret — see
[Staying current](README.md#staying-current).

## Required permissions

```yaml
contents: read
issues: write        # the `file` job
pull-requests: read  # the `build_select` job
actions: read        # the interval gate (BE-4004) — reads this workflow's run
                      # history to find the last real groom run
```

Grant all four **even when `bot_app_id` is set** and the App token does the
actual writing. GitHub validates the grant at startup against what the nested
jobs *declare*, not against what they end up using. A short grant produces a
zero-job `startup_failure` with no logs — omitting `actions: read` specifically
rejects the run with "requesting 'actions: read', but is only allowed
'actions: none'" rather than degrading to a fail-open daily run.

## Inputs

All are optional to GitHub's startup validator, and unlike the other workflows
here `workflows_ref` is optional to groom's own guards too — leaving it unset is
safe, and it is the documented default. See its row for what it falls back to.
The ones that matter:

| Input | Default | Why you'd change it |
|---|---|---|
| `interval_days` | `7` | Effective run cadence (BE-4004): on the daily base cron, a tick within this many days of the last REAL run early-exits before the finder. Wire it to a `GROOM_INTERVAL_DAYS` repo Actions variable so cadence is a variable edit, not a workflow-file change. `workflow_dispatch` bypasses this gate (not `volume_gate`). `0` disables the throttle. |
| `cadence` | `7` | Volume-gate window in days — set to match `interval_days` (wire both to the same variable) so the merge-activity check lines up with how often a real run can happen. Feeds the volume gate below. |
| `volume_gate` | `true` | Skips the (expensive) audit when nothing merged in `cadence` days. **This is the cost control — leave it on** for scheduled runs. |
| `dry_run` | `false` | Full audit + dedup, files nothing, prints what it would file. Use before the first live run. |
| `max_findings` | `12` | Cap issues per run. Lower it on a first pilot. |
| `model` | `claude-opus-5` | The finder/verifier model. |
| `themes` | `duplication, inconsistent patterns, missing abstractions, complexity hotspots, dead code` | Steer the finder at particular kinds of cleanup. The default mirrors the finder brief's own five dimensions, so it is a no-op; **narrow** it (e.g. `duplication, dead code`) to focus a repo. Security/auth-adjacent findings are filed regardless of theme. |
| `scope_label` / `scope_desc` | `whole-repo` | Cosmetic labels for the scope in issue bodies. |
| `workflows_ref` | `''` | **Leaving it unset is safe.** Alone among these workflows groom does not *require* it — it defaults to `''` and each asset checkout falls back to `${{ job.workflow_sha }}`, the commit your `uses:` pin resolved to, so the briefs, `ledger.py` and `interval.py` always match the logic running them with nothing to keep in sync. Set it only to test briefs from a branch. Before BE-8077 that fallback was spelled `github.job_workflow_sha` and silently loaded the assets from this repo's default branch — see the footgun below. |
| `bot_app_id` | `''` | File as your App rather than `github-actions[bot]`. |
| `builder` | `false` | Opt into PR-writing — see below. |
| `max_prs` | `'5'` | Only with `builder: true`. Typed **string**, deliberately. |
| `pr_size_limit` | `400` | Only with `builder: true`. Caps a built PR's diff. |

## Opt-in auto-builder

With `builder: true`, the top `max_prs` CONFIRMED, non-security findings become
**review-gated PRs** — full CI plus cursor-review, **never auto-merged** —
instead of issues. Security findings still file as `groom-security` issues; the
builder skips them.

The security boundary holds: a credential-free `build` job emits only a patch
artifact, and a separate `build_pr` job opens the PR as the bot. Requires
`bot_app_id`. The ledger tracks PR state (open/merged/closed) so a built finding
is not re-proposed.

`max_prs` is a **string, not a number**, so a caller can forward its own
`workflow_dispatch` input straight through without a `fromJSON()` cast, and an
operator can raise the ceiling for one manual run:

```yaml
    inputs:
      max_prs:
        description: How many PRs this run may open.
        type: string
        default: '1'
# ...
    with:
      builder: true
      max_prs: ${{ github.event.inputs.max_prs || '1' }}
```

Parsing and clamping happen once inside the reusable: empty → default,
non-numeric → 0 PRs plus a warning. Never a failed run.

Start at `max_prs: 1`. The current large-repo builder pilot runs at exactly that.

## Footguns

**Never declare `concurrency: groom-…` in your caller.** `groom.yml` already
declares `concurrency: groom-${{ github.repository }}` with
`cancel-in-progress: false` — the TOCTOU guard for its read-then-file ledger.
Duplicating that group **deadlocks the run**: your caller holds the group while
its `uses:` job waits for the same group, and neither yields until timeout.

**`workflows_ref` may be left unset — but check which SHA you pinned from.**
The finder and verifier briefs, `ledger.py` and `interval.py` are checked out at
run time from `${{ inputs.workflows_ref || job.workflow_sha }}`: omitting the
input falls back to the commit your `uses:` pin resolved to, leaving nothing to
keep in sync (BE-4169's intent, delivered in BE-8077). Two things to know. First,
if you pin `uses:` at a `github-workflows` commit **older than BE-8077**, that
fallback is still spelled `github.job_workflow_sha` there — which is **not** a
property of the `github` context (GitHub documents that value as
`job.workflow_sha`, the spelling `groom.yml` has always used where it reads the
agent CLI pin, calling the `github.` form a trap in the same comment). Actions
expands an unknown context property to `''`, and `actions/checkout` with an empty
`ref` takes the **default branch** — so on those older pins an omitted input runs
mutable assets in the jobs holding your `ANTHROPIC_API_KEY` and App token, and
the explicit pin is what keeps groom pinned. Bump past BE-8077 (or set
`workflows_ref`) to close it. Second, `job.workflow_sha` needs an Actions runner
≥ v2.334.0; every groom job now fails closed with an `::error::` if it resolves
empty, so the failure is loud rather than a silent default-branch checkout.

**Bumping is not usually manual.** `bump-callers.sh` rewrites a caller's
`workflows_ref:` in the same pass as its `uses:` pin, so a caller enrolled in the
`GROOM_CALLERS` roster gets both moved together and never needs a hand-bump — do
not move one alone. The callers carrying a manual burden are the ones *missing*
from that roster, which is a roster bug worth fixing rather than a pin to babysit.
Note that the override moves the briefs and the ledger only: the agent CLI pin is
read from `job.workflow_sha` unconditionally, so pointing `workflows_ref` at a
branch to test briefs does **not** move the pinned Claude Code version with it.

## Before you trust the schedule

```bash
gh workflow run groom.yml --repo <org>/<repo> -f dry_run=true
gh run list --repo <org>/<repo> --workflow groom.yml --limit 1
```

A dry run exercises the full audit and dedup and prints what it *would* file to
the job summary. On a large repo it also proves the finder completes inside the
timeout before you find that out on a Monday morning.

Note that `volume_gate` can gate a dry run down to nothing on a quiet week. If
you want the dry run to always do the full audit, bypass the gate for dispatches
only — live scheduled runs keep it on so a quiescent repo still skips the spend:

```yaml
      volume_gate: ${{ github.event.inputs.dry_run != 'true' }}
```

## Cost

Each ungated run is a whole-repo Opus finder pass plus an independent verifier
pass. `volume_gate: true` means a week with nothing merged costs nothing. That
gate plus a weekly cron is what makes broad enrollment affordable — a repo that
went quiet stops billing on its own.
