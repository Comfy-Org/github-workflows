#!/usr/bin/env bash
#
# Staleness / decommission preflight for the bump-* caller fleets.
#
# Every `bump-*-callers.yml` entrypoint has to answer the same two questions
# before it hands control to bump-callers.sh:
#
#   1. Is this run STALE — i.e. has a *later* commit already touched the watched
#      surface, so that commit has its own run and will pin the newer content?
#   2. Has the watched surface been DECOMMISSIONED — deleted — so that pinning
#      callers to this SHA would break every one of them?
#
# Both questions used to be answered by an inline copy of this logic in each
# bump-* entrypoint. Eight near-copies is exactly the drift pattern this
# directory exists to prevent (see bump-callers.sh's header), and they HAD
# drifted: five skipped on a bare tip mismatch, which throws away the ONLY run
# for a change; bump-auto-label-callers.yml compared content but forgot to
# re-point the pin at the verified tip; bump-detect-unreviewed-merge-callers.yml
# was the hardened one; and bump-pr-risk-callers.yml grew a different hardening
# again (a `git rev-list` "did a later COMMIT touch a watched path" test plus an
# is-ancestor orphan check, and no re-point).
#
# This script is the one implementation, and it deliberately adopts the
# bump-detect-unreviewed-merge-callers.yml semantics (PR #117) — exact-refname
# tip parse, FETCH_HEAD verification, `$WATCHED`-variable deletion guard, and the
# NEW_SHA re-point — generalized to multi-path fleets. BE-6476 swapped the
# entrypoints over: SEVEN of the eight now run this script, and their inline
# copies are gone. The exception is bump-pr-risk-callers.yml, which stays on its
# own inline guard because its `paths:` filter carries `:(exclude)` entries that
# a single WATCHED_ASSETS string cannot express — see the COUPLED TO THE PATH
# FILTER note on the re-point below.
#
# Each consuming entrypoint runs this BEFORE it mints its Cloud Code Bot token
# and gates that step on `proceed` too, so a run this guard no-ops never mints an
# org-wide write credential. Keep that ordering when adding a fleet.
#
# What to do with bump-pr-risk-callers.yml's two extra checks was that swap's one
# open decision. BE-6670 made it, and both halves are settled:
#   * The is-ancestor DIRECTION GUARD is ADOPTED here, for every fleet (BE-6675).
#     It was never pr-risk-specific: without it, a main that moved BACKWARDS —
#     a force-push, a revert-reset, or a stale replica answering the tip lookup —
#     either reads as a stale re-run and freezes the whole fleet behind a green
#     run, or re-points every caller to the OLDER tip. See the guard below.
#   * The `git rev-list` "did a later COMMIT touch a watched path" test is
#     deliberately NOT ported. It exists because pr-risk has no re-point: there, a
#     land-then-revert nets to zero content change, so a content comparison calls
#     this run the only one for the change and pins callers BACKWARDS to
#     github.sha. The re-point below already answers that case — it pins the
#     verified TIP, which on a land-then-revert IS the revert commit, i.e.
#     forward. Porting rev-list on top would only add a stricter staleness
#     verdict that skips a run whose content the tip still needs pinned.
#     That decision is about the land-then-revert case, and it is NOT a claim
#     that an object comparison expresses everything a rev-list PATHSPEC can:
#     pr-risk's filter carries `:(exclude)` entries, and an over-broad watched
#     surface here has its own failure mode — see the COUPLED TO THE PATH FILTER
#     note on the re-point below. Swapping an excluding fleet onto this script
#     means narrowing its inputs to what the filter really watches (or keeping
#     it on its own guard), not adding rev-list.
#
# Required environment:
#   WATCHED        Repo-relative path of the watched reusable workflow file
#                  (e.g. .github/workflows/groom.yml). A LITERAL path, never a
#                  `paths:`-filter glob — see the shape validation below.
#   NEW_SHA        The candidate commit to pin callers to (normally github.sha).
#                  Must be a full 40-character lowercase SHA.
#   GITHUB_SHA     This run's own commit (provided by Actions). Must match HEAD.
#   GITHUB_OUTPUT  Step-output file (provided by Actions).
# Optional:
#   WATCHED_ASSETS Watched asset directories for a fleet whose `paths:` filter has
#                  more than one entry — a NEWLINE-SEPARATED LIST of literal
#                  paths, one per line (blank lines ignored). The natural YAML
#                  spelling is a block scalar:
#
#                      WATCHED_ASSETS: |
#                        .github/cursor-review
#                        scripts/check-pr-size
#
#                  A single-line value is just a one-element list, so every
#                  single-asset fleet's existing `WATCHED_ASSETS: .github/groom`
#                  spelling keeps working unchanged. Empty/unset means the fleet
#                  watches nothing beyond WATCHED. Each entry is validated,
#                  compared, and decommission-checked INDEPENDENTLY, with exactly
#                  the semantics a single asset had: any one of them absent or
#                  changed is enough to stop the bump.
#
# Outputs (written to $GITHUB_OUTPUT on every exit-0 path):
#   proceed  "true"  → the caller should run bump-callers.sh
#            "false" → stale or decommissioned; the caller should do nothing
#   new_sha  the SHA to pin callers to — NEW_SHA, or the verified main tip when
#            this run was re-pointed forward (see the re-point block below)
#
# Exits non-zero ONLY for an input we cannot trust (malformed SHA, glob-shaped
# watched path, a HEAD that is not GITHUB_SHA), a lookup we could not perform
# (failed ls-remote, failed fetch, unresolvable FETCH_HEAD, a rev-parse that
# failed rather than reporting absence), or an answer that contradicts history
# (a fetched main tip that does not descend from this run's commit, or from the
# tip the ls-remote reported moments earlier). None of those is evidence of
# staleness — it fails loudly rather than silently no-opping the fleet.
#
# Run from the repository root (the final decommission check tests the run's own
# checked-out tree).
set -euo pipefail

