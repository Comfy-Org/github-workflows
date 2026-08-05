#!/usr/bin/env bash
#
# Functional tests for the generalized caller bumper (bump-callers.sh).
#
# The bump logic is bash, not Python, so — mirroring the test-cursor-review-
# scripts.yml / test-agents-md-integrity.yml lineage of guarding shared CI
# machinery with a unit run on change — this drives the real script against a
# stubbed `gh` and asserts the behavior that a consumer repo depends on:
#   * BOTH caller rosters (CURSOR_REVIEW_CALLERS + AGENTS_MD_CALLERS) parse,
#   * every private repo name is masked out of the public run logs,
#   * the caller's pinned SHA (and only it) is rewritten, the pin comment is
#     normalized, and the committed file keeps its single trailing newline,
#   * BOTH pin halves move in lock-step — the `uses:` ref and the
#     `workflows_ref` input, whatever shape the latter carries (full sha, short
#     sha, tag) — while an unrelated 40-hex merely sharing such a line is left
#     alone, and a pin the rewrite cannot move fails the repo rather than
#     shipping a half-bumped caller,
#   * the pin token's edges hold in both directions: a differently-cased
#     owner/repo is still this repo, while a sibling repo whose name starts the
#     same and a longer key ending in `workflows_ref` are neither rewritten nor
#     misread as a stale pin,
#   * an empty seeded-empty fleet is a clean no-op while a must-have-callers
#     fleet still hard-fails, and a malformed variable hard-fails.
#
# No network: `gh` is a PATH stub that serves a fixture file and captures the
# Git Data API calls (the blob content + the tree's file list) so we can inspect
# exactly what would be committed.

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
# the atomic Git Data API commit (blobs/tree/commit/ref) to $STUB_PUT_DIR;
# everything else returns a canned value so bump-callers.sh runs end to end
# offline.
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

# Model the ONE atomic bump commit in $STUB_PUT_DIR. The script builds a blob
# per staged file (POST git/blobs), one tree carrying all of them off the tip
# (POST git/trees, body on stdin), one commit (POST git/commits), then points
# the bump branch at that commit (POST/PATCH git/refs). We record each blob's
# decoded content (put.$n.txt / put.last.txt; count = number of blobs = files
# committed) and the tree's path list as $STUB_PUT_DIR/branch_files — the
# atomic branch's final file set. Because the whole commit is built BEFORE the
# ref moves, an earlier failure (e.g. a Pass-1 fetch error) leaves NO blobs, NO
# tree, and the ref untouched — the all-or-nothing property this asserts
# (BE-3902) — while the tree still lists BOTH files of a monorepo caller on the
# one branch (BE-3896). (One branch/commit is modeled; a same-repo test drives a
# single repo, so branch_files reflects exactly that repo's PR.)
case "$method:$path" in
  POST:*/git/blobs*)    # blob create — capture the new file content, count it
    # The script now sends the base64 body on stdin (--input -), so read the
    # content out of the JSON body rather than the old --field content= on argv.
    content=$(jq -r '.content' <(cat))
    n=$(( $(cat "$STUB_PUT_DIR/count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$STUB_PUT_DIR/count"
    printf '%s' "$content" | { base64 -d 2>/dev/null || base64 -D; } > "$STUB_PUT_DIR/put.$n.txt"
    cp "$STUB_PUT_DIR/put.$n.txt" "$STUB_PUT_DIR/put.last.txt"
    echo "blobsha${n}"
    exit 0;;
  POST:*/git/trees*)    # tree create — the stdin body lists every bumped path
    body=$(cat)
    jq -r '.tree[].path' <<<"$body" > "$STUB_PUT_DIR/branch_files"
    # Record base_tree so the suite can assert it is the TIP's TREE sha (resolved
    # via GET git/commits below), NOT the tip COMMIT sha — the real Create-a-tree
    # API rejects a commit sha, and a missing/invalid base_tree drops every other
    # file in the caller repo.
    jq -r '.base_tree // ""' <<<"$body" > "$STUB_PUT_DIR/branch_base_tree"
    echo "treesha1"
    exit 0;;
  POST:*/git/commits*)  # commit create — drain the body, return a commit sha
    cat >/dev/null
    echo "commitsha1"
    exit 0;;
  POST:*/git/refs*|PATCH:*/git/refs*)  # point the bump branch at the commit
    exit 0;;
esac

# GET dispatch by resource path.
if [[ "$path" == *"/contents/"* ]]; then
  # Simulate content-fetch failures so the script's 404-vs-transient handling is
  # exercised. STUB_404_FILE: a contents GET whose (decoded-ish) path contains
  # this substring returns a genuine 404 (an expected per-file skip).
  # STUB_FETCH_FAIL: EVERY contents GET returns a transient non-404 error (the
  # script must fail the repo, never ship a partial bump).
  base="${path##*/contents/}"; base="${base%%\?*}"
  if [[ -n "${STUB_404_FILE:-}" && "$base" == *"${STUB_404_FILE}"* ]]; then
    echo "gh: Not Found (HTTP 404)" >&2; exit 1
  fi
  if [[ -n "${STUB_FETCH_FAIL:-}" ]]; then
    echo "gh: Internal Server Error (HTTP 500)" >&2; exit 1
  fi
  b64=$(base64 < "$STUB_CONTENT_FILE" | tr -d '\n')
  printf '{"sha":"blobsha123","content":"%s"}' "$b64"
elif [[ "$path" == *"/git/commits/"* ]]; then
  # Resolve the tip commit's TREE sha (distinct from the commit sha) — the script
  # must pass THIS as base_tree, not the commit sha it was parented on. The stub
  # discards --jq (see arg loop), so emit the post-`.tree.sha` value directly.
  echo "maintreesha1"
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
# The roster fingerprint (BE-6472) is the only auditable trace of WHICH roster
# ran now that it lives in a write-only secret. Assert the shape — a count and a
# full 64-hex digest — and that the digest is a HASH, not a sample: no caller
# name may ride along on that line, which would re-leak what the secret hides.
# ROSTER_LINE / N_BIND / N_SECRET below are consumed by check()'s `eval`,
# which shellcheck cannot see through.
# shellcheck disable=SC2034
ROSTER_LINE=$(grep '^roster: ' <<<"$OUT")
check "logged the roster fingerprint" \
  "grep -qE '^roster: 1 caller\\(s\\), sha256 [0-9a-f]{64}\$' <<<\"\$ROSTER_LINE\""
