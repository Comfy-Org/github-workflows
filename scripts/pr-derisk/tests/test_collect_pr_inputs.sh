#!/usr/bin/env bash
# test_collect_pr_inputs.sh — hermetic tests for collect-pr-inputs.sh, the step that re-grades a
# `/derisk` target with the SAME grader and the SAME base-ref overrides pr-risk graded it with.
# No network and no model: `gh` is stubbed on PATH and every call it receives is logged, and the
# grader is a stub in TOOL_DIR, so the tests assert on WHICH requests were made.
#
# What is pinned here:
#   * THE LIBRARY TRAVELS WITH THIS SCRIPT, NOT WITH TOOL_DIR. TOOL_DIR names the swappable
#     grader; a stub grader directory holding no lib.sh must still run, exactly as the pr-risk
#     suite already stubs TOOL_DIR against grade-targets.sh.
#   * THE SCRATCH TRIO IS MINTED ONCE, IN THE PARENT, AND CLEANED UP. Both resolvers run inside a
#     command substitution, so a lazy init_scratch mints a fresh trio per call IN THE SUBSHELL —
#     nine files per run, none of them removed, because this script installs the only trap over them.
#   * AN UNUSABLE OVERRIDE PATH IS AN ERROR, NOT A FALL-THROUGH. Planning a split against the
#     generic default map when the repo asked for its own is a floor computed from rules nobody read.
#
#   bash tests/test_collect_pr_inputs.sh        # exit 0 = all green
#
# Deliberately bash (shebang), not zsh — CI runners and the workflow both exercise bash.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLDIR="$(cd "$SELF_DIR/.." && pwd)"
COLLECT="$TOOLDIR/collect-pr-inputs.sh"
[ -f "$COLLECT" ] || { echo "FATAL: $COLLECT not found" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found on PATH" >&2; exit 2; }

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pr-derisk-collect-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi; }
has_text() { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1 (wanted '$2')" "$3" ;; esac; }
no_text()  { case "$3" in *"$2"*) bad "$1 (should NOT contain '$2')" "$3" ;; *) ok "$1" ;; esac; }

STUB_DIR="$SANDBOX/stub"
STUB_LOG="$SANDBOX/gh-calls.log"
BIN="$SANDBOX/bin"
mkdir -p "$STUB_DIR" "$BIN"
printf 'release/v2' > "$STUB_DIR/base_ref"
printf '404'        > "$STUB_DIR/contents_mode"

