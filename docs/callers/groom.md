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
All of them:

| Input | Default | Why you'd change it |
|---|---|---|
| `interval_days` | `7` | Effective run cadence (BE-4004): on the daily base cron, a tick within this many days of the last REAL run early-exits before the finder. Wire it to a `GROOM_INTERVAL_DAYS` repo Actions variable so cadence is a variable edit, not a workflow-file change. `workflow_dispatch` bypasses this gate (not `volume_gate`). `0` disables the throttle. |
| `cadence` | `7` | Volume-gate window in days — set to match `interval_days` (wire both to the same variable) so the merge-activity check lines up with how often a real run can happen. Feeds the volume gate below. |
| `volume_gate` | `true` | Skips the (expensive) audit when nothing merged in `cadence` days. **This is the cost control — leave it on** for scheduled runs. |
| `dry_run` | `false` | Full audit + dedup, files nothing, prints what it would file. Use before the first live run. |
| `max_findings` | `12` | Cap issues per run. Lower it on a first pilot. |
| `sink` | `github` | Where verified findings are filed. `github` opens issues in the calling repo, labeled `groom`. `linear` is **reserved for a later phase and not implemented** — selecting it fails the run loudly rather than silently filing nothing. |
| `model` | `claude-opus-5` | The finder/verifier model. |
| `themes` | `duplication, inconsistent patterns, missing abstractions, complexity hotspots, dead code` | Steer the finder at particular kinds of cleanup. The default mirrors the finder brief's own five dimensions, so it is a no-op; **narrow** it (e.g. `duplication, dead code`) to focus a repo. Security/auth-adjacent findings are filed regardless of theme. |
| `scope_label` / `scope_desc` | `whole-repo` | Labels for the scope in issue bodies. `scope_desc` is purely cosmetic; `scope_label` is **not** — it is also the ledger's dedup namespace, so changing it re-files every open finding once. See the `path` row. |
| `path` | `''` | Audit ONE directory instead of the whole repo (BE-4757). Empty is the whole-repo behavior, byte-for-byte. Give a path relative to the repo root (`services/api`); absolute paths, `..` **components** and symlinked directories are rejected before any billed agent runs. Unlike `scope_desc` this **constrains** the run — the finder is handed the in-scope file list, and a finding whose evidence all falls outside the directory is dropped on both the finder's and the verifier's output. The checkout stays full on purpose, so in-scope code can still reference `common/`. Cadence is per scope: a scoped run does not stamp over the next whole-repo tick. Dedup is shared across scopes only while they share a `scope_label` — the ledger signature is namespaced by `scope_label`, never by `path`, so scoped runs that leave it unset (the default) do recognise each other's and the whole-repo run's findings, while per-directory runs that each set a DISTINCT `scope_label` each get their own dedup namespace and can re-file the same finding. |
| `workflows_ref` | `''` | **Leaving it unset is safe.** Alone among these workflows groom does not *require* it — it defaults to `''` and each asset checkout falls back to `${{ job.workflow_sha }}`, the commit your `uses:` pin resolved to, so the briefs, `ledger.py` and `interval.py` always match the logic running them with nothing to keep in sync. Set it only to test briefs from a branch. Before BE-8077 that fallback was spelled `github.job_workflow_sha` and silently loaded the assets from this repo's default branch — see the footgun below. |
| `config` | `''` | JSON escape hatch for a caller that wants its operational config pinned in the reviewed workflow file. **The normal route is `vars.GROOM_CONFIG` on your own repo** — the `gate` job reads it directly (the `vars` context inside a reusable resolves against the CALLER), so a consumer opts in with no `with:` change at all, and you almost certainly want that instead of this. Use this input when a value must beat the repo variable, which it ranks above: a reusable cannot see the caller's `workflow_dispatch` inputs, so forwarding one as a JSON blob here is the only way to make a one-run override win. Same allowlist and same fail-open degradation — see `.github/groom/config.py` for the key list (BE-5227). |
| `bot_app_id` | `''` | File as your App rather than `github-actions[bot]`. |
| `environment` | `''` | Bind a GitHub environment (in YOUR repo) on the three jobs that mint the bot App token — `build_select`, `file`, `build_pr` — so `BOT_APP_PRIVATE_KEY` can be an environment secret behind a deployment-branch policy instead of a repository secret every branch can read. Empty (the default) binds nothing, and so does any value while `bot_app_id` is unset. Deployment-branch policies only, and the environment must exist before you set this. See "Scoping the bot key to an environment" below. |
| `builder` | `false` | Opt into PR-writing — see below. |
| `max_prs` | `'5'` | Only with `builder: true`. Typed **string**, deliberately. |
| `pr_size_limit` | `600` | Only with `builder: true`. Caps a built PR's diff. |
| `bail_sink` | `issue` | Only with `builder: true`. Where the builder's BAIL issues go when a CONFIRMED finding cannot become a PR — no patch, patch over `pr_size_limit`, patch touching a CI-privileged path, patch that will not apply, or the pre-publish secret scan withholding it (BE-6157). `issue` files a `groom` issue so a paid-for audit is not thrown away; that path lives in `build_pr`, so `max_findings` does not cap it. `none` files nothing and announces the bail as a `::warning::` plus a run-summary line instead — but nothing records the signature in the ledger, so a **deterministic** bail re-bails every run and permanently holds one of the `max_prs` slots (at `max_prs: 1`, nothing else is ever built). The secret-scan withhold is filed regardless. `linear` is not implemented and — unlike `sink: linear` two rows up, which does fail the run — does **not** fail: it is dropped with a `::warning::` and resolves back to `issue`. |
| `extra_denied_paths` | `''` | Only with `builder: true`. Newline-separated Python-`re` patterns adding this repo's OWN CI-privileged paths (a `scripts/ci/` entrypoint, a custom runner) to the built-in patch deny-list, so a matching builder patch is filed as an issue rather than opened as a PR. **Additive-only** — a caller widens the deny-list, never narrows it. A pattern that will not compile fails the run **closed** at the `gate` job before any agent spend, blocking the whole audit (both sinks) until fixed — a security-control typo must not silently widen the allow side. Propose broadly-applicable paths upstream in `patch_policy.py`; keep only repo-specific ones here. See below. |