check "fingerprint leaks no caller name" "! grep -q 'secret-alpha' <<<\"\$ROSTER_LINE\""
PUT="${STUB_PUT_DIR}/put.last.txt"
check "committed file exists"                 "[[ -f \"$PUT\" ]]"
check "new SHA written"                       "grep -qF '$NEW_SHA' \"$PUT\""
check "old pin removed"                        "! grep -qF '1111111111111111111111111111111111111111' \"$PUT\""
check "pin comment normalized"                "grep -qF '# github-workflows main ($SHORT)' \"$PUT\""
check "stale pin comment removed"             "! grep -qF '# github-workflows#27' \"$PUT\""
# exactly one trailing newline (#23): last byte is \n (tail -c1 strips to empty),
# and the last two bytes are not both \n (tail -c2 keeps a non-newline byte).
check "single trailing newline"               "[[ -z \"\$(tail -c1 \"$PUT\")\" && -n \"\$(tail -c2 \"$PUT\")\" ]]"
# base_tree must be the tip's TREE sha (resolved via GET git/commits), NOT the
# tip COMMIT sha — a commit sha 422s the real Create-a-tree API, and a bad
# base_tree drops every other file in the caller repo (BE-3902).
BBT="${STUB_PUT_DIR}/branch_base_tree"
check "base_tree is the resolved tree sha"    "[[ \"\$(cat \"$BBT\")\" == 'maintreesha1' ]]"
check "base_tree is NOT the commit sha"       "! grep -qF '1234567890abcdef1234567890abcdef12345678' \"$BBT\""
# No open PR for the stable branch → the create path runs, not the edit path.
check "opened a new PR (pr create called)"    "grep -q '^pr-create' \"\$STUB_PUT_DIR/pr.log\""
check "did not edit (no open PR existed)"     "! grep -q '^pr-edit' \"\$STUB_PUT_DIR/pr.log\""

echo "== groom fleet: BOTH pins (uses: + workflows_ref) and the '# main @' comment move together =="
# A groom caller pins the reusable TWICE — the `uses:` SHA and the `workflows_ref:`
# input that loads the finder/verifier briefs + dedup ledger. They must move in
# lock-step or a run executes one version's workflow against another version's
# briefs. The fixture also carries an `actions/checkout@<40hex>` pin, which must
# NOT be clobbered to github-workflows' SHA.
new_case groom
GROOM_FIXTURE="${WORK}/groom_caller.yml"
printf '%s\n' \
  'name: Groom' \
  'jobs:' \
  '  groom:' \
  '    steps:' \
  '      - uses: actions/checkout@abcdefabcdefabcdefabcdefabcdefabcdefabcd  # v6' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/groom.yml@1111111111111111111111111111111111111111 # main @ 1111111 — groom.yml not on the v1 tag yet' \
  '    with:' \
  '      workflows_ref: 1111111111111111111111111111111111111111  # main @ 1111111' \
  "      max_prs: \${{ github.event.inputs.max_prs || '1' }}" \
  '      # github-workflows pin note, short form, hex run ends at EOL: # main @ 1111111' \
  '      # github-workflows pin note, deliberate FULL sha: # main @ 1111111111111111111111111111111111111111' \
  '      # unrelated third-party note, neither anchor on the line: # main @ 2222222' \
  > "$GROOM_FIXTURE"
STUB_CONTENT_FILE="$GROOM_FIXTURE" run_bump \
  VAR_NAME=GROOM_CALLERS TAG=groom WORKFLOW_FILE=groom.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-groomed","file":".github/workflows/groom.yml","label":""}]'
check "exit 0" "[[ $RC -eq 0 ]]"
check "masked the private repo name"  "grep -q '::add-mask::Comfy-Org/secret-groomed' <<<\"\$OUT\""
check "reported groom fleet complete" "grep -q 'groom bump complete' <<<\"\$OUT\""
PUT="${STUB_PUT_DIR}/put.last.txt"
check "uses: pin moved"                       "grep -qE \"groom.yml@${NEW_SHA}\" \"$PUT\""
check "workflows_ref pin moved"               "grep -qE \"workflows_ref: ${NEW_SHA}\" \"$PUT\""
check "no stale 40-hex pin anywhere"          "! grep -qF '1111111111111111111111111111111111111111' \"$PUT\""
check "'# main @' comment moved to new short" "grep -qF '# main @ $SHORT' \"$PUT\""
# The `# main @` rewrite is bounded to a 7-12 hex SHORT sha, and that bound has to
# hold at BOTH ends of the run or it mangles what it claims to protect.
check "'# main @ <short>' at EOL rewritten"   "grep -qE '# main @ ${SHORT}\$' \"$PUT\""
# A deliberate FULL-sha comment is corrected by the 40-hex rule and must then be
# left alone — an unbounded {7,12} match would eat its first 12 characters, swap in
# the 7-char SHORT and strand the other 28 as a nonsense suffix.
check "full-sha '# main @' keeps full form"   "grep -qF '# main @ ${NEW_SHA}' \"$PUT\""
# The `workflows_ref:` pin is bumped by the 40-hex rule, so its OWN `# main @
# <short>` note has to move with it — the comment rules are anchored to the same
# two pin contexts as that rule for exactly this line. A narrower anchor bumps the
# pin and leaves the comment naming the old commit: a confident lie on the second
# of groom's two pins.
check "workflows_ref's own pin comment moved" \
  "grep -qE \"workflows_ref: ${NEW_SHA} +# main @ ${SHORT}\$\" \"$PUT\""
# ...and the anchor still BOUNDS the rewrite: a `# main @ <short>` note on a line
# naming neither pin context belongs to some other pin and must be left alone.
check "unanchored '# main @' note untouched"  "grep -qF '# main @ 2222222' \"$PUT\""
# The third-party action pin is a full 40-hex SHA on a line that does NOT mention
# github-workflows — the address anchor is what keeps it intact (the org mandates
# SHA-pinning every action, so clobbering it would break the caller's CI).
check "actions/checkout pin untouched"        "grep -qF 'actions/checkout@abcdefabcdefabcdefabcdefabcdefabcdefabcd' \"$PUT\""
# max_prs forwards a workflow_dispatch string (see groom.yml's input docs); the
# bumper must not mangle the expression while rewriting the pins around it.
check "max_prs forward expression intact"     "grep -qF \"github.event.inputs.max_prs || '1'\" \"$PUT\""

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

