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
| [`bump-assign-reviewers-callers.yml`](../workflows/bump-assign-reviewers-callers.yml) | `assign-reviewers.yml` | `ASSIGN_REVIEWERS_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-groom-callers.yml`](../workflows/bump-groom-callers.yml) | `groom.yml` or `groom/**` | `GROOM_CALLERS` | empty `[]` (grows as callers land) |

They stay as thin entrypoints rather than one matrix because their triggers
differ: a `cursor-review.yml` change must not spuriously bump agents-md or
pr-size callers, and vice versa. Everything else (masking, the PR-per-caller
flow, the trailing-newline fix, the single-line PR body) lives once in
`bump-callers.sh`. Registering a new fleet is: add a thin entrypoint (copy an
existing one, swap the path filter + `VAR_NAME`/`TAG`/`WORKFLOW_FILE`/
`ALLOW_EMPTY`), seed its variable, and add a row to this table + the paths in
`test-bump-callers.yml`.

The **groom** fleet is the one that most needs this: a groom caller pins the
reusable **twice** — the `uses:` SHA *and* the `workflows_ref:` input that loads
the finder/verifier/builder briefs plus the dedup ledger. Those must stay in
lock-step or a run executes one version's workflow against another version's
briefs. `bump-callers.sh`'s pin rewrite moves both, so the fleet cannot drift
into that split state through a hand-bump of only one. It also re-points the
`# main @ <short>` pin comment those callers carry — a comment still naming the
old commit after the pin moved is worse than no comment.

## How the pin rewrite is scoped (and why it asserts afterwards)

The rewrite targets the **pin token**, not "any 40-hex on a line that mentions
`github-workflows`" (BE-4662). Two patterns, matched by position rather than by
what the ref *looks like*:

- `Comfy-Org/github-workflows…@<ref>` — the `uses:` pin. The owner/repo is matched
  **case-insensitively**, because GitHub resolves `uses:` that way and a caller
  written `comfy-org/…` is calling this repo; what follows the repo name must be
  the `/` of a path or the `@` of a ref, so a **sibling** repo whose name merely
  starts the same (`github-workflows-tools/action@v1`) is out of reach.
- `workflows_ref: <ref>` as a block-mapping key, optionally quoted — the input pin.

Whatever sits right after the token is the ref, so **any literal ref shape moves**
— full sha, short sha, or a tag like `v1`. That matters because a caller whose
`workflows_ref` is a tag used to be skipped by the old 40-hex rule while its
`uses:` pin moved: a green-looking bump PR on a caller that is now running one
version's workflow against another version's briefs. In the other direction, an
unrelated full SHA that merely *shares* a line with the words `github-workflows`
or `workflows_ref` is now unreachable, and a prose comment mentioning
`workflows_ref:` is left as prose.

Precision cuts both ways, though — a pin form the patterns don't know how to move
would be silently left behind. So before a rewritten file can be staged, the
script re-reads it with a deliberately **broader** reader (any non-whitespace
value sitting where a ref belongs, comments excluded) and **asserts every
github-workflows pin now equals the new SHA**. If one does not — today the one
known case is a `workflows_ref` fed by a `${{ … }}` expression, which is
intentionally never rewritten — it emits a `::warning::` naming the file and the
stale value and **fails that repo**, exactly as it does for a transient fetch
error. A partial bump is worse than no bump (BE-3896). An empty pin
(`workflows_ref: ""`) and a value the rewrite could only half-move (a `#` inside
the ref) fail the same way rather than reading back as clean.

Because that reader is the one place a false positive would block an otherwise
clean caller's bump on every run, it is bounded on both sides: comments are
dropped by YAML's own rule (a `#` preceded by whitespace, so a `#` *inside* a
value survives to be compared), and the `workflows_ref` key needs a real left
boundary, so a longer key that merely ends in it (`upstream_workflows_ref: v1`)
is not read as this repo's pin.

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
