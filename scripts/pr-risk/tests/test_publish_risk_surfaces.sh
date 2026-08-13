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

# <tier> <floor> <files-json> [prov-tier] [rev-tier] [rev-files-json] [rev-reason] -> a record file.
# `rev-files-json` is the `axes.reversibility.files` attribution grade-pr-risk.sh emits (BE-7418):
# the paths that supplied the reversibility tier, or `null` on the rungs where the tier is not
# attributable to files. It defaults to `null`, so every fixture written before it existed keeps
# rendering exactly as it did — which is the backward-compatibility case the suite pins below.
record() {
  local tier="$1" floor="$2" files="$3" prov="${4:-R1}" rev="${5:-R1}" revfiles="${6:-null}"
  local revreason="${7:-checks green but the diff touches no test file}"
  local f="$SANDBOX/rec-$RANDOM.json" ff="$SANDBOX/files-$RANDOM.json"
  # The files array goes in via a FILE, not --argjson. Linux caps a single argv entry at 128KiB
  # (MAX_ARG_STRLEN) regardless of the much larger total ARG_MAX, so the bounded-body fixture below
  # (~170KiB of paths) makes `jq --argjson` die with "Argument list too long" on CI while passing
  # on macOS, which has no per-argument cap. Reading it from disk is limit-free on both.
  # `rev-files-json` stays an --argjson: it names a handful of paths, never the whole diff.
  printf '%s' "$files" > "$ff"
  jq -n --arg t "$tier" --arg fl "$floor" --slurpfile files "$ff" --arg p "$prov" --arg rv "$rev" \
        --argjson rf "$revfiles" --arg rr "$revreason" '
    {pr:7, risk:{map_version:"v0-generic", registry_version:"v0", tier:$t, status:"ok",
      reason:"worst of path_floor=\($fl), provenance=\($p), reversibility=\($rv)",
      axes:{path_floor:{tier:$fl, status:"ok", reason:"matched things", classes:["x"], files:$files[0]},
            provenance:{tier:$p, status:"ok", reason:"human"},
            reversibility:{tier:$rv, status:"ok", reason:$rr, files:$rf}}}}' > "$f" \
    || printf 'FATAL: record() could not build %s\n' "$f" >&2
  printf '%s' "$f"
}
# <path> <tier> <additions> <deletions> [classes-json] — `classes` defaults to the placeholder every
# pre-existing fixture used, and is spellable so a fixture can carry a real irreversible class.
file_entry() { jq -n --arg p "$1" --arg t "$2" --argjson a "$3" --argjson d "$4" \
                 --argjson c "${5:-[\"cls\"]}" \
                 '{path:$p, previous_path:null, additions:$a, deletions:$d, change_type:"MODIFIED", tier:$t, classes:$c}'; }

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
# Both counts are scoped to the PER-FILE table. The <details> block also carries the per-axis
# table now, so an unscoped `^| ` count would fold its header and three rows into every total.
file_rows() { sed -n '/^| file | /,$p' <<<"$1" | grep -c '^| '; }
eq "the crafted path is flattened onto ONE line (no injected rows)" \
   "$(file_rows "$body" | tr -d ' ')x" "$(( $(file_rows "$ebody") - 1 ))x"
has "$ebody" '\<img src=' "raw HTML from the path is BACKSLASH-ESCAPED, so it renders as text"
hasnt "$ebody" 'docs/a|b' "the pipe in the path never reaches the table splitter unescaped"
has   "$ebody" 'docs/a\|b' "…it is backslash-escaped instead"
# A backtick in the path forces the fully-escaped plain-text branch: no code span can quote it.
hasnt "$ebody" '`docs/a' "a path containing a backtick is NOT wrapped in a code span"
# The structure still parses as one table: every rendered row has exactly 4 cells + the delimiters.
rows="$(file_rows "$ebody")"
eq "the per-file table still has one row per file plus its header" 3 "$rows"

