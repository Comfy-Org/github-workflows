#!/usr/bin/env bash
# grade-risk.sh — the risk grader's CLI entrypoint.
#
# Resolves a pull request's net diff and grades it into a risk tier, writing
# three artifacts into --out-dir:
#
#   risk-report.json  machine-readable report (publish_risk.py consumes this)
#   risk-comment.md   the sticky PR comment body
#   risk-check.md     the Check Run title + summary
#
# Usage:
#   grade-risk.sh --base <sha> --head <sha> --out-dir <dir> [--repo-dir <dir>]
#
# This is the single entrypoint for BOTH callers: pr-risk.yml in CI, and an
# offline backfill over merged history (BE-5507). It is loaded at run time from
# a pinned ref of Comfy-Org/github-workflows — never from the PR's own
# checkout — so a pull request cannot rewrite the logic grading it.
#
# It ALWAYS exits 0. A grader that reddens a check would gate a PR, and nothing
# here is allowed to gate. When the diff cannot be read the report is written
# with status "unknown" — never defaulted to R0.

set -uo pipefail

BASE=""
HEAD=""
OUT_DIR=""
REPO_DIR="."
# Empty means "use grade_risk.DEFAULT_MARKER". The marker must match
# publish_risk.MARKER or a re-grade posts a second comment instead of updating
# the sticky one, so it is defined ONCE in Python and never duplicated here.
MARKER=""

# `set -e` is deliberately off (this script must reach its own exit paths), so a
# bare `shift 2` on a trailing flag would FAIL without consuming anything and
# spin the loop below forever, spamming "shift count out of range" into the step
# summary until the job times out. Every value-taking arm checks first.
need_value() {
  if [ "$#" -lt 2 ]; then
    echo "grade-risk.sh: $1 requires a value" >&2
    exit 2
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --base) need_value "$@"; BASE="$2"; shift 2 ;;
    --head) need_value "$@"; HEAD="$2"; shift 2 ;;
    --out-dir) need_value "$@"; OUT_DIR="$2"; shift 2 ;;
    --repo-dir) need_value "$@"; REPO_DIR="$2"; shift 2 ;;
    --marker) need_value "$@"; MARKER="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "grade-risk.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$OUT_DIR" ]; then
  echo "grade-risk.sh: --out-dir is required" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRADER="${SCRIPT_DIR}/grade_risk.py"
PYTHON="${PYTHON:-python3}"

mkdir -p "$OUT_DIR"

# Emit an "unknown" report and exit 0. Used for every failure path below, so an
# ungradable PR is published as explicitly unknown rather than silently skipped
# or defaulted to the safest tier.
emit_unknown() {
  local reason="$1"
  echo "grade-risk.sh: ${reason}" >&2
  # `-I` (isolated) is load-bearing, not tidiness: reading the program from
  # stdin puts '' — the process CWD, which in the `grade` job is the PR's own
  # checkout — at the front of sys.path, ahead of the `sys.path.insert` below
  # and ahead of `import json, os, sys`. Without it a PR shipping a top-level
  # `json.py` executes its own code the first time any unknown path is hit,
  # breaking the "no PR-authored code runs in this job" boundary and letting the
  # PR author the report the privileged publish job consumes. Running the grader
  # from a FILE (below) is unaffected: there sys.path[0] is the script's own
  # pinned directory.
  if ! REASON="$reason" MARKER="$MARKER" OUT_DIR="$OUT_DIR" \
       ATTR_DEGRADED="${ATTR_DEGRADED:-}" "$PYTHON" -I - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["GRADER_DIR"])
import grade_risk

report = grade_risk.unknown_report(os.environ["REASON"])
report["attr_source_degraded"] = bool(os.environ.get("ATTR_DEGRADED"))
marker = os.environ.get("MARKER") or grade_risk.DEFAULT_MARKER
out = os.environ["OUT_DIR"]
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "risk-report.json"), "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2)
    fh.write("\n")
with open(os.path.join(out, "risk-comment.md"), "w", encoding="utf-8") as fh:
    fh.write(grade_risk.render_comment(report, marker))
