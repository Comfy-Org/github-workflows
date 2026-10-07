# cursor-approve — per-axis approval after a passing cursor-review round

`cursor-approve.yml` approves a PR when independent per-axis reviewers agree it
is ready. Each axis is its own reusable workflow that declares only the secrets
it needs, so an axis never sees another axis's credentials. This guide covers
the two credential-free axes, `axis-correctness.yml` and `axis-conformance.yml`;
the context axes (business, design, completeness) are not available yet.

## What it does

1. `cursor-review` runs a round and exposes `approve_gate`, `round` and `max_rounds`.
   Call it with `defer_approval: true`: a passing round then reports
   `approve_gate == 'pass'` **without** posting an approval (and without
   resolving any thread), and withdraws the approving identity's own earlier
   approvals and requests for changes. Without it, cursor-review's `decide` APPROVES first, and that
   severity-only approval satisfies branch protection or auto-merge until the
   start phase withdraws it — and stays standing if the cursor-review run fails
   or is cancelled after approving, because the start phase and the axes are
   then skipped. With it, this workflow's decide phase is the only approver.
2. `cursor-approve` with `phase: start` first withdraws the approving
   identity's own approvals — a backstop: with `defer_approval: true` there is
   none from this round, but a caller that does not set it has one standing
   from cursor-review's `decide`, which must not satisfy branch protection or
   auto-merge while the axes judge. It then
   writes the **status card** — one PR comment, found by
   `<!-- cursor-approve-card -->` and edited in place — with every axis
   pending. When cursor-review reports `approve_gate == 'capped'` it writes
   "Round limit reached, needs a human" instead.
3. Each axis runs only when `approve_gate == 'pass'`, after the start phase: a
   read-only checkout of the PR head, one `cursor-agent` call over
   `.github/cursor-approve/`'s prompt, and one `axis-<name>` artifact holding
   `{"verdict", "confidence", "summary", "commit_sha"}`. A failed axis uploads
   nothing, and an axis refuses to run on a fork PR.
4. `cursor-approve` with `phase: decide` runs `aggregate.py decide` (any
   missing/malformed axis, one not stamped with `commit_sha`, any red, or more
   yellow than `max_yellow_axes` → no approval), reading each axis only from its
   own `axis-<name>` artifact, then `auto-approve.py approve-external`: it
   re-reads the head and base and withholds when either moved since the axes
   ran, never approves a PR labelled `needs-human-review` or
   `skip-cursor-review` (both read live from the PR at decide time, so a veto
   applied while the axes run still stops the approval — a caller's
   label-keyed concurrency group cannot cancel that in-flight decide),
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
| cursor-review with `defer_approval: true` | Without it cursor-review approves on severity alone before any axis has judged the PR (see step 1). |

## Caller

Pin every `uses:` and `workflows_ref` to the same full commit SHA. Keep the
per-PR `concurrency:` group cursor-review's caller already carries: it cancels an
older run when a newer one starts, so two runs never write the card or decide
on the same PR at once.

```yaml
concurrency:
  group: cursor-review-pr-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  cursor-review:
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@<sha>  # v1
    with:
      workflows_ref: <sha>
      approve_max_severity: low
      defer_approval: true  # only cursor-approve's decide approves
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
    needs: [cursor-review, approve-start]
    if: needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-correctness.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}

  axis-conformance:
    needs: [cursor-review, approve-start]
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
      approve_max_severity: low
      poster_login: github-actions[bot]  # `<app-slug>[bot]` if cursor-review runs with bot_app_id
      approve_scope: ${{ needs.cursor-review.outputs.approve_scope_effective }}
    secrets:
      APPROVER_TOKEN: ${{ secrets.APPROVER_TOKEN }}
```

With `defer_approval: true`, thread auto-resolution moves here too: this
phase's decide resolves cursor-review's at-or-below-threshold threads only when
`approve_max_severity` and `poster_login` are both set, and `poster_login` must
be the login cursor-review posts findings under — `<app-slug>[bot]` when
cursor-review runs with `bot_app_id`, else `github-actions[bot]`. A wrong login
resolves nothing, so a "require conversation resolution" ruleset still blocks.
Pass `approve_scope` from cursor-review's `approve_scope_effective` output, not
a literal: under cursor-review's default `approve_scope: delta`, a round marks
findings outside its changes as non-gating, and only when that round really
gated under `delta` may those marked threads stop blocking the resolution of
the others. Omitted, it is `full` and any open above-threshold thread — marked
or not — leaves every thread open for a human.

Pass secrets explicitly to the axes — never `secrets: inherit` — so each axis
receives `CURSOR_API_KEY` and nothing else. `always()` on the decide job makes a
failed axis read as "no result" (withheld) instead of skipping the decision and
leaving an earlier approval standing. The axes `needs: approve-start` so they
never start while cursor-review's approval still stands; a start phase that
fails to withdraw it skips them, which decide reads as "no result".

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
| `approve_max_severity` | `''` | Decide phase: the same threshold cursor-review runs with. With `poster_login`, an approval that stands auto-resolves cursor-review's own at-or-below-threshold threads exactly as cursor-review's own approval does. Empty → no thread is resolved. |
| `poster_login` | `''` | Decide phase: the login cursor-review posts findings under — `<app-slug>[bot]` when it runs with `bot_app_id`, else `github-actions[bot]`. Empty → no thread is resolved. |
| `approve_scope` | `full` | Decide phase: cursor-review's `approve_scope_effective` output. `delta` → an open above-threshold thread that round marked non-gating (*Outside this round's changes*) does not block resolving the at-or-below-threshold threads, exactly as cursor-review's own decide treats it; the marked thread itself is never resolved. `full` (or empty) → any open above-threshold thread, marked or not, blocks all resolution. Any other value warns and resolves as `full`. |

