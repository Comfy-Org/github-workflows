#!/usr/bin/env bash
# collect-pr-inputs.sh — assemble everything plan-derisk.sh needs to plan a split, and nothing else.
#
# `/derisk` arrives with no risk grade attached: the comment event carries a PR number and that is
# all. So this step re-grades the PR with the SAME grader, the SAME per-repo overrides read from
# the SAME base ref that pr-risk grades against, and hands the record on. Reading a stale label
# instead would plan against a tier nobody recomputed, on a PR that may have been pushed to since.
#
# THE OVERRIDE READ IS NOT REIMPLEMENTED HERE. `grade-targets.sh` is sourced for `resolve_base_ref`
# and `fetch_override` — it is written to be sourceable without side effects for exactly this — so
# the rules that judge a split are resolved by the one implementation that resolves the rules that
# judged the PR. Two copies of "which branch's .github/risk.json applies" is how a split plan ends
# up graded against a different map than the grade it claims to reduce.
#
# THE DIFF IS CAPPED, AND AN OVER-BUDGET DIFF IS AN OUTCOME, NOT AN ERROR. A 30k-line PR is exactly
# the PR a split plan would help most and exactly the one a single model call cannot read, so it
# takes the deterministic fallback: `oversized=true` on stdout, no diff file, and plan-derisk.sh
# renders the "too large to plan" comment. Truncating the diff mid-hunk and planning off the half
# that fit would produce a partition that silently ignores files it never saw.
#
# Inputs (env):
#   REPO             owner/name                                                     (required)
#   PR_NUMBER        the PR number                                                  (required)
#   TOOL_DIR         directory holding the pr-risk scripts  (default ../pr-risk beside us)
#   OUT_DIR          where to write record.json / diff.patch (default the cwd)
#   MAX_DIFF_BYTES   diff budget                                                    (default 200000)
#   MAP_PATH         consumer map override path        (default .github/risk.json)
#   RB_PATH          consumer registry override path   (default .github/risk-runbooks.json)
#   FLEET_LOGINS / BOT_LOGINS   forwarded to the grader, same meaning as in pr-risk.yml
#   GH_TOKEN         token for gh — needs `contents: read` + `pull-requests: read` + `checks: read`
#
# Output (GITHUB_OUTPUT when set, and stdout always): `record=`, `diff=`, `oversized=`, `map=`,
# `runbooks=`, `tier=`.
#
# Exit: 0 = inputs collected (including the deliberate oversized/ungradable outcomes).
#       2 = usage/setup error, or the PR could not be read at all.
#
# Deliberately bash (shebang), not zsh — CI runners and the test suite both exercise bash.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO="${REPO:-}"
PR_NUMBER="${PR_NUMBER:-}"
TOOL_DIR="${TOOL_DIR:-$SELF_DIR/../pr-risk}"
OUT_DIR="${OUT_DIR:-.}"
MAX_DIFF_BYTES="${MAX_DIFF_BYTES:-200000}"

# DEFINED TWICE, here and again after the `source` below, and both copies are reachable: these
# serve the argument checks immediately following, which run BEFORE grade-targets.sh is sourced.
# The linter sees only the later definitions shadowing these and calls them dead (SC2329) — its
# static view has no notion of "the redefinition happens partway down the file", which is the whole
# reason the second copy exists. Deleting either one is a real regression, in opposite directions.
# (A comment line may not START with the linter's own name, or it is parsed as a directive.)
# shellcheck disable=SC2329
log()  { printf '[collect-pr-inputs] %s\n' "$*" >&2; }
# shellcheck disable=SC2329
warn() { printf '::warning::[collect-pr-inputs] %s\n' "$*" >&2; }
die()  { printf '[collect-pr-inputs] ERROR %s\n' "$*" >&2; exit 2; }

[ -n "$REPO" ] || die "REPO is required"
[ -n "$PR_NUMBER" ] || die "PR_NUMBER is required"
case "$PR_NUMBER" in ''|*[!0-9]*) die "PR_NUMBER must be a positive integer" ;; esac
case "$MAX_DIFF_BYTES" in ''|*[!0-9]*) MAX_DIFF_BYTES=200000 ;; esac
[ -d "$TOOL_DIR" ] || die "TOOL_DIR '$TOOL_DIR' not found — the pr-risk grader is what computes every floor"
mkdir -p "$OUT_DIR" || die "could not create OUT_DIR '$OUT_DIR'"

