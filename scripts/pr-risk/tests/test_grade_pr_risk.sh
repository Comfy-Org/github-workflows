#!/usr/bin/env bash
# test_grade_pr_risk.sh — hermetic tests for grade-pr-risk.sh. No network: the grading phases
# feed synthetic scorecard records to --stdin, and the live-PR phase stubs `gh` on PATH with a
# fixture GraphQL response. Ported from the fleet's offline grader suite (BE-5507) — the
# safety properties proven there are re-proven here against the extracted script:
#   * WORST-WINS: an R0 path rule cannot cancel an R3 one; a runbook (provenance R0) still
#     grades R3 when the path floor says R3 — an axis may only ever move a PR RISKIER.
#   * PROVENANCE ALONE IS NEVER SUFFICIENT: a runbook IDENTITY whose diff SHAPE does not
#     assert is not a runbook, and the failure is recorded.
#   * EXTERNAL IS NEVER OVERRIDDEN: a fork imitating a runbook's shape is still external R3.
#   * EXTERNAL IS ABOUT FORKS AND FIRST-TIME HUMANS (new here): a repo-owned App authors with
#     `author_association: NONE`, so it is a runbook candidate rather than an outsider — while a
#     bot on a FORK, and a first-time human, both stay external R3.
#   * THE UNKNOWN CONTRACT: an unreadable input is tier null + status unknown + exit 1,
#     never a confident tier; a structurally empty map is refused outright (exit 2).
#   * THE GRADE CARRIES ITS MAP VERSION so a map revision can be replayed later.
#   * SELF-EXCLUDING ROLLUP (new here): with --self-context, the grading workflow's own
#     in-progress check run does not floor every live grade at R2.
#
#   bash tests/test_grade_pr_risk.sh        # exit 0 = all green

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRADER="$SELF_DIR/../grade-pr-risk.sh"
[ -f "$GRADER" ] || { echo "FATAL: $GRADER not found" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found on PATH" >&2; exit 2; }

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pr-risk-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { # <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi
}

# rec — one scorecard-shaped record, mirroring what fetch_pr_record emits. The status twins
# default to `ok` (the fully-collected case); phases that want an unread field say so.
rec() { # <pr> <author> <title> <paths-json> <paths-status> <checks> [head_ref] [assoc] [fork]
  jq -cn --argjson pr "$1" --arg author "$2" --arg title "$3" --argjson paths "$4" \
         --arg pstatus "$5" --arg checks "$6" --arg head "${7:-feature-branch}" \
         --arg assoc "${8:-MEMBER}" --argjson fork "${9:-false}" '
    {repo:"test/repo", pr:$pr, title:$title, author:$author, author_association:$assoc,
     is_fork:$fork, labels:[], head_ref:$head, additions:10, deletions:5, changed_files:($paths | if . == null then 99 else length end),
     checks_state:(if $checks == "null" then null else $checks end),
     checks_status:"ok", provenance_status:"ok",
     changed_paths:$paths, changed_paths_status:$pstatus}'
}

grade() { bash "$GRADER" --stdin 2>/dev/null; }

echo "— phase 1: worst-wins on the path floor —"
# docs (R0 rule) + migration (R3 rule) in one PR: the R0 rule must not cancel the R3 one.
out="$(rec 1 dev 'fix: tweak' '[{"path":"README.md","additions":1,"deletions":0,"change_type":"MODIFIED"},{"path":"db/migrations/0001_x.sql","additions":9,"deletions":0,"change_type":"ADDED"}]' ok SUCCESS | grade)"
eq "docs cannot cancel migrations" R3 "$(jq -r '.risk.axes.path_floor.tier' <<<"$out")"
eq "overall is R3" R3 "$(jq -r '.risk.tier' <<<"$out")"

echo "— phase 2: a runbook cannot buy its way past the path floor —"
out="$(rec 2 'dependabot[bot]' 'chore(deps): bump x from 1 to 2' '[{"path":"go.mod","additions":1,"deletions":1,"change_type":"MODIFIED"},{"path":"go.sum","additions":2,"deletions":2,"change_type":"MODIFIED"}]' ok SUCCESS 'dependabot/go_modules/x-2' CONTRIBUTOR | grade)"
eq "provenance is runbook" runbook "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "provenance proposes R0" R0 "$(jq -r '.risk.axes.provenance.tier' <<<"$out")"
eq "path floor still decides R3" R3 "$(jq -r '.risk.tier' <<<"$out")"

echo "— phase 3: provenance alone is never sufficient (shape assertion) —"
# dependabot's identity, but the diff touches a path outside its permitted set.
out="$(rec 3 'dependabot[bot]' 'chore(deps): bump x' '[{"path":"src/evil.go","additions":9,"deletions":0,"change_type":"MODIFIED"}]' ok SUCCESS 'dependabot/go_modules/x-2' CONTRIBUTOR | grade)"
eq "not classified runbook" human "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
sf="$(jq -r '.risk.axes.provenance.shape_failures | length' <<<"$out")"
if [ "$sf" -ge 1 ]; then ok "shape failure recorded"; else bad "shape failure recorded" "$sf"; fi

echo "— phase 4: external is never overridden by a runbook match —"
out="$(rec 4 'dependabot[bot]' 'chore(deps): bump x from 1 to 2' '[{"path":"go.mod","additions":1,"deletions":1,"change_type":"MODIFIED"}]' ok SUCCESS 'dependabot/go_modules/x-2' NONE true | grade)"
eq "fork stays external" external "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "external grades R3" R3 "$(jq -r '.risk.tier' <<<"$out")"

echo "— phase 5: the unknown contract —"
out="$(rec 5 dev 'mystery' null unknown SUCCESS | grade)"
eq "unknown axis nulls the tier" null "$(jq -r '.risk.tier' <<<"$out")"
eq "overall status is unknown" unknown "$(jq -r '.risk.status' <<<"$out")"
rec 5 dev 'mystery' null unknown SUCCESS | bash "$GRADER" --stdin >/dev/null 2>&1
eq "unknown exits 1" 1 "$?"

echo "— phase 6: the grade carries its map + registry versions —"
out="$(rec 6 dev 'docs: x' '[{"path":"README.md","additions":1,"deletions":0,"change_type":"MODIFIED"}]' ok SUCCESS | grade)"
eq "map version stamped" v0-generic "$(jq -r '.risk.map_version' <<<"$out")"
eq "registry version stamped" v0-generic "$(jq -r '.risk.registry_version' <<<"$out")"

echo "— phase 7: a structurally empty map is refused outright —"
echo "— per-file path floors are REPORTING ONLY: worst(files) == the floor itself —"
# publish-risk-surfaces.sh renders one row per file from risk.axes.path_floor.files. Those
# per-file floors are matched with the SAME rules as the floor, so `worst` over them must equal
# the floor — otherwise the breakdown has quietly become a second grading model that can disagree
# with the tier printed above it.
# ONE line: --stdin is JSONL, and a pretty-printed record is dropped by the per-line reader.
inv="$(grade <<<'{"changed_paths_status":"ok","author":"someone","is_fork":false,"labels":[],"checks_status":"ok","checks_state":"SUCCESS","provenance_status":"ok","changed_paths":[{"path":".github/workflows/a.yml","change_type":"MODIFIED"},{"path":"docs/a.md","change_type":"MODIFIED"},{"path":"src/plain.go","change_type":"MODIFIED"}]}')"
eq "the floor is R3 (the ci rule)" "R3" "$(jq -r '.risk.axes.path_floor.tier' <<<"$inv")"
eq "worst over the per-file floors equals it" "R3" \
   "$(jq -r '[.risk.axes.path_floor.files[].tier] | map({"R0":0,"R1":1,"R2":2,"R3":3}[.]) | max
             | ["R0","R1","R2","R3"][.]' <<<"$inv")"
eq "every changed file gets exactly one row" 3 "$(jq '.risk.axes.path_floor.files | length' <<<"$inv")"
eq "an unmapped path falls to the map default, not to the floor" "R0" \
   "$(jq -r '.risk.axes.path_floor.files[] | select(.path == "src/plain.go") | .tier' <<<"$inv")"
eq "the docs rule keeps its own R0 row under an R3 floor" "R0" \
   "$(jq -r '.risk.axes.path_floor.files[] | select(.path == "docs/a.md") | .tier' <<<"$inv")"

printf '{}' > "$SANDBOX/empty-map.json"
rec 7 dev 'docs: x' '[{"path":"README.md","additions":1,"deletions":0,"change_type":"MODIFIED"}]' ok SUCCESS \
  | bash "$GRADER" --stdin --map "$SANDBOX/empty-map.json" >/dev/null 2>&1
eq "empty map exits 2" 2 "$?"

echo "— phase 8: reversibility floors and rungs —"
out="$(rec 8 dev 'docs: x' '[{"path":"README.md","additions":1,"deletions":0,"change_type":"MODIFIED"}]' ok PENDING | grade)"
eq "pending checks floor reversibility at R2" R2 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
out="$(rec 9 dev 'test: cover x' '[{"path":"pkg/x_test.go","additions":9,"deletions":0,"change_type":"ADDED"}]' ok SUCCESS | grade)"
eq "green + test touched grades reversibility R0" R0 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
eq "human provenance keeps overall at R1" R1 "$(jq -r '.risk.tier' <<<"$out")"

echo "— phase 9: --pr with a stubbed gh: the self-excluding rollup —"
# Fixture: our own workflow's check run is IN_PROGRESS (it always is, mid-run); one other
# workflow's run has completed SUCCESS. Raw rollup is PENDING; excluding self it is SUCCESS.
# The changed-file list is a SEPARATE REST call (previous_filename lives only there), so the
# stub dispatches on the request and applies `--jq` the way gh does.
mkdir -p "$SANDBOX/bin"
export FIXTURE_DIR="$SANDBOX"
cat > "$SANDBOX/fixture.json" <<'FIX'
{"data":{"repository":{"pullRequest":{
  "number":42,"title":"docs: tweak readme","state":"OPEN","isDraft":false,
  "createdAt":"2026-08-01T00:00:00Z","updatedAt":"2026-08-01T00:10:00Z","closedAt":null,"mergedAt":null,
  "author":{"login":"dev"},"authorAssociation":"MEMBER","baseRefName":"main","headRefName":"docs-tweak",
  "isCrossRepository":false,"additions":3,"deletions":1,"changedFiles":1,
  "labels":{"pageInfo":{"hasNextPage":false},"nodes":[]},
  "commits":{"nodes":[{"commit":{"oid":"c0ffee1234567890abcdef1234567890abcdef12","statusCheckRollup":{"state":"PENDING","contexts":{
    "pageInfo":{"hasNextPage":false},
    "nodes":[
      {"__typename":"CheckRun","name":"Grade PR risk","status":"IN_PROGRESS","conclusion":null,
       "checkSuite":{"workflowRun":{"databaseId":999,"workflow":{"name":"CI - PR Risk Grade"}}}},
      {"__typename":"CheckRun","name":"unit tests","status":"COMPLETED","conclusion":"SUCCESS",
       "checkSuite":{"workflowRun":{"databaseId":1000,"workflow":{"name":"CI"}}}}
    ]}}}}]}
}}}}
FIX
cat > "$SANDBOX/files.json" <<'FIX'
[{"filename":"README.md","additions":3,"deletions":1,"status":"modified"}]
FIX
# `gh api` stub: graphql -> the PR fixture, pulls/{n}/files -> the REST file fixture, and a
# --jq filter is applied to the chosen fixture exactly as gh would.
cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
fixture=""; filter=""
for ((i=1; i<=$#; i++)); do
  a="${!i}"
  case "$a" in
    graphql)          fixture="$FIXTURE_DIR/fixture.json" ;;
    *pulls/*/files*)  fixture="$FIXTURE_DIR/files.json" ;;
    --jq)             n=$((i+1)); filter="${!n}" ;;
  esac
done
[ -n "$fixture" ] || { echo "gh stub: unhandled args: $*" >&2; exit 1; }
if [ -n "$filter" ]; then jq -c "$filter" "$fixture"; else cat "$fixture"; fi
STUB
chmod +x "$SANDBOX/bin/gh"
graded_pr() { PATH="$SANDBOX/bin:$PATH" bash "$GRADER" --repo test/repo --pr 42 "$@" 2>/dev/null; }

out="$(graded_pr --self-context 'CI - PR Risk Grade')"
eq "self-excluded rollup reads SUCCESS (by name)" SUCCESS "$(jq -r '.checks_state' <<<"$out")"
# THE RECORD NAMES THE COMMIT IT GRADED. Without it a publish surface can only re-read "head",
# and grading deliberately waits out the rollup settle — so a push in that window would attach
# this commit's tier to a different commit as an immutable Check Run.
eq "the record carries the graded head sha" \
   "c0ffee1234567890abcdef1234567890abcdef12" "$(jq -r '.head_sha' <<<"$out")"
eq "nothing else pending" false "$(jq -r '.checks_pending_excl_self' <<<"$out")"
eq "live docs PR grades R1" R1 "$(jq -r '.risk.tier' <<<"$out")"
# --self-run-id is the EXACT selector: same result, but keyed on github.run_id, so a
# same-named workflow elsewhere in the consumer repo is no longer swept out of the rollup.
out="$(graded_pr --self-run-id 999)"
eq "self-excluded rollup reads SUCCESS (by run id)" SUCCESS "$(jq -r '.checks_state' <<<"$out")"
# A run id that is NOT ours excludes nothing, so our own in-progress run still reads PENDING.
out="$(graded_pr --self-run-id 12345)"
eq "a foreign run id excludes nothing" PENDING "$(jq -r '.checks_state' <<<"$out")"
# Same fixture with NO self selector: the raw rollup (PENDING) must come through untouched —
# that is the offline/terminal behavior, where no self run is in flight.
out="$(graded_pr)"
eq "raw rollup untouched without a self selector" PENDING "$(jq -r '.checks_state' <<<"$out")"
# Flip the other workflow's run to QUEUED: excluding self must now report pending=true.
jq '.data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.contexts.nodes[1].status = "QUEUED"
    | .data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.contexts.nodes[1].conclusion = null' \
  "$SANDBOX/fixture.json" > "$SANDBOX/f2.json" && cp "$SANDBOX/f2.json" "$SANDBOX/fixture.json"
out="$(graded_pr --self-run-id 999)"
eq "other pending check reports pending" true "$(jq -r '.checks_pending_excl_self' <<<"$out")"

echo "— phase 10: self-exclusion can never HIDE a red check —"
# The failing check belongs to OUR OWN run (the case a consumer creates by putting the grading
# job inside its existing CI workflow — every sibling job then shares our run id). Excluding it
# from the rollup would aggregate the remaining green contexts to SUCCESS and grade a RED PR
# R0/R1, so the FAILURE scan deliberately covers self too.
jq '.data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.contexts.nodes[1] =
      {"__typename":"CheckRun","name":"unit tests","status":"COMPLETED","conclusion":"FAILURE",
       "checkSuite":{"workflowRun":{"databaseId":999,"workflow":{"name":"CI - PR Risk Grade"}}}}' \
  "$SANDBOX/fixture.json" > "$SANDBOX/f2.json" && cp "$SANDBOX/f2.json" "$SANDBOX/fixture.json"
out="$(graded_pr --self-run-id 999)"
eq "a failing check in our own run still reads FAILURE" FAILURE "$(jq -r '.checks_state' <<<"$out")"
eq "and reversibility cannot go below R2" R2 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"

echo "— phase 11: a rollup of nothing but SKIPPED is not a green rollup —"
# SKIPPED / NEUTRAL / null conclusions establish NOTHING about whether tests covering these
# lines ran, which is the reversibility axis's entire question — so they must not aggregate to
# SUCCESS and let the axis drop to R0/R1.
jq '.data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.contexts.nodes =
      [{"__typename":"CheckRun","name":"Grade PR risk","status":"IN_PROGRESS","conclusion":null,
        "checkSuite":{"workflowRun":{"databaseId":999,"workflow":{"name":"CI - PR Risk Grade"}}}},
       {"__typename":"CheckRun","name":"unit tests","status":"COMPLETED","conclusion":"SKIPPED",
        "checkSuite":{"workflowRun":{"databaseId":1000,"workflow":{"name":"CI"}}}}]' \
  "$SANDBOX/fixture.json" > "$SANDBOX/f2.json" && cp "$SANDBOX/f2.json" "$SANDBOX/fixture.json"
out="$(graded_pr --self-run-id 999)"
eq "all-SKIPPED does not aggregate to SUCCESS" NEUTRAL "$(jq -r '.checks_state' <<<"$out")"
eq "no green rollup floors reversibility at R2" R2 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"

echo "— phase 12: a SHORT changed-file read is unknown, never a floor from the files that fit —"
# GraphQL says 5 changed files, the file endpoint returned 1. The old `files(first:100)` path
# graded every PR above the cap `unknown`; the REST read pages past it, but a read that comes
# back SHORT of changedFiles is still an unread input and must refuse to grade.
jq '.data.repository.pullRequest.changedFiles = 5' "$SANDBOX/fixture.json" > "$SANDBOX/f2.json" \
  && cp "$SANDBOX/f2.json" "$SANDBOX/fixture.json"
out="$(graded_pr --self-run-id 999)"
eq "short file read is unknown" unknown "$(jq -r '.changed_paths_status' <<<"$out")"
eq "and the overall grade refuses" null "$(jq -r '.risk.tier' <<<"$out")"

echo "— phase 13: a RENAME cannot walk a file out of its guarded directory —"
# The origin path is graded too: `git mv src/auth/x.go misc/x.go` used to be recorded under the
# destination ALONE, so the R3 `auth` rule never matched and the move escaped the floor.
out="$(rec 13 dev 'refactor: move things' '[{"path":"misc/x.go","previous_path":"src/auth/x.go","additions":1,"deletions":1,"change_type":"RENAMED"}]' ok SUCCESS | grade)"
eq "the origin path still hits the auth floor" R3 "$(jq -r '.risk.axes.path_floor.tier' <<<"$out")"
eq "renaming a file out of a sensitive class is not a clean revert" R3 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"

echo "— phase 14: 'deletes a sensitive file' means the DELETED files, not any changed file —"
# MODIFIES an auth file and DELETES an unrelated README. The sensitive-class match used to run
# over every changed file, so this reported "deletes N file(s) under a sensitive class" and
# pinned reversibility R3 — a true tier from a false sentence.
out="$(rec 14 dev 'chore: tidy' '[{"path":"src/auth/x.go","additions":2,"deletions":1,"change_type":"MODIFIED"},{"path":"README.md","additions":0,"deletions":9,"change_type":"DELETED"}]' ok SUCCESS | grade)"
eq "deleting a doc is not deleting an auth file" R1 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
eq "the auth path floor still decides the grade" R3 "$(jq -r '.risk.tier' <<<"$out")"
# ...and a genuinely deleted auth file still pins R3.
out="$(rec 15 dev 'chore: drop it' '[{"path":"src/auth/x.go","additions":0,"deletions":9,"change_type":"DELETED"}]' ok SUCCESS | grade)"
eq "deleting an auth file is R3 on reversibility" R3 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"

echo "— phase 15: the default map's globs match at DEPTH, not just at the repo root —"
# `*` does not cross `/`, so an unprefixed glob compiles to a root-only match and the rule
# silently matches nothing in a real tree. The grader + its map are R3: a PR that edits the
# judge must not be graded safest by that judge.
out="$(rec 16 dev 'chore: retune' '[{"path":"scripts/pr-risk/risk-map.v0.json","additions":3,"deletions":1,"change_type":"MODIFIED"}]' ok SUCCESS | grade)"
eq "a nested risk map is R3" R3 "$(jq -r '.risk.axes.path_floor.tier' <<<"$out")"
out="$(rec 17 dev 'chore: retune' '[{"path":"scripts/pr-risk/grade-pr-risk.sh","additions":3,"deletions":1,"change_type":"MODIFIED"}]' ok SUCCESS | grade)"
eq "the grader itself is R3" R3 "$(jq -r '.risk.axes.path_floor.tier' <<<"$out")"
# A shell test OUTSIDE a tests/ directory must still match the R0 tests class.
out="$(rec 18 dev 'test: smoke' '[{"path":"hack/smoke-test.sh","additions":3,"deletions":0,"change_type":"ADDED"}]' ok SUCCESS | grade)"
if jq -e '.risk.axes.path_floor.classes | index("tests")' >/dev/null <<<"$out"; then
  ok "a nested *-test.sh matches the tests class"
else bad "a nested *-test.sh matches the tests class" "$(jq -c '.risk.axes.path_floor.classes' <<<"$out")"; fi

echo "— phase 16: 'did a test file change?' comes from the MAP, so every ecosystem can answer —"
# The built-in regex knows only the Go/TS shapes, so a Python or Java consumer could never
# reach clean_tier and sat at R1 forever. test_path_patterns in the map is the fix.
for p in pkg/test_foo.py pkg/foo_test.py app/FooTest.java spec/foo_spec.rb; do
  out="$(rec 19 dev 'test: cover it' "[{\"path\":\"$p\",\"additions\":9,\"deletions\":0,\"change_type\":\"ADDED\"}]" ok SUCCESS | grade)"
  eq "$p counts as a touched test" R0 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
done

echo "— phase 17: a map that forgets a provenance class is refused, not guessed —"
# `{}`-shaped omissions used to pass validation and then grade forks off a fallback tier
# nobody chose, silently retiring "external is R3, no exceptions".
jq 'del(.provenance_tiers.external)' "$SELF_DIR/../risk-map.v0.json" > "$SANDBOX/no-external.json"
rec 20 dev 'docs: x' '[{"path":"README.md","additions":1,"deletions":0,"change_type":"MODIFIED"}]' ok SUCCESS \
  | bash "$GRADER" --stdin --map "$SANDBOX/no-external.json" >/dev/null 2>&1
eq "a map missing 'external' exits 2" 2 "$?"

echo "— phase 18: a TRUNCATED label list cannot answer 'is this agent-coded?' —"
out="$(rec 21 dev 'feat: x' '[{"path":"src/x.go","additions":9,"deletions":0,"change_type":"MODIFIED"}]' ok SUCCESS \
       | jq -c '.labels_status = "unknown"' | grade)"
eq "truncated labels make provenance unknown" unknown "$(jq -r '.risk.axes.provenance.status' <<<"$out")"
eq "and the overall grade refuses" null "$(jq -r '.risk.tier' <<<"$out")"

echo "— phase 19: an UNREADABLE PR exits 3 and says why, in GitHub's own words —"
# A short token grant (no `actions: read`) fails the rollup's checkSuite -> workflowRun hop, and
# `gh api graphql` exits non-zero on any top-level `errors` entry even when `data` is present —
# so a misconfigured caller is an unreadable PR, not a partial record. That is the RIGHT
# behaviour (a nulled workflowRun would silently break self-exclusion), but it used to be
# undiagnosable: the caller retries rc=3 four times and labels `ungraded`, and with gh's stderr
# discarded a permanent misconfiguration looked exactly like a rate-limit blip.
mkdir -p "$SANDBOX/bin403"
cat > "$SANDBOX/bin403/gh" <<'STUB'
#!/usr/bin/env bash
for a in "$@"; do
  [ "$a" = graphql ] || continue
  echo 'gh: Although you appear to have the correct authorization credentials, the `actions` scope is required (FORBIDDEN)' >&2
  exit 1
done
exit 0
STUB
chmod +x "$SANDBOX/bin403/gh"
err="$(PATH="$SANDBOX/bin403:$PATH" bash "$GRADER" --repo test/repo --pr 42 \
         --self-run-id 999 2>&1 >/dev/null)"
eq "an unreadable PR exits 3 (retryable, never graded)" 3 "$?"
case "$err" in
  *FORBIDDEN*) ok "gh's reason for the failed PR read reaches the log" ;;
  *) bad "gh's reason for the failed PR read reaches the log" "$err" ;;
esac
case "$err" in
  *"NOT 'no risk'"*) ok "and the warning still refuses to read as low risk" ;;
  *) bad "and the warning still refuses to read as low risk" "$err" ;;
esac

echo "— phase 20: a check rollup deeper than one page is DRAINED, not abandoned —"
# GraphQL caps ANY connection page at 100, so a rollup with more checks than that came back
# `hasNextPage: true` and the whole PR graded UNKNOWN. Measured on Comfy-Org/cloud: an ordinary
# code PR carries 103 checks and graded `risk:ungraded`, while the enrollment PR (66 checks)
# graded fine — so the bug was invisible until real traffic hit it, and the busier a repo's CI
# the more of it went ungraded.
#
# The paging stub answers page 1 when no cursor is passed and page 2 when `cursor=C1` is. OUR
# OWN check deliberately lives on PAGE 2: a drain that dropped page-2 nodes, or fetched them
# with a narrower field selection than page 1, would silently stop excluding self and read
# PENDING forever.
mkdir -p "$SANDBOX/binpage"
cat > "$SANDBOX/page1.json" <<'FIX'
{"data":{"repository":{"pullRequest":{
  "number":42,"title":"docs: tweak readme","state":"OPEN","isDraft":false,
  "createdAt":"2026-08-01T00:00:00Z","updatedAt":"2026-08-01T00:10:00Z","closedAt":null,"mergedAt":null,
  "author":{"login":"dev"},"authorAssociation":"MEMBER","baseRefName":"main","headRefName":"docs-tweak",
  "isCrossRepository":false,"additions":3,"deletions":1,"changedFiles":1,
  "labels":{"pageInfo":{"hasNextPage":false},"nodes":[]},
  "commits":{"nodes":[{"commit":{"statusCheckRollup":{"state":"PENDING","contexts":{
    "pageInfo":{"hasNextPage":true,"endCursor":"C1"},
    "nodes":[
      {"__typename":"CheckRun","name":"unit tests","status":"COMPLETED","conclusion":"SUCCESS",
       "checkSuite":{"workflowRun":{"databaseId":1000,"workflow":{"name":"CI"}}}}
    ]}}}}]}
}}}}
FIX
cat > "$SANDBOX/page2.json" <<'FIX'
{"data":{"repository":{"pullRequest":{
  "commits":{"nodes":[{"commit":{"statusCheckRollup":{"contexts":{
    "pageInfo":{"hasNextPage":false,"endCursor":null},
    "nodes":[
      {"__typename":"CheckRun","name":"Grade PR risk","status":"IN_PROGRESS","conclusion":null,
       "checkSuite":{"workflowRun":{"databaseId":999,"workflow":{"name":"CI - PR Risk Grade"}}}}
    ]}}}}]}
}}}}
FIX
cat > "$SANDBOX/binpage/gh" <<'STUB'
#!/usr/bin/env bash
fixture=""; filter=""; paged=0
for ((i=1; i<=$#; i++)); do
  a="${!i}"
  case "$a" in
    cursor=*)         paged=1 ;;
    graphql)          fixture="$FIXTURE_DIR/page1.json" ;;
    *pulls/*/files*)  fixture="$FIXTURE_DIR/files.json" ;;
    --jq)             n=$((i+1)); filter="${!n}" ;;
  esac
