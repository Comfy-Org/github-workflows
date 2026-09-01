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
# NO `grep -q` HERE, and the reason is not style. `-q` makes grep exit the instant it matches,
# which closes the pipe under a `printf` that is still writing — and with `set -o pipefail` above,
# that EPIPE becomes the PIPELINE's status, so a SUCCESSFUL match is reported as a failure. It is a
# race decided by where the needle sits: a pattern near the top of a 43 KB workflow file loses,
# one near the bottom wins, and a small haystack that fits the pipe buffer always wins — which is
# why this passed on a developer's laptop and on every earlier CI run, and only broke once the
# suite grew assertions against text at the START of pr-derisk.yml. It also silently inverted `no`
# into a FALSE GREEN. Without `-q`, grep drains its input before exiting and there is no race.
has() { if printf '%s\n' "$2" | grep -F -- "$3" >/dev/null; then ok "$1"; else bad "$1" "not found: $3"; fi }
no()  { if printf '%s\n' "$2" | grep -F -- "$3" >/dev/null; then bad "$1" "present: $3"; else ok "$1"; fi }

# ---- fixtures ------------------------------------------------------------------------------------
# `graded <name> <changed_paths-json>` — a REAL graded record, produced by the real grader, so
# every path floor these tests assert against is the shipped map's own answer.
# `<extra>` is merged over the scorecard, which is how the fork case below moves the PROVENANCE
# axis without touching the paths — the only way to produce a record whose headline is set by
# something a split cannot move.
graded() {
  local name="$1" paths="$2" extra="${3:-}"
  [ -n "$extra" ] || extra='{}'
  jq -cn --argjson paths "$paths" --argjson extra "$extra" '
    {repo:"test/repo", pr:7, title:"feat: thing", author:"dev", author_association:"MEMBER",
     is_fork:false, labels:[], head_ref:"feature", additions:50, deletions:10,
     changed_files:($paths|length), checks_state:"SUCCESS", checks_status:"ok",
     provenance_status:"ok", changed_paths:$paths, changed_paths_status:"ok"} + $extra' \
    | bash "$GRADER" --stdin 2>/dev/null > "$SANDBOX/$name.json"
  printf '%s' "$SANDBOX/$name.json"
}

MIXED='[{"path":"db/migrations/0001_x.sql","change_type":"ADDED","additions":20,"deletions":0},
        {"path":"docs/a.md","change_type":"MODIFIED","additions":5,"deletions":3},
        {"path":"src/a.go","change_type":"MODIFIED","additions":10,"deletions":5}]'
# Every file under one xhigh rule: the single-class monolith, where no split can buy a cheaper lane.
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
# The stub states a tier on every step and calls the migration step low. If any of that reaches the
# reader, the whole design is decoration.
GOOD='{"steps":[{"name":"Migration","description":"d","files":["db/migrations/0001_x.sql"],"depends_on":[],"inertness":"i","review_ask":"the chain","tier":"low","floor":"low"},{"name":"Rest","description":"d","files":["docs/a.md","src/a.go"],"depends_on":[0],"inertness":"i","review_ask":"","tier":"xhigh"}],"summary":"step 1 carries it"}'
out="$(plan "$REC_MIXED" "$(stub "$GOOD")")"; printf '%s' "$out" > "$SANDBOX/p1-plan.json"
eq "a valid partition plans" planned "$(jq -r .status <<<"$out")"
eq "the migration step floors xhigh (the grader), not low (the model)" xhigh "$(jq -r '.steps[0].floor' <<<"$out")"
eq "the docs+src step floors low (the grader), not xhigh (the model)" low "$(jq -r '.steps[1].floor' <<<"$out")"
eq "the model's own tier key is dropped from the step" null "$(jq -r '.steps[0].tier // "null"' <<<"$out")"
eq "line counts come from the graded record" 20 "$(jq -r '.steps[0].lines' <<<"$out")"
eq "depends_on survives" 0 "$(jq -r '.steps[1].depends_on[0]' <<<"$out")"

