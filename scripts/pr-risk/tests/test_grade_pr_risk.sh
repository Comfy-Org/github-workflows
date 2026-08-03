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
mkdir -p "$SANDBOX/bin"
cat > "$SANDBOX/fixture.json" <<'FIX'
{"data":{"repository":{"pullRequest":{
  "number":42,"title":"docs: tweak readme","state":"OPEN","isDraft":false,
  "createdAt":"2026-08-01T00:00:00Z","updatedAt":"2026-08-01T00:10:00Z","closedAt":null,"mergedAt":null,
  "author":{"login":"dev"},"authorAssociation":"MEMBER","baseRefName":"main","headRefName":"docs-tweak",
  "isCrossRepository":false,"additions":3,"deletions":1,"changedFiles":1,
  "labels":{"nodes":[]},
  "commits":{"nodes":[{"commit":{"statusCheckRollup":{"state":"PENDING","contexts":{
    "pageInfo":{"hasNextPage":false},
    "nodes":[
      {"__typename":"CheckRun","name":"Grade PR risk","status":"IN_PROGRESS","conclusion":null,
       "checkSuite":{"workflowRun":{"workflow":{"name":"CI - PR Risk Grade"}}}},
      {"__typename":"CheckRun","name":"unit tests","status":"COMPLETED","conclusion":"SUCCESS",
       "checkSuite":{"workflowRun":{"workflow":{"name":"CI"}}}}
    ]}}}}]},
  "files":{"pageInfo":{"hasNextPage":false},
           "nodes":[{"path":"README.md","additions":3,"deletions":1,"changeType":"MODIFIED"}]}
}}}}
FIX
cat > "$SANDBOX/bin/gh" <<STUB
#!/usr/bin/env bash
cat "$SANDBOX/fixture.json"
STUB
chmod +x "$SANDBOX/bin/gh"
out="$(PATH="$SANDBOX/bin:$PATH" bash "$GRADER" --repo test/repo --pr 42 --self-context 'CI - PR Risk Grade' 2>/dev/null)"
eq "self-excluded rollup reads SUCCESS" SUCCESS "$(jq -r '.checks_state' <<<"$out")"
eq "nothing else pending" false "$(jq -r '.checks_pending_excl_self' <<<"$out")"
eq "live docs PR grades R1" R1 "$(jq -r '.risk.tier' <<<"$out")"
# Same fixture WITHOUT --self-context: the raw rollup (PENDING) must come through untouched —
# that is the offline/terminal behavior, where no self run is in flight.
out="$(PATH="$SANDBOX/bin:$PATH" bash "$GRADER" --repo test/repo --pr 42 2>/dev/null)"
eq "raw rollup untouched without --self-context" PENDING "$(jq -r '.checks_state' <<<"$out")"
# Flip the other workflow's run to QUEUED: excluding self must now report pending=true.
jq '.data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.contexts.nodes[1].status = "QUEUED"
    | .data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.contexts.nodes[1].conclusion = null' \
  "$SANDBOX/fixture.json" > "$SANDBOX/fixture2.json" && mv "$SANDBOX/fixture2.json" "$SANDBOX/fixture.json"
out="$(PATH="$SANDBOX/bin:$PATH" bash "$GRADER" --repo test/repo --pr 42 --self-context 'CI - PR Risk Grade' 2>/dev/null)"
eq "other pending check reports pending" true "$(jq -r '.checks_pending_excl_self' <<<"$out")"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
