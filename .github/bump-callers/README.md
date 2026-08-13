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

| Entrypoint | Triggers on a change to | Caller secret | Seeded |
|---|---|---|---|
| [`bump-cursor-review-callers.yml`](../workflows/bump-cursor-review-callers.yml) | `cursor-review.yml`, `cursor-review/**` or `scripts/check-pr-size/**` (minus its `*_test.go`, which no caller executes) | `CURSOR_REVIEW_CALLERS` | non-empty (hard-fails if empty) |
| [`bump-agents-md-callers.yml`](../workflows/bump-agents-md-callers.yml) | `agents-md-integrity.yml` or `agents-md-integrity/**` | `AGENTS_MD_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-pr-size-callers.yml`](../workflows/bump-pr-size-callers.yml) | `pr-size.yml` or `scripts/check-pr-size/**` (minus its `*_test.go`, which no caller executes) | `PR_SIZE_CALLERS` | empty `[]` (grows as callers land) |
| [`bump-pr-risk-callers.yml`](../workflows/bump-pr-risk-callers.yml) | `pr-risk.yml` or `scripts/pr-risk/**` (minus its `tests/` and `README.md`, which no caller executes) | `PR_RISK_CALLERS` | non-empty (hard-fails if empty) |
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

Being in the variable is necessary, not sufficient, in two different ways:

- **A pin's SHAPE doesn't matter — the rewrite self-heals it.** The
  substitution is anchored to the pin token, not to 40-hex-ness (BE-4662), so a
  `uses:` or `workflows_ref:` pinned to a placeholder, a tag, a branch, or a
  short SHA all move to `NEW_SHA` on the next bump regardless of what they
  carried before. A shape the rewrite genuinely cannot move — today, only a
  `workflows_ref` fed by a `${{ … }}` expression — is asserted against
  post-rewrite and **fails the run** rather than shipping a half-bumped caller
  (a partial bump is worse than no bump). That assertion is what actually
  caught the second way `ci-groom.yml` broke (BE-6015): registered, but pinned
  to a `REPLACE_AT_MERGE_…` placeholder a landed PR never replaced — the
  rewrite moves a placeholder like any other shape, so this specific case is
  now a normal, self-healing bump, not a failure.
- **A roster entry can point at a file with nothing of ours to bump.** If the
  file names some *other* github-workflows reusable and none of ours, that is
  a stale roster entry, not a movable pin — the variable is the thing to fix,
  not the file. `bump-callers.sh` checks this against the ORIGINAL content
  before the no-op test, warns per file, then **fails the run** with an
  aggregate error, instead of logging the reassuring
  `already at <short> — skipping` that hid it for `ci-groom.yml`.

Both failure modes land after the whole fleet is processed, so every other
caller still gets its bump; what they refuse to do is report success.

