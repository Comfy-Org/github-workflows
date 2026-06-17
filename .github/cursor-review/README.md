# Cursor Review — multi-model PR review panel

Label-triggered code review that runs a **panel of frontier models from four
labs**, has each one review your PR from two angles, then has a single judge
model consolidate everything into **one** PR review with per-finding severity
badges.

The idea: no single model catches everything, and any one model produces noise.
Running several in parallel and adjudicating their findings gets you broader
coverage with less noise than trusting one model — and it surfaces the findings
the labs *agree* on.

This directory holds the prompts and scripts that drive that review. They are
the **single source of truth**: the reusable workflow
([`.github/workflows/cursor-review.yml`](../workflows/cursor-review.yml)) checks
this directory out at run time, so consumer repos carry only a thin caller and
never a copy of the review logic. Nothing here to keep in sync downstream.

## How it works

```
PR gets the `cursor-review` label
        │
        ▼
   ┌─────────┐   skip if: `skip-cursor-review` present,
   │  Gate   │   PR over the diff-size cap, or this exact
   └────┬────┘   commit was already reviewed (idempotent)
        │ should_run
        ▼
   ┌──────────────────── Panel (8 cells, in parallel) ────────────────────┐
   │                                                                       │
   │            adversarial (security/abuse)   edge-case (correctness)     │
   │   OpenAI          ▢                              ▢                    │
   │   Anthropic       ▢                              ▢                    │
   │   Google          ▢                              ▢                    │
   │   Moonshot        ▢                              ▢                    │
   │                                                                       │
   │   each cell: build prompt → run cursor-agent → extract-findings.py    │
   └───────────────────────────────┬───────────────────────────────────────┘
                                    │ 8 findings artifacts
                                    ▼
                            ┌───────────────┐   prompt-judge.md: drop dupes,
                            │  Judge model  │   false positives, and noise;
                            └───────┬───────┘   keep the actionable findings
                                    │ consolidated findings
                                    ▼
                            ┌───────────────┐   post-review.py: ONE PR review,
                            │  Post review  │   line-anchored inline comments,
                            └───────────────┘   severity badges + panel summary
```

Slack start/complete DMs to the triggerer are sent alongside (optional —
skipped if no Slack token is configured).

### The panel

| Lab | Model (Cursor catalog) |
|---|---|
| OpenAI | `gpt-5.3-codex-xhigh` |
| Anthropic | `claude-opus-4-8-thinking-xhigh` |
| Google | `gemini-3.1-pro` |
| Moonshot | `kimi-k2.5` |

Each model runs **two review types**:

- **adversarial** — security and reliability: input-validation gaps, auth
  bypasses, injection, race conditions, data leaks, DoS. See
  [`prompt-adversarial.md`](prompt-adversarial.md).
- **edge-case** — correctness and logic: nil derefs, off-by-one, unhandled
  inputs, broken error propagation, subtle behavioral bugs. See
  [`prompt-edge-case.md`](prompt-edge-case.md).

A single **judge** model ([`prompt-judge.md`](prompt-judge.md)) then adjudicates
all 8 cells' findings into the final review. If a cell fails (checkout, agent,
extraction), it still shows up in the panel summary tagged `error` rather than
silently vanishing — the review tells you what didn't run.

## What's in this directory

| File | Role |
|---|---|
| [`prompt-adversarial.md`](prompt-adversarial.md) | Prompt for the security/reliability review pass. |
| [`prompt-edge-case.md`](prompt-edge-case.md) | Prompt for the correctness/logic review pass. |
| [`prompt-judge.md`](prompt-judge.md) | Prompt the judge model uses to consolidate panel findings into one review. |
| [`extract-findings.py`](extract-findings.py) | Parses a cell's raw `cursor-agent` output into a normalized findings record. Always emits structured JSON — even on empty output or parse failure — so the consolidate step has uniform input. |
| [`post-review.py`](post-review.py) | Reads the judge's consolidated findings and posts **one** PR review with line-anchored inline comments and severity badges. |
| [`slack-notify.sh`](slack-notify.sh) | Sends the start/complete Slack DMs to the triggerer (no-ops without a token). |

