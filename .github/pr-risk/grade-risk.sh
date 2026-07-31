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
    printf '{"schema":1,"status":"unknown","tier":null,"label":null,"reason":%s,"total_lines":0,"tier_lines":{},"files":[],"top_tier_files":[],"attr_source_degraded":false}\n' \
      "\"grader unavailable\"" > "${OUT_DIR}/risk-report.json"
    # The one place the marker is repeated: by definition Python is unusable
    # here, so grade_risk.DEFAULT_MARKER cannot be read. Keep the two in sync.
    printf '%s\n\nRisk: unknown — the grader could not run.\n' \
      "${MARKER:-<!-- ci-pr-risk -->}" > "${OUT_DIR}/risk-comment.md"
    printf 'Risk: unknown\n\nThe risk grader could not run.\n' > "${OUT_DIR}/risk-check.md"
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
NUMSTAT_FILE="${OUT_DIR}/.numstat"
NUMSTAT_ERR="${OUT_DIR}/.numstat.err"
ATTR_ARG=()
ATTR_DEGRADED_ARG=()
ATTR_DEGRADED=""
if git -C "$REPO_DIR" "--attr-source=${BASE}" rev-parse --quiet --verify HEAD >/dev/null 2>&1; then
  ATTR_ARG=("--attr-source=${BASE}")
else
  ATTR_DEGRADED_ARG=(--attr-degraded)
  ATTR_DEGRADED=1
  echo "grade-risk.sh: this git does not support --attr-source (needs >= 2.42); .gitattributes was read from the PR head, so a PR that marks its own files '-diff' can zero its line counts." >&2
fi
if ! git -C "$REPO_DIR" "${ATTR_ARG[@]+"${ATTR_ARG[@]}"}" diff --numstat -z "${BASE}...${HEAD}" \
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
