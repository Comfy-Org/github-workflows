# bump-callers

The shared machinery that keeps SHA-pinned **callers** of this repo's reusable
workflows from rotting. When a reusable workflow is updated on `main`, it opens
a SHA-bump PR in every repo that pins a caller against it — so consumers move
forward automatically instead of silently drifting commits behind.

- **`bump-callers.sh`** — the one, fleet-agnostic bump script (parse the caller
  list, mask private repo names, rewrite the pin, keep one bump PR per caller
  current). It is the single source of truth; the workflow entrypoints are
  thin wrappers that only supply per-fleet parameters. A forked copy is how other
  shared machinery in the org has drifted — this stays one file on purpose.
  - **One open bump PR per (repo, fleet), updated in place.** The head branch is
    stable (`ci/bump-<tag>`, not SHA-stamped), so each bump rebuilds that branch
    from the caller's current default-branch tip (a clean single-commit "bump to
    @SHORT" diff) and, if a bump PR is already open, refreshes its title/body to
    the new SHA rather than opening another. A fresh PR is opened only when none
    is open (first bump, or the prior one merged/closed since the last run).
- **`tests/`** — a `bash` functional suite (stubs `gh`, no network), run by
  [`test-bump-callers.yml`](../workflows/test-bump-callers.yml) plus shellcheck.

## The fleets

| Entrypoint | Triggers on a change to | Caller variable | Seeded |
|---|---|---|---|
| [`bump-cursor-review-callers.yml`](../workflows/bump-cursor-review-callers.yml) | `cursor-review.yml` or `cursor-review/**` | `CURSOR_REVIEW_CALLERS` | non-empty (hard-fails if empty) |
| [`bump-agents-md-callers.yml`](../workflows/bump-agents-md-callers.yml) | `agents-md-integrity.yml` or `agents-md-integrity/**` | `AGENTS_MD_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-pr-size-callers.yml`](../workflows/bump-pr-size-callers.yml) | `pr-size.yml` or `scripts/check-pr-size/**` | `PR_SIZE_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-pr-risk-callers.yml`](../workflows/bump-pr-risk-callers.yml) | `pr-risk.yml` or `scripts/pr-risk/**` (minus its `tests/` and `README.md`, which no caller executes) | `PR_RISK_CALLERS` | empty `[]` allowed (grows as callers land) |
| [`bump-assign-reviewers-callers.yml`](../workflows/bump-assign-reviewers-callers.yml) | `assign-reviewers.yml` | `ASSIGN_REVIEWERS_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-groom-callers.yml`](../workflows/bump-groom-callers.yml) | `groom.yml` or `groom/**` | `GROOM_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-auto-label-callers.yml`](../workflows/bump-auto-label-callers.yml) | `cursor-review-auto-label.yml` | `AUTO_LABEL_CALLERS` | non-empty (hard-fails if empty) |

### Reusables with no fleet — deliberate, not an oversight

| Reusable | Callers | Why no fleet |
|---|---|---|
| `stale.yml` | 0 | Nothing to bump. Add a fleet when the first caller lands. |
| `assign-prs-to-author.yml` | 0 | Same. |
| `detect-unreviewed-merge.yml` | ~12 | **A real gap.** Its pins are bumped by hand. Deferred deliberately, not missed. |

A reusable that has callers but no fleet is the trap this whole directory exists
to prevent: the pins simply never move, so consumers drift behind indefinitely
and only find out when the caller and the reusable stop being compatible. That is
not hypothetical — the groom fleet omitted *its own* caller (`ci-groom.yml`) from
`GROOM_CALLERS`, the pin sat unchanged from the day it was written, and the caller
ended up failing at startup against a reusable it had drifted away from. Before
adding a caller anywhere, check that its fleet exists **and** that the repo is in
the variable; the second half is the one people skip.

They stay as thin entrypoints rather than one matrix because their triggers
differ: a `cursor-review.yml` change must not spuriously bump agents-md or
pr-size callers, and vice versa. Everything else (masking, the PR-per-caller
flow, the trailing-newline fix, the single-line PR body) lives once in
`bump-callers.sh`. Registering a new fleet is: add a thin entrypoint (copy an
existing one, swap the path filter + `VAR_NAME`/`TAG`/`WORKFLOW_FILE`/
`ALLOW_EMPTY`), seed its variable, and add a row to this table + the paths in
`test-bump-callers.yml`. **Then `workflow_dispatch` the new entrypoint once.**
Landing a fleet does not touch the reusable it watches, so its own merge matches
no path filter and fires no run — callers that were already stale when the fleet
was created stay stale until the reusable next changes. Every entrypoint carries
`workflow_dispatch` for exactly this.

The **groom** fleet is the one that most needs this: a groom caller pins the
reusable **twice** — the `uses:` SHA *and* the `workflows_ref:` input that loads
the finder/verifier/builder briefs plus the dedup ledger. Those must stay in
lock-step or a run executes one version's workflow against another version's
briefs. `bump-callers.sh`'s pin rewrite moves both (it matches the `uses:` line
and any bare `workflows_ref:` line), so the fleet cannot drift into that split
state through a hand-bump of only one. It also re-points the `# main @ <short>`
pin comment those callers carry — a comment still naming the old commit after the
pin moved is worse than no comment. **`pr-risk` callers have the same double-pin
shape** — `uses:` plus a `workflows_ref:` the reusable checks the grader, risk map
and label script out at — so the same rewrite covers them and the same
never-hand-bump-one-alone rule applies.

## The caller variables

This repo is **public** — the workflow files and Actions run logs are both
publicly viewable — and most callers are private, so caller names must never
appear in a committed file or in the logs. Each fleet's caller list lives in a
repo-level Actions **variable** (config, not a credential) as a JSON array of
`{"repo","file","label"}` objects (`label` optional). `bump-callers.sh`
`::add-mask::`es every repo name out of the run logs before echoing it.

> **Known gap.** Each entrypoint hands the roster to the script through the
> step's `env:`, and Actions prints a step's env block *before* the step runs —
> so the raw roster appears in the (public) log ahead of any masking. Closing it
> means fetching the variable at run time (`gh variable get`) and masking it
> before first use, which needs a token permission the fleets do not mint today.
> It is fleet-wide; no single entrypoint can fix it. Until then, assume the
> roster is public.

Adding/removing a caller needs **no public commit** — edit the variable:

```bash
gh variable set AGENTS_MD_CALLERS --repo Comfy-Org/github-workflows \
  --body "$(jq -c . callers.json)"
```

Keep the canonical `callers.json` in a private infra/ops repo so variable edits
have a reviewed source of truth (the org audit log records each edit).
