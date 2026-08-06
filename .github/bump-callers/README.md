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
- **`preflight.sh`** — the staleness/decommission guard that runs *before* the
  bump script (see [Preflight](#preflight) below). Also one file on purpose: it
  was an inline copy in every entrypoint, and the copies drifted.
- **`tests/`** — `bash` functional suites (stubs `gh` / builds throwaway local
  repos; no network), run by
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

## Preflight

Before an entrypoint may bump anything it has to answer two questions: is this
run **stale** (has a later commit already touched the watched surface, so *that*
commit has its own run?), and has the watched surface been **decommissioned**
(deleted, so pinning callers to this SHA would break every one of them)?

`preflight.sh` is that guard. It used to be an inline copy in each
`bump-*-callers.yml`, and the copies drifted — several skipped on a bare tip
mismatch, which throws away the only run for a change and freezes every caller,
and the one that compared content forgot to re-point the pin at the verified tip.
The extracted script deliberately adopts the hardened semantics: exact-refname
tip parse (a branch literally named `foo/refs/heads/main` matches the ls-remote
pattern at component boundaries and must not be consumed), `FETCH_HEAD`
verification before any object is read out of it, deletion tested through the
`$WATCHED` **variable** rather than a second copy of the literal path, and the
re-point that pins callers to the verified tip instead of a stale `github.sha`.

| Input (env) | |
|---|---|
| `WATCHED` | **required** — repo-relative path of the watched reusable workflow (e.g. `.github/workflows/groom.yml`) |
| `WATCHED_ASSETS` | optional — the watched asset directory (e.g. `.github/groom`). Empty/unset means the fleet is single-path |
| `NEW_SHA` | the candidate SHA, normally `github.sha` |
| `GITHUB_SHA`, `GITHUB_OUTPUT` | provided by Actions |

Both watched paths are **literal paths, not the globs from the `paths:` filter** —
`.github/groom`, never `.github/groom/**` and never a trailing slash. A glob
resolves to nothing (`[[ -d '.github/groom/**' ]]` is false,
`git rev-parse 'HEAD:.github/groom/**'` is empty), so it would make every
comparison verify nothing and the fleet a permanent silent no-op. The script
rejects that shape up front rather than reporting it as a decommission, and it
likewise rejects a `NEW_SHA` that is not a full 40-character lowercase SHA (it is
emitted verbatim into `$GITHUB_OUTPUT`, so a newline in it injects output lines)
and a `HEAD` that is not `GITHUB_SHA` (a `ref:` override in the consuming
checkout would have it compare main against itself).

| Output (step output) | |
|---|---|
| `proceed` | `true` → run `bump-callers.sh`; `false` → stale or decommissioned, do nothing |
| `new_sha` | the SHA to pin — `NEW_SHA`, or the verified main tip when the run was re-pointed forward |

Both outputs are written on **every** exit-0 path. The script exits non-zero only
for an input it cannot trust (the shape checks above), a lookup it could not
perform (failed `ls-remote`, failed fetch, unresolvable `FETCH_HEAD`, a
`rev-parse` that *failed* rather than reporting absence), or an answer that
contradicts history (the **direction guard** below): none of those is evidence of
staleness, so it fails loudly rather than silently no-opping the fleet.

**The direction guard.** When main has moved, the fetched tip must *descend from*
this run's commit before anything is compared against it or pinned to it. If main
moved BACKWARDS — a force-push, a revert-reset, or a stale replica answering the
tip lookup — both outcomes are silently wrong: with the watched content differing
at the older commit the run reads as a stale re-run and exits green ("the newer
commit has its own run" — about an *older* commit), freezing every caller behind a
run that will never come; with the content identical, the re-point pins the whole
fleet BACKWARDS. So `git merge-base --is-ancestor` gates both, and a failure is an
`::error::`, not a skip.

It is measured **twice**, because descending from this run's commit is the weaker
half. `ls-remote` and the fetch are two round trips, and a rewind landing in that
window — or a stale replica answering the second one — can hand back a commit that
is older than the tip just reported yet still *ahead* of this run, which sails
through a HEAD-only check and gets compared and pinned anyway. So the tip
`ls-remote` reported must be an ancestor of the fetched one before that move is
logged as an advance, and the fetched one must be a descendant of this run's
commit before anything is read out of it.

Both need real history to answer: against a `--depth=1` graft `--is-ancestor`
returns false even for a legitimate forward move, so the fetch adds `--unshallow`
when the checkout is shallow (which `actions/checkout` makes it) — `--unshallow`
errors out on an already-complete clone, hence a probe rather than an
unconditional flag. The probe is `git rev-parse --is-shallow-repository`, not a
stat of `$(git rev-parse --git-dir)/shallow`: inside a **linked worktree** that
marker lives in the common git dir while `--git-dir` names the per-worktree one,
so the hand-rolled form false-negatives exactly there and silently skips the
deepening the guard is supposed to rest on.

**A multi-path fleet must pass `WATCHED_ASSETS`.** The re-point is only sound
because every entry in the fleet's `paths:` trigger is covered by the comparison
— for `agents-md-integrity` (`.github/agents-md-integrity/**`), `cursor-review`
(`.github/cursor-review/**`), `groom` (`.github/groom/**`) and `pr-size`
(`scripts/check-pr-size/**`) that includes the asset directory the reusable loads
its prompts/scripts/briefs from at run time. (`pr-risk` is multi-path too, but its
filter also carries `:(exclude)` entries that one `WATCHED_ASSETS` string cannot
express — see the note below.) Compare `WATCHED` alone on one of those and a
commit touching only the assets reads as "unchanged", so callers get pinned to a
tip whose other relevant content was never verified. Read the entrypoint's
`paths:` rather than trusting this list, and if you widen a fleet's path filter,
widen these inputs in the same change.

They must not be **wider** than the filter either, which is the direction an
excluding fleet gets wrong. A commit touching only an excluded path (pr-risk's
`scripts/pr-risk/tests`, its `README.md`) starts no run of its own, but it does
change the tree OID of an over-broad `WATCHED_ASSETS` — so this run reports "the
watched surface changed since", skips green as a stale re-run, and waits on a
later run that will never exist. An exclusion is a reason to narrow the inputs, or
to leave that fleet on its own guard; never to point `WATCHED_ASSETS` at the whole
directory.

Consumption is two steps — the guard, then the bump gated on its output:

```yaml
      - name: Preflight (staleness / decommission guard)
        id: preflight
        env:
          WATCHED: .github/workflows/groom.yml
          WATCHED_ASSETS: .github/groom   # omit for a single-path fleet
          NEW_SHA: ${{ github.sha }}
        run: bash .github/bump-callers/preflight.sh

      - name: Bump SHA in caller repos
        if: steps.preflight.outputs.proceed == 'true'
        env:
          GH_TOKEN: ${{ steps.token.outputs.token }}
          NEW_SHA: ${{ steps.preflight.outputs.new_sha }}
          # …VAR_NAME / TAG / WORKFLOW_FILE / CALLERS_JSON as before
        run: bash .github/bump-callers/bump-callers.sh
```

`new_sha` is a **step output**, not a `$GITHUB_ENV` export, and the consuming
step reads it through its own `env:` binding. That is deliberate: a step-level
`env: NEW_SHA:` takes precedence over the job environment, so a `$GITHUB_ENV`
write would be silently overridden by the very binding it is meant to correct.

> **The entrypoints still carry their inline copies.** Swapping them over to this
> script is a separate change. `bump-pr-risk-callers.yml` carried two checks this
> script did not, and **BE-6670 decided both** rather than leaving the swap to
> choose:
>
> - Its **is-ancestor check is adopted here, for every fleet** (BE-6675) — see
>   the direction guard above. It was never pr-risk-specific.
> - Its **`git rev-list` "did a later *commit* touch a watched path" test is
>   deliberately not ported.** It exists because pr-risk has no re-point, so a
>   land-then-revert (net content change of zero) makes that fleet call this run
>   the only one for the change and pin callers *backwards* to `github.sha`. The
>   re-point already answers that case by pinning the verified tip — which on a
>   land-then-revert is the revert commit, i.e. forward. Adding rev-list on top
>   would only skip a run whose content the tip still needs pinned. That settles
>   land-then-revert; it is not a claim that comparing objects expresses
>   everything a rev-list *pathspec* can. Swapping an excluding fleet across means
>   narrowing its inputs to what its filter really watches (above), not adding
>   rev-list back.

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
