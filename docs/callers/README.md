# Setting up a caller

Each reusable workflow in this repo is wired in the same way: you add a small
**caller** workflow to your own repository that hands off to the shared one. The
per-workflow pages give you a complete, copy-pasteable caller:

| Workflow | Setup guide | What you must provide |
|---|---|---|
| `groom.yml` | [groom.md](groom.md) | `ANTHROPIC_API_KEY` |
| `cursor-review.yml` | [cursor-review.md](cursor-review.md) | `CURSOR_API_KEY` |
| `cursor-review-auto-label.yml` | [cursor-review-auto-label.md](cursor-review-auto-label.md) | `vars.APP_ID` + App key |
| `pr-size.yml` | [pr-size.md](pr-size.md) | nothing (bot comment optional) |
| `pr-risk.yml` | [pr-risk.md](pr-risk.md) | nothing |
| `pr-derisk.yml` | [pr-derisk.md](pr-derisk.md) | `ANTHROPIC_API_KEY` |
| `pr-area-label.yml` | [pr-area-label.md](pr-area-label.md) | `.github/area-labels.yml` (+ `ANTHROPIC_API_KEY`, org-wide) |
| `agents-md-integrity.yml` | [agents-md-integrity.md](agents-md-integrity.md) | nothing |
| `coderabbit-config-validate.yml` | [coderabbit-config-validate.md](coderabbit-config-validate.md) | nothing |
| `assign-reviewers.yml` | [assign-reviewers.md](assign-reviewers.md) | `vars.APP_ID` + App key + `.github/reviewers.yml` |
| `assign-prs-to-author.yml` | [assign-prs-to-author.md](assign-prs-to-author.md) | nothing |
| `stale.yml` | [stale.md](stale.md) | `SLACK_BOT_TOKEN` (optional) |
| `detect-unreviewed-merge.yml` | [detect-unreviewed-merge.md](detect-unreviewed-merge.md) | `UNREVIEWED_MERGES_TOKEN` |
| `linear-ticket.yml` | [linear-ticket.md](linear-ticket.md) | `LINEAR_API_TOKEN` + a two-workflow caller |

Everything below applies to all of them. Read it once.

---

## The shape of a caller

A caller is a **complete workflow file** in your repo at
`.github/workflows/<name>.yml`. It needs its own `on:` trigger — the reusable
workflow does not supply one, so a caller without `on:` never runs:

```yaml
name: Groom

on:
  schedule:
    - cron: '17 9 * * 1'      # Mondays 09:17 UTC
  workflow_dispatch:

jobs:
  groom:
    permissions:              # see "Permissions" below — this is not optional
      contents: read
      issues: write
      pull-requests: read
      actions: read           # groom's runtime cadence gate reads run history
    uses: Comfy-Org/github-workflows/.github/workflows/groom.yml@<full-commit-sha>
    with:
      cadence: 7
      interval_days: 7
      workflows_ref: <same-full-commit-sha>   # see "Pinning" — must equal the uses: SHA
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

Three parts people leave out, in order of how much pain they cause:

1. `permissions:` on the calling **job**
2. `on:` at the top
3. the `*_CALLERS` roster entry (see [Staying current](#staying-current))

---

## Permissions — the one that fails at startup

> A nested job in a reusable workflow **cannot request more `GITHUB_TOKEN` scope
> than the calling job grants.** GitHub validates this at **startup**, before any
> job runs — regardless of `if:` guards, and regardless of a GitHub App token
> doing the real writes.

When the grant is short, the run does not fail *a step*. It fails with an opaque
"workflow file issue", **zero jobs, and no logs**. There is nothing to read,
which is what makes it expensive to debug. This has already bitten this repo's
own groom pilot once.

So: grant exactly the union listed on the workflow's setup page. More than the
minimum is a needless privilege; less is a startup failure.

```yaml
jobs:
  my-job:
    permissions:        # on the JOB, not the workflow
      contents: read
      issues: write
