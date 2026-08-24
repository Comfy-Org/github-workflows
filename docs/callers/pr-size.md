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

Then ask a maintainer to add your repo to the `PR_SIZE_CALLERS` roster secret.

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
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. No default on purpose; the run fails fast (`Require a pinned workflows_ref` step) if omitted or empty. The `check-pr-size` tool is built from this ref. |

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

**A bypassed run reports no line count.** With the bypass label present the job
short-circuits: it skips both checkouts, the Go setup and the tool build, and
writes a "Bypassed via `oversized-ok` ✅" report directly — so it costs seconds
instead of minutes, and the sticky comment on a PR that was red flips to ✅ as
soon as the label lands. What you give up is the number: the report says the
check was bypassed, not "1500 counted / 1000 cap", because nothing counted the
lines. Remove the label to get the counted report back.

**Size runs are serialized per PR, newest wins.** The workflow carries a
`concurrency` group keyed on the PR number with `cancel-in-progress: true`, so a
new commit — or adding/removing the bypass label — cancels the run still in
flight. That is what keeps the seconds-long bypassed run from being overwritten
by a minutes-long counted run that started first and finishes last, leaving a
stale red comment on a PR that is now labelled. A cancelled run posts nothing.

**`exclude_tests` is a naming convention, not a proof.** Unlike the
generated-file rules — which require Go's marker *before* the package clause,
and read `.gitattributes` from the base ref precisely so a PR cannot exempt
itself — test detection only looks at the path. Nothing stops production code
being parked in `tests/` to duck the cap. That is why it is off by default, and
why the excluded total is always printed on its own line: the report shows
`Excluded (tests): N` next to the counted number, and lists the largest excluded
files — the biggest contributors, not a complete accounting (it stops at 10), but
enough to sanity-check that the exclusion is really tests. When the
exclusion is the *only* reason a PR is under the cap, the sticky comment posts
even though the check is green — otherwise the number would live solely in the
Actions step summary in precisely the case that matters, and a 5,000-line
"test-only" PR really would pass unremarked.

**That green-check comment needs the bot App.** It requires `comment: true`
(the default) *and* `bot_app_id` + `BOT_APP_PRIVATE_KEY` — all optional. Opt into
`exclude_tests` without the App and you lose the sticky comment — the only
surface that puts the number in front of a reviewer unprompted. The check
annotation described below still fires (it needs no credentials), so the totals
remain reachable via the Checks tab and the run's Details link, but reaching them
is a deliberate click. **If you set `exclude_tests`, configure the App too** —
otherwise you keep the loosening and keep only the weaker half of the
visibility.

**Fork and Dependabot PRs are a blind spot — weigh this before opting in.** Those
runs never receive `BOT_APP_PRIVATE_KEY` (GitHub withholds secrets from them), so
the green-check comment cannot post there *even for a caller that configured the
App correctly*. The consequence is uncomfortable and worth stating bluntly: with
`exclude_tests: true`, a fork PR gets the **weaker cap** and loses the mechanism
that makes the weaker cap safe. The trust gradient inverts — the least-trusted
contributions get the least-scrutinized guardrail — and it is silent, because a
green check with no comment looks exactly like a PR that passed on its own
merits.

**This is why a decisive exclusion also emits a check annotation.** It comes from
the size job itself and needs no credentials, so it reaches fork and Dependabot
PRs, carrying the same numbers as the comment (`counted + test` against the
applied cap).

Be clear about what it does *not* buy, though. An annotation with no file/line
renders on the run-summary page, behind the check's **Details** link, and in the
**Checks** tab — but **not** in the PR conversation (a passing check collapses to
"All checks have passed") and **not** inline on Files changed. So on a fork PR the
number is recorded and reachable, but still a deliberate click away. Only the
sticky comment puts it in front of a reviewer unprompted. Treat the annotation as
a backstop that makes the number *findable*, not as parity with the comment.

What you still lose without the App on a fork PR is the *sticky* comment: the
in-conversation explanation that survives pushes and flips to ✅. If that matters
for your outside contributions, leave `exclude_tests` off.

Note also that `extra_generated_globs` (below) classifies matches as
*generated*, not *test*: they never reach the excluded-test total and never
trigger the green-check comment. A repo leaning on it for an unusual test layout
opts out of this visibility guarantee.

**Recognized as test files.** File names: `*_test.go`; `test_*.py`, `*_test.py`,
`conftest.py`; and for `.js .jsx .mjs .cjs .ts .tsx .mts .cts`, a `test`
component anywhere in the name after the first (`Button.test.tsx`, and the
type-test form `api.test.d.ts`) or a `spec` component immediately before the
extension (`api.spec.ts`) — `spec` is narrower on purpose, because
`api.spec.types.ts` and `openapi.spec.client.ts` are OpenAPI *production* files
here and excluding them would under-count.

**Directories are matched in three cases, not one**, because the names are not
equally trustworthy:

| Case | Segments | Where they match |
|---|---|---|
| 1 | `__tests__/`, `__mocks__/`, `__snapshots__/`, `testdata/` | **any depth** — nothing else is ever called these, and Go nests `testdata` by design |
| 2 | `test/`, `tests/`, `testing/`, `e2e/` | **repo root only** |
| 3 | the same four, plus `it/` | **under a `src/` directory at any depth** — `module-a/src/test/java` works too. `it/` additionally requires a child segment, so `src/it/java/FooIT.java` is excluded but `src/it/messages.properties` **counts** (`it` is also the ISO-639-1 code for Italian) |

The root restriction in case 2 is not fussiness, it is a bug fix. A consumer
keeps production deployment manifests — cluster RBAC and ingress config — under
`deploy/envs/testing/`, where `testing` names the deployment
*environment*, not test code. Matching that name at any depth silently excluded
**cluster RBAC changes** from the cap. Root-anchoring keeps that repo's whole
root-level `testing/` tree excluded while counting the deployment files.

The cost is deliberate and worth knowing: a **nested** ambiguous directory such as
`services/checkout/e2e/` now counts. That is the safe direction — over-counting
starts an argument, under-counting silently shrinks the number the cap protects —
but if your tests live somewhere the three cases miss, they will be counted.

Segment matching is case-insensitive over ASCII (so `Tests/` and `TestData/`
work); the file-name rules stay case-sensitive, because their toolchains define
them in lowercase. `spec/` is deliberately *not* a test directory — in this org
it holds OpenAPI schemas, which are production artifacts. For a layout these
miss, add `extra_generated_globs` (they land in the generated bucket instead).

Leaving it off is a real choice, not just the safe one: a 5,000-line test diff
is genuinely slow to review, and the cap is the only thing that says so.

**Go workspaces:** a consumer with a root `go.work` needs `GOWORK=off` for the
tool build, since `go build` otherwise discovers the consumer's workspace.

**Do not commit the compiled binary.** `pr-size.yml` builds it from source into
`RUNNER_TEMP` on every run.
