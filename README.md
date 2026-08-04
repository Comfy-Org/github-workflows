# Comfy-Org reusable GitHub Actions workflows

Shared, versioned [reusable
workflows](https://docs.github.com/en/actions/using-workflows/reusing-workflows)
for use across Comfy-Org repositories.

This repo is **public**, so any repo — public or private, inside or outside the
org — can call these workflows with no extra GitHub Actions settings.

**→ [Setup guides for every workflow](docs/callers/)** — each gives you a
complete, copy-pasteable caller.

## Workflows

| Workflow | Purpose | Setup |
|---|---|---|
| [`detect-unreviewed-merge.yml`](.github/workflows/detect-unreviewed-merge.yml) | SOC 2 compliance — detects PRs merged without prior approval and opens a tracking issue in [`Comfy-Org/unreviewed-merges`](https://github.com/Comfy-Org/unreviewed-merges). | [detect-unreviewed-merge.md](docs/callers/detect-unreviewed-merge.md) |
| [`cursor-review.yml`](.github/workflows/cursor-review.yml) | Label-triggered multi-model code review. A 4-lab × 2-review-type cursor-agent panel runs adversarial + edge-case passes, a judge model consolidates them into one PR review with per-finding severity badges, and the triggerer gets Slack start/complete DMs. Advisory by default; opt in with `blocking: true` to fail a (required-status-check) gate while findings stay unresolved. Prompts and scripts live in [`.github/cursor-review/`](.github/cursor-review) — the single source of truth, so consumer repos carry only a thin caller. Self-hostable via `runs_on` (JSON, default `ubuntu-latest`) and panel models overridable via `models` (JSON array) for accounts lacking a default provider. Requires `CURSOR_API_KEY` (+ optional `SLACK_BOT_TOKEN`). | [cursor-review.md](docs/callers/cursor-review.md) |
| [`cursor-review-auto-label.yml`](.github/workflows/cursor-review-auto-label.yml) | Companion to `cursor-review.yml`. On PR assignment, applies the review label for an opted-in reviewer (via the CLOUD_CODE_BOT app token, so the label actually triggers the review). The opt-in roster lives in the caller's `vars.CURSOR_REVIEW_OPTED_IN_LOGINS` — no roster is baked into the workflow. Requires `vars.APP_ID` + `CLOUD_CODE_BOT_PRIVATE_KEY`. | [cursor-review-auto-label.md](docs/callers/cursor-review-auto-label.md) |
| [`assign-reviewers.yml`](.github/workflows/assign-reviewers.yml) | Auto-requests expertise-aware, load-balanced PR reviewers with new-folk randomization. Matches changed paths against a caller-repo `.github/reviewers.yml` (path-glob → reviewers, plus a `default_pool`), drops the author + `vars.REVIEWER_EXCLUDE`, ranks candidates by open review load (steering off anyone at/over `vars.REVIEWER_LOAD_CAP`), and may swap a slot for a `vars.REVIEWER_GROWTH_POOL` member. Requests go through the CLOUD_CODE_BOT app token so they work on fork PRs. Requires `vars.APP_ID` + `CLOUD_CODE_BOT_PRIVATE_KEY`. | [assign-reviewers.md](docs/callers/assign-reviewers.md) |
| [`assign-prs-to-author.yml`](.github/workflows/assign-prs-to-author.yml) | Housekeeping — assigns every open PR with no assignees to its author (bot-authored PRs skipped by default). Run on a schedule from a thin caller; useful when a team tracks PR ownership via assignees. The calling job needs `pull-requests: write` and `issues: write`. | [assign-prs-to-author.md](docs/callers/assign-prs-to-author.md) |
| [`pr-size.yml`](.github/workflows/pr-size.yml) | PR-size cap — fails (or, in `mode: warn`, only reports) when a PR's net diff exceeds `max_lines` non-generated changed lines, keeping diffs reviewable. Excludes dependency lockfiles, `linguist-generated` files (read from the base ref, so a PR can't exempt itself), Go generated-code markers, and per-repo `extra_lockfiles` / `extra_generated_globs`. A `bypass_label` (default `oversized-ok`) waves through a legitimately large change; a sticky bot comment explains overages when `bot_app_id` + `BOT_APP_PRIVATE_KEY` are supplied (degrades to status + step summary without them). Counting logic + tests live in [`scripts/check-pr-size/`](scripts/check-pr-size). | [pr-size.md](docs/callers/pr-size.md) |
| [`pr-risk.yml`](.github/workflows/pr-risk.yml) | **Advisory PR risk grading (shadow check)** — **automatic grading off by default** (`enabled: false`; a manual `workflow_dispatch` grades regardless, so a repo can trial it before switching on); switch it on with `enabled: true` or by setting the caller repo's `RISK_CONFIG` variable to `{"enabled": true}`, which outranks the input in both directions so `{"enabled": false}` is a no-PR kill switch. Grades every PR into a tier `R0` (safest) .. `R3` (riskiest) and syncs one label (`risk:R0`..`risk:R3`, or `risk:ungraded` when an input was unreadable). The label is the entire product: nothing is gated, routed, commented, or merged. Deterministic (`gh` + `jq`, no LLM): `grade = worst(path_floor, provenance, reversibility)` — path-glob map, what-process-produced-the-diff (registered runbooks with identity + diff-shape assertions; forks are R3 with no exceptions), and revertability (persistent-state mutation, deletions under sensitive classes, did green checks cover the lines). Grader + generic defaults live in [`scripts/pr-risk/`](scripts/pr-risk); a consumer sharpens them with `.github/risk.json` / `.github/risk-runbooks.json`, read from the PR's **base ref** so a PR can't edit the rules that judge it. The job excludes its own run from the check rollup and waits (`wait_for_checks_minutes`) for the rest to settle before labeling. Labels ride the plain `GITHUB_TOKEN` (cannot fire `labeled` triggers — no cascade risk); disagreement is recorded with a human-owned `risk-dispute` label. Label text is remappable via `label_map`. `workflows_ref` is **required** and its format is **ENFORCED** — pin it to the same full commit SHA as `uses:`, so the grader cannot be loaded from a floating ref after the caller was reviewed; every job that checks it out fails the run *before* the tool checkout unless it is a full 40-hex commit SHA, so a branch, a tag, or a `refs/pull/N/head` is rejected rather than trusted. Enroll it as its own workflow rather than a job inside an existing CI workflow (the rollup exclusion is per-run). The calling job needs `contents: read` + `issues: write` + `pull-requests: write` + `checks: read` + `actions: read` + `statuses: read`; GitHub rejects a shorter grant at startup (a reusable workflow can only narrow the caller's token, never elevate it), so a caller enrolled from an older copy of this row fails before any step runs. Both writes are the ONE label: repo-side label creation on first use maps to `issues`, and labeling a PR maps to `pull-requests` (the labels endpoint is dual-mapped by what the "issue" is, so `issues: write` alone 403s on a PR). `actions: read` is for the rollup's `CheckRun -> checkSuite -> workflowRun` self-exclusion hop. No secrets. | [pr-risk.md](docs/callers/pr-risk.md) |
| [`stale.yml`](.github/workflows/stale.yml) | Stale-PR sweeper (`actions/stale`) plus a Slack digest of what it touched. PRs inactive for N days are labeled `stale`; still-inactive PRs are closed. The digest header names the source repo so batches from different repos posted to the same channel are unambiguous. Thresholds, messages, exempt labels, and the Slack channel are inputs; the caller owns the schedule + dry-run toggle. The calling job needs `pull-requests: write` and `issues: write`. Optional `SLACK_BOT_TOKEN`. | [stale.md](docs/callers/stale.md) |
| [`groom.yml`](.github/workflows/groom.yml) | Scheduled/dispatch org-wide **code-cleanup sweep** (finds only — no commits, no PRs, never merges). A read-only FINDER agent scans a clean default-branch checkout (whole-repo, not a diff) for high-value refactors; an INDEPENDENT VERIFIER agent (fresh session) re-checks each as CONFIRM/DOWNGRADE/REJECT with a stable dedup signature; survivors are deduped against a durable GitHub-issue-state ledger and filed as `groom`-labeled GitHub issues (security-adjacent ones get `groom-security` — investigate, don't auto-implement). Mirrors the cursor-review topology: briefs + ledger live in [`.github/groom/`](.github/groom) as the single source of truth. The finder/verifier/builder agent jobs invoke the Claude CLI directly and mint no GitHub token, so they need nothing beyond `contents: read`; filing runs in a separate job as the bot you configure via `bot_app_id` (Comfy: cloud-code-bot). `dry_run` reports what it would file without opening issues. Runs on a **daily base cron** with a runtime cadence gate: set repo Actions variable `GROOM_INTERVAL_DAYS` (default 7 = weekly) to retune how often a real run happens — weekly → every-3-days → daily — with no workflow-file edit; a tick within the interval no-ops before the finder (`workflow_dispatch` bypasses the interval gate, but the volume gate — when the caller leaves it on — still applies). The calling job must grant `contents: read` + `issues: write` + `pull-requests: read` + `actions: read` — the first three are declared by the `file` / `build_select` jobs (needed even with `bot_app_id` set), and the interval gate needs `actions: read` (reads run history for the last real run); GitHub rejects a shorter grant at startup. Requires `ANTHROPIC_API_KEY` (+ `BOT_APP_PRIVATE_KEY` when `bot_app_id` is set). **Opt-in auto-builder** (`builder: true`, BE-4003): the top `max_prs` (default 5) CONFIRMED, non-security findings become **review-gated PRs** (full CI + cursor-review, **never auto-merged**) instead of issues; a credential-free `build` job emits only a patch artifact and a separate `build_pr` job opens the PR as the bot, preserving the security boundary. The ledger's PR-state (open/merged/closed) stops a built finding being re-proposed. Requires `bot_app_id`. `max_prs` is typed **`string`**, not `number`, so a caller can forward its own `workflow_dispatch` input straight through (`max_prs: ${{ github.event.inputs.max_prs \|\| '1' }}`) and let an operator raise the ceiling for one manual run — no `fromJSON()` cast in the caller, and the parse/clamp (empty → default, non-numeric → 0 PRs + warning, never a failed run) happens once inside the reusable. | [groom.md](docs/callers/groom.md) |
| [`agents-md-integrity.yml`](.github/workflows/agents-md-integrity.yml) | Enforces the Comfy `AGENTS.md` standard on the caller repo: a top-level `AGENTS.md` must exist and stay under a hard line ceiling (`max_lines`, default 200; warns over `warn_lines`, default 150), a `CLAUDE.md` (if present) must be a thin `@AGENTS.md` shim rather than a divergent copy, no legacy `.cursorrules` (gated `forbid_cursorrules`), every nested monorepo `AGENTS.md` needs a sibling `@AGENTS.md` shim and to be under the ceiling (gated `check_nested`), and `AGENTS.md` should have a CODEOWNERS DRI (`require_codeowners`, warn-only by default). `exclude_paths` (newline-/comma-separated globs, default empty) carves payload subtrees — a repo whose product IS agent instructions, e.g. a plugin marketplace shipping `plugins/**/AGENTS.md` + a real `CLAUDE.md` — out of the nested scan without the all-or-nothing `check_nested: false`; exclusions are applied during the walk (never scanned or line-counted), reported in the log as `EXCLUDED: <path> (matched <glob>)`, and a glob that would exclude the ROOT `AGENTS.md`/`CLAUDE.md` — or the whole tree without saying so (`/`, `*`, `*/**`) — is rejected (exit 2). Fails with a non-zero exit + GitHub annotations so it wires in as a required status check. The checker lives in [`.github/agents-md-integrity/`](.github/agents-md-integrity) (pin `workflows_ref` to the same ref as `uses:`); no secrets required. | [agents-md-integrity.md](docs/callers/agents-md-integrity.md) |

Per-workflow inputs and secrets are also documented in each workflow file's
header comment; the setup guides above are the maintained, copy-pasteable version.

## Quick start

A caller is a **complete workflow file** in your repo at
`.github/workflows/<name>.yml`. It needs its own `on:` trigger — the reusable
workflow does not supply one — and its calling job must grant the permissions the
reusable's nested jobs declare:

```yaml
name: PR Size Cap

on:
  pull_request:
    types: [opened, synchronize, reopened, labeled, unlabeled]

jobs:
  size:
    permissions:
      contents: read
    uses: Comfy-Org/github-workflows/.github/workflows/pr-size.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
      max_lines: 1000
```

Get the current SHA:

```bash
gh api repos/Comfy-Org/github-workflows/commits/main --jq .sha
```

Then follow the [setup guide](docs/callers/) for whichever workflow you are
adopting — the exact permission grant, required secrets, and per-workflow
footguns are there.

> **Permissions fail at startup.** A nested job cannot request more
> `GITHUB_TOKEN` scope than the calling job grants, and GitHub validates that
> *before any job runs* — regardless of `if:` guards, and regardless of a GitHub
> App token doing the real writes. Get it wrong and you get an opaque "workflow
> file issue" with **zero jobs and no logs**, not a failing step. Grant exactly
> what the setup guide lists.

## Pinning

Pin `uses:` to a **full commit SHA**, with the version as a trailing comment:

```yaml
uses: Comfy-Org/github-workflows/.github/workflows/pr-size.yml@8c4ff3e… # v1
```

The SHA-pin format satisfies pin-validation tooling (`pinact`, `zizmor`) and gives
auditors immutable supply-chain evidence. Dependabot/Renovate can auto-bump it.

A bare `@v1` tag is technically allowed but **will fail** in repos that run pin
validation in CI — and because `v1` is force-pushed for compatible changes, it
silently changes what you execute.

**Keep `workflows_ref` equal to your `uses:` SHA.** `groom`, `cursor-review`,
`pr-size`, and `agents-md-integrity` load assets — agent briefs, review prompts,
checker scripts — from `workflows_ref` at run time. It defaults to `main`, so a
SHA-pinned caller that leaves it unset runs an old workflow against today's
assets.

## Staying current

Enrolling a repo is **two steps**:

1. Merge the caller workflow into your repo.
2. Add the repo to the matching roster variable here — `GROOM_CALLERS`,
   `CURSOR_REVIEW_CALLERS`, `PR_SIZE_CALLERS`, `AGENTS_MD_CALLERS`, or
   `ASSIGN_REVIEWERS_CALLERS`.

The `bump-*-callers.yml` workflows read those rosters to open pin-bump PRs when a
reusable moves. **A repo absent from the roster keeps its original SHA forever**,
drifts behind, and eventually breaks when the two stop being compatible. Details
in [docs/callers/README.md](docs/callers/README.md#staying-current).

## Versioning

Major-version tags (`v1`, `v2`, …).

- **Breaking changes** bump the major; callers opt in. Adding a required input,
  adding a required secret, or making a nested job request a new permission are
  all breaking — even though nothing in the caller's YAML changed.
- **Backwards-compatible changes** move the existing major tag in place
  (`git tag -f v1 <sha> && git push -f origin v1`). Callers pinned to the tag pick
  it up on their next run; SHA-pinned callers opt in by bumping.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — how to add a new reusable workflow, which
tests to run, and why every change here is live-fire.

Security issues: **do not open a public issue.** See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).
