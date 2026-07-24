# Comfy-Org reusable GitHub Actions workflows

Shared, versioned [reusable workflows](https://docs.github.com/en/actions/using-workflows/reusing-workflows) for use across Comfy-Org repositories.

This repo is **public** so any repo — public or private, inside or outside the org — can call these workflows with no extra GitHub Actions settings.

## Workflows

| Workflow | Purpose |
|---|---|
| [`detect-unreviewed-merge.yml`](.github/workflows/detect-unreviewed-merge.yml) | SOC 2 compliance — detects PRs merged without prior approval and opens a tracking issue in [`Comfy-Org/unreviewed-merges`](https://github.com/Comfy-Org/unreviewed-merges). |
| [`cursor-review.yml`](.github/workflows/cursor-review.yml) | Label-triggered multi-model code review. A 4-lab × 2-review-type cursor-agent panel runs adversarial + edge-case passes, a judge model consolidates them into one PR review with per-finding severity badges, and the triggerer gets Slack start/complete DMs. Advisory by default; opt in with `blocking: true` to fail a (required-status-check) gate while findings stay unresolved. Prompts and scripts live in [`.github/cursor-review/`](.github/cursor-review) — the single source of truth, so consumer repos carry only a thin caller. Self-hostable via `runs_on` (JSON, default `ubuntu-latest`) and panel models overridable via `models` (JSON array) for accounts lacking a default provider. Requires `CURSOR_API_KEY` (+ optional `SLACK_BOT_TOKEN`). |
| [`cursor-review-auto-label.yml`](.github/workflows/cursor-review-auto-label.yml) | Companion to `cursor-review.yml`. On PR assignment, applies the review label for an opted-in reviewer (via the CLOUD_CODE_BOT app token, so the label actually triggers the review). The opt-in roster lives in the caller's `vars.CURSOR_REVIEW_OPTED_IN_LOGINS` — no roster is baked into the workflow. Requires `vars.APP_ID` + `CLOUD_CODE_BOT_PRIVATE_KEY`. |
| [`assign-reviewers.yml`](.github/workflows/assign-reviewers.yml) | Auto-requests expertise-aware, load-balanced PR reviewers with new-folk randomization. Matches changed paths against a caller-repo `.github/reviewers.yml` (path-glob → reviewers, plus a `default_pool`), drops the author + `vars.REVIEWER_EXCLUDE`, ranks candidates by open review load (steering off anyone at/over `vars.REVIEWER_LOAD_CAP`), and may swap a slot for a `vars.REVIEWER_GROWTH_POOL` member. Requests go through the CLOUD_CODE_BOT app token so they work on fork PRs. Requires `vars.APP_ID` + `CLOUD_CODE_BOT_PRIVATE_KEY`. |
| [`assign-prs-to-author.yml`](.github/workflows/assign-prs-to-author.yml) | Housekeeping — assigns every open PR with no assignees to its author (bot-authored PRs skipped by default). Run on a schedule from a thin caller; useful when a team tracks PR ownership via assignees. The calling job needs `pull-requests: write` and `issues: write`. |
| [`pr-size.yml`](.github/workflows/pr-size.yml) | PR-size cap — fails (or, in `mode: warn`, only reports) when a PR's net diff exceeds `max_lines` non-generated changed lines, keeping diffs reviewable. Excludes dependency lockfiles, `linguist-generated` files (read from the base ref, so a PR can't exempt itself), Go generated-code markers, and per-repo `extra_lockfiles` / `extra_generated_globs`. A `bypass_label` (default `oversized-ok`) waves through a legitimately large change; a sticky bot comment explains overages when `bot_app_id` + `BOT_APP_PRIVATE_KEY` are supplied (degrades to status + step summary without them). Counting logic + tests live in [`scripts/check-pr-size/`](scripts/check-pr-size). |
| [`stale.yml`](.github/workflows/stale.yml) | Stale-PR sweeper (`actions/stale`) plus a Slack digest of what it touched. PRs inactive for N days are labeled `stale`; still-inactive PRs are closed. The digest header names the source repo so batches from different repos posted to the same channel are unambiguous. Thresholds, messages, exempt labels, and the Slack channel are inputs; the caller owns the schedule + dry-run toggle. The calling job needs `pull-requests: write` and `issues: write`. Optional `SLACK_BOT_TOKEN`. |
| [`groom.yml`](.github/workflows/groom.yml) | Scheduled/dispatch org-wide **code-cleanup sweep** (finds only — no commits, no PRs, never merges). A read-only FINDER agent scans a clean default-branch checkout (whole-repo, not a diff) for high-value refactors; an INDEPENDENT VERIFIER agent (fresh session) re-checks each as CONFIRM/DOWNGRADE/REJECT with a stable dedup signature; survivors are deduped against a durable GitHub-issue-state ledger and filed as `groom`-labeled GitHub issues (security-adjacent ones get `groom-security` — investigate, don't auto-implement). Mirrors the cursor-review topology: briefs + ledger live in [`.github/groom/`](.github/groom) as the single source of truth. The finder/verifier/builder agent jobs invoke the Claude CLI directly and mint no GitHub token, so they need nothing beyond `contents: read`; filing runs in a separate job as the bot you configure via `bot_app_id` (Comfy: cloud-code-bot). `dry_run` reports what it would file without opening issues. Runs on a **daily base cron** with a runtime cadence gate: set repo Actions variable `GROOM_INTERVAL_DAYS` (default 7 = weekly) to retune how often a real run happens — weekly → every-3-days → daily — with no workflow-file edit; a tick within the interval no-ops before the finder (`workflow_dispatch` always runs). The calling job must grant `contents: read` + `issues: write` + `pull-requests: read` + `actions: read` — the first three are declared by the `file` / `build_select` jobs (needed even with `bot_app_id` set), and the interval gate needs `actions: read` (reads run history for the last real run); GitHub rejects a shorter grant at startup. Requires `ANTHROPIC_API_KEY` (+ `BOT_APP_PRIVATE_KEY` when `bot_app_id` is set). **Opt-in auto-builder** (`builder: true`, BE-4003): the top `max_prs` (default 5) CONFIRMED, non-security findings become **review-gated PRs** (full CI + cursor-review, **never auto-merged**) instead of issues; a credential-free `build` job emits only a patch artifact and a separate `build_pr` job opens the PR as the bot, preserving the security boundary. The ledger's PR-state (open/merged/closed) stops a built finding being re-proposed. Requires `bot_app_id`. |
| [`agents-md-integrity.yml`](.github/workflows/agents-md-integrity.yml) | Enforces the Comfy `AGENTS.md` standard on the caller repo: a top-level `AGENTS.md` must exist and stay under a hard line ceiling (`max_lines`, default 200; warns over `warn_lines`, default 150), a `CLAUDE.md` (if present) must be a thin `@AGENTS.md` shim rather than a divergent copy, no legacy `.cursorrules` (gated `forbid_cursorrules`), every nested monorepo `AGENTS.md` needs a sibling `@AGENTS.md` shim and to be under the ceiling (gated `check_nested`), and `AGENTS.md` should have a CODEOWNERS DRI (`require_codeowners`, warn-only by default). Fails with a non-zero exit + GitHub annotations so it wires in as a required status check. The checker lives in [`.github/agents-md-integrity/`](.github/agents-md-integrity) (pin `workflows_ref` to the same ref as `uses:`); no secrets required. |

## Usage

Reference a workflow by full path and pin to a **full commit SHA** (with the version as a trailing comment). Also set explicit minimum permissions on the calling job so the default permissive token scope isn't granted:

```yaml
permissions:
  contents: read
  pull-requests: read

jobs:
  my-job:
    uses: Comfy-Org/github-workflows/.github/workflows/<workflow-name>.yml@<sha>  # v1
    with:
      <input>: <value>
    secrets:
      <SECRET>: ${{ secrets.<SECRET> }}
```

The SHA-pin format satisfies pin-validation tooling (`pinact`, `zizmor`, etc.) and gives auditors immutable supply-chain evidence. Dependabot/Renovate can auto-bump the SHA when the upstream tag moves.

A bare `@v1` tag is technically allowed but **will fail** in repos that run pin-validation in CI (e.g. `cloud`, `ComfyUI_frontend`).

Per-workflow inputs, required secrets, and triggers are documented in each workflow file's header comment.

## Versioning

Workflows in this repo use **semver-style major-version tags** (`v1`, `v2`, …).

- Breaking changes bump the major (`v1` → `v2`); callers opt in.
- Backwards-compatible changes update the existing major tag in place (`git tag -f v1 <sha> && git push -f origin v1`) — callers pinned to the tag pick up the update on the next run; callers pinned to a SHA opt in by bumping the SHA.

## Adding a new reusable workflow

1. Add the workflow file under `.github/workflows/<descriptive-name>.yml` with `on: workflow_call:` and a header comment documenting inputs/secrets.
2. Update the table in this README.
3. Move the floating `v1` tag (or cut a new major) once the change is reviewed and merged.
