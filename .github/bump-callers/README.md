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

| Entrypoint | Triggers on a change to | Caller secret | Seeded |
|---|---|---|---|
| [`bump-cursor-review-callers.yml`](../workflows/bump-cursor-review-callers.yml) | `cursor-review.yml` or `cursor-review/**` | `CURSOR_REVIEW_CALLERS` | non-empty (hard-fails if empty) |
| [`bump-agents-md-callers.yml`](../workflows/bump-agents-md-callers.yml) | `agents-md-integrity.yml` or `agents-md-integrity/**` | `AGENTS_MD_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-pr-size-callers.yml`](../workflows/bump-pr-size-callers.yml) | `pr-size.yml` or `scripts/check-pr-size/**` | `PR_SIZE_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-pr-risk-callers.yml`](../workflows/bump-pr-risk-callers.yml) | `pr-risk.yml` or `scripts/pr-risk/**` (minus its `tests/` and `README.md`, which no caller executes) | `PR_RISK_CALLERS` | empty `[]` allowed (grows as callers land) |
| [`bump-assign-reviewers-callers.yml`](../workflows/bump-assign-reviewers-callers.yml) | `assign-reviewers.yml` | `ASSIGN_REVIEWERS_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-groom-callers.yml`](../workflows/bump-groom-callers.yml) | `groom.yml` or `groom/**` | `GROOM_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-auto-label-callers.yml`](../workflows/bump-auto-label-callers.yml) | `cursor-review-auto-label.yml` | `AUTO_LABEL_CALLERS` | non-empty (hard-fails if empty) |
| [`bump-detect-unreviewed-merge-callers.yml`](../workflows/bump-detect-unreviewed-merge-callers.yml) | `detect-unreviewed-merge.yml` | `DETECT_UNREVIEWED_MERGE_CALLERS` | **not yet seeded** — hard-fails until it is (see below) |

### Why the detect-unreviewed-merge roster is not seeded yet

