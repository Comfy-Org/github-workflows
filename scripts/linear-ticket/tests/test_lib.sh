#!/usr/bin/env bash
# test_lib.sh — hermetic tests for lib.sh. No network: sources the library and exercises
# candidate extraction, team-key input validation, the attachment policy gate, error
# classification, failure-category selection, and the diagnostic-query builder against
# fixtures. jq only. These cover the design's §11 acceptance cases that live in pure
# functions; the real-Linear behaviours (URL canonicalization, attachment timing) are
# proven in the pilot, not here.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$SELF_DIR/../lib.sh"
[ -f "$LIB" ] || { echo "FATAL: $LIB not found" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found on PATH" >&2; exit 2; }
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../lib.sh
. "$LIB"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi; }
DESC=""
pass_rc() { if "$@" >/dev/null 2>&1; then ok "$DESC"; else bad "$DESC" "rc=$?"; fi; }
fail_rc() { if "$@" >/dev/null 2>&1; then bad "$DESC" "rc=0 (expected non-zero)"; else ok "$DESC"; fi; }

# ── extract_candidates ────────────────────────────────────────────────────────
echo "— extract_candidates —"
eq "uppercases lowercase identifiers" "BE-1234" "$(printf 'fixes be-1234 please' | extract_candidates)"
eq "dedupes case-insensitively, first-seen order" \
  $'BE-1234\nENG-7' \
  "$(printf 'be-1234 ENG-7 BE-1234 eng-7' | extract_candidates)"
eq "reads branch/title/body concatenation" \
  $'BE-1\nENG-2\nOPS-3' \
  "$(printf 'luke/be-1-x\nTitle ENG-2\nCloses OPS-3' | extract_candidates)"
eq "no identifier yields empty" "" "$(printf 'just a normal title' | extract_candidates)"
# 20-candidate cap: feed 25 distinct ids, expect exactly 20 lines.
gen25="$(for i in $(seq 1 25); do printf 'AB-%s ' "$i"; done)"
eq "caps at 20 candidates" "20" "$(printf '%s' "$gen25" | extract_candidates | wc -l | tr -d ' ')"

# ── normalize_team_keys ───────────────────────────────────────────────────────
echo "— normalize_team_keys —"
eq "empty input -> empty (accept any)" "" "$(normalize_team_keys '')"
eq "whitespace-only -> empty (accept any)" "" "$(normalize_team_keys '   ')"
eq "trims and uppercases" "BE,ENG" "$(normalize_team_keys ' be , eng ')"
eq "tolerates stray empty field" "BE,ENG" "$(normalize_team_keys 'BE,,ENG')"
DESC="duplicate key rejected"; fail_rc normalize_team_keys 'BE,be'
DESC="malformed key (punctuation) rejected"; fail_rc normalize_team_keys 'BE,EN!G'
DESC="key starting with digit rejected"; fail_rc normalize_team_keys '1BE'
DESC="valid single key passes"; pass_rc normalize_team_keys 'BE'

# ── filter_issues (THE GATE) ──────────────────────────────────────────────────
echo "— filter_issues —"
# Attachment to THIS PR whose issue is open and on team BE.
LINKED_OPEN_BE='[{"issue":{"identifier":"BE-1","team":{"key":"BE"},"state":{"type":"started"}}}]'
# Same shape but completed.
LINKED_DONE_BE='[{"issue":{"identifier":"BE-1","team":{"key":"BE"},"state":{"type":"completed"}}}]'
# Attachment whose issue belongs to a different team.
LINKED_OPEN_OPS='[{"issue":{"identifier":"OPS-9","team":{"key":"OPS"},"state":{"type":"backlog"}}}]'
# An attachment with no issue (e.g. a non-issue attachment) plus a passing one.
MIXED='[{"issue":null},{"issue":{"identifier":"BE-2","team":{"key":"BE"},"state":{"type":"triage"}}}]'
# Two linked issues, only the second satisfies an open+BE policy.
MULTI='[{"issue":{"identifier":"BE-1","team":{"key":"BE"},"state":{"type":"canceled"}}},{"issue":{"identifier":"ENG-5","team":{"key":"ENG"},"state":{"type":"started"}}}]'

