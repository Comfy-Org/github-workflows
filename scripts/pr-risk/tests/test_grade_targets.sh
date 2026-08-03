#!/usr/bin/env bash
# test_grade_targets.sh — hermetic tests for grade-targets.sh, the orchestration layer of
# pr-risk.yml (resolve the base ref -> fetch that ref's overrides -> poll the grader until the
# rollup settles -> sync the one label). No network: `gh` is stubbed on PATH and every call it
# receives is logged, so the tests can assert on WHICH requests were made, not only on the
# outcome. Labels are DRY_RUN unless a phase is specifically about the label write.
#
# What is pinned here:
#   * THE EVENT PATH IS UNCHANGED. With a base ref supplied from the event payload, the run makes
#     NO extra PR read — the same single grade + label it always did, and the same tier.
#   * THE BY-NUMBER PATH resolves the base ref from the API and reads the override from THAT ref,
#     not from the default branch. A stacked PR (base = a feature branch) is the case that breaks
#     if this regresses, and it is silent when it breaks: the wrong `.github/risk.json`.
#   * AN UNRESOLVABLE BASE REF FAILS ITS TARGET. Never a grade against the default branch's rules,
#     which is what an empty `?ref=` silently resolves to.
#   * ONE BAD TARGET NEVER ABANDONS THE REST. A batch reports the bad one and grades the others.
#   * `ungraded` IS NOT A FAILED RUN, on either path — an unreadable PR is a reported verdict.
#   * FORKS AND BOTS. Forks still grade R3 from the API-derived fork flag; bot-authored PRs DO
#     grade on the by-number path (the actor guard that skips them is a token guard on the event
#     path only).
#   * SELF-EXCLUSION SURVIVES A DISPATCH: a run id that is not in the PR's rollup excludes
#     nothing, and a settled rollup therefore reads its true SUCCESS instead of an R2 floor.
#
#   bash tests/test_grade_targets.sh        # exit 0 = all green
#
# Deliberately bash (shebang), not zsh — CI runners and the workflow both exercise bash.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLDIR="$(cd "$SELF_DIR/.." && pwd)"
TARGETS="$TOOLDIR/grade-targets.sh"
[ -f "$TARGETS" ] || { echo "FATAL: $TARGETS not found" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found on PATH" >&2; exit 2; }

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pr-risk-targets-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { # <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi
}
has_text() { # <desc> <needle> <haystack>
  case "$3" in *"$2"*) ok "$1" ;; *) bad "$1 (wanted '$2')" "$3" ;; esac
}
no_text() { # <desc> <needle> <haystack>
  case "$3" in *"$2"*) bad "$1 (should NOT contain '$2')" "$3" ;; *) ok "$1" ;; esac
}

STUB_DIR="$SANDBOX/stub"
STUB_LOG="$SANDBOX/gh-calls.log"
mkdir -p "$STUB_DIR" "$SANDBOX/bin"

# The PR fixture: one docs-only file, a settled rollup belonging to some OTHER workflow run, and
# no in-flight check of ours — which is exactly the shape a workflow_dispatch sees, because a
# dispatched run's own check attaches to the dispatched ref rather than to the PR's head commit.
write_fixture() { # [author] [assoc] [is_fork]
  jq -n --arg author "${1:-dev}" --arg assoc "${2:-MEMBER}" --argjson fork "${3:-false}" '
    {data:{repository:{pullRequest:{
      number:42, title:"docs: tweak readme", state:"OPEN", isDraft:false,
      createdAt:"2026-08-01T00:00:00Z", updatedAt:"2026-08-01T00:10:00Z",
      closedAt:null, mergedAt:null,
      author:{login:$author}, authorAssociation:$assoc,
      baseRefName:"release/v2", headRefName:"docs-tweak",
      isCrossRepository:$fork, additions:3, deletions:1, changedFiles:1,
      labels:{pageInfo:{hasNextPage:false}, nodes:[]},
      commits:{nodes:[{commit:{statusCheckRollup:{state:"SUCCESS", contexts:{
        pageInfo:{hasNextPage:false},
        nodes:[{__typename:"CheckRun", name:"unit tests", status:"COMPLETED",
                conclusion:"SUCCESS",
                checkSuite:{workflowRun:{databaseId:1000, workflow:{name:"CI"}}}}]}}}}]}
    }}}}' > "$STUB_DIR/fixture.json"
}
write_fixture
printf '%s\n' '[{"filename":"README.md","additions":3,"deletions":1,"status":"modified"}]' \
  > "$STUB_DIR/files.json"
printf 'release/v2' > "$STUB_DIR/base_ref"
printf '404' > "$STUB_DIR/contents_mode"