## Scoping the bot key to an environment

By default `BOT_APP_PRIVATE_KEY` is a **repository** secret, which any workflow on any branch of your repo can read. Set `environment: bot-main` and the three credentialed jobs — `build_select`, `file` and `build_pr`, the only ones that mint the bot App token — bind that environment, so you can hold the key as an **environment** secret with a `main`-only deployment-branch policy instead. The agent jobs (`audit_find`, `audit_verify`, `build`) deliberately never bind it: they run a model over untrusted repo content and must stay outside any credentialed environment. Nothing binds while `bot_app_id` is unset either — with no App there is no token for an environment to guard.

### Why the environment's secret wins

Your caller's `secrets:` mapping is evaluated in the *caller* job, which cannot itself carry `environment:`, so what you pass through is whatever the caller could see. The substitution happens on the other side, and it is documented behaviour rather than an inference — GitHub's [Reuse workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows#using-inputs-and-secrets-in-a-reusable-workflow) guide warns:

> Environment secrets cannot be passed from the caller workflow as `on.workflow_call` does not support the `environment` keyword. If you include `environment` in the reusable workflow at the job level, the environment secret will be used, and not the secret passed from the caller workflow.

That name match is the whole mechanism: **the environment must hold a secret named exactly `BOT_APP_PRIVATE_KEY`.**

What happens when it does *not* — because the name is misspelled, or because a typo'd `environment:` auto-created an empty one — is the one thing here we have **not** confirmed against a run. GitHub's precedence model is a merge in which the most specific level wins, which implies the caller-passed value simply stays in place: a green run that quietly never used the environment copy. The competing reading is that binding the environment leaves the job with no usable key and token minting fails outright. The documented sentence above settles which secret wins when *both* exist; it does not settle the absent case, and we have not tested it.

Assume the silent one. It is the reading that costs you something — a loud mint failure tells you immediately, whereas a green run that still reads the repository key looks exactly like success. Step 4 below is what catches it either way, and it is why step 5 comes last.

### Migration sequence

Do these in order. Steps 1–3 are additive and reversible; **step 5 is the one that actually removes the exposure**, and doing it before step 4 breaks groom.

1. **Create the environment and its deployment-branch policy first.** Settings → Environments → New environment (`bot-main`), then restrict deployment branches to your default branch. Do not skip this: GitHub creates a referenced-but-missing environment **on demand, with no rules and no secrets**, so a typo'd or not-yet-created name gives you a run with no gate on it at all — and, on the reading above, no error to notice either. Check the environment name against Settings → Environments rather than trusting a green run.
2. **Add the App key to that environment** as an environment secret named exactly `BOT_APP_PRIVATE_KEY`.
3. **Set the input** on your caller: `environment: bot-main` under `with:`. It is an ordinary `type: string` input (a `${{ vars.* }}` expression works too) — it is *not* a secret and must not be routed through `secrets:`.
4. **Validate one real run.** Confirm the run shows a deployment to `bot-main` on `build_select` and `file`, and that issues were filed under the bot. Do not treat green alone as proof — a run that is still reading the repository key is also green. The deployment appearing on those two jobs is the signal that the binding took effect. Keep the repository secret until this passes.
5. **Only then delete the repository-level secret** (`CLOUD_CODE_BOT_PRIVATE_KEY` in the Comfy setup). This is the step that ends the "readable from every branch" exposure — until you do it, the key is exactly as reachable as before, no matter what the environment says. Deleting it is also what makes step 4's check meaningful in retrospect: from here on, a working run *cannot* be one that fell back. Treat the repository secret as a **rollback path** rather than a safety net — if the environment-backed run misbehaves, re-adding it restores the previous state.

**Your caller's `secrets:` mapping does not change**, and step 5 does not break it. The final form is still:

```yaml
    with:
      bot_app_id: ${{ vars.APP_ID }}
      environment: bot-main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      # After step 5 this resolves to '' — the repository secret is gone. That
      # is fine and expected: the three credentialed jobs bind `bot-main` and
      # use ITS `BOT_APP_PRIVATE_KEY` instead of the value passed here. Keep the
      # line: the input is declared `required: false`, but dropping it makes the
      # pre-migration and mid-migration states fail instead of degrading.
      BOT_APP_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}
```

### What to point it at

- **Use a DEDICATED environment holding only this key.** Binding is all-or-nothing: *every* secret and variable in the environment is injected into these three jobs — including `build_pr`, which applies a model-authored patch and pushes it as the bot. Pointing this at a pre-existing environment that also holds, say, deploy credentials silently widens what those jobs can reach.
- **Deployment-branch policies only — no required reviewers, no wait timer.** A *pausing* rule suspends `build_select`, which both `file` and `build_pr` depend on. This workflow runs under `concurrency: groom-<repo>` with `cancel-in-progress: false`, so a run parked awaiting approval (up to 30 days) holds the group and every later daily tick queues behind it and is cancelled — groom stops for that repo with no failure and no log. `build_pr` makes it worse: it is a `max-parallel: 1` matrix over up to `max_prs` findings, so it raises one deployment **per finding**, not one per run — up to five sequential approvals, and a cell denied after earlier cells have pushed leaves a half-filed run.
- **Make the branch policy cover every branch groom actually runs from.** A denied deployment is not free: the finder and verifier have already been billed by then, their findings are lost, and the run still counts as the last real one for the `interval_days` cadence gate (which anchors on the finder having spent, not on the run having filed anything), so the next `interval_days` of scheduled ticks no-op. If you `workflow_dispatch` groom from feature branches, a default-branch-only policy will silently eat those runs.

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

### Extending the patch deny-list for your repo (`extra_denied_paths`)

A builder patch that touches a path your CI **executes before a human reviews the merge** — a workflow/action def, a dependency lockfile, a build/test config — is downgraded from an auto-PR to a filed issue: on a same-repo branch push that code runs with your repository secrets and a writable token before review. The shared deny-list lives in the tested [`patch_policy.py`](../../.github/groom/patch_policy.py) and covers the common cross-ecosystem cases, but it cannot know YOUR repo's bespoke privileged surface — a checked-in `scripts/ci/` entrypoint, a custom build runner.

`extra_denied_paths` closes that gap without editing the reusable workflow. Pass newline-separated Python-`re` patterns; each is matched (case-insensitively, same semantics as the built-ins) against every changed path, and any match files the patch as an issue for a human to author:

```yaml
    with:
      builder: true
      extra_denied_paths: |
        ^scripts/ci/
        ^deploy/run\.sh$
```

Two properties matter. It is **additive-only** — patterns are OR-ed onto the built-in list, so you can widen the deny-list but never narrow it (there is no way to un-deny a built-in path). And it **fails closed on a typo**: a pattern that will not compile prints an `::error::` naming it and fails the run at the `gate` job, before any agent spend. Because every downstream job needs that gate, this blocks the whole audit — both the PR builder and the issue-filing sink — until you fix it (a loud, cheap red instead of billing the full agent budget and only then aborting the PR path); a broken deny-list must never silently let the path it was meant to guard sail through to an auto-PR. Erring wide is safe (a false positive only files an issue instead of opening a PR; nothing is dropped), so when in doubt, add the pattern. If a path is privileged for *most* repos, propose it upstream in `patch_policy.py` so every caller benefits, and keep only the genuinely repo-specific ones here.

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
≥ v2.334.0; every job that checks out the **briefs and scripts** now fails
closed with an `::error::` if the ref resolves empty, so that failure is loud
rather than a silent default-branch checkout. The one path deliberately left
warn-only is the **agent CLI pin**: it reads `ref: ${{ job.workflow_sha }}`
alone — no `workflows_ref` in front of it, and no guard — so on a pre-v2.334.0
runner it reads the CLI manifest from the default branch behind a `::warning::`
rather than failing the run. The pin is still validated; it just stops being
pinned by the caller's `uses:` SHA. Every groom job runs on `ubuntu-latest`,
which is always current, so only a stale self-hosted runner reaches either
branch.

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
