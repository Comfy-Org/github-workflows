# cursor-approve — per-axis approval after a passing cursor-review round

`cursor-approve.yml` approves a PR when independent per-axis reviewers agree it
is ready. Each axis is its own reusable workflow that declares only the secrets
it needs, so an axis never sees another axis's credentials. This guide covers
the two credential-free axes, `axis-correctness.yml` and `axis-conformance.yml`;
the context axes (business, design, completeness) are not available yet.

## What it does

1. `cursor-review` runs a round and exposes `approve_gate`, `round` and `max_rounds`.
2. `cursor-approve` with `phase: start` writes the **status card** — one PR
   comment, found by `<!-- cursor-approve-card -->` and edited in place — with
   every axis pending. When cursor-review reports `approve_gate == 'capped'` it
   writes "Round limit reached, needs a human" instead.
3. Each axis runs only when `approve_gate == 'pass'`: a read-only checkout of
   the PR head, one `cursor-agent` call over `.github/cursor-approve/`'s prompt,
   and one `axis-<name>` artifact holding `{"verdict", "confidence", "summary"}`.
   A failed axis uploads nothing.
4. `cursor-approve` with `phase: decide` runs `aggregate.py decide` (any
   missing/malformed axis, any red, or more yellow than `max_yellow_axes` → no
   approval), then `auto-approve.py approve-external`: it re-reads the head and
   withholds when it moved, never approves a PR labelled `needs-human-review`,
   records the reviewed SHA in the review body, and withdraws this identity's
   own earlier approvals whenever it does not approve. An approval is one line
   linking the card. The card is rewritten with the verdicts, the result and
   the rule — or "Superseded by a newer commit" when the head moved.

Every model-supplied string on the card goes through post-review.py's
`neutralize_mentions` plus markdown/HTML escaping, so a summary cannot add a
heading, fire a mention or forge the card marker.

## Prerequisites

| Requirement | Why |
|---|---|
| `secrets.CURSOR_API_KEY` | The axes bill through it. The only secret an axis receives. |
| `secrets.APPROVER_TOKEN` | Token of the approving identity (needs `pull-requests: write` on the repo, and to be allowed to dismiss reviews if branch protection restricts that). Empty → both phases warn and do nothing; there is deliberately no fallback to an App or `GITHUB_TOKEN`. |
| cursor-review with `approve_max_severity` set | Without it `approve_gate` is `off` and no axis runs. |

## Caller

Pin every `uses:` and `workflows_ref` to the same full commit SHA.

```yaml
jobs:
  cursor-review:
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@<sha>  # v1
    with:
      workflows_ref: <sha>
      approve_max_severity: low
    secrets: inherit

  approve-start:
    needs: cursor-review
    if: needs.cursor-review.outputs.approve_gate == 'pass' || needs.cursor-review.outputs.approve_gate == 'capped'
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-approve.yml@<sha>  # v1
    with:
      workflows_ref: <sha>
      phase: start
      commit_sha: ${{ github.event.pull_request.head.sha }}
      axes: correctness,conformance
      round: ${{ needs.cursor-review.outputs.round }}
      max_rounds: ${{ needs.cursor-review.outputs.max_rounds }}
      approve_gate: ${{ needs.cursor-review.outputs.approve_gate }}
    secrets:
      APPROVER_TOKEN: ${{ secrets.APPROVER_TOKEN }}

  axis-correctness:
    needs: cursor-review
    if: needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-correctness.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}

  axis-conformance:
    needs: cursor-review
    if: needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-conformance.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}

  cursor-approve:
    needs: [cursor-review, approve-start, axis-correctness, axis-conformance]
    if: always() && needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-approve.yml@<sha>  # v1
    with:
      workflows_ref: <sha>
      phase: decide
      commit_sha: ${{ github.event.pull_request.head.sha }}
      axes: correctness,conformance
      max_yellow_axes: 0
      round: ${{ needs.cursor-review.outputs.round }}
      max_rounds: ${{ needs.cursor-review.outputs.max_rounds }}
    secrets:
      APPROVER_TOKEN: ${{ secrets.APPROVER_TOKEN }}
```

Pass secrets explicitly to the axes — never `secrets: inherit` — so each axis
receives `CURSOR_API_KEY` and nothing else. `always()` on the decide job makes a
failed axis read as "no result" (withheld) instead of skipping the decision and
leaving an earlier approval standing.

## Inputs

`cursor-approve.yml`:

| Input | Default | Meaning |
|---|---|---|
| `workflows_ref` | — (required) | Same commit SHA as the `uses:` pin; checked against `job.workflow_sha`. |
| `phase` | — (required) | `start` (card with pending axes) or `decide` (aggregate, approve, final card). |
| `commit_sha` | — (required) | The PR head the axes judged. |
| `axes` | — (required) | Comma-separated axes the caller ran, e.g. `correctness,conformance`. |
| `max_yellow_axes` | `0` | Yellow axes tolerated (0–3, strictly below the number of axes). |
| `round` | `''` | cursor-review's `round` output, for the card heading. |
| `max_rounds` | `''` | cursor-review's `max_rounds` output, for the card heading. |
| `approve_gate` | `''` | cursor-review's `approve_gate` output; `capped` makes the start card say a human is needed. |

## Axis inputs

`axis-correctness.yml` and `axis-conformance.yml` (each declares only `CURSOR_API_KEY`):

| Input | Default | Meaning |
|---|---|---|
| `commit_sha` | — (required) | The PR head to judge; checked out read-only with full history. |
| `model` | `claude-opus-5-thinking-xhigh` | Cursor model id (cursor-review's judge model). |
| `runs_on` | `"ubuntu-latest"` | JSON-encoded `runs-on`, as in cursor-review. |

## Base inputs

`cursor-axis-base.yml` is the shared body the axis wrappers call; consumers do
not call it directly. It loads its prompts from this repo at `job.workflow_sha`.

| Input | Default | Meaning |
|---|---|---|
| `axis` | — (required) | Which axis prompt to render. |
| `commit_sha` | — (required) | As above. |
| `model` | `claude-opus-5-thinking-xhigh` | As above. |
| `runs_on` | `"ubuntu-latest"` | As above. |
| `checkout` | `false` | Full read-only checkout of the PR head (`persist-credentials: false`). |
| `mcp_config` | `''` | Reserved for the context axes; must be empty. |
| `no_shell` | `false` | Reserved for the context axes; must be false. |

## Trust model

Every verdict is model output over the PR's own content, so a diff that
prompt-injects every axis can steer a round to an approval. Treat the approval
as an automated review signal, not a substitute for a human reviewer.
