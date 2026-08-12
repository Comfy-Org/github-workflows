# bump-callers

The shared machinery that keeps SHA-pinned **callers** of this repo's reusable
workflows from rotting. When a reusable workflow is updated on `main`, it opens
a SHA-bump PR in every repo that pins a caller against it — so consumers move
forward automatically instead of silently drifting commits behind.

- **`bump-callers.sh`** — the one, fleet-agnostic bump script (fetch the caller
  list from its fleet's Actions variable, mask private repo names, rewrite the
  pin, keep one bump PR per caller current). It is the single source of truth; the workflow entrypoints are
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

Every roster used to reach `bump-callers.sh` through the step's `env:` block, and
Actions prints that block — values and all — before the script's `::add-mask::`
could run. This repo is public, so each seeded fleet had already published its
roster in a world-readable log. The difference here was that two of this fleet's
callers are non-public repos appearing in **no** already-seeded roster, so
seeding would have published two names that are not out yet — and a public log
entry cannot be unpublished. The trade was: seed now and take an irreversible
disclosure, or leave it unseeded and take a red run. The red run is reversible
and is already the designed behaviour for an empty roster, so it won.

**That blocker is gone** (BE-6482 — the roster is fetched and masked inside the
script), but the variable is still unset, so the red run persists until someone
seeds it. Do that once a dispatch of the entrypoint has been seen reading its
variable *without* the "cannot read Actions variables" error — that error means
the App still lacks **Variables: read**, and a roster the script cannot read is
one it cannot mask either. Never seed it merely to turn the red run green.

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

The roster is **fetched at run time by the script**, keyed by the `VAR_NAME` its
entrypoint passes, and masked before the first log line — it is not bound into
the step's `env:` block any more (BE-6482). That binding was the leak: Actions
prints a step's env block *before* the step runs, so the raw roster reached the
public log ahead of the masking meant to cover it. Fetching inside the script
puts the read, the shape check and the `::add-mask::` in an order the log cannot
get in front of.

Two consequences worth knowing:

- **Two tokens, not one.** A downscoped App token cannot read Actions variables
  — the app-permissions schema (the installation-token request body) has no
  `variables` key, so there is no `permission-variables` input to ask for, and
  only a token minted with *no* `permission-*` inputs carries the App's
  **Variables: read** grant. Rather than widen the token that writes across the
  whole fleet, each entrypoint mints a **second** token scoped to this repo alone
  (`repositories: github-workflows`) and hands it to the script as `VAR_TOKEN`,
  used for the roster read and nothing else. The write token (`GH_TOKEN`) keeps
  its `contents` / `pull-requests` / `issues` downscoping. `VAR_TOKEN` is
  optional: unset, the read falls back to `GH_TOKEN` (manual runs).
- **The App needs the grant.** The Cloud Code Bot App itself must hold the
  repository permission **Variables: read**, approved on the Comfy-Org
  installation, or every fleet fails with an explicit error naming that grant.

The read tries the **repo-level** variable first and falls back to the
**org-level** one, matching what the `${{ vars.* }}` binding it replaced
resolved (repo wins on a name clash). Absent at both scopes is an *empty roster*
— handled by `ALLOW_EMPTY` exactly as an empty variable is — but it always logs
a `::warning::`, because a 404 is also how GitHub answers "this token cannot see
the repository at all".

`CALLERS_JSON` still works as an explicit override for a manual run and is what
the test suite drives; a set-but-empty value means "empty roster", not "fetch".

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
