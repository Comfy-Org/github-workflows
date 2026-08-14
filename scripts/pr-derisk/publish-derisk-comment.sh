#!/usr/bin/env bash
# publish-derisk-comment.sh — render a de-risk plan as ONE sticky PR comment, and keep it there.
#
# Reads the plan object plan-derisk.sh emits and draws the same comment for all three of its
# statuses. A silent no-op is never an outcome: someone typed `/derisk` and is waiting, so a
# fallback and a failure each get a comment saying which one happened and why.
#
# ── RENDERING CONVENTIONS ARE INHERITED, NOT REINVENTED ────────────────────────────────────────
# publish-risk-surfaces.sh (BE-6326) set them and this file follows them exactly:
#   * ONE LINE ABOVE THE FOLD, the long form inside collapsed `<details>`. This comment lands on
#     PRs that already carry CodeRabbit and an 8-cell review panel; an advisory suggestion has
#     the weakest claim on the reader's scroll.
#   * EVERY PR-CONTROLLED STRING IS ESCAPED. Paths come from the diff and git permits `|`,
#     backticks and newlines in a filename; rendered raw into a table such a path breaks out of
#     its row. Here the MODEL'S OWN OUTPUT is PR-controlled too — a diff can try to talk the
#     planner into writing markdown — so step names, descriptions and rationales get identical
#     treatment. Nothing in this comment may start a line it was not given.
#   * BOUNDED BY CONSTRUCTION under GitHub's 65536-character comment limit, with a backstop
#     truncation, because a 422 here would leave the reader with no comment at all.
#   * CREATED ONCE, UPDATED IN PLACE, matched on a marker that is its own first line.
#
# There is deliberately NO checkbox and no label: this comment collects nothing and drives
# nothing. It is a suggestion an author acts on or ignores.
#
# Inputs (env):
#   REPO        owner/name                                                          (required)
#   PR_NUMBER   the PR number                                                       (required)
#   PLAN        path to the plan JSON from plan-derisk.sh                            (required)
#   ACTOR       the login that typed /derisk (rendered as attribution; escaped)
#   STICKY_LOGINS  extra bot logins allowed to own our sticky comment (comma-separated)
#   DRY_RUN     1 = render to stdout and write nothing
#   GH_TOKEN    token for gh — needs `pull-requests: write`
#
# Exit: 0 = published, or rendered under DRY_RUN, or a write failed and was ANNOUNCED.
#       2 = usage/setup error — nothing was attempted.
#
# Deliberately bash (shebang), not zsh — CI runners and the test suite both exercise bash.

set -uo pipefail

REPO="${REPO:-}"
PR_NUMBER="${PR_NUMBER:-}"
PLAN="${PLAN:-}"
ACTOR="${ACTOR:-}"
STICKY_LOGINS="${STICKY_LOGINS:-}"
DRY_RUN="${DRY_RUN:-0}"

# The one marker. Rendered as the body's FIRST line; matched by find_sticky. Distinct from
# pr-risk's `<!-- ci-pr-risk -->` so the two stickies can never adopt each other's comment.
STICKY_MARKER="<!-- ci-pr-derisk -->"
COMMENT_MAX_CHARS=65000
STICKY_MAX_PAGES="${STICKY_MAX_PAGES:-10}"

log()  { printf '[publish-derisk-comment] %s\n' "$*" >&2; }
warn() { printf '::warning::[publish-derisk-comment] %s\n' "$*" >&2; }
die()  { printf '[publish-derisk-comment] ERROR %s\n' "$*" >&2; exit 2; }

if [ -z "$PLAN" ] || [ ! -f "$PLAN" ]; then
  die "PLAN (path to the plan JSON) is required and must be readable"
fi
command -v jq >/dev/null 2>&1 || die "jq is required"
if [ "$DRY_RUN" != 1 ]; then
  [ -n "$REPO" ] || die "REPO is required"
  [ -n "$PR_NUMBER" ] || die "PR_NUMBER is required"
  command -v gh >/dev/null 2>&1 || die "gh is required"
fi

