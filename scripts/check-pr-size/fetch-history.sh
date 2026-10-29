#!/usr/bin/env bash
# Fetch the base commit, the head commit and their merge-base history into a
# `fetch-depth: 1`, `persist-credentials: false` PR checkout — instead of the
# `fetch-depth: 0` clone of every branch and tag that used to sit in front of
# check-pr-size. Shared by pr-size.yml, cursor-review.yml's `diff-size` job and
# cursor-axis-base.yml's checkout axes, so they cannot drift on how history is
# narrowed. (cursor-review adopted it to take the clone off its review round's
# critical path.)
#
# Run from the root of the PR checkout. Inputs:
#   env FETCH_TOKEN  the job's read-only token (`permissions: contents: read`)
#   env BASE_SHA     the PR's base commit
#   env HEAD_SHA     the PR's head commit
#   args             OPTIONAL extra commits whose ancestry a later step asks
#                    about (cursor-review's last-reviewed head and its recorded
#                    merge base). Best-effort: an empty, malformed or unfetchable
#                    extra is skipped, never fatal — the step that reads it
#                    already treats "not here" as "no incremental block".
#
# Fails (exit 1, with an ::error:: naming the fetch or the merge-base cause)
# when BASE_SHA/HEAD_SHA cannot be fetched or no trustworthy merge base of the
# two exists after the deepen ladder. Writes no credential to `.git/config`.
#
# Why not `filter: blob:none`: a partial clone defers blob fetches to a point
# where no credential exists to make them — the checkout is hardened with
# `persist-credentials: false` — so it works on a public repo and fails on every
# private one.
set -euo pipefail

: "${FETCH_TOKEN:?FETCH_TOKEN is required}"
: "${BASE_SHA:?BASE_SHA is required}"
: "${HEAD_SHA:?HEAD_SHA is required}"

# One-shot credential. GIT_CONFIG_COUNT/KEY/VALUE applies http.extraheader to
# the git processes this script starts and nothing else: it is never written to
# `.git/config` (so the checkout's hardening still holds while the tool runs)
# and, unlike `git -c`, it never reaches argv where another process could read
# it. Masked because the base64 form is not the literal GITHUB_TOKEN the runner
# already masks, and git echoes request headers on some transport errors.
B64="$(printf 'x-access-token:%s' "$FETCH_TOKEN" | base64 | tr -d '\n')"
echo "::add-mask::${B64}"
# SCOPED to the server, exactly as `actions/checkout` writes
# `http.https://github.com/.extraheader`. An unscoped `http.extraheader` rides
# along on every request this script's git processes make, including a redirect
# to another host — git does not strip a header injected through config the way
# curl strips its own auth. Bounded (read-only token, origin is the only
# remote), but there is no reason to leave it unscoped.
export GIT_CONFIG_COUNT=1
SERVER_URL="${GITHUB_SERVER_URL:-https://github.com}"
export GIT_CONFIG_KEY_0="http.${SERVER_URL%/}/.extraheader"
export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${B64}"

# Extras reach `git fetch` as positional refspecs, so anything that is not a
# full lower-case SHA is dropped here — a value starting with `-` would
# otherwise be parsed as an option. They come from the ledger, which already
# validates the shape; this is the second lock, not the first. A whole-string
# `=~`, not a line-oriented grep, so a multi-line value cannot pass on one
# 40-hex line; and the rejected value is never echoed, so a newline in it cannot
# smuggle a ::workflow command:: into the log.
EXTRAS=()
pos=0
for sha in "$@"; do
  pos=$((pos + 1))
  if [[ $sha =~ ^[0-9a-f]{40}$ ]]; then
    EXTRAS+=("$sha")
  elif [ -n "$sha" ]; then
    echo "Ignoring extra commit argument #${pos}: not a full 40-hex SHA." >&2
  fi
done

