#!/usr/bin/env bash
# Almost every literal below is a fragment of the YAML under inspection, so `${{ }}` and
# `${VAR}` are the text being tested and must NOT expand. File-wide, hence up here.
# shellcheck disable=SC2016
# test_pin_contract.sh — hermetic structural tests for the `workflows_ref` pin guard in
# .github/workflows/pr-risk.yml. No network, no Actions: this reads the workflow file as text.
#
# The guard is the trust boundary of the whole workflow — it is what stops the grader being
# loaded from a mutable ref, or from a revision other than the one running, into a job holding
# the caller's `pull-requests: write` token. It lives in the WORKFLOW rather than in a script (a
# script-side check would sit inside the blast radius it bounds), which means the normal script
# suites cannot cover it. Nothing else in CI would notice these regressions:
#
#   * A NEW JOB CHECKS OUT `workflows_ref` WITHOUT THE GUARD. The invariant "every job that
#     checks it out re-asserts this itself" is stated in a comment and held up by hand-copying.
#     A job added later that skips the guard silently loses the boundary for that job.
#   * THE COPIES DRIFT. They are byte-identical on purpose; one-character drift between them
#     (a loosened regex, a dropped `exit 1`) would leave one job weaker than the other with no
#     visible symptom.
#   * THE GUARD STOPS BEING FIRST. It only bounds what it precedes — a guard after the checkout
#     it protects is decoration.
#   * AN AXIS IS DROPPED, OR STOPS BEING FATAL. Shape alone proves the ref is immutable, not
#     which commit it is; the `github.job_workflow_sha` comparison is what proves it is the
#     revision already running. Either axis degraded to a warning is a silent no-op.
#
# ASSERT PROPERTIES, NOT COUNTS. An earlier draft pinned a literal number of `exit 1` lines,
# which would have gone red on the very next hardening of the guard — a test that blocks its own
# subject's improvement teaches people to delete the test. What is asserted here instead is that
# no rejection path can be non-fatal (no `exit 0` at all) and that every `::error::` is paired
# with an exit. The awk passes also self-check that they matched anything, so a brittle anchor
# fails loudly rather than passing vacuously with zero coverage.
#
#   bash tests/test_pin_contract.sh          # exit 0 = all green
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WF="$SELF_DIR/../../../.github/workflows/pr-risk.yml"
[ -f "$WF" ] || { echo "FATAL: $WF not found" >&2; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi }
has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "not found: $3" ;; esac }
no()  { case "$2" in *"$3"*) bad "$1" "present: $3" ;; *) ok "$1" ;; esac }

GUARD_NAME='      - name: Enforce workflows_ref pin contract'