: "${WATCHED:?WATCHED is required}"
: "${NEW_SHA:?NEW_SHA is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
WATCHED_ASSETS="${WATCHED_ASSETS-}"

# --- input shape validation --------------------------------------------------
# A watched path is a LITERAL repo-relative path, never the glob from the fleet's
# `paths:` filter. Every instruction to "widen these inputs to match the fleet's
# path filter" points a maintainer straight at `.github/groom/**`, and a glob
# resolves to NOTHING here: `[[ -d '.github/groom/**' ]]` is false and
# `git rev-parse 'HEAD:.github/groom/**'` returns empty. A trailing slash
# (`.github/groom/`) does the same. Either one would make every comparison
# silently verify nothing and turn the whole fleet into a permanent no-op behind
# a green run — reject the shape instead of reporting it as a decommission.
validate_path() { # $1 = input name, $2 = value ("" = unset, skip)
  [[ -n "$2" ]] || return 0
  if [[ "$2" == *'*'* || "$2" == *'?'* || "$2" == *'['* ]]; then
    echo "::error::$1 must be a literal path, not a glob (got '$2') — pass the directory itself, e.g. .github/groom, not .github/groom/**"
    exit 1
  fi
  if [[ "$2" == */ ]]; then
    echo "::error::$1 must not end in a slash (got '$2') — a trailing slash resolves to nothing, so the comparison would silently verify nothing"
    exit 1
  fi
}
validate_path WATCHED "$WATCHED"