eq "canonical open attachment passes (any team)" "BE-1" "$(filter_issues "$LINKED_OPEN_BE" '' true)"
eq "empty attachments -> no pass" "" "$(filter_issues '[]' '' true)"
eq "completed issue rejected when require-open" "" "$(filter_issues "$LINKED_DONE_BE" '' true)"
eq "completed issue accepted when NOT require-open" "BE-1" "$(filter_issues "$LINKED_DONE_BE" '' false)"
eq "restricted team-keys accepts matching team" "BE-1" "$(filter_issues "$LINKED_OPEN_BE" 'BE,ENG' true)"
eq "restricted team-keys rejects other team" "" "$(filter_issues "$LINKED_OPEN_OPS" 'BE,ENG' true)"
eq "null-issue attachment ignored, real one passes" "BE-2" "$(filter_issues "$MIXED" '' true)"
eq "multi-link passes when at least one satisfies policy" "ENG-5" "$(filter_issues "$MULTI" '' true)"
eq "multi-link empty when restricted team excludes the open one" "" "$(filter_issues "$MULTI" 'BE' true)"

# count_linked
eq "count_linked counts non-null issues" "1" "$(count_linked "$MIXED")"
eq "count_linked zero for empty" "0" "$(count_linked '[]')"
eq "count_linked counts both linked issues" "2" "$(count_linked "$MULTI")"

# ── classify_linear_error ─────────────────────────────────────────────────────
echo "— classify_linear_error —"
eq "RATELIMITED code is retryable (even on HTTP 400)" "retryable" "$(classify_linear_error 400 'RATELIMITED')"
eq "HTTP 503 is retryable" "retryable" "$(classify_linear_error 503 '')"
eq "HTTP 429 is retryable" "retryable" "$(classify_linear_error 429 '')"
eq "auth error is terminal" "terminal" "$(classify_linear_error 401 'AUTHENTICATION_ERROR')"
eq "schema/400 without ratelimit is terminal" "terminal" "$(classify_linear_error 400 'GRAPHQL_VALIDATION_FAILED')"

# ── select_failure_category ───────────────────────────────────────────────────
echo "— select_failure_category —"
eq "infra error dominates" "infra_error" "$(select_failure_category true 0 0)"
eq "infra error dominates even with links" "infra_error" "$(select_failure_category true 2 3)"
eq "linked-but-policy -> policy_mismatch" "policy_mismatch" "$(select_failure_category false 1 0)"
eq "no link, identifier referenced -> exists_not_linked" "exists_not_linked" "$(select_failure_category false 0 2)"
eq "referenced count decides, not resolution -> exists_not_linked" "exists_not_linked" "$(select_failure_category false 0 1)"
eq "no link, nothing referenced -> no_candidate" "no_candidate" "$(select_failure_category false 0 0)"

# ── failure_guidance ──────────────────────────────────────────────────────────
echo "— failure_guidance —"
for cat in no_candidate exists_not_linked policy_mismatch infra_error; do
  DESC="guidance for $cat is non-empty"
  if [ -n "$(failure_guidance "$cat")" ]; then ok "$DESC"; else bad "$DESC" ""; fi
done
DESC="unknown category fails"; fail_rc failure_guidance bogus

# ── build_diagnostic_query / count_resolved_candidates ────────────────────────
echo "— build_diagnostic_query —"
DESC="empty candidate list -> rc1"; fail_rc build_diagnostic_query < /dev/null
Q="$(printf 'BE-1\nENG-2\n' | build_diagnostic_query)"
eq "builds one alias per candidate" "2" "$(printf '%s' "$Q" | grep -cE '^\s*c[0-9]+: issueSearch')"
eq "embeds the identifier as a query literal" "1" "$(printf '%s' "$Q" | grep -c 'query: "BE-1"')"

echo "— count_resolved_candidates —"
RESP='{"data":{"c0":{"nodes":[{"identifier":"BE-1"}]},"c1":{"nodes":[]}}}'
eq "counts aliases with at least one node" "1" "$(count_resolved_candidates "$RESP")"
eq "malformed response counts as 0" "0" "$(count_resolved_candidates 'not json')"
eq "absent data counts as 0" "0" "$(count_resolved_candidates '{"errors":[]}')"

# ── summary ───────────────────────────────────────────────────────────────────
echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
