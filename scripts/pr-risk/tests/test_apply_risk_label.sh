#!/usr/bin/env bash
# test_apply_risk_label.sh — hermetic tests for apply-risk-label.sh. No network: DRY_RUN
# covers the mapping/ownership logic, and the validation phases exit before any gh call.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SELF_DIR/../apply-risk-label.sh"
[ -f "$SCRIPT" ] || { echo "FATAL: $SCRIPT not found" >&2; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi; }

run() { # <tier> [label_map] -> stdout (the target label); rc in $?
  REPO=test/repo PR_NUMBER=7 TIER="$1" LABEL_MAP="${2:-}" DRY_RUN=1 bash "$SCRIPT" 2>/dev/null
}

echo "— default map —"
eq "R0 maps to risk:R0" "risk:R0" "$(run R0)"
eq "R3 maps to risk:R3" "risk:R3" "$(run R3)"
eq "unknown maps to risk:ungraded" "risk:ungraded" "$(run unknown)"
eq "empty tier reads as unknown" "risk:ungraded" "$(run '')"
eq "literal null reads as unknown" "risk:ungraded" "$(run null)"

echo "— caller remap (a 1-indexed R1..R4 scheme is one input) —"
MAP='R0=risk:R1,R1=risk:R2,R2=risk:R3,R3=risk:R4,unknown=risk:ungraded'
eq "R0 remaps to risk:R1" "risk:R1" "$(run R0 "$MAP")"
eq "R3 remaps to risk:R4" "risk:R4" "$(run R3 "$MAP")"

echo "— validation refuses bad input before any write —"
run R7 >/dev/null 2>&1;                              eq "bad tier exits 2" 2 "$?"
run R2 'R0=a,R1=b,R2=c,R3=d' >/dev/null 2>&1;        eq "map missing unknown exits 2" 2 "$?"
run R2 'R0=,R1=b,R2=c,R3=d,unknown=e' >/dev/null 2>&1; eq "empty label exits 2" 2 "$?"
REPO='bad repo' PR_NUMBER=7 TIER=R1 DRY_RUN=1 bash "$SCRIPT" >/dev/null 2>&1
eq "bad repo exits 2" 2 "$?"
REPO=test/repo PR_NUMBER=x TIER=R1 DRY_RUN=1 bash "$SCRIPT" >/dev/null 2>&1
eq "bad pr number exits 2" 2 "$?"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
