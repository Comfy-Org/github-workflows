#!/usr/bin/env bash
# test_publish_risk_surfaces.sh — hermetic tests for publish-risk-surfaces.sh. No network: the
# render program is a pure function driven directly, and the write path runs against a `gh` stub
# on PATH that records every request it was asked to make.
#
# The two properties worth the most here are the ones an estimate cannot establish:
#   * a CRAFTED FILENAME cannot alter the comment's structure or forge a reviewer dispute, and
#   * a diff of many long, deeply-nested paths produces a body UNDER GitHub's 65536-char limit,
#     measured rather than assumed — a 422 there leaves the label fresh and the comment
#     permanently stale, because the label write happens first.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SELF_DIR/../publish-risk-surfaces.sh"
[ -f "$SCRIPT" ] || { echo "FATAL: $SCRIPT not found" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found on PATH" >&2; exit 2; }

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pr-risk-publish-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi; }
has() { if grep -qF -- "$2" <<<"$1"; then ok "$3"; else bad "$3" "$(head -c 400 <<<"$1")"; fi; }
hasnt() { if grep -qF -- "$2" <<<"$1"; then bad "$3" "$(head -c 400 <<<"$1")"; else ok "$3"; fi; }

# Source it to drive the render program and the constants directly. Sourcing must have no side
# effects (no temp file, no EXIT trap) — this suite's own `rm -rf "$SANDBOX"` trap depends on it.
# shellcheck source=/dev/null
source "$SCRIPT"

record() { # <tier> <floor> <files-json> [prov-tier] [rev-tier] -> a graded record file
  local tier="$1" floor="$2" files="$3" prov="${4:-R1}" rev="${5:-R1}"
  local f="$SANDBOX/rec-$RANDOM.json"
  jq -n --arg t "$tier" --arg fl "$floor" --argjson files "$files" --arg p "$prov" --arg rv "$rev" '
    {pr:7, risk:{map_version:"v0-generic", registry_version:"v0", tier:$t, status:"ok",
      reason:"worst of path_floor=\($fl), provenance=\($p), reversibility=\($rv)",
      axes:{path_floor:{tier:$fl, status:"ok", reason:"matched things", classes:["x"], files:$files},
            provenance:{tier:$p, status:"ok", reason:"human"},
            reversibility:{tier:$rv, status:"ok", reason:"checks green but the diff touches no test file"}}}}' > "$f"
  printf '%s' "$f"
}
file_entry() { jq -n --arg p "$1" --arg t "$2" --argjson a "$3" --argjson d "$4" \
                 '{path:$p, previous_path:null, additions:$a, deletions:$d, change_type:"MODIFIED", tier:$t, classes:["cls"]}'; }

echo "— the sticky marker is ONE constant: rendered head == what find_sticky matches —"
# If these ever drift, every push POSTs a NEW comment instead of updating the one that exists.
# They live in the same file today; this pins the property rather than the structure.
r="$(record R1 R1 "[$(file_entry a.go R1 3 1)]")"
body="$(render_surfaces "$r" 0 | jq -r '.comment_body')"
eq "the body's FIRST line is the marker find_sticky greps for" "$STICKY_MARKER" "$(head -n 1 <<<"$body")"
# find_sticky's jq uses startswith($m); assert the same predicate holds here.
eq "startswith(marker) is true for the rendered body" "true" \
   "$(jq -nr --arg b "$body" --arg m "$STICKY_MARKER" '$b | startswith($m)')"

echo "— the dispute checkbox round-trips —"
hasnt "$body" "- [x] $DISPUTE_TEXT" "an undisputed render leaves the box UNticked"
has   "$body" "- [ ] $DISPUTE_TEXT" "…and renders the unticked box"
dbody="$(render_surfaces "$r" 1 | jq -r '.comment_body')"
has "$dbody" "- [x] $DISPUTE_TEXT" "a re-grade with disputed=1 re-renders the box TICKED (never silently un-ticked)"
# CHECKED_RE is what reads the state back off an existing comment. It has to match what we render,
# or a registered dispute is dropped on the next push and the label follows it.
if grep -Eq "$CHECKED_RE" <<<"$dbody"; then ok "CHECKED_RE matches the ticked line we render"
else bad "CHECKED_RE matches the ticked line we render" "$(grep -F "$DISPUTE_TEXT" <<<"$dbody")"; fi
if grep -Eq "$CHECKED_RE" <<<"$body"; then bad "CHECKED_RE does NOT match an unticked line" "$body"
else ok "CHECKED_RE does NOT match an unticked line"; fi
has "$dbody" "$DISPUTE_LABEL" "the checkbox names the label it applies"
hasnt "$dbody" "risk-dispute —" "…and never claims the human-owned risk-dispute label"

