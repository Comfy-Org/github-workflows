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
  action="$1"; shift || true
  echo "pr-$action $*" >> "$STUB_PUT_DIR/pr.log"
  # Faithfully model `gh pr list --json <fields> --jq <expr>`: build the JSON
  # array real gh would return for the query, then run the caller's ACTUAL --jq
  # over it. Modeling the post-jq output honestly (rather than echoing a bare
  # number, or nothing) is what makes the no-open-PR case emit exactly what real
  # gh emits — so an empty list can't silently mask a `gh pr edit null`
  # regression, and a decoy fork PR is actually exercised.
  #   STUB_OPEN_PR — number of an open bump PR on the repo's OWN branch.
  #   STUB_FORK_PR — number of a cross-repository (fork) PR on the same branch
  #                  name; the script must ignore it.
  if [[ "$action" == "list" ]]; then
    jqexpr=""; a=("$@")
    for ((j=0; j<${#a[@]}; j++)); do
      [[ "${a[$j]}" == "--jq" ]] && jqexpr="${a[$((j+1))]}"
    done
    entries=()
    [[ -n "${STUB_FORK_PR:-}" ]] && entries+=("{\"number\":${STUB_FORK_PR},\"isCrossRepository\":true}")
    [[ -n "${STUB_OPEN_PR:-}" ]] && entries+=("{\"number\":${STUB_OPEN_PR},\"isCrossRepository\":false}")
    json="[$(IFS=,; echo "${entries[*]}")]"
    if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr" <<<"$json"; fi
  fi
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

# Model the ONE bump branch's committed file set in $STUB_PUT_DIR/branch_files:
# a ref create/reset (POST/PATCH on git/refs) rebuilds the branch at the tip and
# so DROPS every prior bump commit (truncate), while a contents PUT commits a
# file onto it (append its path). This is what lets a test assert the branch's
# final contents — and catch the BE-3896 regression where resetting the branch
# per file left only the last file on it. (One branch is modeled; a same-repo
# test drives a single repo, so branch_files reflects exactly that repo's PR.)
case "$method" in
  POST)  # branch create at the tip — starts a fresh (empty) bump branch
    [[ "$path" == *"/git/refs"* ]] && : > "$STUB_PUT_DIR/branch_files"
    exit 0;;
  PATCH) # force-reset of the bump branch ref — discards prior bump commits
    [[ "$path" == *"/git/refs"* ]] && : > "$STUB_PUT_DIR/branch_files"
    exit 0;;
  PUT)
    n=$(( $(cat "$STUB_PUT_DIR/count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$STUB_PUT_DIR/count"
    printf '%s' "$content" | { base64 -d 2>/dev/null || base64 -D; } > "$STUB_PUT_DIR/put.$n.txt"
    cp "$STUB_PUT_DIR/put.$n.txt" "$STUB_PUT_DIR/put.last.txt"
    echo "${path##*/contents/}" >> "$STUB_PUT_DIR/branch_files"   # file now on the branch
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
# No open PR for the stable branch → the create path runs, not the edit path.
check "opened a new PR (pr create called)"    "grep -q '^pr-create' \"\$STUB_PUT_DIR/pr.log\""
check "did not edit (no open PR existed)"     "! grep -q '^pr-edit' \"\$STUB_PUT_DIR/pr.log\""

echo "== cursor-review fleet: an open bump PR is UPDATED IN PLACE, not re-opened (BE-3882) =="
new_case reuse
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  STUB_OPEN_PR=42 \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-alpha","file":".github/workflows/ci-cursor-review.yml","label":""}]'
check "exit 0" "[[ $RC -eq 0 ]]"
check "updated the existing PR in place"      "grep -q 'PR #42 updated to $SHORT' <<<\"\$OUT\""
check "did NOT report a new PR opened"        "! grep -q 'PR opened' <<<\"\$OUT\""
check "called pr edit on the open PR"         "grep -q '^pr-edit 42 ' \"\$STUB_PUT_DIR/pr.log\""
check "did NOT open a second PR"              "! grep -q '^pr-create' \"\$STUB_PUT_DIR/pr.log\""
check "branch still refreshed to the new SHA" "grep -qF '$NEW_SHA' \"\${STUB_PUT_DIR}/put.last.txt\""

echo "== cursor-review fleet: a decoy fork PR on the stable branch is IGNORED =="
# An attacker pre-opens a fork PR whose head branch NAME collides with the
# predictable stable branch (ci/bump-<tag>). `gh pr list --head` matches by name
# across forks, so without the isCrossRepository filter the bot would edit the
# attacker's PR and skip the real bump. The real caller has NO open bump PR here.
new_case fork
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  STUB_FORK_PR=1337 \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-alpha","file":".github/workflows/ci-cursor-review.yml","label":""}]'
check "exit 0" "[[ $RC -eq 0 ]]"
check "ignored the fork PR, opened the real one"  "grep -q 'PR opened' <<<\"\$OUT\""
check "did NOT edit the attacker's fork PR"       "! grep -q '^pr-edit 1337' \"\$STUB_PUT_DIR/pr.log\""
check "opened a fresh PR via create"              "grep -q '^pr-create' \"\$STUB_PUT_DIR/pr.log\""

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

echo "== monorepo: TWO files in the SAME repo BOTH land on the one branch (BE-3896) =="
# A repo listed more than once (a monorepo pinning the reusable workflow from
# two workflow files) must land BOTH files on its single stable branch. The old
# per-entry loop reset the branch before each file, so the second file's reset
# discarded the first file's commit and the PR shipped only the last file — a
# silent partial bump. The stub now models the branch's file set (a reset
# truncates it, a PUT appends), so this asserts the branch keeps BOTH files.
new_case mono
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-mono","file":".github/workflows/ci-a.yml","label":""},{"repo":"Comfy-Org/secret-mono","file":".github/workflows/ci-b.yml","label":""}]'
BF="${STUB_PUT_DIR}/branch_files"
check "exit 0" "[[ $RC -eq 0 ]]"
check "committed both files (2 PUTs)"          "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 2 ]]"
check "branch holds exactly two files"         "[[ \$(wc -l < \"$BF\") -eq 2 ]]"
check "first file present on the branch"       "grep -q 'ci-a.yml' \"$BF\""   # the file the old code dropped
check "second file present on the branch"      "grep -q 'ci-b.yml' \"$BF\""
check "opened exactly ONE PR for the repo"     "[[ \$(grep -c '^pr-create' \"\$STUB_PUT_DIR/pr.log\") -eq 1 ]]"
check "masked the repo name once"              "grep -q '::add-mask::Comfy-Org/secret-mono' <<<\"\$OUT\""
check "reported fleet complete"                "grep -q 'cursor-review bump complete' <<<\"\$OUT\""

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
