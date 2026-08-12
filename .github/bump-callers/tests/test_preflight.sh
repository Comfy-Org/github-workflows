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

# The paths the one EXCLUDING fleet (pr-risk) watches, in the shape its
# entrypoint's `paths:` filter has them: a reusable, a tool directory, and two
# negations for the parts of that directory no pinned caller executes. Its
# decommission surface is the three grader scripts, not the directory.
RISK_WATCHED=".github/workflows/pr-risk.yml"
RISK_TOOLS="scripts/pr-risk"
RISK_PATHSPECS=$'.github/workflows/pr-risk.yml\nscripts/pr-risk\n:(exclude)scripts/pr-risk/tests\n:(exclude)scripts/pr-risk/README.md'
RISK_EXEC=$'.github/workflows/pr-risk.yml\nscripts/pr-risk/grade-pr-risk.sh\nscripts/pr-risk/grade-targets.sh\nscripts/pr-risk/resolve-enabled.sh'

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

# Adds the pr-risk-shaped surface to the fixture (on top of the initial commit)
# and re-clones the workspace so HEAD carries it: the reusable, three executed
# grader scripts, and the two things its `paths:` filter excludes — a tests/
# directory and the tool README.
seed_pr_risk() {
  mkdir -p "${SRC}/${RISK_TOOLS}/tests"
  printf 'name: PR risk\non:\n  workflow_call:\n' > "${SRC}/${RISK_WATCHED}"
  local s
  for s in grade-pr-risk grade-targets resolve-enabled; do
    printf '#!/usr/bin/env bash\necho %s v1\n' "$s" > "${SRC}/${RISK_TOOLS}/${s}.sh"
  done
  printf 'grader test v1\n' > "${SRC}/${RISK_TOOLS}/tests/test_grade.sh"
  printf 'tool README v1\n' > "${SRC}/${RISK_TOOLS}/README.md"
  push_src 'seed the pr-risk tool surface'
  clone_work
}

# Actions' own `actions/checkout` is SHALLOW by default, so the deepening arm of
# the script's `--unshallow` probe is the one that runs in production — and a
# full-clone fixture never exercises it. This is the variant that does.
clone_work_shallow() {
  rm -rf "$WORKDIR"
  git clone -q --depth=1 "file://${ORIGIN}" "$WORKDIR"
}

# The same shallow checkout, but reached through a LINKED WORKTREE. Git keeps the
# `shallow` marker in the COMMON git dir, while `git rev-parse --git-dir` inside a
# linked worktree answers with that worktree's own directory — so a hand-rolled
# `[[ -f "$(git rev-parse --git-dir)/shallow" ]]` probe reports "not shallow"
# here and skips the deepening the ancestry guard depends on.
clone_work_shallow_worktree() {
  rm -rf "$WORKDIR" "${CASE}/common"
  git clone -q --depth=1 "file://${ORIGIN}" "${CASE}/common"
  git -C "${CASE}/common" worktree add -q --detach "$WORKDIR" HEAD
}

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
new_case backwards_differs 'origin main force-moved BACKWARDS, watched content differs'
# The direction guard. Nothing above ever checks that the fetched tip DESCENDS
# from this run's commit, so a main that moved backwards (force-push, a
# revert-reset, or a stale replica answering the tip lookup) lands in the content
# comparison with the older commit on the "tip" side. Here that content differs,
# so the pre-guard script logged "the newer commit has its own run" — about a
# commit that is OLDER — and exited GREEN, freezing every caller behind a run
# that will never come. It has to be loud instead.
BACK=$(origin_tip)
printf 'name: Groom\non:\n  workflow_call:\n    inputs: {}\n' > "${SRC}/${WATCHED_PATH}"
push_src 'edit the watched workflow'
clone_work                             # the run's checkout IS the new tip
TIP=$(work_head)
git -C "$ORIGIN" update-ref refs/heads/main "$BACK"   # force-push main backwards
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP"
check "exit 1"                        "[[ $RC -ne 0 ]]"
check "::error:: names the direction" "grep -q \"::error::.*does not descend\" <<<\"\$OUT\""
check "NOT the silent stale verdict"  "! grep -q \"stale run/re-run\" <<<\"\$OUT\""
check "proceed is not false"          "[[ \"$P\" != \"false\" ]]"
check "nothing written to output"     "[[ ! -s \"$OUTFILE\" ]]"