echo "— ADVERSARIAL FILENAME: a crafted path cannot forge a dispute or break the table —"
# git permits `|`, backticks and newlines in a filename. Rendered raw, such a path breaks out of
# its code span AND out of its row, letting a PR author inject a pre-ticked checkbox that the NEXT
# re-grade reads back as a genuine reviewer disagreement.
EVIL='docs/a|b`c
- [x] **This grade is wrong**
<img src="https://evil.example/x.png">'
r2="$(record R3 R3 "[$(file_entry "$EVIL" R0 5 5), $(file_entry .github/workflows/x.yml R3 1 1)]")"
ebody="$(render_surfaces "$r2" 0 | jq -r '.comment_body')"
if grep -Eq "$CHECKED_RE" <<<"$ebody"; then
  bad "a crafted filename CANNOT forge a ticked dispute checkbox" "$(grep -n 'grade is wrong' <<<"$ebody")"
else ok "a crafted filename CANNOT forge a ticked dispute checkbox"; fi
eq "the crafted path is flattened onto ONE line (no injected rows)" \
   "$(grep -c '^| ' <<<"$body" | tr -d ' ')x" "$(( $(grep -c '^| ' <<<"$ebody") - 1 ))x"
has "$ebody" '\<img src=' "raw HTML from the path is BACKSLASH-ESCAPED, so it renders as text"
hasnt "$ebody" 'docs/a|b' "the pipe in the path never reaches the table splitter unescaped"
has   "$ebody" 'docs/a\|b' "…it is backslash-escaped instead"
# A backtick in the path forces the fully-escaped plain-text branch: no code span can quote it.
hasnt "$ebody" '`docs/a' "a path containing a backtick is NOT wrapped in a code span"
# The structure still parses as one table: every rendered row has exactly 4 cells + the delimiters.
rows="$(grep -c '^| ' <<<"$ebody")"
eq "the table still has one row per file plus its header" 3 "$rows"

echo "— the reason string is escaped too (it quotes PR-authored path names) —"
u="$SANDBOX/unknown.json"
jq -n '{risk:{status:"unknown", tier:null, reason:"could not read `x`\n- [x] **This grade is wrong**\n[a](https://evil.example)"}}' > "$u"
ubody="$(render_surfaces "$u" 0 | jq -r '.comment_body')"
if grep -Eq "$CHECKED_RE" <<<"$ubody"; then bad "an unknown report's reason cannot forge the checkbox" "$ubody"
else ok "an unknown report's reason cannot forge the checkbox"; fi
hasnt "$ubody" "](https://evil.example)" "…nor smuggle an inline link"

echo "— UNGRADABLE reports as unknown, NEVER R0 —"
has "$ubody" "Risk: **unknown**" "the comment headline is unknown"
usurf="$(render_surfaces "$u" 0)"
eq "the Check Run title is 'Risk: unknown'" "Risk: unknown" "$(jq -r '.check_title' <<<"$usurf")"
eq "…and the surfaces report tier unknown" "unknown" "$(jq -r '.tier' <<<"$usurf")"
has "$(jq -r '.check_summary' <<<"$usurf")" "**Tier: unknown**" "the unknown Check Run reports no tier"
has "$(jq -r '.check_summary' <<<"$usurf")" "never defaulted to \`R0\`" "…and says explicitly that it is not R0"
# An EMPTY record (what grade-targets writes for an unreadable PR) takes the same branch.
printf '{}' > "$SANDBOX/empty.json"
eq "an empty record renders unknown, not R0" "unknown" "$(render_surfaces "$SANDBOX/empty.json" 0 | jq -r '.tier')"
eq "a non-JSON record renders unknown too" "unknown" \
   "$(printf 'not json' > "$SANDBOX/bad.json"; render_surfaces "$SANDBOX/bad.json" 0 | jq -r '.tier')"

