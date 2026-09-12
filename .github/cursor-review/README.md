# Cursor Review — multi-model PR review panel

Label-triggered code review that runs a **panel of frontier models from three
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
   ┌──────────────────── Panel (6 cells, in parallel) ────────────────────┐
   │                                                                       │
   │            adversarial (security/abuse)   edge-case (correctness)     │
   │   OpenAI          ▢                              ▢                    │
   │   Anthropic       ▢                              ▢                    │
   │   Moonshot        ▢                              ▢                    │
   │                                                                       │
   │   each cell: cursor-agent records findings through a stdio MCP tool   │
   └───────────────────────────────┬───────────────────────────────────────┘
                                    │ 6 findings artifacts
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
skipped if no Slack token is configured). A skip for the diff-size cap is
announced on the PR rather than passing for a clean review — see [Over the
diff-size cap](#over-the-diff-size-cap). With `blocking: true` a final
**Blocking gate** job follows Post review and fails while any posted finding
thread is unresolved — see
[Optional: make the review blocking](#optional-make-the-review-blocking).

**Post review is its own job, and that is a security boundary.** No job both
checks out PR code and holds a write-scoped credential. Every job that checks out
PR code and runs `cursor-agent` over it — every panel cell and the judge's
`Consolidate panel` — holds `contents: read` and nothing else. They hand their
result to `Post review` as an artifact; that job checks out no PR code, loads
only this directory's scripts from the pinned `workflows_ref`, and carries
`pull-requests: write` plus the review bot's App-key mint (as does the
over-the-cap comment job, which likewise checks out nothing).
The panel and the judge run `--print --trust` with no `--sandbox`, and
print mode can use write and shell tools — so a model steered by a malicious diff
could rewrite the assets checkout or a downloaded action *inside its own job*. On
a fresh runner with a fresh pinned checkout there is nothing tampered left for the
minted token to meet. `tests/test_workflow_job_isolation.py` pins the property, and
[`pr-size.yml`](../workflows/pr-size.yml) uses the identical split for its comment job.

### The prior-review ledger and the repeat policy

With `ledger_prior_review` on (the default), [`build-ledger.py`](build-ledger.py) rebuilds what earlier rounds raised on this PR — each finding, its thread, and the author's replies — and splices it into the panel and judge prompts as untrusted DATA. The rule it enforces is that only an **answered** finding costs a repeat slot: a finding the author or a maintainer replied to may be raised again only if the judge emits `repeat_of` (that thread's permalink) and `repeat_round`, and at most `REPEAT_CAP` such re-raises survive per review, so a round can never be all re-litigation. A finding nobody answered is free to raise again.

A finding that could not be anchored to a line the reviewed diff carries is **demoted** to the review body, and one that lost its whole review to a failed POST is delivered as prose — neither gets a thread, so neither can be answered and neither costs a slot. But a demoted finding may itself have been a *re-raise*, and that lineage used to disappear with the trailer that was stripped out of its recovered body: the next round saw a fresh, thread-less, cap-exempt finding, so one demoted hop made every later re-raise of the same finding free. The sentinel now carries the ancestor's URL as a field, and the ledger resolves it against the PR's own comments: the trailing `discussion_r<id>` must name a **root** comment of one of *our* consolidated reviews on this PR, so a hallucinated id resolves to nothing. Only that id is read — the owner/repo/PR-number half of the URL is shape-checked but never compared — so a link naming another repo resolves whenever its id matches one of our roots; what the entry renders is always the ancestor's *own* permalink, never the relayed string, and an ancestor with no permalink yields no lineage. The round is recovered the same way, from the review that ancestor belongs to, rather than from the payload. When it resolves, the entry renders `re_raise_of: <url> (round N; ancestor_answers=<k>)` — a key deliberately distinct from the `answers_from_author_or_maintainer` on the entry's own `thread:` line, since the two describe different findings — plus a `re_raise_answer:` excerpt of the ancestor's most recent answering reply, and the repeat rule is applied to that **ancestor's** answer state: with `k >= 1` a further re-raise must carry `repeat_of` and costs a slot, and with `k == 0` nothing changes. Resolution runs over every consolidated review before the round cap, so the ancestor is still found after its own round has aged out of the ledger. A claim that does *not* resolve — a dismissed review, a deleted comment — is not silently rebuilt as a fresh cap-exempt finding: the entry says the lineage is unverified and withholds the exemption.

