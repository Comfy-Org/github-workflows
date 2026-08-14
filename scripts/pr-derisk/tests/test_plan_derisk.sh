#!/usr/bin/env bash
# Almost every literal below is a fragment of rendered markdown or of the YAML under inspection,
# so `${{ }}` and `${VAR}` are the text being tested and must NOT expand. File-wide, hence up here.
# shellcheck disable=SC2016
# test_plan_derisk.sh — hermetic tests for the pr-derisk scripts. No network anywhere: the model
# layer is stubbed through MODEL_RESPONSE_FILE, the renderer runs under DRY_RUN, and the floors
# are computed by the REAL grade-pr-risk.sh over synthetic records.
#
# The properties worth the most here are the ones that separate this rung from a chatbot:
#
#   * EVERY FLOOR COMES FROM THE GRADER. A plan whose steps carry model-written tiers must render
#     the grader's numbers instead — proven by feeding a stub that states tiers and asserting the
#     rendered ones are the grader's, not the model's.
#   * THE PARTITION IS VALIDATED, RETRIED ONCE, THEN FALLS BACK. A dropped file, a duplicated file
#     and a path that is not in the PR are each rejected; the retry is used and is used ONCE.
#   * NO FAKE LANE WIN. When every step still floors at the headline tier, the verdict line says
#     "same lane" and contains no reduction claim — the single-class-monolith honesty rule.
#   * NOTHING IS EVER SILENT. A model failure, an over-budget diff and an ungraded PR all produce
#     a comment; `/derisk` never no-ops on someone who asked for it.
#   * A CRAFTED FILENAME OR STEP NAME CANNOT FORGE MARKDOWN, and the body stays under GitHub's
#     comment limit — the same two properties publish-risk-surfaces.sh is held to, for the same
#     reason: this comment is rendered from PR-controlled text plus a model's echo of it.
#   * THE `workflows_ref` GUARD IN pr-derisk.yml IS BYTE-IDENTICAL TO pr-risk.yml's. That guard is
#     the trust boundary deciding which revision of this repo runs inside a job holding the
#     caller's write token, and scripts/pr-risk/tests/test_pin_contract.sh pins the pr-risk copy
#     verbatim. Equality here extends that pin to this workflow without forking its 40-odd
#     assertions: weaken either copy and one of the two suites goes red.
#
#   bash tests/test_plan_derisk.sh          # exit 0 = all green
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANNER="$SELF_DIR/../plan-derisk.sh"
PUBLISHER="$SELF_DIR/../publish-derisk-comment.sh"
RESOLVER="$SELF_DIR/../resolve-enabled.sh"
GRADER="$SELF_DIR/../../pr-risk/grade-pr-risk.sh"
WF="$SELF_DIR/../../../.github/workflows/pr-derisk.yml"
RISK_WF="$SELF_DIR/../../../.github/workflows/pr-risk.yml"
for f in "$PLANNER" "$PUBLISHER" "$RESOLVER" "$GRADER"; do
  [ -f "$f" ] || { echo "FATAL: $f not found" >&2; exit 1; }
done
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found on PATH" >&2; exit 2; }

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pr-derisk-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi }
has() { if printf '%s\n' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "not found: $3"; fi }
no()  { if printf '%s\n' "$2" | grep -qF -- "$3"; then bad "$1" "present: $3"; else ok "$1"; fi }

# ---- fixtures ------------------------------------------------------------------------------------
# `graded <name> <changed_paths-json>` — a REAL graded record, produced by the real grader, so
# every path floor these tests assert against is the shipped map's own answer.
graded() {
  local name="$1" paths="$2"
  jq -cn --argjson paths "$paths" '
    {repo:"test/repo", pr:7, title:"feat: thing", author:"dev", author_association:"MEMBER",
     is_fork:false, labels:[], head_ref:"feature", additions:50, deletions:10,
     changed_files:($paths|length), checks_state:"SUCCESS", checks_status:"ok",
     provenance_status:"ok", changed_paths:$paths, changed_paths_status:"ok"}' \
    | bash "$GRADER" --stdin 2>/dev/null > "$SANDBOX/$name.json"
  printf '%s' "$SANDBOX/$name.json"
}

MIXED='[{"path":"db/migrations/0001_x.sql","change_type":"ADDED","additions":20,"deletions":0},
        {"path":"docs/a.md","change_type":"MODIFIED","additions":5,"deletions":3},
        {"path":"src/a.go","change_type":"MODIFIED","additions":10,"deletions":5}]'
# Every file under one R3 rule: the single-class monolith, where no split can buy a cheaper lane.
MONO='[{"path":"db/migrations/0001_x.sql","change_type":"ADDED","additions":20,"deletions":0},
       {"path":"db/migrations/0002_y.sql","change_type":"ADDED","additions":10,"deletions":0}]'