echo "== cursor-review fleet: wire_bot=true also wires the cloud-code-bot identity (BE-1814) =="
# The real wire-bot-identity.py helper, driven end to end (no stub) — a caller
# flagged wire_bot must get BOTH the SHA bump AND the identity wired in one PR.
WIRE_SCRIPT="${SCRIPT_DIR}/../../cursor-review/wire-bot-identity.py"
WIRE_FIXTURE="${WORK}/wire_caller.yml"
printf '%s\n' \
  'name: CI cursor-review' \
  'jobs:' \
  '  review:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@1111111111111111111111111111111111111111  # github-workflows#27' \
  '    with:' \
  '      pr_number: 42' \
  '    secrets:' \
  '      CURSOR_API_KEY: dummy' \
  > "$WIRE_FIXTURE"
new_case wire
STUB_CONTENT_FILE="$WIRE_FIXTURE" WIRE_BOT_SCRIPT="$WIRE_SCRIPT" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-wired","file":".github/workflows/ci-cursor-review.yml","label":"","wire_bot":true}]'
check "exit 0" "[[ $RC -eq 0 ]]"
PUT="${STUB_PUT_DIR}/put.last.txt"
check "SHA bumped"                          "grep -qF '$NEW_SHA' \"$PUT\""
check "bot_app_id wired in"                  "grep -q 'bot_app_id: \${{ vars.APP_ID }}' \"$PUT\""
check "BOT_APP_PRIVATE_KEY wired in"          "grep -q 'BOT_APP_PRIVATE_KEY: \${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}' \"$PUT\""
check "PR body notes the wiring"             "grep -q 'BE-1814' \"\$STUB_PUT_DIR/pr.log\""
check "reported fleet complete"              "grep -q 'cursor-review bump complete' <<<\"\$OUT\""

echo "== cursor-review fleet: wire_bot=false (default) never wires, even with WIRE_BOT_SCRIPT set =="
new_case nowire
STUB_CONTENT_FILE="$WIRE_FIXTURE" WIRE_BOT_SCRIPT="$WIRE_SCRIPT" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-unwired","file":".github/workflows/ci-cursor-review.yml","label":""}]'
check "exit 0" "[[ $RC -eq 0 ]]"
PUT="${STUB_PUT_DIR}/put.last.txt"
check "SHA still bumped"                     "grep -qF '$NEW_SHA' \"$PUT\""
check "bot_app_id NOT wired in"              "! grep -q 'bot_app_id:' \"$PUT\""
check "BOT_APP_PRIVATE_KEY NOT wired in"     "! grep -q 'BOT_APP_PRIVATE_KEY:' \"$PUT\""

echo "== cursor-review fleet: wire_bot=true but WIRE_BOT_SCRIPT unset degrades to SHA-bump-only =="
new_case wirenoscript
STUB_CONTENT_FILE="$WIRE_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-noscript","file":".github/workflows/ci-cursor-review.yml","label":"","wire_bot":true}]'
check "exit 0 (degrades, does not fail the repo)" "[[ $RC -eq 0 ]]"
check "warned WIRE_BOT_SCRIPT is unset"            "grep -q 'WIRE_BOT_SCRIPT is unset' <<<\"\$OUT\""
PUT="${STUB_PUT_DIR}/put.last.txt"
check "SHA still bumped"                           "grep -qF '$NEW_SHA' \"$PUT\""
check "bot_app_id NOT wired in"                    "! grep -q 'bot_app_id:' \"$PUT\""

echo "== cursor-review fleet: already-wired + already-current caller is a clean skip (Chesterton's Fence) =="
# A caller that already has the wiring AND is already at the target SHA must be
# a true no-op — the content-equality check (not a bare SHA grep) is what makes
# a wiring-only change on an already-current caller still stage, while a
# fully-converged caller (this case) stays a clean skip.
ALREADY_WIRED_FIXTURE="${WORK}/already_wired_caller.yml"
printf '%s\n' \
  'name: CI cursor-review' \
  'jobs:' \
  '  review:' \
  "    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@${NEW_SHA}  # github-workflows main (${SHORT})" \
  '    with:' \
  '      bot_app_id: dummy' \
  '    secrets:' \
  '      CURSOR_API_KEY: dummy' \
  '      BOT_APP_PRIVATE_KEY: dummy' \
  > "$ALREADY_WIRED_FIXTURE"
new_case alreadywired
STUB_CONTENT_FILE="$ALREADY_WIRED_FIXTURE" WIRE_BOT_SCRIPT="$WIRE_SCRIPT" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-converged","file":".github/workflows/ci-cursor-review.yml","label":"","wire_bot":true}]'
check "exit 0" "[[ $RC -eq 0 ]]"
check "reported already at SHORT (+ wired)" "grep -q 'already at $SHORT' <<<\"\$OUT\""
check "committed nothing"                    "[[ ! -f \"\$STUB_PUT_DIR/count\" ]]"

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
# two workflow files) must land BOTH files on its single stable branch. Both are
# now built into ONE atomic commit (one tree carrying both blobs), so the branch
# holds them together or not at all. The stub records the tree's path list as
# the branch's file set, so this asserts the branch keeps BOTH files — the old
# per-entry loop reset the branch before each file and shipped only the last one
# (BE-3896), and the per-file PUT loop that replaced it could still leave a
# partial commit on failure (BE-3902).
new_case mono
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-mono","file":".github/workflows/ci-a.yml","label":""},{"repo":"Comfy-Org/secret-mono","file":".github/workflows/ci-b.yml","label":""}]'
BF="${STUB_PUT_DIR}/branch_files"
check "exit 0" "[[ $RC -eq 0 ]]"
check "committed both files (2 blobs)"         "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 2 ]]"
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

echo "== cursor-review fleet: empty roster is a hard error =="
new_case crempty
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON=''
check "exit 1 on empty must-have fleet" "[[ $RC -eq 1 ]]"
check "error names the roster secret"   "grep -q 'CURSOR_REVIEW_CALLERS secret is missing or empty' <<<\"\$OUT\""

echo "== any fleet: malformed roster is a hard error =="
new_case malformed
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='{"not":"an array"}'
check "exit 1 on malformed" "[[ $RC -eq 1 ]]"
check "error explains shape" "grep -q 'not a non-empty JSON array' <<<\"\$OUT\""