# ---- the render program -------------------------------------------------------------------------
# PURE: plan JSON in, comment body out. No network, no writes — which is what makes the escaping
# and the length bound unit-testable without stubbing GitHub.
render_program() {
cat <<'JQ'
    # --- markdown safety, applied to EVERYTHING that is or quotes PR-controlled text -----------
    # Lifted verbatim in behaviour from publish-risk-surfaces.sh; the two files render different
    # comments from different inputs, but the threat is identical and so is the answer.
    def flat: tostring | gsub("[\r\n]"; " ");
    def md_escape: gsub("(?<c>[\\\\`*_{}\\[\\]()#+.!<>|~-])"; "\\" + .c);
    def md_text($lim): flat | .[0:$lim] | md_escape;
    def md_path:
      (flat | if length > 160 then .[0:160] + "…" else . end)
      | if test("[`\\\\]") then md_escape else "`" + gsub("\\|"; "\\|") + "`" end;
    def tier_rank: {"R0":0,"R1":1,"R2":2,"R3":3}[.] // 3;

    . as $p
    | ($p.headline_tier // null) as $tier
    # THE COMPARISON AXIS IS THE PATH FLOOR, NOT THE HEADLINE, and mixing them up is how this
    # comment claims a lane win it cannot deliver. `grade = worst(path_floor, provenance,
    # reversibility)` but a split only moves the PATH axis: on a fork PR (provenance R3, path floor
    # R0) or a `/derisk` typed while checks are still pending (reversibility R2), EVERY step reads
    # "below" the headline while the axis that actually set the grade is untouched. Older plans
    # carry no `path_floor_tier`, so the headline is the fallback there.
    | (($p.path_floor_tier // $p.headline_tier) // null) as $ptier
    | ($p.steps // []) as $steps
    | ($p.status // "failed") as $status
    # THE VERDICT IS COMPUTED FROM THE FLOORS, NEVER FROM THE MODEL'S PROSE. `summary` is the
    # model's sentence and is rendered as such, below the fold; the line above the fold is
    # arithmetic over tiers the grader produced. That is what makes the single-class-monolith
    # honesty rule a property rather than a request: when nothing lands below the path floor there
    # is no wording available here that claims a lane win.
    | ([$steps[] | select((.floor | type) == "string" and ($ptier | type) == "string"
                          and (.floor | tier_rank) < ($ptier | tier_rank))]) as $cheaper
    | ([$steps[] | select((.floor | type) == "string" and ($ptier | type) == "string"
                          and (.floor | tier_rank) >= ($ptier | tier_rank))]) as $atfloor
    | ([$cheaper[] | .lines] | add // 0) as $cheap_lines
    | ($p.total_lines // 0) as $total
    | (if $total <= 0 then 0 else (100 * $cheap_lines / $total) | round end) as $cheap_pct
    # Named explicitly when a NON-PATH axis holds the grade up, because then even a perfect
    # partition cannot lower the lane and the reader is owed that in the same breath as the split.
    | (if ($tier | type) == "string" and ($ptier | type) == "string"
           and ($tier | tier_rank) > ($ptier | tier_rank)
        then " The pull request's overall **\($tier)** is set by a non-path axis (provenance or reversibility), which no partition moves."
        else "" end) as $axisnote

    | (if $status != "planned" then
         (if $status == "fallback" then "**No split plan** · " else "**`/derisk` failed** · " end)
         + ($p.note | md_text(600))
       elif ($cheaper | length) == 0 then
         "**\($steps | length) smaller single-concern \($ptier // "?")s — same lane.** Every step still path-floors at **\($ptier // "?")**, so this split buys review focus, not a cheaper lane." + $axisnote
       else
         "**Split into \($steps | length): \($cheaper | length) step(s) path-floor below \($ptier // "?")** (\($cheap_pct)% of the changed lines), leaving \($atfloor | length) step(s) carrying the risk." + $axisnote
       end) as $headline

    | ([ "| # | step | files | lines | path floor |",
         "|---|---|---|---|---|" ]
       + [ $steps[]
           | "| \(.index + 1) | \(.name | md_text(80)) | \(.files | length) | \(.lines) | "
             + (if (.floor | type) == "string" then "**\(.floor)**" else "unknown" end) + " |" ]) as $chain

    | ([ $steps[]
         | "#### \(.index + 1). \(.name | md_text(120))",
           "",
           (.description | md_text(400)),
           "",
           "- **Path floor:** " + (if (.floor | type) == "string" then "**\(.floor)**" else "unknown" end)
             + (if (.floor_reason | type) == "string" then " — \(.floor_reason | md_text(200))" else "" end),
           "- **Lands after:** " + (if (.depends_on | length) == 0 then "nothing — it can go first"
                                    else ([.depends_on[] | "step \(. + 1)"] | join(", ")) end),
           "- **Why it is cheap to review:** " + (.inertness | md_text(400)),
           ( if (.review_ask // "") == "" then empty
             else "- **Review ask:** " + (.review_ask | md_text(400)) end ),
           "",
           "<details><summary>files (\(.files | length))</summary>",
           "",
           ([.files[] | "- " + (. | md_path)] | join("\n")),
           "",
           "</details>",
           "" ]) as $detail

    | ([ "**Assumptions behind every floor above.** Each number is the PATH FLOOR that",
         "`grade-pr-risk.sh --stdin` computed for that step's files — the same judge, the same map",
         "and the same rules that graded this pull request. It is a floor with its assumptions named,",
         "not a promised grade: the final grade is `worst(path_floor, provenance, reversibility)`, and",
         "the other two axes are re-derived per split PR. Provenance follows the author (a narrower",
         "path set can newly assert — or newly fail — a runbook shape), and reversibility keys on a",
         "check rollup that has not run yet. So a split can land WORSE than its floor; it can never",
         "land better.",
         "",
         "The model proposed the partition only. It was told not to state a tier, and any tier it",
         "stated was discarded before this comment was rendered." ]) as $caveats

    | ([ "**Land it as a chain, not a stack.** These are sequential pull requests against the default",
         "branch in dependency order — never stacked branches, whose base disappears on merge.",
         "",
         "Link the whole chain from every PR in it, and state the risk-carrying PR's review scope as",
         "THE CHAIN, not its own diff. A split that lets a risky change be reviewed as a small one is",
         "risk laundering, and it is the failure mode this plan is most able to cause." ]) as $howto

    | ([ ($p.note | md_text(600)) ]) as $modelnote

    | { body:
        ([ "\(env.MARKER)",
           $headline,
           "" ]
         + (if $status == "planned" then
              $chain
              + [ "",
                  "<details><summary>The plan — step by step</summary>", "" ]
              + $detail
              + [ "</details>", "",
                  "<details><summary>How to land this chain</summary>", "" ]
              + $howto
              + [ "", "</details>", "",
                  "<details><summary>How these floors were computed</summary>", "" ]
              + $caveats
              + [ "", "</details>", "",
                  "<details><summary>What the planner said</summary>", "" ]
              + $modelnote
              + [ "", "</details>" ]
            else [] end)
         + [ "", "<sub>Advisory. Nothing here is gated, routed or filed"
             + (if env.ACTOR == "" then "" else " · requested by " + (env.ACTOR | md_text(60)) end)
             + " · `/derisk` (beta)</sub>" ]
         | join("\n")) }
JQ
}

BODY="$(MARKER="$STICKY_MARKER" ACTOR="$ACTOR" jq -r "$(render_program) | .body" "$PLAN")" \
  || die "the plan could not be rendered"

# BOUNDED BY CONSTRUCTION, then backstopped. The per-field caps above make an over-long body
# unlikely; this makes it impossible. A body over the limit is a 422 and the reader gets NOTHING,
# which is strictly worse than a truncated plan that still names where it was cut.
if [ "${#BODY}" -gt "$COMMENT_MAX_CHARS" ]; then
  BODY="${BODY:0:$((COMMENT_MAX_CHARS - 200))}

_…truncated: the rendered plan exceeded GitHub's comment limit._"
  log "body truncated to fit the comment limit"
fi

if [ "$DRY_RUN" = 1 ]; then
  printf '%s\n' "$BODY"
  exit 0
fi

# ---- the sticky ----------------------------------------------------------------------------------
ERRF="$(mktemp "${TMPDIR:-/tmp}/publish-derisk-err.XXXXXX")" || die "mktemp failed"
trap 'rm -f "$ERRF"' EXIT
gherr() { tr '\n' ' ' < "$ERRF" | sed 's/[[:space:]]*$//'; }
ghq()   { gh "$@" 2>"$ERRF"; }

# Bot TYPE alone is not identity: every other GitHub App on the repo is also a Bot. Matching the
# marker alone is worse — the marker is public, so a PR author could pre-post a comment carrying
# it and thereafter own the surface this workflow updates.
allowed_logins() {
  printf '%s\n' "github-actions[bot]"
  printf '%s' "$STICKY_LOGINS" | tr ',' '\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep . || true
}

# The listing is always OLDEST-FIRST (`GET /issues/{n}/comments` takes no sort/direction), so the
# scan walks BACKWARDS from the last page: our sticky is a comment we posted, so it is near the
# end. Scanning forwards made the page bound unrecoverable — on a chatty PR the sticky sits past
# the cap and every re-run POSTs a fresh one that also lands past it.
sticky_last_page() {
  local hdr n
  hdr="$(ghq api -i --silent "repos/$REPO/issues/$PR_NUMBER/comments?per_page=100&page=1")" || return 1
  n="$(printf '%s' "$hdr" | tr -d '\r' | grep -i '^link:' \
       | tr ',' '\n' | sed -n 's/.*[?&]page=\([0-9][0-9]*\)>[[:space:]]*;[[:space:]]*rel="last".*/\1/p' | head -n 1)"
  case "$n" in ''|*[!0-9]*) n=1 ;; esac
  printf '%s' "$n"
}

# rc 1 means WE DO NOT KNOW whether a sticky exists. The caller must not fall through to the
# create branch on it, or every transient list failure posts another comment.
find_sticky() {
  local page last floor body allow id
  allow="$(allowed_logins | jq -Rsc 'split("\n") | map(select(length > 0))')"
  last="$(sticky_last_page)" || { warn "could not list comments on $REPO#$PR_NUMBER: $(gherr)"; return 1; }
  page="$last"
  floor=$(( last - STICKY_MAX_PAGES + 1 )); [ "$floor" -ge 1 ] || floor=1
  while [ "$page" -ge "$floor" ]; do
    body="$(ghq api "repos/$REPO/issues/$PR_NUMBER/comments?per_page=100&page=${page}")" \
      || { warn "could not list comments on $REPO#$PR_NUMBER: $(gherr)"; return 1; }
    # Our body is rendered with the marker as its FIRST line. A bot that QUOTES this comment
    # carries the marker too, nested inside its own prose — which login matching alone cannot see,
    # because every other GITHUB_TOKEN workflow in the repo posts as github-actions[bot] as well.
    id="$(jq -r --argjson allow "$allow" --arg m "$STICKY_MARKER" '
            [ .[] | . as $c
                  | select((.user.type // "") == "Bot")
                  | select(($allow | index($c.user.login // "")) != null)
                  | select(((.body // "") | startswith($m)))
                  | .id ] | last // empty' <<<"$body")" || return 1
    [ -z "$id" ] || { printf '%s' "$id"; return 0; }
    page=$(( page - 1 ))
  done
  return 0
}

id=""
if ! id="$(find_sticky)"; then
  warn "the existing /derisk comment could not be located on $REPO#$PR_NUMBER, so nothing was written — a failed read is not evidence there is no comment, and posting on it would leave two."
  exit 0
fi

if [ -n "$id" ]; then
  jq -n --arg b "$BODY" '{body:$b}' \
    | ghq api -X PATCH "repos/$REPO/issues/comments/${id}" --input - >/dev/null \
    || { warn "could not update the /derisk comment ${id} on $REPO#$PR_NUMBER: $(gherr)"; exit 0; }
  log "updated the /derisk comment (${id}) on $REPO#$PR_NUMBER"
else
  jq -n --arg b "$BODY" '{body:$b}' \
    | ghq api -X POST "repos/$REPO/issues/$PR_NUMBER/comments" --input - >/dev/null \
    || { warn "could not post the /derisk comment on $REPO#$PR_NUMBER: $(gherr)"; exit 0; }
  log "posted the /derisk comment on $REPO#$PR_NUMBER"
fi
