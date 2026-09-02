#!/usr/bin/env bash
# apply-risk-label.sh — sync a PR's risk label to the computed tier. The one write the
# reusable pr-risk.yml workflow performs.
#
# OWNERSHIP CONTRACT: this script owns EXACTLY the label names in LABEL_MAP's values, and a PR it
# has written to carries EXACTLY ONE of them. That holds under concurrency BY CONSTRUCTION, not by
# luck: the sync is a single atomic `PUT .../labels`, which replaces the PR's whole label set in one
# request, so there is no delete-then-add window for a second run to interleave into. Two runs
# racing (a `pr_numbers` batch and a `pull_request` event run sit in different concurrency groups,
# so they CAN overlap) end last-writer-wins with still exactly one owned label — possibly the staler
# tier. That is acceptable because the label is advisory and the next grade re-syncs it, with the
# honest caveat that on the LAST push to a PR there is no next grade: a superseded run whose PUT
# lands after the winner's (cancellation is not instantaneous) can leave the stale tier standing,
# with a green check and no signal. Re-dispatch on `pr_number` if a grade looks wrong. Zero owned
# labels is impossible: every PUT this script builds contains the target, and LABEL_MAP is rejected
# up front if any tier maps to an empty name, so the array can never degrade to the empty-labels
# request that strips a PR. Labels it does NOT own are carried through the PUT as the snapshot read
# saw them (see RESIDUAL), so a human who disagrees with a grade records that with their
# OWN label (the pilot convention is `risk-dispute`) and the grader will never fight it. Editing the
# grader-owned label by hand is futile by design: the next push re-syncs it.
#
# RESIDUAL — the price of atomicity, worth knowing for the pilot: the PUT is built from a SNAPSHOT
# read, so it is an unguarded read-modify-write (the labels endpoint offers no version precondition
# that could make it otherwise), and it clobbers BOTH directions inside that window. A NON-owned
# label ADDED there is silently dropped — `risk-dispute` INCLUDED — and a non-owned label REMOVED
# there is RESURRECTED, so a `do-not-merge` or review label a human or a sibling labeler cleared in
# that instant comes back. The window opens ONLY on a run that actually changes the grade (an
# in-sync PR writes nothing at all) and is normally ~one API round-trip; on the FIRST grade in a
# repo the label probe and the pre-create sit inside it too, so enrollment widens it to about
# three. It is far narrower than the double-label window it replaces — that one lasted until the
# next grade — and unlike that one it cannot leave the PR self-contradictory. The loss is not
# silent, though it is easy to miss: GitHub records the drop on the PR's own timeline as an
# `unlabeled` event attributed to the grader token, so a dispute lost here is recoverable from the
# timeline rather than gone. Re-add the label if one lands in that instant. Note that no post-PUT
# verification read could do better: a label added INSIDE the window is absent from both the
# before-snapshot and the after-read, so the diff that would have to catch it is empty by
# construction.
#
# ONE MORE LIMIT, on a different axis: ownership is defined by the CURRENT LABEL_MAP plus the four
# default grade labels. A remap retires those defaults on the first re-grade. Custom names from an
# older map are unknowable and still need one-time repo-side cleanup.
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
#       must go red rather than pretend the label landed. Because the write is a single PUT there
#       is no partial-write state to reason about: the red check means "applied or not applied",
#       never "half applied". It does NOT mean "definitely not applied" — a client timeout, a
#       reset connection or a proxy 5xx can surface as a nonzero `gh` after GitHub committed the
#       replacement, so read the PR's labels before concluding the old set survived.

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
OWNED+=("risk:R0" "risk:R1" "risk:R2" "risk:R3")
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