done
if [ "$paged" -eq 1 ]; then
  [ -z "${FAILPAGE:-}" ] || { echo "HTTP 502" >&2; exit 1; }
  fixture="$FIXTURE_DIR/${PAGE2:-page2.json}"
fi
[ -n "$fixture" ] || { echo "gh stub: unhandled args: $*" >&2; exit 1; }
if [ -n "$filter" ]; then jq -c "$filter" "$fixture"; else cat "$fixture"; fi
STUB
chmod +x "$SANDBOX/binpage/gh"
paged_pr() { PATH="$SANDBOX/binpage:$PATH" bash "$GRADER" --repo test/repo --pr 42 "$@" 2>/dev/null; }

out="$(paged_pr --self-run-id 999)"
eq "a two-page rollup grades instead of going unknown" ok "$(jq -r '.risk.status' <<<"$out")"
# Self lives on page 2, so this only reads SUCCESS if the drained page was both fetched AND
# carried the checkSuite.workflowRun.databaseId that is_self keys on.
eq "self-exclusion still works past the page boundary" SUCCESS "$(jq -r '.checks_state' <<<"$out")"
eq "drained rollup reports nothing else pending" false "$(jq -r '.checks_pending_excl_self' <<<"$out")"

# A page-2 read that FAILS must leave the record's hasNextPage set — the honest ungraded
# outcome — never a grade computed from page 1 alone.
out="$(FAILPAGE=1 paged_pr --self-run-id 999)"
eq "a failed rollup page stays UNKNOWN, never a partial grade" unknown "$(jq -r '.risk.status' <<<"$out")"