echo "— the reason string is escaped too (it quotes PR-authored path names) —"
u="$SANDBOX/unknown.json"
jq -n '{risk:{status:"unknown", tier:null, reason:"could not read `x`\n- [x] **This grade is wrong**\n[a](https://evil.example)"}}' > "$u"
ubody="$(render_surfaces "$u" 0 | jq -r '.comment_body')"
if grep -Eq "$CHECKED_RE" <<<"$ubody"; then bad "an unknown report's reason cannot forge the checkbox" "$ubody"
else ok "an unknown report's reason cannot forge the checkbox"; fi
hasnt "$ubody" "](https://evil.example)" "…nor smuggle an inline link"

echo "— UNGRADABLE reports as unknown, NEVER R0 —"
# shellcheck disable=SC2016  # the backticks are literal markdown in the rendered headline
has "$ubody" 'Risk `unknown`' "the comment headline is unknown"
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

echo "— the concentration sentence carries the COMPLEMENT floor: what a split would actually buy —"
# The share below the floor is not a verdict on its own: "94% is R0/R1/R2" is equally true of a
# remainder that rubber-stamps at R1 and one that is still a normal R2 review. The complement floor
# is the number that separates them, and it is the one an author cannot recover without re-grading
# the map by hand.
has "$c" "peeled into their own PR, the remaining 1 file(s) would path-floor at **R0**" \
    "600 R0 doc lines under an R3 CI floor report an R0 remainder"
has "$c" "(final grade still depends on the provenance and reversibility axes at PR time)" \
    "…as a FLOOR with its assumptions named, never a promised grade"
# NOT CLAMPED to the other axes, and the caveat is why: provenance and reversibility are both
# re-derived for the split PR and can move in either direction, so max(path, provenance,
# reversibility) would be no more a floor for the remainder than the path number alone. Path floor
# R3 over provenance R1 / reversibility R2 still reports the remainder's own R0.
has "$(render_surfaces "$(record R3 R3 "$big" R1 R2)" 0 | jq -r '.concentration')" \
    "the remaining 1 file(s) would path-floor at **R0**" \
    "a lower-ranked provenance/reversibility does NOT clamp the path-axis number"
# Worst-of over the remainder, not the biggest or the last file: 500 R0 lines cannot cancel 100 R1
# ones, exactly as the floor itself is a max rather than last-match-wins.
mix="[$(file_entry docs/a.md R0 500 0), $(file_entry src/app.ts R1 100 0), $(file_entry .github/workflows/d.yml R3 30 10)]"
has "$(render_surfaces "$(record R3 R3 "$mix")" 0 | jq -r '.concentration')" \
    "the remaining 2 file(s) would path-floor at **R1**" \
    "a mixed remainder takes its WORST per-file floor (R0 + R1 -> R1), over both files"
# The ticket's worked example, and the case that makes the readout worth printing: the remainder is
# still R2, so peeling the CI file out buys a normal review rather than the R1 rubber-stamp lane.
worked="[$(file_entry src/app.ts R2 200 0), $(file_entry package.json R3 4 0), $(file_entry .github/workflows/ci.yml R3 10 0)]"
has "$(render_surfaces "$(record R3 R3 "$worked")" 0 | jq -r '.concentration')" \
    "the remaining 1 file(s) would path-floor at **R2**" \
    "an R2 remainder says R2 — the split that is NOT worth much still reports honestly"

echo "— …and stays SILENT where a split cannot help —"
# IRREDUCIBLE: every changed line is already at the floor, so there is no remainder to peel. The
# existing wording is the whole answer; a clause here would offer a split that does not exist.
irr="$(render_surfaces "$(record R3 R3 "[$(file_entry .github/workflows/a.yml R3 20 0), $(file_entry .github/workflows/b.yml R3 20 0)]")" 0 | jq -r '.concentration')"
eq "an irreducible diff renders byte-identically to before this clause existed" \
   "All 40 changed lines sit at R3 on the path axis." "$irr"
