#!/usr/bin/env bash

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/handle-risk-dispute.sh"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
mkdir -p "$SANDBOX/bin"

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf 'ok - %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'not ok - %s: %s\n' "$1" "$2"; }
eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want '$2', got '$3'"; fi
}

export GH_LOG="$SANDBOX/gh.log"
export LABELS="$SANDBOX/labels.json"
export LAST_PUT="$SANDBOX/put.json"
export AUDIT="$SANDBOX/audit.json"
export CREATED_LABELS="$SANDBOX/created-labels.txt"
export HEAD_SHA="0123456789abcdef0123456789abcdef01234567"

cat >"$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$GH_LOG"
case "$*" in
  *"issues/7/labels?per_page=100"*) cat "$LABELS" ;;
  *"repos/test/repo/labels?per_page=100"*) printf '%s\n' '["risk-dispute:R0"]' ;;
  *"repos/test/repo/labels/risk-dispute"*) echo '{}' ;;
  *"-X PUT repos/test/repo/issues/7/labels"*) cat >"$LAST_PUT" ;;
  *"-X POST repos/test/repo/issues/7/comments"*) cat >"$AUDIT" ;;
  *"repos/test/repo/pulls/7"*) printf '%s\n' "$HEAD_SHA" ;;
  *"-X POST repos/test/repo/labels"*) printf '%s\n' "$*" >>"$CREATED_LABELS" ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
STUB
chmod +x "$SANDBOX/bin/gh"

cat >"$SANDBOX/record.json" <<EOF
{"head_sha":"$HEAD_SHA","risk":{"tier":"R1","map_version":"test-map"}}
EOF

reset_case() {
  : >"$GH_LOG"
  rm -f "$LAST_PUT" "$AUDIT" "$CREATED_LABELS"
}

run_handler() {
  PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 \
    RECORD="$SANDBOX/record.json" NOW=2026-08-17T22:00:00Z RUN_ID=99 \
    bash "$SCRIPT"
}

audit_record() {
  jq -r '.body' "$AUDIT" | sed -n '1s/^<!-- ci-pr-risk-dispute:v1 \([^ ]*\) -->$/\1/p' \
    | jq -Rr '@base64d | fromjson'
}

reset_case
printf '%s\n' '["risk:R1","risk-dispute"]' >"$LABELS"
EVENT_NAME=issue_comment EVENT_ACTION=created ACTOR=reviewer ACTOR_ASSOCIATION=MEMBER \
  COMMENT_ID=55 COMMENT_URL=https://github.com/test/repo/pull/7#issuecomment-55 \
  COMMENT_BODY=$'/risk-dispute R2 because tests only\nSecond line.' run_handler
eq "comment adds the requested dispute label" true \
  "$(jq -r '.labels | index("risk-dispute:R2") != null' "$LAST_PUT")"
eq "comment preserves the computed label" true \
  "$(jq -r '.labels | index("risk:R1") != null' "$LAST_PUT")"
eq "a tiered comment replaces the legacy label" false \
  "$(jq -r '.labels | index("risk-dispute") != null' "$LAST_PUT")"
eq "comment records the human tier" R2 "$(audit_record | jq -r '.human_tier')"
eq "comment records the optional reason" $'because tests only\nSecond line.' \
  "$(audit_record | jq -r '.reason')"

reset_case
printf '%s\n' '["risk:R1"]' >"$LABELS"
EVENT_NAME=issue_comment EVENT_ACTION=created ACTOR=reviewer ACTOR_ASSOCIATION=MEMBER \
  COMMENT_BODY='/risk-dispute R3' run_handler
eq "an empty reason is accepted" null "$(audit_record | jq -r '.reason')"

reset_case
printf '%s\n' '["risk:R1"]' >"$LABELS"
EVENT_NAME=issue_comment EVENT_ACTION=created ACTOR=reviewer ACTOR_ASSOCIATION=MEMBER \
  COMMENT_BODY=$'/risk-dispute Existing workflow\nMore context.' run_handler
eq "a legacy comment adds the plain dispute label" \
  '["risk:R1","risk-dispute"]' "$(jq -c '.labels' "$LAST_PUT")"
eq "a legacy comment records no human tier" null \
  "$(audit_record | jq -r '.human_tier')"
eq "a legacy comment preserves its reason" $'Existing workflow\nMore context.' \
  "$(audit_record | jq -r '.reason')"

reset_case
printf '%s\n' '["risk:R1"]' >"$LABELS"
EVENT_NAME=issue_comment EVENT_ACTION=created ACTOR=reviewer ACTOR_ASSOCIATION=MEMBER \
  COMMENT_BODY='/risk-dispute' run_handler
eq "a bare legacy command is accepted" risk-dispute \
  "$(jq -r '.labels[-1]' "$LAST_PUT")"
