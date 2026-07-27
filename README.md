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
| [`groom.yml`](.github/workflows/groom.yml) | Scheduled whole-repo code-cleanup sweep. A read-only finder agent proposes refactors, an independent verifier re-checks each, and survivors are deduped against a durable ledger and filed as `groom` issues. Opt-in builder turns top findings into review-gated PRs. | [groom.md](docs/callers/groom.md) |
| [`cursor-review.yml`](.github/workflows/cursor-review.yml) | Label-triggered multi-model code review. A 4-lab × 2-review-type panel runs adversarial + edge-case passes; a judge model consolidates them into one PR review with per-finding severity badges, and the triggerer gets Slack DMs. | [cursor-review.md](docs/callers/cursor-review.md) |
| [`cursor-review-auto-label.yml`](.github/workflows/cursor-review-auto-label.yml) | Companion to the above. Applies the review label for an opted-in reviewer using an App token, so the label actually fires the review. | [cursor-review-auto-label.md](docs/callers/cursor-review-auto-label.md) |
| [`pr-size.yml`](.github/workflows/pr-size.yml) | PR-size cap. Fails (or, in `mode: warn`, only reports) when a PR's net **non-generated** diff exceeds `max_lines`, excluding lockfiles and generated code. Bypass label for legitimately large changes. | [pr-size.md](docs/callers/pr-size.md) |
| [`agents-md-integrity.yml`](.github/workflows/agents-md-integrity.yml) | Enforces the org `AGENTS.md` standard: it exists, stays under a line ceiling, `CLAUDE.md` is a thin shim, no legacy `.cursorrules`. Wires in as a required status check. | [agents-md-integrity.md](docs/callers/agents-md-integrity.md) |
| [`assign-reviewers.yml`](.github/workflows/assign-reviewers.yml) | Expertise-aware, load-balanced PR routing from a per-repo `.github/reviewers.yml`, with new-folk randomization. Writes the **assignee** field. | [assign-reviewers.md](docs/callers/assign-reviewers.md) |
| [`assign-prs-to-author.yml`](.github/workflows/assign-prs-to-author.yml) | Housekeeping. Assigns every open PR with no assignees to its author. Run on a schedule. | [assign-prs-to-author.md](docs/callers/assign-prs-to-author.md) |
| [`stale.yml`](.github/workflows/stale.yml) | Stale-PR sweeper (`actions/stale`) plus a Slack digest that names the repo on every PR line, so multi-repo batches in one channel stay readable. | [stale.md](docs/callers/stale.md) |
| [`detect-unreviewed-merge.yml`](.github/workflows/detect-unreviewed-merge.yml) | SOC 2 compliance. Detects PRs merged without prior approval and opens a tracking issue in [`Comfy-Org/unreviewed-merges`](https://github.com/Comfy-Org/unreviewed-merges). | [detect-unreviewed-merge.md](docs/callers/detect-unreviewed-merge.md) |

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
