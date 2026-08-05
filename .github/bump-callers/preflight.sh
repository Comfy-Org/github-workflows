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
# Both questions are answered today by an inline copy of this logic in each
# bump-* entrypoint. Eight near-copies is exactly the drift pattern this
# directory exists to prevent (see bump-callers.sh's header), and they HAVE
# drifted: five skip on a bare tip mismatch, which throws away the ONLY run for a
# change; bump-auto-label-callers.yml compares content but forgets to re-point
# the pin at the verified tip; bump-detect-unreviewed-merge-callers.yml is the
# hardened one; and bump-pr-risk-callers.yml has since grown a different
# hardening again (a `git rev-list` "did a later COMMIT touch a watched path"
# test plus an is-ancestor orphan check, and no re-point).
#
# This script is the one implementation, and it deliberately adopts the
# bump-detect-unreviewed-merge-callers.yml semantics (PR #117) — exact-refname
# tip parse, FETCH_HEAD verification, `$WATCHED`-variable deletion guard, and the
# NEW_SHA re-point — generalized to multi-path fleets. Nothing consumes it yet;
# swapping the entrypoints over is a separate change, and the pr-risk swap in
# particular has to decide what to do with that fleet's extra checks rather than
# drop them.
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
#   WATCHED_ASSETS Watched asset directory (e.g. .github/groom) for a fleet whose
#                  `paths:` filter has more than one entry. Empty/unset means the
#                  fleet is single-path. Also a literal path.
#
# Outputs (written to $GITHUB_OUTPUT on every exit-0 path):
#   proceed  "true"  → the caller should run bump-callers.sh
#            "false" → stale or decommissioned; the caller should do nothing
#   new_sha  the SHA to pin callers to — NEW_SHA, or the verified main tip when
#            this run was re-pointed forward (see the re-point block below)
#
# Exits non-zero ONLY for an input we cannot trust (malformed SHA, glob-shaped
# watched path, a HEAD that is not GITHUB_SHA) or a lookup we could not perform
# (failed ls-remote, failed fetch, unresolvable FETCH_HEAD, a rev-parse that
# failed rather than reporting absence). Neither is evidence of staleness — it
# fails loudly rather than silently no-opping the fleet.
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
validate_path WATCHED_ASSETS "$WATCHED_ASSETS"

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
  # ref the exact-refname `ls-remote` match above resolved.
  if ! git fetch --depth=1 origin refs/heads/main; then
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
  # A benign race, not an error: main can advance in the seconds between the
  # ls-remote and the fetch. Compare against — and re-point to — the tip whose
  # objects we actually read, and say so in the log.
  if [[ "$fetched_tip" != "$main_tip" ]]; then
    echo "main advanced from $main_tip to $fetched_tip between the tip lookup and the fetch — comparing against the fetched tip"
  fi
  main_tip="$fetched_tip"
  resolve_oid "FETCH_HEAD:$WATCHED"
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
  # Multi-path fleets pin a second surface: the asset directory the reusable
  # loads its prompts/scripts/briefs from at run time. Compare its TREE OID the
  # same way — see the COUPLED TO THE PATH FILTER note on the re-point below.
  tip_assets=""
  here_assets=""
  if [[ -n "$WATCHED_ASSETS" ]]; then
    resolve_oid "FETCH_HEAD:$WATCHED_ASSETS"
    tip_assets="$RESOLVED"
    resolve_oid "HEAD:$WATCHED_ASSETS"
    here_assets="$RESOLVED"
    # Same reasoning as the here_blob guard above: an asset tree that is already
    # gone at this run's own commit is a decommission, not a "changed since".
    if [[ -z "$here_assets" ]]; then
      echo "::warning::$WATCHED_ASSETS is absent at this run's own commit $GITHUB_SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
      emit false "$NEW_SHA"
      exit 0
    fi
  fi
  # EITHER watched surface being gone at the tip is a decommission — not both.
  # Retirement is normally staged (delete the reusable, clean up its asset
  # directory in a later commit), so an AND here would let the common case fall
  # through to the "stale run/re-run" branch and exit green, suppressing the
  # ::warning:: below. It would also disagree with the local -f/-d guards at the
  # bottom of this script, which already treat either surface missing as a
  # decommission — the same situation must not get two different verdicts
  # depending on which branch reached it.
  tip_gone=""
  if [[ -z "$tip_blob" ]]; then
    tip_gone="$WATCHED"
  elif [[ -n "$WATCHED_ASSETS" ]] && [[ -z "$tip_assets" ]]; then
    tip_gone="$WATCHED_ASSETS"
  fi
  if [[ -n "$tip_gone" ]]; then
    # ::warning:: not a bare echo: if the reusable was deleted while live callers
    # still pin it, they all hard-fail at startup and a silently-green run here
    # is the fleet's only chance to say so.
    echo "::warning::$tip_gone no longer exists on main ($main_tip) — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
    emit false "$NEW_SHA"
    exit 0
  fi
  if [[ "$tip_blob" != "$here_blob" ]] || [[ "$tip_assets" != "$here_assets" ]]; then
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
  # fleet passes WATCHED alone; a fleet whose filter also watches an asset
  # directory MUST pass WATCHED_ASSETS too, or the comparison silently
  # under-verifies and callers get pinned to a tip whose other relevant content
  # was never compared. Today that is agents-md-integrity
  # (.github/agents-md-integrity/**), cursor-review (.github/cursor-review/**),
  # groom (.github/groom/**) and pr-size (scripts/check-pr-size/**) — plus
  # pr-risk, whose filter also carries `:(exclude)` entries that a single
  # WATCHED_ASSETS string cannot express (see the header). Read the entrypoint's
  # `paths:` rather than trusting this list, and if you widen a fleet's filter
  # again, widen the inputs here in the same change.
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
if [[ -n "$WATCHED_ASSETS" ]] && [[ ! -d "$WATCHED_ASSETS" ]]; then
  echo "::warning::$WATCHED_ASSETS absent at this SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
  emit false "$NEW_SHA"
  exit 0
fi

emit true "$NEW_SHA"