They stay as thin entrypoints rather than one matrix because their triggers
differ: a `cursor-review.yml` change must not spuriously bump agents-md or
pr-size callers, and vice versa. Everything else (masking, the PR-per-caller
flow, the trailing-newline fix, the single-line PR body) lives once in
`bump-callers.sh`. Registering a new fleet is: add a thin entrypoint (copy an
existing one, swap the path filter + `VAR_NAME`/`TAG`/`WORKFLOW_FILE`/
`ALLOW_EMPTY` + the preflight's `WATCHED`/`WATCHED_ASSETS`), seed its roster
secret, and add a row to this table + the paths in `test-bump-callers.yml`.
**Then `workflow_dispatch` the new entrypoint once.**
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

`preflight.sh` is that guard, and every `bump-*-callers.yml` entrypoint runs it —
except `bump-pr-risk-callers.yml`, which stays on its own inline guard because
its `paths:` filter carries `:(exclude)` entries a single `WATCHED_ASSETS` string
cannot express (see the narrowing note below). It used to be an inline copy in
each entrypoint, and the copies drifted — several skipped on a bare tip
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
| `WATCHED_ASSETS` | optional — the watched assets, a **newline-separated list** of literal paths, one per line (blank lines and surrounding whitespace ignored). A single-line value is just a one-element list, so `WATCHED_ASSETS: .github/groom` keeps working unchanged; a fleet watching more than one spells it as a YAML **literal** block scalar — `\|`, never the folded `>`, which joins the lines into one space-separated string (see below). Empty/unset means the fleet watches nothing beyond `WATCHED` |
| `WATCHED_PATHSPECS` | optional — newline-separated git **pathspecs** (`:(exclude)` entries allowed) covering what the fleet's `paths:` filter watches. When set, they replace the `WATCHED`/`WATCHED_ASSETS` object comparison as the staleness test. Every positive entry must select a tracked path, and the list must select `WATCHED` **and something under every `WATCHED_ASSETS` entry**. Used by `pr-risk`, `pr-size` and `cursor-review` |
| `WATCHED_EXEC` | optional — newline-separated repo-relative **files** a pinned caller actually executes. When set, each is probed for deletion at the tip and — unless the run was re-pointed, which makes that tip the pin target — locally too, in addition to `WATCHED`/`WATCHED_ASSETS`. **Only `pr-risk` needs this today** |
| `NEW_SHA` | the candidate SHA, normally `github.sha` |
| `GITHUB_SHA`, `GITHUB_OUTPUT` | provided by Actions |

`WATCHED`, `WATCHED_ASSETS` and every `WATCHED_EXEC` entry are **literal,
repo-relative paths, not the globs from the `paths:` filter** —
`.github/groom`, never `.github/groom/**` and never a trailing slash. A glob
resolves to nothing (`[[ -d '.github/groom/**' ]]` is false,
`git rev-parse 'HEAD:.github/groom/**'` is empty), so it would make every
comparison verify nothing and the fleet a permanent silent no-op. An absolute or
`../` path fails the other way: no tree contains one, while the local `-f`/`-d`
probe resolves it *outside* the checkout, so the two halves of the same check
disagree and the verdict turns on whether main happened to move. A
`WATCHED_EXEC` entry naming a **directory** is rejected for the same reason (it
resolves to a tree at the tip — present — and fails `[[ -f ]]` locally —
absent); `WATCHED_ASSETS` is the input for a directory. The script rejects each
of these shapes up front rather than reporting it as a decommission, and it
likewise rejects a `NEW_SHA` that is not a full 40-character lowercase SHA (it is
emitted verbatim into `$GITHUB_OUTPUT`, so a newline in it injects output lines)
and a `HEAD` that is not `GITHUB_SHA` (a `ref:` override in the consuming
checkout would have it compare main against itself).

`WATCHED_PATHSPECS` is the one input where glob syntax is legal — the pathspecs go
to `git diff`, which does the matching itself — but only `:(exclude)<path>` magic
is accepted there (`:!`, `:(glob)`, `:/` are rejected: a magic prefix this script
has not reasoned about, or a typo in one, silently changes or empties what gets
compared). `!path`, the `paths:` filter's *own* negation syntax, is rejected too
and its message names the `:(exclude)` spelling — git reads the `!` literally, so
such an entry excludes nothing and matches nothing. A list of *only* exclusions
is rejected as well — git reads that as "every path except these", the widest
possible watched surface. Both list inputs ignore blank lines, indentation and
whole-line `#` comments, so the fleet's `paths:` filter can be pasted into a YAML
block scalar with its comments intact; a variable that is **set but blank** is a
hard error rather than a fall-back to unset, because every way that shape arises
means a check the entrypoint asked for would silently not happen.

Two more things about `WATCHED_PATHSPECS` are **enforced, not merely documented**,
because both fail green: every positive entry must select at least one tracked
path (in this run's tree or the tip's), and the list as a whole must select
`WATCHED` — plus, when `WATCHED_ASSETS` is also set, something under it.
`git diff --quiet` exits 0 both for "nothing changed under these pathspecs" and
for "these pathspecs match nothing", and the second reads as *unchanged*, so a
typo, a directory rename, a positive an `:(exclude)` swallows entirely, or a list
that simply omits the reusable would re-point every caller to a tip at which
nothing was compared. The check asks `git diff` itself what the list selects, so
coverage is never judged by looser rules than the verdict.

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

**A multi-path fleet must pass `WATCHED_ASSETS`, and must list EVERY asset it
watches.** The re-point is only sound because every entry in the fleet's `paths:`
trigger is covered by the comparison — for `agents-md-integrity`
(`.github/agents-md-integrity/**`), `cursor-review` (`.github/cursor-review/**`),
`groom` (`.github/groom/**`) and `pr-size` (`scripts/check-pr-size/**`) that
includes the asset directory the reusable loads its prompts/scripts/briefs from at
run time. (`pr-risk` is multi-path too, but its filter also carries `:(exclude)`
entries that `WATCHED_ASSETS` cannot express, so its comparison is
`WATCHED_PATHSPECS` — it still *sets* `WATCHED_ASSETS`, for a different reason;
see the note below.) Compare `WATCHED` alone on one of those and a commit touching
only the assets reads as "unchanged", so callers get pinned to a tip whose other
relevant content was never verified. Read the entrypoint's
`paths:` rather than trusting this list, and if you widen a fleet's path filter,
widen these inputs in the same change.

**`cursor-review` is the two-asset fleet** (BE-7045). It watches
`.github/cursor-review/**` for the review prompts/scripts *and*
`scripts/check-pr-size/**` for the classifier `cursor-review.yml` builds at run
time from the caller's pinned `workflows_ref` — that classifier decides which of
a PR's files count as generated and therefore skip review, so a change to it
needs the same caller bump as a workflow-file change. That is why
`WATCHED_ASSETS` is a **list**: each entry is validated, compared and
decommission-checked independently, with exactly the semantics a single asset
had (any one entry absent or changed is enough to stop the bump), and a
one-element list behaves identically to the old single-value form. Note that
`scripts/check-pr-size` is deliberately watched by **two** fleets — `pr-size`
consumes the same tool — which is correct and conflict-free: they push different
stable branches.

```yaml
          WATCHED_ASSETS: |
            .github/cursor-review
            scripts/check-pr-size
```

**Spell it `|`, never `>`.** A folded scalar joins its lines into the single
string `.github/cursor-review scripts/check-pr-size`, which carries no glob and
no trailing slash — so it would pass the obvious validation, then resolve to
nothing and emit a silent `proceed=false` decommission that freezes the fleet.
`validate_path` therefore rejects an entry containing whitespace outright, along
with a leading `#` or `- `: a block scalar has no comment syntax and takes no
list dashes, so either is literal content that resolves to nothing the same way.
The contract test honors only `|` for the same reason — it must read the value
exactly as the runtime does, or it certifies a config preflight.sh misparses.

They must not be **wider** than the filter either, which is the direction an
excluding fleet gets wrong. A commit touching only an excluded path (pr-risk's
`scripts/pr-risk/tests` and its `README.md`; pr-size's and cursor-review's
`scripts/check-pr-size/*_test.go`) starts no run of its own, but it does
change the tree OID of an over-broad `WATCHED_ASSETS` — so this run reports "the
watched surface changed since", skips green as a stale re-run, and waits on a
later run that will never exist. An exclusion is a reason to narrow the inputs, or
to carry it into `WATCHED_PATHSPECS`; never to point `WATCHED_ASSETS` at the whole
directory **as the comparison**.

Setting `WATCHED_ASSETS` to that directory *alongside* `WATCHED_PATHSPECS` is a
different thing, and it is what `pr-risk` does. The pathspec diff **supersedes**
the object comparison, so the asset tree OID is never compared and the freeze
above cannot arise (verified: a commit touching only `scripts/pr-risk/tests` and
the tool `README.md` still proceeds and re-points). What `WATCHED_ASSETS` buys
there is the **coverage assertion** — the check that the pathspec list still
selects something under it. Without it, deleting the `scripts/pr-risk` line from
that list leaves `.github/workflows/pr-risk.yml` matching itself, satisfies every
remaining guard, and silently stops watching the grading logic while the fleet
keeps re-pointing callers. So: with `WATCHED_PATHSPECS`, set it; without,
narrow it.

**`test_paths_contract.sh` enforces both directions.** These pairs are
hand-written, one per entrypoint, and until BE-6476 the only thing holding them
in step was the checklist line above — `test_preflight.sh` drives synthetic
fixtures and never reads the entrypoints. The contract test does: it parses each
`bump-*-callers.yml`'s `push:` `paths:` filter, normalizes each POSITIVE entry's
`…/**` to the literal path, and requires set equality with that file's `WATCHED`
+ `WATCHED_ASSETS` (the negations are held against `WATCHED_PATHSPECS` instead,
below — they are precisely what those two inputs cannot express)
(splitting a multi-line `WATCHED_ASSETS` into one entry per line, and reading the
`|` block-scalar spelling as well as the single-line one — the parser mirrors
preflight.sh deliberately, including honoring only `|`, taking block content
literally, and stripping a trailing `# comment` from a single-line value; a
parser that read the file more permissively than the runtime does would certify
a config the runtime misparses, which is worse than no test at all).
A *new* entrypoint that runs no preflight at all fails too — the pr-risk
exemption is an explicit allow-list entry, not a silent skip, so migrating it
later fails the test until the entry is removed. Widen a filter and the test
tells you to widen the inputs in the same change.

**A filter with `!` negations must carry an equivalent `WATCHED_PATHSPECS`
(BE-7084).** Before that input existed, a `:(exclude)` on a preflight fleet was
rejected outright: a tree-OID comparison cannot express an exclusion, so such a
fleet would have frozen as a permanent stale re-run. It is no longer rejected —
it is *required to be expressible*. For a preflight fleet whose filter carries
`!` entries the test REQUIRES `WATCHED_PATHSPECS` and compares the two as sets:
each positive filter entry normalized (`x/**` → `x`) must appear as a positive
pathspec, each `!x` must appear as `:(exclude)x`, and neither side may carry an
entry the other lacks. A **file glob** in an exclusion is kept verbatim
(`!scripts/check-pr-size/*_test.go` ↔ `:(exclude)scripts/check-pr-size/*_test.go`)
— reducing it to the parent directory would widen the exclusion to swallow the
whole tool and the fleet would never bump again, so the two do NOT compare equal.
The one thing that IS normalized is a trailing `/**`, on both sides and exactly as
it is for a positive, because `x/**` and `x` select the same set in either syntax:
`!scripts/pr-risk/tests/**` is satisfied by `:(exclude)scripts/pr-risk/tests` (the
spelling pr-risk's own guard and the example below use) or by
`:(exclude)scripts/pr-risk/tests/**`, and by nothing wider. Whole-line `#`
comments in the block are ignored here exactly as `preflight.sh` ignores them, so
a filter pasted in with its comments intact still compares clean. A `!`-carrying
fleet with **no** `WATCHED_PATHSPECS` still fails, naming the freeze — that is the
case the old flat rejection existed to prevent, and it is the one that has to keep
failing. A fleet with no `!` that sets `WATCHED_PATHSPECS` anyway is held to the
same equivalence, so the input cannot drift away from the filter unnoticed.

**Keep a `*` exclusion's directory flat.** Set-equality of the two spellings is
textual, and there is one case where equal text selects *different* sets: a
`paths:` filter's `*` does not cross `/`, while a bare git pathspec's does
(`:(glob)` would fix that, and `preflight.sh` rejects that magic). While
`scripts/check-pr-size` is flat, `!…/*_test.go` and `:(exclude)…/*_test.go` agree.
Put a `*_test.go` in a *subdirectory* and they stop: the trigger fires on it,
the staleness diff has already excluded it, and the run re-points having compared
nothing that moved — the pure-churn bump BE-7084 removed, one directory down.
`test_paths_contract.sh` measures the tree for exactly this and fails the build
the day it becomes true, so it cannot happen quietly.

**An excluding fleet passes `WATCHED_PATHSPECS`; a per-file fleet passes
`WATCHED_EXEC`.** `pr-size` and `cursor-review` need the first (BE-7084): each
excludes `scripts/check-pr-size/*_test.go`, since a pinned caller builds and runs
that tool and never runs `go test`, so a test-only commit would otherwise mint a
token and fan a pure-churn bump PR to every consumer. `pr-risk` needs both — and
they are what let it move onto this script instead of keeping its own guard:

- Its `paths:` filter negates `scripts/pr-risk/tests/**` and the tool README, and
  no object comparison can express a negation. `WATCHED_PATHSPECS` is handed
  verbatim to `git diff`, so the staleness test asks precisely what the filter
  asks. **It MUST mirror the filter, exclusions included** — the same coupling as
  above, and dropping one `:(exclude)` line reinstates the false-stale freeze
  exactly. The half of that MUST which fails *green* — a list that selects
  nothing, or that never reaches `WATCHED` — is enforced rather than trusted (see
  the input rules above). It compares two trees and walks no history, so it
  composes with the deepening but does not need it.
- Its decommission surface is the three grader scripts a caller executes, not the
  directory holding them: a commit deleting the graders while leaving `tests/` and
  the README behind satisfies a `-d scripts/pr-risk` probe and would bump every
  caller onto a SHA where the tools are gone. `WATCHED_EXEC` names those files, and
  they are probed at the tip (before the staleness test, so a deletion warns rather
  than reading as "a newer commit has its own run") and again in this run's tree —
  the latter only when the run was *not* re-pointed, since a re-point makes that
  same tip the SHA callers are pinned to and this checkout no longer the thing
  worth probing.

Every other fleet leaves both unset and behaves exactly as before.

Consumption is two steps — the guard, then the bump gated on its output:

```yaml
      - name: Preflight (staleness / decommission guard)
        id: preflight
        env:
          WATCHED: .github/workflows/groom.yml
          WATCHED_ASSETS: .github/groom   # omit for a single-path fleet;
                                          # use a `|` block scalar (one literal
                                          # path per line) to watch several
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

An excluding / per-file fleet swaps the guard step's `env:` block for the list
inputs (everything else, including the gated bump step, is unchanged):

```yaml
      - name: Preflight (staleness / decommission guard)
        id: preflight
        env:
          WATCHED: .github/workflows/pr-risk.yml
          # MIRRORS the fleet's `paths:` filter, exclusions included.
          WATCHED_PATHSPECS: |
            .github/workflows/pr-risk.yml
            scripts/pr-risk
            :(exclude)scripts/pr-risk/tests
            :(exclude)scripts/pr-risk/README.md
          # The files a pinned caller actually executes.
          WATCHED_EXEC: |
            .github/workflows/pr-risk.yml
            scripts/pr-risk/grade-pr-risk.sh
            scripts/pr-risk/grade-targets.sh
            scripts/pr-risk/resolve-enabled.sh
          NEW_SHA: ${{ github.sha }}
        run: bash .github/bump-callers/preflight.sh
```

`new_sha` is a **step output**, not a `$GITHUB_ENV` export, and the consuming
step reads it through its own `env:` binding. That is deliberate: a step-level
`env: NEW_SHA:` takes precedence over the job environment, so a `$GITHUB_ENV`
write would be silently overridden by the very binding it is meant to correct.

Run the preflight **before** the Cloud Code Bot token step, and gate that step on
`proceed` too. The token is an org-wide contents/pull-requests/issues write
credential; minting it for a run the guard has already decided will bump nothing
buys nothing and leaves it live for the job. Every entrypoint is ordered that
way, and `test_paths_contract.sh` enforces it.

> **The swap is done — all eight entrypoints run this script and no inline copy
> remains.** `bump-pr-risk-callers.yml` went first (BE-6677) because it was the
> hardest: the only excluding, per-executed-file fleet, so `WATCHED_PATHSPECS`
> and `WATCHED_EXEC` are exercised by a real caller and not just by the tests.
> BE-6476 then swapped the remaining seven, which were a mechanical repeat of the
> two-step block above.
>
> pr-risk carried two checks this script did not, and **BE-6670 decided both**
> rather than leaving the swap to choose:
>
> - Its **is-ancestor check is adopted here, for every fleet** (BE-6675) — see
>   the direction guard above. It was never pr-risk-specific. The swap also fixed
>   a latent hazard in the inline copy on its way out: that one fetched a bare
>   `main`, which resolves `refs/tags/main` ahead of `refs/heads/main`, and this
>   repo force-moves major tags. This script fetches `refs/heads/main`.
> - Its **`git rev-list` "did a later *commit* touch a watched path" test is
>   deliberately not ported.** It existed because pr-risk had no re-point, so a
>   land-then-revert (net content change of zero) made that fleet call the run
>   the only one for the change and pin callers *backwards* to `github.sha`. The
>   re-point already answers that case by pinning the verified tip — which on a
>   land-then-revert is the revert commit, i.e. forward. Adding rev-list on top
>   would only skip a run whose content the tip still needs pinned. That settles
>   land-then-revert; it is not a claim that comparing objects expresses
>   everything a rev-list *pathspec* can. Swapping an excluding fleet across means
>   narrowing its inputs to what its filter really watches (above), not adding
>   rev-list back — and since BE-6676 it can express that filter directly, with
>   `WATCHED_PATHSPECS` + `WATCHED_EXEC`, instead of narrowing anything away. The
>   staleness test there is still a two-tree comparison, not a history walk.

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