echo "== monorepo: a genuinely-missing (404) file is skipped, the present one still bumps =="
# One file 404s (expected per-file skip), the other bumps. The repo must still
# succeed and open its PR with the file that WAS present — a 404 is not a repo
# failure.
new_case miss404
STUB_CONTENT_FILE="$CR_FIXTURE" STUB_404_FILE="ci-b.yml" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-mono","file":".github/workflows/ci-a.yml","label":""},{"repo":"Comfy-Org/secret-mono","file":".github/workflows/ci-b.yml","label":""}]'
check "exit 0 (404 is a skip, not a failure)" "[[ $RC -eq 0 ]]"
check "reported the 404 file as not found"    "grep -q 'ci-b.yml not found' <<<\"\$OUT\""
check "committed only the present file"        "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 1 ]]"
check "still opened the PR"                     "grep -q 'PR opened' <<<\"\$OUT\""

echo "== transient fetch error fails the repo — NEVER a silent partial bump =="
# A non-404 fetch error (auth/rate-limit/5xx/network) must fail the whole repo:
# skipping it and opening a PR with only the files that DID fetch is the exact
# partial-bump this refactor exists to prevent (BE-3896).
new_case transient
STUB_CONTENT_FILE="$CR_FIXTURE" STUB_FETCH_FAIL=1 run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-alpha","file":".github/workflows/ci-cursor-review.yml","label":""}]'
check "exit 1 on transient fetch error"        "[[ $RC -eq 1 ]]"
check "warned about avoiding a partial bump"   "grep -q 'failing repo to avoid a partial bump' <<<\"\$OUT\""
check "committed NOTHING"                       "[[ ! -f \"\$STUB_PUT_DIR/count\" ]]"
check "opened NO PR"                            "[[ ! -f \"\$STUB_PUT_DIR/pr.log\" ]] || ! grep -q '^pr-create' \"\$STUB_PUT_DIR/pr.log\""
check "job failed for the repo"                 "grep -q 'bump failed for 1 repo' <<<\"\$OUT\""

echo "== same repo+file listed twice is de-duped to ONE blob/tree entry =="
# A repo listed twice for the same path must stage that file once; a duplicate
# tree entry for the same path is ambiguous (the atomic commit must carry each
# path exactly once), so the dedup keeps the commit well-formed.
new_case dedup
STUB_CONTENT_FILE="$CR_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-dup","file":".github/workflows/ci.yml","label":"ci"},{"repo":"Comfy-Org/secret-dup","file":".github/workflows/ci.yml","label":"ci"}]'
check "exit 0" "[[ $RC -eq 0 ]]"
check "committed the file exactly once"        "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 1 ]]"
check "opened exactly one PR"                   "[[ \$(grep -c '^pr-create' \"\$STUB_PUT_DIR/pr.log\") -eq 1 ]]"

echo "== a full-SHA pin of ANOTHER action is NOT clobbered to github-workflows' SHA =="
# The caller also pins actions/checkout by full SHA (the org's mandated
# practice). The 40-hex rewrite must touch only the github-workflows pin, not
# every hex token in the file.
new_case anchor
ANCHOR_FIXTURE="${WORK}/anchor_caller.yml"
printf '%s\n' \
  'name: CI cursor-review' \
  'jobs:' \
  '  review:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@1111111111111111111111111111111111111111  # github-workflows#27' \
  '  build:' \
  '    runs-on: ubuntu-latest' \
  '    steps:' \
  '      - uses: actions/checkout@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  # v4' \
  > "$ANCHOR_FIXTURE"
STUB_CONTENT_FILE="$ANCHOR_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-anchor","file":".github/workflows/ci.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0" "[[ $RC -eq 0 ]]"
check "github-workflows pin bumped"            "grep -qF '$NEW_SHA' \"$PUT\""
check "old github-workflows pin removed"        "! grep -qF '1111111111111111111111111111111111111111' \"$PUT\""
check "actions/checkout SHA left intact"        "grep -qF 'actions/checkout@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \"$PUT\""

echo "== half-bumped file (one ref at NEW_SHA, one stale) is REPAIRED, not skipped =="
# The already-pinned check compares rewritten-vs-original content, so a file
# where only one of two refs reached NEW_SHA still differs and is re-staged —
# the old 'NEW_SHA appears anywhere' grep would have skipped it, stranding the
# stale ref.
new_case halfbump
HALF_FIXTURE="${WORK}/half_caller.yml"
printf '%s\n' \
  'name: AGENTS.md Integrity' \
  'jobs:' \
  '  agents-md:' \
  "    uses: Comfy-Org/github-workflows/.github/workflows/agents-md-integrity.yml@${NEW_SHA}  # v1" \
  '    with:' \
  '      workflows_ref: 2222222222222222222222222222222222222222' \
  > "$HALF_FIXTURE"
STUB_CONTENT_FILE="$HALF_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-half","file":".github/workflows/agents-md-integrity.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0" "[[ $RC -eq 0 ]]"
check "re-staged the half-bumped file"         "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 1 ]]"
check "both refs now at NEW_SHA"               "[[ \$(grep -cF '$NEW_SHA' \"$PUT\") -eq 2 ]]"
check "stale second ref repaired"              "! grep -qF '2222222222222222222222222222222222222222' \"$PUT\""

echo "== a fully already-pinned file is a clean skip (no commit, no PR) =="
new_case pinned
PINNED_FIXTURE="${WORK}/pinned_caller.yml"
printf '%s\n' \
  'name: CI cursor-review' \
  'jobs:' \
  '  review:' \
  "    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@${NEW_SHA}  # github-workflows main (${SHORT})" \
  > "$PINNED_FIXTURE"
STUB_CONTENT_FILE="$PINNED_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-pinned","file":".github/workflows/ci.yml","label":""}]'
check "exit 0" "[[ $RC -eq 0 ]]"
check "reported already at SHORT"              "grep -q 'already at $SHORT' <<<\"\$OUT\""
check "committed nothing"                       "[[ ! -f \"\$STUB_PUT_DIR/count\" ]]"
check "opened no PR"                            "[[ ! -f \"\$STUB_PUT_DIR/pr.log\" ]] || ! grep -q '^pr-create' \"\$STUB_PUT_DIR/pr.log\""

echo "== a TAG-pinned workflows_ref moves in lock-step with uses: (BE-4662) =="
# The under-rewrite half of BE-4662. A caller pins this repo TWICE — the `uses:`
# sha and the `workflows_ref` input that loads the briefs/prompts/scripts. The
# old substitution rewrote "any 40-hex on a line mentioning github-workflows", so
# a `workflows_ref` pinned to a TAG was left behind while `uses:` moved: a
# green-looking bump PR running one version's workflow against another version's
# assets. The rewrite is anchored to the pin token now, so ref SHAPE is irrelevant.
new_case reftag
TAG_FIXTURE="${WORK}/tag_ref_caller.yml"
printf '%s\n' \
  'name: AGENTS.md Integrity' \
  'jobs:' \
  '  agents-md:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/agents-md-integrity.yml@2222222222222222222222222222222222222222  # v1' \
  '    with:' \
  '      workflows_ref: v1' \
  > "$TAG_FIXTURE"