# Every rung's failure is recorded rather than discarded. Without this the
# ladder degrades so thoroughly that a PERMANENT problem — a force-pushed and
# GC'd base SHA, a pruned fork PR ref, a token that cannot read the repo — is
# silently relabelled as a merge-base problem by the error at the bottom,
# sending the reader to the wrong place. A transient 5xx still degrades exactly
# as before; only the final message changes.
LAST_FETCH_ERR=''
git_fetch() {
  git fetch --no-tags --no-recurse-submodules --quiet "$@" 2>&1
}
# Naming the SHAs explicitly is the whole fix: the server walks these commits
# and nothing else. GitHub allows fetching an arbitrary commit by SHA, and a
# fork PR's head is reachable in the base repo (refs/pull/N/head), so this
# covers forks too.
fetch() {
  local out
  if out="$(git_fetch "$@" origin "$BASE_SHA" "$HEAD_SHA")"; then
    # Cleared on success so the variable means "the LAST fetch failed", not
    # "some fetch failed once" — a rung that failed and then recovered must not
    # relabel a genuine merge-base failure below.
    LAST_FETCH_ERR=''
    return 0
  fi
  # Last line only: git can echo the request (headers included) on a transport
  # error, and the whole point is a one-line attribution. The base64 credential
  # is ::add-mask::ed above, so a leak through this path is masked either way.
  LAST_FETCH_ERR="$(printf '%s' "$out" | tail -n 1)"
  # Capturing stderr must not cost the log the failure itself. Plain stderr,
  # not ::warning::, so a transient rung does not annotate a check that goes on
  # to succeed.
  printf 'git fetch %s failed: %s\n' "$*" "$LAST_FETCH_ERR" >&2
  return 1
}
# Extras are fetched ONE AT A TIME and apart from BASE/HEAD: a single
# unfetchable SHA fails a whole multi-SHA `git fetch`, and an extra must never
# take the mandatory pair down with it. An extra already in the object store
# (an ancestor of HEAD, the common case) is not fetched again. One that is
# missing is fetched with an ABSOLUTE depth, and the boundaries that registers
# can land inside BASE/HEAD history; that is why this runs BEFORE the
# BASE/HEAD ladder below, which re-checks the merge base and deepens past any
# such graft.
FETCHED_EXTRAS=()
fetch_extras() {
  local sha out
  FETCHED_EXTRAS=()
  for sha in "${EXTRAS[@]+"${EXTRAS[@]}"}"; do
    if git cat-file -e "${sha}^{commit}" 2>/dev/null; then
      FETCHED_EXTRAS+=("$sha")
    elif out="$(git_fetch --depth=100 origin "$sha")"; then
      FETCHED_EXTRAS+=("$sha")
    else
      printf 'git fetch %s (extra) failed: %s\n' "$sha" "$(printf '%s' "$out" | tail -n 1)" >&2
    fi
  done
}
# `--deepen` only ever moves boundaries DEEPER, so unlike the absolute-depth
# fetch above it cannot cut into the history the merge base was proven on.
deepen_extras() {
  [ "${#FETCHED_EXTRAS[@]}" -gt 0 ] || return 0
  git_fetch "$@" origin "${FETCHED_EXTRAS[@]}" >/dev/null || true
}
is_shallow() { [ "$(git rev-parse --is-shallow-repository)" = "true" ]; }
# True when no shallow boundary is among the commits `rev-list "$@"` walks:
# nothing the graft hides can then change the answer of a question asked about
# that walk.
walk_is_complete() {
  local shallow_file walked
  is_shallow || return 0
  shallow_file="$(git rev-parse --git-path shallow)"
  # -s, not -f: an empty shallow file would make `grep -f` match nothing
  # anyway, but the early return keeps the intent explicit.
  [ -s "$shallow_file" ] || return 0
  walked="$(git rev-list "$@" 2>/dev/null)" || return 1
  # A here-string, not `printf | grep -q`: under pipefail, grep -q exiting on
  # its first match SIGPIPEs a writer still holding more than a pipe buffer of
  # the walk, and the pipeline's non-zero status would read as "no boundary" —
  # vouching for exactly the walk this exists to reject. Only exit 1 means "no
  # boundary"; a grep error (2) is not proof of anything.
  local rc=0
  grep -qxF -f "$shallow_file" <<<"$walked" || rc=$?
  [ "$rc" -eq 1 ]
}

# A merge base read off a SHALLOW graph can be WRONG, not merely missing:
# grafted boundary commits look parentless, so while the true fork point is
# still hidden `git merge-base` will happily return an older common ancestor
# that some other path does reach — a long-lived branch rooted before the fork
# point and merged into the base branch recently is enough. The tool would then
# diff from there and count every base-branch change in between, reddening a
# small PR with no error to explain it. `fetch-depth: 0` could not produce that
# false positive, so the narrowing must not either.
#
# An answer is trustworthy once every shallow boundary this walk can reach is
# itself an ancestor of that answer: everything the graft hides is then OLDER
# than the merge base, and a better (newer) common ancestor cannot be hiding in
# it. Asked in ONE pass: `rev-list BASE HEAD --not mb` IS the set of commits
# this walk reaches that are not ancestors of `mb`, so a shallow boundary
# appearing in it is exactly a boundary inside the walk and not covered by `mb`.
have_merge_base() {
  local mb
  mb="$(git merge-base "$BASE_SHA" "$HEAD_SHA" 2>/dev/null)" || return 1
  walk_is_complete "$BASE_SHA" "$HEAD_SHA" --not "$mb"
}