# WATCHED_ASSETS is a newline-separated LIST. Parse it into an array before
# validating, so each entry gets the same glob / trailing-slash rejection a
# single asset used to get — a fleet whose second line is `.github/groom/**`
# must fail as loudly as one whose only line is.
# Surrounding whitespace is stripped per entry. A flow scalar
# (`WATCHED_ASSETS: .github/groom`) cannot carry it, but a block scalar can hold
# trailing spaces that are invisible in review, and ` .github/groom` resolves to
# nothing exactly like the glob above — silently verifying nothing is the failure
# this whole validation block exists to prevent, and there is no legitimate
# watched path with leading or trailing spaces.
asset_dirs=()
while IFS= read -r asset_line; do
  asset_line="${asset_line#"${asset_line%%[![:space:]]*}"}"
  asset_line="${asset_line%"${asset_line##*[![:space:]]}"}"
  [[ -n "$asset_line" ]] || continue
  validate_path WATCHED_ASSETS "$asset_line"
  asset_dirs+=("$asset_line")
done <<<"$WATCHED_ASSETS"

# NEW_SHA is the one value here that is never derived from a lookup — every check
# below validates GITHUB_SHA/HEAD, while NEW_SHA is emitted verbatim into
# $GITHUB_OUTPUT and handed to bump-callers.sh's pin rewrite. Its SHAPE is
# therefore load-bearing: a value containing a newline injects extra output lines
# (an injected `proceed=true` would win over the `proceed=false` this script
# wrote), and any non-SHA silently becomes what every caller in the fleet is
# pinned to.
require_sha() { # $1 = input name, $2 = value
  if [[ ! "$2" =~ ^[0-9a-f]{40}$ ]]; then
    # Strip CR/LF before logging: an untrusted multi-line value would otherwise
    # spread across the log as forged annotation lines of its own.
    echo "::error::$1 must be a full 40-character lowercase commit SHA (got '${2//[$'\n'$'\r']/ }')"
    exit 1
  fi
}
require_sha NEW_SHA "$NEW_SHA"
require_sha GITHUB_SHA "$GITHUB_SHA"

# The staleness decision is keyed off $GITHUB_SHA, but the "here" side of every
# comparison below is read from HEAD (and from the working tree by the final
# -f/-d guards) — nothing otherwise asserts the two agree. A consuming job whose
# `actions/checkout` uses a `ref:` override, or any earlier step that moves HEAD,
# would have this script compare main against itself: every comparison reads
# "unchanged", so every stale re-run proceeds and re-points.
if ! head_sha=$(git rev-parse --verify --quiet 'HEAD^{commit}'); then
  echo "::error::Could not resolve HEAD — preflight must run from the root of this run's own checkout"
  exit 1
fi
if [[ "$head_sha" != "$GITHUB_SHA" ]]; then
  echo "::error::HEAD ($head_sha) is not this run's commit GITHUB_SHA ($GITHUB_SHA) — the checkout must not use a ref: override. Refusing to compare main against itself."
  exit 1
fi

# Resolve a rev to an object id, distinguishing "absent from that tree"
# (rev-parse exit 1, empty result — a real answer this script acts on) from "the
# lookup itself failed" (any other status: a missing promisor object, a corrupt
# pack). A bare `|| true` collapses both into "absent", which turns a lookup we
# could not perform into a silent decommissioned/stale verdict — the same
# not-evidence anti-pattern the ls-remote guard below rejects.
RESOLVED=""
resolve_oid() { # $1 = rev; result in $RESOLVED, empty when absent from the tree
  local rc=0
  RESOLVED=$(git rev-parse --verify --quiet "$1") || rc=$?
  if (( rc > 1 )); then
    echo "::error::Could not look up $1 (git rev-parse exited $rc) — refusing to read a failed lookup as a deletion"
    exit 1
  fi
}

# Both outputs are written on EVERY exit-0 path, so a consuming step never reads
# an empty `new_sha` off a skip. NEW_SHA is deliberately a step OUTPUT and not a
# $GITHUB_ENV export: a step-level `env: NEW_SHA:` binding in the consuming step
# takes precedence over the job environment, so a $GITHUB_ENV write would be
# silently overridden by the very binding it is meant to correct.
emit() {
  printf 'proceed=%s\n' "$1" >> "$GITHUB_OUTPUT"
  printf 'new_sha=%s\n' "$2" >> "$GITHUB_OUTPUT"
}

# The main-only ref guard in the entrypoints cannot catch a manual RE-RUN of an
# older main run: github.ref is refs/heads/main but github.sha is that run's
# original (now stale) commit, and the bumper would force-repin every caller to
# it. So establish the current main tip first; the guard below decides whether
# this run is genuinely stale.
# Don't pipe into `cut`: the pipeline would report cut's status, so a failed
# ls-remote (network blip, remote hiccup) yields an EMPTY main_tip that then
# compares unequal to github.sha and silently no-ops the whole fleet bump as if
# the run were stale. A lookup we couldn't perform is not evidence of staleness
# — fail loudly.
# `--refs` plus an exact-refname match, not just the first line: git matches ref
# patterns at component boundaries, so a branch literally named
# `foo/refs/heads/main` also matches this pattern and could be the line a bare
# `%%\t*` parse consumes.
if ! ls_remote=$(git ls-remote --refs origin refs/heads/main); then
  echo "::error::Could not look up the current main tip (git ls-remote failed)"
  exit 1
fi
main_tip=$(awk '$2 == "refs/heads/main" { print $1; exit }' <<<"$ls_remote")
if [[ -z "$main_tip" ]]; then
  echo "::error::git ls-remote returned no SHA for refs/heads/main"
  exit 1
fi

if [[ "$main_tip" != "$GITHUB_SHA" ]]; then
  # main has moved on. That alone does NOT make this run stale: the push trigger
  # is path-filtered to the watched surface, so an unrelated commit landing in
  # the seconds between this run's trigger and this check starts NO run of its
  # own. Skipping on a bare SHA mismatch would discard the only run for this
  # change and leave every caller frozen — the exact pin-drift this fleet exists
  # to prevent. (That bare-mismatch skip is what five of the entrypoints did
  # before this script existed.)
  # What actually distinguishes a stale re-run is that the watched surface has
  # CHANGED since: then a later commit did touch a filtered path and does have
  # its own run, which will pin the newer content.
  # Fetch the BRANCH ref explicitly. `git fetch origin main` resolves the bare
  # name through the refspec rules, which consult `refs/tags/<name>` BEFORE
  # `refs/heads/<name>` — and this repo routinely creates and force-moves major
  # tags. A tag named `main` would shadow the branch, silently making an
  # arbitrary tagged commit the FETCH_HEAD that the blob comparison and the
  # re-point below both run against — and therefore what every caller gets
  # pinned to. `refs/heads/main` can only ever be the branch, which is also the
  # ref the exact-refname `ls-remote` match above resolved. (bump-pr-risk's copy
  # fetches a bare `main` — do not "align" with it; that is the shadowing bug.)
  #
  # Fetch REAL history, not just the tip. The direction guards below ask whether
  # one commit DESCENDS from another, and `git merge-base --is-ancestor` cannot
  # answer that against a `--depth=1` graft: a parentless FETCH_HEAD makes even a
  # legitimate forward move read as not-an-ancestor, so the naive one-liner would
  # hard-fail every re-point (verified empirically, spike BE-6670). Deepening is
  # what makes the guards sound. actions/checkout clones shallow, and
  # `--unshallow` errors out on an already-complete clone — hence the probe
  # rather than an unconditional flag.
  # Ask git whether the REPOSITORY is shallow; don't stat `$(git rev-parse
  # --git-dir)/shallow`. Inside a linked worktree `--git-dir` is that worktree's
  # own directory while the `shallow` marker lives in the COMMON git dir, so the
  # hand-rolled probe false-negatives there and skips the deepening entirely.
  # The verdict still comes out right today — a plain fetch into a shallow clone
  # sends the new commits down to the existing boundary, so HEAD stays reachable
  # (measured; test_preflight.sh's shallow_worktree case) — but that leaves the
  # guards resting on fetch's boundary behavior instead of on the `--unshallow`
  # this comment says makes them sound, and one refspec away from the shape that
  # does break it. `--is-shallow-repository` is correct in both layouts (and,
  # being a non-empty array either way, the form below is also safe under
  # `set -u` on bash 3.2, where an empty `"${arr[@]}"` is an unbound variable).
  # Cost: bounded by this repo's own history — a full clone of it measures well
  # under 2s today — and this branch only runs on a tip mismatch. Deliberately no
  # `--deepen` ceiling: a partial deepening cannot answer the ancestry question,
  # which is the whole point of fetching here. The caller job's `timeout-minutes`
  # is the backstop for an origin that hangs.
  fetch_args=(origin refs/heads/main)
  if [[ "$(git rev-parse --is-shallow-repository)" == "true" ]]; then
    fetch_args=(--unshallow "${fetch_args[@]}")
  fi
  if ! git fetch "${fetch_args[@]}"; then
    echo "::error::Could not fetch the current main tip to compare $WATCHED"
    exit 1
  fi
  # Prove FETCH_HEAD resolves to a real commit BEFORE reading objects out of it.
  # `git rev-parse --verify --quiet` returns empty both for "that path is absent
  # from this tree" and for "this revision could not be resolved at all" (a
  # partial fetch, an unexpected FETCH_HEAD state). Without this guard the second
  # case is indistinguishable from deletion and would exit 0 as "decommissioned"
  # — the same "a lookup we couldn't perform is not evidence" anti-pattern the
  # ls-remote guard above rejects, but silently no-opping the whole fleet. With
  # it, an empty tip_blob genuinely means absent-from-tree.
  if ! fetched_tip=$(git rev-parse --verify --quiet "FETCH_HEAD^{commit}"); then
    echo "::error::Fetched main but FETCH_HEAD does not resolve to a commit — cannot compare $WATCHED"
    exit 1
  fi
  # main can move in the seconds between the ls-remote and the fetch. Moving
  # FORWARD is a benign race: compare against — and re-point to — the tip whose
  # objects we actually read, and say so in the log. But "advanced" is a
  # DIRECTION, and it has to be measured rather than assumed: the fetch can
  # equally land on a commit OLDER than the one ls-remote reported moments
  # earlier (a force-push landing in that window, or a stale read replica
  # answering the fetch). The HEAD check below does NOT catch that — a rewind to
  # a commit that is still ahead of this run passes it, and the fleet is then
  # silently pinned to a tip we already know main was ahead of. So require the
  # observed tip to be an ancestor of the fetched one.
  # A `--is-ancestor` that cannot be performed at all (exit >1: the observed tip
  # is not even present locally after the fetch) lands here too, and correctly —
  # in a forward move the observed tip IS an ancestor of the fetched one and
  # therefore present, so its absence is itself evidence the move was not
  # forward. The message names both readings.
  if [[ "$fetched_tip" != "$main_tip" ]]; then
    if ! git merge-base --is-ancestor "$main_tip" "$fetched_tip"; then
      echo "::error::the fetched main tip ($fetched_tip) does not descend from the tip the lookup reported moments earlier ($main_tip) — main moved backwards, or a stale replica answered the fetch (or the ancestry could not be checked at all); refusing to compare or re-point"
      exit 1
    fi
    echo "main advanced from $main_tip to $fetched_tip between the tip lookup and the fetch — comparing against the fetched tip"
  fi
  main_tip="$fetched_tip"
  # Refuse to act on a tip that is not a descendant of this run's commit: a
  # force-push back, a revert-reset, or a stale replica answering the lookup.
  # Proceeding either way is wrong, and BOTH ways are silent today — if the
  # watched content DIFFERS at the older tip, the comparison below reports a
  # false "stale run/re-run; the newer commit has its own run" and exits green,
  # freezing every caller behind a run that will never come; if it MATCHES, the
  # re-point hands `new_sha` the OLDER tip and pins the whole fleet BACKWARDS.
  # Loud, not a green skip — a lookup that contradicts history is not evidence of
  # staleness. (Ported from bump-pr-risk-callers.yml, BE-6670.)
  #
  # Unlike resolve_oid, this deliberately does NOT split "not an ancestor" (exit
  # 1) from "the check itself errored" (exit >1). There the distinction is
  # load-bearing because the two verdicts DIVERGE — absence exits 0 and skips
  # silently, a failed lookup must not. Here they converge: both are an
  # `::error::` and exit 1, so splitting them would only reword a log line — and
  # the line already names both readings rather than asserting the diagnosis.
  #
  # Compare the OID we resolved and verified above, not the FETCH_HEAD ref: that
  # ref is mutable, and anything rewriting it in between (a retry wrapper, a
  # concurrent git operation in the same workspace, a hook) would let a commit
  # other than the one checked here be the one whose blobs are compared and
  # emitted as new_sha. Same reason the reads below take $fetched_tip.
  if ! git merge-base --is-ancestor "$head_sha" "$fetched_tip"; then
    echo "::error::the fetched main tip ($main_tip) does not descend from this run's commit $GITHUB_SHA — main moved backwards, or a stale replica answered (or the ancestry could not be checked at all); refusing to compare or re-point"
    exit 1
  fi
  resolve_oid "$fetched_tip:$WATCHED"
  tip_blob="$RESOLVED"
  # HEAD is this run's own checkout, so the lookup itself must succeed (a failure
  # exits inside resolve_oid). An EMPTY here_blob means $WATCHED is absent at
  # github.sha, which is the deletion-commit case the final guard handles — don't
  # let it fall into the "changed since" branch below and be reported as a stale
  # re-run, which would be a misleading log for a real decommission.
  resolve_oid "HEAD:$WATCHED"
  here_blob="$RESOLVED"
  if [[ -z "$here_blob" ]]; then
    echo "::warning::$WATCHED is absent at this run's own commit $GITHUB_SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
    emit false "$NEW_SHA"
    exit 0
  fi
  # Multi-path fleets pin further surfaces: the asset directories the reusable
  # loads its prompts/scripts/briefs from — or, for cursor-review, BUILDS the
  # check-pr-size classifier from — at run time. Compare each one's TREE OID the
  # same way, INDEPENDENTLY: any single entry differing is enough to make this a
  # stale re-run, and any single entry missing is enough to make it a
  # decommission. See the COUPLED TO THE PATH FILTER note on the re-point below.
  #
  # Every absent-at-HEAD check runs before any tip_gone verdict is acted on
  # (that is why this loop exits on the former but only RECORDS the latter),
  # preserving the single-asset ordering: a surface already gone at this run's
  # own commit is a decommission, not a "changed since".
  assets_tip_gone=""
  assets_changed=""
  if (( ${#asset_dirs[@]} > 0 )); then
    for asset_dir in "${asset_dirs[@]}"; do
      resolve_oid "$fetched_tip:$asset_dir"
      tip_asset="$RESOLVED"
      resolve_oid "HEAD:$asset_dir"
      here_asset="$RESOLVED"
      # Same reasoning as the here_blob guard above: an asset tree that is
      # already gone at this run's own commit is a decommission, not a
      # "changed since".
      if [[ -z "$here_asset" ]]; then
        echo "::warning::$asset_dir is absent at this run's own commit $GITHUB_SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
        emit false "$NEW_SHA"
        exit 0
      fi
      # First match wins for both, so the reported path is deterministic
      # (the entries are compared in the order the fleet lists them).
      if [[ -z "$tip_asset" ]] && [[ -z "$assets_tip_gone" ]]; then
        assets_tip_gone="$asset_dir"
      fi
      if [[ "$tip_asset" != "$here_asset" ]] && [[ -z "$assets_changed" ]]; then
        assets_changed="$asset_dir"
      fi
    done
  fi
  # ANY ONE watched surface being gone at the tip is a decommission — not all of
  # them. Retirement is normally staged (delete the reusable, clean up its asset
  # directories in later commits), so an AND here would let the common case fall
  # through to the "stale run/re-run" branch and exit green, suppressing the
  # ::warning:: below. It would also disagree with the local -f/-d guards at the
  # bottom of this script, which already treat any one surface missing as a
  # decommission — the same situation must not get two different verdicts
  # depending on which branch reached it.
  # WATCHED is reported ahead of the assets, and the assets in the order the
  # fleet lists them, so the named path is deterministic rather than incidental.
  tip_gone=""
  if [[ -z "$tip_blob" ]]; then
    tip_gone="$WATCHED"
  elif [[ -n "$assets_tip_gone" ]]; then
    tip_gone="$assets_tip_gone"
  fi
  if [[ -n "$tip_gone" ]]; then
    # ::warning:: not a bare echo: if the reusable was deleted while live callers
    # still pin it, they all hard-fail at startup and a silently-green run here
    # is the fleet's only chance to say so.
    echo "::warning::$tip_gone no longer exists on main ($main_tip) — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
    emit false "$NEW_SHA"
    exit 0
  fi
  if [[ "$tip_blob" != "$here_blob" ]] || [[ -n "$assets_changed" ]]; then
    echo "github.sha $GITHUB_SHA is behind main ($main_tip) and the watched surface changed since — stale run/re-run; the newer commit has its own run. Nothing to bump"
    emit false "$NEW_SHA"
    exit 0
  fi
  # Pin callers to the VERIFIED TIP, not to this run's stale github.sha. We have
  # just proved every watched object is byte-identical at both, so the tip is the
  # same reusable content at a commit that is actually current — pinning the
  # older SHA would hand every caller a non-tip commit (and, on a
  # land-then-revert, re-pin them backwards).
  #
  # COUPLED TO THE PATH FILTER — this is only sound because every entry in the
  # fleet's `paths:` trigger is covered by the comparison above. A single-path
  # fleet passes WATCHED alone; a fleet whose filter also watches asset
  # directories MUST list EVERY one of them in WATCHED_ASSETS, or the comparison
  # silently under-verifies and callers get pinned to a tip whose other relevant
  # content was never compared. Today that is agents-md-integrity
  # (.github/agents-md-integrity/**), groom (.github/groom/**), pr-size
  # (scripts/check-pr-size/**) and cursor-review, which watches TWO
  # (.github/cursor-review/** for the review prompts/scripts and
  # scripts/check-pr-size/** for the classifier it builds at run time) — plus
  # pr-risk, whose filter also carries `:(exclude)` entries that WATCHED_ASSETS
  # cannot express at all (see the header). Read the entrypoint's `paths:`
  # rather than trusting this list, and if you widen a fleet's filter again,
  # widen the inputs here in the same change — test_paths_contract.sh fails the
  # build if you don't.
  #
  # The inputs must not be WIDER than the filter either, and that direction is
  # the one an excluding fleet gets wrong. A commit touching only an EXCLUDED
  # path (pr-risk's `scripts/pr-risk/tests`, its `README.md`) starts no run of
  # its own — but it does change the tree OID of an over-broad WATCHED_ASSETS,
  # so the comparison above reports "the watched surface changed since" and this
  # run skips green as a stale re-run, waiting on a later run that will never
  # exist. That freezes the fleet exactly as a bare tip mismatch used to. An
  # exclusion is therefore a reason to narrow the inputs (or leave that fleet on
  # its own guard), never to point WATCHED_ASSETS at the whole directory.
  echo "main moved to $main_tip since $GITHUB_SHA, but the watched surface is unchanged — this run is still the only one for that change; pinning callers to $main_tip and proceeding"
  NEW_SHA="$main_tip"
fi

# The push path filter also matches a commit that DELETES the reusable workflow;
# bumping callers to a SHA where it is gone would break every caller. Deletion
# means decommissioning — no-op.
# Test "$WATCHED", not a second copy of the literal path: two literals drift
# apart on a rename, and the stale one would name a file that never exists,
# making this test always true and the whole fleet a permanent silent no-op.
if [[ ! -f "$WATCHED" ]]; then
  echo "::warning::$WATCHED absent at this SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
  emit false "$NEW_SHA"
  exit 0
fi
# Every watched asset directory gets the same test, for the same reason: ANY one
# of them missing at this SHA means a caller pinned here would load a surface
# that is gone. Same first-match-wins ordering as the tip comparison above.
if (( ${#asset_dirs[@]} > 0 )); then
  for asset_dir in "${asset_dirs[@]}"; do
    if [[ ! -d "$asset_dir" ]]; then
      echo "::warning::$asset_dir absent at this SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
      emit false "$NEW_SHA"
      exit 0
    fi
  done
fi

emit true "$NEW_SHA"