# A cursor that repeats would spin until the page guard; it must stop and stay unknown.
jq '.data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.contexts.pageInfo
      = {"hasNextPage":true,"endCursor":"C1"}' "$SANDBOX/page2.json" > "$SANDBOX/page2stuck.json"
out="$(PAGE2=page2stuck.json paged_pr --self-run-id 999)"
eq "a non-advancing cursor stops and stays UNKNOWN" unknown "$(jq -r '.risk.status' <<<"$out")"

# The page guard is a runaway bound, not a policy: past it the PR stays ungraded rather than
# being graded off the pages that fit.
out="$(PR_RISK_MAX_ROLLUP_PAGES=0 paged_pr --self-run-id 999)"
eq "a rollup past the page guard stays UNKNOWN" unknown "$(jq -r '.risk.status' <<<"$out")"

# A PR with NO checks at all has a null statusCheckRollup. Assigning into a null CREATES the
# object in jq, so an unguarded splice would turn "no rollup" into "an empty rollup" and route
# it away from the branch that treats a checkless PR as readable. Pagination must not run here.
jq '.data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup = null' \
  "$SANDBOX/page1.json" > "$SANDBOX/page1norollup.json"
cp "$SANDBOX/page1norollup.json" "$SANDBOX/page1.json"
out="$(paged_pr --self-run-id 999)"
eq "a PR with no checks is untouched by the drain" ok "$(jq -r '.checks_status // "ok"' <<<"$out")"
eq "and still grades" ok "$(jq -r '.risk.status' <<<"$out")"

