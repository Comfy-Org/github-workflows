#!/usr/bin/env bash

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SELF_DIR/../migrate-risk-labels.sh"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/risk-label-migration-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$1"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi; }

mkdir -p "$SANDBOX/bin" "$SANDBOX/state"
export GH_LOG="$SANDBOX/gh.log" STATE_DIR="$SANDBOX/state"

cat >"$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail

printf '%s\n' "$*" >>"$GH_LOG"
url=""
for arg in "$@"; do [[ "$arg" == repos/* ]] && url="$arg"; done

source_for_url() {
  case "$1" in
    *risk%3AR0*) echo 'risk:R0' ;; *risk%3AR1*) echo 'risk:R1' ;;
    *risk%3AR2*) echo 'risk:R2' ;; *risk%3AR3*) echo 'risk:R3' ;;
  esac
}

if [[ "$url" == *'/issues?state=all&labels='* ]]; then
  source="$(source_for_url "$url")"
  for file in "$STATE_DIR"/*; do
    [ -f "$file" ] || continue
    grep -Fqx "$source" "$file" && basename "$file"
  done
  exit 0
fi

if [[ "$url" =~ /issues/([0-9]+)/labels\? ]]; then
  pr="${BASH_REMATCH[1]}"
  jq -Rsc 'split("\n") | map(select(length > 0))' "$STATE_DIR/$pr"
  exit 0
fi

if [[ " $* " == *' -X POST '* && "$url" =~ /issues/([0-9]+)/labels$ ]]; then
  pr="${BASH_REMATCH[1]}"
  target=""
  for arg in "$@"; do [[ "$arg" == labels\[\]=* ]] && target="${arg#*=}"; done
  if [ "${FAIL_ADD:-0}" != 1 ] && ! grep -Fqx "$target" "$STATE_DIR/$pr"; then
    printf '%s\n' "$target" >>"$STATE_DIR/$pr"
  fi
  exit 0
fi

if [[ " $* " == *' -X DELETE '* && "$url" =~ /issues/([0-9]+)/labels/ ]]; then
  pr="${BASH_REMATCH[1]}"
  source="$(source_for_url "$url")"
  grep -Fvx "$source" "$STATE_DIR/$pr" >"$STATE_DIR/$pr.next" || true
  mv "$STATE_DIR/$pr.next" "$STATE_DIR/$pr"
  exit 0
fi

exit 1
STUB
chmod +x "$SANDBOX/bin/gh"

reset_state() {
  printf 'risk:R0\nkeep\n' >"$STATE_DIR/1"
  printf 'risk:R1\nrisk:medium\n' >"$STATE_DIR/2"
  printf 'bug\n' >"$STATE_DIR/3"
  : >"$GH_LOG"
}

eq "help exits 0" 0 "$(PATH="$SANDBOX/bin:$PATH" bash "$SCRIPT" --help >/dev/null; echo $?)"
PATH="$SANDBOX/bin:$PATH" bash "$SCRIPT" bad-repo >/dev/null 2>&1
eq "bad repository exits 2" 2 "$?"

reset_state
dry="$(PATH="$SANDBOX/bin:$PATH" bash "$SCRIPT" test/repo)"
case "$dry" in *'dry-run: 2 assignment(s)'*) ok "dry-run reports the migration count" ;; *) bad "dry-run reports the migration count" ;; esac
if grep -Eq -- '-X (POST|DELETE)' "$GH_LOG"; then bad "dry-run writes nothing"; else ok "dry-run writes nothing"; fi

reset_state
out="$(PATH="$SANDBOX/bin:$PATH" bash "$SCRIPT" test/repo --apply 2>/dev/null)"
case "$out" in *'complete: migrated=2 failures=0 residual=0'*) ok "apply completes with no residuals" ;; *) bad "apply completes with no residuals" ;; esac
eq "R0 becomes low without dropping unrelated labels" $'keep\nrisk:low' "$(cat "$STATE_DIR/1")"
eq "an existing target is kept while R1 is removed" 'risk:medium' "$(cat "$STATE_DIR/2")"
post_line="$(grep -n -- '-X POST repos/test/repo/issues/1/labels' "$GH_LOG" | cut -d: -f1)"
delete_line="$(grep -n -- '-X DELETE repos/test/repo/issues/1/labels/risk%3AR0' "$GH_LOG" | cut -d: -f1)"
if [ "$post_line" -lt "$delete_line" ]; then ok "target is added before source is removed"; else bad "target is added before source is removed"; fi

reset_state
PATH="$SANDBOX/bin:$PATH" FAIL_ADD=1 bash "$SCRIPT" test/repo --apply >/dev/null 2>&1
eq "an unverified add fails the run" 1 "$?"
if grep -q -- '-X DELETE repos/test/repo/issues/1/labels/risk%3AR0' "$GH_LOG"; then
  bad "an unverified add never removes the source"
else
  ok "an unverified add never removes the source"
fi

printf '%s\n' "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
