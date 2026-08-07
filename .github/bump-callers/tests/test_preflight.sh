#!/usr/bin/env bash
#
# Functional tests for the bump-fleet staleness/decommission preflight
# (preflight.sh).
#
# The guard this script replaces is copy-pasted into every bump-*-callers.yml
# entrypoint, where nothing can test it — and the copies drifted: five skip on a
# bare tip mismatch (throwing away the only run for a change), one compares blobs
# but forgets to re-point the pin at the verified tip, and only one of the two
# content-comparing copies covers the asset directory that multi-path fleets also
# watch. Now that the logic is one script, it gets the same treatment as
# bump-callers.sh: drive the REAL script and assert the behavior each entrypoint
# depends on.
#
# No network and no GitHub: each case builds a throwaway bare repo as `origin`
# and a clone as the run's workspace, drives preflight.sh with $GITHUB_OUTPUT
# pointed at a temp file, and asserts the exit code, both step outputs, and the
# presence/absence of ::error::/::warning:: annotations.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFLIGHT="${SCRIPT_DIR}/../preflight.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1 [$2]"; fi; }

# The paths a real multi-path fleet (groom) watches. Any pair would do; using the
# real ones keeps the fixtures recognizable.
WATCHED_PATH=".github/workflows/groom.yml"
ASSETS_PATH=".github/groom"

CASE=""; SRC=""; ORIGIN=""; WORKDIR=""; OUTFILE=""; OUT=""; RC=0; P=""; N=""

# --- fixture: a bare repo as `origin`, a clone as the run workspace -----------
# SRC is the scratch tree used to author commits; ORIGIN is the bare repo the
# script's `git ls-remote` / `git fetch` talk to; WORKDIR is the checkout the
# script runs in (Actions' own `actions/checkout` of github.sha). `file://` URLs
# so `git fetch --depth=1` really does a shallow fetch instead of being ignored
# as a local-path clone.
new_case() {
  echo
  echo "== $2 =="
  CASE="${WORK}/$1"
  SRC="${CASE}/src"; ORIGIN="${CASE}/origin.git"; WORKDIR="${CASE}/work"
  OUTFILE="${CASE}/gh_output"
  mkdir -p "$SRC"
  git -c init.defaultBranch=main init -q "$SRC"
  git -C "$SRC" config user.email preflight-tests@example.invalid
  git -C "$SRC" config user.name  'Preflight Tests'
  mkdir -p "${SRC}/.github/workflows" "${SRC}/${ASSETS_PATH}"
  printf 'name: Groom\non:\n  workflow_call:\n' > "${SRC}/${WATCHED_PATH}"
  printf 'finder brief v1\n'                    > "${SRC}/${ASSETS_PATH}/finder.md"
  printf 'unrelated file\n'                     > "${SRC}/README.md"
  git -C "$SRC" add -A
  git -C "$SRC" commit -qm 'initial'
  git clone -q --bare "$SRC" "$ORIGIN"
  git -C "$SRC" remote add origin "file://${ORIGIN}"
  clone_work
}

clone_work() { rm -rf "$WORKDIR"; git clone -q "file://${ORIGIN}" "$WORKDIR"; }

# Commit whatever is staged in SRC and advance origin/main to it.
push_src() {
  git -C "$SRC" add -A
  git -C "$SRC" commit -qm "$1"
  git -C "$SRC" push -q origin main
}

origin_tip() { git -C "$ORIGIN" rev-parse main; }
work_head()  { git -C "$WORKDIR" rev-parse HEAD; }

# Run the real script in WORKDIR. Extra `VAR=value` arguments are appended to the
# environment (so a case can add WATCHED_ASSETS or override anything above).
run_preflight() {
  : > "$OUTFILE"
  # shellcheck disable=SC2034  # OUT/RC/P/N are read by the `check` assertions below
  OUT=$(cd "$WORKDIR" && env \
    WATCHED="$WATCHED_PATH" \
    GITHUB_OUTPUT="$OUTFILE" \
    "$@" bash "$PREFLIGHT" 2>&1)
  RC=$?
  P=$(grep '^proceed=' "$OUTFILE" 2>/dev/null | tail -1 | cut -d= -f2-)
  N=$(grep '^new_sha=' "$OUTFILE" 2>/dev/null | tail -1 | cut -d= -f2-)
}