## Adopt it in your repo

The review logic lives here; your repo adds only a **thin caller**. Pin `uses:`
to a full commit SHA (see the [top-level README](../../README.md#usage) for the
why and the versioning policy).

**1. Add the caller workflow** at `.github/workflows/ci-cursor-review.yml`:

```yaml
name: CI - Cursor Review
on:
  pull_request:
    types: [labeled, unlabeled]
permissions:
  contents: read
  pull-requests: write
concurrency:
  # Re-labeling cancels an in-flight run for the same PR + label.
  group: cursor-review-pr-${{ github.event.pull_request.number }}-${{ github.event.label.name }}
  cancel-in-progress: true
jobs:
  cursor-review:
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@<sha>  # v1
    with:
      # Exclude generated/vendored paths from BOTH the size cap and the diff.
      diff_excludes: >-
        :!**/package-lock.json
        :!**/*.generated.*
      # Pin the prompts/scripts to the same ref you pin `uses:` to.
      workflows_ref: <sha>
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
      SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}   # optional
```

**2. Configure secrets and (optionally) variables** on the calling repo:

| Kind | Name | Required | Purpose |
|---|---|---|---|
| Secret | `CURSOR_API_KEY` | **yes** | Bills the panel + judge `cursor-agent` calls. |
| Secret | `SLACK_BOT_TOKEN` | no | Enables start/complete DMs to the triggerer. |
| Variable | `CURSOR_REVIEW_DM_EMAIL_MAP` | no | Maps GitHub logins → emails for Slack DM lookup. |

**3. Trigger a review** by adding the `cursor-review` label to a PR. That's it.

### Optional: review-on-assignment

To fire the review when a PR is *assigned* to an opted-in reviewer (instead of
clicking a label), add the companion caller for
[`cursor-review-auto-label.yml`](../workflows/cursor-review-auto-label.yml). It
translates assignment into the trigger label using a GitHub App token (a label
applied by the default `GITHUB_TOKEN` does **not** start a new workflow run, so
an app token is required). The opt-in roster lives in the caller's
`vars.CURSOR_REVIEW_OPTED_IN_LOGINS`. See that workflow's header for the full
example and the `vars.APP_ID` / `CLOUD_CODE_BOT_PRIVATE_KEY` requirements.

## Configuration knobs

All optional, with defaults — pass them under `with:` in the caller. Full
descriptions live in the [workflow header](../workflows/cursor-review.yml).

| Input | Default | What it does |
|---|---|---|
| `judge_model` | `claude-opus-4-8-thinking-xhigh` | Model that consolidates panel findings. |
| `diff_size_cap` | `5000` | Max changed lines (after excludes); larger PRs are skipped. |
| `review_label` | `cursor-review` | Label whose addition triggers the review. |
| `diff_excludes` | lockfiles, `node_modules`, `dist`, `vendor`, minified/generated files | Pathspecs excluded from both the size count and the reviewed diff. |
| `workflows_ref` | `main` | Ref this directory's prompts/scripts are loaded from. Pin to your `uses:` SHA. |

### Escape hatches

- **Skip a PR**: add the `skip-cursor-review` label. It wins even if the trigger
  label is present. Removing it (while the trigger label is on) starts a run.
- **Re-review after changes**: push commits. The new HEAD SHA bypasses the
  idempotency check and a re-applied label runs a fresh panel.
- **Re-review unchanged content**: dismiss the existing review, then re-apply
  the label.

## Customizing the review

Because the prompts and scripts are the single source of truth, tuning the
review for everyone is a normal PR to this directory:

- Sharpen what the panel looks for → edit the `prompt-*.md` files.
- Change the consolidation bar (severity, dedup, what counts as actionable) →
  edit `prompt-judge.md`.
- Swap or add a lab → edit the matrix in
  [`cursor-review.yml`](../workflows/cursor-review.yml) and the lab list in
  `prompt-judge.md`.

Consumers pinned to a SHA pick up changes when they bump the SHA; consumers on a
floating major tag pick them up on the next run.