# `gh` stub. Logs every invocation, then dispatches on the endpoint shape. The order of the
# patterns matters: `pulls/{n}/files` must be recognised before the bare `pulls/{n}` PR read.
cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
kind=""; filter=""
for ((i=1; i<=$#; i++)); do
  a="${!i}"
  case "$a" in
    --jq) n=$((i+1)); filter="${!n}" ;;
  esac
done
for a in "$@"; do
  case "$a" in
    graphql)            kind=graphql ;;
    *pulls/*/files*)    kind=files ;;
    *contents/*)        kind=contents ;;
    *pulls/[0-9]*)      [ -n "$kind" ] || kind=pull ;;
    *issues/*/labels*)  kind=issuelabels ;;
    *repos/*/labels*)   kind=repolabels ;;
  esac
done
case "$kind" in
  graphql)
    [ -f "$STUB_DIR/graphql_fail" ] && {
      echo 'gh: Could not resolve to a PullRequest with that number (NOT_FOUND)' >&2; exit 1; }
    cat "$STUB_DIR/fixture.json" ;;
  files)
    if [ -n "$filter" ]; then jq -c "$filter" "$STUB_DIR/files.json"; else cat "$STUB_DIR/files.json"; fi ;;
  pull)
    if [ -f "$STUB_DIR/pull_fail" ]; then echo 'gh: Not Found (HTTP 404)' >&2; exit 1; fi
    # Per-number failure, so a batch can have exactly one bad target.
    num=""
    for a in "$@"; do
      case "$a" in *pulls/[0-9]*) num="${a##*pulls/}"; num="${num%%[^0-9]*}" ;; esac
    done
    if [ -n "$num" ] && grep -qw -- "$num" "$STUB_DIR/fail_numbers" 2>/dev/null; then
      echo "gh: Could not resolve to a PullRequest with the number of ${num} (HTTP 404)" >&2
      exit 1
    fi
    cat "$STUB_DIR/base_ref"; echo ;;
  contents)
    case "$(cat "$STUB_DIR/contents_mode" 2>/dev/null || echo 404)" in
      404) echo 'gh: Not Found (HTTP 404)' >&2; exit 1 ;;
      403) echo 'gh: Resource not accessible by integration (HTTP 403)' >&2; exit 1 ;;
      *)   cat "$(cat "$STUB_DIR/contents_mode")" ;;
    esac ;;
  issuelabels)
    [ -f "$STUB_DIR/label_fail" ] && { echo 'gh: Resource not accessible by integration (HTTP 403)' >&2; exit 1; }
    echo '[]' ;;
  repolabels) echo '{}' ;;
  *) echo "gh stub: unhandled args: $*" >&2; exit 1 ;;
esac
exit 0
STUB
chmod +x "$SANDBOX/bin/gh"

# run_targets VAR=val ... — a fresh working directory per call, so the per-target record files and
# the results JSONL of one phase never leak into the next. Sets RC and OUT.
WORK=""
run_targets() {
  WORK="$(mktemp -d "$SANDBOX/work.XXXXXX")"
  : > "$STUB_LOG"
  RC=0
  OUT="$( cd "$WORK" && PATH="$SANDBOX/bin:$PATH" \
      env REPO=test/repo TOOL_DIR="$TOOLDIR" DRY_RUN=1 \
          JOB_TIMEOUT_MINUTES=30 WAIT_MINUTES=0 \
          MAX_UNREADABLE_TRIES=1 READ_RETRY_BUDGET_SECONDS=0 POLL_DELAY_SECONDS=1 \
          STUB_LOG="$STUB_LOG" STUB_DIR="$STUB_DIR" \
          "$@" bash "$TARGETS" 2>&1 )" || RC=$?
}
results() { cat "$WORK/pr-risk-results.jsonl" 2>/dev/null; }
res()     { jq -s -r --argjson i "${2:-0}" ".[\$i] | ${1}" "$WORK/pr-risk-results.jsonl" 2>/dev/null; }
calls()   { cat "$STUB_LOG" 2>/dev/null; }

echo "— phase 1: the event path — a supplied base ref is used as-is, with no extra PR read —"
run_targets PR_NUMBERS=42 BASE_REF=main
eq "the run succeeds"                       0 "$RC"
eq "one result recorded"                    1 "$(results | jq -s length)"
eq "status is graded"                       graded "$(res '.status')"
eq "the docs PR grades R1"                  R1 "$(res '.tier')"
eq "the event's base ref is the one used"   main "$(res '.base_ref')"
eq "the label is the mapped R1 label"       risk:R1 "$(res '.label')"
# The BC proof: the event path spends no API call re-reading a base ref it was handed. `pulls/42`
# appears only as the grader's own `pulls/42/files` read.
no_text "no base-ref PR read is issued" "pulls/42 " "$(calls | grep -v files || true)"
has_text "the override is read from the event's base ref" "contents/.github/risk.json?ref=main" "$(calls)"

echo "— phase 2: the by-number path resolves the base ref from the API —"
# The stub's PR says base = release/v2. A stacked PR is the real-world case: reading the override
# from the default branch instead would grade it against rules that belong to another branch.
run_targets PR_NUMBERS=42
eq "the run succeeds"                    0 "$RC"
eq "the base ref came from the API"      release/v2 "$(res '.base_ref')"
has_text "the base ref is read once" "repos/test/repo/pulls/42 " "$(calls | grep -v files || true)"
has_text "the risk map is read from THAT ref" "contents/.github/risk.json?ref=release/v2" "$(calls)"
has_text "and so is the runbook registry" "contents/.github/risk-runbooks.json?ref=release/v2" "$(calls)"
no_text  "never from an empty ref" "?ref=" "$(calls | grep 'contents/' | grep -F '?ref=' | grep -v 'ref=release/v2' || true)"
eq "the tier is unchanged by the path taken" R1 "$(res '.tier')"

echo "— phase 3: a 404 override falls back to the shipped default and still grades —"
eq "contents_mode is 404 for this phase" 404 "$(cat "$STUB_DIR/contents_mode")"
run_targets PR_NUMBERS=42
eq "absent overrides are not an error"  0 "$RC"
eq "and the PR is graded"               graded "$(res '.status')"
has_text "the fallback is announced" "using the generic default" "$OUT"

echo "— phase 4: a NON-404 override read fails the target, never grades generic —"
printf '403' > "$STUB_DIR/contents_mode"
run_targets PR_NUMBERS=42
eq "the run fails"                    1 "$RC"
eq "the target is recorded failed"    failed "$(res '.status')"
eq "and no tier was invented"         null "$(res '.tier')"
eq "and no label was applied"         null "$(res '.label')"
has_text "the refusal names the reason" "rules nobody read" "$OUT"
printf '404' > "$STUB_DIR/contents_mode"

echo "— phase 5: an unresolvable base ref fails the target and reads no override at all —"
touch "$STUB_DIR/pull_fail"
run_targets PR_NUMBERS=44
eq "the run fails"                 1 "$RC"
eq "the target is recorded failed" failed "$(res '.status')"
eq "with no base ref"              null "$(res '.base_ref')"
has_text "the refusal names the default-branch hazard" "default branch" "$OUT"
# The whole point: nothing may be graded against SOME ref when the right one is unknown.
no_text "no contents read is attempted" "contents/" "$(calls)"
no_text "and the grader is never invoked" "graphql" "$(calls)"
rm -f "$STUB_DIR/pull_fail"

echo "— phase 6: an empty base ref is treated as unresolved, not as 'the default branch' —"
printf '' > "$STUB_DIR/base_ref"
run_targets PR_NUMBERS=42
eq "an empty base ref fails the target" failed "$(res '.status')"
no_text "and reads no override" "contents/" "$(calls)"
printf 'release/v2' > "$STUB_DIR/base_ref"

echo "— phase 7: batch — one unreadable target reports and does NOT abandon the rest —"
# The middle target's PR read fails at the base-ref hop, so 42 and 43 must still be graded and
# labeled. The run reports the failure through its exit code, after every target had its turn.
printf '99' > "$STUB_DIR/fail_numbers"
run_targets PR_NUMBERS="42,99,43"
eq "the run reports failure"        1 "$RC"
eq "all three targets are recorded" 3 "$(results | jq -s length)"
eq "the first target graded"        graded   "$(res '.status' 0)"
eq "the unreadable one failed"      failed   "$(res '.status' 1)"
eq "the LAST target still graded"   graded   "$(res '.status' 2)"
eq "and it carries a real tier"     R1       "$(res '.tier' 2)"
has_text "the batch summary counts both outcomes" "2 graded, 0 ungraded, 1 failed" "$OUT"
rm -f "$STUB_DIR/fail_numbers"

echo "— phase 8: an UNREADABLE PR is labeled ungraded and the run stays GREEN —"
# This is the event path's existing contract and the by-number path must not disagree: rc=3 from
# the grader is a reported verdict, not a broken run.
touch "$STUB_DIR/graphql_fail"
run_targets PR_NUMBERS=42 BASE_REF=main
eq "the run stays green"           0 "$RC"
eq "the target is recorded ungraded" ungraded "$(res '.status')"
eq "labeled ungraded"              risk:ungraded "$(res '.label')"
has_text "and it refuses to read as low risk" "NOT a low-risk verdict" "$(results)"
rm -f "$STUB_DIR/graphql_fail"

echo "— phase 9: a failed LABEL write fails the target —"
# Graded fine, but the one write this workflow exists to perform did not land. Reporting success
# there would leave the PR carrying a stale grade behind a green check.
touch "$STUB_DIR/label_fail"
run_targets PR_NUMBERS=42 BASE_REF=main DRY_RUN=0
eq "the run fails"                 1 "$RC"
eq "the target is recorded failed" failed "$(res '.status')"
eq "the tier it computed is kept"  R1 "$(res '.tier')"
has_text "and the note says the label did not land" "label write FAILED" "$(results)"
rm -f "$STUB_DIR/label_fail"

echo "— phase 10: forks still grade R3 from the API-derived fork flag, on the by-number path —"
write_fixture dev NONE true
run_targets PR_NUMBERS=42
eq "a fork grades R3"          R3 "$(res '.tier')"
eq "provenance is external"    external \
   "$(jq -r '.risk.axes.provenance.provenance' "$WORK/record-42.json" 2>/dev/null)"
write_fixture

echo "— phase 11: a BOT-authored PR grades on the by-number path —"
# The actor guard that skips bots on the event path is a token guard (a bot's pull_request run
# gets a read-only token), not a policy one. On a dispatch it does not apply, and a backfill that
# dropped every bot PR would be a biased corpus.
write_fixture 'dependabot[bot]' CONTRIBUTOR false
run_targets PR_NUMBERS=42
eq "the bot's PR is graded, not skipped" graded "$(res '.status')"
eq "and it carries a real tier"          R1 "$(res '.tier')"
write_fixture

echo "— phase 12: self-exclusion survives a dispatch — a foreign run id excludes nothing —"
# A dispatched run's check is attached to the dispatched ref, not the PR's head, so it is absent
# from the rollup entirely. The rollup must then read its true settled state rather than the R2
# floor a still-pending self check produces on the event path.
run_targets PR_NUMBERS=42 SELF_RUN_ID=777777
eq "the settled rollup reads SUCCESS" SUCCESS \
   "$(jq -r '.checks_state' "$WORK/record-42.json" 2>/dev/null)"
eq "nothing reads as pending"         false \
   "$(jq -r '.checks_pending_excl_self' "$WORK/record-42.json" 2>/dev/null)"
# R1 = "checks green but no test file touched" (the fixture's only file is a README), which is
# the point: the axis ANSWERED the question. An R2 here would mean "no green rollup" — the floor
# the event path pays when its own check is still in flight, and the one a dispatch must not pay.
eq "so reversibility is not floored at R2" R1 \
   "$(jq -r '.risk.axes.reversibility.tier' "$WORK/record-42.json" 2>/dev/null)"
eq "and the grade is a real tier"     R1 "$(res '.tier')"

echo "— phase 13: the target list is validated, and a dispatch with no target FAILS LOUDLY —"
run_targets PR_NUMBERS=""
eq "an empty list is a usage error" 2 "$RC"
has_text "and it names both inputs" "pr_number or pr_numbers" "$OUT"
no_text  "nothing was graded" "graphql" "$(calls)"

run_targets PR_NUMBERS="42,abc"
eq "a non-numeric target is refused" 2 "$RC"
has_text "and says which one" "bad PR number 'abc'" "$OUT"

run_targets PR_NUMBERS="007"
eq "a leading-zero number is refused" 2 "$RC"

run_targets PR_NUMBERS="42,43,44" MAX_TARGETS=2
eq "an over-long list is refused, not truncated" 2 "$RC"
has_text "and says to split the backfill" "split the backfill" "$OUT"

run_targets PR_NUMBERS="42,42"
eq "a duplicated target is graded once" 1 "$(results | jq -s length)"
eq "and the run succeeds"               0 "$RC"

echo "— phase 14: a supplied base ref is IGNORED once there is more than one target —"
# The event payload's base ref belongs to the event's PR. Applying it to a list would read one
# PR's rules and grade another PR by them — silently, and only for the stacked ones.
run_targets PR_NUMBERS="42,43" BASE_REF=main
eq "target one resolved its own base ref" release/v2 "$(res '.base_ref' 0)"
eq "target two resolved its own base ref" release/v2 "$(res '.base_ref' 1)"
no_text "the event's base ref is never used" "ref=main" "$(calls)"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