```

A caller-level `permissions:` block also works, but per-job is clearer once a
caller grows a second job.

---

## Pinning

Pin `uses:` to a **full 40-character commit SHA**, with the human-readable
version as a trailing comment:

```yaml
uses: Comfy-Org/github-workflows/.github/workflows/pr-size.yml@8c4ff3e… # v1
```

Get the current SHA:

```bash
gh api repos/Comfy-Org/github-workflows/commits/main --jq .sha
```

**Why not `@v1`?** The `v1` tag is a *moving* pointer — we force-push it for
backwards-compatible changes, so `@v1` silently changes what you execute. A SHA
is immutable and gives auditors real supply-chain evidence. Repos that run pin
validation in CI (`pinact`, `zizmor`) **will fail** a bare tag.

### `workflows_ref` must match your pin

Several workflows load assets — agent briefs, review prompts, checker scripts —
from a ref **at run time**, controlled by the `workflows_ref` input. The ref the
assets come from must be the **same commit** as your `uses:` pin — otherwise the
workflow is pinned and the code it runs is not.

**Only some workflows take the input at all.** `cursor-review`, `pr-size`,
`agents-md-integrity`, `coderabbit-config-validate`, `refresh-reviewers`,
`pr-risk`, `pr-derisk`, `pr-area-label`, `linear-ticket` and `groom` declare it;
`assign-reviewers`, `cursor-review-auto-label`, `detect-unreviewed-merge`,
`assign-prs-to-author` and `stale` do **not**. GitHub rejects an undeclared
`with:` key at startup, so copying the line into one of those callers fails the
run before any job starts.

Of the ten that do declare it, all but `groom` make it **required with no
default**. There is no `main` to "leave" it at: GitHub does not enforce
`required:` for `workflow_call` inputs, so each of them re-checks the value at
run time, in the checkout's own job, rather than letting `actions/checkout`
silently fall back to this repo's default branch. Two tiers of checking:

- `cursor-review`, `pr-size`, `agents-md-integrity`, `coderabbit-config-validate`
  and `refresh-reviewers` accept any non-empty ref, and `::warning::` a value
  that is not a full 40-hex commit SHA (branches and tags are mutable and can
  skew between jobs mid-run). An empty or omitted value fails with
  `::error::workflows_ref is required…`.
- `pr-risk`, `pr-derisk`, `pr-area-label` and `linear-ticket` **reject** it
  before checkout unless it is a full 40-hex lowercase SHA *and* an ancestor of
  this repo's `main` — so a branch, a tag, a `refs/pull/N/head` and any
  not-yet-merged SHA all fail. Merge the change here first, then bump the pin.
  (`pr-area-label` and `linear-ticket` name an empty value explicitly;
  `pr-risk` and `pr-derisk` have no separate empty branch, so an omitted input
  falls through to the shape error — `…must be the FULL 40-hex lowercase commit
  SHA … (got '')`. Both still fail before checkout.)

Two caveats on "it fails fast", so you do not rely on it as a safety net:

- The guard lives in **each checkout's own job**, not in a single gate. A job
  that is skipped (`cursor-review`'s label gate short-circuiting the run, say)
  never evaluates it, so an omitted ref can pass a run silently.
- One job is deliberately exempt: `cursor-review`'s `Prior-review ledger` job
  must never fail the run — the review matrix `needs:` it — so it carries no
  fail-closed guard. Since BE-8077 it resolves the ref in a step that WARNS on
  empty and skips the asset checkout, degrading the ledger to `status=unknown`
  rather than checking assets out of this repo's default branch.

`groom` is the one workflow that declares `workflows_ref` **without** making it
required (it defaults to `''`), and there leaving it unset **is safe**: an
omitted input falls back to `job.workflow_sha`, the commit the caller's `uses:`
resolved to, and every groom job fails closed with an `::error::` if that
resolves empty (it needs an Actions runner ≥ v2.334.0).

That is only true from BE-8077 onward. BE-4169 spelled the fallback
`github.job_workflow_sha`, which is **not** a property of the `github` context —
GitHub documents the commit-of-the-current-job's-workflow-file value as
`job.workflow_sha`, and this repo's own `groom.yml` said so where it reads the
agent CLI pin. Actions expands an unknown context property to `''` rather than
erroring, so on a groom pin older than BE-8077 an omitted `workflows_ref`
resolves to the empty string and `actions/checkout` takes this repo's **default
branch** — the mutable-assets hole the pin exists to close. On those pins, set
the input.

So, everywhere the input is **required**, set it explicitly to the same SHA:

```yaml
uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@07154fb…
with:
  workflows_ref: 07154fb…   # keep in lock-step with the uses: SHA above