# ---------------------------------------------------------------------------
new_case backwards_equal 'origin main force-moved BACKWARDS, watched content identical'
# The other half of the same bug, and the worse one. With the watched surface
# byte-identical at both commits, the pre-guard script fell straight through to
# the re-point and handed `new_sha` the OLDER tip — pinning every caller in the
# fleet BACKWARDS, which is exactly what the re-point exists to avoid.
BACK=$(origin_tip)
printf 'unrelated file, edited\n' > "${SRC}/README.md"
push_src 'a commit that touches only the unrelated file'
clone_work                             # the run's checkout IS the new tip
TIP=$(work_head)
git -C "$ORIGIN" update-ref refs/heads/main "$BACK"
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 1"                        "[[ $RC -ne 0 ]]"
check "::error:: names the direction" "grep -q \"::error::.*does not descend\" <<<\"\$OUT\""
check "did not re-point backwards"    "[[ \"$N\" != \"$BACK\" ]]"
check "no backwards re-point logged"  "! grep -q \"pinning callers to $BACK\" <<<\"\$OUT\""
check "nothing written to output"     "[[ ! -s \"$OUTFILE\" ]]"

# ---------------------------------------------------------------------------
new_case shallow_repoint 'a SHALLOW workdir still re-points: the --unshallow arm'
# The guard above needs REAL history: `git merge-base --is-ancestor` against a
# `--depth=1` graft returns false even for a legitimate forward move, so a naive
# port would hard-fail every re-point in production — where actions/checkout is
# shallow. This is the happy path run against that real-world shape.
printf 'unrelated file, v1\n' > "${SRC}/README.md"
push_src 'a second commit, so a depth=1 clone really truncates'
clone_work_shallow
BEHIND=$(work_head)
check "workdir really is shallow"     "[[ -f \"${WORKDIR}/.git/shallow\" ]]"
printf 'unrelated file, v2\n' > "${SRC}/README.md"
push_src 'unrelated commit'
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=true"                  "[[ \"$P\" == \"true\" ]]"
check "new_sha re-pointed to the tip" "[[ \"$N\" == \"$TIP\" ]]"
check "no ::error::"                  "! grep -q \"::error::\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case shallow_worktree 'a shallow LINKED WORKTREE deepens too, and re-points'
# Same happy path as above, one layout over: the checkout is a linked worktree of
# a shallow clone. Git stores the `shallow` marker in the COMMON git dir, but
# `git rev-parse --git-dir` inside a linked worktree answers with the per-worktree
# directory — so probing for `$(git rev-parse --git-dir)/shallow` false-negatives
# exactly here and skips the deepening. Asking git (`--is-shallow-repository`) is
# correct in both layouts.
# The verdict below still comes out right either way (a plain fetch into a shallow
# clone sends the new commits down to the existing boundary, so HEAD stays
# reachable) — which is precisely why this needs an assertion on the DEEPENING and
# not just on the outputs. Left unfixed, the guard's soundness quietly depends on
# that boundary behavior instead of on the `--unshallow` its comment says makes it
# sound, and the shape that does break it (fetch grafting a parentless tip, which
# is what the original `--depth=1` fetch did) is one refspec away.
printf 'unrelated file, v1\n' > "${SRC}/README.md"
push_src 'a second commit, so a depth=1 clone really truncates'
clone_work_shallow_worktree
BEHIND=$(work_head)
check "the marker is NOT in --git-dir" \
  "[[ ! -f \"\$(git -C \"$WORKDIR\" rev-parse --git-dir)/shallow\" ]]"
check "but the repo really is shallow" \
  "[[ \"\$(git -C \"$WORKDIR\" rev-parse --is-shallow-repository)\" == true ]]"
printf 'unrelated file, v2\n' > "${SRC}/README.md"
push_src 'unrelated commit'
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=true"                  "[[ \"$P\" == \"true\" ]]"
check "new_sha re-pointed to the tip" "[[ \"$N\" == \"$TIP\" ]]"
check "no ::error::"                  "! grep -q \"::error::\" <<<\"\$OUT\""
check "the fetch really did deepen" \
  "[[ \"\$(git -C \"$WORKDIR\" rev-parse --is-shallow-repository)\" == false ]]"

# ---------------------------------------------------------------------------
new_case rewound_between 'main REWOUND between the tip lookup and the fetch'
# The direction guard above only proves the fetched tip descends from THIS RUN's
# commit — it says nothing about the tip `ls-remote` reported moments earlier. A
# rewind that lands on a commit still AHEAD of this run therefore sails through
# it: the objects compared, and the SHA every caller is pinned to, come from a
# commit main was already known to be ahead of. Measure the direction of that
# move too.
# The fixture stages C0 (this run) → B → A on origin, then rewinds main to B in
# the window between the two lookups, via a `git` shim on PATH that fires right
# after the ls-remote. B still descends from C0, so ONLY the observed-tip
# comparison can catch this.
BEHIND=$(work_head)
printf 'unrelated file, b\n' > "${SRC}/README.md"
push_src 'commit B'
REWOUND_TO=$(origin_tip)
printf 'unrelated file, a\n' > "${SRC}/README.md"
push_src 'commit A — what ls-remote reports'
SHIM="${CASE}/shim"; mkdir -p "$SHIM"
REAL_GIT="$(command -v git)"
cat > "${SHIM}/git" <<SHIMEOF
#!/usr/bin/env bash
"${REAL_GIT}" "\$@"; rc=\$?
# Rewind origin the instant the tip lookup has answered, so the fetch that
# follows lands on the older commit.
if [[ "\${1:-}" == "ls-remote" ]]; then
  "${REAL_GIT}" -C "${ORIGIN}" update-ref refs/heads/main "${REWOUND_TO}" || true
fi
exit \$rc
SHIMEOF
chmod +x "${SHIM}/git"
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" PATH="${SHIM}:${PATH}"
check "exit 1"                        "[[ $RC -ne 0 ]]"
check "::error:: names the lookup tip" \
  "grep -q \"::error::.*does not descend from the tip the lookup reported\" <<<\"\$OUT\""
check "NOT read as a benign advance"  "! grep -q \"main advanced\" <<<\"\$OUT\""
check "NOT the silent stale verdict"  "! grep -q \"stale run/re-run\" <<<\"\$OUT\""
check "did not pin the rewound tip"   "[[ \"$N\" != \"$REWOUND_TO\" ]]"
check "nothing written to output"     "[[ ! -s \"$OUTFILE\" ]]"

# ---------------------------------------------------------------------------
new_case land_then_revert 'land-then-revert: pin the TIP, not this stale run'
# The case bump-pr-risk-callers.yml's `git rev-list` check exists for — and the
# reason it is deliberately not ported here. A watched change landed and was
# reverted, so the net content at the tip equals the content at this run's
# commit. rev-list would call this a stale re-run; the re-point does something
# better, pinning the VERIFIED TIP (the revert commit — forward, not backwards),
# which is the content every caller should be on.
BEHIND=$(work_head)
printf 'name: Groom\non:\n  workflow_call:\n    inputs: {}\n' > "${SRC}/${WATCHED_PATH}"
push_src 'land a watched change'
printf 'name: Groom\non:\n  workflow_call:\n' > "${SRC}/${WATCHED_PATH}"
push_src 'revert it'
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED_ASSETS="$ASSETS_PATH"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=true"                  "[[ \"$P\" == \"true\" ]]"
check "new_sha is the TIP"            "[[ \"$N\" == \"$TIP\" ]]"
check "not this run's stale sha"      "[[ \"$N\" != \"$BEHIND\" ]]"
check "no ::error::"                  "! grep -q \"::error::\" <<<\"\$OUT\""

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

# ---------------------------------------------------------------------------
new_case pathspec_excluded 'pathspec fleet: a commit touching only EXCLUDED paths is not stale'
# The failure the excluding fleet's own guard documents. pr-risk's `paths:`
# filter negates `scripts/pr-risk/tests/**` and the tool README, so a test-only
# commit starts NO run of its own — but it does move the `scripts/pr-risk` TREE
# OID, so the object comparison reads "the watched surface changed since",
# discards the only real run for this change, and freezes every caller waiting
# on a run that will never exist. The pathspec comparison asks what the filter
# asks.
seed_pr_risk
BEHIND=$(work_head)
printf 'grader test v2\n' > "${SRC}/${RISK_TOOLS}/tests/test_grade.sh"
printf 'tool README v2\n' > "${SRC}/${RISK_TOOLS}/README.md"
push_src 'edit only excluded paths (tests + tool README)'
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" \
  WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS="$RISK_PATHSPECS"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=true"                  "[[ \"$P\" == \"true\" ]]"
check "new_sha re-pointed to the tip" "[[ \"$N\" == \"$TIP\" ]]"
check "not reported as stale"         "! grep -q \"stale run/re-run\" <<<\"\$OUT\""
check "no ::error::"                  "! grep -q \"::error::\" <<<\"\$OUT\""
check "no ::warning::"                "! grep -q \"::warning::\" <<<\"\$OUT\""
# ...and the tree-OID comparison it replaces false-stales that same commit,
# which is what makes the pathspec input necessary rather than cosmetic.
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" \
  WATCHED="$RISK_WATCHED" WATCHED_ASSETS="$RISK_TOOLS"
check "tree-OID config false-stales it" "[[ \"$P\" == \"false\" ]]"
# Blank and indented lines are ignored, so the input can be pasted straight out
# of a YAML block scalar sitting under the `paths:` filter it mirrors.
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" \
  WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS=$'\n  .github/workflows/pr-risk.yml  \n\n  scripts/pr-risk\n  :(exclude)scripts/pr-risk/tests\n  :(exclude)scripts/pr-risk/README.md\n'
check "blank/indented lines ignored"  "[[ \"$P\" == \"true\" ]]"
# A negation only strips the paths it matches: a commit touching an excluded
# path AND a watched one is a real watched change and still has its own run.
printf '#!/usr/bin/env bash\necho grade-pr-risk v2\n' > "${SRC}/${RISK_TOOLS}/grade-pr-risk.sh"
printf 'grader test v3\n'                            > "${SRC}/${RISK_TOOLS}/tests/test_grade.sh"
push_src 'edit a grader AND its test'
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" \
  WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS="$RISK_PATHSPECS"
check "mixed commit is still stale"   "[[ \"$P\" == \"false\" ]]"
check "mixed commit logged as stale"  "grep -q \"stale run/re-run\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case pathspec_watched 'pathspec fleet: a real watched change behind IS stale'
# The other half — the verdict the excluding fleet must keep. A later commit
# touched a path the filter watches, so that commit has its own run and will pin
# the newer grading logic; this run is a stale re-run.
seed_pr_risk
BEHIND=$(work_head)
printf '#!/usr/bin/env bash\necho grade-targets v2\n' > "${SRC}/${RISK_TOOLS}/grade-targets.sh"
push_src 'change the grading logic callers execute'
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" \
  WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS="$RISK_PATHSPECS" WATCHED_EXEC="$RISK_EXEC"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "logged as a stale run"         "grep -q \"stale run/re-run\" <<<\"\$OUT\""
check "no ::warning::"                "! grep -q \"::warning::\" <<<\"\$OUT\""
check "no ::error::"                  "! grep -q \"::error::\" <<<\"\$OUT\""
# The reusable workflow file is in the pathspec list too, not just the tools —
# and asserting that needs a FRESH baseline. Reusing $BEHIND from above would
# leave the grade-targets commit sitting between it and the tip, so the diff
# reports a change whether or not the reusable is covered and the assertion
# could not fail. Re-clone so HEAD is that commit, then move only the reusable.
clone_work
BEHIND_REUSABLE=$(work_head)
printf 'name: PR risk\non:\n  workflow_call:\n    inputs: {}\n' > "${SRC}/${RISK_WATCHED}"
push_src 'change ONLY the reusable itself'
run_preflight GITHUB_SHA="$BEHIND_REUSABLE" NEW_SHA="$BEHIND_REUSABLE" \
  WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS="$RISK_PATHSPECS"
check "reusable change is stale too"  "[[ \"$P\" == \"false\" ]]"
check "reusable change logged stale"  "grep -q \"stale run/re-run\" <<<\"\$OUT\""
# ...and that verdict is the coverage of the reusable doing the work, which is
# why a list that omits it is refused rather than believed: without the guard the
# very same commit reads as "surface unchanged" and this stale run re-points and
# bumps in parallel with the run that commit started for itself.
run_preflight GITHUB_SHA="$BEHIND_REUSABLE" NEW_SHA="$BEHIND_REUSABLE" \
  WATCHED="$RISK_WATCHED" \
  WATCHED_PATHSPECS=$'scripts/pr-risk\n:(exclude)scripts/pr-risk/tests\n:(exclude)scripts/pr-risk/README.md'
check "a list omitting it: exit 1"    "[[ $RC -eq 1 ]]"
check "a list omitting it: ::error::" \
  "grep -q \"::error::WATCHED_PATHSPECS does not select WATCHED\" <<<\"\$OUT\""
check "a list omitting it: no output" "[[ ! -s \"$OUTFILE\" ]]"

# ---------------------------------------------------------------------------
new_case pathspec_selects 'a pathspec list that selects nothing is an error, not "unchanged"'
# `git diff --quiet` exits 0 both for "nothing changed under these pathspecs" and
# for "these pathspecs match nothing at all" — and the second reads as
# "unchanged", re-pointing every caller to a tip at which NOTHING was compared.
# One typo, one directory rename, or one positive fully covered by an
# `:(exclude)` reaches it. Each case below is paired with the baseline run that
# shows the same commit is otherwise a legitimate re-point, so the assertions are
# about the LIST, not about the commit.
seed_pr_risk
BEHIND=$(work_head)
printf 'unrelated file, edited\n' > "${SRC}/README.md"
push_src 'an unrelated commit, so the comparison branch is reached'
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" \
  WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS="$RISK_PATHSPECS"
check "baseline: proceed=true"        "[[ \"$P\" == \"true\" ]]"
check "baseline: re-pointed"          "[[ \"$N\" == \"$TIP\" ]]"
# A typo'd positive — the same shape a directory rename leaves behind.
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED="$RISK_WATCHED" \
  WATCHED_PATHSPECS=$'.github/workflows/pr-risk.yml\nscripts/pr-riskk'
check "typo'd positive: exit 1"       "[[ $RC -eq 1 ]]"
check "typo'd positive: ::error::" \
  "grep -q \"::error::WATCHED_PATHSPECS entry 'scripts/pr-riskk' selects no tracked path\" <<<\"\$OUT\""
check "typo'd positive: not re-pointed" "! grep -q \"pinning callers to\" <<<\"\$OUT\""
check "typo'd positive: no output"    "[[ ! -s \"$OUTFILE\" ]]"
# A positive an exclusion swallows entirely: each entry is checked WITH the
# exclusions applied, exactly as the comparison applies them, so this is caught
# even though the path itself exists.
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED="$RISK_WATCHED" \
  WATCHED_PATHSPECS=$'.github/workflows/pr-risk.yml\nscripts/pr-risk/tests\n:(exclude)scripts/pr-risk/tests'
check "swallowed positive: exit 1"    "[[ $RC -eq 1 ]]"
check "swallowed positive: ::error::" \
  "grep -q \"selects no tracked path\" <<<\"\$OUT\""
# WATCHED_ASSETS alongside: the pathspec comparison SUPERSEDES its tree-OID
# comparison, so a list that reaches nothing under it leaves it unverified.
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED="$RISK_WATCHED" \
  WATCHED_ASSETS="$RISK_TOOLS" WATCHED_PATHSPECS=".github/workflows/pr-risk.yml"
check "assets unreached: exit 1"      "[[ $RC -eq 1 ]]"
check "assets unreached: ::error::" \
  "grep -q \"selects nothing under WATCHED_ASSETS\" <<<\"\$OUT\""
# `!path` is the `paths:` filter's negation, not git's — the spelling a
# maintainer told to MIRROR that filter pastes. git reads the `!` literally, so
# the entry would exclude nothing, match nothing, and still satisfy the
# all-negative guard.
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED="$RISK_WATCHED" \
  WATCHED_PATHSPECS=$'.github/workflows/pr-risk.yml\nscripts/pr-risk\n!scripts/pr-risk/tests'
check "bang negation: exit 1"         "[[ $RC -eq 1 ]]"
check "bang negation: ::error::"      "grep -q \"starts with '!'\" <<<\"\$OUT\""
check "bang negation: names the fix" \
  "grep -q \":(exclude)scripts/pr-risk/tests\" <<<\"\$OUT\""
# Whole-line `#` comments are dropped, so the fleet's commented `paths:` filter
# can be pasted verbatim: same verdict as the plain list above. Without that,
# every comment line would be a positive matching nothing — i.e. the error two
# cases up, on a paste the docs invite.
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED="$RISK_WATCHED" \
  WATCHED_PATHSPECS=$'# the reusable itself\n.github/workflows/pr-risk.yml\n# the grading logic callers execute\nscripts/pr-risk\n# ...but not its own tests or docs\n:(exclude)scripts/pr-risk/tests\n:(exclude)scripts/pr-risk/README.md'
check "commented paste: proceed=true" "[[ \"$P\" == \"true\" ]]"
check "commented paste: re-pointed"   "[[ \"$N\" == \"$TIP\" ]]"
check "commented paste: no ::error::" "! grep -q \"::error::\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case exec_gone_tip 'WATCHED_EXEC: an executed file deleted at the tip is decommissioned'
# The directory OUTLIVES the scripts: deleting the graders leaves `tests/` and
# the tool README behind, so `scripts/pr-risk` still exists and a directory
# probe reports the surface healthy. Probe the executed files instead — and
# report it as a decommission, not as a stale re-run, because the ::warning:: is
# the fleet's only chance to say live callers are about to hard-fail.
seed_pr_risk
BEHIND=$(work_head)
git -C "$SRC" rm -q "${RISK_TOOLS}/grade-targets.sh"
push_src 'delete one grader, leave tests/ and the README'
check "the tool dir survives at the tip" \
  "[[ -n \"\$(git -C \"$ORIGIN\" ls-tree main -- \"$RISK_TOOLS\")\" ]]"
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" \
  WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS="$RISK_PATHSPECS" WATCHED_EXEC="$RISK_EXEC"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "::warning:: names the file" \
  "grep -q \"::warning::${RISK_TOOLS}/grade-targets.sh no longer exists on main\" <<<\"\$OUT\""
check "not reported as stale"         "! grep -q \"stale run/re-run\" <<<\"\$OUT\""
# Without WATCHED_EXEC the same deletion is just another watched change: the run
# skips green as a stale re-run and nothing warns that the graders are gone.
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" \
  WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS="$RISK_PATHSPECS"
check "without it: no ::warning::"    "! grep -q \"::warning::\" <<<\"\$OUT\""
check "without it: silent stale"      "grep -q \"stale run/re-run\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case exec_gone_local 'WATCHED_EXEC: an executed file absent in this run own tree'
# The deleting commit is usually the tip itself, so none of the "main moved"
# comparisons run at all and the LOCAL probe is the only guard left. A `-d
# scripts/pr-risk` probe passes here — the directory still holds tests/ and the
# README — and would bump every caller onto a SHA where the graders are gone.
seed_pr_risk
git -C "$SRC" rm -q "${RISK_TOOLS}/resolve-enabled.sh"
push_src 'delete an executed grader at the tip'
clone_work
TIP=$(origin_tip)
check "the tool dir survives locally" "[[ -d \"${WORKDIR}/${RISK_TOOLS}\" ]]"
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" \
  WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS="$RISK_PATHSPECS" WATCHED_EXEC="$RISK_EXEC"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "::warning:: names the file" \
  "grep -q \"::warning::${RISK_TOOLS}/resolve-enabled.sh absent at this SHA\" <<<\"\$OUT\""
# The directory probe this replaces bumps the fleet onto that SHA.
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" \
  WATCHED="$RISK_WATCHED" WATCHED_ASSETS="$RISK_TOOLS"
check "a -d probe would have bumped"  "[[ \"$P\" == \"true\" ]]"

# ---------------------------------------------------------------------------
new_case exec_deleted_here 'WATCHED_EXEC: a deletion by THIS run own commit is attributed to it'
# Same verdict as exec_gone_tip — decommissioned, no bump — but the annotation
# has to name the right SHA. WATCHED and WATCHED_ASSETS each get an explicit
# "absent at this run's own commit" branch; without the same distinction here, a
# file this run's commit deleted is reported as "no longer exists on main
# ($main_tip)" and sends an operator to a commit that never touched it.
seed_pr_risk
git -C "$SRC" rm -q "${RISK_TOOLS}/grade-targets.sh"
push_src 'this run own commit deletes a grader'
clone_work
BEHIND=$(work_head)
printf 'unrelated file, edited\n' > "${SRC}/README.md"
push_src 'a later unrelated commit, so main has moved on'
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" \
  WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS="$RISK_PATHSPECS" WATCHED_EXEC="$RISK_EXEC"
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=false"                 "[[ \"$P\" == \"false\" ]]"
check "::warning:: names this run sha" \
  "grep -q \"::warning::${RISK_TOOLS}/grade-targets.sh is absent at this run's own commit ${BEHIND}\" <<<\"\$OUT\""
check "does not blame the main tip"   "! grep -q \"no longer exists on main\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case exec_added_at_tip 'WATCHED_EXEC: the local probe does not outlive the re-point'
# Once this run has been re-pointed, `new_sha` is the TIP — so the checkout the
# local probe reads is no longer the SHA callers get pinned to. An executed file
# added between github.sha and that tip exists at the pin target (the tip-side
# probe just proved it) and is absent here, and reading this tree would discard a
# legitimate bump over a file that is not missing where it matters. Reachable
# whenever an executed file is outside the compared surface — as here, where the
# fleet compares WATCHED alone.
seed_pr_risk
BEHIND=$(work_head)
printf '#!/usr/bin/env bash\necho grade-new v1\n' > "${SRC}/${RISK_TOOLS}/grade-new.sh"
push_src 'add a fourth grader at the tip'
TIP=$(origin_tip)
check "the new grader is absent here" "[[ ! -f \"${WORKDIR}/${RISK_TOOLS}/grade-new.sh\" ]]"
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED="$RISK_WATCHED" \
  WATCHED_EXEC="${RISK_EXEC}"$'\nscripts/pr-risk/grade-new.sh'
check "exit 0"                        "[[ $RC -eq 0 ]]"
check "proceed=true"                  "[[ \"$P\" == \"true\" ]]"
check "new_sha re-pointed to the tip" "[[ \"$N\" == \"$TIP\" ]]"
check "no false decommission"         "! grep -q \"::warning::\" <<<\"\$OUT\""
# The local probe still guards the case it exists for — the deleting commit IS
# the tip, so nothing was re-pointed and this checkout is the pin target. That is
# exec_gone_local above; assert here that the skip is conditional, not blanket.
clone_work
git -C "$SRC" rm -q "${RISK_TOOLS}/grade-new.sh"
push_src 'delete it again at the tip'
clone_work
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" \
  WATCHED_EXEC="${RISK_EXEC}"$'\nscripts/pr-risk/grade-new.sh'
check "not re-pointed: probe applies" "[[ \"$P\" == \"false\" ]]"
check "not re-pointed: ::warning::" \
  "grep -q \"::warning::${RISK_TOOLS}/grade-new.sh absent at this SHA\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case list_shapes 'the list inputs are shape-checked, not silently skipped'
# Every one of these shapes would otherwise make the script verify LESS than the
# entrypoint asked for, behind a green run: a set-but-blank value falls back to
# the comparison the fleet cannot use, a glob or a magic prefix in WATCHED_EXEC
# names a file that never exists (so every probe reports a decommission), and an
# all-negative pathspec is git's "everything EXCEPT these" — the widest possible
# watched surface.
seed_pr_risk
TIP=$(origin_tip)
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS=""
check "blank PATHSPECS: exit 1"       "[[ $RC -eq 1 ]]"
check "blank PATHSPECS: ::error::" \
  "grep -q \"::error::WATCHED_PATHSPECS is set but contains no entries\" <<<\"\$OUT\""
check "blank PATHSPECS: no output"    "[[ ! -s \"$OUTFILE\" ]]"
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" WATCHED_PATHSPECS=$'\n   \n'
check "whitespace PATHSPECS: exit 1"  "[[ $RC -eq 1 ]]"
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" WATCHED_EXEC=""
check "blank EXEC: exit 1"            "[[ $RC -eq 1 ]]"
check "blank EXEC: ::error::" \
  "grep -q \"::error::WATCHED_EXEC is set but contains no entries\" <<<\"\$OUT\""
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" \
  WATCHED_EXEC=$'.github/workflows/pr-risk.yml\nscripts/pr-risk/*.sh'
check "glob in EXEC: exit 1"          "[[ $RC -eq 1 ]]"
check "glob in EXEC: ::error::" \
  "grep -q \"::error::WATCHED_EXEC entry must be a literal path, not a glob\" <<<\"\$OUT\""
check "glob in EXEC: not proceed"     "[[ \"$P\" != \"true\" ]]"
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" \
  WATCHED_EXEC="scripts/pr-risk/"
check "trailing slash in EXEC: exit 1" "[[ $RC -eq 1 ]]"
check "trailing slash in EXEC: ::error::" \
  "grep -q \"must not end in a slash\" <<<\"\$OUT\""
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" \
  WATCHED_EXEC=$'scripts/pr-risk/grade-pr-risk.sh\n:(exclude)scripts/pr-risk/tests'
check "magic in EXEC: exit 1"         "[[ $RC -eq 1 ]]"
check "magic in EXEC: ::error::" \
  "grep -q \"only legal in WATCHED_PATHSPECS\" <<<\"\$OUT\""
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" \
  WATCHED_PATHSPECS=$':(exclude)scripts/pr-risk/tests\n:(exclude)scripts/pr-risk/README.md'
check "all-negative PATHSPECS: exit 1" "[[ $RC -eq 1 ]]"
check "all-negative PATHSPECS: ::error::" \
  "grep -q \"::error::WATCHED_PATHSPECS contains only\" <<<\"\$OUT\""
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" \
  WATCHED_PATHSPECS=$'scripts/pr-risk\n:!scripts/pr-risk/tests'
check "shorthand magic: exit 1"       "[[ $RC -eq 1 ]]"
check "shorthand magic: ::error::" \
  "grep -q \"unsupported pathspec magic\" <<<\"\$OUT\""
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" \
  WATCHED_PATHSPECS=$'scripts/pr-risk\n:(exclude)'
check "empty exclusion: exit 1"       "[[ $RC -eq 1 ]]"
check "empty exclusion: ::error::" \
  "grep -q \"names no path\" <<<\"\$OUT\""
# A DIRECTORY in WATCHED_EXEC is the entry whose two probes disagree: the tip
# side resolves the tree and calls it present, the local `[[ -f ]]` calls it
# absent — so the verdict would flip on whether main happened to move.
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" \
  WATCHED_EXEC=$'.github/workflows/pr-risk.yml\nscripts/pr-risk'
check "directory in EXEC: exit 1"     "[[ $RC -eq 1 ]]"
check "directory in EXEC: ::error::" \
  "grep -q \"::error::WATCHED_EXEC entry 'scripts/pr-risk' is a directory\" <<<\"\$OUT\""
check "directory in EXEC: names the right input" \
  "grep -q \"Use WATCHED_ASSETS for a directory\" <<<\"\$OUT\""
# An absolute or ../ path is the same disagreement by another route: absent from
# every tree, yet resolvable on disk OUTSIDE the checkout by the local probe.
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" \
  WATCHED_EXEC=$'.github/workflows/pr-risk.yml\n/etc/hosts'
check "absolute in EXEC: exit 1"      "[[ $RC -eq 1 ]]"
check "absolute in EXEC: ::error::"   "grep -q \"must be a repo-relative path\" <<<\"\$OUT\""
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="$RISK_WATCHED" \
  WATCHED_EXEC=$'.github/workflows/pr-risk.yml\n../outside/grade.sh'
check "../ in EXEC: exit 1"           "[[ $RC -eq 1 ]]"
check "../ in EXEC: ::error::"        "grep -q \"must be a repo-relative path\" <<<\"\$OUT\""
run_preflight GITHUB_SHA="$TIP" NEW_SHA="$TIP" WATCHED="/etc/hosts"
check "absolute WATCHED: exit 1"      "[[ $RC -eq 1 ]]"
check "absolute WATCHED: ::error::"   "grep -q \"must be a repo-relative path\" <<<\"\$OUT\""

# ---------------------------------------------------------------------------
new_case pathspec_unusable 'a pathspec git REFUSES is an error, not a verdict'
# The shape checks above are deliberately narrow (globs are legal in a pathspec,
# so most typos cannot be caught by inspection) — git is the one that decides
# whether a pathspec is usable, and it answers with exit 128, not 0 or 1. An
# absolute path is the reachable case: `git diff -- /etc` is "outside
# repository". Reading that as "unchanged" would re-point every caller to a tip
# nothing was compared at; reading it as "changed" would freeze the fleet. It
# must be neither.
seed_pr_risk
BEHIND=$(work_head)
printf 'unrelated file, edited\n' > "${SRC}/README.md"
push_src 'an unrelated commit, so the comparison branch is reached'
run_preflight GITHUB_SHA="$BEHIND" NEW_SHA="$BEHIND" WATCHED="$RISK_WATCHED" \
  WATCHED_PATHSPECS=$'scripts/pr-risk\n/etc/passwd'
check "exit 1"                        "[[ $RC -ne 0 ]]"
check "::error:: names the compare" \
  "grep -q \"::error::Could not compare the watched pathspecs\" <<<\"\$OUT\""
check "not read as stale"             "! grep -q \"stale run/re-run\" <<<\"\$OUT\""
check "not re-pointed"                "! grep -q \"pinning callers to\" <<<\"\$OUT\""
check "nothing written to output"     "[[ ! -s \"$OUTFILE\" ]]"

echo
echo "== $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
