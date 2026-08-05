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
| [`bump-detect-unreviewed-merge-callers.yml`](../workflows/bump-detect-unreviewed-merge-callers.yml) | `detect-unreviewed-merge.yml` | `DETECT_UNREVIEWED_MERGE_CALLERS` | **not yet seeded** — hard-fails until it is (see below) |

### Why the detect-unreviewed-merge roster is not seeded yet

Its 12 live callers are known and correctly wired (each pins a full 40-hex SHA
against this repo's path, i.e. exactly what the rewrite moves). It is unseeded
anyway, on purpose.

Every roster reaches `bump-callers.sh` through the step's `env:` block, and
Actions prints that block — values and all — before the script's `::add-mask::`
can run. This repo is public, so each seeded fleet already publishes its roster
in a world-readable log. That is the known gap documented in every `bump-*`
header. The difference here is that two of this fleet's callers are non-public
repos that appear in **no** already-seeded roster, so seeding would publish two
names that are not out yet — and a public log entry cannot be unpublished.

So the trade is: seed now and take an irreversible disclosure, or leave it
unseeded and take a red run. The red run is reversible and is already the
designed behaviour for an empty roster, so it wins. Seed the variable as the
immediate follow-on to the masking fix — never as a way to turn that red run
green.

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

The ref pattern stops at whitespace *and* at YAML's flow-style delimiters (`,`,
`}`), so the ref in `{uses: …@v1, secrets: inherit}` does not swallow the mapping's
comma. Flow style is not rewritten — it is failed, by the assertion below — but it
is never silently *corrupted*.

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

### Entry format — validated, and why

Every field reaches a privileged sink: `repo` is interpolated into each
`gh api repos/<repo>/…` write and into `gh pr create --repo`, `file` becomes the
path committed into the caller's tree, and `label` is passed to
`gh pr create --label`. They run under an org-wide app token (`owner: Comfy-Org`,
contents + pull-requests + issues write, no `repositories:` narrowing), and the
roster is a **variable** — editable outside code review. So `bump-callers.sh`
constrains the values before it makes a single API call:

| Field | Rule |
|---|---|
| `repo` | must match `^Comfy-Org/[A-Za-z0-9._-]+$` (the owner case-insensitively, as GitHub itself resolves it), and the name may not consist *only* of dots — `\A[.]+\z`, so `..` and `...` alike (a dot-leading or dot-bearing name such as `.github` or `a.b.c` is fine; an all-dots name is a path segment, not a repo) |
| `file` | must match `^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$` (the class excludes `/`, so `../` traversal cannot appear) |
| `label` | optional; when present must be a string containing no `\|`, no comma, and no control character |

Each pattern is anchored `\A…\z`, not `^…$`: jq matches with Oniguruma, where `$`
also matches *before a trailing newline*, so `"Comfy-Org/legit\n"` would otherwise
pass and then split into two tuples — one of them never validated, with an empty
`repo`.

A violation is a **hard fail before the fan-out**, reported as the entry's
zero-based **index** and the rule it broke — never the value, because masking has
not been applied at that point and this repo's run logs are public. The `label`
rule is not cosmetic. Entries are carried internally as `repo|file|label|wire_bot`
tuples, so a pipe-bearing label would truncate and bleed its tail into the
`wire_bot` field; a control character is dropped or mangled somewhere between
`jq` and the flag (bash's `read` silently discards a NUL); and `--label` is a
cobra StringSlice, which CSV-splits, so `ci,do-not-merge` would quietly apply a
second, potentially blocking label the entry does not appear to name. Spaces,
`:` and `/` are all still fine — this bars what is structurally unsafe, not what
GitHub disallows.

The owner is normalised to `Comfy-Org` once validated, and repos are grouped
case-insensitively. Accepting `comfy-org/x` without folding it would make two
spellings of one repo into two bump runs against it, the second force-moving the
shared bump branch off the first's commit — a green run shipping a partial bump.

### Un-bumpable entries fail the run

Being in the variable is necessary, not sufficient. The bumper can only move a
pin it can *find*, and a file carrying no pin this fleet can address rewrites to
itself — which the content-equality check then reports as the reassuring
`already at <short> — skipping`. That is how a wrong roster entry drifts forever
behind a green run. Each caller file is now checked for such a pin *before* the
rewrite; if there is none, the run warns per file and then **fails** with an
aggregate error naming how many caller files were affected. Three shapes trip it:

- the file **does not exist** on the caller's default branch (a renamed or
  typo'd path) — previously a silent `not found — skipping`;
- the file carries no `uses:` pin of `Comfy-Org/github-workflows` at all (wrong
  file, or not a caller);
- its only `github-workflows` `uses:` names a **sibling** fleet's reusable, so
  this fleet has nothing to move — the stale entry, not the file, is the bug.

A `uses:` pin is required in all three cases: a bare `workflows_ref:` never
rescues a file. That input carries no workflow name, so it cannot vouch for
*this* fleet, and admitting it would let the run stamp this fleet's SHA onto a
sibling caller's assets ref. Every caller of every seeded fleet carries a `uses:`
pin today, so nothing legitimate is caught by this.

"`uses:` pin" is meant literally: the check is anchored to the `uses:` key
(optionally quoted value), not merely to a `Comfy-Org/github-workflows@<ref>`
token somewhere on the line. A `run:` step that curls this repo, or a repo@ref
passed as an input to some *other* action, is not a caller — and since the pin
rewrite keys on the same token, admitting one would mean opening a bump PR
against a file that never called us.

What does **not** trip it is a pin that is merely not a full SHA. The rewrite
matches a ref by position rather than shape, so a caller on `@v1` is *self-healed*
to the new SHA — dragging floating pins back onto immutable ones is the point of
the fleet, not an error. Nor does a pin the rewrite knows about but cannot move
(a `uses:` ref fed by a `${{ … }}` expression): that is admitted here and then
fails loudly in the post-rewrite assertion, which is the check that owns it. The
failure lands after every caller has been processed,
so one bad entry never blocks the rest of the fleet's bumps; what it refuses to do
is report success.
