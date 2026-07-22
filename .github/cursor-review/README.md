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
PR gets `cursor-review` or `cursor-review-xl`
        │
        ▼
   ┌─────────┐   skip if: `skip-cursor-review` present,
   │  Gate   │   PR over the diff-size cap (unless XL), or this exact
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
| OpenAI | `gpt-5.6-sol-max` |
| Anthropic | `claude-opus-4-8-thinking-max` |
| Google | `gemini-3.1-pro` |
| Moonshot | `kimi-k2.7-code` |

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
| [`gate-unresolved.py`](gate-unresolved.py) | The opt-in blocking gate (`blocking: true`). Queries the PR's review threads and exits non-zero while any cursor-review finding thread is unresolved. |
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

**3. Trigger a review** by adding `cursor-review` to a PR. For an intentionally
large PR, add `cursor-review-xl` instead; it runs the same panel without the
diff-size cap. Create both labels once in each consumer repository.

### Large diffs: `cursor-review-xl`

`cursor-review-xl` is a standalone alternate trigger. It keeps the normal
diff exclusions and line-count reporting, but bypasses `diff_size_cap`. If both
review labels are present, XL mode wins.

Concurrency is owned by the reusable workflow: adding XL cancels an in-flight
standard panel for the same PR, while unrelated label changes cannot cancel a
review. Callers do not need their own `concurrency` block.

### Optional: review-on-assignment

To fire the review when a PR is *assigned* to an opted-in reviewer (instead of
clicking a label), add the companion caller for
[`cursor-review-auto-label.yml`](../workflows/cursor-review-auto-label.yml). It
translates assignment into the trigger label using a GitHub App token (a label
applied by the default `GITHUB_TOKEN` does **not** start a new workflow run, so
an app token is required). The opt-in roster lives in the caller's
`vars.CURSOR_REVIEW_OPTED_IN_LOGINS`. See that workflow's header for the full
example and the `vars.APP_ID` / `CLOUD_CODE_BOT_PRIVATE_KEY` requirements.

### Optional: make the review blocking (merge gate)

By default the review is **advisory** — it posts findings as PR review threads,
but an unresolved (red) review never blocks merge. Opt into a blocking gate by
passing `blocking: true`:

```yaml
jobs:
  cursor-review:
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@<sha>  # v1
    with:
      blocking: true
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
```

With `blocking: true`, a final **Blocking gate** job queries the PR's review
threads and **fails the check while any cursor-review finding thread is
unresolved**. Resolve the thread(s) — or push a fix and re-trigger the review —
and the gate goes green. It is idempotent: resolving threads and re-running the
check turns it green without a fresh panel.

> **This workflow cannot set branch protection.** A red check is visible but
> does not block merge on its own. To actually gate merges you must **also mark
> the `Blocking gate` check as a required status check** in the calling repo:
> *Settings → Branches → Branch protection rule* (or *Rulesets*) → **Require
> status checks to pass** → add **`Blocking gate`** (it appears once the
> workflow has run at least once with `blocking: true`). The check name is
> prefixed with your caller job id, e.g. `cursor-review / Blocking gate`.

Notes:

- `blocking: false` (the default, or simply not passing the input) is **exactly
  today's behavior** — no caller changes until it opts in.
- A thread is recognized as a cursor-review thread by its originating review's
  body marker, so the gate works whether the review posts as
  `github-actions[bot]` or under a dedicated `bot_app_id`.
- Outdated threads (their code changed since the finding was posted) don't
  block — a re-review re-posts anything still wrong as a fresh thread.

## Configuration knobs

All optional, with defaults — pass them under `with:` in the caller. Full
descriptions live in the [workflow header](../workflows/cursor-review.yml).

| Input | Default | What it does |
|---|---|---|
| `judge_model` | `claude-opus-4-8-thinking-max` | Model that consolidates panel findings. |
| `diff_size_cap` | `5000` | Max changed lines (after excludes); larger PRs are skipped. |
| `review_label` | `cursor-review` | Label whose addition triggers the review. |
| `xl_review_label` | `cursor-review-xl` | Alternate trigger that bypasses `diff_size_cap`; wins when both labels are present. |
| `diff_excludes` | lockfiles, `node_modules`, `dist`, `vendor`, minified/generated files | Pathspecs excluded from both the size count and the reviewed diff. |
| `workflows_ref` | `main` | Ref this directory's prompts/scripts are loaded from. Pin to your `uses:` SHA. |
| `bot_app_id` | `''` | Optional GitHub App ID; when set (with `BOT_APP_PRIVATE_KEY`), the review posts under that App's identity instead of `github-actions[bot]`. |
| `blocking` | `false` | Opt-in merge gate. `true` fails the **Blocking gate** check while any cursor-review finding thread is unresolved. See [Make the review blocking](#optional-make-the-review-blocking-merge-gate). |

### Escape hatches

- **Skip a PR**: add the `skip-cursor-review` label. It wins even if the trigger
  label is present. Removing it (while the trigger label is on) starts a run.
- **Review a large PR**: add `cursor-review-xl`; it bypasses only the line cap,
  while normal exclusions, idempotency, and failure handling still apply.
- **Re-review after changes**: push commits. The new HEAD SHA bypasses the
  idempotency check and re-applying the active review label runs a fresh panel.
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