eq "a bare legacy command needs no reason" null "$(audit_record | jq -r '.reason')"

reset_case
printf '%s\n' '["risk:R1","risk-dispute"]' >"$LABELS"
EVENT_NAME=pull_request EVENT_ACTION=labeled EVENT_LABEL=risk-dispute ACTOR=reviewer \
  run_handler
rewrite=0
[ ! -e "$LAST_PUT" ] || rewrite=1
eq "the legacy label is accepted without rewriting labels" 0 "$rewrite"
eq "the legacy label records no human tier" null "$(audit_record | jq -r '.human_tier')"

reset_case
printf '%s\n' '["risk:R1","risk-dispute","risk-dispute:R3","risk-dispute:R2"]' >"$LABELS"
EVENT_NAME=pull_request EVENT_ACTION=labeled EVENT_LABEL=risk-dispute:R2 ACTOR=reviewer \
  run_handler
eq "a label replaces the previous dispute tier" \
  '["risk:R1","risk-dispute:R2"]' "$(jq -c '.labels' "$LAST_PUT")"
eq "label entry records no reason" null "$(audit_record | jq -r '.reason')"
eq "label entry records its source" label "$(audit_record | jq -r '.source')"
eq "label entry records only the tier that preceded it" '["R3"]' \
  "$(audit_record | jq -c '.previous_tiers')"

reset_case
printf '%s\n' '["risk:R1","risk-dispute:R3"]' >"$LABELS"
EVENT_NAME=pull_request EVENT_ACTION=unlabeled EVENT_LABEL=risk-dispute ACTOR=reviewer \
  run_handler
rewrite=0
[ ! -e "$LAST_PUT" ] || rewrite=1
eq "removing the legacy label preserves a tiered dispute" 0 "$rewrite"
eq "legacy removal retains the tier in history" '["R3"]' \
  "$(audit_record | jq -c '.previous_tiers')"

reset_case
printf '%s\n' '["risk:R1"]' >"$LABELS"
EVENT_NAME=pull_request EVENT_ACTION=unlabeled EVENT_LABEL=risk-dispute:R2 ACTOR=reviewer \
  run_handler
rewrite=0
[ ! -e "$LAST_PUT" ] || rewrite=1
eq "removing an already-removed dispute needs no label rewrite" 0 "$rewrite"
eq "label removal records no current human tier" null \
  "$(audit_record | jq -r '.human_tier')"
eq "label removal records the removed tier" '["R2"]' \
  "$(audit_record | jq -c '.previous_tiers')"

reset_case
printf '%s\n' '["risk:R1","risk-dispute","risk-dispute:R2"]' >"$LABELS"
EVENT_NAME=issue_comment EVENT_ACTION=created ACTOR=reviewer ACTOR_ASSOCIATION=MEMBER \
  COMMENT_BODY='/risk-dispute clear' run_handler
eq "clear removes the dispute without touching computed risk" \
  '["risk:R1"]' "$(jq -c '.labels' "$LAST_PUT")"
eq "clear is recorded" clear "$(audit_record | jq -r '.action')"

reset_case
printf '%s\n' '["risk:R1","risk-dispute","risk-dispute:R2"]' >"$LABELS"
EVENT_NAME=pull_request EVENT_ACTION=synchronize ACTOR=author run_handler
eq "a new push expires the dispute" '["risk:R1"]' "$(jq -c '.labels' "$LAST_PUT")"
eq "expiry is recorded" expire "$(audit_record | jq -r '.action')"
eq "synchronize provisions the remaining dispute labels" 4 \
  "$(wc -l <"$CREATED_LABELS" | tr -d ' ')"

reset_case
printf '%s\n' '["risk:R1"]' >"$LABELS"
EVENT_NAME=pull_request EVENT_ACTION=labeled EVENT_LABEL=area:testing ACTOR=reviewer \
  run_handler
eq "unrelated labels are ignored" 0 "$(wc -l <"$GH_LOG" | tr -d ' ')"

reset_case
printf '%s\n' '["risk:R1"]' >"$LABELS"
EVENT_NAME=issue_comment EVENT_ACTION=created ACTOR=outsider ACTOR_ASSOCIATION=NONE \
  COMMENT_BODY='/risk-dispute R2' run_handler
eq "unauthorized comments are ignored" 0 "$(wc -l <"$GH_LOG" | tr -d ' ')"

reset_case
printf '%s\n' '["risk:R1"]' >"$LABELS"
EVENT_NAME=issue_comment EVENT_ACTION=created ACTOR=reviewer ACTOR_ASSOCIATION=MEMBER \
  COMMENT_BODY='/risk-dispute R4 unsupported tier' run_handler >/dev/null 2>&1
eq "invalid commands fail validation" 2 "$?"

printf '%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
