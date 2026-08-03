#!/usr/bin/env bash
# apply-risk-label.sh — sync a PR's risk label to the computed tier. The one write the
# reusable pr-risk.yml workflow performs.
#
# OWNERSHIP CONTRACT: this script owns EXACTLY the label names in LABEL_MAP's values. It
# removes stale ones and applies the computed one, and never touches any other label — so a
# human who disagrees with a grade records that with their OWN label (the pilot convention is
# `risk-dispute`), which this script will never fight. Editing the grader-owned label by hand
# is futile by design: the next push re-syncs it.
#
# The label is applied with the plain GITHUB_TOKEN on purpose: GITHUB_TOKEN-applied labels do
# not fire `labeled` workflow triggers, which makes the shadow check incapable of starting a
# workflow cascade. When a later phase WANTS the label to trigger routing, that is a deliberate
# switch to an app token (the cursor-review-auto-label.yml pattern), not a default.
#
# Inputs (env):
#   REPO        owner/name of the repo holding the PR                        (required)
#   PR_NUMBER   the PR number                                                (required)
#   TIER        R0 | R1 | R2 | R3 | unknown ('' and 'null' read as unknown)  (required)
#   LABEL_MAP   tier=label pairs, comma-separated                            (optional)
#               default: R0=risk:R0,R1=risk:R1,R2=risk:R2,R3=risk:R3,unknown=risk:ungraded
#               Relabeling (e.g. a 1-indexed R1..R4 scheme) is a caller-side remap of the
#               VALUES only; tier keys are fixed R0..R3 + unknown everywhere else.
#   DRY_RUN     1 = print the plan, write nothing
#   GH_TOKEN    token for gh (in CI: the job's GITHUB_TOKEN; needs pull-requests: write for
#               the label add/remove on the PR — the labels endpoint is dual-mapped and
#               issues:write alone 403s on a PR — plus issues: write to create the risk:*
#               labels repo-side on first use)
#
# Missing labels are created on first use (color-coded, described), so enrolling a repo needs
# no manual label setup.
#
# Exit: 0 = label in sync (or dry run). 2 = usage error. 4 = a GitHub write failed — the run
#       must go red rather than pretend the label landed.

set -uo pipefail

REPO="${REPO:-}"
PR_NUMBER="${PR_NUMBER:-}"
TIER="${TIER:-}"
LABEL_MAP="${LABEL_MAP:-}"
DRY_RUN="${DRY_RUN:-0}"
DEFAULT_MAP="R0=risk:R0,R1=risk:R1,R2=risk:R2,R3=risk:R3,unknown=risk:ungraded"

log()  { printf '[apply-risk-label] %s\n' "$*" >&2; }
die()  { printf '[apply-risk-label] ERROR %s\n' "$*" >&2; exit 2; }
fail() { printf '[apply-risk-label] FAIL %s\n' "$*" >&2; exit 4; }

[ -n "$REPO" ] || die "REPO is required"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "bad REPO '$REPO' (want owner/name)"
[[ "$PR_NUMBER" =~ ^[0-9]+$ ]] || die "bad PR_NUMBER '$PR_NUMBER'"
command -v jq >/dev/null 2>&1 || die "jq not found on PATH"
[ "$DRY_RUN" = 1 ] || command -v gh >/dev/null 2>&1 || die "gh not found on PATH"

# '' and 'null' arrive when the grader refused a confident tier — both are the unknown lane.
case "$TIER" in ""|null) TIER="unknown" ;; esac
case "$TIER" in R0|R1|R2|R3|unknown) ;; *) die "bad TIER '$TIER' (want R0..R3 or unknown)" ;; esac

[ -n "$LABEL_MAP" ] || LABEL_MAP="$DEFAULT_MAP"

# Parse "tier=label,tier=label" without eval. All five tiers must resolve: a map that forgets
# `unknown` would leave ungradeable PRs silently unlabeled, which reads as "grader never ran".
label_for() { # <tier> -> label on stdout, rc 1 when unmapped
  printf '%s' "$LABEL_MAP" | tr ',' '\n' | awk -F= -v t="$1" '$1 == t { print $2; found=1 } END { exit !found }'
}
OWNED=()
for t in R0 R1 R2 R3 unknown; do
  l="$(label_for "$t")" || die "LABEL_MAP is missing a label for tier '$t' (got '$LABEL_MAP')"
  [ -n "$l" ] || die "LABEL_MAP maps tier '$t' to an empty label"
  OWNED+=("$l")
done
TARGET="$(label_for "$TIER")"