# A label name is a PATH SEGMENT in the repo-label probe below, and GitHub label names legally
# contain spaces, `/`, `#`, `?` and `%`. A caller who remaps `R3=risk high` would otherwise build a
# malformed or misrouted URL: the probe misses, and a rename paints the check red or spams
# pre-creates. Encoded for the path only — the raw name is what we log, compare and send as a form
# field. (The label sync itself needs no encoding: its path carries only the PR number, and the
# label names ride in form fields.)
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
# returns 30 per page: on a PR with more than 30 labels a label past page one is invisible here,
# and this snapshot is what the PUT below is built from — a truncated read would both miss a stale
# grader-owned label and DELETE every unowned label that fell off page one.
#
# The names stay INSIDE JSON from here to the PUT — `--jq '[.[].name]'` per page, merged with
# `add`, never `--jq '.[].name'` into a newline-delimited round trip. A label name is user-supplied
# text and the PUT writes the whole set BACK, so a name that survived a split incorrectly would not
# merely skew a comparison: it would replace the real label with its fragments.
current="$(ghq api --paginate "repos/$REPO/issues/$PR_NUMBER/labels?per_page=100" --jq '[.[].name]' \
           | jq -sc 'add // []')" \
  || fail "could not read labels on $REPO#$PR_NUMBER: $(gherr)"

# GitHub label identity is CASE-INSENSITIVE (you cannot hold both `risk:R2` and `Risk:R2`), so every
# comparison against the snapshot has to be too. A case variant — an older LABEL_MAP spelling, or a
# hand-created label — otherwise reads as "not the target AND not owned": the in-sync short-circuit
# never fires, so every run issues the PUT (widening the residual window the header confines to
# grade-changing runs), and the variant is carried through the PUT beside the new target, which is
# the two-`risk:*`-labels state this shape exists to make impossible. Folding is ASCII-only, which
# is what jq offers and what every mapped name uses; a caller who maps a tier to a non-ASCII name
# and then hand-creates a case variant of it is outside what this can normalize.
#
# rc 0 = present, rc 1 = absent, ANYTHING ELSE = jq malfunctioned and there is NO answer. Treating
# an error as "absent" is the dangerous direction: a failure inside the owned-label loop below would
# leave in_sync=1, log "already labeled", exit 0, and leave a double-labeled PR unhealed forever.
has() {
  jq -e --arg l "$1" 'map(ascii_downcase) | index($l | ascii_downcase) != null' \
    >/dev/null 2>&1 <<<"$current"
  case "$?" in
    0) return 0 ;;
    1) return 1 ;;
    *) fail "label-membership check for '$1' on $REPO#$PR_NUMBER failed (jq error) — refusing to guess" ;;
  esac
}

# ASCII case folding for shell-side comparisons. Literal `A-Z`/`a-z` rather than the suggested
# [:upper:]/[:lower:] classes ON PURPOSE: this must fold EXACTLY what jq's ascii_downcase folds
# above, and a locale-dependent class would make the two disagree on non-ASCII names.
# shellcheck disable=SC2018,SC2019
lc() { printf '%s' "$1" | tr 'A-Z' 'a-z'; }

# In sync = the target is present AND no other owned label is. Short-circuit so the common case
# (re-grading a PR whose tier did not move) performs no write at all — which is also what keeps the
# read→PUT residual documented in the header confined to grade-CHANGING runs.
in_sync=0
if has "$TARGET"; then
  in_sync=1
  target_lc="$(lc "$TARGET")"
  for l in "${OWNED[@]}"; do
    # Case-folded, like every other comparison here: a LABEL_MAP that maps two tiers to case
    # variants of ONE name (they are the same GitHub label) would otherwise never skip the variant,
    # `has` would always match it, in_sync could never reach 1, and every run would issue the PUT.
    if [ "$(lc "$l")" != "$target_lc" ] && has "$l"; then in_sync=0; break; fi
  done
fi

if [ "$in_sync" = 1 ]; then
  log "already labeled '$TARGET' — nothing to do"
  printf '%s\n' "$TARGET"
  exit 0
fi