# NOT PATH-DECIDED: provenance supplied the headline, so peeling the top path files leaves the tier
# where it is. Quoting a path-axis reduction under it would point the reader at the wrong number.
# Its own fixture rather than $big, because the floor here is R2 and grade-pr-risk.sh derives the
# floor as `worst` over the SAME per-file rules: a record whose floor is R2 while a file on it reads
# R3 cannot be graded, so reusing $big would pin the clause against an input production never emits.
path_r2="[$(file_entry docs/a.md R0 500 100), $(file_entry src/app.ts R2 30 10)]"
np="$(render_surfaces "$(record R3 R2 "$path_r2" R3 R1)" 0 | jq -r '.concentration')"
hasnt "$np" "peeled into their own PR" "a headline another axis supplied gets NO reducibility clause"
has "$np" "40 lines. The headline tier is R3 rather than the path floor R2" \
    "…and the sentence ends exactly as it did before, straight into the axis attribution"
# A PROVENANCE TIE is not "the path axis decided": provenance is a property of the AUTHOR, so if it
# also proposes R3 the remainder is R3 too and the split buys nothing. This is the case a rank
# comparison alone would get wrong.
tied="$(render_surfaces "$(record R3 R3 "$big" R3 R1)" 0 | jq -r '.concentration')"
hasnt "$tied" "peeled into their own PR" "an axis TIED with the path floor also suppresses the clause"

echo "— …but a REVERSIBILITY tie the peel would remove lets the clause speak (BE-7419) —"
# A reversibility tie is a property of specific FILES, not of the author — and those files can be
# exactly the ones the clause proposes peeling. One R3 migration under 600 R0 doc lines rendered no
# clause at all, while the identical file set at reversibility R1 rendered the full split pitch:
# the same peel, described two ways, because the gate could not tell the two ties apart.
mig="[$(file_entry docs/a.md R0 500 100), $(file_entry migrations/0042_drop.sql R3 30 10 '["migrations"]')]"
MIG_WHY="touches migrations — mutates persistent state or deletes data; reverting the code does not restore it"
rev_surf="$(render_surfaces "$(record R3 R3 "$mig" R1 R3 '["migrations/0042_drop.sql"]' "$MIG_WHY")" 0)"
rev_conc="$(jq -r '.concentration' <<<"$rev_surf")"
rev_body="$(jq -r '.comment_body' <<<"$rev_surf")"
has "$rev_conc" "peeled into their own PR, the remaining 1 file(s) would path-floor at **R0**" \
    "a reversibility tie whose attributed files are ALL inside the peeled set gets the clause"
has "$rev_conc" "(final grade still depends on the provenance and reversibility axes at PR time)" \
    "…keeping the caveat, which stays honest: the remainder re-derives reversibility on its own checks"
# The <details> pitches a PATH split, so a headline crediting reversibility alone would send the
# reader looking for a reduction on the axis it just told them decided the tier.
has "$rev_body" "**path and reversibility**: touches migrations" \
    "…the headline names BOTH axes and carries reversibility's reason"
has "$rev_body" "6% of 640 changed lines carry it (1 file(s))." \
    "…and \$conc_short fires for the combined driver, not just plain 'path'"

# THE CONSUMER-OVERRIDE CASE the full-subset test exists for. A map override can put an
# irreversible-class file BELOW the path floor — remap `migrations` to R1 while leaving it in
# `irreversible_classes` — and there peeling $topf (the CI file) leaves the reversibility reason
# exactly where it was. A "does reversibility name any peeled file?" test would speak here wrongly.
override="[$(file_entry docs/a.md R0 500 100), $(file_entry migrations/0042_drop.sql R1 20 0 '["migrations"]'), $(file_entry .github/workflows/ci.yml R3 30 10)]"
ov_surf="$(render_surfaces "$(record R3 R3 "$override" R1 R3 '["migrations/0042_drop.sql"]' "touches migrations")" 0)"
hasnt "$(jq -r '.concentration' <<<"$ov_surf")" "peeled into their own PR" \
      "a reversibility tie attributed to a file BELOW the floor keeps the clause SILENT"
