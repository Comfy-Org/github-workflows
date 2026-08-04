#!/usr/bin/env bash
# Almost every literal below is a fragment of the YAML under inspection, so `${{ }}` and
# `${VAR}` are the text being tested and must NOT expand. File-wide, hence up here.
# shellcheck disable=SC2016
# test_pin_contract.sh — hermetic structural tests for the `workflows_ref` pin guard in
# .github/workflows/pr-risk.yml. No network, no Actions: this reads the workflow file as text.
#
# The guard is the trust boundary of the whole workflow — it is what stops the grader being
# loaded from a mutable ref, or from a fork-authored commit of this PUBLIC repo, into a job
# holding the caller's `pull-requests: write` token. It lives in the WORKFLOW rather than in a
# script (a script-side check would sit inside the blast radius it bounds), which means the
# normal script suites cannot cover it. Nothing else in CI would notice these regressions:
#
#   * A NEW JOB CHECKS OUT `workflows_ref` WITHOUT THE GUARD. The invariant "every job that
#     checks it out re-asserts this itself" is stated in a comment and held up by hand-copying.
#     A job added later that skips the guard silently loses the boundary for that job.
#   * THE COPIES DRIFT. They are byte-identical on purpose; one-character drift between them
#     (a loosened regex, a dropped `exit 1`) would leave one job weaker than the other with no
#     visible symptom.
#   * THE GUARD STOPS BEING FIRST. It only bounds what it precedes — a guard after the checkout
#     it protects is decoration.
#   * AN AXIS IS DROPPED. Shape alone proves the ref is immutable, not whose commit it is; the
#     `github.job_workflow_sha` comparison is what proves it is this repo's reviewed code.
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

GUARD_NAME='      - name: Enforce workflows_ref pin contract'
CHECKOUT_REF='          ref: ${{ inputs.workflows_ref }}'

# --- every `workflows_ref` checkout is preceded, in its own job, by the guard -----------------
# Job boundaries are the 2-space-indented `<name>:` keys under `jobs:`; the guard flag resets at
# each one, so a guard in `gate` cannot vouch for a checkout in `grade`.
unguarded="$(awk -v guard="$GUARD_NAME" -v ref="$CHECKOUT_REF" '
  /^jobs:$/            { injobs = 1; next }
  injobs && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ { job = $1; guarded = 0 }
  injobs && $0 == guard { guarded = 1 }
  injobs && $0 == ref  { if (!guarded) print job }
' "$WF")"
eq "every workflows_ref checkout sits behind the guard, in its own job" "" "$unguarded"

guards=$(grep -cxF "$GUARD_NAME" "$WF")
checkouts=$(grep -cxF "$CHECKOUT_REF" "$WF")
eq "one guard per workflows_ref checkout" "$checkouts" "$guards"
if [ "$guards" -ge 2 ]; then
  ok "the guard is restated per job rather than centralized ($guards copies)"
else
  bad "the guard is restated per job rather than centralized" "$guards copies"
fi

# --- the copies have not drifted --------------------------------------------------------------
# Slice each guard from its `- name:` line up to the next step OR the comment block introducing
# it (both live at 6-space indent; the guard's own body is indented deeper), then compare every
# copy against the first.
copies="$(mktemp -d "${TMPDIR:-/tmp}/pr-risk-pin.XXXXXX")"
trap 'rm -rf "$copies"' EXIT
awk -v guard="$GUARD_NAME" -v out="$copies" '
  $0 == guard { n += 1; f = out "/guard." n; inguard = 1; print > f; next }
  inguard && /^      [-#]/ { inguard = 0 }
  inguard { print > f }
' "$WF"
drift=""
first="$copies/guard.1"
for f in "$copies"/guard.*; do
  cmp -s "$first" "$f" || drift="$drift $(basename "$f")"
done
eq "all copies of the guard step are byte-identical" "" "$drift"

# --- both axes are still enforced, and enforced fatally ---------------------------------------
body="$(cat "$first")"
case "$body" in
  *'^[0-9a-fA-F]{40}$'*) ok "axis 1: the ref must be shaped like a full 40-hex commit SHA" ;;
  *) bad "axis 1: the ref must be shaped like a full 40-hex commit SHA" "regex missing//changed" ;;
esac
case "$body" in
  *'JOB_WORKFLOW_SHA: ${{ github.job_workflow_sha }}'*)
    ok "axis 2: the runner-supplied resolved SHA is read into the guard" ;;
  *) bad "axis 2: the runner-supplied resolved SHA is read into the guard" "env var missing" ;;
esac
case "$body" in
  *'"${WORKFLOWS_REF,,}" != "${JOB_WORKFLOW_SHA,,}"'*)
    ok "axis 2: the pin is compared against it, case-insensitively" ;;
  *) bad "axis 2: the pin is compared against it, case-insensitively" "comparison missing" ;;
esac
eq "each rejection is fatal (both axes exit 1)" "2" "$(printf '%s\n' "$body" | grep -c 'exit 1')"

# --- the input itself is never interpolated into the shell or echoed raw ----------------------
# `${{ inputs.workflows_ref }}` inline in `run:` would be a shell-injection vector, and echoing
# the raw value into a `::error::` lets a multi-line value forge workflow commands in a public log.
case "$body" in
  *'run: |'*'${{'*) bad "the input reaches the script only via env:" "\${{ }} inside run:" ;;
  *) ok "the input reaches the script only via env:" ;;
esac
case "$body" in
  *'${WORKFLOWS_REF}'*) bad "only the sanitized value is echoed into annotations" "raw value echoed" ;;
  *) ok "only the sanitized value is echoed into annotations" ;;
esac

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