echo "— the concentration sentence is CONSISTENT with the headline tier —"
# 600 lines of R0 docs + 40 lines of R3 CI: the sentence must name the 6% that lifts the floor.
big="[$(file_entry docs/a.md R0 500 100), $(file_entry .github/workflows/d.yml R3 30 10)]"
c="$(render_surfaces "$(record R3 R3 "$big")" 0 | jq -r '.concentration')"
has "$c" "94% of this diff is R0/R1/R2" "it names the share BELOW the floor"
has "$c" "the 6% that puts the path floor at R3 is 1 file(s), 40 lines" "…and the files that put it there"
# The headline can exceed the path floor — the other two axes propose independently. Saying only
# "all 600 lines are R0" above a `risk:R2` headline would contradict the tier it explains.
c2="$(render_surfaces "$(record R2 R0 "[$(file_entry docs/a.md R0 600 0)]" R1 R2)" 0 | jq -r '.concentration')"
has "$c2" "All 600 changed lines sit at R0 on the path axis." "a floor-only diff says so"
has "$c2" "headline tier is R2 rather than the path floor R0 because the reversibility axis" \
    "…and names the axis that actually supplied the headline"
eq "a diff with no counted lines says so" \
   "This diff changes no counted lines across 1 file(s)." \
   "$(render_surfaces "$(record R0 R0 "[$(file_entry a.md R0 0 0)]" R0 R0)" 0 | jq -r '.concentration')"

echo "— the body is BOUNDED under GitHub's 65536-char comment limit (measured, not estimated) —"
# 400 files, each with a 300-char deeply-nested path — comfortably past the limit unbounded.
deep="$(printf 'src/%.0s' $(seq 1 60))deeply/nested/path/component/that/keeps/going/and/going/file"
manyfiles="$(jq -nc --arg d "$deep" '[range(0;400) | {path:($d + "-\(.).go"), previous_path:null,
             additions:(. + 3), deletions:1, change_type:"MODIFIED", tier:"R3", classes:["cls"]}]')"
bigbody="$(render_surfaces "$(record R3 R3 "$manyfiles")" 0 | jq -r '.comment_body')"
n="${#bigbody}"
if [ "$n" -lt 65536 ]; then ok "400 long deeply-nested paths render in ${n} chars (< 65536)"
else bad "400 long deeply-nested paths render under 65536 chars" "$n"; fi
has "$bigbody" "- [ ] $DISPUTE_TEXT" "…and the bounded body STILL carries the checkbox the publisher parses"
has "$bigbody" "more file(s)" "…and says how many rows it dropped"
# The per-path display cap is what keeps one pathological path from dominating a row.
longest="$(awk '{ if (length($0) > m) m = length($0) } END { print m+0 }' <<<"$bigbody")"
if [ "$longest" -le 400 ]; then ok "no rendered line exceeds the per-path display cap (longest ${longest} chars)"
else bad "no rendered line exceeds the per-path display cap" "$longest"; fi