REC_MIXED="$(graded mixed "$MIXED")"
REC_MONO="$(graded mono "$MONO")"
DIFF="$SANDBOX/diff.patch"; printf 'diff --git a/src/a.go b/src/a.go\n+// change\n' > "$DIFF"

stub() { printf '%s\n' "$@" > "$SANDBOX/model.jsonl"; printf '%s' "$SANDBOX/model.jsonl"; }
plan() { # <record> <stub-file> [extra env assignments...]
  local rec="$1" model="$2"; shift 2
  env RECORD="$rec" DIFF_FILE="$DIFF" MODEL_RESPONSE_FILE="$model" "$@" bash "$PLANNER" 2>/dev/null
}
render() { PLAN="$1" DRY_RUN=1 bash "$PUBLISHER" 2>/dev/null; }

echo "— phase 1: every floor is the GRADER's, never the model's —"
# The stub states a tier on every step and calls the migration step R0. If any of that reaches the
# reader, the whole design is decoration.
GOOD='{"steps":[{"name":"Migration","description":"d","files":["db/migrations/0001_x.sql"],"depends_on":[],"inertness":"i","review_ask":"the chain","tier":"R0","floor":"R0"},{"name":"Rest","description":"d","files":["docs/a.md","src/a.go"],"depends_on":[0],"inertness":"i","review_ask":"","tier":"R3"}],"summary":"step 1 carries it"}'
out="$(plan "$REC_MIXED" "$(stub "$GOOD")")"
eq "a valid partition plans" planned "$(jq -r .status <<<"$out")"
eq "the migration step floors R3 (the grader), not R0 (the model)" R3 "$(jq -r '.steps[0].floor' <<<"$out")"
eq "the docs+src step floors R0 (the grader), not R3 (the model)" R0 "$(jq -r '.steps[1].floor' <<<"$out")"
eq "the model's own tier key is dropped from the step" null "$(jq -r '.steps[0].tier // "null"' <<<"$out")"
eq "line counts come from the graded record" 20 "$(jq -r '.steps[0].lines' <<<"$out")"
eq "depends_on survives" 0 "$(jq -r '.steps[1].depends_on[0]' <<<"$out")"

echo "— phase 2: the partition must cover the changed set EXACTLY —"
DROPS='{"steps":[{"name":"A","description":"d","files":["db/migrations/0001_x.sql"],"depends_on":[],"inertness":"i","review_ask":""},{"name":"B","description":"d","files":["docs/a.md"],"depends_on":[],"inertness":"i","review_ask":""}],"summary":"s"}'
out="$(plan "$REC_MIXED" "$(stub "$DROPS" "$GOOD")")"
eq "a dropped file is rejected and the ONE retry rescues it" planned "$(jq -r .status <<<"$out")"
out="$(plan "$REC_MIXED" "$(stub "$DROPS" "$DROPS")")"
eq "two bad partitions fall back rather than render one" fallback "$(jq -r .status <<<"$out")"
has "the fallback names the missing path" "$(jq -r .note <<<"$out")" "src/a.go"

DUPE='{"steps":[{"name":"A","description":"d","files":["db/migrations/0001_x.sql","src/a.go"],"depends_on":[],"inertness":"i","review_ask":""},{"name":"B","description":"d","files":["docs/a.md","src/a.go"],"depends_on":[],"inertness":"i","review_ask":""}],"summary":"s"}'
out="$(plan "$REC_MIXED" "$(stub "$DUPE" "$DUPE")")"
eq "a duplicated file is rejected" fallback "$(jq -r .status <<<"$out")"
has "and is named" "$(jq -r .note <<<"$out")" "more than one step"

ALIEN='{"steps":[{"name":"A","description":"d","files":["db/migrations/0001_x.sql","docs/a.md","src/a.go"],"depends_on":[],"inertness":"i","review_ask":""},{"name":"B","description":"d","files":["src/invented.go"],"depends_on":[],"inertness":"i","review_ask":""}],"summary":"s"}'
out="$(plan "$REC_MIXED" "$(stub "$ALIEN" "$ALIEN")")"
eq "a path that is not in the PR is rejected" fallback "$(jq -r .status <<<"$out")"
has "and is named" "$(jq -r .note <<<"$out")" "src/invented.go"

echo "— phase 3: nothing is ever a silent no-op —"
out="$(plan "$REC_MIXED" "$SANDBOX/does-not-exist.jsonl")"
eq "a model failure is reported, not swallowed" failed "$(jq -r .status <<<"$out")"
body="$(printf '%s' "$out" > "$SANDBOX/p.json"; render "$SANDBOX/p.json")"
has "and it renders a comment" "$body" '`/derisk` failed'