Its 12 live callers are known and correctly wired (each pins a full 40-hex SHA
against this repo's path, i.e. exactly what the rewrite moves). It is unseeded
anyway — originally on purpose, and now only because seeding it is a separate,
deliberate step that has not happened yet.

The original reason was the run-log gap. Every roster reaches `bump-callers.sh`
through the step's `env:` block, and Actions prints that block — values and all —
before the script's `::add-mask::` can run. This repo is public, so while the
rosters were repo **variables**, each seeded fleet published its roster in a
world-readable log. Two of this fleet's callers are non-public repos that appear
in **no** other roster, so seeding would have published two names that were not
out yet — and a public log entry cannot be unpublished, while a red run can. So
the red run won.

**That blocker is gone** (BE-6472): every roster is a repo **secret** now, and
the runner masks a secret everywhere, that env dump included. Seeding this fleet
is unblocked and is tracked as the follow-on — do it deliberately, with
`gh secret set` from the canonical `callers.json`, never reflexively to turn the
red run green.

### Reusables with no fleet — deliberate, not an oversight

| Reusable | Callers | Why no fleet |
|---|---|---|
| `stale.yml` | 0 | Nothing to bump. Add a fleet when the first caller lands. |
| `assign-prs-to-author.yml` | 0 | Same. |

A reusable that has callers but no fleet is the trap this whole directory exists
to prevent: the pins simply never move, so consumers drift behind indefinitely
and only find out when the caller and the reusable stop being compatible. That is
not hypothetical — the groom fleet omitted *its own* caller (`ci-groom.yml`) from
`GROOM_CALLERS`, the pin sat unchanged from the day it was written, and the caller
ended up failing at startup against a reusable it had drifted away from. Before
adding a caller anywhere, check that its fleet exists **and** that the repo is in
its roster secret; the second half is the one people skip.

They stay as thin entrypoints rather than one matrix because their triggers
differ: a `cursor-review.yml` change must not spuriously bump agents-md or
pr-size callers, and vice versa. Everything else (masking, the PR-per-caller
flow, the trailing-newline fix, the single-line PR body) lives once in
`bump-callers.sh`. Registering a new fleet is: add a thin entrypoint (copy an
existing one, swap the path filter + `VAR_NAME`/`TAG`/`WORKFLOW_FILE`/
`ALLOW_EMPTY`), seed its roster secret, and add a row to this table + the paths
in `test-bump-callers.yml`. **Then `workflow_dispatch` the new entrypoint once.**
Landing a fleet does not touch the reusable it watches, so its own merge matches
no path filter and fires no run — callers that were already stale when the fleet
was created stay stale until the reusable next changes. Every entrypoint carries
`workflow_dispatch` for exactly this.

The **groom** fleet is the one that most needs this: a groom caller pins the
reusable **twice** — the `uses:` SHA *and* the `workflows_ref:` input that loads
the finder/verifier/builder briefs plus the dedup ledger. Those must stay in
lock-step or a run executes one version's workflow against another version's
briefs. `bump-callers.sh`'s pin rewrite moves both, so the fleet cannot drift
into that split state through a hand-bump of only one. It also re-points the
`# main @ <short>` pin comment those callers carry — a comment still naming the
old commit after the pin moved is worse than no comment. **`pr-risk` callers have
the same double-pin shape** — `uses:` plus a `workflows_ref:` the reusable checks
the grader, risk map and label script out at — so the same rewrite covers them
and the same never-hand-bump-one-alone rule applies.

A caller pinning **two** github-workflows reusables in the same file is a
special case: the `uses:` pin rewrite and the `# main @ <short>` comment rewrite
are both address-restricted to the line calling THIS fleet's `WORKFLOW_FILE`, so
a sibling fleet's pin and annotation are left untouched rather than stamped with
this fleet's SHA (BE-4523). The legacy `# github-workflows#NN` / already-converted
`github-workflows main (<short>)` markers name a SHA but not which reusable they
annotate, so they are refreshed only when the file is provably ours alone;
otherwise they are left as found and the run logs a warning. Inert for every
caller today (all call exactly one reusable); it exists so a caller that starts
calling two cannot be corrupted.

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

## The caller secrets

This repo is **public** — the workflow files and Actions run logs are both
publicly viewable — and most callers are private, so caller names must never
appear in a committed file or in the logs. Each fleet's caller list lives in a
repo-level Actions **secret** as a JSON array of `{"repo","file","label"}`
objects (`label` optional). `bump-callers.sh` `::add-mask::`es every repo name
out of the run logs before echoing it.

### Why a secret and not a variable (BE-6472)

These rosters are configuration, not credentials, so a variable is the intuitive
home — and that is what they were until BE-6472. The problem is *how* the roster
reaches the script: each entrypoint hands it over through the step's `env:`
block, and Actions prints a step's env block **before** the step runs, so the raw
value landed in this public repo's log ahead of any masking the script could
possibly do. A live run of one of these fleets rendered `GH_TOKEN: ***` directly
above an unmasked `CALLERS_JSON` in the same block — which is also the proof that
a secret fixes it: the runner masked the secret in the very dump that printed the
variable in full. (The run is cited in the ticket rather than here; this file is
public, and a pointer to a log still holding the old roster is not something to
publish.)

The obvious alternative — fetch the roster at run time with `gh variable get`
under a permission-narrowed app token — is **not implementable**:
`actions/create-github-app-token` has no `permission-variables` input at any
version, and the underlying `POST /app/installations/{id}/access_tokens` API has
no variables permission key at all. A narrowed token can never read a variable.

The cost of the move is **read-back**: there is no `gh secret get`, so you cannot
diff what a fleet holds against what you meant to set. Two things cover that:

- the canonical `callers.json` in a private infra/ops repo is the **sole** source
  of truth (it always should have been), and
- every run that gets past roster validation logs a line of the form
  `roster: <N> caller(s), sha256 <digest>` (a run that hard-fails on a missing or
  malformed roster says so in its error instead), which you reproduce from the
  canonical file with

  ```bash
  jq -cS . callers.json | sha256sum
  ```

  Equal digests mean the fleet ran exactly that roster. The digest is taken over
  the **canonical** (`jq -cS`) form, not the raw secret bytes, so pretty-printing,
  key order and a trailing newline cannot make the same roster fingerprint two
  ways; array order is preserved, since that is the order repos are bumped in.
  A fleet that no-ops on an empty roster (`ALLOW_EMPTY: true`) logs the line too,
  as `roster: 0 caller(s), sha256 n/a (roster empty or unset)` — there is no
  canonical form to hash, and the point of printing it anyway is that the run
  shape you most need to recognize is not the one with no audit line at all.

  What the digest gives an outside reader, stated precisely: a sha256 is not
  reversible, but it **is** a check function, so a guess can be tested against it
  offline. The preimage is the entire canonical array — every repo, its file path
  and label, and their order — so a confirmable guess means reconstructing the
  whole roster verbatim, not testing whether one repo is a member; the count is
  the only bound on that space. Closing even this residual means a keyed HMAC and
  an operator-held fingerprint key (which the reproduction command would then
  need too) — deliberately not done, and worth revisiting only if a roster's
  contents ever become guessable in bulk.

Adding/removing a caller still needs **no public commit** — set the secret:

```bash
jq -c . callers.json | gh secret set AGENTS_MD_CALLERS --repo Comfy-Org/github-workflows
```

Keep the canonical `callers.json` in a private infra/ops repo so roster edits
have a reviewed source of truth (the org audit log records each edit).

**Finish the cutover by deleting the old variables.** A `*_CALLERS` variable left
behind after its secret is seeded still holds the private roster in this repo's
Actions config, readable by anything with variables-read access — the pre-BE-6472
copy of the exact data the move exists to hide. Once a fleet's first post-merge
run has logged the expected count and digest:

```bash
gh variable delete AGENTS_MD_CALLERS --repo Comfy-Org/github-workflows
```

Do that for every migrated fleet. Delete only **after** the run confirms the
secret is good — the variable is the rollback.

The `VAR_NAME` env key the entrypoints pass keeps its historical name even though
it now names a secret; it exists only to name the roster in an error message.