echo "— phase 21: a repo-owned App is a runbook candidate, not an outsider —"
# A GitHub App is never an org member, so EVERY PR a repo-owned App opens arrives with
# `author_association: NONE`. Testing that string BEFORE the login classification graded every
# such PR `external` => R3 regardless of its diff, and no consumer lever could reach it: the
# `bot_logins` input and .github/risk-runbooks.json are both read further down. The fix narrows
# the association half to non-bots; the FORK half stays unconditional, which is what keeps a bot
# on a fork from presenting a bot login to escape R3.
cat > "$SANDBOX/app-runbooks.json" <<'FIX'
{"registry_version":"v0-test","runbooks":[
  {"id":"data-snapshot-refresh",
   "why":"a repo-owned App's cron that refreshes one committed data snapshot",
   "identity":{"logins":["cloud-code-bot[bot]"]},
   "permitted_paths":["data/*.json"],
   "shape":{"max_changed_files":1,"max_additions":5000,"max_deletions":5000},
   "daily_cap":4,"lane":"data-refresh"}]}
FIX
app_grade() { bash "$GRADER" --stdin --runbooks "$SANDBOX/app-runbooks.json" 2>/dev/null; }
SNAP='[{"path":"data/skills.json","additions":40,"deletions":12,"change_type":"MODIFIED"}]'

