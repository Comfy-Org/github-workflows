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
#   * AN AXIS BECOMES A TAUTOLOGY. Rebind `WORKFLOWS_REF` to `${{ github.job_workflow_sha }}`
#     and the lock-step test compares the resolved SHA with itself — always green, while the
#     checkout below still uses the unvalidated input. Nothing about the guard's SHAPE changes.
#   * THE STEP IS NEUTERED WHOLESALE. `continue-on-error: true` makes every `exit 1` advisory
#     and an `if:` switches the step off — added to both copies they stay byte-identical, every
#     assertion above still holds, and the checkout proceeds on an unvalidated ref. Or the
#     cheaper version, which leaves the guard untouched and edits its VICTIM: `if: always()` on
#     the checkout, or `continue-on-error` at job level.
#   * THE INPUT IS ALIASED PAST THE SCAN. The unguarded-checkout scan matches `ref:` keys naming
#     `inputs.workflows_ref`. Bind it to an `env:` key first, forward it to a composite action,
#     or hand it to a `git fetch` in a `run:` step, and the scan has nothing left to see.
#   * THE RAW VALUE IS EMITTED AGAIN. The runner re-parses any line of step output, so a
#     multi-line ref can forge workflow commands in a public log — including one hop through
#     another variable, which is why the emit scan whitelists what may touch the value rather
#     than blacklisting the emit shapes someone thought of.
#
# ASSERT PROPERTIES, NOT COUNTS, AND POSITIONS, NOT SUMS. An earlier draft pinned a literal
# number of `exit 1` lines, which would have gone red on the very next hardening of the guard —
# a test that blocks its own subject's improvement teaches people to delete the test. A later
# one compared total `::error::` and `exit 1` counts, which one path could satisfy on another's
# behalf. What is asserted here is that no rejection path can be non-fatal (no `exit 0` at all)
# and that each `::error::` is FOLLOWED by an exit. The awk passes also self-check that they
# matched anything, so a brittle anchor fails loudly rather than passing vacuously with zero
# coverage — and the patterns are deliberately over-inclusive, since a false positive here costs
# a puzzled minute and a false negative ships an unguarded checkout.
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
# `grep -F`, not `case` globbing: half these needles are regex text (`^[0-9a-fA-F]{40}$`), whose
# brackets and braces a glob would happily reinterpret into something laxer than it reads.
has() { if printf '%s\n' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "not found: $3"; fi }
no()  { if printf '%s\n' "$2" | grep -qF -- "$3"; then bad "$1" "present: $3"; else ok "$1"; fi }

GUARD_NAME='      - name: Enforce workflows_ref pin contract'

