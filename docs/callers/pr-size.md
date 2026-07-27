# `pr-size.yml` — PR-size cap

Read [the shared caller contract](README.md) first.

## What it does

Fails a PR whose net diff exceeds `max_lines` **non-generated** changed lines, to
keep diffs reviewable. In `mode: warn` it reports without failing.

Excluded from the count: dependency lockfiles, `linguist-generated` files (read
from the **base ref**, so a PR cannot exempt itself by editing
`.gitattributes`), Go generated-code markers, and anything you add via
`extra_lockfiles` / `extra_generated_globs`.

The counting logic and its tests live in
[`scripts/check-pr-size/`](../../scripts/check-pr-size) and are compiled from
source at run time.

## Prerequisites

None required. Optionally `vars.APP_ID` + `secrets.BOT_APP_PRIVATE_KEY` for the
explanatory sticky comment.

## Caller

`.github/workflows/pr-size.yml`:

```yaml
name: PR Size Cap

on:
  pull_request:
    branches: [main]
    # labeled/unlabeled are beyond the default set, so toggling the bypass label
    # re-runs the check immediately instead of needing a push.
    types: [opened, synchronize, reopened, labeled, unlabeled]

jobs:
  size:
    permissions:
      contents: read
    uses: Comfy-Org/github-workflows/.github/workflows/pr-size.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
      max_lines: 1000
      bot_app_id: ${{ vars.APP_ID }}
    secrets:
      BOT_APP_PRIVATE_KEY: ${{ secrets.BOT_APP_PRIVATE_KEY }}
```

Then add your repo to `vars.PR_SIZE_CALLERS`.

## Required permissions

```yaml
contents: read
```

## Inputs

| Input | Default | Notes |
|---|---|---|
| `max_lines` | `1000` | Net non-generated changed lines allowed. |
| `mode` | `enforce` | `warn` reports without failing — the right first step on an existing repo. |
| `bypass_label` | `oversized-ok` | Waves through a legitimately large change. |
| `extra_lockfiles` | `''` | Additional lockfiles to exclude. |
| `extra_generated_globs` | `''` | Additional generated-path globs to exclude. |
| `comment` | `true` | Sticky bot comment explaining an overage. |
| `bot_app_id` | `''` | Without it, degrades to status + step summary. |
| `workflows_ref` | `main` | **Set to your `uses:` SHA** — the tool is built from this ref. |

## Gotchas

**Start in `mode: warn`.** Turning on enforcement over an existing PR queue fails
open PRs that were fine when opened. Warn for a cycle, read the numbers, then
enforce.

**Without `bot_app_id` the bypass route is not discoverable.** The failing status
alone does not mention `oversized-ok`; the sticky comment is what tells an author
the escape hatch exists. Supply the App or expect confused authors.

**Go workspaces:** a consumer with a root `go.work` needs `GOWORK=off` for the
tool build, since `go build` otherwise discovers the consumer's workspace.

**Do not commit the compiled binary.** `pr-size.yml` builds it from source into
`RUNNER_TEMP` on every run.