# The extras' ancestry questions are what cursor-review's incremental block
# asks, so the args form a CHAIN read the same way: each extra is asked whether
# it is an ancestor of the one before it, the first of HEAD (HEAD <- last
# reviewed head <- its recorded merge base). A YES from a shallow graph is
# always right (a graft only removes parents); a NO is right only once no
# boundary on the descendant's side hides a path, so an unvouched NO earns one
# bounded deepen at the end. These are for PARITY with a full clone, not for
# safety: the step that reads the answer fails closed to "no incremental block"
# on a NO either way. A link whose extra did not fetch is not asked.
have_extras() {
  local prev="$HEAD_SHA" a
  for a in "${EXTRAS[@]+"${EXTRAS[@]}"}"; do
    if ! git cat-file -e "${a}^{commit}" 2>/dev/null; then
      prev=''
      continue
    fi
    if [ -n "$prev" ] && ! git merge-base --is-ancestor "$a" "$prev" 2>/dev/null; then
      walk_is_complete "$prev" --not "$a" || return 1
    fi
    prev="$a"
  done
  return 0
}

# The first rung is mandatory, but no more fatal than the deepen rungs below
# it: under `set -e` a transient network or 5xx failure here would exit and
# redden the check, when a retry — or the fallback further down, which fetches
# the same two SHAs — recovers. Retry once, then let the ladder carry it.
fetch --depth=100 || { sleep 3; fetch --depth=100; } || true
fetch_extras

# Deepen until the merge base is trustworthy. Only the BASE/HEAD pair drives
# this ladder and the fallback below: the extras are advisory, and letting an
# unvouched NO about them (the answer after every rebase) climb every rung and
# unshallow the repo would put the full-history cost back on the critical path
# for an answer its reader treats the same either way. The distance to the fork
# point is unknown — a monorepo's base branch can be far ahead of it — and a
# shallow graft hides the ancestry `merge-base` walks, so guessing one depth is
# not an option. Each rung is incremental, so the cost is the depth actually
# needed rather than the sum of the rungs, and the loop stops the moment the
# repo stops being shallow (deepening past the root cannot help).
for extra in 400 1600 6400; do
  have_merge_base && break
  is_shallow || break
  # A rung that errors is NOT fatal — and must not skip the rest of the ladder
  # either. `continue` retries at the next depth; only a genuinely exhausted
  # ladder should reach the fallback below, which is the most expensive git
  # operation here.
  fetch "--deepen=${extra}" || continue
done

# Last resort: complete history — but still only for the named commits, which is
# the part `fetch-depth: 0` got wrong. `--unshallow` refuses to run on an
# already-complete repo, hence the guard.
if ! have_merge_base && is_shallow; then
  fetch --unshallow || fetch --depth=2147483647 || true
fi

# Hard-fail rather than let the tool report `could not resolve merge base` from
# steps away — or, worse, count a diff from a merge base the shallow graph
# cannot vouch for. Only the BASE/HEAD pair is fatal; an extra the graph still
# cannot vouch for here only costs an incremental block, which its reader
# already declines on a NO.
if ! have_merge_base; then
  if [ -n "$LAST_FETCH_ERR" ]; then
    echo "::error::could not fetch the history of ${BASE_SHA} and ${HEAD_SHA} — the last git fetch failed: ${LAST_FETCH_ERR}. A base SHA that was force-pushed and garbage-collected, a pruned fork PR ref, or a token without read access all land here; this is a fetch failure, not a merge-base one."
  else
    echo "::error::could not resolve a trustworthy merge base of ${BASE_SHA} and ${HEAD_SHA} after fetching both commits' history"
  fi
  exit 1
fi
# The extras get ONE bounded deepen, after the merge base is settled and only
# when their own answer is the one still unvouched.
if ! have_extras && is_shallow; then
  deepen_extras --deepen=400
fi
if ! have_extras; then
  echo "::warning::the ancestry of the last-reviewed commit(s) could not be fully resolved — the incremental block may be skipped this round."
fi