title, summary = grade_risk.render_check(report)
with open(os.path.join(out, "risk-check.md"), "w", encoding="utf-8") as fh:
    fh.write(f"{title}\n\n{summary}\n")
print(f"{title}\n\n{summary}")
PY
  then
    # Last-resort fallback: even the Python renderer is unavailable. Write a
    # minimal but well-formed report so the publish job still has something to
    # publish as unknown, instead of the run vanishing silently.
    #
    # `reason` carries git's stderr, which quotes PR-authored path names, so it
    # is sanitised twice over.
    #
    # For JSON: reduced to printable ASCII, then the two structural characters
    # are backslash-escaped (backslashes FIRST, or the escape of a quote would
    # itself be escaped). Dropping non-ASCII before `cut` is what makes the
    # length bound safe — `cut -c` is byte-oriented in the C locale, so
    # truncating a UTF-8 path at byte 500 could sever a multi-byte character
    # and leave a report the publisher cannot even decode.
    #
    # For markdown: a stricter whitelist still, because nothing PR-influenced
    # may introduce a `[`, a backtick, a `<` or a newline into this
    # bot-authored comment — a forged `- [x] **This grade is wrong**` line
    # would read back as a genuine reviewer dispute.
    local json_reason md_reason
    json_reason=$(printf '%s' "$reason" | tr -c '\040-\176' ' ' | cut -c1-500 \
      | sed 's/\\/\\\\/g; s/"/\\"/g')
    md_reason=$(printf '%s' "$reason" | tr -c 'A-Za-z0-9 ._/:-' ' ' | cut -c1-500)
    printf '{"schema":1,"status":"unknown","tier":null,"label":null,"reason":"%s","total_lines":0,"tier_lines":{},"files":[],"top_tier_files":[],"attr_source_degraded":%s}\n' \
      "$json_reason" "$([ -n "${ATTR_DEGRADED:-}" ] && echo true || echo false)" \
      > "${OUT_DIR}/risk-report.json"
    # The one place the marker and the dispute checkbox are repeated: by
    # definition Python is unusable here, so grade_risk.DEFAULT_MARKER and its
    # rendered footer cannot be read. The checkbox line MUST stay in the form
    # publish_risk.UNCHECKED_RE matches — this body overwrites the sticky
    # comment, and without a box for `upsert_sticky` to re-tick, a registered
    # dispute is silently discarded and `risk-grade-disputed` becomes
    # unclearable. A unit test asserts the two forms stay in step.
    # shellcheck disable=SC2016  # the backticks are markdown, not substitution
    printf '%s\n\n## ⚪ Risk: **unknown**\n\nThe risk grader could not run: %s\n\nNo `risk:*` label was applied. Push again to re-grade.\n\n- [ ] **This grade is wrong** — tick this box if the tier above is off. Nothing is gated on it either way.\n\n<sub>Advisory only — this check never fails and never blocks merge.</sub>\n' \
      "${MARKER:-<!-- ci-pr-risk -->}" "$md_reason" > "${OUT_DIR}/risk-comment.md"
    printf 'Risk: unknown\n\nThe risk grader could not run: %s\n' \
      "$md_reason" > "${OUT_DIR}/risk-check.md"
  fi
  exit 0
}
export GRADER_DIR="$SCRIPT_DIR"

if [ ! -f "$GRADER" ]; then
  emit_unknown "grader not found at ${GRADER}"
fi
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  emit_unknown "${PYTHON} is not available on this runner"
fi
if [ -z "$BASE" ] || [ -z "$HEAD" ]; then
  emit_unknown "--base and --head are both required to resolve the diff"
fi

