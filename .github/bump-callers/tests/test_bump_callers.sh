#!/usr/bin/env bash
#
# Functional tests for the generalized caller bumper (bump-callers.sh).
#
# The bump logic is bash, not Python, so — mirroring the test-cursor-review-
# scripts.yml / test-agents-md-integrity.yml lineage of guarding shared CI
# machinery with a unit run on change — this drives the real script against a
# stubbed `gh` and asserts the behavior that a consumer repo depends on:
#   * BOTH caller variables (CURSOR_REVIEW_CALLERS + AGENTS_MD_CALLERS) parse,
#   * every private repo name is masked out of the public run logs,
#   * the caller's pinned SHA (and only it) is rewritten, the pin comment is
#     normalized, and the committed file keeps its single trailing newline,
#   * an empty seeded-empty fleet is a clean no-op while a must-have-callers
#     fleet still hard-fails, and a malformed variable hard-fails.
#
# No network: `gh` is a PATH stub that serves a fixture file and captures the
# contents PUT so we can inspect exactly what would be committed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUMP="${SCRIPT_DIR}/../bump-callers.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

NEW_SHA="abcdef0123456789abcdef0123456789abcdef01"   # 40 hex, != any fixture pin
SHORT="${NEW_SHA:0:7}"

PASS=0
FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1 [$2]"; fi; }

# --- portable base64 decode (BSD `-D` vs GNU `-d`) --------------------------
b64d() { base64 -d 2>/dev/null || base64 -D; }
export -f b64d

# --- build the gh stub on PATH ----------------------------------------------
STUB_BIN="${WORK}/bin"
mkdir -p "$STUB_BIN"
cat > "${STUB_BIN}/gh" <<'STUB'
#!/usr/bin/env bash
# Minimal `gh` stub. Serves $STUB_CONTENT_FILE for a contents GET and captures
# the decoded contents PUT to $STUB_PUT_DIR; everything else returns a canned
# value so bump-callers.sh runs end to end offline.
sub="$1"; shift || true
if [[ "$sub" == "pr" ]]; then
  echo "pr-create $*" >> "$STUB_PUT_DIR/pr.log"
  exit 0
fi
[[ "$sub" == "api" ]] || exit 0

