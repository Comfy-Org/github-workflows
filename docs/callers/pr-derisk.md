# `pr-derisk.yml` — the `/derisk` split planner (beta)

Read [the shared caller contract](README.md) first, and
[`pr-risk.md`](pr-risk.md) — this workflow runs that grader.

## What it does

Someone with write access comments `/derisk` on a pull request. The workflow re-grades that PR
with the pr-risk grader, makes **one** model call for a semantic partition of the diff into a
chain of 2–5 **sequential** pull requests, has the **grader** compute the path floor of every
proposed step, and posts one sticky advisory comment.

**Nothing is gated, routed, merged, labelled or filed.** The comment is the entire product.

The model proposes *which files go together* and nothing else. Every floor in the comment is
computed by `grade-pr-risk.sh --stdin`, and any tier the model wrote is discarded before
rendering — see [`scripts/pr-derisk/README.md`](../../scripts/pr-derisk/README.md).

## Prerequisites

- **`ANTHROPIC_API_KEY`** available to the caller repo (org-level secret, or repo-level). It is
  passed through as the `anthropic_api_key` secret and used by exactly one step.
  **Confirm the org secret's visibility actually covers this repo before enrolling** — an
  org-level secret restricted to selected repositories reaches the workflow as an empty string,
  and the run then posts a failure comment rather than a plan.
- Nothing else. The label, check and comment surfaces of pr-risk are unrelated and need not be on.

## Caller

`.github/workflows/ci-pr-derisk.yml`:

```yaml
name: CI - PR de-risk

on:
  # Fires on EVERY comment in the repo, plain issues included. The `if:` below is
  # what keeps the run count sane; the reusable re-asserts the same conditions, so
  # forgetting one costs a skipped runner, not a hole.
  issue_comment:
    types: [created]

permissions: {}

concurrency:
  # Per PR, and deliberately NOT cancel-in-progress: two `/derisk` comments in a row
  # should queue. Cancelling the first can leave the reader with a comment from a run
  # that was killed mid-write.
  group: pr-derisk-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  derisk:
    if: >-
      github.event.issue.pull_request &&
      startsWith(github.event.comment.body, '/derisk')
    permissions:
      contents: read        # the consumer's .github/risk.json override, from the BASE ref
      pull-requests: write  # read the PR + diff, post/update the ONE advisory comment
      checks: read          # the check rollup the grader's reversibility axis reads
      actions: read         # that rollup's CheckRun -> checkSuite -> workflowRun hop
      statuses: read        # that rollup's legacy commit-status contexts
    uses: Comfy-Org/github-workflows/.github/workflows/pr-derisk.yml@<40-hex SHA>  # v1
    with:
      enabled: true
      workflows_ref: <the SAME 40-hex SHA, written out literally>
    secrets:
      anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
```

Pin **twice** — `uses:` and `workflows_ref:` — to the same full commit SHA, written out
literally. `bump-pr-derisk-callers.yml` moves both together; never hand-bump one alone.

## Inputs worth setting

| input | default | why you would change it |
|---|---|---|
| `enabled` | `false` | Ship the caller off, switch it on later. `vars.DERISK_CONFIG` = `{"enabled": true}` does the same with no PR, and `{"enabled": false}` is a kill switch that outranks this input. |
| `allowed_associations` | `OWNER,MEMBER,COLLABORATOR` | **Narrow it, never widen it.** `CONTRIBUTOR` and `NONE` are anyone with a GitHub account, and this command spends money. |
| `command` | `/derisk` | Matched with `startsWith`, so `/derisk please` works and a mid-sentence mention does not. |
| `model` | `claude-opus-5` | The partition is a judgement about a whole diff and the call happens once, on demand. |
| `max_steps` | `5` | A chain nobody will actually open is not a plan. |
| `max_diff_bytes` | `200000` | Over budget takes the deterministic fallback rather than planning off half a diff. |
| `repo_map_path` / `repo_runbooks_path` | `.github/risk.json` / `.github/risk-runbooks.json` | Same overrides pr-risk reads, from the same base ref. |

## Gotchas

- **The permission block is the UNION of every job's, including jobs a run will skip.** GitHub
  validates each nested job's declaration at startup, before any `if:` is evaluated, so all five
  grants above are required whatever `enabled:` is set to.
- **Enrolling a caller is TWO steps.** Merge the caller *and* add the repo to the
  `PR_DERISK_CALLERS` roster secret, or the pin never moves and the caller silently drifts behind
  the tool.
- **`issue_comment` runs from your DEFAULT BRANCH**, always. That is what makes a comment command
  safe to hold write permission and a secret — a pull request cannot edit the workflow that serves
  it — and it also means a change to this caller only takes effect once merged.
- **A comment from someone outside `allowed_associations` skips the run entirely** — no plan, no
  comment, no spend. That is deliberate: an explanation posted to an outside commenter would be a
  free way to make the bot talk.
- **The commenter gate is not a rate limit.** Any collaborator can type `/derisk` repeatedly and
  each one is a model call. The per-PR concurrency group serializes them; if that is not enough,
  turn the repo off with `vars.DERISK_CONFIG`.
- **It grades on demand, not on push.** The plan reflects the PR as of the moment you asked. Push
  and ask again.
- **Beta.** The comment format, the input names and the chain shape may change between pins.
