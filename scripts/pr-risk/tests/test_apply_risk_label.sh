#!/usr/bin/env bash
# test_apply_risk_label.sh — hermetic tests for apply-risk-label.sh. No network: DRY_RUN
# covers the mapping/ownership logic, the validation phases exit before any gh call, and the
# write path runs against a `gh` stub on PATH that records the requests it was asked to make.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SELF_DIR/../apply-risk-label.sh"
[ -f "$SCRIPT" ] || { echo "FATAL: $SCRIPT not found" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found on PATH" >&2; exit 2; }

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pr-risk-label-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

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

echo "— the write path: label names are PATH SEGMENTS, and get encoded like it —"
# GitHub label names legally contain spaces, `/`, `#`, `?` and `%`. Interpolated raw, a caller
# remap like `R3=risk high/urgent` built a malformed or misrouted URL: the DELETE failed, `fail`
# fired, and a rename painted the check red. The stub records every request so the test can
# assert on the paths actually built.
mkdir -p "$SANDBOX/bin"
export GH_LOG="$SANDBOX/gh.log" CURRENT_LABELS="$SANDBOX/current.txt"
printf 'risk:R0\nkeep-me\n' > "$CURRENT_LABELS"
cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
for a in "$@"; do
  case "$a" in
    *issues/*/labels*) [ "${1:-}" = api ] && [[ " $* " != *" -X POST "* && " $* " != *" -X DELETE "* ]] \
                         && cat "$CURRENT_LABELS"
                       exit 0 ;;
  esac
done
exit 0
STUB
chmod +x "$SANDBOX/bin/gh"

MAP2='R0=risk:R0,R1=risk:R1,R2=risk:R2,R3=risk high/urgent,unknown=risk:ungraded'
out="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 TIER=R3 LABEL_MAP="$MAP2" \
        bash "$SCRIPT" 2>/dev/null)"
eq "the raw name is what gets returned/logged" "risk high/urgent" "$out"
if grep -q 'risk%20high%2Furgent' "$GH_LOG"; then
  ok "the target label is percent-encoded in the request path"
else bad "the target label is percent-encoded in the request path" "$(tr '\n' '|' < "$GH_LOG")"; fi
if grep -q -- '-X POST repos/test/repo/issues/7/labels -f labels\[\]=risk high/urgent' "$GH_LOG"; then
  ok "but the FORM FIELD carries the raw name, not the encoding"
else bad "but the FORM FIELD carries the raw name, not the encoding" "$(tr '\n' '|' < "$GH_LOG")"; fi
if grep -q 'DELETE repos/test/repo/issues/7/labels/risk%3AR0' "$GH_LOG"; then
  ok "the stale label is removed via an encoded path"
else bad "the stale label is removed via an encoded path" "$(tr '\n' '|' < "$GH_LOG")"; fi
if grep -q -- '--paginate repos/test/repo/issues/7/labels' "$GH_LOG"; then
  ok "the label read paginates (a stale label past page 1 must still be found)"
else bad "the label read paginates" "$(tr '\n' '|' < "$GH_LOG")"; fi

# A label the script does NOT own is never touched, however the grade lands.
if grep -q 'keep-me' "$GH_LOG"; then
  bad "an unowned label is never written to" "$(grep keep-me "$GH_LOG" | tr '\n' '|')"
else ok "an unowned label is never written to"; fi

# Already-correct label: no write at all beyond the read.
: > "$GH_LOG"; printf 'risk:R2\n' > "$CURRENT_LABELS"
PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 TIER=R2 bash "$SCRIPT" >/dev/null 2>&1
eq "an in-sync label writes nothing" 1 "$(wc -l < "$GH_LOG" | tr -d ' ')"

echo "— a failed write reports GITHUB's reason, not just 'could not' —"
# The likeliest misconfiguration is a caller granting `issues: write` but not
# `pull-requests: write`: the labels endpoint is dual-mapped, so labeling a PR 403s with
# "Resource not accessible by integration". Swallowing gh's stderr made that identical to a
# network blip or a deleted PR, so the diagnosis never reached a public run log. This stub
# 403s the label ADD and the test asserts the reason is carried into the failure message.
mkdir -p "$SANDBOX/bin403"
cat > "$SANDBOX/bin403/gh" <<'STUB'
#!/usr/bin/env bash
for a in "$@"; do
  case "$a" in
    *issues/*/labels*)
      if [[ " $* " == *" -X POST "* ]]; then
        echo 'gh: Resource not accessible by integration (HTTP 403)' >&2; exit 1
      fi
      printf 'keep-me\n'; exit 0 ;;
  esac
done
exit 0
STUB
chmod +x "$SANDBOX/bin403/gh"
err="$(PATH="$SANDBOX/bin403:$PATH" REPO=test/repo PR_NUMBER=7 TIER=R1 bash "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
eq "a 403 on the label add exits 4" 4 "$rc"
case "$err" in
  *"Resource not accessible by integration"*) ok "the 403 text reaches the log" ;;
  *) bad "the 403 text reaches the log" "$err" ;;
esac

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