echo "— phase 1b: historical R0..R3 plans render with canonical names —"
jq '
  def legacy:
    if . == "low" then "R0" elif . == "medium" then "R1"
    elif . == "high" then "R2" elif . == "xhigh" then "R3" else . end;
  walk(if type == "string" then legacy else . end)' "$SANDBOX/p1-plan.json" > "$SANDBOX/legacy-plan.json"
legacy_body="$(render "$SANDBOX/legacy-plan.json")"
has "legacy plan tiers render canonically" "$legacy_body" "path-floor below xhigh"
no "legacy tier names are not re-published" "$legacy_body" "**R3**"

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
MONOPLAN='{"steps":[{"name":"First migration","description":"d","files":["db/migrations/0001_x.sql"],"depends_on":[],"inertness":"i","review_ask":"the chain"},{"name":"Second migration","description":"d","files":["db/migrations/0002_y.sql"],"depends_on":[0],"inertness":"i","review_ask":""}],"summary":"both are xhigh"}'
out="$(plan "$REC_MONO" "$(stub "$MONOPLAN")")"; printf '%s' "$out" > "$SANDBOX/mono-plan.json"
eq "both steps still floor xhigh" "xhigh xhigh" "$(jq -r '[.steps[].floor] | join(" ")' <<<"$out")"
body="$(render "$SANDBOX/mono-plan.json")"
has "the verdict says same lane" "$body" "same lane"
no  "and claims no reduction" "$body" "path-floor below"

echo "— phase 4b: a headline set by a NON-PATH axis is never claimed as a lane win —"
# `grade = worst(path_floor, provenance, reversibility)` and a split only moves the PATH axis. This
# fork PR grades xhigh on PROVENANCE with a path floor of low, so every step trivially sits below the
# HEADLINE while the axis that actually set the grade is untouched. Comparing against the headline
# printed "2 step(s) path-floor below xhigh (100% of the changed lines)" — a reduction no partition
# here can deliver, on exactly the pull requests the no-fake-lane-win rule exists for.
SOFT='[{"path":"docs/a.md","change_type":"MODIFIED","additions":5,"deletions":3},
       {"path":"src/a.go","change_type":"MODIFIED","additions":10,"deletions":5}]'
REC_FORK="$(graded fork "$SOFT" '{"is_fork":true,"author_association":"NONE"}')"
eq "the fixture grades xhigh overall" xhigh "$(jq -r '.risk.tier' "$REC_FORK")"
eq "but its PATH floor is low" low "$(jq -r '.risk.axes.path_floor.tier' "$REC_FORK")"
FORKPLAN='{"steps":[{"name":"Docs","description":"d","files":["docs/a.md"],"depends_on":[],"inertness":"i","review_ask":""},{"name":"Code","description":"d","files":["src/a.go"],"depends_on":[0],"inertness":"i","review_ask":"the chain"}],"summary":"s"}'
out="$(plan "$REC_FORK" "$(stub "$FORKPLAN")")"; printf '%s' "$out" > "$SANDBOX/fork-plan.json"
eq "the plan carries the path floor separately from the headline" "xhigh low" \
   "$(jq -r '[.headline_tier, .path_floor_tier] | join(" ")' <<<"$out")"
body="$(render "$SANDBOX/fork-plan.json")"
no  "no step is claimed to land below the non-path headline" "$body" "path-floor below xhigh"
has "the verdict speaks in the PATH floor instead"            "$body" 'path-floors at **low**'
has "and names the axis a split cannot move"                  "$body" "non-path axis"
# The same comparison must still fire normally when the PATH axis IS the one holding the grade up.
body="$(render "$SANDBOX/p1-plan.json")"
has "a genuine path-axis reduction is still reported" "$body" "path-floor below xhigh"
no  "and carries no non-path caveat"                  "$body" "non-path axis"