has   "$(jq -r '.comment_body' <<<"$ov_surf")" "**reversibility**: touches migrations" \
      "…and the headline credits reversibility alone, exactly as it did before"
hasnt "$(jq -r '.comment_body' <<<"$ov_surf")" "changed lines carry it" \
      "…with no above-the-fold split fragment either"

# BACKWARD COMPATIBILITY, pinned: `files` is absent/null on the R2 and R1 rungs (properties of the
# head commit and of the whole change set, removable by dropping no files) and on every record
# graded before BE-7418. Those must all fail SAFE, back to the unconditional suppression.
hasnt "$(render_surfaces "$(record R3 R3 "$mig" R1 R3)" 0 | jq -r '.concentration')" "peeled into their own PR" \
      "a reversibility tie carrying files:null (an R2-style tie, or a pre-BE-7418 record) stays SILENT"
hasnt "$(render_surfaces "$(record R3 R3 "$mig" R1 R3 '[]')" 0 | jq -r '.concentration')" "peeled into their own PR" \
      "…and an EMPTY attribution is rejected, not read as a subset of everything"
hasnt "$(render_surfaces "$(record R3 R3 "$mig" R1 R3 '["migrations/0042_drop.sql","docs/a.md"]')" 0 | jq -r '.concentration')" \
      "peeled into their own PR" \
      "…nor is a PARTIAL subset — one attributed path outside the peeled set is enough to suppress"
# Both ties at once: the provenance half is unaffected by any peel, so it still decides.
hasnt "$(render_surfaces "$(record R3 R3 "$mig" R3 R3 '["migrations/0042_drop.sql"]' "$MIG_WHY")" 0 | jq -r '.concentration')" \
      "peeled into their own PR" \
      "a provenance tie suppresses the clause even when the reversibility tie IS removable"
# The ungraded surfaces never reach the sentence at all.
hasnt "$(render_surfaces "$u" 0 | jq -r '.concentration')" "peeled into their own PR" \
      "an ungradable record proposes no split"
# A REMAINDER THAT ROUNDS TO 0%: one R0 line against 9999 R3 ones. There IS a below-floor file, so
# the set is non-empty, but the sentence has just printed "**0%** of this diff is R0/R1/R2" — and a
# clause under that would pitch a whole extra PR to relocate a single line. The gate is the
# sentence's own printed share, so the two halves can never contradict each other.
tiny="$(render_surfaces "$(record R3 R3 "[$(file_entry docs/a.md R0 1 0), $(file_entry .github/workflows/d.yml R3 9999 0)]")" 0 | jq -r '.concentration')"
has "$tiny" "**0% of this diff is R0/R1/R2**" "a 1-line remainder still rounds the share to 0%…"
hasnt "$tiny" "peeled into their own PR" "…and a 0% remainder is offered NO split"
# One line the other way is enough: 1% is a share the sentence prints, so the clause speaks.
small="$(render_surfaces "$(record R3 R3 "[$(file_entry docs/a.md R0 100 0), $(file_entry .github/workflows/d.yml R3 9900 0)]")" 0 | jq -r '.concentration')"
has "$small" "peeled into their own PR, the remaining 1 file(s) would path-floor at **R0**" \
    "a remainder the share sentence does print (1%) keeps the clause"

echo "— the body is BOUNDED under GitHub's 65536-char comment limit (measured, not estimated) —"
# 400 files, each with a 300-char deeply-nested path — comfortably past the limit unbounded.
deep="$(printf 'src/%.0s' $(seq 1 60))deeply/nested/path/component/that/keeps/going/and/going/file"
manyfiles="$(jq -nc --arg d "$deep" '[range(0;400) | {path:($d + "-\(.).go"), previous_path:null,
             additions:(. + 3), deletions:1, change_type:"MODIFIED", tier:"R3", classes:["cls"]}]')"