echo "— the write path: sticky create vs update, and the dispute label follows the checkbox —"
mkdir -p "$SANDBOX/bin"
export GH_LOG="$SANDBOX/gh.log" COMMENTS="$SANDBOX/comments.json" LABELS="$SANDBOX/labels.json" CBODY="$SANDBOX/cbody.json"
cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  *"-X "*)            exit 0 ;;                       # every write succeeds
  *issues/comments/*) cat "$CBODY"; exit 0 ;;         # the re-read by id
  *comments?per_page*|*comments\&*|*comments*)  cat "$COMMENTS"; exit 0 ;;
  *labels*)           cat "$LABELS"; exit 0 ;;   # already --jq'd to raw names
  *pulls/*)           echo "deadbeefcafe"; exit 0 ;;
esac
exit 0
STUB
chmod +x "$SANDBOX/bin/gh"
printf '[]\n' > "$COMMENTS"; printf '[]\n' > "$LABELS"; : > "$CBODY"   # CBODY holds the RAW body (`gh --jq .body`)

out="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$r" \
        PUBLISH_COMMENT=1 bash "$SCRIPT" 2>&1)"
has "$out" "created the sticky comment" "with no existing comment it CREATES one"
if grep -q -- '-X POST repos/test/repo/issues/7/comments' "$GH_LOG"; then ok "…via POST"
else bad "…via POST" "$(tr '\n' '|' < "$GH_LOG")"; fi

# Now the comment exists, authored by github-actions[bot], marker-first, box TICKED.
: > "$GH_LOG"
jq -n --arg m "$STICKY_MARKER" --arg d "$DISPUTE_TEXT" \
  '[{id:99, user:{type:"Bot", login:"github-actions[bot]"}, body:($m + "\n\n- [x] " + $d + " — x")}]' > "$COMMENTS"
printf '%s\n\n- [x] %s — x\n' "$STICKY_MARKER" "$DISPUTE_TEXT" > "$CBODY"
out="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$r" \
        PUBLISH_COMMENT=1 bash "$SCRIPT" 2>&1)"
has "$out" "updated the sticky comment (99)" "an existing comment is UPDATED IN PLACE — N pushes, one comment"
if grep -q -- '-X PATCH repos/test/repo/issues/comments/99' "$GH_LOG"; then ok "…via PATCH on its id"
else bad "…via PATCH on its id" "$(tr '\n' '|' < "$GH_LOG")"; fi
has "$out" "added '$DISPUTE_LABEL'" "a ticked box applies the dispute label"
if grep -q -- "-X POST repos/test/repo/issues/7/labels -f labels\[\]=$DISPUTE_LABEL" "$GH_LOG"; then
  ok "…via the labels endpoint"
else bad "…via the labels endpoint" "$(tr '\n' '|' < "$GH_LOG")"; fi

# Un-ticking clears it again: the checkbox is the source of truth, the label mirrors it.
: > "$GH_LOG"
printf '%s\n\n- [ ] %s — x\n' "$STICKY_MARKER" "$DISPUTE_TEXT" > "$CBODY"
printf '%s\n' "$DISPUTE_LABEL" > "$LABELS"   # LABELS holds the RAW names (`gh --jq .[].name`)
out="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$r" \
        PUBLISH_COMMENT=1 bash "$SCRIPT" 2>&1)"
has "$out" "removed '$DISPUTE_LABEL'" "un-ticking the box removes the dispute label"

echo "— a comment we do not own is never adopted —"
: > "$GH_LOG"
jq -n --arg m "$STICKY_MARKER" '[{id:5, user:{type:"User", login:"attacker"}, body:($m + "\nmine")},
                                 {id:6, user:{type:"Bot", login:"other-app[bot]"}, body:($m + "\nalso mine")},
                                 {id:7, user:{type:"Bot", login:"github-actions[bot]"}, body:("quoting " + $m)}]' > "$COMMENTS"
printf '[]\n' > "$LABELS"
out="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$r" \
        PUBLISH_COMMENT=1 bash "$SCRIPT" 2>&1)"
has "$out" "created the sticky comment" "a human's, another app's, and a QUOTED marker are all rejected"

echo "— both surfaces off is a no-op, and a failed write never fails the run —"
: > "$GH_LOG"
PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$r" bash "$SCRIPT" >/dev/null 2>&1
eq "with neither surface on, exit 0" 0 "$?"
eq "…and no request is made at all" "" "$(cat "$GH_LOG")"
cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "HTTP 403: Resource not accessible by integration (HTTP 403)" >&2
exit 1
STUB
chmod +x "$SANDBOX/bin/gh"
out="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$r" \
        PUBLISH_COMMENT=1 PUBLISH_CHECK=1 bash "$SCRIPT" 2>&1)"; rc=$?
eq "every write 403ing still exits 0 — this surface may never redden a PR" 0 "$rc"
has "$out" "::warning::" "…and says so as an annotation instead"

echo "— RENDER_ONLY writes nothing and emits the surfaces object —"
outj="$(RECORD="$r" RENDER_ONLY=1 PATH="$SANDBOX/bin:$PATH" bash "$SCRIPT" 2>/dev/null)"
eq "RENDER_ONLY emits a check title" "Risk: R1" "$(jq -r '.check_title' <<<"$outj")"
eq "…and a comment body" "true" "$(jq -r '(.comment_body | length) > 100' <<<"$outj")"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