echo "— phase 4c: the ordering a plan exists to state cannot be impossible —"
# A self-reference, a forward reference or a two-step cycle all render a "Lands after" nobody can
# execute. Bounding each entry by the index of the step it sits on makes a cycle unrepresentable.
CYCLE='{"steps":[{"name":"A","description":"d","files":["db/migrations/0001_x.sql"],"depends_on":[1],"inertness":"i","review_ask":""},{"name":"B","description":"d","files":["docs/a.md","src/a.go"],"depends_on":[1,0],"inertness":"i","review_ask":""}],"summary":"s"}'
out="$(plan "$REC_MIXED" "$(stub "$CYCLE")")"
eq "the plan still renders"                    planned "$(jq -r .status <<<"$out")"
eq "a forward reference is dropped"            ""      "$(jq -r '.steps[0].depends_on | join(",")' <<<"$out")"
eq "a self-reference is dropped, the real one kept" "0" "$(jq -r '.steps[1].depends_on | join(",")' <<<"$out")"
# A SCALAR depends_on type-errored the merge jq, which emitted `planned` with an empty chain — the
# steps the reader came for, silently gone, under a headline that counted them.
SCALARDEP='{"steps":[{"name":"A","description":"d","files":["db/migrations/0001_x.sql"],"depends_on":[],"inertness":"i","review_ask":""},{"name":"B","description":"d","files":["docs/a.md","src/a.go"],"depends_on":0,"inertness":"i","review_ask":""}],"summary":"s"}'
out="$(plan "$REC_MIXED" "$(stub "$SCALARDEP" "$SCALARDEP")")"
eq "a scalar depends_on is rejected, not silently emptied" fallback "$(jq -r .status <<<"$out")"
has "and the rejection says what shape was wanted" "$(jq -r .note <<<"$out")" "depends_on must be an ARRAY"
out="$(plan "$REC_MIXED" "$(stub "$SCALARDEP" "$GOOD")")"
eq "the retry rescues it" planned "$(jq -r .status <<<"$out")"
eq "and every step is still present" 2 "$(jq -r '.steps | length' <<<"$out")"

echo "— phase 4d: a diff that came back EMPTY is not a diff —"
# `gh` exits 0 on a followed redirect that returned no body, so an empty file reaches the planner
# looking like a legitimate read. Planning off it produces a confident, evidence-free partition.
: > "$SANDBOX/empty.patch"
out="$(env RECORD="$REC_MIXED" DIFF_FILE="$SANDBOX/empty.patch" MODEL_RESPONSE_FILE="$(stub "$GOOD")" bash "$PLANNER" 2>/dev/null)"
eq "an empty diff file takes the deterministic fallback" fallback "$(jq -r .status <<<"$out")"
has "and says the diff could not be read" "$(jq -r .note <<<"$out")" "could not be read"

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
  # ANCHORED AT BOTH ENDS. `format('{0},', …)` anchors the trailing delimiter only, which makes the
  # test an unanchored substring match: an allowlist naming FIRST_TIME_CONTRIBUTOR then also admits
  # plain CONTRIBUTOR — anyone with one merged PR — to a command that spends money.
  has "the association allowlist is anchored at BOTH ends" "$(cat "$WF")" \
      "contains(format(',{0},', inputs.allowed_associations), format(',{0},', github.event.comment.author_association))"
  no  "and never with a trailing-only anchor"              "$(cat "$WF")" \
      "contains(format('{0},', inputs.allowed_associations)"
  # `startsWith(body, '')` is always true, so an empty command is every comment on every PR.
  has "an empty command prefix cannot arm the trigger" "$(cat "$WF")" "inputs.command != ''"
  # The `always()` publisher writes into a directory only collect-pr-inputs.sh creates, and the
  # failures it exists to cover are exactly the ones where collect never ran.
  has "the always() publisher creates the plan directory first" "$(cat "$WF")" 'mkdir -p "$(dirname "$PLAN")"'
fi

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