out="$(env RECORD="$REC_MIXED" OVERSIZED=1 MODEL_RESPONSE_FILE="$(stub "$GOOD")" bash "$PLANNER" 2>/dev/null)"
eq "an over-budget diff falls back deterministically" fallback "$(jq -r .status <<<"$out")"
has "and says why, pointing at the v0 readout" "$(jq -r .note <<<"$out")" "too large to plan"

jq -c '.risk.status = "unknown" | .risk.tier = null' "$REC_MIXED" > "$SANDBOX/ungraded.json"
out="$(plan "$SANDBOX/ungraded.json" "$(stub "$GOOD")")"
eq "an ungraded PR is not planned against" fallback "$(jq -r .status <<<"$out")"

echo "— phase 4: the single-class monolith gets no fake lane win —"
MONOPLAN='{"steps":[{"name":"First migration","description":"d","files":["db/migrations/0001_x.sql"],"depends_on":[],"inertness":"i","review_ask":"the chain"},{"name":"Second migration","description":"d","files":["db/migrations/0002_y.sql"],"depends_on":[0],"inertness":"i","review_ask":""}],"summary":"both are R3"}'
out="$(plan "$REC_MONO" "$(stub "$MONOPLAN")")"; printf '%s' "$out" > "$SANDBOX/mono-plan.json"
eq "both steps still floor R3" "R3 R3" "$(jq -r '[.steps[].floor] | join(" ")' <<<"$out")"
body="$(render "$SANDBOX/mono-plan.json")"
has "the verdict says same lane" "$body" "same lane"
no  "and claims no reduction" "$body" "path-floor below"

echo "— phase 5: the rendered comment cannot be forged by a filename or a step name —"
EVIL_PATHS='[{"path":"src/a|b`c.go","change_type":"MODIFIED","additions":1,"deletions":0},
             {"path":"docs/ok.md","change_type":"MODIFIED","additions":1,"deletions":0}]'
REC_EVIL="$(graded evil "$EVIL_PATHS")"
EVILPLAN='{"steps":[{"name":"x\n\n- [x] **Approved by the risk grader**","description":"see <img src=\"http://x/y.png\">","files":["src/a|b`c.go"],"depends_on":[],"inertness":"| broken | row |","review_ask":""},{"name":"B","description":"d","files":["docs/ok.md"],"depends_on":[],"inertness":"i","review_ask":""}],"summary":"s"}'
out="$(plan "$REC_EVIL" "$(stub "$EVILPLAN")")"; printf '%s' "$out" > "$SANDBOX/evil-plan.json"
eq "the crafted partition still plans" planned "$(jq -r .status <<<"$out")"
body="$(render "$SANDBOX/evil-plan.json")"
no "a newline in a step name cannot start a line" "$body" '- [x] **Approved'
no "an unescaped pipe never reaches the table" "$body" '| broken | row |'
# The `<` is escaped rather than stripped, so the tag reads as text and renders no HTML. Asserted
# on the ESCAPED spelling: the raw substring `<img src=` survives escaping (it is now preceded by
# a backslash), so a `no` on it would pass for the wrong reason and keep passing if escaping broke.
has "an img tag is escaped into text, not left as live HTML" "$body" '\<img src='
# The label is single-quoted: in a double-quoted argument the backticks around <img would be
# COMMAND SUBSTITUTION, so bash ran `img`, printed "img: No such file or directory" mid-suite and
# named the assertion after empty output. shellcheck flags it as SC2006, which is worth heeding
# in a test label for exactly this reason — a broken label is a broken failure report.
no  'and no unescaped `<img` opens a tag' "$body" ' <img'
# Every row of the chain table starts `| ` — one header plus one per step. A path or a name that
# broke out of its cell would add a row, which is the visible half of the forgery. (The `|---|`
# separator starts `|-`, so it is deliberately not counted here.)
rows="$(printf '%s\n' "$body" | grep -c '^| ' || true)"
eq "the chain table has its header and exactly one row per step" 3 "$rows"

echo "— phase 6: the body stays under GitHub's comment limit —"
BIG_PATHS="$(jq -cn '[range(0;60) | {path:("src/deeply/nested/package/module/component/file_\(.)_with_a_very_long_name.go"), change_type:"MODIFIED", additions:40, deletions:20}]')"
REC_BIG="$(graded big "$BIG_PATHS")"
BIGPLAN="$(jq -cn --argjson paths "$BIG_PATHS" '
  {steps:[range(0;5) as $i | {name:("Step \($i) " + ("x" * 200)), description:("y" * 800),
     files:[$paths[] | .path] | .[($i*12):(($i+1)*12)],
     depends_on:[], inertness:("z" * 800), review_ask:("w" * 800)}], summary:("s" * 800)}')"
