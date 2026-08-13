#!/usr/bin/env bash
# publish-risk-surfaces.sh — the two OPT-IN publish surfaces of the advisory pr-risk grade:
# a Check Run on the head commit, and ONE sticky PR comment carrying the per-file breakdown,
# the risk concentration and a "this grade is wrong" checkbox.
#
# The label (apply-risk-label.sh) is the shipped default and is untouched by this script. Both
# surfaces here are OFF unless the caller switches them on (pr-risk.yml `check_run:` /
# `sticky_comment:`, both `false` by default), because a bot comment on every PR across every
# enrolled repo is a visible behaviour change and has to be a per-consumer decision — the same
# precedent pr-size.yml set with `mode: warn`.
#
# NOTHING HERE CAN FAIL A CHECK OR BLOCK A MERGE. The Check Run's conclusion is hardcoded
# `neutral` (a neutral check cannot fail a PR even if a repo later marks it required), and every
# write failure in this script is an annotation plus exit 0 — the grade is advisory, so a
# publish surface that could redden a PR would quietly turn it into a gate. Only a usage error
# (a missing repo/PR, an unreadable record path) exits non-zero, and grade-targets.sh treats
# even that as a warning on the target rather than a failed grade.
#
# ── WHY A PER-FILE BREAKDOWN AT ALL ────────────────────────────────────────────────────────────
# A bare `risk:R3` is unexplained: the only way to learn why was to open the Actions run. The
# concentration sentence ("94% of this diff is R0/R1; the 6% that makes it R3 is these two files,
# 40 lines") is what makes an R3 actionable rather than opaque, and what tells a reviewer when an
# R3 is a technicality.
#
# THE BREAKDOWN IS THE PATH AXIS, AND SAYS SO. The shipped grade is
# worst(path_floor, provenance, reversibility) and only the PATH axis is per-file at all — the
# other two are properties of the PR, not of any file. So each row carries that file's PATH
# FLOOR, and whenever the headline tier is above the path floor the sentence names the axis that
# supplied it and quotes its reason. That is what keeps the sentence consistent with the headline
# it sits under: without it, a diff of nothing but docs can headline R2 (no green checks) directly
# above "all 40 changed lines are R0", and the explanation contradicts the tier it explains.
# The per-file floors come from grade-pr-risk.sh (`risk.axes.path_floor.files`), computed from
# the SAME rules as the floor itself, so this file re-derives nothing and cannot drift into a
# second grading model.
#
# ── THE INJECTION PATH THIS SCRIPT IS HARDENED AGAINST ─────────────────────────────────────────
# Paths come straight from the diff and git permits `|`, backticks and newlines in a filename.
# Rendered raw into a markdown table, such a path breaks out of its code span AND out of its row,
# which lets a PR author inject a pre-ticked `- [x] **This grade is wrong**` line that the NEXT
# re-grade reads back as a genuine reviewer dispute — corrupting the exact calibration signal the
# checkbox exists to collect — or a remote image that logs reviewer IPs. See `md_path` / `md_text`
# in the render program: newlines are flattened (nothing PR-controlled may ever START a line),
# pipes are backslash-escaped, and a path containing a backtick or a backslash falls back to
# fully-escaped plain text, because no inline-code span can quote a backtick reliably and a
# literal backslash in front of a pipe escapes the escape. The same treatment covers an `unknown`
# report's reason string, which carries the grader's own error text and therefore quotes
# PR-authored path names.
#
# ── AND AGAINST A BODY GITHUB WILL NOT ACCEPT ──────────────────────────────────────────────────
# GitHub rejects an issue-comment body over 65536 characters with a 422. The label write happens
# FIRST, so an over-long body would leave the label fresh and the comment stale on EVERY
# subsequent re-grade. So the body is bounded by construction: per-path (160) and per-reason
# (500) display caps, at most 50 rows, row-dropping by halving until it fits, and an
# unconditional backstop truncation that RE-APPENDS the footer so even a truncated body still
# carries the checkbox this script parses.
#
# ── THE STICKY MARKER IS ONE CONSTANT ──────────────────────────────────────────────────────────
# `STICKY_MARKER` is rendered as the body's first line AND is what `find_sticky` matches on. They
# live in this one file precisely so they cannot drift — if they ever did, every push would POST a
# NEW comment instead of updating the one that exists. tests/test_publish_risk_surfaces.sh pins
# the equality anyway, because "they are in the same file" is a property of today's structure.
#
# ── DISPUTE ROUND-TRIP ─────────────────────────────────────────────────────────────────────────
# The checkbox in the comment is the SOURCE OF TRUTH and the label follows it: ticking the box
# applies `risk-grade-disputed` on the next grade, and a re-grade re-renders the body with the box
# still ticked rather than silently un-ticking a registered disagreement. Un-ticking clears the
# label the same way. The reference implementation (the superseded PR #100) instead OR-ed the
# label back into the state, which made the label one-way: once set, no un-tick could ever clear
# it. Body-as-truth costs one narrow race — a tick landing between the read-back and the PATCH is
# overwritten, and the reviewer re-ticks — which is why the body is re-read by id IMMEDIATELY
# before the write rather than reused from the paginated scan.
#
# `risk-grade-disputed` (this script's) is DELIBERATELY DISTINCT from `risk-dispute` (the human
# convention the grader never touches). One is a machine-maintained mirror of a checkbox and is
# rewritten on every grade; the other is a human's own label and would be fought over if this
# script owned it.
#
# Inputs (env):
#   REPO             owner/name of the repo holding the PR                          (required)
#   PR_NUMBER        the PR number                                                  (required)
#   RECORD           path to the graded record from grade-pr-risk.sh                (required)
#                    `{}` / an unreadable record renders the UNKNOWN surfaces — never R0.
#   HEAD_SHA         head commit for the Check Run (read from the API when empty)
#   PUBLISH_CHECK    1 = create the Check Run                       (default 0)
#   PUBLISH_COMMENT  1 = create/update the sticky comment           (default 0)
#   CHECK_NAME       Check Run name        (default "PR risk (advisory)")
#   DISPUTE_LABEL    label mirroring the checkbox (default risk-grade-disputed)
#   STICKY_LOGINS    extra bot logins allowed to own our sticky comment (comma-separated)
#   DRY_RUN          1 = render and print the plan, write nothing
#   GH_TOKEN         token for gh — needs `checks: write` for the Check Run and
#                    `pull-requests: write` for the comment + dispute label
#
# Exit: 0 = published, or nothing to do, or a write failed and was ANNOUNCED (advisory).
#       2 = usage/setup error — nothing was attempted.
#
# Deliberately bash (shebang), not zsh — CI runners and the test suite both exercise bash.