A `repeat_of` is adjudicated on two independent layers, and neither is the other's backstop. The **writer** ([`post-review.py`](post-review.py)) checks SHAPE — one anchored GitHub discussion permalink under 512 characters, nothing else, applied to the rendered trailer and the structural field alike — and, given `--ledger`, MEMBERSHIP: the URL must be one the ledger the judge was actually shown carried, i.e. an *anchored* entry's `discussion_url` or the `repeat_of` lineage a *demoted* re-raise entry renders on its `re_raise_of:` line, mirroring the gates the render itself applies. The **reader** ([`build-ledger.py`](build-ledger.py)) separately resolves the trailing comment id against this PR's own consolidated-review roots, as above. Membership catches precisely what resolution cannot: an id that resolves but was never SHOWN — aged past the round cap, dropped by the byte cap, or never rendered — which the judge therefore had no basis to cite. A URL failing either check is dropped whole — no trailer, no sentinel field, nothing carried into the next round — while the judge's *declaration* that the finding is a re-raise still spends its `REPEAT_CAP` slot. That asymmetry is deliberate: refunding the slot would make a link we reject strictly cheaper than an honest one, so a judge could turn a whole round into uncapped re-litigation simply by citing threads it was never shown. Under the cap a bad link costs the finding its lineage and nothing else. It is only when that declaration is the one that overruns `REPEAT_CAP` that the finding is cut whole — exactly as an honest over-cap re-raise is, since the cap counts *declarations* and so by construction cannot tell the two apart. That is the asymmetry's price, not a second penalty: a refused link never costs a finding its place while the round has slots left. An `empty`, `disabled` or `unknown` ledger carries `entries: []`, and the judge prompt permits `repeat_of` only inside a ledger block, so on those runs the shown set is legitimately empty and every `repeat_of` is dropped. A ledger that cannot be read at all is the one case that must NOT drop anything — no path, no file, unreadable, not an object, no `entries` list — so it degrades to shape-only, exactly as before, the reader's half still holds, and the run carries a `::warning::` annotation so a silently disabled guard is visible rather than buried in a step log. The judge's and the poster's ledger downloads fail independently, and only the **judge's** decides what was shown: when that one fails the workflow passes `--no-judge-ledger` and the shown set is empty, because the judge prompt was spliced an empty ledger block however intact this job's own copy of `ledger.json` may be.

Those per-entry lines are the ledger's own structure, so imported prose must not be able to write one. An entry's fields sit at a two-space indent, and a finding body or an author reply keeps its line breaks — so every line of quoted prose *after its first* is prefixed `  | ` (a blank line renders as a bare `  |`, since no rendered line carries trailing whitespace), and the block header tells both audiences that a two-space line without `|` is a field this workflow wrote. The entry HEADER line takes the other half of the same contract: a `path` — which git permits a line break inside, and which a thread-derived entry takes straight from the review comment — is flattened to one line before it is interpolated there. A reply from any GitHub account that contains `\n  thread: … answers_from_author_or_maintainer=1` or `\n  discussion_url: <url>` therefore renders as visibly quoted text rather than as a field the judge would follow to grant itself a repeat slot, or a URL `post-review.py` would publish unvalidated. The prose is still shown in full, and single-line prose renders exactly as it did before.

### The panel

| Lab | Model (Cursor catalog) |
|---|---|
| OpenAI | `gpt-5.6-sol-max` |
| Anthropic | `claude-opus-5-thinking-max` |
| Moonshot | `kimi-k3-high` |

Gemini 3.1 Pro was dropped on 2026-08-27 after a spend review: across 1,450
judge-kept findings its two cells were the sole raiser of 2.5% of findings and
3 of 213 critical/high ones, at ~9% of every run's cost. Kimi moved from `-max`
to `-high` and the judge from `-max` to `-xhigh` in the same review. Callers can
override the panel list with the `panel_models` input (see below).

Each model runs **two review types**:

- **adversarial** — security and reliability: input-validation gaps, auth
  bypasses, injection, race conditions, data leaks, DoS. See
  [`prompt-adversarial.md`](prompt-adversarial.md).
