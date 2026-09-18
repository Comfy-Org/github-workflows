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
# `[gate, grade]`), whose brackets and parens a glob would reinterpret into something laxer.
has() { if printf '%s\n' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "not found: $3"; fi }
no()  { if printf '%s\n' "$2" | grep -qF -- "$3"; then bad "$1" "present: $3"; else ok "$1"; fi }

# --- exactly one of each job, which is what makes the extraction below meaningful --------------
# The slicer keys on a 2-space-indented job key and stops at that job's `name:`. A second job of
# the same name is not valid YAML, but a RENAME that leaves the old key behind, or a copy-pasted
# block, would have the slicer silently concatenate two `if:`s into one blob where a `has` needle
# satisfied by either copy reads as green. Count first.
count_job() { grep -cE "^  $1:[[:space:]]*(#.*)?\$" "$WF"; }
eq "exactly one grade: job"         1 "$(count_job grade)"
eq "exactly one publish-check: job" 1 "$(count_job 'publish-check')"

# --- the `if:` block of a named job, comments stripped, folded to one line ---------------------
# Slice the job from its 2-space key to its `name:` key (`if:` always precedes `name:` in this
# file), drop WHOLE-LINE comments — the rationale above `publish-check`'s gate quotes the very
# anti-pattern this file forbids, so a comment-blind scan would fail on the prose explaining why
# the code is right — then keep the `if:` key and its folded continuation lines and squash all
# whitespace to single spaces. The squash is load-bearing, not cosmetic: a `>-` block splits the
# expression across lines, so a multi-token needle would never match the raw text and every
# negative assertion below would pass vacuously.
job_if() {
  awk -v job="$1" '
    $0 ~ "^  " job ":[[:space:]]*(#.*)?$" { injob = 1; next }
    !injob { next }
    /^[[:space:]]*#/ { next }
    /^    name:/ { injob = 0; next }
    /^    if:/ { inif = 1; print; next }
    inif && /^      / { print; next }
    inif { inif = 0 }
  ' "$WF" | tr '\n' ' ' | tr -s '[:space:]' ' ' | sed 's/ $//'
}

GRADE_IF="$(job_if grade)"
PUBLISH_IF="$(job_if 'publish-check')"

# Coverage self-checks. Without these, a slicer whose anchors went stale returns "" and every
# `has` below reports a real failure while every `no` reports a fake success — the worse half.
if [ -n "$GRADE_IF" ]; then ok "the grade if: was extracted ($GRADE_IF)"
else bad "the grade if: was extracted" "empty — the slicer's anchors are stale"; fi
if [ -n "$PUBLISH_IF" ]; then ok "the publish-check if: was extracted ($PUBLISH_IF)"
else bad "the publish-check if: was extracted" "empty — the slicer's anchors are stale"; fi

# --- grade: `enabled` OR a manual dispatch ----------------------------------------------------
# PR #116's exemption. Dropping the dispatch arm would make `enabled: false` a lockout rather than
# a mute and delete the staged rollout the input exists for; dropping the `enabled` arm would make
# every enrolled repo grade whatever the switch says.
has "grade is gated on the resolved enabled flag" "$GRADE_IF" "needs.gate.outputs.enabled == 'true'"
has "grade exempts a manual workflow_dispatch"    "$GRADE_IF" "github.event_name == 'workflow_dispatch'"

# --- publish-check: follows GRADE, never `enabled` --------------------------------------------
has "publish-check is gated on grade having run"   "$PUBLISH_IF" "needs.grade.result != 'skipped'"
has "publish-check still honours the opt-in input" "$PUBLISH_IF" "inputs.check_run"
has "publish-check still runs on a failed grade"   "$PUBLISH_IF" '!cancelled()'

# THE REGRESSION THIS FILE EXISTS FOR. `needs.gate.outputs.enabled` in this gate is the
# dispatch-preview-publishes-nothing failure mode: it re-imposes the automatic stream's switch on
# a manual run that grades, labels and comments regardless.
no "publish-check is NOT gated on enabled" "$PUBLISH_IF" "needs.gate.outputs.enabled"

# THE OTHER HALF, and it is a CONJUNCTION rather than a needle: `inputs.check_run && !cancelled()`
# is a legitimate SUBSTRING of the correct gate, so its presence proves nothing on its own. What
# must never happen is that pair standing WITHOUT a `needs.grade.result` test — the skipped-needs
# runner spin, which no script suite and no green workflow run would ever reveal.
if printf '%s\n' "$PUBLISH_IF" | grep -qF -- 'inputs.check_run && !cancelled()' \
   && ! printf '%s\n' "$PUBLISH_IF" | grep -qF -- 'needs.grade.result'; then
  bad "publish-check never pairs !cancelled() with no needs.grade.result test" "$PUBLISH_IF"
else
  ok "publish-check never pairs !cancelled() with no needs.grade.result test"
fi

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