STUB_CONTENT_FILE="$TAG_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-tagref","file":".github/workflows/agents-md-integrity.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0" "[[ $RC -eq 0 ]]"
check "staged the caller"                      "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 1 ]]"
check "uses: pin bumped"                       "grep -qF 'agents-md-integrity.yml@$NEW_SHA' \"$PUT\""
check "TAG workflows_ref bumped too"           "grep -qF 'workflows_ref: $NEW_SHA' \"$PUT\""
check "no tag left in workflows_ref"           "! grep -qE '^[[:space:]]*workflows_ref:[[:space:]]*v1[[:space:]]*\$' \"$PUT\""
check "both pins at the new SHA"               "[[ \$(grep -cF '$NEW_SHA' \"$PUT\") -eq 2 ]]"
check "'# v1' comment left intact"             "grep -qF '# v1' \"$PUT\""

echo "== a SHORT-SHA (and quoted) workflows_ref also moves in lock-step (BE-4662) =="
# The other non-40-hex shape a caller can carry. The quotes must survive — only
# the ref token inside them is replaced, so the YAML stays valid.
new_case refshort
SHORT_FIXTURE="${WORK}/short_ref_caller.yml"
printf '%s\n' \
  'name: AGENTS.md Integrity' \
  'jobs:' \
  '  agents-md:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/agents-md-integrity.yml@2222222222222222222222222222222222222222  # v1' \
  '    with:' \
  "      workflows_ref: '2222222'" \
  > "$SHORT_FIXTURE"
STUB_CONTENT_FILE="$SHORT_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-shortref","file":".github/workflows/agents-md-integrity.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0" "[[ $RC -eq 0 ]]"
check "staged the caller"                      "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 1 ]]"
check "short-sha workflows_ref bumped"         "grep -qF \"workflows_ref: '$NEW_SHA'\" \"$PUT\""
check "closing quote preserved"                "! grep -qF \"workflows_ref: $NEW_SHA'\" \"$PUT\""
check "short sha gone"                         "! grep -qF \"workflows_ref: '2222222'\" \"$PUT\""
check "both pins at the new SHA"               "[[ \$(grep -cF '$NEW_SHA' \"$PUT\") -eq 2 ]]"

echo "== an unrelated 40-hex SHARING a github-workflows line is NOT clobbered (BE-4662) =="
# The over-rewrite half of BE-4662. The old substitution keyed on the LINE ("any
# 40-hex on a line that mentions github-workflows or workflows_ref"), so an
# unrelated full-SHA value that merely shared such a line — another action pinned
# next to a mention of this repo, a digest documented as tracking workflows_ref —
# was rewritten to github-workflows' SHA. Only the pin TOKEN may move. (The
# sibling `anchor` case above covers the easy version, where the third-party pin
# sits on its own line.)
new_case coloc
COLOC_FIXTURE="${WORK}/coloc_caller.yml"
printf '%s\n' \
  'name: CI cursor-review' \
  'jobs:' \
  '  review:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@1111111111111111111111111111111111111111  # github-workflows#27' \
  '  audit:' \
  '    runs-on: ubuntu-latest' \
  '    steps:' \
  '      - uses: some-org/mirror-check@cccccccccccccccccccccccccccccccccccccccc  # verifies the github-workflows mirror' \
  '      - name: digest' \
  '        run: echo dddddddddddddddddddddddddddddddddddddddd  # kept in sync with workflows_ref' \
  > "$COLOC_FIXTURE"
STUB_CONTENT_FILE="$COLOC_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-coloc","file":".github/workflows/ci.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0" "[[ $RC -eq 0 ]]"
check "github-workflows pin bumped"            "grep -qF 'cursor-review.yml@$NEW_SHA' \"$PUT\""
check "co-located action SHA left intact"      "grep -qF 'some-org/mirror-check@cccccccccccccccccccccccccccccccccccccccc' \"$PUT\""
check "co-located digest left intact"          "grep -qF 'echo dddddddddddddddddddddddddddddddddddddddd' \"$PUT\""
check "exactly ONE pin was rewritten"          "[[ \$(grep -cF '$NEW_SHA' \"$PUT\") -eq 1 ]]"

echo "== a pin the rewrite cannot move FAILS the repo — no commit, no PR (BE-4662) =="
# The assertion is the backstop for anything the (deliberately precise) rewrite
# does not know how to move — here a `workflows_ref` fed by a GitHub expression,
# which must never be half-rewritten into a broken value. Staging it would open a
# green-looking PR whose `uses:` moved and whose assets ref did not, so the repo
# fails instead: a partial bump is worse than no bump (BE-3896).
new_case assertfire
EXPR_FIXTURE="${WORK}/expr_ref_caller.yml"
# shellcheck disable=SC2016  # the `${{ }}` must reach the fixture verbatim
printf '%s\n' \
  'name: AGENTS.md Integrity' \
  'jobs:' \
  '  agents-md:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/agents-md-integrity.yml@2222222222222222222222222222222222222222  # v1' \
  '    with:' \
  '      workflows_ref: ${{ inputs.workflows_ref }}' \
  > "$EXPR_FIXTURE"
STUB_CONTENT_FILE="$EXPR_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-expr","file":".github/workflows/agents-md-integrity.yml","label":""}]'
check "exit 1 — the repo failed"               "[[ $RC -eq 1 ]]"
check "warning names the file"                 "grep -q 'agents-md-integrity.yml still pins github-workflows' <<<\"\$OUT\""
check "warning names the un-bumped pin"        "grep -qF 'inputs.workflows_ref' <<<\"\$OUT\""
check "warning reads as a sentence"            "grep -qF 'inputs.workflows_ref }} after the rewrite' <<<\"\$OUT\""
check "committed NOTHING"                      "[[ ! -f \"\$STUB_PUT_DIR/count\" ]]"
check "opened NO PR"                           "[[ ! -f \"\$STUB_PUT_DIR/pr.log\" ]] || ! grep -q '^pr-create' \"\$STUB_PUT_DIR/pr.log\""
check "job failed for the repo"                "grep -q 'bump failed for 1 repo' <<<\"\$OUT\""