# (1) non-fork App PR, author_association NONE, registry entry asserts -> runbook, R0 on the axis.
out="$(rec 22 'cloud-code-bot[bot]' 'chore: refresh skills snapshot' "$SNAP" ok SUCCESS bot/refresh NONE false | app_grade)"
eq "a non-fork App PR is not external" runbook "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "and names the runbook that asserted" data-snapshot-refresh "$(jq -r '.risk.axes.provenance.runbook' <<<"$out")"
eq "provenance proposes R0" R0 "$(jq -r '.risk.axes.provenance.tier' <<<"$out")"
# The other axes still decide: an unmapped data path floors R0 and green-but-no-test is R1, so
# the grade now RESPONDS to the diff instead of being pinned R3 by the author's association.
eq "the diff, not the association, decides the grade" R1 "$(jq -r '.risk.tier' <<<"$out")"

# (2) the SAME App PR from a FORK is still external R3. The fork test runs first and is
# unconditional — reordering the two would open exactly this hole.
out="$(rec 23 'cloud-code-bot[bot]' 'chore: refresh skills snapshot' "$SNAP" ok SUCCESS bot/refresh NONE true | app_grade)"
eq "a bot on a fork is still external" external "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "and still grades R3" R3 "$(jq -r '.risk.tier' <<<"$out")"

# (3) a non-fork HUMAN with author_association NONE is untouched: the first-time-contributor
# guard is narrowed to non-bots, not removed.
out="$(rec 24 'drive-by-human' 'feat: my first patch' "$SNAP" ok SUCCESS feature NONE false | app_grade)"
eq "a first-time human contributor is still external" external "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "and still grades R3" R3 "$(jq -r '.risk.tier' <<<"$out")"

# (4) identity alone buys NO trust: a bot with no asserting registry entry falls back to human.
out="$(rec 25 'unregistered-bot[bot]' 'chore: something' "$SNAP" ok SUCCESS bot/x NONE false | app_grade)"
eq "an unregistered bot grades human, never runbook" human "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "human provenance proposes R1, not R0" R1 "$(jq -r '.risk.axes.provenance.tier' <<<"$out")"
# ...and so does a REGISTERED bot whose diff shape does not assert (paths outside its set).
out="$(rec 26 'cloud-code-bot[bot]' 'chore: refresh' '[{"path":"src/evil.go","additions":9,"deletions":0,"change_type":"MODIFIED"}]' ok SUCCESS bot/refresh NONE false | app_grade)"
eq "a registered App failing its shape is human, not runbook" human "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
sf="$(jq -r '.risk.axes.provenance.shape_failures | length' <<<"$out")"
if [ "$sf" -ge 1 ]; then ok "and the shape failure is recorded"; else bad "and the shape failure is recorded" "$sf"; fi

# (5) --bot-logins reaches the classification too: a bot login WITHOUT the `[bot]` suffix (a
# machine user, or an App whose login the API reports unsuffixed) is only a bot if the caller
# says so, and it must get the same non-external treatment when it is.
out="$(rec 27 'snapshot-machine' 'chore: refresh skills snapshot' "$SNAP" ok SUCCESS bot/refresh NONE false \
       | bash "$GRADER" --stdin --runbooks "$SANDBOX/app-runbooks.json" --bot-logins snapshot-machine 2>/dev/null)"
eq "a --bot-logins machine user is not external" human "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
out="$(rec 28 'snapshot-machine' 'chore: refresh skills snapshot' "$SNAP" ok SUCCESS bot/refresh NONE false | app_grade)"
eq "and without --bot-logins it reads as a first-time human" external "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"

# (6) A LABEL CANNOT STAND IN FOR THE SHAPE ASSERTION. `agent-coded` and `fleet_logins` both
# sat ABOVE the bot test, and both are reachable for a bot only now that the association half
# has stopped pinning every App `external` first — so they are pinned here, alongside that fix.
# A REGISTERED producer was never at risk (the shape assertion resolves to `runbook` ahead of
# the base class either way) — this pins that, then pins the case that WAS wrong: the
# UNREGISTERED bot, which either half classified `agent-supervised`, contradicting the promise
# that a bot with no asserting entry falls back to `human`. The default map tiers both R1, so
# no grade moves here; the classes exist so a CONSUMER map can tier them apart, and a consumer
# that trusts its supervised agents at R0 would otherwise hand R0 to any `agent-coded` bot PR.
out="$(rec 29 'cloud-code-bot[bot]' 'chore: refresh skills snapshot' "$SNAP" ok SUCCESS bot/refresh NONE false \
       | jq -c '.labels = ["agent-coded"]' | app_grade)"
eq "a registered App is runbook with or without the label" runbook "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "and still earns R0 from the shape assertion" R0 "$(jq -r '.risk.axes.provenance.tier' <<<"$out")"
out="$(rec 30 'unregistered-bot[bot]' 'chore: something' "$SNAP" ok SUCCESS bot/x NONE false \
       | jq -c '.labels = ["agent-coded"]' | app_grade)"
eq "an agent-coded unregistered bot is human, not agent-supervised" human "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "and human is R1, exactly what agent-supervised was" R1 "$(jq -r '.risk.axes.provenance.tier' <<<"$out")"
# The same for `fleet_logins`. That collision exists only because `author_is_bot` no longer
# comes from the login string: a Bot actor's GraphQL login arrives UNSUFFIXED, so an operator
# who lists it in --fleet-logins makes `classify_login` say "fleet" while GitHub says Bot. The
# resolver's own rule is bot-beats-fleet; this use site must not contradict it.
out="$(rec 31 'cloud-code-bot' 'chore: refresh skills snapshot' "$SNAP" ok SUCCESS bot/refresh NONE false \
       | jq -c '.author_is_bot = true' \
       | bash "$GRADER" --stdin --fleet-logins cloud-code-bot 2>/dev/null)"