# Three-dot diff: the PR's NET changes (merge-base of base and head, to head),
# so commits landed on the base branch since the PR opened are not counted
# against it. `-z` keeps paths with spaces/newlines intact. stdout and stderr
# go to separate files: git's diagnostics must never be spliced into the
# numstat stream the grader parses.
#
# `--attr-source` reads .gitattributes from the BASE ref rather than from the
# working tree, which here is the PR's own head. Without it a PR that adds
# `* -diff` (or marks paths `binary`) makes git emit `-` counts for every file:
# every `changed` becomes 0, and both FILE_ESCALATE_LINES and
# SIZE_ESCALATE_LINES become unreachable — a PR could suppress its own size
# signal. pr-size.yml guards the analogous `linguist-generated` case the same
# way. It is a TOP-LEVEL git option (before the subcommand) and needs git
# >= 2.42.
#
# Support is probed EXPLICITLY rather than inferred from the diff failing.
# Falling back whenever the first diff errored would silently restore the very
# `* -diff` bypass this closes, for any unrelated failure — so the fallback is
# reached only when git genuinely does not know the option, and a real diff
# failure stays a real failure. The degradation is recorded in the report so it
# is visible rather than living in one stderr line.
#
# The probe's EXIT CODE alone is not that signal. `rev-parse --verify HEAD`
# also fails on an unborn or invalid HEAD, on a --repo-dir that is not a git
# repository, and on an unreadable one — none of which say anything about
# --attr-source support. Treating those as "unsupported" dropped the anti-bypass
# guard on a runner that does support it and reported attr_source_degraded to
# reviewers who were not degraded. So the unknown-option signal is identified
# specifically (git exits 129 and says so on stderr); every OTHER probe failure
# KEEPS the option, and the real diff below reports the real error.
NUMSTAT_FILE="${OUT_DIR}/.numstat"
NUMSTAT_ERR="${OUT_DIR}/.numstat.err"
ATTR_PROBE_ERR="${OUT_DIR}/.attr-probe.err"
ATTR_ARG=("--attr-source=${BASE}")
ATTR_DEGRADED_ARG=()
ATTR_DEGRADED=""
git -C "$REPO_DIR" "--attr-source=${BASE}" rev-parse --quiet --verify HEAD \
  >/dev/null 2>"$ATTR_PROBE_ERR"
ATTR_PROBE_RC=$?
if [ "$ATTR_PROBE_RC" -ne 0 ] && { [ "$ATTR_PROBE_RC" -eq 129 ] \
   || grep -qiE 'unknown option|unknown switch|unrecognized option' "$ATTR_PROBE_ERR"; }; then
  ATTR_ARG=()
  ATTR_DEGRADED_ARG=(--attr-degraded)
  ATTR_DEGRADED=1
  echo "grade-risk.sh: this git does not support --attr-source (needs >= 2.42); .gitattributes was read from the PR head, so a PR that marks its own files '-diff' can zero its line counts." >&2
fi
rm -f "$ATTR_PROBE_ERR"
# The trailing `--` is load-bearing: without it git falls back to treating an
# unresolvable `A...B` as a PATHSPEC, which exits 0 with an empty diff — an
# ungradable ref would then be graded R0 (empty diff) instead of reported
# unknown, the one default this workflow forbids. CI always passes a real base
# SHA, but the offline backfill caller (BE-5507) passes operator-supplied refs.
if ! git -C "$REPO_DIR" "${ATTR_ARG[@]+"${ATTR_ARG[@]}"}" diff --numstat -z "${BASE}...${HEAD}" -- \
     >"$NUMSTAT_FILE" 2>"$NUMSTAT_ERR"; then
  emit_unknown "git diff ${BASE}...${HEAD} failed: $(tr '\n' ' ' <"$NUMSTAT_ERR")"
fi

# --marker is forwarded only when the caller explicitly set one; otherwise the
# grader's own DEFAULT_MARKER applies, keeping a single source of truth.
MARKER_ARG=()
if [ -n "$MARKER" ]; then
  MARKER_ARG=(--marker "$MARKER")
fi
if ! "$PYTHON" "$GRADER" --numstat "$NUMSTAT_FILE" --out-dir "$OUT_DIR" \
     "${MARKER_ARG[@]+"${MARKER_ARG[@]}"}" "${ATTR_DEGRADED_ARG[@]+"${ATTR_DEGRADED_ARG[@]}"}"; then
  emit_unknown "the grader exited non-zero"
fi

rm -f "$NUMSTAT_FILE" "$NUMSTAT_ERR"