```

---

## Concurrency

Check whether the reusable already declares a `concurrency` group. Today only
`groom.yml` does (`groom-${{ github.repository }}`).

**If it does, do not declare the same group in your caller.** The caller holds
the group while waiting for its `uses:` job, which is waiting to acquire the same
group with `cancel-in-progress: false`. Neither yields. The run hangs until
timeout. Let the reusable own serialization and stay out of the group.

If the reusable declares none, a caller-level group is fine and often useful
(`stale.yml`'s header shows exactly that).

---

## Staying current

Enrolling a repo is **two steps**, and the second is the one people miss.

1. Merge the caller workflow into your repo.
2. Add the repo to the matching roster secret on **this** repo:

   | Workflow | Roster secret |
   |---|---|
   | `groom.yml` | `GROOM_CALLERS` |
   | `cursor-review.yml` | `CURSOR_REVIEW_CALLERS` |
   | `cursor-review-auto-label.yml` | `AUTO_LABEL_CALLERS` |
   | `pr-size.yml` | `PR_SIZE_CALLERS` |
   | `pr-risk.yml` | `PR_RISK_CALLERS` |
   | `pr-derisk.yml` | `PR_RISK_CALLERS` (shared with `pr-risk.yml` — one entry per caller file) |
   | `pr-area-label.yml` | `AREA_LABEL_CALLERS` |
   | `linear-ticket.yml` | `LINEAR_TICKET_CALLERS` |
   | `agents-md-integrity.yml` | `AGENTS_MD_CALLERS` |
   | `coderabbit-config-validate.yml` | `CODERABBIT_CONFIG_CALLERS` |
   | `assign-reviewers.yml` | `ASSIGN_REVIEWERS_CALLERS` |
   | `detect-unreviewed-merge.yml` | `DETECT_UNREVIEWED_MERGE_CALLERS` |

   Each entry is `{"repo": "...", "file": ".github/workflows/<caller>.yml", "label": ""}`.

   The rosters are repo **secrets**, not variables (BE-6472) — a variable handed
   to a step through `env:` is printed unmasked in the env dump Actions emits
   before that step, which published private caller names in this public repo's
   logs. That means they are **write-only**: there is no read-back, so the
   canonical `callers.json` lives in a private infra/ops repo and a maintainer
   applies your entry from it with `gh secret set`. Ask a maintainer rather than
   editing the roster directly.

The `bump-*-callers.yml` workflows read those rosters to open pin-bump PRs when
the reusable moves. **A repo absent from the roster keeps its original SHA
forever** — it drifts behind, and eventually breaks when the two are no longer
compatible. That is not hypothetical: this repo's own `ci-groom.yml` is not in
`GROOM_CALLERS`, and its pin has not moved since the day it was written.

---

## Verifying it works

Before trusting a schedule, prove the wiring. How you trigger it depends on the
caller's `on:` block — there is no universal recipe.

```bash
# 1. Does the caller parse and resolve at all? (all callers)
gh workflow list --repo <your-org>/<your-repo>

# 2. Read the result of its most recent run
gh run list --repo <your-org>/<your-repo> --workflow <caller>.yml --limit 1
```

**Scheduled / dispatchable callers** — `groom`, `stale`, `assign-prs-to-author`.
These declare `workflow_dispatch`, so you can fire them by hand:

```bash
gh workflow run <caller>.yml --repo <your-org>/<your-repo>
```

`groom` and `stale` are the two that take a **`dry_run`** input: a full audit that
files/labels nothing and prints what it *would* do. Use it before the first live
run. It only reaches the reusable if your caller plumbs a `workflow_dispatch` input
through to `with: dry_run:` — [groom.md](groom.md) and [stale.md](stale.md) both
show that wiring.

```bash
gh workflow run groom.yml --repo <your-org>/<your-repo> -f dry_run=true
```

**PR-triggered callers** — `cursor-review`, `cursor-review-auto-label`, `pr-size`,
`agents-md-integrity`, `assign-reviewers`. As shown in their setup guides these
declare **no `workflow_dispatch` and no `dry_run`**, so `gh workflow run` errors out
instead of starting a run. Verify by opening a throwaway PR — and for
`cursor-review`, applying the review label.

Adding `workflow_dispatch` to the caller is only worth it for
`agents-md-integrity`, which has no event dependency at all: it checks out the repo
and validates the files, so a manual dispatch is a genuine smoke test. For the
others it buys nothing — `cursor-review`, `cursor-review-auto-label` and `pr-size`
read `github.event.pull_request`, which a dispatch does not populate, and
`assign-reviewers` logs `Not a pull_request event — nothing to do` and exits.

**Push-triggered caller** — `detect-unreviewed-merge` runs on push to the default
branch. Verify it by landing any commit there, then reading the run.

`conclusion: startup_failure` with no job logs is **always** worth checking the
permission grant first.

## Secrets

Comfy-Org provides `ANTHROPIC_API_KEY` and `CURSOR_API_KEY` at the **org** level,
so enrolled repos generally need no per-repo secret. Confirm rather than assume:

```bash
gh secret list --repo <your-org>/<your-repo>     # repo-level
gh variable list --repo <your-org>/<your-repo>
```

**Omitting** a required `secrets:` mapping fails at startup — loudly, with zero
jobs. But **mapping a secret that is not set** passes an empty string through, and
that failure is quieter: `cursor-review` degrades to a panel where every cell
produces nothing rather than failing red. So confirm the secret exists, not just
that the mapping is present.