method="GET"; path=""; content=""
args=("$@"); i=0
while (( i < ${#args[@]} )); do
  case "${args[$i]}" in
    --method) method="${args[$((i+1))]}"; i=$((i+2));;
    --jq)     i=$((i+2));;
    --field|-f|-F)
      f="${args[$((i+1))]}"
      [[ "$f" == content=* ]] && content="${f#content=}"
      i=$((i+2));;
    repos/*)  path="${args[$i]}"; i=$((i+1));;
    *)        i=$((i+1));;
  esac
done

case "$method" in
  POST) exit 0;;
  PUT)
    n=$(( $(cat "$STUB_PUT_DIR/count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$STUB_PUT_DIR/count"
    printf '%s' "$content" | { base64 -d 2>/dev/null || base64 -D; } > "$STUB_PUT_DIR/put.$n.txt"
    cp "$STUB_PUT_DIR/put.$n.txt" "$STUB_PUT_DIR/put.last.txt"
    exit 0;;
esac

# GET dispatch by resource path.
if [[ "$path" == *"/contents/"* ]]; then
  b64=$(base64 < "$STUB_CONTENT_FILE" | tr -d '\n')
  printf '{"sha":"blobsha123","content":"%s"}' "$b64"
elif [[ "$path" == *"/git/refs/heads/"* ]]; then
  echo "1234567890abcdef1234567890abcdef12345678"
else
  echo "main"   # repos/<repo> default_branch
fi
exit 0
STUB
chmod +x "${STUB_BIN}/gh"
export PATH="${STUB_BIN}:${PATH}"

# fresh capture dir + fixture per case
new_case() {
  STUB_PUT_DIR="${WORK}/put.$1"; rm -rf "$STUB_PUT_DIR"; mkdir -p "$STUB_PUT_DIR"
  export STUB_PUT_DIR
}

run_bump() { # runs the real script, capturing stdout+stderr and exit code.
  # `env` sets the per-case NAME=value args ("$@") — words from an expansion are
  # NOT recognized as assignment prefixes, so `env` is required here.
  # OUT/RC are consumed by check()'s `eval`, which shellcheck can't see.
  # shellcheck disable=SC2034
  OUT=$(env GH_TOKEN=x NEW_SHA="$NEW_SHA" STUB_CONTENT_FILE="$STUB_CONTENT_FILE" \
            STUB_PUT_DIR="$STUB_PUT_DIR" "$@" bash "$BUMP" 2>&1)
  RC=$?
}
set +e   # we manage errors explicitly below

echo "== cursor-review fleet: single caller, pin + comment rewrite =="
new_case cr
CR_FIXTURE="${WORK}/cr_caller.yml"
printf '%s\n' \
  'name: CI cursor-review' \
  'jobs:' \
  '  review:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@1111111111111111111111111111111111111111  # github-workflows#27' \
  > "$CR_FIXTURE"
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-alpha","file":".github/workflows/ci-cursor-review.yml","label":""}]'
check "exit 0" "[[ $RC -eq 0 ]]"
check "masked the private repo name" "grep -q '::add-mask::Comfy-Org/secret-alpha' <<<\"\$OUT\""
check "reported PR opened"           "grep -q 'PR opened' <<<\"\$OUT\""
check "reported fleet complete"      "grep -q 'cursor-review bump complete' <<<\"\$OUT\""
PUT="${STUB_PUT_DIR}/put.last.txt"
check "committed file exists"                 "[[ -f \"$PUT\" ]]"
check "new SHA written"                       "grep -qF '$NEW_SHA' \"$PUT\""
check "old pin removed"                        "! grep -qF '1111111111111111111111111111111111111111' \"$PUT\""
check "pin comment normalized"                "grep -qF '# github-workflows main ($SHORT)' \"$PUT\""
check "stale pin comment removed"             "! grep -qF '# github-workflows#27' \"$PUT\""
# exactly one trailing newline (#23): last byte is \n (tail -c1 strips to empty),
# and the last two bytes are not both \n (tail -c2 keeps a non-newline byte).
check "single trailing newline"               "[[ -z \"\$(tail -c1 \"$PUT\")\" && -n \"\$(tail -c2 \"$PUT\")\" ]]"

echo "== agents-md fleet: two callers, two SHA refs, '# v1' preserved =="
new_case amd
AMD_FIXTURE="${WORK}/amd_caller.yml"
printf '%s\n' \
  'name: AGENTS.md Integrity' \
  'jobs:' \
  '  agents-md:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/agents-md-integrity.yml@2222222222222222222222222222222222222222  # v1' \
  '    with:' \
  '      workflows_ref: 2222222222222222222222222222222222222222' \
  > "$AMD_FIXTURE"
STUB_CONTENT_FILE="$AMD_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-beta","file":".github/workflows/agents-md-integrity.yml","label":""},{"repo":"Comfy-Org/secret-gamma","file":".github/workflows/agents-md-integrity.yml","label":"ci"}]'
check "exit 0" "[[ $RC -eq 0 ]]"
check "masked caller beta"  "grep -q '::add-mask::Comfy-Org/secret-beta' <<<\"\$OUT\""
check "masked caller gamma" "grep -q '::add-mask::Comfy-Org/secret-gamma' <<<\"\$OUT\""
check "reported fleet complete" "grep -q 'agents-md-integrity bump complete' <<<\"\$OUT\""
check "committed both callers"  "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 2 ]]"
PUT="${STUB_PUT_DIR}/put.last.txt"
check "both SHA refs rewritten (2 occurrences)" "[[ \$(grep -cF '$NEW_SHA' \"$PUT\") -eq 2 ]]"
check "old agents-md pin removed" "! grep -qF '2222222222222222222222222222222222222222' \"$PUT\""
check "'# v1' comment left intact" "grep -qF '# v1' \"$PUT\""

echo "== agents-md fleet: empty list is a clean no-op (ALLOW_EMPTY) =="
new_case empty
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[]'
check "exit 0 on empty" "[[ $RC -eq 0 ]]"
check "logged no-op"    "grep -q 'no callers yet' <<<\"\$OUT\""
check "no commit made"  "[[ ! -f \"\$STUB_PUT_DIR/count\" ]]"

echo "== cursor-review fleet: empty variable is a hard error =="
new_case crempty
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON=''
check "exit 1 on empty must-have fleet" "[[ $RC -eq 1 ]]"
check "error names the variable"        "grep -q 'CURSOR_REVIEW_CALLERS variable is missing or empty' <<<\"\$OUT\""

echo "== any fleet: malformed variable is a hard error =="
new_case malformed
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='{"not":"an array"}'
check "exit 1 on malformed" "[[ $RC -eq 1 ]]"
check "error explains shape" "grep -q 'not a non-empty JSON array' <<<\"\$OUT\""

echo
echo "== $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