echo "== the assertion reads PINS, not prose — a commented workflows_ref is not a stale pin =="
# The assertion is deliberately broader than the rewrite, which makes it the one
# place a false positive would hard-fail an otherwise-clean repo. Comments are
# stripped before it scans, so a human note that happens to say `workflows_ref:`
# cannot masquerade as an un-bumped pin — while the real pin on the next line is
# still asserted (it is checked before the `#`).
new_case prosecomment
PROSE_FIXTURE="${WORK}/prose_caller.yml"
printf '%s\n' \
  'name: AGENTS.md Integrity' \
  'jobs:' \
  '  agents-md:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/agents-md-integrity.yml@2222222222222222222222222222222222222222  # v1' \
  '    with:' \
  '      # workflows_ref: keep this in lock-step with the uses: pin above' \
  '      workflows_ref: 2222222222222222222222222222222222222222  # bumped by CI' \
  > "$PROSE_FIXTURE"
STUB_CONTENT_FILE="$PROSE_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-prose","file":".github/workflows/agents-md-integrity.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0 — prose did not trip the assert"  "[[ $RC -eq 0 ]]"
check "staged the caller"                       "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 1 ]]"
check "both real pins bumped"                   "[[ \$(grep -cF '$NEW_SHA' \"$PUT\") -eq 2 ]]"
check "old pin gone"                            "! grep -qF '2222222222222222222222222222222222222222' \"$PUT\""
check "prose comment still readable"            "grep -qF 'keep this in lock-step with the uses: pin above' \"$PUT\""

echo "== a SIBLING repo that merely starts with our name is NOT repinned (BE-4662) =="
# The pin token ends at a DELIMITER. Without one, the path glob after the repo
# name also swallows a sibling repo's name — `github-workflows-tools/action@v1`
# would be repinned to THIS repo's SHA, and because the assertion reads pins with
# the same pattern it would read the corrupted value back as NEW_SHA and stage it.
new_case sibling
SIB_FIXTURE="${WORK}/sibling_caller.yml"
printf '%s\n' \
  'name: CI cursor-review' \
  'jobs:' \
  '  review:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@1111111111111111111111111111111111111111  # github-workflows#27' \
  '  tools:' \
  '    uses: Comfy-Org/github-workflows-tools/.github/workflows/lint.yml@cccccccccccccccccccccccccccccccccccccccc  # v3' \
  '    steps:' \
  '      - uses: Comfy-Org/github-workflows-actions/setup@v2' \
  > "$SIB_FIXTURE"
STUB_CONTENT_FILE="$SIB_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-sibling","file":".github/workflows/ci.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0" "[[ $RC -eq 0 ]]"
check "our pin bumped"                          "grep -qF 'cursor-review.yml@$NEW_SHA' \"$PUT\""
check "sibling repo's SHA pin left intact"      "grep -qF 'github-workflows-tools/.github/workflows/lint.yml@cccccccccccccccccccccccccccccccccccccccc' \"$PUT\""
check "sibling repo's tag pin left intact"      "grep -qF 'github-workflows-actions/setup@v2' \"$PUT\""
check "exactly ONE pin was rewritten"           "[[ \$(grep -cF '$NEW_SHA' \"$PUT\") -eq 1 ]]"

echo "== a lowercase owner/repo is still THIS repo — both halves move (BE-4662) =="
# GitHub resolves `uses:` owner/repo case-insensitively, so a caller written
# `comfy-org/…` is calling this repo. A case-SENSITIVE match would skip its
# `uses:` half while rule 2 (repo-agnostic) bumped `workflows_ref` anyway, and the
# assertion — reading `uses:` with the same pattern — would not see the stale half
# either: a silently half-bumped caller, the split this change exists to prevent.
new_case lowercase
LC_FIXTURE="${WORK}/lowercase_caller.yml"
printf '%s\n' \
  'name: AGENTS.md Integrity' \
  'jobs:' \
  '  agents-md:' \
  '    uses: comfy-org/github-workflows/.github/workflows/agents-md-integrity.yml@2222222222222222222222222222222222222222  # v1' \
  '    with:' \
  '      workflows_ref: v1' \
  > "$LC_FIXTURE"
STUB_CONTENT_FILE="$LC_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-lowercase","file":".github/workflows/agents-md-integrity.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0" "[[ $RC -eq 0 ]]"
check "staged the caller"                       "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 1 ]]"
check "lowercase uses: pin bumped"              "grep -qF 'comfy-org/github-workflows/.github/workflows/agents-md-integrity.yml@$NEW_SHA' \"$PUT\""
check "workflows_ref bumped in lock-step"       "grep -qF 'workflows_ref: $NEW_SHA' \"$PUT\""
check "owner case preserved as written"         "! grep -qF 'Comfy-Org/github-workflows/.github' \"$PUT\""
check "both pins at the new SHA"                "[[ \$(grep -cF '$NEW_SHA' \"$PUT\") -eq 2 ]]"

echo "== a longer key ENDING in workflows_ref is not a stale pin =="
# The assertion is looser than the rewrite by design, which makes it the one place
# a false positive hard-fails a clean repo. `upstream_workflows_ref: v1` is not
# this repo's input — rule 2 correctly leaves it alone — so reading it as an
# un-bumped github-workflows pin would block this caller's bump on every run.
new_case longkey
LONGKEY_FIXTURE="${WORK}/longkey_caller.yml"
printf '%s\n' \
  'name: AGENTS.md Integrity' \
  'jobs:' \
  '  agents-md:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/agents-md-integrity.yml@2222222222222222222222222222222222222222  # v1' \
  '    with:' \
  '      workflows_ref: 2222222222222222222222222222222222222222' \
  '      upstream_workflows_ref: v1' \
  > "$LONGKEY_FIXTURE"
STUB_CONTENT_FILE="$LONGKEY_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-longkey","file":".github/workflows/agents-md-integrity.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0 — the foreign key did not trip it" "[[ $RC -eq 0 ]]"
check "staged the caller"                        "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 1 ]]"
check "both real pins bumped"                    "[[ \$(grep -cF '$NEW_SHA' \"$PUT\") -eq 2 ]]"
check "foreign key's value left alone"           "grep -qE '^[[:space:]]*upstream_workflows_ref:[[:space:]]*v1\$' \"$PUT\""

