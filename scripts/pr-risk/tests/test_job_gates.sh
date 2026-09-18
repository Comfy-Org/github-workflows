#!/usr/bin/env bash
# test_job_gates.sh — hermetic structural tests for the JOB-LEVEL `if:` conditions in
# .github/workflows/pr-risk.yml. No network, no Actions: this reads the workflow file as text.
#
# WHY A SCRIPT SUITE CANNOT COVER THIS. Every other pr-risk suite tests a script, and a job `if:`
# is not in one — it exists only in the workflow, is evaluated by the runner before any step is
# dispatched, and decides whether a script is invoked at all. `resolve-enabled.sh` can be green on
# every input it takes and still have its answer consumed by the wrong job, because which jobs
# read `enabled` is not a fact any script can see. So the gates get the same treatment
# `test_pin_contract.sh` gives the `workflows_ref` guard: a structural read over the YAML text.
#
# THE TWO FAILURE MODES THIS FILE PINS, both of which ship green through every script suite:
#
#   * THE DISPATCH PREVIEW PUBLISHES NOTHING. `enabled` governs the AUTOMATIC `pull_request`
#     stream only — the `grade` job is exempt on `workflow_dispatch` precisely so a repo enrolled
#     OFF can grade one PR and see what the grader would say. Re-gating `publish-check` on
#     `needs.gate.outputs.enabled` silently removes the Check Run from that preview: the run
#     grades, labels and comments, and the one immutable commit-attached artifact the preview
#     exists to demonstrate is the single thing missing. Nothing fails; the reader just concludes
#     the feature does not work.
#
#   * THE SKIPPED-NEEDS RUNNER SPIN. The tempting minimal fix is `inputs.check_run &&
#     !cancelled()`. But `!cancelled()` is a status function, and ANY status function in an `if:`
#     replaces the implicit "all needs succeeded" check — so on a disabled `pull_request` run,
#     where `grade` was merely SKIPPED, `publish-check` RUNS: it boots a runner, finds an empty
#     `surfaces`, publishes nothing and exits 0. Green, invisible, and one wasted runner per
#     disabled PR across every enrolled repo — exactly the cost the cheap `gate` job exists to
#     avoid. Hence the `needs.grade.result` test, which is what makes the `!cancelled()` safe.
#
# WHY `!= 'skipped'` AND NOT `== 'success'`, restated here because the two read alike: the publish
# step already tolerates a partial render (an empty `surfaces` is "nothing to publish", not an
# error, and per-target failures are warnings). A `success()`-shaped gate would throw away the
# targets that DID render whenever grade failed partway.
#
# The grade side is pinned too, as the other half of the same contract: PR #116's dispatch
# exemption is what makes the publish-check gate correct, so a change that drops it must fail
# here rather than leave this file asserting a preview that no longer grades.
#
# THE EXPRESSIONS ARE PINNED BY EQUALITY, and the named needles below are RESTATEMENTS, not the
# defence — the same division `test_pin_contract.sh` settled on, for the same reason. A needle
# proves a term is PRESENT and nothing about how it is JOINED, so a scan built only from needles
# passes on gates that mean the opposite of what it claims to pin: `needs.grade.result !=
# 'skipped' || inputs.check_run || !cancelled()` contains every positive needle and none of the
# forbidden one, yet `!cancelled()` alone then boots a `checks: write` runner on every
# non-cancelled run — the skipped-needs spin above. `!inputs.check_run` likewise satisfies an
# `inputs.check_run` needle while inverting the opt-in. Only the equality sees the operators. The
# needles stay because they name WHICH invariant broke instead of only printing two strings.
#
# Editing a gate on purpose is therefore a deliberate two-place edit — the workflow and the
# expected string here — whose diff a reviewer sees. That is the property, not an obstacle.
#
# THE GATE IS NOT THE ONLY PLACE `enabled` COULD COME BACK. A step-level `if:` on `Create the
# Check Run(s)` reproduces the dispatch-preview-publishes-nothing mode exactly, with a job gate
# that still reads correctly. So the forbidden `needs.gate.outputs.enabled` is scanned for over
# the WHOLE publish-check job body, not just its `if:`. And the gate's safety rests on `grade`
# being in `needs:` — drop it and `needs.grade.result` resolves to the empty string, `'' !=
# 'skipped'` is true, and the job both stops waiting for grade and runs on every non-cancelled
# run. That membership is asserted too.
#
# Extraction is by anchor, so every scan self-checks that it matched something — a stale anchor
# fails loudly rather than printing a vacuous `ok` over an empty string.
#
#   bash tests/test_job_gates.sh          # exit 0 = all green
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WF="$SELF_DIR/../../../.github/workflows/pr-risk.yml"
[ -f "$WF" ] || { echo "FATAL: $WF not found" >&2; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi }
# `grep -F`, not `case` globbing: these needles are workflow-expression syntax (`!cancelled()`,
# `needs.gate.outputs.enabled`), whose parens and dots a glob or a regex would reinterpret into
# something laxer.
has() { if printf '%s\n' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "not found: $3"; fi }
no()  { if printf '%s\n' "$2" | grep -qF -- "$3"; then bad "$1" "present: $3"; else ok "$1"; fi }