# --- every `workflows_ref` checkout is preceded, in its own job, by the guard -----------------
# Job boundaries are the 2-space-indented `<name>:` keys under `jobs:`; the guard flag resets at
# each one, so a guard in `gate` cannot vouch for a checkout in `grade`. Checkout detection is
# deliberately NOT a byte-exact line match: a future job that quotes the expression, drops the
# inner spaces, indents differently, or adds a trailing comment must still be seen, or an
# unguarded checkout ships green and defeats the point of this file. Match on the whitespace-
# stripped form instead. The awk also reports how many jobs and how many refs it saw, so a
# pattern that silently matches nothing fails below rather than printing a vacuous `ok`.
scan="$(awk -v guard="$GUARD_NAME" '
  function squash(s) { gsub(/[[:space:]]/, "", s); return s }
  /^jobs:[[:space:]]*(#.*)?$/ { injobs = 1; next }
  !injobs { next }
  /^  [A-Za-z0-9_-]+:[[:space:]]*(#.*)?$/ { job = $1; jobs += 1; guarded = 0; next }
  $0 == guard { guarded = 1; next }
  squash($0) ~ /^ref:\$\{\{inputs\.workflows_ref\}\}$/ {
    refs += 1
    if (!guarded) print "UNGUARDED:" job
  }
  END { print "JOBS:" jobs+0; print "REFS:" refs+0 }
' "$WF")"

eq "every workflows_ref checkout sits behind the guard, in its own job" \
   "" "$(printf '%s\n' "$scan" | grep '^UNGUARDED:' | tr '\n' ' ' | sed 's/ $//')"

# The two coverage self-checks: without them the assertion above is `ok` when the anchors match
# nothing at all, which is the failure mode a structural test is most prone to.
njobs="$(printf '%s\n' "$scan" | sed -n 's/^JOBS://p')"
nrefs="$(printf '%s\n' "$scan" | sed -n 's/^REFS://p')"
if [ "${njobs:-0}" -ge 2 ]; then ok "the job scan matched the workflow's jobs ($njobs)"
else bad "the job scan matched the workflow's jobs" "$njobs — anchors are stale, coverage is vacuous"; fi
if [ "${nrefs:-0}" -ge 2 ]; then ok "the checkout scan matched the workflows_ref checkouts ($nrefs)"
else bad "the checkout scan matched the workflows_ref checkouts" "$nrefs — anchors are stale, coverage is vacuous"; fi

guards=$(grep -cxF "$GUARD_NAME" "$WF")
eq "one guard per workflows_ref checkout" "$nrefs" "$guards"
if [ "$guards" -ge 2 ]; then
  ok "the guard is restated per job rather than centralized ($guards copies)"
else
  bad "the guard is restated per job rather than centralized" "$guards copies"
fi

# --- the copies have not drifted --------------------------------------------------------------
# Slice each guard from its `- name:` line to the first following line that is NOT indented
# deeper than it — the next step, the comment block introducing one, or the next job's key. A
# terminator that only recognized sibling steps would run past the end of a job whose LAST step
# is the guard and swallow the following job.
copies="$(mktemp -d "${TMPDIR:-/tmp}/pr-risk-pin.XXXXXX")" || { echo "FATAL: mktemp failed" >&2; exit 1; }
[ -d "$copies" ] || { echo "FATAL: mktemp produced no directory" >&2; exit 1; }
trap 'rm -rf "$copies"' EXIT
awk -v guard="$GUARD_NAME" -v out="$copies" '
  $0 == guard { n += 1; f = out "/guard." n; inguard = 1; print > f; next }
  !inguard { next }
  /^[[:space:]]*$/ { print > f; next }
  /^       / { print > f; next }
  { inguard = 0 }
' "$WF"
drift=""
first="$copies/guard.1"
for f in "$copies"/guard.*; do
  cmp -s "$first" "$f" || drift="$drift $(basename "$f")"
done
eq "all copies of the guard step are byte-identical" "" "$drift"

# --- both axes are still enforced, and no rejection path can be non-fatal ---------------------
body="$(cat "$first")"
# Comments are stripped before any assertion about control flow, so an `exit 1` or an `exit 0`
# quoted in prose neither satisfies nor breaks a check.
code="$(printf '%s\n' "$body" | sed 's/[[:space:]]*#.*$//')"

has "axis 1: the ref must be shaped like a full 40-hex commit SHA" \
    "$code" '^[0-9a-fA-F]{40}$'
has "axis 2: the runner-supplied resolved SHA is read into the guard" \
    "$body" 'JOB_WORKFLOW_SHA: ${{ github.job_workflow_sha }}'
has "axis 2: the pin is compared against it, case-insensitively" \
    "$code" '"${WORKFLOWS_REF,,}" != "${JOB_WORKFLOW_SHA,,}"'
has "axis 2: an unreadable job_workflow_sha is itself a rejection (fail closed)" \
    "$code" '[[ -z "$JOB_WORKFLOW_SHA" ]]'

# Every rejection is fatal, expressed without pinning a literal count: nothing in the guard may
# exit successfully mid-way, and each error annotation must be paired with a failing exit. A new
# axis therefore extends this cleanly instead of turning it red.
no  "no rejection path exits non-fatally" "$code" "exit 0"
errors=$(printf '%s\n' "$code" | grep -c '::error::')
fatals=$(printf '%s\n' "$code" | grep -c 'exit 1')
eq  "every ::error:: is paired with a failing exit" "$errors" "$fatals"
if [ "$errors" -ge 3 ]; then ok "all three rejection paths are present (shape, unreadable, mismatch)"
else bad "all three rejection paths are present (shape, unreadable, mismatch)" "$errors ::error:: lines"; fi

# --- the input itself is never interpolated into the shell or echoed raw ----------------------
# `${{ inputs.workflows_ref }}` inline in `run:` would be a shell-injection vector, and echoing
# the raw value into a `::error::` lets a multi-line value forge workflow commands in a public
# log. The echo check matches the value in ANY spelling — `$WORKFLOWS_REF` unbraced is the more
# likely regression than `${WORKFLOWS_REF}`, so it must not be the one the test misses.
script="$(printf '%s\n' "$code" | sed -n '/run: |/,$p')"
no "the input reaches the script only via env:" "$script" '${{'
echoed="$(printf '%s\n' "$code" | grep -E '^[[:space:]]*echo ' | grep -E '\$\{?WORKFLOWS_REF' || true)"
eq "only the sanitized value is echoed into annotations" "" "$echoed"
has "the sanitized value is what the annotations use" "$code" '${safe_ref}'

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
