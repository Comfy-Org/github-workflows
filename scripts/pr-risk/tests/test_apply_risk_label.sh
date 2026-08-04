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

echo "— the write path: ONE atomic PUT of the whole label set —"
# The sync is a single `PUT .../labels`, which replaces the PR's entire label set in one request.
# That is what makes the "exactly one owned label" contract hold under concurrency: a batch
# dispatch and a `pull_request` event run for one of its numbers sit in different concurrency
# groups, so they CAN overlap, and the old read / delete-stale / add-target sequence let them
# interleave into two contradictory `risk:*` labels. The stub records every request so the tests
# can assert on the requests actually built.
mkdir -p "$SANDBOX/bin"
export GH_LOG="$SANDBOX/gh.log" CURRENT_LABELS="$SANDBOX/current.txt"
printf 'risk:R0\nkeep-me\n' > "$CURRENT_LABELS"
cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
for a in "$@"; do
  case "$a" in
    *issues/*/labels*) [ "${1:-}" = api ] && [[ " $* " != *" -X POST "* && " $* " != *" -X PUT "* ]] \
                         && cat "$CURRENT_LABELS"
                       exit 0 ;;
  esac
done
exit 0
STUB
chmod +x "$SANDBOX/bin/gh"

# Label names are still a PATH SEGMENT in the repo-label probe, and GitHub label names legally
# contain spaces, `/`, `#`, `?` and `%` — so a caller remap like `R3=risk high/urgent` must still be
# encoded there, while the PUT carries the raw names as form fields.
MAP2='R0=risk:R0,R1=risk:R1,R2=risk:R2,R3=risk high/urgent,unknown=risk:ungraded'
out="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 TIER=R3 LABEL_MAP="$MAP2" \
        bash "$SCRIPT" 2>/dev/null)"
eq "the raw name is what gets returned/logged" "risk high/urgent" "$out"
if grep -q 'risk%20high%2Furgent' "$GH_LOG"; then
  ok "the target label is percent-encoded in the request path"
else bad "the target label is percent-encoded in the request path" "$(tr '\n' '|' < "$GH_LOG")"; fi
if grep -q -- '-X PUT repos/test/repo/issues/7/labels ' "$GH_LOG"; then
  ok "the sync is a PUT to the PR's labels collection"
else bad "the sync is a PUT to the PR's labels collection" "$(tr '\n' '|' < "$GH_LOG")"; fi
put="$(grep -- '-X PUT repos/test/repo/issues/7/labels ' "$GH_LOG")"
case "$put" in
  *'-f labels[]=keep-me'*) ok "the PUT carries the unowned label through (a PUT replaces the SET)" ;;
  *) bad "the PUT carries the unowned label through" "$put" ;;
esac
case "$put" in
  *'-f labels[]=risk high/urgent'*) ok "and the FORM FIELD carries the raw name, not the encoding" ;;
  *) bad "and the FORM FIELD carries the raw name, not the encoding" "$put" ;;
esac
case "$put" in
  *'risk:R0'*) bad "the stale owned label is absent from the PUT" "$put" ;;
  *) ok "the stale owned label is absent from the PUT (it is dropped BY the replace)" ;;
esac
# The delete/add pair is precisely what the race exploited; neither may survive anywhere.
if grep -q -- '-X DELETE' "$GH_LOG"; then
  bad "no DELETE is issued at all" "$(grep -- '-X DELETE' "$GH_LOG" | tr '\n' '|')"
else ok "no DELETE is issued at all"; fi
if grep -q -- '-X POST repos/test/repo/issues/7/labels' "$GH_LOG"; then
  bad "no additive POST to the PR's labels is issued" "$(grep -- '-X POST repos/test/repo/issues/7/labels' "$GH_LOG" | tr '\n' '|')"
else ok "no additive POST to the PR's labels is issued"; fi
if grep -q -- '--paginate repos/test/repo/issues/7/labels' "$GH_LOG"; then
  ok "the label read paginates (the PUT is built from this snapshot — a short read would drop labels)"
else bad "the label read paginates" "$(tr '\n' '|' < "$GH_LOG")"; fi

echo "— the race shape: an EMPTY current set still syncs with a PUT, never a POST —"
# This is the assertion that pins the race closed. With no owned label present the tempting
# "optimization" is an additive POST (nothing to remove, so why replace?). That reintroduces the
# exact interleaving: run A and run B both read {}, both see nothing stale, both POST — and the PR
# ends up carrying risk:R1 AND risk:R2 at once. A PUT cannot do that: whichever writer lands second
# replaces the set, so the PR always ends with exactly one owned label.
: > "$GH_LOG"; : > "$CURRENT_LABELS"
outrace="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 TIER=R1 bash "$SCRIPT" 2>/dev/null)"
eq "an empty current set still returns the target" "risk:R1" "$outrace"
if grep -q -- '-X PUT repos/test/repo/issues/7/labels -f labels\[\]=risk:R1' "$GH_LOG"; then
  ok "with nothing stale the write is STILL a PUT of the full set"
else bad "with nothing stale the write is STILL a PUT of the full set" "$(tr '\n' '|' < "$GH_LOG")"; fi
if grep -q -- '-X POST repos/test/repo/issues/7/labels' "$GH_LOG"; then
  bad "and never an additive POST (that is the interleaving)" "$(tr '\n' '|' < "$GH_LOG")"
else ok "and never an additive POST (that is the interleaving)"; fi

echo "— unowned labels are preserved verbatim, disputes included —"
: > "$GH_LOG"; printf 'risk:R1\nrisk-dispute\nbug\n' > "$CURRENT_LABELS"
PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 TIER=R3 bash "$SCRIPT" >/dev/null 2>&1
put3="$(grep -- '-X PUT repos/test/repo/issues/7/labels ' "$GH_LOG")"
eq "the PUT carries exactly the unowned labels plus the new target" \
   "api -X PUT repos/test/repo/issues/7/labels -f labels[]=risk-dispute -f labels[]=bug -f labels[]=risk:R3" \
   "$put3"

echo "— first use: the label is pre-created before the sync —"
# A PUT creates the ASSOCIATION but never the label's color or description, so enrollment still
# needs the explicit repo-side create on first use — and it must land BEFORE the sync.
mkdir -p "$SANDBOX/bin404label"
cat > "$SANDBOX/bin404label/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
for a in "$@"; do
  case "$a" in
    */labels/*) [[ " $* " != *" -X "* ]] && { echo 'gh: Not Found (HTTP 404)' >&2; exit 1; } ;;
  esac
done
for a in "$@"; do
  case "$a" in
    *issues/*/labels*) [ "${1:-}" = api ] && [[ " $* " != *" -X POST "* && " $* " != *" -X PUT "* ]] \
                         && cat "$CURRENT_LABELS"
                       exit 0 ;;
  esac