# --- exactly one of each job, which is what makes the extraction below meaningful --------------
# The slicer keys on a 2-space-indented job key. A second job of the same name is not valid YAML,
# but a RENAME that leaves the old key behind, or a copy-pasted block, would have it silently
# read the FIRST copy while the runner obeys the last. Count first.
count_job() { grep -cE "^  $1:[[:space:]]*(#.*)?\$" "$WF"; }
eq "exactly one grade: job"         1 "$(count_job grade)"
eq "exactly one publish-check: job" 1 "$(count_job 'publish-check')"

# --- the whole body of a named job, WHOLE-LINE comments stripped -------------------------------
# From the job's own 2-space key to the NEXT 2-space key (or EOF). Bounding on the next job — not
# on this job's `name:`, as an earlier draft did — is what keeps a rename or a dropped `name:`
# from running the slice on into the following job, where a needle satisfied by somebody else's
# gate reads as green. Whole-line comments go because the rationale above `publish-check`'s gate
# QUOTES the very anti-pattern this file forbids, so a comment-blind scan would fail on the prose
# explaining why the code is right.
#
# The comment strip must come BEFORE the terminator, not after: a 2-space `# ...` line is not a
# job key, and this workflow separates its jobs with sixteen-line comment blocks at exactly that
# indent. Terminating on one would end the slice early — harmless between jobs, but a truncated
# body is a VACUOUS pass for the whole-job `enabled` scan below, which is the half that has no
# equality behind it.
job_body() {
  awk -v job="$1" '
    $0 ~ "^  " job ":[[:space:]]*(#.*)?$" { injob = 1; next }
    !injob { next }
    /^[[:space:]]*#/ { next }
    /^  [^[:space:]]/ { injob = 0; next }
    { print }
  ' "$WF"
}

# --- that job's `if:` expression, folded to one line, the key and block indicator stripped -----
# Keep the `if:` line and its continuations, then squash all whitespace to single spaces. The
# squash is load-bearing, not cosmetic: a `>-` scalar splits the expression across lines, so a
# multi-token needle would never match the raw text and every negative assertion below would pass
# vacuously. A BLANK line is a continuation, not a terminator — a blank inside a `>-` scalar is
# legal YAML, and treating it as the end would silently truncate the expression to a prefix and
# let the equality and the `no` needles pass over the half they never saw. Only a real 4-space
# key ends the block.
job_if() {
  job_body "$1" | awk '
    /^    if:/ { inif = 1; print; next }
    !inif { next }
    /^[[:space:]]*$/ { next }
    /^      / { print; next }
    { inif = 0 }
  ' | tr '\n' ' ' | tr -s '[:space:]' ' ' \
    | sed -E 's/^[[:space:]]*if:[[:space:]]*(>-|>|\|-|\|)?[[:space:]]*//; s/[[:space:]]+$//'
}

# --- that job's `needs:`, normalized to a bare comma list --------------------------------------
# Flow style (`needs: [gate, grade]`) and block style (`- gate` on following lines) both reduce to
# `gate,grade`, so a pure reformat of the key does not turn into a spurious red here.
job_needs() {
  job_body "$1" | awk '
    /^    needs:/ { sub(/^    needs:[[:space:]]*/, ""); v = $0; inneeds = 1; next }
    inneeds && /^      *- / { sub(/^[[:space:]]*-[[:space:]]*/, ""); v = v (v == "" ? "" : ",") $0; next }
    inneeds { exit }
    END { gsub(/[][[:space:]]/, "", v); print v }
  '
}
needs_has() { case ",$2," in *",$3,"*) ok "$1" ;; *) bad "$1" "needs: ${2:-<none>}" ;; esac }

GRADE_IF="$(job_if grade)"
PUBLISH_IF="$(job_if 'publish-check')"
PUBLISH_BODY="$(job_body 'publish-check')"