echo "== a '#' inside the ref cannot fake its way past the assertion =="
# `#` is legal in a git ref name, and REF_RE stops at it, so rule 2 rewrites only
# the part before: `'feature#1'` -> `'<NEW_SHA>#1'`. Stripping comments at the
# FIRST `#` would read that back as a bare NEW_SHA and accept the corrupted value;
# stripping by YAML's rule (whitespace-preceded `#`) keeps the value whole, so it
# compares unequal and fails the repo instead of shipping broken YAML.
new_case hashref
HASH_FIXTURE="${WORK}/hash_ref_caller.yml"
printf '%s\n' \
  'name: AGENTS.md Integrity' \
  'jobs:' \
  '  agents-md:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/agents-md-integrity.yml@2222222222222222222222222222222222222222  # v1' \
  '    with:' \
  "      workflows_ref: 'feature#1'" \
  > "$HASH_FIXTURE"
STUB_CONTENT_FILE="$HASH_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-hashref","file":".github/workflows/agents-md-integrity.yml","label":""}]'
check "exit 1 — the repo failed"                 "[[ $RC -eq 1 ]]"
check "warning names the half-rewritten value"   "grep -qF '#1' <<<\"\$OUT\""
check "committed NOTHING"                        "[[ ! -f \"\$STUB_PUT_DIR/count\" ]]"
check "opened NO PR"                             "[[ ! -f \"\$STUB_PUT_DIR/pr.log\" ]] || ! grep -q '^pr-create' \"\$STUB_PUT_DIR/pr.log\""

echo "== an EMPTY workflows_ref is a stale pin, not a blank to skip =="
# `workflows_ref: \"\"` is a pin rule 2 cannot move either (REF_RE needs >=1
# character), so dropping empty extracted values would let it slip past while
# `uses:` moved — the silent half-bump the assertion exists to catch.
new_case emptyref
EMPTY_FIXTURE="${WORK}/empty_ref_caller.yml"
printf '%s\n' \
  'name: AGENTS.md Integrity' \
  'jobs:' \
  '  agents-md:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/agents-md-integrity.yml@2222222222222222222222222222222222222222  # v1' \
  '    with:' \
  '      workflows_ref: ""' \
  > "$EMPTY_FIXTURE"
STUB_CONTENT_FILE="$EMPTY_FIXTURE" run_bump \
  VAR_NAME=AGENTS_MD_CALLERS TAG=agents-md-integrity WORKFLOW_FILE=agents-md-integrity.yml ALLOW_EMPTY=true \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-emptyref","file":".github/workflows/agents-md-integrity.yml","label":""}]'
check "exit 1 — the repo failed"                 "[[ $RC -eq 1 ]]"
check "warning names the empty pin"              "grep -qF '(empty)' <<<\"\$OUT\""
check "committed NOTHING"                        "[[ ! -f \"\$STUB_PUT_DIR/count\" ]]"

echo "== an ALREADY-CONVERTED 'main (<short>)' marker is refreshed, not frozen (BE-4523) =="
# The legacy `# github-workflows#NN` rule only fires once. After a caller has
# been migrated to the `main (<short>)` marker, every later bump used to advance
# the real 40-hex pin and leave the human-readable annotation at the SHA the file
# no longer uses (Comfy-iOS shipped exactly that and hand-fixed it). Both marker
# spellings must track the pin: the prose comment ABOVE the `uses:` line and the
# trailing comment ON it.
new_case converted
CONVERTED_FIXTURE="${WORK}/converted_caller.yml"
printf '%s\n' \
  'name: CI cursor-review' \
  'jobs:' \
  '  review:' \
  '    # Pinned to github-workflows main (1111111). The bump-cursor-review-callers' \
  '    # workflow auto-opens a SHA-bump PR here when cursor-review.yml changes' \
  '    # upstream; keep workflows_ref matching.' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@1111111111111111111111111111111111111111 # github-workflows main (1111111)' \
  '    with:' \
  '      workflows_ref: 1111111111111111111111111111111111111111' \
  > "$CONVERTED_FIXTURE"
STUB_CONTENT_FILE="$CONVERTED_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-converted","file":".github/workflows/ci-cursor-review.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0" "[[ $RC -eq 0 ]]"
check "staged the file (not a skip)"            "[[ \$(cat \"\$STUB_PUT_DIR/count\") -eq 1 ]]"
check "both SHA refs bumped"                    "[[ \$(grep -cF '$NEW_SHA' \"$PUT\") -eq 2 ]]"
check "old 40-hex pin removed"                  "! grep -qF '1111111111111111111111111111111111111111' \"$PUT\""
check "no stale short SHA left in any marker"   "! grep -qF 'main (1111111)' \"$PUT\""
check "BOTH markers refreshed to the new short" "[[ \$(grep -cF 'github-workflows main ($SHORT)' \"$PUT\") -eq 2 ]]"
check "prose marker above uses: refreshed"      "grep -qF '# Pinned to github-workflows main ($SHORT).' \"$PUT\""
check "no attribution warning for a single-reusable caller" \
  "! grep -q 'pin comments untouched' <<<\"\$OUT\""

echo "== a SECOND reusable in the same file keeps BOTH its pin and its marker (BE-4523) =="
# A `main (<short>)` marker names a SHA but not WHICH reusable it annotates, so a
# file calling several github-workflows reusables gives no honest way to tell
# whose marker is whose. The bumper must refuse to guess: leave every marker as
# found and warn, rather than stamping this fleet's SHA onto a sibling fleet's
# annotation (Comfy-iOS pins cursor-review-auto-label.yml independently, at its
# own older SHA — in a separate file today, but the guard is what keeps a
# single-file variant from regressing).
#
# The sibling's REAL PIN matters at least as much as its comment: the 40-hex
# substitution used to be addressed at any `/github-workflows/` line, so it
# repinned the sibling's `uses: …@<40-hex>` to THIS fleet's SHA — pointing that
# caller at a commit its own fleet never shipped. Asserting only the comment here
# would have let that clobber stay green, so both are asserted.
new_case multireusable
MULTI_FIXTURE="${WORK}/multi_caller.yml"
printf '%s\n' \
  'name: CI cursor-review' \
  'jobs:' \
  '  review:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@1111111111111111111111111111111111111111 # github-workflows main (1111111)' \
  '  auto-label:' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review-auto-label.yml@2222222222222222222222222222222222222222 # github-workflows#27 # main @ 2222222' \
  '    with:' \
  '      workflows_ref: 1111111111111111111111111111111111111111  # main @ 1111111' \
  > "$MULTI_FIXTURE"