# ---------------------------------------------------------------------------
new_case decoy 'a decoy refs/heads/foo/refs/heads/main is not the main tip'
# `git ls-remote origin refs/heads/main` matches ref patterns at COMPONENT
# BOUNDARIES, so a branch literally named foo/refs/heads/main also matches — and
# it sorts FIRST (f < m), so a bare "first line" parse consumes it. Point the
# decoy at an older commit: if the parse picks it up, the script thinks main
# moved and logs the re-point. It must not.
printf 'unrelated file, edited\n' > "${SRC}/README.md"
push_src 'second commit'
clone_work
DECOY_SHA=$(git -C "$ORIGIN" rev-parse 'main^')
git -C "$ORIGIN" update-ref refs/heads/foo/refs/heads/main "$DECOY_SHA"
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=true"                  "[[ \"$P\" == \"true\" ]]"
check "new_sha is the real main tip"  "[[ \"$N\" == \"$TIP\" ]]"
check "decoy ref was not consumed"    "! grep -q \"main moved\" <<<\"\$OUT\""
check "no ::error::"                  "! grep -q \"::error::\" <<<\"\$OUT\""
check "no ::warning::"                "! grep -q \"::warning::\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case lsremote 'a failed ls-remote is a hard error, not a silent no-op'
# "A lookup we couldn't perform is not evidence of staleness" — the whole reason
# the tip is not parsed through a pipe. An unreachable origin must fail the job,
# never quietly leave every caller un-bumped.
git -C "$WORKDIR" remote set-url origin "file://${CASE}/does-not-exist.git"
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP"
check "exit 1"                        "[[ $RC -eq 1 ]]"
check "::error:: annotation"          "grep -q \"::error::\" <<<\"\$OUT\""
check "proceed is not true"           "[[ \"$P\" != \"true\" ]]"

# ---------------------------------------------------------------------------
new_case stale_blob 'stale re-run: the watched workflow changed on main since'
# A later commit touched the watched path, so that commit has its own run and
# will pin the newer content. This run is a stale re-run — skip.
BEHIND=$(work_head)
printf 'name: Groom\non:\n  workflow_call:\n    inputs: {}\n' > "${SRC}/${WATCHED_PATH}"
push_src 'edit the watched workflow'
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "logged as a stale run"         "grep -q \"stale run/re-run\" <<<\"\$OUT\""
check "no ::warning::"                "! grep -q \"::warning::\" <<<\"\$OUT\""
check "no ::error::"                  "! grep -q \"::error::\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case repoint 'main moved but the watched surface is unchanged: re-point'
# An unrelated commit landed between the trigger and this check. The path filter
# means it started NO run of its own, so skipping here would discard the only run
# for this change. Proceed — but pin to the VERIFIED TIP, not this run's stale
# github.sha (which would hand callers a non-tip commit).
BEHIND=$(work_head)
printf 'unrelated file, edited\n' > "${SRC}/README.md"
push_src 'unrelated commit'
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=true"                  "[[ \"$P\" == \"true\" ]]"
check "new_sha re-pointed to the tip" "[[ \"$N\" == \"$TIP\" ]]"
check "new_sha is not the stale sha"  "[[ \"$N\" != \"$BEHIND\" ]]"
check "re-point logged"               "grep -q \"pinning callers to $TIP\" <<<\"\$OUT\""
check "no ::warning::"                "! grep -q \"::warning::\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case decommissioned 'the watched workflow was deleted on main: decommissioned'
# The push path filter also matches the commit that DELETES the reusable. Bumping
# callers to a SHA where it is gone would break every one of them, so this is a
# warned no-op, not a bump.
BEHIND=$(work_head)
git -C "$SRC" rm -rq "${WATCHED_PATH}" "${ASSETS_PATH}"
push_src 'retire the groom reusable'
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "::warning:: annotation"        "grep -q \"::warning::\" <<<\"\$OUT\""
check "decommission message"          "grep -q \"no longer exists on main\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case decommissioned_staged 'the workflow was deleted but its assets survive'
# The real retirement sequence: delete the reusable now, clean up its asset
# directory in a later commit. EITHER surface being gone at the tip has to read
# as a decommission — an AND would let this (the common case) fall through to the
# stale branch and exit green, suppressing the ::warning:: that is the fleet's
# only chance to say that live callers now hard-fail at startup.
BEHIND=$(work_head)
git -C "$SRC" rm -rq "${WATCHED_PATH}"
push_src 'retire the groom reusable, briefs cleaned up later'
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "::warning:: annotation"        "grep -q \"::warning::\" <<<\"\$OUT\""
check "names the deleted workflow"    "grep -q \"::warning::${WATCHED_PATH} no longer exists on main\" <<<\"\$OUT\""
check "not reported as stale"         "! grep -q \"stale run/re-run\" <<<\"\$OUT\""
# ...and the mirror image: the asset dir goes first, the workflow file survives.
new_case decommissioned_assets 'the asset dir was deleted but the workflow survives'
BEHIND=$(work_head)
git -C "$SRC" rm -rq "${ASSETS_PATH}"
push_src 'retire the groom briefs, workflow cleaned up later'
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "names the deleted asset dir"   "grep -q \"::warning::${ASSETS_PATH} no longer exists on main\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case assets 'multi-path fleet: only the asset dir changed — still stale'
# The case a naive single-blob port gets WRONG. groom/cursor-review/pr-size
# callers pin an asset directory too (the briefs/prompts/scripts loaded at run
# time), so a commit that touches only that directory DOES have its own run —
# comparing $WATCHED alone would re-point and double-bump.
BEHIND=$(work_head)
printf 'finder brief v2\n' > "${SRC}/${ASSETS_PATH}/finder.md"
push_src 'edit the finder brief only'
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "logged as a stale run"         "grep -q \"stale run/re-run\" <<<\"\$OUT\""
# ...and the same commit WITHOUT WATCHED_ASSETS is the under-verifying single-path
# comparison, which proves the widened comparison is what makes the difference.
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND"
check "single-path config would re-point" "[[ \"$P\" == \"true\" ]]"

# ---------------------------------------------------------------------------
new_case own_commit 'the watched workflow is absent at this run OWN commit'
# An unresolvable HEAD:$WATCHED is the deletion-commit case; it must not fall
# into the "changed since" branch and be reported as a stale re-run.
git -C "$SRC" rm -rq "${WATCHED_PATH}"
push_src 'retire the groom reusable'
clone_work
BEHIND=$(work_head)
printf 'unrelated file, edited again\n' > "${SRC}/README.md"
push_src 'unrelated commit on top'
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "::warning:: annotation"        "grep -q \"::warning::\" <<<\"\$OUT\""
check "own-commit message"            "grep -q \"absent at this run.s own commit\" <<<\"\$OUT\""
check "not reported as stale"         "! grep -q \"stale run/re-run\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case current_tip 'happy path: this run IS the current main tip'
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=true"                  "[[ \"$P\" == \"true\" ]]"
check "new_sha is github.sha"         "[[ \"$N\" == \"$TIP\" ]]"
check "no fetch/compare happened"     "! grep -q \"main moved\" <<<\"\$OUT\""
check "no ::error::"                  "! grep -q \"::error::\" <<<\"\$OUT\""
check "no ::warning::"                "! grep -q \"::warning::\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case missing_dir 'current tip, but the watched asset dir is gone locally'
# The final decommission check tests "$WATCHED"/"$WATCHED_ASSETS" — the
# VARIABLES, never a second copy of the literal path. Two literals drift apart on
# a rename and the stale one names a file that never exists, making the test
# always true and the whole fleet a permanent silent no-op.
git -C "$SRC" rm -rq "${ASSETS_PATH}"
push_src 'retire the groom briefs'
clone_work
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "::warning:: names the assets"  "grep -q \"::warning::${ASSETS_PATH} absent\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case missing_file 'current tip, but the watched workflow is gone locally'
# The fleet's PRIMARY decommission path — the `[[ ! -f "$WATCHED" ]]` guard — is
# reached when the DELETING commit is itself the tip, so none of the "main moved"
# comparisons run at all. Its WATCHED_ASSETS variant is covered above; this is
# the one every single-path fleet depends on.
git -C "$SRC" rm -rq "${WATCHED_PATH}"
push_src 'retire the groom reusable at the tip'
clone_work
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "::warning:: names the file"    "grep -q \"::warning::${WATCHED_PATH} absent\" <<<\"\$OUT\""
check "no fetch/compare happened"     "! grep -q \"main moved\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case tag_shadow 'a TAG named main does not shadow refs/heads/main'
# `git fetch origin main` resolves the bare name through refs/tags/<name> BEFORE
# refs/heads/<name>, and this repo routinely creates and force-moves major tags.
# A tag named `main` would silently become the FETCH_HEAD that the comparison and
# the re-point run against — i.e. what every caller gets pinned to. Point the tag
# at a commit whose watched file DIFFERS, so consuming it reads as "stale" while
# the correct branch fetch re-points.
BEHIND=$(work_head)
printf 'name: Groom\non:\n  workflow_call:\n    inputs: {}\n' > "${SRC}/${WATCHED_PATH}"
push_src 'a decoy commit that edits the watched workflow'
DECOY_SHA=$(origin_tip)
git -C "$SRC" checkout -q -- . 2>/dev/null || true
printf 'name: Groom\non:\n  workflow_call:\n' > "${SRC}/${WATCHED_PATH}"
printf 'unrelated file, edited\n'             > "${SRC}/README.md"
push_src 'restore the watched workflow, edit something unrelated'
TIP=$(origin_tip)
git -C "$ORIGIN" tag main "$DECOY_SHA"
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=true"                  "[[ \"$P\" == \"true\" ]]"
check "re-pointed to the BRANCH tip"  "[[ \"$N\" == \"$TIP\" ]]"
check "did not consume the tag"       "[[ \"$N\" != \"$DECOY_SHA\" ]]"
check "not reported as stale"         "! grep -q \"stale run/re-run\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case glob_input 'a glob-shaped watched path is rejected, not read as absent'
# The README tells maintainers to widen these inputs to match the fleet's
# `paths:` filter, which points straight at `.github/groom/**`. A glob resolves
# to NOTHING — `[[ -d '.github/groom/**' ]]` is false and
# `git rev-parse 'HEAD:.github/groom/**'` is empty — so left unvalidated it makes
# every comparison verify nothing and the whole fleet a permanent silent no-op
# behind a green run.
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED_ASSETS="${ASSETS_PATH}/**"
check "exit 1"                        "[[ $RC -eq 1 ]]"
check "::error:: annotation"          "grep -q \"::error::WATCHED_ASSETS must be a literal path\" <<<\"\$OUT\""
check "proceed is not true"           "[[ \"$P\" != \"true\" ]]"
# A trailing slash is the same footgun with no glob character in sight.
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED_ASSETS="${ASSETS_PATH}/"
check "trailing slash: exit 1"        "[[ $RC -eq 1 ]]"
check "trailing slash: ::error::"     "grep -q \"must not end in a slash\" <<<\"\$OUT\""
check "trailing slash: not proceed"   "[[ \"$P\" != \"true\" ]]"

# ---------------------------------------------------------------------------
new_case bad_new_sha 'a malformed NEW_SHA is rejected before it reaches an output'
# NEW_SHA is the one value never derived from a lookup: it is emitted verbatim
# into $GITHUB_OUTPUT and handed to bump-callers.sh's pin rewrite. A newline in
# it injects extra output lines — and an injected `proceed=true` would win over
# the `proceed=false` this script wrote, since a consuming step reads the LAST
# value of a repeated key.
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$TIP" NEW_SHA=$'0000000000000000000000000000000000000000\nproceed=true'
check "exit 1"                        "[[ $RC -eq 1 ]]"
check "::error:: annotation"          "grep -q \"::error::NEW_SHA must be a full 40\" <<<\"\$OUT\""
check "no injected proceed=true"      "! grep -q \"^proceed=true\$\" \"$OUTFILE\""
check "nothing written to output"     "[[ ! -s \"$OUTFILE\" ]]"
# A short/abbreviated SHA is the other way a bad pin reaches every caller.
run_preflight GITHUB_SHA="$TIP" NEW_SHA="${TIP:0:12}"
check "short sha: exit 1"             "[[ $RC -eq 1 ]]"
check "short sha: not proceed"        "[[ \"$P\" != \"true\" ]]"

# ---------------------------------------------------------------------------
new_case head_mismatch 'HEAD that is not GITHUB_SHA is a hard error'
# Every "here" side is read from HEAD while every decision is keyed off
# GITHUB_SHA. A consuming checkout with a `ref:` override (or any earlier step
# that moves HEAD) would have the script compare main against itself: every
# comparison reads "unchanged", so every stale re-run proceeds and re-points.
BEHIND=$(work_head)
printf 'name: Groom\non:\n  workflow_call:\n    inputs: {}\n' > "${SRC}/${WATCHED_PATH}"
push_src 'edit the watched workflow'
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP"   # HEAD is still $BEHIND
check "exit 1"                        "[[ $RC -eq 1 ]]"
check "::error:: annotation"          "grep -q \"::error::HEAD ($BEHIND) is not this run\" <<<\"\$OUT\""
check "proceed is not true"           "[[ \"$P\" != \"true\" ]]"

echo
echo "== $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