# Colors keyed by TIER (not label text, which callers may remap): green .. red, gray unknown.
color_for() {
  case "$1" in
    R0) echo "0e8a16" ;; R1) echo "fbca04" ;; R2) echo "d93f0b" ;; R3) echo "b60205" ;;
    *)  echo "cfd3d7" ;;
  esac
}

if [ "$DRY_RUN" = 1 ]; then
  log "DRY RUN — would sync $REPO#$PR_NUMBER to '$TARGET' (owned set: ${OWNED[*]})"
  printf '%s\n' "$TARGET"
  exit 0
fi

# A label name is a PATH SEGMENT in every request below, and GitHub label names legally contain
# spaces, `/`, `#`, `?` and `%`. A caller who remaps `R3=risk high` would otherwise build a
# malformed or misrouted URL: the DELETE fails, `fail` fires, and a rename paints the check red.
# Encoded for the path only — the raw name is what we log, compare and send as a form field.
enc() { jq -rn --arg s "$1" '$s | @uri'; }

# EVERY WRITE BELOW REPORTS WHY IT FAILED. The two permissions this script needs both fail as a
# bare 403, and with gh's stderr discarded the log said only `could not add label ...` — so the
# single most likely misconfiguration (granting `issues: write` but not `pull-requests: write`,
# which 403s on a PR because the labels endpoint is dual-mapped) was indistinguishable from a
# network blip or a deleted PR. `ghq` keeps gh's own message and hands it to the caller's
# fail/log text. It names endpoints and scopes, never PR content, so it is safe in a public log.
ERRF="$(mktemp "${TMPDIR:-/tmp}/apply-risk-label-err.XXXXXX")" || die "mktemp failed"
trap 'rm -f "$ERRF"' EXIT
ghq() { gh "$@" 2>"$ERRF"; }
gherr() { tr '\n' ' ' < "$ERRF" | sed 's/[[:space:]]*$//'; }

# Current labels on the PR (a PR is an issue to the labels API). --paginate because the endpoint
# returns 30 per page: on a PR with more than 30 labels a stale grader-owned label falls off page
# one, `has()` reports false, the removal loop skips it, and the PR carries two contradictory risk
# labels at once — the exact state the ownership contract above promises cannot happen.
current="$(ghq api --paginate "repos/$REPO/issues/$PR_NUMBER/labels?per_page=100" --jq '.[].name' \
           | jq -Rsc 'split("\n") | map(select(length > 0))')" \
  || fail "could not read labels on $REPO#$PR_NUMBER: $(gherr)"

has() { jq -e --arg l "$1" 'index($l) != null' >/dev/null 2>&1 <<<"$current"; }

# Remove stale grader-owned labels (everything in the owned set except the target).
for l in "${OWNED[@]}"; do
  [ "$l" = "$TARGET" ] && continue
  if has "$l"; then
    # `fail` exits BEFORE the target is added, so a 403 here leaves the PR carrying the PREVIOUS
    # push's grade for changed code. That is why the message has to name the resulting state and
    # the cause: the red check is the only signal, and a reader must not read the stale label as
    # a current verdict.
    ghq api -X DELETE "repos/$REPO/issues/$PR_NUMBER/labels/$(enc "$l")" >/dev/null \
      || fail "could not remove stale label '$l' from $REPO#$PR_NUMBER: $(gherr) — '$TARGET' was NOT applied, so the PR still carries the STALE grade '$l'"
    log "removed stale '$l'"
  fi
done

if has "$TARGET"; then
  log "already labeled '$TARGET' — nothing to do"
else
  # Ensure the label exists in the repo first, so enrollment needs no manual label setup. This
  # probe keeps its own 2>/dev/null: a 404 here is the EXPECTED first-use answer, and routing it
  # through ghq would leave that 404 text in $ERRF to be misreported by a later failure.
  if ! gh api "repos/$REPO/labels/$(enc "$TARGET")" >/dev/null 2>&1; then
    ghq api -X POST "repos/$REPO/labels" \
      -f name="$TARGET" -f color="$(color_for "$TIER")" \
      -f description="PR risk grade (advisory shadow check; grader-owned)" >/dev/null \
      || log "label '$TARGET' could not be pre-created (may already exist): $(gherr) — trying the add anyway"
  fi
  ghq api -X POST "repos/$REPO/issues/$PR_NUMBER/labels" -f "labels[]=$TARGET" >/dev/null \
    || fail "could not add label '$TARGET' to $REPO#$PR_NUMBER: $(gherr)"
  log "added '$TARGET'"
fi

printf '%s\n' "$TARGET"
exit 0
