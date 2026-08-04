#!/usr/bin/env bash
# test_resolve_enabled.sh — hermetic tests for resolve-enabled.sh. No network, no Actions: the
# script reads two env vars and writes to GITHUB_OUTPUT / GITHUB_STEP_SUMMARY, both of which are
# pointed at sandbox files here.
#
# The properties under test:
#   * THE VARIABLE OUTRANKS THE INPUT IN BOTH DIRECTIONS — it can switch a caller on, and it can
#     switch a caller off. A one-way lever would make the kill switch require the PR it exists
#     to avoid.
#   * A MALFORMED VARIABLE DEGRADES TO THE REVIEWED INPUT, NEVER TO OFF. "The variable is
#     broken" and "the operator switched it off" must not look alike, or a typo silently
#     un-enrolls a repo and looks deliberate.
#   * A WELL-FORMED OBJECT WITHOUT `enabled` IS SILENT — no warning, because `enabled` is the
#     only key today and a per-run annotation would train operators to ignore it.
#
#   bash tests/test_resolve_enabled.sh        # exit 0 = all green
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SELF_DIR/../resolve-enabled.sh"
[ -f "$SCRIPT" ] || { echo "FATAL: $SCRIPT not found" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found on PATH" >&2; exit 2; }

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pr-risk-enabled.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi }

# run <input_enabled> <risk_config> -> sets OUT (resolved value), ERR (stderr), SUMMARY (body)
run() {
  : > "$SANDBOX/out"; : > "$SANDBOX/summary"
  ERR="$(INPUT_ENABLED="$1" RISK_CONFIG="$2" \
         GITHUB_OUTPUT="$SANDBOX/out" GITHUB_STEP_SUMMARY="$SANDBOX/summary" \
         bash "$SCRIPT" 2>&1 >/dev/null)"
  OUT="$(sed -n 's/^enabled=//p' "$SANDBOX/out")"
  SUMMARY="$(cat "$SANDBOX/summary")"
}

echo "— phase 1: with no variable, the reviewed input decides —"
run false ""; eq "default off stays off" false "$OUT"
run true  ""; eq "a reviewed enable is honoured" true "$OUT"

echo "— phase 2: the variable outranks the input, in BOTH directions —"
run false '{"enabled": true}'
eq "the variable can switch a caller ON" true "$OUT"
run true '{"enabled": false}'
eq "the variable can switch a caller OFF (kill switch)" false "$OUT"

echo "— phase 3: a malformed variable degrades to the REVIEWED value, never to off —"
# The load-bearing case: were this to read as `false`, one typo would silently un-enrol a repo
# and be indistinguishable from a deliberate shutdown.
run true 'not json at all'
eq "unparseable keeps the reviewed enable" true "$OUT"
case "$ERR" in *"::warning::"*"not a JSON object"*) ok "and says so" ;; *) bad "and says so" "$ERR" ;; esac
run true '[1,2,3]'
eq "a non-object keeps the reviewed enable" true "$OUT"
run true '{"enabled": "true"}'
eq "a STRING \"true\" is not a boolean — reviewed value stands" true "$OUT"
case "$ERR" in *"not a boolean"*) ok "and names the type problem" ;; *) bad "and names the type problem" "$ERR" ;; esac
run true '{"enabled": 1}'
eq "a numeric 1 is not a boolean either" true "$OUT"
# Same three shapes with the input OFF must stay off — degrading to the reviewed value cuts
# both ways, and must never accidentally ENABLE a repo that did not ask.
run false 'not json at all'; eq "malformed cannot enable a disabled repo" false "$OUT"
run false '{"enabled": "true"}'; eq "a string cannot enable a disabled repo" false "$OUT"

echo "— phase 4: a well-formed object with no \`enabled\` is silent —"
run true '{"something_else": 1}'
eq "no enabled key leaves the reviewed value" true "$OUT"
case "$ERR" in *"::warning::"*) bad "and warns nothing" "$ERR" ;; *) ok "and warns nothing" ;; esac
run false '{}'
eq "an empty object leaves the reviewed value" false "$OUT"

echo "— phase 5: blank and whitespace-only variables are 'unset', not 'malformed' —"
run true '   '
eq "whitespace-only is ignored" true "$OUT"
case "$ERR" in *"::warning::"*) bad "silently" "$ERR" ;; *) ok "silently" ;; esac

echo "— phase 6: a disabled run explains itself in the step summary —"
run false ""
case "$SUMMARY" in
  *DISABLED*"no \`risk:*\` label was added, changed, or removed"*) ok "summary states nothing was touched" ;;
  *) bad "summary states nothing was touched" "$SUMMARY" ;;
esac
case "$SUMMARY" in *RISK_CONFIG*) ok "and names the switch" ;; *) bad "and names the switch" "$SUMMARY" ;; esac
# An ENABLED run writes no summary at all — the grade job owns the output from there.
run true ""
eq "an enabled run writes no summary" "" "$SUMMARY"

echo "— phase 7: it is runnable outside Actions (neither env file set) —"
out="$(INPUT_ENABLED=true RISK_CONFIG='' bash "$SCRIPT" 2>/dev/null)"
case "$out" in *"enabled=true"*) ok "reports to stdout with no GITHUB_OUTPUT" ;; *) bad "reports to stdout with no GITHUB_OUTPUT" "$out" ;; esac

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