eq "a Bot actor in fleet_logins is still read as a bot" human "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
# ...and with a registry entry that lists the unsuffixed form, the same record reaches `runbook`.
jq '.runbooks[0].identity.logins = ["cloud-code-bot[bot]","cloud-code-bot"]' \
  "$SANDBOX/app-runbooks.json" > "$SANDBOX/app-runbooks-bothforms.json"
out="$(rec 32 'cloud-code-bot' 'chore: refresh skills snapshot' "$SNAP" ok SUCCESS bot/refresh NONE false \
       | jq -c '.author_is_bot = true' \
       | bash "$GRADER" --stdin --runbooks "$SANDBOX/app-runbooks-bothforms.json" --fleet-logins cloud-code-bot 2>/dev/null)"
eq "and the registry, not the fleet list, is what promotes it" runbook "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
# A HUMAN is untouched by the reorder — the label still classifies a human author.
out="$(rec 33 'a-teammate' 'feat: something' "$SNAP" ok SUCCESS feature MEMBER false \
       | jq -c '.labels = ["agent-coded"]' | app_grade)"
eq "an agent-coded human is still agent-supervised" agent-supervised "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"

# (7) `author_is_bot` is tested `== true`, NOT for jq truthiness. It is the one field that can
# switch the `external` guard off, and in jq the STRING "false" — what a foreign collector
# writing JSON by hand emits — is truthy. Read loosely, that grades a first-time outsider
# `human` R1 on a field nobody set to true.
for junk in '"false"' '0' '""' 'null'; do
  out="$(rec 34 'drive-by-human' 'feat: my first patch' "$SNAP" ok SUCCESS feature NONE false \
         | jq -c ".author_is_bot = $junk" | app_grade)"
  eq "author_is_bot=$junk is not a bot — still external" external "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
done
out="$(rec 35 'drive-by-human' 'feat: my first patch' "$SNAP" ok SUCCESS feature NONE false \
       | jq -c '.author_is_bot = true' | app_grade)"
eq "and only the literal boolean flips it" human "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"

echo "— phase 22: on the LIVE path the login alone cannot tell you it is a bot —"
# The case above is the corpus/REST record shape. The live path does not see it: GraphQL reports
# a Bot actor login WITHOUT the `[bot]` suffix, so `cloud-code-bot[bot]` arrives as
# `cloud-code-bot` and the resolver's suffix test never fires. Measured on the real PR in the
# ticket: `author.login` is `cloud-code-bot`, `author.__typename` is `Bot`. That typename is
# GitHub's own actor resolution and is what the record carries, so the fix must survive it.
mkdir -p "$SANDBOX/binbot"
jq '.data.repository.pullRequest.author = {"login":"cloud-code-bot","__typename":"Bot"}
    | .data.repository.pullRequest.authorAssociation = "NONE"
    # phase 12 left changedFiles at 5 on the shared fixture; this stub serves ONE file, and a
    # short read is `unknown` by design — so restate it rather than inherit a truncated record.
    | .data.repository.pullRequest.changedFiles = 1
    | .data.repository.pullRequest.title = "data: refresh bundled skills snapshot (auto)"
    | .data.repository.pullRequest.headRefName = "bot/refresh-skills"
    # ...and the ROLLUP STATE with it. Without --self-run-id the grader reads `.state` directly,
    # and phase 9 left it PENDING — which floored reversibility R2 and meant the SUCCESS CheckRun
    # below was never actually consulted, so the fixture implied coverage it did not have.
    | .data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.state = "SUCCESS"
    | .data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.contexts.nodes =
        [{"__typename":"CheckRun","name":"unit tests","status":"COMPLETED","conclusion":"SUCCESS",
          "checkSuite":{"workflowRun":{"databaseId":1000,"workflow":{"name":"CI"}}}}]' \
  "$SANDBOX/fixture.json" > "$SANDBOX/botfixture.json"