# --- every `workflows_ref` checkout is preceded, in its own job, by the guard -----------------
# Job boundaries are the 2-space-indented `<name>:` keys under `jobs:`; the guard flag resets at
# each one, so a guard in `gate` cannot vouch for a checkout in `grade`. Checkout detection is
# deliberately NOT a byte-exact line match: a future job that quotes the expression, drops the
# inner spaces, indents differently, or adds a trailing comment must still be seen, or an
# unguarded checkout ships green and defeats the point of this file. So: strip whitespace, then
# match any `ref:` key mentioning `inputs.workflows_ref` ANYWHERE in the value. Deliberately
# over-inclusive — a false positive here costs one puzzled minute, a false negative ships an
# unguarded checkout. The awk also reports how many jobs and how many refs it saw, so a
# pattern that silently matches nothing fails below rather than printing a vacuous `ok`.
scan="$(awk -v guard="$GUARD_NAME" '
  function squash(s) { gsub(/[[:space:]]/, "", s); return s }
  /^jobs:[[:space:]]*(#.*)?$/ { injobs = 1; next }
  !injobs { next }
  /^  [A-Za-z0-9_-]+:[[:space:]]*(#.*)?$/ { job = $1; jobs += 1; guarded = 0; next }
  $0 == guard { guarded = 1; next }
  squash($0) ~ /^ref:.*inputs\.workflows_ref/ {
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
# Without `set -e` and without `nullglob`, a slicer that produced nothing would leave the loop
# below iterating the literal glob and `cmp`-ing a nonexistent file — the byte-identity check
# would dissolve into a confusing message instead of a clean failure. Count first.
nslices=$(find "$copies" -maxdepth 1 -name 'guard.*' -type f | wc -l | tr -d ' ')
if [ "$nslices" -ge 2 ]; then ok "the guard slicer produced one slice per copy ($nslices)"
else bad "the guard slicer produced one slice per copy" "$nslices — nothing to compare"; fi

drift=""
first="$copies/guard.1"
if [ -f "$first" ]; then
  for f in "$copies"/guard.*; do
    cmp -s "$first" "$f" || drift="$drift $(basename "$f")"
  done
else
  drift="no slices"
fi
eq "all copies of the guard step are byte-identical" "" "$drift"

# --- both axes are still enforced, and no rejection path can be non-fatal ---------------------
body="$(cat "$first" 2>/dev/null)"
# WHOLE-LINE comments are dropped before any assertion about control flow, so an `exit 1` or an
# `exit 0` quoted in prose neither satisfies nor breaks a check. Deliberately NOT a trailing-`#`
# strip: `#` has no special meaning inside a shell string, and this repo routinely writes
# `github-workflows#NN` and the like in exactly the annotation lines below — a naive
# `s/[[:space:]]*#.*$//` would truncate such a line and could silently drop the `$WORKFLOWS_REF`
# mention that the emit scan exists to inspect. The residual cost runs the other, harmless way:
# a trailing comment that happens to say `exit 0` trips a check. False positive, one puzzled
# minute — the trade this file makes everywhere.
code="$(printf '%s\n' "$body" | grep -v '^[[:space:]]*#')"

# The value under test must be the caller's INPUT, not a restatement of the runner's own. Rebind
# it to `${{ github.job_workflow_sha }}` and the lock-step comparison compares the resolved SHA
# with itself — a tautology that always passes while the checkout below still uses the
# unvalidated input, with every other assertion in this file staying green.
has "the guarded value is the caller's input, not the resolved SHA restated" \
    "$code" 'WORKFLOWS_REF: ${{ inputs.workflows_ref }}'
has "axis 1: the ref must be shaped like a full 40-hex commit SHA" \
    "$code" '^[0-9a-fA-F]{40}$'
has "axis 2: the runner-supplied resolved SHA is read into the guard" \
    "$code" 'JOB_WORKFLOW_SHA: ${{ github.job_workflow_sha }}'
has "axis 2: the pin is compared against it, case-insensitively" \
    "$code" '"${WORKFLOWS_REF,,}" != "${JOB_WORKFLOW_SHA,,}"'
has "axis 2: an unreadable job_workflow_sha is itself a rejection (fail closed)" \
    "$code" '[[ -z "$JOB_WORKFLOW_SHA" ]]'

# Every rejection is fatal, expressed without pinning a literal count: nothing in the guard may
# exit successfully mid-way, and each error annotation must be followed by a failing exit. A new
# axis therefore extends this cleanly instead of turning it red.
no  "no rejection path exits non-fatally" "$code" "exit 0"
# Positionally, not by totals. Equal SUMS would let one path degrade to log-and-continue so long
# as another gained a spare `exit 1` — which is exactly the regression this is here to catch, so
# each `::error::` must be followed by `exit 1` as the next non-blank line.
unpaired="$(printf '%s\n' "$code" | awk '
  /^[[:space:]]*$/ { next }
  pending && $0 !~ /^[[:space:]]*exit 1[[:space:]]*$/ { print "UNPAIRED:" NR; pending = 0 }
  { pending = /::error::/ }
  END { if (pending) print "UNPAIRED:eof" }
' | tr '\n' ' ' | sed 's/ $//')"
eq "every ::error:: is followed by a failing exit" "" "$unpaired"
errors=$(printf '%s\n' "$code" | grep -c '::error::')
if [ "$errors" -ge 3 ]; then ok "all three rejection paths are present (shape, unreadable, mismatch)"
else bad "all three rejection paths are present (shape, unreadable, mismatch)" "$errors ::error:: lines"; fi

# --- the guard cannot be neutered while staying byte-identical --------------------------------
# The cheapest way to disarm this without tripping any check above is a step-level key:
# `continue-on-error: true` makes the `exit 1` advisory, and an `if:` can switch the whole step
# off. Added to BOTH copies they stay identical, every error stays paired, and the checkout
# proceeds with an unvalidated ref. So the step must carry neither.
no "the guard is not softened by continue-on-error" "$code" "continue-on-error"
gatedon="$(printf '%s\n' "$code" | grep -E '^        if:' || true)"
eq "the guard is not conditional (no step-level if:)" "" "$gatedon"

# --- nor can the step it PROTECTS be made to run anyway ---------------------------------------
# Everything above inspects the guard. The cheaper bypass leaves the guard byte-identical and
# edits its victim instead: `if: always()` (or `success() || failure()`) on the checkout makes it
# run after the guard has exited 1, and a job-level `continue-on-error: true` demotes the guard's
# failure for the whole job. Either one and the checkout proceeds on an unvalidated ref with
# every assertion in this file still green — so the protected step is checked too.
CHECKOUT_NAME='      - name: Load pr-risk tool'
protected="$(awk -v step="$CHECKOUT_NAME" '
  $0 == step { instep = 1; print; next }
  !instep { next }
  /^[[:space:]]*$/ { next }
  /^       / { print; next }
  { instep = 0 }
' "$WF" | grep -v '^[[:space:]]*#')"
nprotected=$(printf '%s\n' "$protected" | grep -cxF "$CHECKOUT_NAME")
if [ "$nprotected" = "$nrefs" ]; then ok "the protected-checkout scan matched every guarded checkout ($nprotected)"
else bad "the protected-checkout scan matched every guarded checkout" "$nprotected of $nrefs — anchors are stale, coverage is vacuous"; fi
no "the protected checkout is not run-anyway (no step-level if:)" "$protected" "if:"
no "the protected checkout is not softened by continue-on-error" "$protected" "continue-on-error"
# Job level, where one key covers the guard and its checkout at once. `if:` at this indent is
# legitimate (`grade` is gated on enablement); `continue-on-error` never is.
jobsoft="$(grep -nE '^    continue-on-error:' "$WF" | tr '\n' ' ' | sed 's/ $//')"
eq "no job demotes its own failures wholesale (no job-level continue-on-error)" "" "$jobsoft"

# --- the input itself is never interpolated into the shell nor emitted raw --------------------
# `${{ inputs.workflows_ref }}` inline in `run:` would be a shell-injection vector, and emitting
# the raw value lets a multi-line value forge workflow commands in a public log — the runner
# re-parses ANY line of step output, so this is not only about `echo` and not only about lines
# that themselves contain `::`. Every line naming the value must therefore be one that consumes
# it (a `[[ ]]` test, or the assignment that sanitizes it), never one that emits it: no bare
# `echo`/`printf`, no workflow command, no redirect into an Actions file, and no continuation of
# a line that was doing one of those.
script="$(printf '%s\n' "$code" | sed -n '/run: |/,$p')"
# Unlike every other anchor here, this one had no coverage self-check: reshape the block scalar
# (`run: >-`, or a `run:` with a trailing comment) and `$script` comes back EMPTY, at which point
# both assertions below pass over nothing at all — the vacuous-pass mode this file's header
# claims to have eliminated. Anchor on a line the guard's script must contain.
has "the run: block scan found the guard's script body" "$script" 'set -euo pipefail'
# WHITELIST, not blacklist. The named categories below are diagnostics — they say WHICH way a
# line leaks — but the verdict is the `else`: the raw value may be read by the sanitizing
# assignment and by the `[[ ]]` tests, and by NOTHING else. A blacklist of emit-shapes has a
# one-hop hole (`raw=$WORKFLOWS_REF` on one line, `echo "$raw"` on the next, and every shape rule
# sees nothing) and an open-ended tail of spellings to keep chasing — `export`, `local`, `read`,
# a herestring, a here-doc. Inverting it is what lets the assertion's name be true: any new way
# of touching the value is reported until someone widens this list on purpose.
emitted="$(printf '%s\n' "$script" | awk '
  { line = $0; sub(/^[[:space:]]+/, "", line) }
  line ~ /\$\{?WORKFLOWS_REF/ {
    sanctioned = (!cont && (line ~ /^safe_ref=\$\(printf/ || line ~ /^(el)?if \[\[ /))
    if (!sanctioned) {
      if (cont)                             { print "CONTINUATION:" NR }
      else if (line ~ /::/)                 { print "WORKFLOW-COMMAND:" NR }
      else if (line ~ /GITHUB_(STEP_SUMMARY|OUTPUT|ENV|PATH)/) { print "ACTIONS-FILE:" NR }
      else if (line ~ /^(echo|printf)[[:space:]]/)             { print "BARE-EMIT:" NR }
      else if (line ~ /[^0-9a-zA-Z_]>>?[[:space:]]*[\$\/"]/)   { print "REDIRECT:" NR }
      else                                  { print "UNSANCTIONED:" NR }
    }
  }
  { cont = (line ~ /\\$/) }
' | tr '\n' ' ' | sed 's/ $//')"
eq "the raw value is only sanitized or tested, never emitted or copied elsewhere" "" "$emitted"
no "the input reaches the script only via env:" "$script" '${{'
has "the sanitized value is what the annotations use" "$code" '${safe_ref}'

# --- ...and the input is never aliased out from under the checkout scan ------------------------
# The "every workflows_ref checkout sits behind the guard" scan at the top matches literal `ref:`
# keys naming `inputs.workflows_ref`. Bind the input to something else first — `env: REF: ${{
# inputs.workflows_ref }}` then `ref: ${{ env.REF }}`, or forward it to a composite action's
# `with:`, or hand it to a `git fetch`/`gh api` in a `run:` step — and that scan sees no `ref:`
# to check, `REFS` stays where it was, and an unguarded fetch of an unvalidated ref ships green.
# Closing that by teaching the scan to follow aliases is a dataflow problem; forbidding the alias
# is a grep. So: OUTSIDE comments, `inputs.workflows_ref` may appear only as the guard's own env
# binding or as a `ref:` key — the two shapes the scans above actually understand. A future job
# that legitimately needs it elsewhere adds the shape here, deliberately, rather than by accident.
aliased="$(awk '
  { line = $0; sub(/^[[:space:]]*/, "", line) }
  line ~ /^#/ { next }
  line !~ /inputs\.workflows_ref/ { next }
  { squashed = line; gsub(/[[:space:]]/, "", squashed) }
  squashed ~ /^WORKFLOWS_REF:\$\{\{inputs\.workflows_ref\}\}$/ { next }
  squashed ~ /^ref:/ { next }
  { print "ALIASED:" NR }
' "$WF" | tr '\n' ' ' | sed 's/ $//')"
eq "the input is referenced only as the guard's env binding or a ref: key" "" "$aliased"
nmentions=$(awk '
  { line = $0; sub(/^[[:space:]]*/, "", line) }
  line ~ /^#/ { next }
  line ~ /inputs\.workflows_ref/ { n += 1 }
  END { print n+0 }
' "$WF")
want=$((nrefs * 2))
if [ "$nmentions" -ge "$want" ]; then ok "the alias scan saw one env binding and one ref: per guarded job ($nmentions)"
else bad "the alias scan saw one env binding and one ref: per guarded job" "$nmentions, expected >= $want — anchors are stale, coverage is vacuous"; fi

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