# `gh` stub. The base-ref read and the diff read hit the SAME endpoint and are told apart the way
# the script tells them apart: the base-ref read carries `--jq`, the diff read an Accept header.
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
kind=""; want_diff=0
for a in "$@"; do
  case "$a" in
    *contents/*)                        kind=contents ;;
    *vnd.github.diff*)                  want_diff=1 ;;
    *pulls/[0-9]*)                      [ -n "$kind" ] || kind=pull ;;
  esac
done
case "$kind" in
  contents)
    case "$(cat "$STUB_DIR/contents_mode" 2>/dev/null || echo 404)" in
      404) echo 'gh: Not Found (HTTP 404)' >&2; exit 1 ;;
      *)   cat "$(cat "$STUB_DIR/contents_mode")" ;;
    esac ;;
  pull)
    if [ "$want_diff" = 1 ]; then
      cat "$STUB_DIR/diff.patch" 2>/dev/null || printf 'diff --git a/README.md b/README.md\n+x\n'
    else
      cat "$STUB_DIR/base_ref"; echo
    fi ;;
  *) echo "gh stub: unhandled args: $*" >&2; exit 1 ;;
esac
exit 0
STUB
chmod +x "$BIN/gh"

# `mktemp` wrapper that COUNTS calls, so the suite can tell one trio from three. It must still be a
# real mktemp — the script's scratch files and the record files both go through it.
REAL_MKTEMP="$(command -v mktemp)"
cat > "$BIN/mktemp" <<MKSTUB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "\$MKTEMP_LOG"
exec "$REAL_MKTEMP" "\$@"
MKSTUB
chmod +x "$BIN/mktemp"

# A stub grader in TOOL_DIR. Deliberately a directory that holds NOTHING ELSE — no lib.sh — which
# is the whole point: the library must come from beside collect-pr-inputs.sh, not from this input.
STUBTOOL="$SANDBOX/stubtool"
mkdir -p "$STUBTOOL"
printf '#!/usr/bin/env bash\nprintf %%s %s\n' "'{\"risk\":{\"tier\":\"R2\"}}'" > "$STUBTOOL/grade-pr-risk.sh"
chmod +x "$STUBTOOL/grade-pr-risk.sh"

WORK=""; RC=0; OUT=""; SCRATCH=""
run_collect() { # VAR=val ...
  WORK="$(mktemp -d "$SANDBOX/work.XXXXXX")"
  SCRATCH="$WORK/tmp"; mkdir -p "$SCRATCH"
  : > "$STUB_LOG"; : > "$SANDBOX/mktemp.log"
  RC=0
  OUT="$( cd "$WORK" && PATH="$BIN:$PATH" \
      env REPO=test/repo PR_NUMBER=42 OUT_DIR="$WORK/out" TOOL_DIR="$STUBTOOL" \
          TMPDIR="$SCRATCH" MKTEMP_LOG="$SANDBOX/mktemp.log" \
          READ_RETRY_TRIES=1 READ_RETRY_DELAY_SECONDS=0 \
          STUB_LOG="$STUB_LOG" STUB_DIR="$STUB_DIR" \
          "$@" bash "$COLLECT" 2>&1 )" || RC=$?
}
calls() { cat "$STUB_LOG" 2>/dev/null; }
# How many scratch files init_scratch minted, counted at the source: one trio is 3.
scratch_mints() { grep -c 'grade-targets-' "$SANDBOX/mktemp.log" 2>/dev/null || echo 0; }
# …and how many of them are still on disk once the script has exited.
scratch_left()  { find "$SCRATCH" -name 'grade-targets-*' -type f 2>/dev/null | wc -l | tr -d ' '; }

echo "— phase 1: the happy path collects a record, a diff and the resolved tier —"
run_collect
eq "the step succeeds"                 0 "$RC"
has_text "the base ref was read"       "pulls/42" "$(calls)"
# `%2F`, not `/`: enc() percent-encodes the whole ref as one URL component, which is what keeps a
# branch named `fix/#123-thing` from truncating the request at the `#` into an empty — and
# therefore default-branch — `?ref=`.
has_text "the override is read from THAT ref, not the default branch" "?ref=release%2Fv2" "$(calls)"
has_text "the tier is emitted"         "tier=R2" "$OUT"
has_text "…and the record path with it" "record=" "$OUT"
eq "the record is on disk"             1 "$( [ -s "$WORK/out/record.json" ] && echo 1 || echo 0 )"

echo "— phase 2: lib.sh comes from beside THIS script, not from TOOL_DIR —"
# TOOL_DIR above holds a stub grader and nothing else. Sourcing the library from it would hard-die
# here instead of merely swapping the grader — the pr-risk suite stubs TOOL_DIR the same way, and
# grade-targets.sh sources from SELF_DIR for exactly this reason.
eq "a stub TOOL_DIR with no lib.sh still runs"  0 "$RC"
no_text "and never complains about sourcing"    "could not source lib.sh" "$OUT"

echo "— phase 3: ONE scratch trio, minted in the parent, and nothing left behind —"
# resolve_base_ref and both fetch_override calls run inside a command substitution. A lazy
# init_scratch therefore mints its trio in the SUBSHELL and the ERRF/LABELF/OUTF assignments never
# reach this scope: each of the three calls mints a FRESH set — nine files — and with no trap in
# the parent every one of them survives the run.
eq "three scratch files were minted, not nine"  3 "$(scratch_mints)"
eq "…and none of them survives the run"         0 "$(scratch_left)"

echo "— phase 4: an override path that leaves the contents tree is refused —"
# The planner's floors come from the map this step resolves. Falling through to the generic default
# because the requested path was unfetchable would plan a split against rules nobody read — the
# same failure resolving the base ref exists to prevent.
run_collect MAP_PATH='../../../etc/passwd'
eq "the step fails rather than planning"  2 "$RC"
has_text "and says why"  "contains a '..' segment" "$OUT"
no_text "the path was never requested"    "/etc/passwd" "$(calls)"
no_text "…and it is NOT read as an absent override" "using the generic default" "$OUT"
eq "no scratch file survives that path either"  0 "$(scratch_left)"

echo "— phase 5: an unresolvable base ref stops the step —"
printf '' > "$STUB_DIR/base_ref"
run_collect
eq "an empty base ref is fatal"  2 "$RC"
has_text "and names the reason"  "read back empty" "$OUT"
eq "no scratch file survives the failure path"  0 "$(scratch_left)"
printf 'release/v2' > "$STUB_DIR/base_ref"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