# Coverage self-checks. Without these, a slicer whose anchors went stale returns "" and every
# `has` below reports a real failure while every `no` reports a fake success — the worse half.
if [ -n "$GRADE_IF" ]; then ok "the grade if: was extracted ($GRADE_IF)"
else bad "the grade if: was extracted" "empty — the slicer's anchors are stale"; fi
if [ -n "$PUBLISH_IF" ]; then ok "the publish-check if: was extracted ($PUBLISH_IF)"
else bad "the publish-check if: was extracted" "empty — the slicer's anchors are stale"; fi
if [ -n "$PUBLISH_BODY" ]; then ok "the publish-check job body was extracted"
else bad "the publish-check job body was extracted" "empty — the slicer's anchors are stale"; fi
# The slice must stop at the job it belongs to. Assert it on `grade`, the one with a successor:
# publish-check is last, so its own slice running long has nothing to run into and would prove
# nothing. `needs.grade.result` appears only in publish-check, so seeing it inside grade's body
# means the two jobs were concatenated — the exact failure a `name:`-bounded slicer had.
no "the grade slice stops before the next job" "$(job_body grade)" "needs.grade.result"
no "grade's if: did not absorb publish-check's" "$GRADE_IF" "inputs.check_run"

# --- THE PINS. The expressions verbatim; everything below them only names what broke -----------
eq "the grade gate is exactly what it should be" \
   "needs.gate.outputs.enabled == 'true' || github.event_name == 'workflow_dispatch'" \
   "$GRADE_IF"
eq "the publish-check gate is exactly what it should be" \
   "needs.grade.result != 'skipped' && inputs.check_run && !cancelled()" \
   "$PUBLISH_IF"

# --- grade: `enabled` OR a manual dispatch ----------------------------------------------------
# PR #116's exemption. Dropping the dispatch arm would make `enabled: false` a lockout rather than
# a mute and delete the staged rollout the input exists for; dropping the `enabled` arm would make
# every enrolled repo grade whatever the switch says.
has "grade is gated on the resolved enabled flag" "$GRADE_IF" "needs.gate.outputs.enabled == 'true'"
has "grade exempts a manual workflow_dispatch"    "$GRADE_IF" "github.event_name == 'workflow_dispatch'"
needs_has "grade waits on gate" "$(job_needs grade)" "gate"

# --- publish-check: follows GRADE, never `enabled` --------------------------------------------
# `grade` in `needs:` is what gives `needs.grade.result` a value at all. Without it the term
# resolves to the empty string, `'' != 'skipped'` is TRUE, and the gate silently degenerates into
# the `!cancelled()`-only form this file exists to forbid — with every needle below still green.
needs_has "publish-check waits on grade" "$(job_needs 'publish-check')" "grade"
has "publish-check is gated on grade having run"   "$PUBLISH_IF" "needs.grade.result != 'skipped'"
has "publish-check still honours the opt-in input" "$PUBLISH_IF" "inputs.check_run"
no  "publish-check does not INVERT the opt-in"     "$PUBLISH_IF" "!inputs.check_run"
has "publish-check still runs on a failed grade"   "$PUBLISH_IF" '!cancelled()'
has "publish-check joins its terms with AND"       "$PUBLISH_IF" "&&"
no  "publish-check joins no term with OR"          "$PUBLISH_IF" "||"

# THE REGRESSION THIS FILE EXISTS FOR. `needs.gate.outputs.enabled` anywhere in this job is the
# dispatch-preview-publishes-nothing failure mode: it re-imposes the automatic stream's switch on
# a manual run that grades, labels and comments regardless. Scanned over the WHOLE job, because a
# step-level `if:` on the publishing step reproduces it exactly while the job gate still reads
# right — and the job gate is the only thing an `if:`-only scan can see.
no "publish-check is NOT gated on enabled"                 "$PUBLISH_IF"   "needs.gate.outputs.enabled"
no "and no step inside publish-check re-imposes it either" "$PUBLISH_BODY" "needs.gate.outputs.enabled"

# THE OTHER HALF, and it is a CONJUNCTION rather than a needle: `inputs.check_run && !cancelled()`
# is a legitimate SUBSTRING of the correct gate, so its presence proves nothing on its own. What
# must never happen is a status function standing WITHOUT a `needs.grade.result` test — the
# skipped-needs runner spin, which no script suite and no green workflow run would ever reveal.
if printf '%s\n' "$PUBLISH_IF" | grep -qE -- '!?(cancelled|always|success|failure)\(\)' \
   && ! printf '%s\n' "$PUBLISH_IF" | grep -qF -- 'needs.grade.result'; then
  bad "publish-check never pairs a status function with no needs.grade.result test" "$PUBLISH_IF"
else
  ok "publish-check never pairs a status function with no needs.grade.result test"
fi

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
