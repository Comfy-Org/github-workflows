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
#                  (e.g. .github/workflows/groom.yml).
#   NEW_SHA        The candidate commit to pin callers to (normally github.sha).
#   GITHUB_SHA     This run's own commit (provided by Actions).
#   GITHUB_OUTPUT  Step-output file (provided by Actions).
# Optional:
#   WATCHED_ASSETS Watched asset directory (e.g. .github/groom) for a fleet whose
#                  `paths:` filter has more than one entry. Empty/unset means the
#                  fleet is single-path.
#
# Outputs (written to $GITHUB_OUTPUT on every exit-0 path):
#   proceed  "true"  → the caller should run bump-callers.sh
#            "false" → stale or decommissioned; the caller should do nothing
#   new_sha  the SHA to pin callers to — NEW_SHA, or the verified main tip when
#            this run was re-pointed forward (see the re-point block below)
#
# Exits non-zero ONLY for a lookup we could not perform (failed ls-remote, failed
# fetch, unresolvable FETCH_HEAD). A lookup we couldn't perform is not evidence
# of staleness — it fails loudly rather than silently no-opping the fleet.
#
# Run from the repository root (the final decommission check tests the run's own
# checked-out tree).
set -euo pipefail

: "${WATCHED:?WATCHED is required}"
: "${NEW_SHA:?NEW_SHA is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
WATCHED_ASSETS="${WATCHED_ASSETS-}"

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
  if ! git fetch --depth=1 origin main; then
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
  if ! main_tip=$(git rev-parse --verify --quiet "FETCH_HEAD^{commit}"); then
    echo "::error::Fetched main but FETCH_HEAD does not resolve to a commit — cannot compare $WATCHED"
    exit 1
  fi
  tip_blob=$(git rev-parse --verify --quiet "FETCH_HEAD:$WATCHED" || true)
  # HEAD is this run's own checkout, so it must resolve. An empty here_blob means
  # $WATCHED is absent at github.sha, which is the deletion-commit case the final
  # guard handles — don't let it fall into the "changed since" branch below and
  # be reported as a stale re-run, which would be a misleading log for a real
  # decommission.
  if ! here_blob=$(git rev-parse --verify --quiet "HEAD:$WATCHED"); then
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
    tip_assets=$(git rev-parse --verify --quiet "FETCH_HEAD:$WATCHED_ASSETS" || true)
    here_assets=$(git rev-parse --verify --quiet "HEAD:$WATCHED_ASSETS" || true)
  fi
  if [[ -z "$tip_blob" ]] && { [[ -z "$WATCHED_ASSETS" ]] || [[ -z "$tip_assets" ]]; }; then
    # ::warning:: not a bare echo: if the reusable was deleted while live callers
    # still pin it, they all hard-fail at startup and a silently-green run here
    # is the fleet's only chance to say so.
    echo "::warning::$WATCHED no longer exists on main ($main_tip) — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
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
  # directory (cursor-review / groom / pr-size) MUST pass WATCHED_ASSETS too, or
  # the comparison silently under-verifies and callers get pinned to a tip whose
  # other relevant content was never compared. If you widen a fleet's filter
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