done
exit 0
STUB
chmod +x "$SANDBOX/bin404label/gh"
: > "$GH_LOG"; printf 'risk:R0\n' > "$CURRENT_LABELS"
PATH="$SANDBOX/bin404label:$PATH" REPO=test/repo PR_NUMBER=7 TIER=R2 bash "$SCRIPT" >/dev/null 2>&1
create_line="$(grep -n -- '-X POST repos/test/repo/labels ' "$GH_LOG" | head -1 | cut -d: -f1)"
put_line="$(grep -n -- '-X PUT repos/test/repo/issues/7/labels ' "$GH_LOG" | head -1 | cut -d: -f1)"
if [ -n "$create_line" ] && [ -n "$put_line" ] && [ "$create_line" -lt "$put_line" ]; then
  ok "a 404 on the label probe pre-creates the label BEFORE the PUT"
else bad "a 404 on the label probe pre-creates the label BEFORE the PUT" "$(tr '\n' '|' < "$GH_LOG")"; fi
if grep -q -- '-X POST repos/test/repo/labels .*-f color=d93f0b' "$GH_LOG"; then
  ok "and the pre-create carries the tier's color (a PUT cannot)"
else bad "and the pre-create carries the tier's color" "$(tr '\n' '|' < "$GH_LOG")"; fi

echo "— a PR the OLD race already double-labeled is HEALED, not read as in-sync —"
# "In sync" is target-present AND no other owned label present — not just target-present. It has to
# be, twice over: PRs graded before the atomic PUT landed can still be carrying two `risk:*` labels
# right now, and a human can hand-add a second one at any time. A bare `has "$TARGET"` check calls
# both of those states in-sync and writes nothing, so the contradiction never gets repaired.
: > "$GH_LOG"; printf 'risk:R0\nrisk:R2\nkeep-me\n' > "$CURRENT_LABELS"
PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 TIER=R2 bash "$SCRIPT" >/dev/null 2>&1
putheal="$(grep -- '-X PUT repos/test/repo/issues/7/labels ' "$GH_LOG")"
eq "the extra owned label is squashed down to the one target" \
   "api -X PUT repos/test/repo/issues/7/labels -f labels[]=keep-me -f labels[]=risk:R2" \
   "$putheal"

# Already-correct label: no write at all beyond the read.
: > "$GH_LOG"; printf 'risk:R2\nkeep-me\n' > "$CURRENT_LABELS"
outsync="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 TIER=R2 bash "$SCRIPT" 2>/dev/null)"
rcsync=$?
eq "an in-sync label writes nothing" 1 "$(wc -l < "$GH_LOG" | tr -d ' ')"
eq "an in-sync run exits 0" 0 "$rcsync"
eq "an in-sync run still prints the target" "risk:R2" "$outsync"

echo "— a failed write reports GITHUB's reason, not just 'could not' —"
# The likeliest misconfiguration is a caller granting `issues: write` but not
# `pull-requests: write`: the labels endpoint is dual-mapped, so labeling a PR 403s with
# "Resource not accessible by integration". Swallowing gh's stderr made that identical to a
# network blip or a deleted PR, so the diagnosis never reached a public run log. This stub
# 403s the PUT and the test asserts the reason — and the PR it names — reach the failure message.
mkdir -p "$SANDBOX/bin403"
cat > "$SANDBOX/bin403/gh" <<'STUB'
#!/usr/bin/env bash
for a in "$@"; do
  case "$a" in
    *issues/*/labels*)
      if [[ " $* " == *" -X PUT "* ]]; then
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
eq "a 403 on the label sync exits 4" 4 "$rc"
case "$err" in
  *"Resource not accessible by integration"*) ok "the 403 text reaches the log" ;;
  *) bad "the 403 text reaches the log" "$err" ;;
esac
case "$err" in
  *"test/repo#7"*) ok "and the failure names the PR" ;;
  *) bad "and the failure names the PR" "$err" ;;
esac
# A failed PUT is atomic — nothing landed. Saying so is the diagnosability the delete-then-add
# shape could not offer: there, a mid-sequence failure left the PR carrying the previous grade.
case "$err" in
  *UNCHANGED*) ok "and states the label state is UNCHANGED (a failed PUT applies nothing)" ;;
  *) bad "and states the label state is UNCHANGED" "$err" ;;
esac

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
