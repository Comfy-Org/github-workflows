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
#   * THE CHECK IS WEAKENED, IN ANY OF THE WAYS A PATTERN SCAN CANNOT ENUMERATE. Loosen the
#     character class, drop the length test, wrap the strict test in a permissive branch
#     (`if <7-hex> then accept; else <the 40-hex check>; fi`), rebind `WORKFLOWS_REF` to
#     something other than the input, emit the raw value in a new spelling (`printenv`, `env`,
#     `declare -p`, `set -x`), or add a second emitting statement after a sanctioned prefix.
#   * THE STEP IS NEUTERED WHOLESALE. `continue-on-error: true` makes every `exit 1` advisory
#     and an `if:` switches the step off — added to both copies they stay byte-identical, every
#     assertion above still holds, and the checkout proceeds on an unvalidated ref. Or the
#     cheaper version, which leaves the guard untouched and edits its VICTIM: `if: always()` on
#     the checkout, or `continue-on-error` at job level.
#   * THE INPUT IS ALIASED PAST THE SCAN. The unguarded-checkout scan matches `ref:` keys naming
#     `inputs.workflows_ref`. Bind it to an `env:` key first, forward it to a composite action,
#     or hand it to a `git fetch` in a `run:` step, and the scan has nothing left to see.
#
# THE GUARD'S EXECUTABLE BODY IS PINNED VERBATIM, and that is deliberate. Earlier drafts of this
# file tried to characterize the body instead — assert both axes are present, assert every
# `::error::` is followed by an `exit 1`, whitelist which statements may touch the value — and
# each round of review found another way through, because every one of them was a pattern over an
# open-ended language: the whitelist cleared a whole `;`-delimited statement on its PREFIX (so
# `if [[ -n "$REF" ]] && echo "$REF" >> "$GITHUB_STEP_SUMMARY"` passed), its TRIGGER was itself a
# blacklist (so `printenv WORKFLOWS_REF` and `set -euxo pipefail` were invisible), and a `has`
# needle proves a test is PRESENT, never that it is the only path to the checkout. A trust
# boundary this small — nine executable lines — is better served by an equality: the body is what
# it is below, or the build is red. Widening it is then a deliberate two-place edit whose diff a
# reviewer sees, which is the property all those scans were reaching for.
#
# STRUCTURE IS PINNED; PROSE IS NOT. The `::error::` message is free to be reworded (it is
# canonicalized away before the comparison) but is separately checked to expand nothing but the
# sanitized copy — otherwise a wording tweak would fail this test for no security reason, while
# `echo "::error::$WORKFLOWS_REF"` would slip past a prose-blind pin. Everything AROUND the body —
# which jobs have the guard, whether it precedes its checkout, whether the copies agree, whether
# either has been neutered — stays a property assertion, and each of those scans self-checks that
# it matched anything, so a stale anchor fails loudly rather than passing vacuously.
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
# `grep -F`, not `case` globbing: these needles are workflow and shell syntax (`${{`, `[[ `),
# whose brackets and braces a glob would happily reinterpret into something laxer than it reads.
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

# --- the guard's executable body is exactly this -----------------------------------------------
body="$(cat "$first" 2>/dev/null)"
# WHOLE-LINE comments are dropped before any assertion about control flow, so an `exit 1` or an
# `exit 0` quoted in prose neither satisfies nor breaks a check. Deliberately NOT a trailing-`#`
# strip: `#` has no special meaning inside a shell string, and this repo routinely writes
# `github-workflows#NN` and the like in exactly the annotation lines below — a naive
# `s/[[:space:]]*#.*$//` would truncate such a line and could silently drop the `$WORKFLOWS_REF`
# mention the pin below exists to inspect. The residual cost runs the other, harmless way: a
# trailing comment that happens to say `exit 1` becomes part of the pinned text. False positive,
# one puzzled minute — the trade this file makes everywhere.
code="$(printf '%s\n' "$body" | grep -v '^[[:space:]]*#')"

