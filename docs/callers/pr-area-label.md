# `pr-area-label.yml` — agentic PR area labeling

Read [the shared caller contract](README.md) first.

## What it does

Two jobs, one taxonomy that lives in **your** repo (`.github/area-labels.yml` by default):

- **`sync-labels`** — on push to your default branch that touches the taxonomy (or a manual
  full-sync), create-or-updates every `area:*` label from the YAML. Edit the YAML, never the
  GitHub UI. No secret.
- **`label-pr`** — classifies each PR into exactly one `area:*` label, plus any
  [path sub-labels](#path-sub-labels) the PR's changed files matched. An LLM does the
  judgement (the domain-vs-path call a static `paths:` map can't make), but it gets **no
  tools and no token**: PR metadata is passed as data inside `<pr_data>` tags, the reply is
  enum-constrained by a JSON schema to your taxonomy's own names, and a deterministic step
  applies the result with targeted `area:*` ops. The taxonomy is read from the PR's **base
  ref**, so a PR cannot rewrite the rules that classify it. Everything fails soft.

The classifier scripts load from **this** repo at the pinned `workflows_ref` — never from
the classified PR's checkout. Repo-specific framing goes in the taxonomy's optional
`repo_context:` key, so the shared workflow carries nothing repo-specific.

## Prerequisites

| | |
|---|---|
| `.github/area-labels.yml` | **Required.** Your taxonomy (see shape below). Without it, `label-pr` skips cleanly and `sync-labels` has nothing to do. |
| `secrets.ANTHROPIC_API_KEY` | Needed by `label-pr`. Provided org-wide at Comfy-Org, so enrolled repos generally need no per-repo secret. If unset, `label-pr` fails soft (warns, labels nothing); `sync-labels` still works. |

## Caller

`.github/workflows/pr-area-label.yml`:

```yaml
name: PR Area Label

on:
  push:
    branches: [main]                     # replace `main` if your default branch differs —
                                         # otherwise the label sync never runs
    paths: ['.github/area-labels.yml']   # sync labels when the taxonomy changes
  pull_request:
    types: [opened, synchronize, ready_for_review, reopened]
  workflow_dispatch:
    inputs:
      pr_number:
        description: "PR to (re)classify (empty ⇒ full label sync)"
        required: false
      dry_run:
        description: "Classify and log without applying"
        type: boolean
        default: false

jobs:
  area-label:
    permissions:
      contents: read          # sync reads the taxonomy; label-pr checks out no PR code
      issues: write           # sync-labels: labels are an issues API surface
      pull-requests: write    # label-pr: apply the label
    uses: Comfy-Org/github-workflows/.github/workflows/pr-area-label.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
      pr_number: ${{ github.event.inputs.pr_number }}
      dry_run: ${{ github.event.inputs.dry_run == 'true' }}
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

Then ask a maintainer to add your repo to the `AREA_LABEL_CALLERS` roster secret on
`Comfy-Org/github-workflows` — that roster is what keeps your pin current
(see [Staying current](README.md#staying-current)). Enrolment does not backfill your pin:
ask them to `workflow_dispatch` `bump-area-label-callers.yml` once after adding you.

## Required permissions

```yaml
contents: read
issues: write
pull-requests: write
```

**Grant the whole union.** A reusable workflow can only narrow the caller's token, and
GitHub validates *every* nested job's declared `permissions:` against this block at
**startup** — before any `if:` runs. `sync-labels` declares `issues: write` and `label-pr`
declares `pull-requests: write`, so a short grant fails the whole run with an opaque
"workflow file issue" and no job-level detail, even though only one job would have run.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. Validated to be a full 40-hex lowercase SHA before checkout — a branch, tag, or short SHA fails the run. |
| `taxonomy_path` | `.github/area-labels.yml` | Path of your taxonomy YAML. |
| `model` | `claude-opus-4-8` | Classifier model. |
| `pr_number` | `''` | For a manual dispatch: the PR to classify. Empty on push/PR runs; a dispatch with no number runs a full label sync. |
| `dry_run` | `false` | Classify and log the decision without applying. |

## Taxonomy shape

```yaml
# .github/area-labels.yml
repo_context: |            # optional — repo/domain framing injected into the system prompt
  Infra-as-code for Example Org across GCP and Cloudflare. Classify GKE *cluster infra* as
  area:gcp but *workloads on it* as area:kubernetes.
labels:
  - name: "area:gcp"       # must match ^area:[a-z0-9-]+$, unique across the file
    color: "0e8a16"
    description: "GCP infrastructure"       # what GitHub stores (≤100 chars)
    guidance: "terraform/gcp; GKE cluster infra lives here."   # optional; classifier reads
                                                               # this, falling back to description

sub_labels:                # optional — see "Path sub-labels" below
  - name: "area:router"    # same shape as above; must NOT also appear in labels[]
    color: "7057ff"
    description: "Router: the model-routing layer inside the API service"
    paths:
      - "services/api/router*/**"
      - "**/*router*"
```

## Path sub-labels

Use these when a subsystem lives **inside** another area's service and you still want to
filter for it — the classic case being a component that is genuinely a subset of one area, so
promoting it to its own area would just hide those PRs from the parent area's filter.

`labels[]` answers *"which area owns this PR?"* — a judgement call, so a model makes it and
exactly one wins. `sub_labels[]` answers *"does this PR touch X?"* — a path question, so the
model never sees it: the globs are matched against the PR's changed files and the result
rides **alongside** the classified area.

A PR touching `services/api/routerqueue/queue.go` ends up with **`area:api` +
`area:router`**. Matched sub-labels are exempt from the one-area cleanup; a sub-label that
stops matching (the PR dropped those files) is retired by that same cleanup on the next run.
Glob semantics match a workflow `paths:` filter's, so you can copy a glob between the two:
`*` and `?` stay within one path segment, `**` crosses segments, and a leading `**/` matches
zero or more directories (`**/*router*` also matches a root-level file).

Sub-label names must be canonical `area:*` slugs and **disjoint** from `labels[]` — a name in
both would be ambiguous during cleanup, and the run is skipped with a warning rather than
guessing. Adding a `sub_labels:` block needs no caller change; `sync-labels` creates the
labels when it lands on your default branch. Repos that omit the key are unaffected.

## Gotchas

**Fork and Dependabot PRs are skipped by construction.** Both run without the writable token
and the ANTHROPIC secret, so they can't label. `label-pr` guards on
`head.repo.full_name == github.repository` and `pull_request.user.login != 'dependabot[bot]'`
inside the reusable — the caller stays a bare `uses:`.

**Labeling starts on the PR *after* the taxonomy lands.** `label-pr` reads the taxonomy from
the base ref; the PR that first introduces `.github/area-labels.yml` reads a base ref where
it doesn't exist yet and skips cleanly. After that PR merges, `sync-labels` creates the
labels and the next PR gets classified.

**A sub-label is only as good as its globs.** They are matched against the PR's changed
file paths and nothing else — no content, no judgement. A file that *is* the subsystem's work
but doesn't live under a matching path won't be tagged, and a rename that moves files out
from under a glob silently stops matching. Keep the globs broad (a `**/*name*` catch-all next
to the directory patterns costs nothing) and treat the label as a filter, not an audit. The
changed-file list also comes from `gh pr view --json files`, which returns the first 100
files, so a sub-label can be missed on a PR larger than that — the same list the classifier
itself has always been shown.

**The label is applied with the plain `GITHUB_TOKEN`.** That means it cannot fire `labeled`
triggers — this classifier is structurally unable to start a workflow cascade. Don't
"upgrade" the write to an App token.