set -uo pipefail

REPO="${REPO:-}"
PR_NUMBER="${PR_NUMBER:-}"
RECORD="${RECORD:-}"
HEAD_SHA="${HEAD_SHA:-}"
PUBLISH_CHECK="${PUBLISH_CHECK:-0}"
PUBLISH_COMMENT="${PUBLISH_COMMENT:-0}"
CHECK_NAME="${CHECK_NAME:-PR risk (advisory)}"
DISPUTE_LABEL="${DISPUTE_LABEL:-risk-grade-disputed}"
STICKY_LOGINS="${STICKY_LOGINS:-}"
DRY_RUN="${DRY_RUN:-0}"

# The one marker. Rendered as the body's FIRST line; matched by find_sticky.
STICKY_MARKER="<!-- ci-pr-risk -->"
# The checkbox label, and the two patterns that read its state back. All three describe ONE line
# of rendered markdown, so they are defined together.
DISPUTE_TEXT="**This grade is wrong**"
CHECKED_RE='^[[:space:]]*[-*][[:space:]]+\[[xX]\][[:space:]]+\*\*This grade is wrong\*\*'
COMMENT_MAX_CHARS=65000
# GitHub's own caps on the Check Run output fields.
CHECK_TITLE_MAX=255
CHECK_SUMMARY_MAX=65535
# Bounded scan for our own comment: at most this many pages of 100, walked from the LAST page
# BACKWARDS. The order matters and cannot be asked for: `GET /issues/{n}/comments` takes only
# `since`/`per_page`/`page` — `sort`/`direction` belong to the repo-wide `/issues/comments` route
# and are silently ignored here — so the listing is always OLDEST-FIRST. Scanning it forwards made
# the bound unrecoverable: on a very chatty PR the sticky sits past the cap, every re-grade POSTs a
# fresh one that ALSO lands past the cap, and each new body resets the reviewer's tick. Our sticky
# is the comment we posted, so walking back from the end finds it on the first page tried.
STICKY_MAX_PAGES="${STICKY_MAX_PAGES:-10}"

log()  { printf '[publish-risk-surfaces] %s\n' "$*" >&2; }
warn() { printf '::warning::[publish-risk-surfaces] %s\n' "$*" >&2; }
die()  { printf '[publish-risk-surfaces] ERROR %s\n' "$*" >&2; exit 2; }