# THE EQUALITY. Every property the scans in earlier drafts reached for — the input is the value
# under test and not a restatement of something else, the shape test is the ONLY path to the
# checkout rather than merely a present one, no branch accepts early, no statement emits the raw
# value under any spelling, `set -euo pipefail` has not grown an `x` — is a consequence of the
# body being exactly this and nothing else. See the header for why an equality rather than yet
# another pattern. To CHANGE the guard, change it here too: that second edit is the point.
expect="$(cat <<'PINNED'
      - name: Enforce workflows_ref pin contract
        env:
          WORKFLOWS_REF: ${{ inputs.workflows_ref }}
        run: |
          set -euo pipefail
          safe_ref=$(printf '%s' "$WORKFLOWS_REF" | tr -c 'A-Za-z0-9._/-' '?')
          safe_ref=${safe_ref:0:64}
          if [[ ${#WORKFLOWS_REF} -ne 40 || "$WORKFLOWS_REF" == *[!0-9a-f]* ]]; then
            echo "::error::<message>"
            exit 1
          fi
PINNED
)"
# Blank lines are noise here (the slicer keeps them so the byte-identity check above sees them),
# and the annotation's PROSE is canonicalized to `<message>` so a rewording is not a test failure.
actual="$(printf '%s\n' "$code" | grep -v '^[[:space:]]*$' | sed 's/\(::error::\).*/\1<message>"/')"
if [ "$actual" = "$expect" ]; then
  ok "the guard's executable body is exactly the pinned one"
else
  bad "the guard's executable body is exactly the pinned one" \
      "$(diff <(printf '%s\n' "$expect") <(printf '%s\n' "$actual") | tr '\n' '~')"
fi

# The one thing the canonicalization above deliberately stops seeing. `<message>` hides the
# annotation's text, so it must be checked here that the text expands NOTHING but the sanitized
# copy — otherwise `echo "::error::$WORKFLOWS_REF"` would read as a mere rewording, and a
# multi-line ref would forge workflow commands in a PUBLIC log exactly as before.
errline="$(printf '%s\n' "$code" | grep -F '::error::')"
stripped="${errline//\$\{safe_ref\}/}"
if [ -n "$errline" ] && [ "${stripped#*\$}" = "$stripped" ]; then
  ok "the annotation expands nothing but the sanitized copy"
else
  bad "the annotation expands nothing but the sanitized copy" "$errline"
fi

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

# --- the input reaches the shell only through env: ---------------------------------------------
# `${{ inputs.workflows_ref }}` interpolated inline into a `run:` body would be a shell-injection
# vector — the runner substitutes the text BEFORE bash ever sees it, so quoting inside the script
# cannot help. The pinned body above already spells the binding out, but this states the rule by
# name so a failure says WHICH rule broke rather than just showing a diff.
script="$(printf '%s\n' "$code" | sed -n '/run: |/,$p')"
has "the run: block scan found the guard's script body" "$script" 'set -euo pipefail'
no  "the input reaches the script only via env:" "$script" '${{'

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
#
# WHICH STEP OWNS THE BINDING MATTERS. Sanctioning the env-binding SHAPE anywhere in the file
# leaves the hole open one step to the right: a later step that binds `WORKFLOWS_REF: ${{
# inputs.workflows_ref }}` of its own and then runs `git fetch origin "$WORKFLOWS_REF"` adds no
# `ref:` key, so `REFS` and the guard count both stay put, the binding clears on shape, and an
# unguarded fetch of an unvalidated ref ships green — the exact bypass this scan exists to close.
# So the binding is sanctioned only INSIDE a guard step: step ownership is tracked by resetting
# at every step key (`- ` at step indent) and every job key, and set only by the guard's own
# `- name:` line, which the byte-identity check above already pins.
aliased="$(awk -v guard="$GUARD_NAME" '
  $0 == guard { inguard = 1; next }
  /^      - / { inguard = 0 }
  /^  [A-Za-z0-9_-]+:/ { inguard = 0 }
  { line = $0; sub(/^[[:space:]]*/, "", line) }
  line ~ /^#/ { next }
  line !~ /inputs\.workflows_ref/ { next }
  { squashed = line; gsub(/[[:space:]]/, "", squashed) }
  inguard && squashed ~ /^WORKFLOWS_REF:\$\{\{inputs\.workflows_ref\}\}$/ { next }
  squashed ~ /^ref:/ { next }
  { print "ALIASED:" NR }
' "$WF" | tr '\n' ' ' | sed 's/ $//')"
eq "the input is referenced only as the guard step's own env binding or a ref: key" "" "$aliased"
# Coverage self-check, PER CATEGORY rather than against a `nrefs * 2` total. The total encoded
# today's exact shape twice over — one binding AND one `ref:` per checkout — so a job that
# legitimately checked the tool out twice, or a consolidation that bound the input once for two
# steps, failed with the misleading "anchors are stale" message even though coverage was intact.
# What actually needs proving is that each scan matched at all: one binding per guard copy, and
# the `ref:` keys the top-of-file scan already counted. The alias scan above is what forbids
# anything else, so these two need not add up to the mentions.
nbind=$(grep -cE '^ *WORKFLOWS_REF: \$\{\{ inputs\.workflows_ref \}\}$' "$WF")
eq "each copy of the guard binds the input exactly once" "$guards" "$nbind"
nrefkeys=$(awk '
  { line = $0; gsub(/[[:space:]]/, "", line) }
  line ~ /^#/ { next }
  line ~ /^ref:.*inputs\.workflows_ref/ { n += 1 }
  END { print n+0 }
' "$WF")
eq "every counted checkout names the input on its own ref: key" "$nrefs" "$nrefkeys"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
