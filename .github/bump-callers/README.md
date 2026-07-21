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

They stay as thin entrypoints rather than one matrix because their triggers
differ: a `cursor-review.yml` change must not spuriously bump agents-md or
pr-size callers, and vice versa. Everything else (masking, the PR-per-caller
flow, the trailing-newline fix, the single-line PR body) lives once in
`bump-callers.sh`. Registering a new fleet is: add a thin entrypoint (copy an
existing one, swap the path filter + `VAR_NAME`/`TAG`/`WORKFLOW_FILE`/
`ALLOW_EMPTY`), seed its variable, and add a row to this table + the paths in
`test-bump-callers.yml`.

## The caller variables

This repo is **public** — the workflow files and Actions run logs are both
publicly viewable — and most callers are private, so caller names must never
appear in a committed file or in the logs. Each fleet's caller list lives in a
repo-level Actions **variable** (config, not a credential) as a JSON array of
`{"repo","file","label"}` objects (`label` optional). `bump-callers.sh`
`::add-mask::`es every repo name out of the run logs before echoing it.

Adding/removing a caller needs **no public commit** — edit the variable:

```bash
gh variable set AGENTS_MD_CALLERS --repo Comfy-Org/github-workflows \
  --body "$(jq -c . callers.json)"
```

Keep the canonical `callers.json` in a private infra/ops repo so variable edits
have a reviewed source of truth (the org audit log records each edit).