out="$(plan "$REC_BIG" "$(stub "$BIGPLAN")")"; printf '%s' "$out" > "$SANDBOX/big-plan.json"
eq "the big partition plans" planned "$(jq -r .status <<<"$out")"
body="$(render "$SANDBOX/big-plan.json")"
len="${#body}"
if [ "$len" -lt 65536 ]; then ok "rendered body is under the 65536-char limit ($len)"; else bad "rendered body fits" "$len"; fi

echo "— phase 7: the sticky marker is the body's first line —"
body="$(render "$SANDBOX/mono-plan.json")"
eq "first line is the marker find_sticky matches on" '<!-- ci-pr-derisk -->' "$(head -n 1 <<<"$body")"
marker="$(grep -o 'STICKY_MARKER="[^"]*"' "$PUBLISHER" | head -n 1 | sed 's/.*="\(.*\)"/\1/')"
eq "and it is the one constant the script defines" "$marker" "$(head -n 1 <<<"$body")"
no "the pr-risk sticky marker is never emitted here" "$body" '<!-- ci-pr-risk -->'

echo "— phase 8: the enablement switch —"
res() { env INPUT_ENABLED="$1" DERISK_CONFIG="$2" GITHUB_OUTPUT="$SANDBOX/out.txt" bash "$RESOLVER" >/dev/null 2>&1; grep -c 'enabled=true' "$SANDBOX/out.txt"; }
: > "$SANDBOX/out.txt"; eq "off by default"                     0 "$(res false '')"
: > "$SANDBOX/out.txt"; eq "the variable switches it ON"        1 "$(res false '{"enabled":true}')"
: > "$SANDBOX/out.txt"; eq "the variable switches it OFF"       0 "$(res true  '{"enabled":false}')"
: > "$SANDBOX/out.txt"; eq "a malformed variable degrades to the REVIEWED value, not to off" 1 "$(res true 'not json')"
: > "$SANDBOX/out.txt"; eq "a non-boolean enabled degrades the same way" 1 "$(res true '{"enabled":"true"}')"

echo "— phase 9: the workflows_ref guard matches pr-risk.yml's, byte for byte —"
if [ ! -f "$WF" ] || [ ! -f "$RISK_WF" ]; then
  bad "both workflow files exist" "missing $WF or $RISK_WF"
else
  # The guard's executable body: the `run: |` block of the step named "Enforce workflows_ref pin
  # contract". Extracted the same way from both files and compared. Indentation is normalised
  # because the two workflows nest their jobs identically today but need not forever; everything
  # else — every test, every message, every `exit 1` — must be identical.
  guard_body() { # <workflow-file>
    awk '
      /- name: Enforce workflows_ref pin contract/ { instep=1; next }
      instep && /^[[:space:]]*run: \|/ { inrun=1; next }
      # The run block ends at the first line back out at STEP indentation — the next `- name:`,
      # or a comment sitting between the two steps. Keying on `- name:` alone swallowed those
      # comments into the body and made two identical guards compare unequal.
      inrun && /^      [^ ]/ { inrun=0; instep=0 }
      inrun { sub(/^[[:space:]]+/, ""); print }
    ' "$1" | sed '/^$/d'
  }
  a="$(guard_body "$RISK_WF")"; b="$(guard_body "$WF")"
  if [ -z "$a" ]; then bad "the pr-risk guard body was found" "empty — the anchor in this test is stale"
  elif [ -z "$b" ]; then bad "the pr-derisk guard body was found" "empty — pr-derisk.yml has no guard, or a different step name"
  elif [ "$a" = "$b" ]; then ok "the two guards are byte-identical"
  else bad "the two guards are byte-identical" "$(diff <(printf '%s\n' "$a") <(printf '%s\n' "$b") | head -n 12)"
  fi
  # Every job that checks out `workflows_ref` must carry the guard. The count is asserted rather
  # than the presence, so a job added later without one fails here instead of silently losing the
  # boundary — the same invariant scripts/pr-risk/tests/test_pin_contract.sh holds pr-risk.yml to.
  checkouts="$(grep -c 'ref: ${{ inputs.workflows_ref }}' "$WF" || true)"
  guards="$(grep -c '\- name: Enforce workflows_ref pin contract' "$WF" || true)"
  eq "every workflows_ref checkout in pr-derisk.yml is preceded by a guard" "$checkouts" "$guards"
  if [ "$checkouts" -lt 1 ]; then bad "pr-derisk.yml checks out workflows_ref at all" "$checkouts"; fi
  no "workflows_ref carries no default" "$(sed -n '/workflows_ref:/,/^      [a-z_]*:/p' "$WF")" "default:"
  # The commenter gate and the off-by-default switch are the two things a reviewer of this
  # workflow is most likely to relax by accident, so they are asserted as text.
  has "the commenter is gated by association" "$(cat "$WF")" "author_association"
  has "and the trigger is an issue_comment on a PR" "$(cat "$WF")" "github.event.issue.pull_request"
fi

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