- **edge-case** — correctness and logic: nil derefs, off-by-one, unhandled
  inputs, broken error propagation, subtle behavioral bugs. See
  [`prompt-edge-case.md`](prompt-edge-case.md).

A single **judge** model ([`prompt-judge.md`](prompt-judge.md)) then adjudicates
all cells' findings and submits the final review through the same slim stdio
MCP server. Model prose is never parsed for results: tool schemas validate the
records before writing them, which removes formatting drift, markdown fences,
truncated JSON, and reformat retries from the result path. If a cell fails
(checkout, agent, or tool submission), it still shows up in the panel summary
tagged `error` rather than silently vanishing.

## What's in this directory

| File | Role |
|---|---|
| [`prompt-adversarial.md`](prompt-adversarial.md) | Prompt for the security/reliability review pass. |
| [`prompt-edge-case.md`](prompt-edge-case.md) | Prompt for the correctness/logic review pass. |
| [`prompt-judge.md`](prompt-judge.md) | Prompt the judge model uses to consolidate panel findings into one review. |
| [`review-output-mcp.py`](review-output-mcp.py) | Dependency-free stdio MCP server. Reviewers record individual findings and finish; the judge submits the final findings array. It validates and atomically writes the normalized records consumed by later jobs. |
| [`post-review.py`](post-review.py) | Reads the judge's consolidated findings and posts **one** PR review with line-anchored inline comments and severity badges. |
| [`gate-unresolved.py`](gate-unresolved.py) | The opt-in blocking gate (`blocking: true` → the **Blocking gate** job): queries the PR's review threads and exits non-zero while any cursor-review finding thread is unresolved and non-outdated. Dropped from `cursor-review.yml` by accident in #31 and restored by BE-4691 — see [the blocking section](#optional-make-the-review-blocking). Double-billed: [`build-ledger.py`](build-ledger.py) also imports it for `CONSOLIDATED_MARKER`, the paging `reviewThreads` GraphQL query and the `iter_threads` / `is_cursor_thread` helpers, so the gate and the ledger can never disagree about which threads are ours. |
| [`slack-notify.sh`](slack-notify.sh) | Sends the start/complete Slack DMs to the triggerer (no-ops without a token). |
| [`install-cursor-cli.sh`](install-cursor-cli.sh) | Installs the Cursor agent CLI from the versioned, sha256-pinned release artifact — not `curl cursor.com/install \| bash`. Used by all three CLI-using jobs; the pin (`CURSOR_CLI_VERSION` / `CURSOR_CLI_SHA256`) lives in `cursor-review.yml`'s top-level `env:`. |
| [`build-ledger.py`](build-ledger.py) | Builds the **prior-review ledger** — what earlier rounds raised on this PR and how the author answered — and splices it into the panel/judge prompts. Untrusted prose is defanged twice over: fence-opening lines are rewritten, and continuation lines of quoted prose are prefixed ` |` so no imported text can sit at a field's indent and forge one. Also the prompt splicer, so the no-ledger path is byte-identical to the pre-ledger prompt. |
| [`fence-diff.py`](fence-diff.py) | Wraps the reviewed diff (plus the incremental hunks, and the judge's panel-findings block) in `=== BEGIN/END DIFF <nonce> ===` fences. The diff is attacker-authored PR bytes, so static literal fences are not a control; the nonce is what a PR cannot forge. Each prompt-build step mints its OWN nonce (`mint`), into a shell variable rather than a step `env:` or job output — Actions dumps a step's env map into the public run log, and a per-prompt value means a leak in one job cannot forge a fence in another. Copies the body through **byte for byte** — it never defangs or normalizes the payload. |
| [`catalog-drift.py`](catalog-drift.py) | Backs the weekly catalog-drift check. Extracts the pins from `cursor-review.yml`, diffs them against raw `cursor-agent models` output, and renders the sticky issue title + body (delisted pins, pins marked NO-ZDR, unpinned same-lab ids, catalog ids from unpinned families, stale audit date). Reports only — it never edits a pin. |

## Adopt it in your repo

The review logic lives here; your repo adds only a **thin caller**. Pin `uses:`
to a full commit SHA (see the [top-level README](../../README.md#pinning) for the
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
      # Repo-specific generated/vendored paths, excluded from BOTH the size cap
      # and the diff. Base-ref `linguist-generated` files, the Go codegen marker
      # and eight common lockfile base names (go.sum, go.work.sum,
      # package-lock.json, pnpm-lock.yaml, yarn.lock, Cargo.lock, poetry.lock,
      # uv.lock) are built into the classifier — you never list THOSE. Any other
      # lockfile (Gemfile.lock, composer.lock, Pipfile.lock, bun.lock,
      # flake.lock, gradle.lockfile) is NOT built in — add it to
      # `extra_lockfiles`. Setting this input REPLACES the default list, so the
      # defaults are re-stated verbatim below and the repo's own paths appended.
      # Path shape is load-bearing in BOTH directions: a pattern with no `/`
      # matches the base name only, while a pattern WITH a `/` is anchored to
      # the whole repo-relative path unless it starts with `**/`. So
      # `data/object_info.json.gz` below matches only the ROOT-level file — use
      # `**/data/object_info.json.gz` to catch it at any depth.
      extra_generated_globs: >-
        **/node_modules/**
        **/dist/**
        **/vendor/**
        **/*.generated.*
        **/*.min.js
        **/*.min.css
        data/object_info.json.gz
        **/*.snap
      # REQUIRED — the same SHA as the `uses:` pin above.
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

### Optional: make the review blocking

By default the review is **advisory**: it posts findings as PR review threads,
and an unresolved (red) review never blocks merge. Passing `blocking: true` adds
a fail-closed **Blocking gate** job that fails while the PR has unresolved,
non-outdated cursor-review finding threads — resolve every thread, or push a fix
that outdates them, and it passes. (Shipped in
[#16](https://github.com/Comfy-Org/github-workflows/pull/16) (BE-1891), dropped
by accident in [#31](https://github.com/Comfy-Org/github-workflows/pull/31),
restored by BE-4691.)

The input alone only turns the check red — a workflow cannot set branch
protection. To block the merge, ALSO mark `<caller job id> / Blocking gate` as a
required status check in the caller repo's branch-protection / ruleset settings,
and widen the caller's triggers first so pushes and thread resolutions re-report
the check — the trigger shape, and the rest of the semantics (what waives the
gate, what doesn't, who can resolve), live in
[the setup guide's blocking-gate gotchas](../../docs/callers/cursor-review.md#blocking-gate-gotchas).

Marking `… / Consolidate panel` required is **not** a substitute: GitHub counts
a skipped required check as passing, and that job skips whenever no review runs
(no trigger label, dedupe hit, diff over `diff_size_cap`, fork PR), so the check
would go green in precisely the cases a gate is meant to catch. The Blocking
gate closes that hole by running on every delivered event when `blocking` is on
— its verdict is a live thread-state query, never a skip.

An empty thread query is not by itself a pass, either. The gate's fail-closed
guards hold the check red when the trigger job failed, when the pipeline broke,
when post-review exited zero without actually delivering a review to the PR (a
read-only token, an error review, an all-cells-failed round), when every finding
landed in the review body with no thread, when the diff was over
`diff_size_cap`, and when the run was cancelled — because each of those leaves
zero threads for reasons that have nothing to do with the PR being clean. The
full list, and the two things the gate still cannot promise, are in
[the setup guide](../../docs/callers/cursor-review.md#blocking-gate-gotchas).

## Configuration knobs

All optional except `workflows_ref` (required, no default) — pass them under
`with:` in the caller. Full descriptions live in the
[workflow header](../workflows/cursor-review.yml).

| Input | Default | What it does |
|---|---|---|
| `judge_model` | `claude-opus-5-thinking-xhigh` | Model that consolidates panel findings. |
| `panel_models` | `''` (built-in list) | JSON array of Cursor model ids that **replaces** the panel list; each still runs both review types and is validated against the live catalog by preflight. For per-repo experiments (e.g. `-xhigh` vs `-max` tiers). |
| `skip_bot_branch_prefixes` | `ci/bump- chore/refresh- auto/refresh-` | Skip the panel when the PR author is a Bot **and** the head branch starts with one of these prefixes (machine pin bumps / catalog refreshes). `''` reviews every bot PR. |
| `diff_size_cap` | `5000` | Max counted changed lines (after generated-file exclusion and comment discounting); larger PRs are skipped. |
| `ignore_comments` | `true` | Discount blank/comment-only lines from the size count (count-only; the panel still sees them). |
| `review_label` | `cursor-review` | Label whose addition triggers the review. |
| `extra_generated_globs` | `**/node_modules/**`<br>`**/dist/**`<br>`**/vendor/**`<br>`**/*.generated.*`<br>`**/*.min.js`<br>`**/*.min.css` | Extra globs the shared `check-pr-size` classifier treats as generated — excluded from BOTH the size count and the reviewed diff. Setting it **replaces** the defaults; re-state them verbatim. Path shape is load-bearing both ways: a pattern with no `/` matches only the base name (bare `node_modules` excludes nothing under the directory), while a pattern **with** a `/` is anchored to the full repo-relative path unless it opens with `**/` (`data/gen.json` misses `pkg/x/data/gen.json`). `.claude` is deliberately absent — see [the setup guide](../../docs/callers/cursor-review.md). |
| `extra_lockfiles` | `''` | Extra lockfile **base names** for the classifier, on top of its built-ins (`go.sum`, `go.work.sum`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`, `uv.lock`). Anything else — `Gemfile.lock`, `composer.lock`, `Pipfile.lock`, `bun.lock`, `flake.lock`, `gradle.lockfile` — is **not** built in and must be listed here. A path (anything containing `/`) is rejected. |
| `diff_excludes` | `''` | Pathspecs excluded from the reviewed diff ONLY (not the size count) — back-compat escape hatch; prefer `extra_generated_globs`. Entries need pathspec-magic (`:!…`); a plain path excludes nothing. One caveat before you empty it: if `check-pr-size` itself fails, the run degrades to rebuilding the patch as `git diff "$BASE...$HEAD" -- . $DIFF_EXCLUDES`, where **only `diff_excludes` applies** and the classifier globs are not consulted — so on a degraded run a caller that moved everything to `extra_generated_globs` feeds its vendored/minified trees to every panel cell plus the judge. Keeping the heaviest trees listed in both inputs is the belt-and-braces option. |
| `workflows_ref` | **required** (no default) | Ref this directory's prompts/scripts are loaded from. Must be the same commit SHA as your `uses:` pin — omit it and the run fails fast, because pinning `uses:` while loading scripts from a mutable branch defeats the pin. |
| `bot_app_id` | `''` | Optional GitHub App ID; when set (with `BOT_APP_PRIVATE_KEY`), the review posts under that App's identity instead of `github-actions[bot]`. |
| `ledger_prior_review` | `true` | Give each round the prior rounds' findings + author replies, so a refuted or deferred finding is not re-litigated. |
| `run_without_label` | `false` | Run on plain PR events instead of requiring the trigger label. Also requires widening the caller's `types:` — see [the setup guide](../../docs/callers/cursor-review.md). |
| `blocking` | `false` | Adds the fail-closed **Blocking gate** check: red while any cursor-review finding thread is unresolved and non-outdated, and red when the round that should have produced those threads did not land (including an over-cap skip). Blocking the merge additionally requires marking that check required in the caller's ruleset — see [the blocking section above](#optional-make-the-review-blocking). |

### Over the diff-size cap

A PR whose counted diff exceeds `diff_size_cap` gets **no review panel**, and
that skip is not a failure — the run is green either way. So it announces itself
rather than passing for a clean review: the *Diff size check* job emits a
`::warning::` annotation and a step-summary block naming the counted total and
the cap (both credential-free, so they reach Dependabot PRs, whose runs can't
read Actions secrets), and a separate `over-cap-comment` job upserts one sticky
PR comment saying no panel ran. Get the PR under the cap and re-trigger, and
that same comment flips to ✅ instead of stacking a second one; it never posts on
a PR that was under the cap all along. Neither half reaches a **fork** PR — the
gate skips a cross-repo head before the size check runs at all, so a fork PR is
skipped for being a fork, not for its size.

The comment posts as the bot app when one is configured (`bot_app_id` +
`BOT_APP_PRIVATE_KEY`) and as `github-actions[bot]` otherwise, so it works in the
default configuration; the sticky finder matches both logins, so switching
identities updates the existing comment rather than posting a second one. If the
API write fails the job degrades to the annotation + summary and logs why. Mint
and upsert are both `continue-on-error`: the size verdict lives in the
`diff-size` job, so the comment path can never redden a run.

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