# ---- the render program -------------------------------------------------------------------------
# PURE: record JSON in, `{check_title, check_summary, comment_body}` out. No network, no writes —
# which is what makes the escaping and the length bound unit-testable without stubbing GitHub.
render_program() {
cat <<'JQ'
    # --- markdown safety, applied to EVERYTHING that is or quotes PR-controlled text -----------
    # Newlines are flattened FIRST and unconditionally: nothing PR-controlled may ever start a
    # line of this comment, or it could forge the dispute checkbox the publisher reads back.
    def flat: tostring | gsub("[\r\n]"; " ");
    # Every ASCII punctuation character CommonMark lets you backslash-escape. Escaping only pipes
    # would leave `[`, `]`, `(`, `)`, `!` and `<` live — enough for an inline link, a remote image
    # that logs reviewer IPs, or raw HTML. Escaped punctuation renders as the bare character, so
    # ordinary text is unchanged on screen.
    def md_escape: gsub("(?<c>[\\\\`*_{}\\[\\]()#+.!<>|~-])"; "\\" + .c);
    def md_text($lim): flat | .[0:$lim] | md_escape;
    # Axis reasons are written as fragments, some with a trailing stop and some without, and the
    # headline concatenates one with a following sentence. Normalize so the seam never renders as
    # "…CI classes 14% of 350 lines…". Tested against an escaped stop too: md_escape turns "." into
    # "\.", whose LAST character is still ".", so this does not double up.
    def endstop: sub("[[:space:]]+$"; "") | if . == "" or test("[.!?;:]$") then . else . + "." end;
    # A path renders inside an inline code span when it safely can, and as fully-escaped plain
    # text when it cannot. A backtick cannot be quoted reliably inside a code span, and a literal
    # backslash in front of a pipe (`a\|b`) escapes the ESCAPE and hands the pipe back to the
    # table splitter — so either character forces the plain-text branch.
    def md_path:
      (flat | if length > 160 then .[0:160] + "…" else . end)
      | if test("[`\\\\]") then md_escape else "`" + gsub("\\|"; "\\|") + "`" end;

    def tier_rank: {"R0":0,"R1":1,"R2":2,"R3":3}[.] // 3;
    def pct($p; $w): if $w <= 0 then 0 else (100 * $p / $w) | round end;

    . as $r
    | ($r.risk // null) as $R
    | (($R.status // "unknown") == "ok" and ($R.tier // null) != null) as $graded
    | ($R.tier // null) as $tier
    | ($R.axes.path_floor // null) as $A1
    | ($R.axes.provenance // null) as $A2
    | ($R.axes.reversibility // null) as $A3
    | ($A1.tier // "R0") as $floor
    | ([$A1.files[]? | . + {lines: ((.additions // 0) + (.deletions // 0))}]) as $files
    | ([$files[] | .lines] | add // 0) as $total
    | ([$files[] | select((.tier | tier_rank) >= ($floor | tier_rank))]) as $topf
    | ([$topf[] | .lines] | add // 0) as $toplines

    # WHICH AXIS SUPPLIED THE HEADLINE. Without this the sentence can contradict the tier printed
    # directly above it, which is the one thing an explanation must never do. Defined BEFORE the
    # sentence because both of its conditional clauses key off it, from opposite sides: the
    # attribution tail speaks only when ANOTHER axis outranked the path floor, the reducibility
    # clause only when the path axis is what decided.
    | ([{a:"provenance", t:($A2.tier // null), r:($A2.reason // "")},
        {a:"reversibility", t:($A3.tier // null), r:($A3.reason // "")}]
       | map(select(.t != null and ($tier != null) and ((.t | tier_rank) == ($tier | tier_rank))))) as $drivers
    # THE PATH AXIS DECIDED: it is graded, no other axis ties or beats the headline, and the
    # headline IS the floor. A TIE deliberately reads as "not decided by path" — if provenance also
    # proposes R3, peeling the R3 files out changes nothing, and promising a reduction there would
    # be the one wrong answer. Same predicate the above-the-fold `$conc_short` already gates on.
    | ($graded and (($drivers | length) == 0)
       and (($tier | tier_rank) == ($floor | tier_rank))) as $path_decided
    # ---- the reducible remainder ---------------------------------------------------------------
    # The COMPLEMENT of $topf: the files strictly below the overall path floor — exactly the set the
    # sentence already counts as the below-floor share. Its worst per-file floor is what that set
    # would earn on the path axis as its own PR, and it is computed here by the same worst-of rule
    # the floor itself uses, over floors this script did not derive (grade-pr-risk.sh did). So it is
    # the real judge's answer, not an estimate of one — which is the whole point of printing it: the
    # number that separates "peel 2 files and the rest rubber-stamps" from "peel 2 files and the
    # rest is still a normal review" is not otherwise recoverable without re-grading by hand.
    | ([$files[] | select((.tier | tier_rank) < ($floor | tier_rank))]) as $below
    | (if ($below | length) > 0 then ([$below[] | .tier | tier_rank] | max) else null end) as $comp_rank

    # ---- the concentration sentence ----------------------------------------------------------
    # Built on the SHIPPED model (per-file PATH floors), never lifted from the superseded
    # per-file-tier model — that grader assigned every file an overall tier, which this one
    # deliberately does not do.
    | (if ($files | length) == 0
       then "No per-file breakdown is available for this diff."
       elif $total <= 0
       then "This diff changes no counted lines across \($files | length) file(s)."
       elif ($floor | tier_rank) == 0 or ($total - $toplines) <= 0
       then "All \($total) changed lines sit at \($floor) on the path axis."
       else "**\(pct($total - $toplines; $total))% of this diff is "
            + ([range(0; ($floor | tier_rank))] | map("R\(.)") | join("/"))
            + "**; the \(pct($toplines; $total))% that puts the path floor at \($floor) is "
            + "\($topf | length) file(s), \($toplines) lines"
            # A FLOOR WITH ITS ASSUMPTIONS NAMED, never a promised grade. The remainder's own
            # provenance and check rollup are unknowable at render time — a human-authored split
            # floors at R1 on provenance, and the future PR's checks have not run — so this says
            # what the PATH axis would say and states plainly what it cannot.
            + (if $path_decided and $comp_rank != null
               then "; peeled into their own PR, the remaining \($below | length) file(s) would "
                    + "path-floor at **R\($comp_rank)** (final grade still depends on provenance "
                    + "and checks at PR time)."
               else "." end)
       end) as $conc_core
    | (if $graded and (($tier | tier_rank) > ($floor | tier_rank)) and (($drivers | length) > 0)
       then " The headline tier is \($tier) rather than the path floor \($floor) because the "
            + ([$drivers[] | .a] | join(" and ")) + " axis proposes \($tier): "
            + ($drivers[0].r | md_text(300)) + "."
       else "" end) as $conc_tail
    | ($conc_core + $conc_tail) as $concentration

    # ---- the Check Run -----------------------------------------------------------------------
    | (if $graded then "Risk: \($tier)" else "Risk: unknown" end) as $check_title
    | (if $graded then
        ([ "**Tier: `\($tier)`** — advisory only.",
           "",
           "Grade = worst(path\\_floor, provenance, reversibility) — \($R.reason | md_text(500))",
           "",
           $concentration,
           "",
           "| axis | tier | reason |", "|---|---|---|" ]
         + [ ($R.axes // {}) | to_entries[]
             | "| \(.key) | \(.value.tier // "unknown") | \(.value.reason | md_text(400)) |" ]
         + [ "",
             "Map `\($R.map_version // "unknown")` · registry `\($R.registry_version // "unknown")` · \($files | length) file(s), \($total) counted lines.",
             "",
             "This check is advisory: its conclusion is always `neutral`, so it never fails, never blocks a merge, and no automation consumes the tier." ]
         | join("\n"))
       else
        ([ "**Tier: unknown** — this pull request could not be graded.",
           "",
           ($R.reason // "the graded record was empty or unreadable — nothing was graded" | md_text(1000)),
           "",
           "An ungradable PR is reported as `unknown`, never defaulted to `R0`: a PR whose inputs we could not read is exactly the PR that might touch auth. Push again or re-run the check to retry.",
           "",
           "This check is advisory: its conclusion is always `neutral`, so it never fails and never blocks a merge." ]
         | join("\n"))
       end) as $check_summary

    # ---- the sticky comment ------------------------------------------------------------------
    # DELIBERATELY ONE LINE ABOVE THE FOLD. This lands on PRs that already carry CodeRabbit and an
    # 8-cell review panel, so an advisory grade nothing routes on has no claim on vertical space:
    # the visible body is the tier, why, and at most one concentration clause. Everything else —
    # the formula, the per-axis reasons, the per-file table, the caveats — moves inside the
    # <details>. The Check Run keeps the long form; it is a surface nobody has to scroll past.
    | (if $disputed == "1" then "x" else " " end) as $box
    # ONE definition of the footer, and it is what the publisher's CHECKED_RE reads back. It is
    # re-appended by the truncation backstop too, so even a body that had to be cut still carries
    # the checkbox — a truncated body that lost it would read as "never disputed" forever. CHECKED_RE
    # anchors only through the bolded label, so the trailing half-sentence is free to be short.
    | ([ "",
         "- [\($box)] \($dispute_text) — tick if this tier is wrong; applies the `\($dispute_label)` label. Nothing is gated on it either way.",
         "",
         "<sub>Advisory · never fails a check or blocks a merge · re-grades on every push, updated in place.</sub>" ]) as $tail

    # WHICH AXIS TO NAME IN THE HEADLINE, and its human reason. `$R.reason` is deliberately NOT
    # used here: it is the machine trace ("worst of path_floor=R1, provenance=R2, ..."), which
    # restates the tier instead of explaining it. The formula and that trace both still appear
    # inside the <details>, so nothing is lost by leading with the axis that actually decided.
    | (if ($drivers | length) > 0
       then {n: ([$drivers[] | .a] | join(" and ")), r: ($drivers[0].r // "")}
       # $path_decided, not a second spelling of it: the reducibility clause in the sentence above
       # and this headline attribution must name the path axis on exactly the same records, or the
       # comment offers a split in the <details> under a headline crediting another axis.
       elif $path_decided
       then {n: "path", r: ($A1.reason // "")}
       else {n: null, r: ""} end) as $driver
    # The concentration clause, cut to a headline-sized fragment — and shown ONLY when the path
    # axis is what drove the tier. Quoting "14% of lines set the path floor at R1" under an R2
    # headline that provenance produced points the reader at the wrong number.
    | (if ($driver.n == "path") and ($files | length) > 0 and $total > 0
          and ($floor | tier_rank) > 0 and ($total - $toplines) > 0
       then " \(pct($toplines; $total))% of \($total) changed lines carry it (\($topf | length) file(s))."
       else "" end) as $conc_short
    | (if $graded then
         ([ $marker, "",
            # The whole visible comment: tier, the axis that decided it, and that axis's reason.
            "**Risk `\($tier)`** · advisory"
              + (if $driver.n != null and ($driver.r | length) > 0
                 then " — **\($driver.n)**: \($driver.r | md_text(220) | endstop)"
                 else " — \($R.reason | md_text(220) | endstop)" end)
              + $conc_short, "",
            "<details><summary>How this grade was reached</summary>", "",
            "Grade = worst(path\\_floor, provenance, reversibility).", "",
            $concentration, "",
            "| axis | tier | reason |", "|---|---|---|" ]
          + [ ($R.axes // {}) | to_entries[]
              | "| \(.key) | \(.value.tier // "unknown") | \(.value.reason | md_text(400)) |" ]
          + [ "",
              "Map `\($R.map_version // "unknown")` · registry `\($R.registry_version // "unknown")` · \($files | length) file(s), \($total) counted lines.", "",
              "Each row below is that file's PATH-AXIS floor — the tier its path alone earns. The headline is the worst of the path floor, provenance and reversibility, so a file's row is a contribution, not its own overall grade.", "",
              "| file | +/- | path tier | matched classes |", "|---|---|---|---|" ])
       else
         [ $marker, "",
           "**Risk `unknown`** · advisory — this pull request could not be graded: \($R.reason // "the graded record was empty or unreadable" | md_text(240)). An ungradable PR is never defaulted to `R0`; push again or re-run to retry." ]
       end) as $head
    | ([ $files[] ] | sort_by([ -(.tier | tier_rank), -(.lines) ])) as $shown
    | ([ $shown[0:50][]
         | "| \(.path | md_path) | +\(.additions // 0)/-\(.deletions // 0) | \(.tier) | "
           + ((.classes // []) | join(", ") | if . == "" then "—" else md_text(160) end) + " |" ]) as $rows

    # Assemble at N rows. Dropping rows is what bounds the graded branch; the per-path and
    # per-reason caps already keep 50 rows well inside the limit, so this is the property that
    # makes "the comment always fits" true rather than estimated.
    | def assemble($n):
        ($rows[0:$n]) as $kept
        | (($files | length) - $n) as $omitted
        | (if $omitted > 0 then $kept + ["| _…and \($omitted) more file(s)_ | | | |"] else $kept end) as $kept2
        | (($head + $kept2 + ["", "</details>"] + $tail) | join("\n")) + "\n";
      (if $graded then
         ({n: ($rows | length)} | .body = assemble(.n)
          | until(.n == 0 or ((.body | length) <= $max);
                  .n = (.n / 2 | floor) | .body = assemble(.n))
          | .body)
       else (($head + $tail) | join("\n")) + "\n" end) as $body0
    # UNCONDITIONAL BACKSTOP. Row-dropping bounds the graded branch and everything else is
    # length-capped, so this should never fire — but "the comment always fits" has to hold on
    # every branch or the 422 it exists to prevent returns through whichever one the estimate
    # missed, and a 422 leaves the label fresh and the comment stale on every future re-grade.
    # The cut lands mid-table, INSIDE the `<details>` the graded branch opened, so the footer has
    # to close it — otherwise the dispute checkbox this script parses back renders swallowed by
    # the collapsed section instead of below it. The ungraded branch never opens one.
    | (if ($body0 | length) <= $max then $body0
       else (("\n" + (["", "_(truncated — the full grade is in the Check Run and the step summary.)_"]
                      + (if $graded then ["", "</details>"] else [] end) + $tail | join("\n")))) as $footer
            | ($body0[0:($max - ($footer | length) - 1)] + $footer + "\n")
       end) as $comment_body

    | {check_title: $check_title, check_summary: $check_summary, comment_body: $comment_body,
       concentration: $concentration, tier: (if $graded then $tier else "unknown" end),
       graded: $graded}
JQ
}

# render_surfaces <record-file> <disputed 0|1> -> the surfaces object on stdout
# An unreadable or non-JSON record renders the UNKNOWN surfaces rather than failing: the caller
# already decided to publish, and "we could not read our own record" is a verdict worth showing.
render_surfaces() {
  local rec="$1" disputed="${2:-0}" raw
  raw="$(cat "$rec" 2>/dev/null)"
  jq -e . >/dev/null 2>&1 <<<"$raw" || raw='{}'
  jq -c --arg marker "$STICKY_MARKER" --arg disputed "$disputed" \
        --arg dispute_text "$DISPUTE_TEXT" --arg dispute_label "$DISPUTE_LABEL" \
        --argjson max "$COMMENT_MAX_CHARS" "$(render_program)" <<<"$raw"
}

# ---- the writes ----------------------------------------------------------------------------------
ERRF=""
init_scratch() {
  [ -z "$ERRF" ] || return 0
  ERRF="$(mktemp "${TMPDIR:-/tmp}/publish-risk-err.XXXXXX")" || die "mktemp failed"
  [ "${PRS_DIRECT:-0}" = 1 ] && trap 'rm -f "$ERRF"' EXIT
  return 0
}
gherr() { [ -n "$ERRF" ] && [ -f "$ERRF" ] || return 0; tr '\n' ' ' < "$ERRF" | sed 's/[[:space:]]*$//'; }
ghq()   { init_scratch; gh "$@" 2>"$ERRF"; }
enc()   { jq -rn --arg s "$1" '$s | @uri'; }

# The logins allowed to own our sticky comment. Bot TYPE alone is not identity: every other
# GitHub App installed on the repo is also a Bot, and adopting one would mean PATCHing over its
# body on every grade (or 403ing forever) while reading the dispute state out of a foreign
# comment. Matching the marker alone is worse still — the marker is public, so a PR author could
# pre-post a comment carrying it and thereafter control the preserved checkbox state.
allowed_logins() {
  printf '%s\n' "github-actions[bot]"
  printf '%s' "$STICKY_LOGINS" | tr ',' '\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep . || true
}

# sticky_last_page -> the highest page number of the comment listing (1 when there is only one).
# Read from the Link header rather than assumed, because the listing has no way to ask for
# newest-first and the sticky is always at the END of it.
sticky_last_page() {
  local hdr n
  # `-i --silent` = headers only: this call exists purely for `Link: …rel="last"`.
  hdr="$(ghq api -i --silent "repos/$REPO/issues/$PR_NUMBER/comments?per_page=100&page=1")" || return 1
  n="$(printf '%s' "$hdr" | tr -d '\r' | grep -i '^link:' \
       | tr ',' '\n' | sed -n 's/.*[?&]page=\([0-9][0-9]*\)>[[:space:]]*;[[:space:]]*rel="last".*/\1/p' | head -n 1)"
  case "$n" in ''|*[!0-9]*) n=1 ;; esac
  printf '%s' "$n"
}

# find_sticky -> the comment id on stdout (empty when there is none), rc 1 on a read failure.
# rc 1 means WE DO NOT KNOW whether a sticky exists — the caller must not fall through to the
# create branch on it, or every transient list failure POSTs another comment with the tick reset.
find_sticky() {
  local page last floor body allow id
  allow="$(allowed_logins | jq -Rsc 'split("\n") | map(select(length > 0))')"
  last="$(sticky_last_page)" \
    || { warn "could not list comments on $REPO#$PR_NUMBER: $(gherr)"; return 1; }
  page="$last"
  floor=$(( last - STICKY_MAX_PAGES + 1 )); [ "$floor" -ge 1 ] || floor=1
  while [ "$page" -ge "$floor" ]; do
    body="$(ghq api "repos/$REPO/issues/$PR_NUMBER/comments?per_page=100&page=${page}")" \
      || { warn "could not list comments on $REPO#$PR_NUMBER: $(gherr)"; return 1; }
    # Our body is rendered with the marker as its FIRST line. A bot that QUOTES the sticky
    # comment carries the marker too, nested inside its own prose — which login matching alone
    # cannot see, because every other GITHUB_TOKEN workflow in the repo also posts as
    # github-actions[bot].
    id="$(jq -r --argjson allow "$allow" --arg m "$STICKY_MARKER" '
            [ .[] | . as $c
                  | select((.user.type // "") == "Bot")
                  # $allow is the input INSIDE this pipe, so the login has to come from the
                  # captured comment — `.user.login` here would index the allow-list array.
                  | select(($allow | index($c.user.login // "")) != null)
                  | select(((.body // "") | startswith($m)))
                  | .id ] | last // empty' <<<"$body")" || return 1
    [ -z "$id" ] || { printf '%s' "$id"; return 0; }
    page=$(( page - 1 ))
  done
  return 0
}

# comment_is_disputed <comment-id> -> rc 0 = ticked, rc 1 = definitely unticked, rc 2 = UNKNOWN.
# Re-read by id IMMEDIATELY before the write rather than reused from the paginated scan: that
# does not make the update atomic (GitHub offers no conditional comment update) but it shrinks
# the window in which a reviewer's tick can be overwritten from the whole scan to one round trip.
#
# THE THIRD OUTCOME IS THE POINT. Folding a failed re-read into "unticked" is not a conservative
# default here, it is destructive: the body would be PATCHed back to `- [ ]` and the dispute label
# actively REMOVED, permanently erasing a registered disagreement on nothing worse than a 500 —
# the exact outcome this file's header promises cannot happen. On rc 2 the caller writes nothing.
comment_is_disputed() {
  local id="$1" body
  body="$(ghq api "repos/$REPO/issues/comments/${id}" --jq '.body // ""')" || {
    warn "could not re-read comment ${id} on $REPO#$PR_NUMBER: $(gherr) — the dispute state is UNKNOWN, so the comment and the label are left exactly as they are"
    return 2
  }
  grep -Eq "$CHECKED_RE" <<<"$body"
}

publish_check_run() {
  local title="$1" summary="$2" sha="$3"
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY RUN — would create Check Run '$CHECK_NAME' (neutral) on ${sha} titled '${title}'"
    return 0
  fi
  # `conclusion: neutral` is hardcoded and must stay that way: a neutral check cannot fail a PR
  # even if a repo later marks this check required, which is the entire "nothing is gated"
  # guarantee. There is no branch of this script that can emit success/failure.
  jq -n --arg name "$CHECK_NAME" --arg sha "$sha" --arg t "${title:0:$CHECK_TITLE_MAX}" \
        --arg s "${summary:0:$CHECK_SUMMARY_MAX}" \
     '{name:$name, head_sha:$sha, status:"completed", conclusion:"neutral",
       output:{title:$t, summary:$s}}' \
    | ghq api -X POST "repos/$REPO/check-runs" --input - >/dev/null \
    || { warn "could not create the Check Run on $REPO@${sha}: $(gherr) — the grade is unaffected; the label and the step summary still carry it"; return 1; }
  log "created Check Run '$CHECK_NAME' on ${sha}"
}

sync_dispute_label() {
  local disputed="$1" have=0 current
  current="$(ghq api --paginate "repos/$REPO/issues/$PR_NUMBER/labels?per_page=100" --jq '.[].name')" \
    || { warn "could not read labels on $REPO#$PR_NUMBER: $(gherr) — the dispute label was not synced"; return 1; }
  grep -qxF "$DISPUTE_LABEL" <<<"$current" && have=1
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY RUN — dispute=${disputed}, label present=${have}"
    return 0
  fi
  if [ "$disputed" = 1 ] && [ "$have" = 0 ]; then
    ghq api -X POST "repos/$REPO/issues/$PR_NUMBER/labels" -f "labels[]=$DISPUTE_LABEL" >/dev/null \
      || { warn "could not add '$DISPUTE_LABEL' to $REPO#$PR_NUMBER: $(gherr)"; return 1; }
    log "added '$DISPUTE_LABEL'"
  elif [ "$disputed" = 0 ] && [ "$have" = 1 ]; then
    # A 404 is the desired state reached by someone else, not a failure — same contract as the
    # stale-label removal in apply-risk-label.sh.
    ghq api -X DELETE "repos/$REPO/issues/$PR_NUMBER/labels/$(enc "$DISPUTE_LABEL")" >/dev/null \
      || case "$(gherr)" in
           *"(HTTP 404)"*) log "'$DISPUTE_LABEL' was already gone (404)" ;;
           *) warn "could not remove '$DISPUTE_LABEL' from $REPO#$PR_NUMBER: $(gherr)"; return 1 ;;
         esac
    log "removed '$DISPUTE_LABEL'"
  fi
}

# ---- main ----------------------------------------------------------------------------------------
main() {
  [ -n "$RECORD" ] || die "RECORD is required"
  command -v jq >/dev/null 2>&1 || die "jq not found on PATH"

  # RENDER-ONLY: emit the surfaces object and write nothing. This is how the Check Run gets
  # rendered in the GRADING job (which holds no `checks: write`) and POSTED from a separate job
  # that does — see pr-risk.yml's `publish-check`. All of the PR-controlled text is escaped
  # HERE, in the job that never holds the elevated token, so the job that does never renders
  # anything it read from a pull request.
  if [ "${RENDER_ONLY:-0}" = 1 ]; then
    render_surfaces "$RECORD" "${RENDER_DISPUTED:-0}" || die "the surfaces could not be rendered from $RECORD"
    exit 0
  fi

  [ "$PUBLISH_CHECK" = 1 ] || [ "$PUBLISH_COMMENT" = 1 ] || { log "both surfaces are off — nothing to publish"; exit 0; }
  [ -n "$REPO" ] || die "REPO is required"
  [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "bad REPO '$REPO' (want owner/name)"
  [[ "$PR_NUMBER" =~ ^[0-9]+$ ]] || die "bad PR_NUMBER '$PR_NUMBER'"
  [ "$DRY_RUN" = 1 ] || command -v gh >/dev/null 2>&1 || die "gh not found on PATH"

  # ONCE, HERE — not lazily inside `ghq`. Most `ghq` calls sit inside a command substitution
  # (`id="$(find_sticky)"`, `current="$(ghq api …)"`), so a lazy init sets ERRF in the SUBSHELL and
  # the enclosing scope keeps the empty default. Every `gherr` in the failure warnings then
  # interpolated nothing, hiding whether a failure was auth, throttling or transport — on exactly
  # the warnings an operator has to act on.
  init_scratch

  local rc=0

  if [ "$PUBLISH_COMMENT" = 1 ]; then
    local id="" disputed=0 surfaces body find_rc=0 dis_rc=0
    # A READ FAILURE IS NOT "THERE IS NO COMMENT". Both reads below are load-bearing: falling
    # through on a failed LIST posts a second sticky (and every later failure another), and
    # falling through on a failed RE-READ rewrites the body with the box cleared and strips the
    # dispute label. Either turns a transient 5xx into permanent damage to the one signal this
    # surface exists to collect, so an unknown state means WRITE NOTHING and say so.
    id="$(find_sticky)" || find_rc=1
    if [ "$find_rc" -ne 0 ]; then
      warn "the sticky comment could not be looked up on $REPO#$PR_NUMBER — nothing was written (a blind create would post a DUPLICATE and reset the dispute checkbox)"
      rc=1
    else
      if [ -n "$id" ]; then
        comment_is_disputed "$id"; dis_rc=$?
        [ "$dis_rc" -eq 0 ] && disputed=1
      fi
      if [ "$dis_rc" -eq 2 ]; then
        rc=1   # already announced inside comment_is_disputed; the comment and label stay untouched
      else
        surfaces="$(render_surfaces "$RECORD" "$disputed")" || { warn "the surfaces could not be rendered"; surfaces=""; }
        if [ -z "$surfaces" ]; then
          rc=1
        else
          body="$(jq -r '.comment_body' <<<"$surfaces")"
          if [ "$DRY_RUN" = 1 ]; then
            log "DRY RUN — would $( [ -n "$id" ] && echo "update comment $id" || echo "create a comment" ) (disputed=${disputed})"
            printf '%s' "$body"
          elif [ -n "$id" ]; then
            # The success line lives in the `else`: logging it unconditionally made a 403 print a
            # warning AND "updated the sticky comment", so the run log contradicted itself for
            # whoever was diagnosing a permanently stale comment.
            if jq -n --arg b "$body" '{body:$b}' \
                 | ghq api -X PATCH "repos/$REPO/issues/comments/${id}" --input - >/dev/null
            then log "updated the sticky comment (${id})"
            else warn "could not update the sticky comment ${id} on $REPO#$PR_NUMBER: $(gherr)"; rc=1; fi
          else
            if jq -n --arg b "$body" '{body:$b}' \
                 | ghq api -X POST "repos/$REPO/issues/$PR_NUMBER/comments" --input - >/dev/null
            then log "created the sticky comment"
            else warn "could not create the sticky comment on $REPO#$PR_NUMBER: $(gherr)"; rc=1; fi
          fi
        fi
        sync_dispute_label "$disputed" || rc=1
      fi
    fi
  fi

  if [ "$PUBLISH_CHECK" = 1 ]; then
    local sha="$HEAD_SHA" surfaces2
    # THE GRADED COMMIT, NOT "HEAD NOW". The record carries the oid whose rollup and file list
    # produced this tier, and grading deliberately waits out the rollup settle (up to
    # `wait_for_checks_minutes`) — so re-reading head at publish time would stamp the PREVIOUS
    # commit's grade onto a new head as an immutable, timestamped Check Run, in the artifact whose
    # whole value is being a trustworthy commit-attached record. A push during that window gets
    # its own grade and its own check; this one stays attached to what it actually graded.
    if [ -z "$sha" ]; then
      sha="$(jq -r '.head_sha // empty' "$RECORD" 2>/dev/null)"
      [ -n "$sha" ] && log "attaching the Check Run to the GRADED commit ${sha}"
    fi
    # Only a record with no head_sha (an ungradable PR, or a pre-schema record) falls back to the
    # API — an ungraded `unknown` verdict is about the PR, not about a particular commit.
    if [ -z "$sha" ] && [ "$DRY_RUN" != 1 ]; then
      sha="$(ghq api "repos/$REPO/pulls/$PR_NUMBER" --jq '.head.sha')" \
        || { warn "could not read the head sha of $REPO#$PR_NUMBER: $(gherr) — no Check Run was created"; sha=""; rc=1; }
    fi
    if [ -z "$sha" ]; then
      [ "$DRY_RUN" = 1 ] && sha="(head sha)"
    fi
    if [ -n "$sha" ]; then
      surfaces2="$(render_surfaces "$RECORD" 0)"
      if [ -z "$surfaces2" ]; then
        warn "the surfaces could not be rendered — no Check Run was created"; rc=1
      else
        publish_check_run "$(jq -r '.check_title' <<<"$surfaces2")" \
                          "$(jq -r '.check_summary' <<<"$surfaces2")" "$sha" || rc=1
      fi
    fi
  fi

  # ALWAYS 0. A publish surface that could redden a PR would quietly turn an advisory grade into a
  # gate; every failure above has already been announced as a warning annotation.
  [ "$rc" -eq 0 ] || log "one or more publish surfaces failed — announced above; exiting 0 because this grade is advisory"
  exit 0
}

# Sourceable without side effects (the suite sources this file to drive the render program and the
# helpers directly); only a direct invocation runs main or installs the EXIT trap.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  PRS_DIRECT=1
  main "$@"
fi