bigrec="$(record R3 R3 "$manyfiles")"
# Pin the FIXTURE before asserting on the body: a record that failed to build renders the short
# "unknown" body, which would sail under the limit and pass the size check for the wrong reason.
eq "the oversized fixture really carries 400 files" "400" \
   "$(jq '.risk.axes.path_floor.files | length' "$bigrec" 2>/dev/null)"
bigbody="$(render_surfaces "$bigrec" 0 | jq -r '.comment_body')"
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

echo "— A FAILED READ WRITES NOTHING. Both reads are load-bearing —"
# `rc=1` alone is not enough: control used to fall through to the create/clear branches. A failed
# LIST then POSTed a SECOND sticky (and every later failure another, breaking "N pushes, one
# comment" on exactly the transient class a 50-target backfill makes likely), and a failed RE-READ
# rewrote the body with the box cleared AND removed the dispute label — permanently erasing a
# registered reviewer disagreement on nothing worse than a 500.
: > "$GH_LOG"; printf '[]\n' > "$LABELS"
cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  *issues/7/comments*) echo "gh: upstream is unavailable (HTTP 500)" >&2; exit 1 ;;
  *labels*)            cat "$LABELS"; exit 0 ;;
esac
exit 0
STUB
chmod +x "$SANDBOX/bin/gh"
out="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$r" \
        PUBLISH_COMMENT=1 bash "$SCRIPT" 2>&1)"; rc=$?
eq "a failed comment LIST still exits 0 (advisory)" 0 "$rc"
hasnt "$(cat "$GH_LOG")" "-X " "…and issues NO write at all — a blind create is a DUPLICATE"
has "$out" "DUPLICATE" "…and names that as the reason nothing was written"
# ERRF is initialised once at startup, not lazily inside `ghq`: most ghq calls sit in a command
# substitution, so a lazy init set it in the SUBSHELL and every gherr in a warning printed
# nothing — hiding whether the failure was auth, throttling or transport.
has "$out" "HTTP 500" "…and the warning carries gh's own stderr (ERRF survives the subshell)"