## Axis inputs

Every axis wrapper — `axis-correctness.yml`, `axis-conformance.yml`,
`axis-business.yml`, `axis-design.yml`, `axis-completeness.yml` — takes the same
three inputs. Their secrets differ; see [Context axes](#context-axes) for the
last three. (Without a checkout, `commit_sha` is the head whose merge-base
diff the workflow fetches from the GitHub API.)

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
| `context_sources` | `''` | Comma list of `linear`, `notion`, `slack`: the context-proxy tools the agent gets. Business, design and completeness only; private repos only. |
| `no_shell` | `false` | Deny cursor-agent's shell, file-write and web-fetch tools; the job fails if the transcript shows a shell or file-write call, or no recognizable tool call at all. Required, with `checkout: false`, for business and design. |

Every axis also uploads `transcript-axis-<axis>` — the agent's stream-json
transcript, plus the proxy's call log for the context axes. On a context axis
each tool call in it is cut to the tool's name: no arguments and no results, so
no Linear, Notion or Slack content. Only the verdict
JSON (`verdict`, `confidence`, `summary` capped at 1200 characters) reaches
`cursor-approve.yml`; its `axis-*` download never matches a transcript.

## Context axes

`axis-business.yml`, `axis-design.yml` and `axis-completeness.yml` judge a PR
against company context, not just its code. They read Linear, Notion and Slack
through `.github/cursor-approve/context-proxy.py`, a read-only MCP server that
holds the tokens: the agent can search, but never holds a credential.

| Axis | Agent sees | Context tools | Secrets (all but `CURSOR_API_KEY` optional) |
|---|---|---|---|
| business | PR title, body, changed-file list with line counts — no checkout, no shell | `linear_search`, `linear_get_issue`, `notion_search`, `notion_get_page`, `slack_search`, `slack_history` | `CURSOR_API_KEY`, `LINEAR_KEY`, `NOTION_TOKEN`, `SLACK_TOKEN` |
| design | PR title, body, merge-base diff cut at 200 KB — no checkout, no shell | `linear_search`, `linear_get_issue`, `notion_search`, `notion_get_page` | `CURSOR_API_KEY`, `LINEAR_KEY`, `NOTION_TOKEN` |
| completeness | Full read-only checkout (`persist-credentials: false`), shell allowed for `git` | `linear_search`, `linear_get_issue` | `CURSOR_API_KEY`, `LINEAR_KEY` |

A missing token leaves that source unconfigured; the axis still runs. The tokens
are read-only bot identities (Linear and Notion as the tools bot, Slack as the
cursor-approver app). The Slack bot cannot call `search.messages`, so the proxy
loads the last 30 days of every public channel the bot is a member of and
searches that.

How a token reaches the proxy without reaching the agent:

1. One step, the only one with the token secrets in its env, writes the enabled
   tokens to a `0600` file and passes on nothing but its path.
2. The axis step starts `context-proxy.py` FIRST, on two FIFOs and without
   `CURSOR_API_KEY`; the proxy reads the token file and deletes it, and the
   agent is not started until the file is gone. Tokens are never environment
   variables of the proxy or the agent.
3. cursor-agent's only MCP server is `mcp-relay.py`, which copies bytes between
   the agent and the already-running proxy. The axis step's env holds none of
   `LINEAR_KEY`, `NOTION_TOKEN`, `SLACK_TOKEN` (a test enforces this).

Each axis runs only on a same-repo PR in a **private** repository. On a fork PR
or a public repository the wrapper skips its axis with a notice; the base fails
closed on a public repo too. A skipped axis uploads no verdict, so leave it out
of `axes:` on a repo where it cannot run, or decide reads it as "no result".

Business and design fetch the change from the GitHub compare API, which
`contents: read` covers; no extra permission is needed.

```yaml
  axis-business:
    needs: [cursor-review, approve-start]
    if: needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-business.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
      LINEAR_KEY: ${{ secrets.LINEAR_KEY }}
      NOTION_TOKEN: ${{ secrets.NOTION_TOKEN }}
      SLACK_TOKEN: ${{ secrets.SLACK_TOKEN }}

  axis-design:
    needs: [cursor-review, approve-start]
    if: needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-design.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
      LINEAR_KEY: ${{ secrets.LINEAR_KEY }}
      NOTION_TOKEN: ${{ secrets.NOTION_TOKEN }}

  axis-completeness:
    needs: [cursor-review, approve-start]
    if: needs.cursor-review.outputs.approve_gate == 'pass'
    uses: Comfy-Org/github-workflows/.github/workflows/axis-completeness.yml@<sha>  # v1
    with:
      commit_sha: ${{ github.event.pull_request.head.sha }}
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
      LINEAR_KEY: ${{ secrets.LINEAR_KEY }}
```

**Accepted risk.** These axes read author-written text (the PR, and whatever
anyone wrote in an issue, page or channel) while holding read-only company
context. A prompt injection in any of it can make the agent repeat that context
into its verdict summary, which lands on the PR, or steer its verdict. That is
acceptable on private repositories only, where everyone who can read the PR can
already read the company, and is why the axes refuse to run on a public one.
The completeness axis keeps a shell for `git`: on a hosted runner (passwordless
`sudo`) a hostile agent could read the running proxy's memory, so its Linear
token is protected by policy and prompt, not by the sandbox.

## Trust model

Every verdict is model output over the PR's own content, so a diff that
prompt-injects every axis can steer a round to an approval. Treat the approval
as an automated review signal, not a substitute for a human reviewer.