cat > "$SANDBOX/botfiles.json" <<'FIX'
[{"filename":"data/skills.json","additions":40,"deletions":12,"status":"modified"}]
FIX
cat > "$SANDBOX/binbot/gh" <<'STUB'
#!/usr/bin/env bash
fixture=""; filter=""
for ((i=1; i<=$#; i++)); do
  a="${!i}"
  case "$a" in
    graphql)          fixture="$FIXTURE_DIR/botfixture.json" ;;
    *pulls/*/files*)  fixture="$FIXTURE_DIR/botfiles.json" ;;
    --jq)             n=$((i+1)); filter="${!n}" ;;
  esac
done
[ -n "$fixture" ] || { echo "gh stub: unhandled args: $*" >&2; exit 1; }
if [ -n "$filter" ]; then jq -c "$filter" "$fixture"; else cat "$fixture"; fi
STUB
chmod +x "$SANDBOX/binbot/gh"
bot_pr() { PATH="$SANDBOX/binbot:$PATH" bash "$GRADER" --repo test/repo --pr 42 "$@" 2>/dev/null; }

out="$(bot_pr)"
eq "the live record carries GitHub's own actor type" true "$(jq -r '.author_is_bot' <<<"$out")"
eq "and the login really does arrive unsuffixed" cloud-code-bot "$(jq -r '.author' <<<"$out")"
eq "an App PR is no longer external on the live path" human "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
# ...and with the consumer's registry entry it reaches `runbook` — the ticket's end state. The
# entry names the App the way a human can SEE it (`cloud-code-bot[bot]`, what REST and the web
# UI show); the grader restores the suffix GraphQL dropped, so no consumer has to know this.
out="$(bot_pr --runbooks "$SANDBOX/app-runbooks.json" --bot-logins '')"
eq "an entry listing only the SUFFIXED form asserts against an unsuffixed Bot login" \
   runbook "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "and the tier now responds to the diff" R0 "$(jq -r '.risk.axes.provenance.tier' <<<"$out")"
# The rollup really is read on this path: green, but no test file in the diff, so reversibility
# proposes R1 and that — not the author's account type — is what decides the grade.
eq "the green rollup is actually consulted" R1 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
eq "and the overall grade is R1, decided by the diff" R1 "$(jq -r '.risk.tier' <<<"$out")"
# THE SYNTHESIS IS ONE-WAY AND GATED ON GITHUB'S ACTOR TYPE, which is the whole safety argument:
# the suffix is only ever ADDED, and only for an author GitHub types `Bot`. A USER account that
# happens to be named `cloud-code-bot` presents `cloud-code-bot` and nothing turns the entry's
# `cloud-code-bot[bot]` back into it — so it cannot inherit the App's runbook.
jq '.data.repository.pullRequest.author = {"login":"cloud-code-bot","__typename":"User"}
    | .data.repository.pullRequest.authorAssociation = "MEMBER"' \
  "$SANDBOX/botfixture.json" > "$SANDBOX/bfuser.json" && cp "$SANDBOX/bfuser.json" "$SANDBOX/botfixture.json"
out="$(bot_pr --runbooks "$SANDBOX/app-runbooks.json" --bot-logins '')"
eq "a same-named USER cannot inherit the App's runbook" human "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
# A BARE-SLUG entry still matches literally — that is what a machine USER account needs — and
# that is exactly why a bare slug must never be used to name an App.
jq '.runbooks[0].identity.logins = ["cloud-code-bot"]' \
  "$SANDBOX/app-runbooks.json" > "$SANDBOX/app-runbooks-bare.json"
out="$(bot_pr --runbooks "$SANDBOX/app-runbooks-bare.json" --bot-logins '')"
eq "a bare-slug entry matches a same-named USER too" runbook "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
# Restore the Bot author for the rest of the phase.
jq '.data.repository.pullRequest.author = {"login":"cloud-code-bot","__typename":"Bot"}
    | .data.repository.pullRequest.authorAssociation = "NONE"' \
  "$SANDBOX/botfixture.json" > "$SANDBOX/bfbot.json" && cp "$SANDBOX/bfbot.json" "$SANDBOX/botfixture.json"
jq '.runbooks[0].identity.logins = ["cloud-code-bot[bot]","cloud-code-bot"]' \
  "$SANDBOX/app-runbooks.json" > "$SANDBOX/app-runbooks-both.json"
# The fork half is unconditional on the live path too — the API fork flag, not the actor.
jq '.data.repository.pullRequest.isCrossRepository = true' \
  "$SANDBOX/botfixture.json" > "$SANDBOX/bf2.json" && cp "$SANDBOX/bf2.json" "$SANDBOX/botfixture.json"
out="$(bot_pr --runbooks "$SANDBOX/app-runbooks-both.json")"
eq "a Bot author on a fork is still external R3" external "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "and the grade is R3" R3 "$(jq -r '.risk.tier' <<<"$out")"
# A HUMAN author on the same live path keeps the first-time-contributor guard.
jq '.data.repository.pullRequest.isCrossRepository = false
    | .data.repository.pullRequest.author = {"login":"drive-by","__typename":"User"}' \
  "$SANDBOX/botfixture.json" > "$SANDBOX/bf2.json" && cp "$SANDBOX/bf2.json" "$SANDBOX/botfixture.json"
out="$(bot_pr --runbooks "$SANDBOX/app-runbooks-both.json")"
eq "a live first-time human is still external" external "$(jq -r '.risk.axes.provenance.provenance' <<<"$out")"
eq "and still grades R3" R3 "$(jq -r '.risk.tier' <<<"$out")"

echo "— phase 23: reversibility says WHICH files supplied its tier — REPORTING ONLY —"
# `axes.reversibility.files` exists so the publisher (BE-7414) can ask "if the top path-floor
# files were peeled off this PR, would the reversibility reason go with them?". It is derived
# from the tier decision and read nowhere else in the grader, exactly like the per-file path
# floors above — every tier asserted in this phase is the one the grader emitted BEFORE the
# field existed, so a future edit cannot quietly turn the attribution into a second input.
# The three non-attributable rungs stay `null`, which the publisher reads as "not attributable,
# keep suppressing" — an empty array would read as "attributable to nothing", which is false.

# subset_ok — every attributed path must equal some axes.path_floor.files[].path, because the
# publisher's peel test compares the two lists directly. Both record the DESTINATION path.
subset_ok() { jq -e '((.risk.axes.reversibility.files // []) - [.risk.axes.path_floor.files[].path]) | length == 0' >/dev/null; }

# peeled_tier — the PROPERTY `files` claims, not its shape: drop exactly the attributed paths,
# re-grade, and the axis must no longer be R3. Asserting the shape alone passes green on an
# attribution that names a real file but not ALL the files holding the tier up, which is the
# only reading of the field the publisher actually makes. The per-file path-floor phase pins
# its analogue (`worst(files) == floor`); this is the reversibility equivalent.
peeled_tier() { # <pr> <paths-json> <graded-out> -> the reversibility tier after the peel
  local peeled
  peeled="$(jq -c --argjson f "$(jq -c '.risk.axes.reversibility.files // []' <<<"$3")" \
              '[.[] | . as $row | select(($f | index($row.path)) == null)]' <<<"$2")"
  rec "$1" dev 'peel' "$peeled" ok SUCCESS | grade | jq -r '.risk.axes.reversibility.tier'
}

# (a) irreversible class: the migration supplied the tier; the docs file rode along.
paths='[{"path":"db/migrations/0001_x.sql","additions":9,"deletions":0,"change_type":"ADDED"},{"path":"docs/a.md","additions":1,"deletions":0,"change_type":"MODIFIED"}]'
out="$(rec 30 dev 'feat: schema' "$paths" ok SUCCESS | grade)"
eq "irreversible-class reversibility is still R3" R3 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
eq "and the overall tier is still R3" R3 "$(jq -r '.risk.tier' <<<"$out")"
eq "only the migration is attributed" '["db/migrations/0001_x.sql"]' "$(jq -c '.risk.axes.reversibility.files' <<<"$out")"
if subset_ok <<<"$out"; then ok "attributed paths are a subset of the path-floor rows"; else bad "attributed paths are a subset of the path-floor rows" "$(jq -c '.risk.axes.reversibility.files' <<<"$out")"; fi
eq "peeling the attributed paths drops the irreversible-class tier" R1 "$(peeled_tier 130 "$paths" "$out")"

# (b) delete-sensitive: attribution is BY ROW, never by intersecting a row's classes with the
# sensitive list. `src/auth/y.go` is MODIFIED and carries class `auth` too — a class
# intersection would name it and send the publisher peeling a file that removed nothing. The
# DELETED README is a delete, but not of a sensitive class, so it is not what supplied the tier
# either (peeling it alone would leave the reason standing).
paths='[{"path":"src/auth/x.go","additions":0,"deletions":9,"change_type":"DELETED"},{"path":"src/auth/y.go","additions":2,"deletions":1,"change_type":"MODIFIED"},{"path":"README.md","additions":0,"deletions":4,"change_type":"DELETED"}]'
out="$(rec 31 dev 'chore: drop it' "$paths" ok SUCCESS | grade)"
eq "delete-sensitive reversibility is still R3" R3 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
eq "and the overall tier is still R3" R3 "$(jq -r '.risk.tier' <<<"$out")"
eq "only the DELETED sensitive file is attributed" '["src/auth/x.go"]' "$(jq -c '.risk.axes.reversibility.files' <<<"$out")"
eq "the deleted_files count is untouched by the attribution" 2 "$(jq -r '.risk.axes.reversibility.deleted_files' <<<"$out")"
if subset_ok <<<"$out"; then ok "delete attribution is a subset of the path-floor rows"; else bad "delete attribution is a subset of the path-floor rows" "$(jq -c '.risk.axes.reversibility.files' <<<"$out")"; fi
eq "peeling the attributed path drops the delete-sensitive tier" R1 "$(peeled_tier 131 "$paths" "$out")"
# ...and the SENTENCE agrees with the list beside it: one sensitive removal, class `auth` only.
# The count used to be every removal and the classes every class any removal matched, so this
# fixture printed "removes 2 file(s) under a sensitive class (auth, docs)" — with `docs` not
# even in delete_sensitive_classes, next to a `files` naming exactly one auth file.
eq "the reason counts only the SENSITIVE removals" \
   "removes 1 file(s) under a sensitive class (auth) — not a single clean revert" \
   "$(jq -r '.risk.axes.reversibility.reason' <<<"$out")"

# (c) a RENAME out of a sensitive directory removes the ORIGIN path, but is recorded under its
# DESTINATION — which is the only form the path-floor rows carry, so it is the only form the
# publisher can compare against.
paths='[{"path":"misc/x.go","previous_path":"src/auth/x.go","additions":1,"deletions":1,"change_type":"RENAMED"}]'
out="$(rec 32 dev 'refactor: move things' "$paths" ok SUCCESS | grade)"
eq "renaming out of a sensitive class is still R3" R3 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
eq "the renamed row is attributed under its DESTINATION" '["misc/x.go"]' "$(jq -c '.risk.axes.reversibility.files' <<<"$out")"
if subset_ok <<<"$out"; then ok "rename attribution is a subset of the path-floor rows"; else bad "rename attribution is a subset of the path-floor rows" "$(jq -c '.risk.axes.reversibility.files' <<<"$out")"; fi
eq "peeling the renamed row drops the tier" R1 "$(peeled_tier 132 "$paths" "$out")"

# (c2) BOTH R3 RUNGS AT ONCE — the tier ladder is first-match, the attribution is NOT. This PR
# adds a migration (irreversible class) AND deletes an auth file (sensitive removal). Naming
# only the migration would answer the publisher's peel question "yes, the reversibility reason
# goes with these files" while the delete-sensitive rung silently held the axis at R3 — a false
# positive out of the very suppression the `null` rungs exist for. The peel below is the assert
# that matters: strip everything `files` names and the axis must actually leave R3.
paths='[{"path":"db/migrations/0002_y.sql","additions":9,"deletions":0,"change_type":"ADDED"},{"path":"src/auth/x.go","additions":0,"deletions":9,"change_type":"DELETED"},{"path":"docs/a.md","additions":1,"deletions":0,"change_type":"MODIFIED"}]'
out="$(rec 38 dev 'feat: schema + drop auth' "$paths" ok SUCCESS | grade)"
eq "both-rungs reversibility is still R3" R3 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
eq "the reason is still the FIRST rung's" \
   "touches migrations — mutates persistent state or deletes data; reverting the code does not restore it" \
   "$(jq -r '.risk.axes.reversibility.reason' <<<"$out")"
eq "but BOTH rungs' files are attributed" \
   '["db/migrations/0002_y.sql","src/auth/x.go"]' "$(jq -c '.risk.axes.reversibility.files' <<<"$out")"
if subset_ok <<<"$out"; then ok "the union is a subset of the path-floor rows"; else bad "the union is a subset of the path-floor rows" "$(jq -c '.risk.axes.reversibility.files' <<<"$out")"; fi
eq "peeling the union clears EVERY attributable rung" R1 "$(peeled_tier 138 "$paths" "$out")"

# (d)/(e)/(f) the three non-attributable rungs. "No green rollup" is a property of the head
# COMMIT and "no test touched" of the WHOLE change set; neither is removable by dropping files.
out="$(rec 33 dev 'docs: x' '[{"path":"README.md","additions":1,"deletions":0,"change_type":"MODIFIED"}]' ok PENDING | grade)"
eq "no green rollup is still R2" R2 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
eq "and attributes nothing" null "$(jq -r '.risk.axes.reversibility.files' <<<"$out")"
out="$(rec 34 dev 'feat: x' '[{"path":"src/x.go","additions":9,"deletions":0,"change_type":"MODIFIED"}]' ok SUCCESS | grade)"
eq "no test touched is still R1" R1 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
eq "and attributes nothing" null "$(jq -r '.risk.axes.reversibility.files' <<<"$out")"
out="$(rec 35 dev 'test: cover x' '[{"path":"pkg/x_test.go","additions":9,"deletions":0,"change_type":"ADDED"}]' ok SUCCESS | grade)"
eq "a clean revert is still R0" R0 "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
eq "and attributes nothing" null "$(jq -r '.risk.axes.reversibility.files' <<<"$out")"

# An unknown path axis takes reversibility unknown BEFORE the attribution runs, so the field is
# never emitted from a file list nobody read.
out="$(rec 36 dev 'mystery' null unknown SUCCESS | grade)"
eq "an unknown reversibility axis emits no attribution at all" null "$(jq -r '.risk.axes.reversibility.files // "null"' <<<"$out")"

# The record shape the publisher and the labeler read is ADDITIVE — nothing was renamed or
# moved out from under them.
out="$(rec 37 dev 'feat: schema' '[{"path":"db/migrations/0001_x.sql","additions":9,"deletions":0,"change_type":"ADDED"}]' ok SUCCESS | grade)"
eq "the reversibility record keeps every field it had" \
   "deleted_files files flag_gated reason residual_tier status tier" \
   "$(jq -r '.risk.axes.reversibility | keys | join(" ")' <<<"$out")"

echo "— phase 23b: residual_tier bounds where the axis lands AFTER the peel (BE-7419) —"
# `files` alone says the CURRENTLY ATTRIBUTED reason is removable; it does not say the tier drops,
# because the three rungs the remainder falls back to are map-configurable. `residual_tier` is that
# missing half, and the publisher requires it to rank below the headline before it will pitch a
# split. It is REPORTING ONLY like `files` — computed from the decision, read nowhere else here.
paths='[{"path":"db/migrations/0001_x.sql","additions":9,"deletions":0,"change_type":"ADDED"},{"path":"docs/a.md","additions":1,"deletions":0,"change_type":"MODIFIED"}]'
out="$(rec 39 dev 'feat: schema' "$paths" ok SUCCESS | grade)"
# The bound has to be the tier the peel ACTUALLY produces — which phase 23 measures directly, by
# peeling and re-grading. Asserting them equal is what stops the bound drifting into a guess.
eq "residual_tier equals the tier a real peel produces (green rollup)" \
   "$(peeled_tier 139 "$paths" "$out")" "$(jq -r '.risk.axes.reversibility.residual_tier' <<<"$out")"
eq "…which on the repo map's defaults is R1" R1 "$(jq -r '.risk.axes.reversibility.residual_tier' <<<"$out")"
# NOT GREEN: rung 3 tests the HEAD COMMIT's rollup, which no peel can change, so the remainder
# lands exactly on no_green_checks_tier — R2 here, still below the R3 headline.
out="$(rec 40 dev 'feat: schema' "$paths" ok PENDING | grade)"
eq "a non-green rollup pins the residual at no_green_checks_tier" R2 \
   "$(jq -r '.risk.axes.reversibility.residual_tier' <<<"$out")"
# THE CONSUMER OVERRIDE the field exists to catch: raise no_green_checks_tier to R3 and the
# remainder lands back on R3, so peeling the migration buys the PR nothing. The attribution is
# unchanged — only the bound moves, and it is the bound the publisher gates on.
ovmap="$SANDBOX/map-no-green-r3.json"
jq '.reversibility.no_green_checks_tier = "R3"' "$SELF_DIR/../risk-map.v0.json" > "$ovmap"
out="$(rec 41 dev 'feat: schema' "$paths" ok PENDING | bash "$GRADER" --stdin --map "$ovmap" 2>/dev/null)"
eq "…and an override that raises that rung to R3 is reported as R3" R3 \
   "$(jq -r '.risk.axes.reversibility.residual_tier' <<<"$out")"
eq "…while the attribution itself is unchanged" '["db/migrations/0001_x.sql"]' \
   "$(jq -c '.risk.axes.reversibility.files' <<<"$out")"
eq "…and the tier the override cannot touch is still R3 from the irreversible class" R3 \
   "$(jq -r '.risk.axes.reversibility.tier' <<<"$out")"
# The three non-attributable rungs answer no peel question at all, so they carry no bound either.
out="$(rec 42 dev 'feat: x' '[{"path":"src/x.go","additions":9,"deletions":0,"change_type":"MODIFIED"}]' ok SUCCESS | grade)"
eq "a rung with no attribution carries no residual bound either" null \
   "$(jq -r '.risk.axes.reversibility.residual_tier' <<<"$out")"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