# Now the sticky EXISTS and its box is TICKED, but the re-read by id fails.
: > "$GH_LOG"
jq -n --arg m "$STICKY_MARKER" '[{id:99, user:{type:"Bot", login:"github-actions[bot]"}, body:($m + "\nx")}]' > "$COMMENTS"
printf '%s\n' "$DISPUTE_LABEL" > "$LABELS"
cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  *issues/comments/*)  echo "gh: bad gateway (HTTP 502)" >&2; exit 1 ;;   # the re-read by id
  *labels*)            cat "$LABELS"; exit 0 ;;
  *comments*)          cat "$COMMENTS"; exit 0 ;;
esac
exit 0
STUB
chmod +x "$SANDBOX/bin/gh"
out="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$r" \
        PUBLISH_COMMENT=1 bash "$SCRIPT" 2>&1)"; rc=$?
eq "a failed dispute RE-READ still exits 0" 0 "$rc"
hasnt "$(cat "$GH_LOG")" "-X PATCH" "…and does NOT rewrite the body with the box cleared"
hasnt "$(cat "$GH_LOG")" "-X DELETE" "…and does NOT strip the dispute label it cannot verify"
has "$out" "UNKNOWN" "…and reports the dispute state as unknown rather than unticked"

echo "— the comment scan runs BACKWARDS from the last page —"
# `sort=created&direction=desc` was never a parameter of GET /issues/{n}/comments (those belong to
# the repo-wide /issues/comments route) and GitHub ignores unknown query params, so the listing is
# always OLDEST-first. Our sticky is at the END: the scan has to start there or the page bound is
# unrecoverable on a chatty PR.
: > "$GH_LOG"
printf '%s\n\n- [ ] %s — x\n' "$STICKY_MARKER" "$DISPUTE_TEXT" > "$CBODY"
printf '[]\n' > "$LABELS"
cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  *--silent*)         printf 'HTTP/2.0 200 OK\r\nlink: <https://api.github.com/x?per_page=100&page=2>; rel="next", <https://api.github.com/x?per_page=100&page=3>; rel="last"\r\n\r\n'; exit 0 ;;
  *page=3*)           cat "$COMMENTS"; exit 0 ;;
  *issues/comments/*) cat "$CBODY"; exit 0 ;;
  *labels*)           cat "$LABELS"; exit 0 ;;
  *comments*)         echo '[]'; exit 0 ;;
esac
exit 0
STUB
chmod +x "$SANDBOX/bin/gh"
out="$(PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$r" \
        PUBLISH_COMMENT=1 bash "$SCRIPT" 2>&1)"
has "$out" "updated the sticky comment (99)" "a sticky on the LAST page is found and UPDATED"
has "$(cat "$GH_LOG")" "page=3" "…because page 3 was the first page fetched"
# `&page=1` anchored at end-of-line: `per_page=100` contains the substring "page=1", and the
# header-only probe legitimately asks for page 1.
if grep -v -- '--silent' "$GH_LOG" | grep -Eq '&page=1$'
then bad "…and page 1 was never walked forwards" "$(tr '\n' '|' < "$GH_LOG")"
else ok "…and page 1 was never walked forwards"; fi

echo "— the Check Run attaches to the GRADED commit, not to head-as-of-now —"
# The grade job waits out the rollup settle and the publish is a whole further job, so re-reading
# head here stamps the PREVIOUS commit's tier onto a new head — in the one artifact whose value is
# being an immutable, correctly-attributed commit record.
export CHECKBODY="$SANDBOX/checkbody.json"
rsha="$SANDBOX/rec-graded-sha.json"
jq '. + {head_sha:"9999999999999999999999999999999999999999"}' "$r" > "$rsha"
: > "$GH_LOG"
cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  *check-runs*) cat > "$CHECKBODY"; exit 0 ;;
  *pulls/*)     echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; exit 0 ;;
esac
exit 0
STUB
chmod +x "$SANDBOX/bin/gh"
PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$rsha" \
  PUBLISH_CHECK=1 bash "$SCRIPT" >/dev/null 2>&1
eq "the check is posted on the graded sha" \
   "9999999999999999999999999999999999999999" "$(jq -r '.head_sha' "$CHECKBODY" 2>/dev/null)"
hasnt "$(cat "$GH_LOG")" "pulls/" "…and head is never re-read when the record names its commit"
eq "…and the conclusion is still hardcoded neutral" neutral "$(jq -r '.conclusion' "$CHECKBODY" 2>/dev/null)"
# A record with NO graded oid (an ungradable PR) still gets a check — that verdict is about the
# PR, not about a commit, so head is the honest anchor there.
: > "$GH_LOG"
PATH="$SANDBOX/bin:$PATH" REPO=test/repo PR_NUMBER=7 RECORD="$r" \
  PUBLISH_CHECK=1 bash "$SCRIPT" >/dev/null 2>&1
eq "a record with no graded sha falls back to the API head" \
   "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" "$(jq -r '.head_sha' "$CHECKBODY" 2>/dev/null)"

echo "— a TRUNCATED body is still well-formed markdown —"
# The backstop re-appends the footer so a cut body still carries the checkbox. It has to close the
# `<details>` the graded branch opened too, or that checkbox renders INSIDE the collapsed section.
# shellcheck disable=SC2034  # read by render_surfaces in the SOURCED publisher, not here
COMMENT_MAX_CHARS=900
tbody="$(render_surfaces "$bigrec" 0 | jq -r '.comment_body')"
# shellcheck disable=SC2034  # restored so any later assertion renders against the real bound
COMMENT_MAX_CHARS=65000
has "$tbody" "(truncated" "the backstop fires when everything else fails to fit"
has "$tbody" "- [ ] $DISPUTE_TEXT" "…and the truncated body STILL carries the checkbox"
eq "…and every <details> it opened is closed" \
   "$(grep -c '<details>' <<<"$tbody")" "$(grep -c '</details>' <<<"$tbody")"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