# Sourced for `resolve_base_ref` / `fetch_override` / `retry_read`. It reads its configuration from
# the environment at source time, so REPO and the override paths are already set above. It installs
# no traps and creates no scratch files when sourced (see its GT_DIRECT guard), which is what makes
# this safe rather than merely convenient.
# shellcheck source=/dev/null
source "$TOOL_DIR/grade-targets.sh" || die "could not source grade-targets.sh from '$TOOL_DIR'"
# RE-DECLARED AFTER THE SOURCE, deliberately. grade-targets.sh defines its own `log`/`die` at file
# scope, so sourcing it silently replaced ours — and every diagnostic below then went out under a
# `[grade-targets]` prefix naming a script that did not emit it, which is precisely the wrong
# answer to "which step failed?" in a public run log. Redefining costs nothing and keeps the
# attribution honest. (`warn` has no counterpart there; it is repeated for symmetry, so a future
# helper appearing in that file cannot quietly capture it either.)
log()  { printf '[collect-pr-inputs] %s\n' "$*" >&2; }
warn() { printf '::warning::[collect-pr-inputs] %s\n' "$*" >&2; }
die()  { printf '[collect-pr-inputs] ERROR %s\n' "$*" >&2; exit 2; }

emit() { # <key> <value>
  printf '%s=%s\n' "$1" "$2"
  [ -z "${GITHUB_OUTPUT:-}" ] || printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"
}

base_ref="$(resolve_base_ref "$PR_NUMBER")" || die "the base ref of ${REPO}#${PR_NUMBER} could not be resolved — the rules that judge a split come from that branch, so there is nothing to grade against"

map_override="$(fetch_override "$MAP_PATH" "$OUT_DIR/risk-override.json" "$base_ref")" \
  || die "the risk map override could not be read from '${base_ref}'"
rb_override="$(fetch_override "$RB_PATH" "$OUT_DIR/runbooks-override.json" "$base_ref")" \
  || die "the runbook registry override could not be read from '${base_ref}'"

RECORD="$OUT_DIR/record.json"
args=( --repo "$REPO" --pr "$PR_NUMBER" )
[ -z "$map_override" ] || args+=( --map "$map_override" )
[ -z "$rb_override" ]  || args+=( --runbooks "$rb_override" )
[ -z "$FLEET_LOGINS" ] || args+=( --fleet-logins "$FLEET_LOGINS" )
[ -z "$BOT_LOGINS" ]   || args+=( --bot-logins "$BOT_LOGINS" )
# NO --self-run-id / --self-context. Unlike a pr-risk grading run, this run's own check is not on
# the PR's head commit (a `/derisk` run is triggered by an issue_comment, so its check attaches to
# the default-branch ref), so there is nothing of ours in the rollup to exclude — and excluding by
# name would hide a genuinely failing check of the same name.
bash "$TOOL_DIR/grade-pr-risk.sh" "${args[@]}" > "$RECORD" 2>"$OUT_DIR/grader-err.txt"
grc=$?
# rc 1 is the deliberate `unknown` grade and IS a record — plan-derisk.sh turns it into the honest
# "not graded, nothing to plan against" comment. rc 3 means nothing was graded at all.
if [ "$grc" -ge 2 ] || [ ! -s "$RECORD" ]; then
  warn "the PR could not be graded: $(head -c 400 "$OUT_DIR/grader-err.txt" | tr '\n' ' ')"
  die "${REPO}#${PR_NUMBER} could not be graded, so there is no risk concentration to plan against"
fi
tier="$(jq -r '.risk.tier // "unknown"' "$RECORD" 2>/dev/null)"

# ---- the diff -------------------------------------------------------------------------------------
# `Accept: application/vnd.github.diff` on the PR itself, not `pulls/{n}/files`: the planner needs
# to read the CHANGE to judge whether a step is genuinely inert, and the files endpoint gives it
# patches per file with its own 3000-file ceiling and no whole-PR context.
DIFF="$OUT_DIR/diff.patch"
oversized=false
if ! gh api "repos/$REPO/pulls/$PR_NUMBER" -H "Accept: application/vnd.github.diff" > "$DIFF" 2>"$OUT_DIR/diff-err.txt"; then
  warn "could not read the diff of ${REPO}#${PR_NUMBER}: $(head -c 300 "$OUT_DIR/diff-err.txt" | tr '\n' ' ')"
  rm -f "$DIFF"
  oversized=true
else
  bytes="$(wc -c < "$DIFF" | tr -d ' ')"
  if [ "$bytes" -gt "$MAX_DIFF_BYTES" ]; then
    log "diff is ${bytes} bytes, over the ${MAX_DIFF_BYTES}-byte budget — taking the deterministic fallback"
    rm -f "$DIFF"
    oversized=true
  fi
fi

emit record "$RECORD"
emit diff "$( [ "$oversized" = true ] && printf '' || printf '%s' "$DIFF" )"
emit oversized "$oversized"
emit map "$map_override"
emit runbooks "$rb_override"
emit tier "$tier"