STUB_CONTENT_FILE="$MULTI_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-multi","file":".github/workflows/ci-cursor-review.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0" "[[ $RC -eq 0 ]]"
check "warned that pin comments were left alone" "grep -q 'pin comments untouched' <<<\"\$OUT\""
check "OUR reusable's pin still bumped"          "grep -q \"cursor-review.yml@$NEW_SHA\" \"$PUT\""
check "the SIBLING's 40-hex pin is UNCHANGED"    "grep -qF 'cursor-review-auto-label.yml@2222222222222222222222222222222222222222' \"$PUT\""
check "the sibling's legacy marker is untouched" "grep -qF '# github-workflows#27' \"$PUT\""
check "our own marker left alone (ambiguous)"    "grep -qF 'github-workflows main (1111111)' \"$PUT\""
check "this fleet's short SHA was NOT stamped in" "! grep -qF 'github-workflows main ($SHORT)' \"$PUT\""
# The `# main @ <short>` form is attributed differently from the two markers above:
# it rides ON the pin line, so it moves with whatever the 40-hex rule moves — same
# address, both regimes. That makes the multi-reusable case a two-sided assertion:
#   * the SIBLING's `# main @` note shares its line with a pin the tightened address
#     deliberately does NOT bump, so stamping the note would be the same cross-fleet
#     lie as stamping the pin, and
#   * a `workflows_ref:` pin IS bumped even here (that context stays broad — an
#     un-bumped workflows_ref disagreeing with its own `uses:` is the worse
#     failure), so its own note MUST move with it or the guard manufactures exactly
#     the stale comment BE-4346 removed.
# Unreachable today (no caller calls two reusables); asserted so the guard and the
# groom fleet's lock-step cannot silently trade one for the other.
check "sibling's '# main @' note NOT stamped"    "grep -qF '# main @ 2222222' \"$PUT\""
check "workflows_ref pin bumped even here"       "grep -qE \"workflows_ref: ${NEW_SHA}\" \"$PUT\""
check "workflows_ref's '# main @' moved with it" \
  "grep -qE \"workflows_ref: ${NEW_SHA} +# main @ ${SHORT}\\\$\" \"$PUT\""

echo "== merely NAMING a sibling workflow in a comment is not a second caller (BE-4523) =="
# The multi-reusable check reads `uses:` lines only. A single-reusable caller that
# mentions a sibling workflow in prose (or a docs URL, or a commented-out block)
# must NOT be misread as multi-reusable — that would suppress the marker refresh
# and leave the annotation stale, which is exactly the bug BE-4523 fixes.
new_case mentiononly
MENTION_FIXTURE="${WORK}/mention_caller.yml"
printf '%s\n' \
  'name: CI cursor-review' \
  '# Labels come from github-workflows/.github/workflows/cursor-review-auto-label.yml' \
  '# (see also https://github.com/Comfy-Org/github-workflows/.github/workflows/groom.yml)' \
  'jobs:' \
  '  review:' \
  '    # uses: Comfy-Org/github-workflows/.github/workflows/pr-size.yml@3333333333333333333333333333333333333333' \
  '    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@1111111111111111111111111111111111111111 # github-workflows main (1111111)' \
  > "$MENTION_FIXTURE"
STUB_CONTENT_FILE="$MENTION_FIXTURE" run_bump \
  VAR_NAME=CURSOR_REVIEW_CALLERS TAG=cursor-review WORKFLOW_FILE=cursor-review.yml \
  CALLERS_JSON='[{"repo":"Comfy-Org/secret-mention","file":".github/workflows/ci-cursor-review.yml","label":""}]'
PUT="${STUB_PUT_DIR}/put.last.txt"
check "exit 0" "[[ $RC -eq 0 ]]"
check "no spurious attribution warning"          "! grep -q 'pin comments untouched' <<<\"\$OUT\""
check "the marker WAS refreshed"                 "grep -qF 'github-workflows main ($SHORT)' \"$PUT\""
check "no stale short SHA left behind"           "! grep -qF 'main (1111111)' \"$PUT\""

echo "== STATIC: every fleet feeds CALLERS_JSON from a SECRET, never a variable (BE-6472) =="
# Not a run of the script — a source check of the entrypoints that feed it.
# A roster passed as `${{ vars.X }}` is printed IN THE CLEAR in the step's env
# dump, which Actions emits BEFORE the step (and therefore before any
# `::add-mask::` the script could run) — so on this PUBLIC repo it published
# every private caller name in a world-readable log. `${{ secrets.X }}` is
# runner-masked everywhere, that dump included. Nothing about a `vars.` binding
# looks wrong at review time, and no functional test can catch it (the script
# sees an identical string either way), so the guard has to be static and has to
# cover the WHOLE glob: a fleet added later must not be able to reintroduce the
# leak just by copying a pre-migration entrypoint.
WF_DIR="${SCRIPT_DIR}/../../workflows"
GUARD_FILES=("$WF_DIR"/bump-*-callers.yml)
check "found the fleet entrypoints to guard" "(( ${#GUARD_FILES[@]} >= 6 ))"
for GF in "${GUARD_FILES[@]}"; do
  GNAME=$(basename "$GF")
  # An unmatched glob comes back as the literal pattern, and `! grep` on a file
  # that does not exist "passes" — so prove the file is real before trusting a
  # negative grep over it. (The count check above already fails in that case;
  # this keeps the per-file lines from reporting a green they did not earn.)
  check "${GNAME}: entrypoint file exists" "[[ -f \"$GF\" ]]"
  # (a) no `vars.` binding anywhere in the file.
  check "${GNAME}: no CALLERS_JSON from vars." \
    "! grep -qE '^[[:space:]]*CALLERS_JSON:[[:space:]]*\\\$\\{\\{[[:space:]]*vars\\.' \"$GF\""
  # (b) every CALLERS_JSON binding is a bare secrets.<NAME> expression. Counting
  #     both sides (rather than just grepping for one good line) is what makes a
  #     SECOND, differently-spelled binding — or one wrapped in a fallback like
  #     `secrets.X || vars.X` — fail instead of hiding behind a compliant sibling.
  # shellcheck disable=SC2034  # read by check()'s eval
  N_BIND=$(grep -cE '^[[:space:]]*CALLERS_JSON:' "$GF")
  # shellcheck disable=SC2034  # read by check()'s eval
  N_SECRET=$(grep -cE '^[[:space:]]*CALLERS_JSON:[[:space:]]*\$\{\{ secrets\.[A-Z_]+ \}\}[[:space:]]*$' "$GF")
  check "${GNAME}: has exactly one CALLERS_JSON binding" "(( N_BIND == 1 ))"
  check "${GNAME}: that binding is \${{ secrets.<NAME> }}" "(( N_SECRET == N_BIND ))"
done

echo
echo "== $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
