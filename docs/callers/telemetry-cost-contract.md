# `telemetry-cost-contract.yml` — require a cost-impact explanation for new billable telemetry

Read [the shared caller contract](README.md) first.

## What it does

For a PR targeting a **protected base branch**, scans every changed file's diff for an
**added** line matching one of this caller's `patterns` regexes (a new Datadog metrics-client
call, a new Sentry capture, a new PostHog `.capture(`, a new RUM custom action — whatever the
repo's `patterns` input names). When a match is found, the PR body must carry a `## Cost
impact` section with real content (priced unit, baseline/scale forecast, protected signal —
the observability program's TDD section 7.1 contract), or the PR must carry the `cost-exempt`
label. A PR with no matching added line passes trivially; a PR targeting an unprotected branch
skips without scanning at all.

Detection reads only the `patch` fragments GitHub's changed-files API returns — never a
checkout of the PR branch — so it is safe to run on fork and Dependabot PRs.

### Why two workflows

Same reason as `linear-ticket.yml`: the validator needs a write token (to publish the commit
status and the marker comment) that GitHub withholds from fork and Dependabot PR runs. The fix
is the standard **`pull_request` → `workflow_run`** split:

- an unprivileged **signal** workflow runs on the PR (`contents: read`, no secret, checks out
  nothing), and
- a privileged **validate** workflow runs on `workflow_run: completed` of the signal workflow
  and calls this reusable, which GitHub grants a write token even for a fork PR.

The validate job runs on your **default branch**, so its job-level check is *not* the thing
you require in branch protection. This reusable publishes a commit status named
**`Telemetry cost contract`** on the PR head SHA; you require **that context** once warn-only
observation is done.

## Prerequisites

None — no secret is required. Detection and the Cost-impact-section check both read only
GitHub-owned PR data (changed-file patches, PR body, PR labels).

## Caller — two files

### 1. `.github/workflows/telemetry-cost-contract-signal.yml` (unprivileged, no-op)

```yaml
name: Telemetry Cost Contract Signal   # this exact name is referenced by the validate workflow

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
        run: echo "Signalling the telemetry-cost-contract validator (no PR content is checked out)."
```

The `labeled`/`unlabeled` types are required so adding or removing `cost-exempt` reruns the
check. This workflow checks out nothing and emits no artifact — that is what makes the
`workflow_run` privilege boundary safe.

### 2. `.github/workflows/telemetry-cost-contract-validate.yml` (privileged, calls this reusable)

```yaml
name: Telemetry Cost Contract

on:
  workflow_run:
    workflows: ["Telemetry Cost Contract Signal"]   # must match the signal workflow's name exactly
    types: [completed]

jobs:
  validate:
    permissions:
      actions: read
      contents: read
      pull-requests: write
      statuses: write
    uses: Comfy-Org/github-workflows/.github/workflows/telemetry-cost-contract.yml@<full-commit-sha>  # v1
    with:
      workflows_ref: <same-full-commit-sha>
      patterns: |
        statsd\.(Gauge|Count|Histogram|Distribution)\(
        sentrygo\.CaptureException\(
      # exempt-label: cost-exempt    # default; empty disables exemption
      # enforce: false               # default; WARN-ONLY during rollout, flip to true when ready
      # soft-fail: true              # default; warn-only still shows a RED (non-blocking) check
```

`patterns` is **newline-separated**, not comma-separated — a regex alternation like the Go
example above routinely contains no comma, but a comma-separated grammar would corrupt a
pattern that did. Blank lines and `#`-comment lines are skipped.

Example `patterns` for a TypeScript/frontend repo:

```yaml
    with:
      patterns: |
        posthog\.capture\(
        Sentry\.(captureException|addBreadcrumb)\(
        useRumAction\(
```

## Required permissions

```yaml
actions: read
contents: read
pull-requests: write
statuses: write
```

**Grant the whole union** — see [the shared caller contract](README.md#permissions) for why a
short grant fails the whole run with no job-level detail.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. Validated to be a full 40-hex lowercase SHA **and an ancestor of this repo's main** before checkout. |
| `patterns` | — (**required**) | Newline-separated regexes (Python `re`). A match against an added diff line marks the PR cost-sensitive. Blank/`#`-comment lines are skipped. At least one must survive and every one must compile, or the run fails as a caller misconfiguration. |
| `exempt-label` | `cost-exempt` | Single label that waives the requirement. Empty disables exemption. |
| `enforce` | `false` | `false` is warn-only: a failing **verdict** never exits the job nonzero. `soft-fail` decides how loudly it is reported. `true` fails closed and exits the job nonzero. |
| `soft-fail` | `true` | Warn-only only. `true` publishes a red `failure` status (loud, non-blocking while the context is not required). `false` restores the silent variant (always-green warn-only). |

## Rollout (warn-only first)

1. Land both caller files with `enforce: false`, and leave `Telemetry cost contract` out of
   your ruleset's required checks. Observe real PRs for about a week: does `patterns` catch
   the call sites you expect, does it false-positive on moved/renamed lines, how often is
   `cost-exempt` actually used.
2. Tune `patterns` from the observed false positives/negatives.
3. Flip `enforce: true`. The check still is not required, so nothing blocks yet — this step
   only makes a systemic breakage visible in the Actions tab.
4. Add the **`Telemetry cost contract`** status context as a required check in the rulesets
   that should enforce it. Only now does anything block.

Nothing here changes branch protection automatically — the last step is a manual repo-admin
action, matching every other reusable in this catalog.

## Gotchas

Same two gotchas as `linear-ticket.yml` (read that page's Gotchas section for the full
rationale): the required check is the commit *status* `Telemetry cost contract`, not the job
`Telemetry Cost Contract / validate` which runs on your default branch; and a red check while
warn-only is a visible, non-blocking signal, not a stuck merge — what blocks is your ruleset
requiring the context.
