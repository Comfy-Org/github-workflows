# `pr-size.yml` — PR-size cap

Read [the shared caller contract](README.md) first.

## What it does

Fails a PR whose net diff exceeds `max_lines` **non-generated** changed lines, to
keep diffs reviewable. In `mode: warn` it reports without failing.

Excluded from the count: dependency lockfiles, `linguist-generated` files (read
from the **base ref**, so a PR cannot exempt itself by editing
`.gitattributes`), Go generated-code markers, and anything you add via
`extra_lockfiles` / `extra_generated_globs`. Optionally test files too — see
`exclude_tests` below.

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
    branches: [main]        # or [master] — YOUR default branch; see the gotcha
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
| `exclude_tests` | `false` | Keep test-file lines out of the count — cap production code, not coverage. Always reported separately. |
| `comment` | `true` | Sticky bot comment explaining an overage. |
| `bot_app_id` | `''` | Without it, degrades to status + step summary. |
| `workflows_ref` | `main` | **Set to your `uses:` SHA** — the tool is built from this ref. |

## Gotchas

**`branches:` must name your actual default branch.** The filter matches the PR's
**base**, so a repo whose default is `master` (or `develop`, or a release train)
gets *no* runs from the snippet above — the check silently never fires, no status
appears, and nothing signals the misconfiguration. Adapt the value, or drop the
`branches:` filter to size every PR regardless of base.

**Start in `mode: warn`.** Turning on enforcement over an existing PR queue fails
open PRs that were fine when opened. Warn for a cycle, read the numbers, then
enforce.

**Without `bot_app_id` the bypass route is not discoverable.** The failing status
alone does not mention `oversized-ok`; the sticky comment is what tells an author
the escape hatch exists. Supply the App or expect confused authors.

**`exclude_tests` is a naming convention, not a proof.** Unlike the
generated-file rules — which require Go's marker *before* the package clause,
and read `.gitattributes` from the base ref precisely so a PR cannot exempt
itself — test detection only looks at the path. Nothing stops production code
being parked in `tests/` to duck the cap. That is why it is off by default, and
why the excluded total is always printed on its own line: the report shows
`Excluded (tests): N` next to the counted number, and names the largest excluded
files so the number can be audited rather than taken on trust. When the
exclusion is the *only* reason a PR is under the cap, the sticky comment posts
even though the check is green — otherwise the number would live solely in the
Actions step summary in precisely the case that matters, and a 5,000-line
"test-only" PR really would pass unremarked.

**That green-check comment needs the bot App.** It requires `comment: true`
(the default) *and* `bot_app_id` + `BOT_APP_PRIVATE_KEY` — all optional. Opt into
`exclude_tests` without the App and you get precisely the outcome the paragraph
above says is prevented: a green check whose excluded total is visible only to
someone who opens the Actions step summary. **If you set `exclude_tests`,
configure the App too** — otherwise you keep the loosening and lose the
visibility that justifies it.

Note also that `extra_generated_globs` (below) classifies matches as
*generated*, not *test*: they never reach the excluded-test total and never
trigger the green-check comment. A repo leaning on it for an unusual test layout
opts out of this visibility guarantee.

Recognized: `*_test.go`; `test_*.py`,
`*_test.py`, `conftest.py`; `*.test.*` / `*.spec.*` for `.js .jsx .mjs .cjs .ts
.tsx .mts .cts`; and any file under a `test/`, `tests/`, `testing/`,
`testdata/`, `e2e/`, `__tests__/`, `__mocks__/` or `__snapshots__/` **directory**
segment (segment matching is case-insensitive, so `Tests/` and `TestData/` work
too; the file-name rules stay case-sensitive because their toolchains define
them in lowercase). `spec/` is deliberately *not* a test directory — in this org it holds
OpenAPI schemas, which are production artifacts. For a layout these miss, add
`extra_generated_globs` (they land in the generated bucket instead).

Leaving it off is a real choice, not just the safe one: a 5,000-line test diff
is genuinely slow to review, and the cap is the only thing that says so.

**Go workspaces:** a consumer with a root `go.work` needs `GOWORK=off` for the
tool build, since `go build` otherwise discovers the consumer's workspace.

**Do not commit the compiled binary.** `pr-size.yml` builds it from source into
`RUNNER_TEMP` on every run.