# Ensure the label exists in the repo first, so enrollment needs no manual label setup: a PUT
# creates the association but never the label's color or description. This probe keeps its own
# stderr rather than routing through ghq: a 404 here is the EXPECTED first-use answer, and leaving
# that 404 text in $ERRF would let a later failure misreport it.
#
# A NON-404 is a different animal and must not be silently read as "label does not exist": a 403
# (the caller granted pull-requests but not `issues: write`) or a transient 5xx would otherwise send
# the run down the pre-create path, fail there too, and surface only as a confusing PUT error later.
if ! probe_err="$(gh api "repos/$REPO/labels/$(enc "$TARGET")" 2>&1 >/dev/null)"; then
  case "$probe_err" in
    # Matched on gh's own wording rather than a bare `404`, which a label NAME could contain.
    *"HTTP 404"*|*"Not Found"*)
      ghq api -X POST "repos/$REPO/labels" \
        -f name="$TARGET" -f color="$(color_for "$TIER")" \
        -f description="PR risk grade (advisory shadow check; grader-owned)" >/dev/null \
        || log "label '$TARGET' could not be pre-created (may already exist): $(gherr) — trying the sync anyway; if the PUT auto-creates it, the label lands with a default color and no description and NO later run will fix that (the probe will find it and skip this create), so set it once by hand: gh label edit '$TARGET' -R $REPO --color $(color_for "$TIER")"
      ;;
    *)
      log "label probe for '$TARGET' failed with a NON-404 — this is the error to chase, not anything the sync reports next: $(printf '%s' "$probe_err" | tr '\n' ' ')"
      ;;
  esac
fi

# THE ONE WRITE — and it is literally one request. `PUT .../labels` REPLACES the PR's whole label
# set, so the desired set is "every label we do not own, plus the target". There is no window
# between removing the stale grade and adding the new one for a concurrent run to interleave into;
# see the ownership contract at the top for why that is the property this shape exists to buy.
#
# Do NOT "optimize" this into a POST when no stale label is present: two runs that both read an
# empty owned set would both POST, and the PR ends up with both their labels — the exact race this
# closes. And do NOT wrap it in a verify-and-retry loop: concurrent writers would just ping-pong,
# and the exactly-one invariant already holds after ANY single writer's PUT.
#
# The empty-labels footgun of this endpoint (a PUT with an empty array strips every label) is
# closed at both ends: the array below always ends with "$TARGET", and "$TARGET" cannot be empty
# because LABEL_MAP validation above rejects a tier mapped to an empty name before any write.
#
# EVERY jq call here is status-checked, and none may run inside a process substitution. `set -e` is
# off and `pipefail` does not reach `< <(...)`, so a failing filter there — `--argjson` rejecting a
# malformed owned_json, an OOM, a jq that is not on PATH — would append NOTHING, silently degrade
# this destructive full-set replace to `labels[]=$TARGET` alone, DELETE every unowned label on the
# PR, and still log "synced" and exit 0. A replace this total must refuse to run unless the set it
# is replacing with is known-good. The names also stay inside JSON (built with `--args`, indexed out
# one at a time) rather than round-tripping through newline-delimited text, for the reason at the
# snapshot read: what is written back must be the name that was read, not a fragment of it.
owned_json="$(jq -nc '$ARGS.positional' --args "${OWNED[@]}")" \
  || fail "could not build the owned-label set (jq failed) — refusing to PUT a partial label set"
carry_json="$(jq -c --argjson owned "$owned_json" \
                '($owned | map(ascii_downcase)) as $o
                 | map(select((ascii_downcase) as $x | ($o | index($x)) == null))' <<<"$current")" \
  || fail "could not compute the carry-through labels for $REPO#$PR_NUMBER (jq failed) — refusing to PUT a partial label set"
carry_n="$(jq 'length' <<<"$carry_json")" \
  || fail "could not size the carry-through labels for $REPO#$PR_NUMBER (jq failed) — refusing to PUT a partial label set"

put_args=()
for ((i = 0; i < carry_n; i++)); do
  l="$(jq -r --argjson i "$i" '.[$i]' <<<"$carry_json")" \
    || fail "could not read carry-through label $i for $REPO#$PR_NUMBER (jq failed) — refusing to PUT a partial label set"
  put_args+=(-f "labels[]=$l")
done
put_args+=(-f "labels[]=$TARGET")

ghq api -X PUT "repos/$REPO/issues/$PR_NUMBER/labels" "${put_args[@]}" >/dev/null \
  || fail "could not sync labels on $REPO#$PR_NUMBER to '$TARGET': $(gherr) — the write is a single atomic PUT, so it applied fully or not at all, never half; a failed request does not prove WHICH (a timeout or proxy 5xx can land after GitHub committed it), so re-read the PR's labels before assuming the previous set survived"

log "synced to '$TARGET' ($(( ${#put_args[@]} / 2 )) labels on the PR)"

printf '%s\n' "$TARGET"
exit 0
