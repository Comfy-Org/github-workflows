#!/usr/bin/env bash
#
# Contract test: every bump-*-callers.yml entrypoint's `paths:` filter must match
# the WATCHED / WATCHED_ASSETS it hands preflight.sh, EXACTLY.
#
# Why this exists. preflight.sh's re-point (pin callers to the verified main tip
# rather than to this run's github.sha) is only sound when the surface it
# compared covers every entry in the fleet's trigger filter — see the COUPLED TO
# THE PATH FILTER note there. Both directions of a mismatch are silent and both
# are bad:
#
#   * inputs NARROWER than the filter → the re-point pins callers to a tip whose
#     other filtered content was never compared;
#   * inputs WIDER than the filter → a commit touching only the extra path
#     starts no run of its own but does change the compared tree, so this run
#     skips green as a "stale re-run" waiting on a run that will never exist,
#     freezing the fleet.
#
# Until this test, the only guard on those seven hand-written pairs was a
# checklist line in the README: test_preflight.sh drives synthetic fixtures and
# never reads the entrypoints. This reads the real files.
#
# The parser is deliberately strict — a file whose shape it cannot read FAILS
# rather than passing silently, because a contract test that quietly matches
# nothing is worse than no test at all.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
WORKFLOWS="${REPO_ROOT}/.github/workflows"
PREFLIGHT="${SCRIPT_DIR}/../preflight.sh"

# The bump step's name, read OUT OF preflight.sh rather than restated here — it is
# the discriminator the owed-bump probe (BE-10008) keys on to tell a run that
# really bumped from one that declined, and two copies of it would be free to
# drift in exactly the direction nothing else can see.
OWED_STEP_NAME="$(sed -n "s/^OWED_BUMP_STEP_NAME='\(.*\)'$/\1/p" "$PREFLIGHT")"

PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
# A skip counts as NEITHER. It is printed because an unmeasured exclusion that
# leaves no line behind is indistinguishable from one that measured clean, and
# counting it as a pass would let a widening skip rule inflate the pass total
# while asserting less and less.
skip() { echo "  skip: $1"; }

# --- parsers -----------------------------------------------------------------

# The `paths:` list under the `push:` trigger, one entry per line, comments and
# quotes stripped. Anchored to `^  push:` so a `pull_request:` filter in the same
# file can never be the block we read.
parse_push_paths() { # $1 = workflow file
  awk '
    /^  push:/        { inpush = 1; next }
    /^  [a-z_]+:/     { inpush = 0; inpaths = 0 }
    inpush && /^    paths:/ { inpaths = 1; next }
    inpush && /^    [a-z_]+:/ { inpaths = 0 }
    inpaths && /^      #/ { next }
    inpaths && /^      - / {
      v = substr($0, 9)
      gsub(/^[ \t]+|[ \t]+$/, "", v)
      gsub(/^['"'"'"]|['"'"'"]$/, "", v)
      print v
      next
    }
    inpaths && /^[^ ]/ { inpaths = 0 }
  ' "$1"
}

# A `KEY: value` from inside the Preflight step block only — so an identically
# named env on some other step cannot answer for it.
#
# Two shapes are accepted, and the output is the same either way: one entry per
# line, so a caller splits on newlines regardless.
#
#   KEY: value          → one line
#   KEY: |              → a LITERAL BLOCK SCALAR: every following line indented
#     a                   deeper than the key, until the indentation returns.
#     b                   This is how a multi-asset fleet spells WATCHED_ASSETS.
#
# `KEY:` with nothing after it and no indented block yields nothing — the caller
# treats that as unparsed and fails, which is the point: a shape this cannot read
# must never pass silently (a contract test that quietly matches nothing is worse
# than no test at all). Anything else — including a FOLDED `>` scalar — falls
# through as a plain value and mismatches loudly against the `paths:` filter.
#
# ONLY `|` IS A BLOCK INDICATOR HERE, and its modifier is at most one chomping
# indicator plus one 1-9 indentation digit, because both looser spellings would
# make this test certify a config the runtime reads DIFFERENTLY:
#
#   * `>` folds its lines into ONE space-joined string, so preflight.sh receives
#     `.github/cursor-review scripts/check-pr-size` as a single entry that
#     resolves to nothing (a silent proceed=false that freezes the fleet) while
#     splitting on newlines here would show two correct entries and pass.
#   * `|0` / `|++` / `|12` are not valid block headers at all, so GitHub cannot
#     parse the workflow — worse than a mis-comparison, and certifiable under a
#     `[0-9+-]*` modifier pattern.
#
# Block CONTENT is taken literally, `#` lines included: a YAML block scalar has
# no comment syntax, so such a line really is a watched path as far as
# preflight.sh is concerned (where validate_path rejects it with an ::error::).
# Stripping it here would hide that shape behind a green contract test.
parse_preflight_env() { # $1 = workflow file, $2 = key
  awk -v key="$2" '
    /^      - name: Preflight/ { instep = 1; next }
    instep && /^      - name: / { instep = 0 }
    inblock {
      # Blank lines are part of the block; anything indented at or below the
      # key ends it.
      if ($0 ~ /^[ \t]*$/) next
      match($0, /^[ \t]*/)
      if (RLENGTH <= keyindent) { exit }
      else {
        v = $0
        gsub(/^[ \t]+|[ \t]+$/, "", v)
        print v
        next
      }
    }
    instep && !inblock {
      line = $0
      sub(/^[ \t]+/, "", line)
      if (index(line, key ":") == 1) {
        v = substr(line, length(key) + 2)
        # Strip a trailing YAML comment. On the KEY line (unlike inside a block)
        # ` # …` really is a comment, and the bump-callers README hands
        # maintainers exactly that spelling — `WATCHED_ASSETS: .github/groom
        # # omit for a single-path fleet` — so not stripping it makes this test
        # fail on a shape the docs invite. Watched paths never contain a space,
        # so requiring leading whitespace before the `#` is unambiguous.
        sub(/[ \t]+#.*$/, "", v)
        gsub(/^[ \t]+|[ \t]+$/, "", v)
        # A literal block indicator (with an optional chomping/indent modifier)
        # means the value is the indented lines that follow, not this line.
        if (v ~ /^[|]([1-9][+-]?|[+-][1-9]?)?$/) {
          match($0, /^[ \t]*/)
          keyindent = RLENGTH
          inblock = 1
          next
        }
        gsub(/^['"'"'"]|['"'"'"]$/, "", v)
        print v
        exit
      }
    }
  ' "$1"
}

has_preflight() { grep -q '^      - name: Preflight' "$1"; }

# `.github/groom/**` → `.github/groom`; a bare file path is returned unchanged.
# This is the same literal-path shape preflight.sh's validate_path enforces.
normalize_glob() { local v="$1"; printf '%s' "${v%/\*\*}"; }

# The same `x/**` → `x` reduction, applied to the PATH INSIDE an `:(exclude)`
# entry and to nothing else. Keeps the two equivalent spellings of a
# directory-wide exclusion comparing equal without touching a file glob.
#
# Like the positive side, this conflates a BARE `x` with `x/**` — so a filter
# that negates a bare directory (`!x/tests`, which in an Actions filter matches
# only a FILE at that exact path, not the subtree) reads as equivalent to
# `:(exclude)x/tests/**`, which is not. That filter entry is already wrong on its
# own terms, and normalize_glob has conflated the two spellings for positives
# since BE-6476; the alternative — normalizing one side only — would fail
# pr-risk's documented, correct config, which is the worse trade.
normalize_exclusion() { # $1 = a pathspec entry
  local v="$1"
  case "$v" in
    ':(exclude)'*) printf ':(exclude)%s' "$(normalize_glob "${v#:(exclude)}")" ;;
    *) printf '%s' "$v" ;;
  esac
}

# Set-compare a fleet's WATCHED_PATHSPECS against its `paths:` filter.
#   $1  = the parsed WATCHED_PATHSPECS value (newline-separated)
#   $2… = the filter's POSITIVE entries, already normalized, then its NEGATIVE
#         (`!…`) entries verbatim — the caller splits them because the two halves
#         translate differently.
# Prints a two-line diagnostic and returns 1 on any mismatch; returns 0 on an
# exact match. Extracted from the loop below so the self-test can drive it with
# fixtures: the real entrypoints are all CORRECT by construction, so nothing in
# this file would otherwise exercise a single rejection path.
compare_pathspecs() { # $1 = pathspecs, $2.. = positives then negatives
  local specs="$1"; shift
  local want_specs=() got_specs=() p s
  for p in "$@"; do
    if [[ "$p" == '!'* ]]; then
      # An exclusion keeps its glob VERBATIM apart from the ONE normalization a
      # positive also gets: a trailing `/**` is stripped, on BOTH sides, because
      # `x/**` and `x` select the same set in the filter and in git alike. That
      # is what lets `!scripts/pr-risk/tests/**` be mirrored as
      # `:(exclude)scripts/pr-risk/tests` — the spelling the README documents and
      # `bump-pr-risk-callers.yml`'s own inline guard already uses, which a
      # strictly-verbatim rule would have failed the moment pr-risk migrates onto
      # preflight (BE-6475). Nothing else is normalized: `*_test.go` carries no
      # `/**` suffix, so reducing it to the parent directory — which would widen
      # the exclusion to swallow the whole tool and the fleet would then never
      # bump — is still a mismatch, and the self-test below pins that.
      want_specs+=(":(exclude)$(normalize_glob "${p#!}")")
    else
      want_specs+=("$p")
    fi
  done
  while IFS= read -r s; do
    [[ -n "$s" ]] || continue
    # preflight.sh's split_lines drops whole-line `#` comments from
    # WATCHED_PATHSPECS (the README invites pasting the `paths:` filter "with its
    # comments intact"), while parse_preflight_env above hands the block content
    # through verbatim — deliberately, because WATCHED_ASSETS *rejects* such a
    # line and hiding it would certify a path that resolves to nothing. Read the
    # pathspec list the way its runtime reads it, or a config the README invites
    # fails here while preflight.sh accepts it — the exact two-sides-disagree the
    # contract exists to catch.
    case "$s" in '#'*) continue ;; esac
    got_specs+=("$(normalize_exclusion "$s")")
  done <<<"$specs"
  local want_sorted got_sorted
  want_sorted="$(printf '%s\n' "${want_specs[@]}" | LC_ALL=C sort)"
  got_sorted="$(printf '%s\n' "${got_specs[@]}" | LC_ALL=C sort)"
  [[ "$want_sorted" == "$got_sorted" ]] && return 0
  printf '        filter →  %s\n        pathspecs: %s' \
    "$(echo "$want_sorted" | tr '\n' ' ')" "$(echo "$got_sorted" | tr '\n' ' ')"
  return 1
}

# The one way a `!` filter entry and its `:(exclude)` mirror can be textually
# equal yet select DIFFERENT sets, measured against the real tree.
#
# A `paths:` filter's `*` does NOT cross `/`; a bare git pathspec's does (git
# matches without FNM_PATHNAME unless `:(glob)` is asked for, and preflight.sh
# rejects that magic outright). So `!x/*_test.go` and `:(exclude)x/*_test.go` —
# which compare_pathspecs pronounces equal, and which ARE equal while `x` is flat
# — diverge the moment a matching file appears in a SUBDIRECTORY of `x`: the
# filter still fires the trigger on it, while the pathspec staleness diff already
# excluded it, so the run reads "unchanged", re-points and fans a bump nothing
# asked for. That is the very churn the exclusion exists to stop, leaking back in
# one directory down. Nothing textual can catch it, so measure the tree instead.
#
# The measurement mirrors GIT, not `find -name`: git applies the pattern to the
# whole relative PATH with `*` crossing `/`, so each walked path is matched, in
# full and relative to the exclusion's directory half, by `case` — bash's `case`
# is fnmatch WITHOUT FNM_PATHNAME, the same matcher semantics git uses. Testing
# the BASENAME instead would be wrong in BOTH directions for any glob that is not
# suffix-anchored: for `!x/test_*.sh` (this repo's own shell-test convention) it
# would MISS `x/test_dir/b.sh` — which git excludes and the filter does not, a
# real divergence — and INVENT one for `x/sub/test_a.sh`, where the pathspec needs
# a literal `x/test_` prefix and the two syntaxes therefore agree.
#
# FOUR verdicts, because a refusal to measure must never read as a measurement:
#
#   0  MEASURED   — stdout carries the deep matches, one per line, `LC_ALL=C`
#                   sorted and capped at 3. EMPTY stdout is the positive
#                   assertion that the two spellings still select the same set.
#   1  EQUIVALENT — the shape provably cannot diverge this way, so there is
#                   nothing to measure:
#                     * `**` anywhere in the basename — it crosses `/` on BOTH
#                       sides, at every depth;
#                     * a literal FILE name (or a name absent from the tree) —
#                       no glob, so nothing can cross `/`.
#   2  UNMEASURED — a shape this walk cannot decide. It asserts nothing either
#                   way and SAYS so, rather than certifying a pair it never
#                   compared:
#                     * a glob in the DIRECTORY half — no single tree to walk,
#                       and git's `*` there spans levels the filter's cannot;
#                     * a directory that is not in the tree — nothing to walk;
#                     * a literal DIRECTORY — `!x/tests` matches only a file
#                       named exactly that while `:(exclude)x/tests` drops the
#                       whole subtree, and the filter half of that cannot be
#                       executed here. Spell it `!x/tests/**` and it is
#                       EQUIVALENT by the `**` rule above.
#   3  ERROR      — the walk itself failed. Never mistake it for a clean result.
glob_exclusion_deep_matches() { # $1 = repo root, $2 = negation without the leading `!`
  # `matches` is a newline-joined STRING, not an array: this suite is run locally
  # with bash 3.2, where an empty array under `set -u` is the fatal this file
  # already works around twice below, and the caller wants a string anyway.
  local root="$1" neg="$2" negbase negdir out p rc=0 matches="" n=0
  negbase="${neg##*/}"
  # A bare entry with no `/` is anchored at the repo ROOT, which is a tree like
  # any other rather than a special case: `!*_test.go` there has exactly the
  # divergence measured below, since the filter's `*` holds it to the root while
  # git's reaches every depth.
  if [[ "$neg" == */* ]]; then negdir="${neg%/*}"; else negdir="."; fi

  # Both UNMEASURED checks run BEFORE the basename rules, and that order is
  # load-bearing. The `**` rule below holds only for a FIXED, PRESENT directory:
  # `!x/*/tests/**` does diverge (git's `*` in the directory half spans several
  # levels, the filter's one), and a `/**` exclusion over a directory that is not
  # there at all proves nothing about the one that used to be.
  case "$negdir" in *'*'*|*'?'*|*'['*) return 2 ;; esac
  [[ -d "${root}/${negdir}" ]] || return 2

  case "$negbase" in
    # `**` ANYWHERE in the basename, not just as the whole of it: `!x/tests/**`
    # and `!x/**_test.go` alike cross `/` on both sides and select the same set at
    # every depth. It has to be spelled out, because `**` also satisfies the glob
    # test below — and it used to fall through it, so any `/**` exclusion over a
    # directory that merely HAD a subdirectory failed with a message about a file
    # glob it does not have (BE-15254).
    *'**'*) return 1 ;;
    # `?` and `[` are globs in both syntaxes too, and git's `?` crosses `/` just
    # as its `*` does (`!x/foo?bar.go` excludes `x/foo/bar.go` on the pathspec
    # side alone), so they are MEASURED rather than waved through as literals.
    *'*'*|*'?'*|*'['*) ;;
    # A literal name. A file cannot diverge; a DIRECTORY can, and is unmeasured.
    *) [[ -d "${root}/${neg}" ]] && return 2
       return 1 ;;
  esac

  # No `head` in this pipeline, so there is no SIGPIPE status to launder into a
  # verdict: the walk either succeeds whole or reports ERROR, and its stderr is
  # left VISIBLE so a failure says why. `-type f` keeps a directory whose own name
  # matches the glob out of the result — it can never make a `paths:` trigger and
  # a `git diff` pathspec disagree. `sort` makes the reported matches
  # deterministic; the cap is applied after matching, below.
  out="$(cd "${root}/${negdir}" && find . -mindepth 2 -type f -print | LC_ALL=C sort)" || rc=$?
  (( rc != 0 )) && return 3
  while IFS= read -r p; do
    p="${p#./}"
    [[ -n "$p" ]] || continue
    # `.git` is in no tree either side can see — a `paths:` filter never matches
    # inside it and `git diff` never reports it — so a match there would be a pure
    # false divergence. Only a bare root-anchored exclusion can reach it, since
    # that is the one shape whose walk starts at the repo root.
    [[ "$p" == .git/* ]] && continue
    # `$negbase` is UNQUOTED on purpose: it is the glob being applied, matched
    # against the whole relative path exactly as git would.
    # shellcheck disable=SC2254
    case "$p" in
      $negbase) matches="${matches}${p}"$'\n'; n=$((n+1)); (( n >= 3 )) && break ;;
    esac
  done <<<"$out"
  [[ -n "$matches" ]] && printf '%s' "$matches"
  return 0
}

# --- the contract ------------------------------------------------------------

shopt -s nullglob
FILES=("${WORKFLOWS}"/bump-*-callers.yml)
if (( ${#FILES[@]} == 0 )); then
  echo "FAIL: found no bump-*-callers.yml entrypoints to check — parser or layout changed"
  exit 1
fi

for path in "${FILES[@]}"; do
  file="$(basename "$path")"
  echo
  echo "== ${file} =="

  # --- the fleet's trigger filter ---
  filter=()
  while IFS= read -r line; do [[ -n "$line" ]] && filter+=("$line"); done < <(parse_push_paths "$path")
  if (( ${#filter[@]} == 0 )); then
    bad "${file}: parsed NO push paths: entries — the file's shape changed and this test would have passed vacuously"
    continue
  fi

  # No fleet is exempt. The one exemption this test ever carried was pr-risk's,
  # granted only because its excluding `paths:` filter could not be expressed as
  # a tree-OID comparison; WATCHED_PATHSPECS (BE-7084) expresses exactly that,
  # pr-risk has migrated, and so has every other entrypoint. An allow-list would
  # now be a list nothing can legitimately join.
  if ! has_preflight "$path"; then
    bad "${file}: runs no preflight step — every fleet must run preflight.sh"
    continue
  fi

  # --- what it tells preflight.sh it watches ---
  watched="$(parse_preflight_env "$path" WATCHED)"
  assets="$(parse_preflight_env "$path" WATCHED_ASSETS)"
  if [[ -z "$watched" ]]; then
    bad "${file}: has a Preflight step but no WATCHED env could be parsed"
    continue
  fi

  # An excluding filter cannot be expressed as WATCHED/WATCHED_ASSETS: the tree
  # OID of the broader directory still moves when an excluded file changes, which
  # reads as "the watched surface changed" and freezes the fleet as a permanent
  # stale re-run. Until BE-6676 there was nothing that COULD express it, so this
  # test rejected the shape outright. WATCHED_PATHSPECS can — `git diff` takes
  # the exclusions verbatim — so the rule is no longer "no exclusions" but
  # "exclusions must be mirrored into WATCHED_PATHSPECS". The freeze the old
  # rejection prevented is now the missing-input case below, which still fails.
  pathspecs="$(parse_preflight_env "$path" WATCHED_PATHSPECS)"
  positives=() negatives=()
  for p in "${filter[@]}"; do
    if [[ "$p" == '!'* ]]; then negatives+=("$p"); else positives+=("$(normalize_glob "$p")"); fi
  done

  if (( ${#negatives[@]} > 0 )) && [[ -z "$pathspecs" ]]; then
    bad "${file}: runs preflight.sh and its \`paths:\` filter carries a \`!\` exclusion, but it sets no WATCHED_PATHSPECS — WATCHED/WATCHED_ASSETS compare tree OIDs, which cannot express an exclusion, so every commit touching an excluded path would freeze this fleet as a permanent stale re-run. Mirror the filter into WATCHED_PATHSPECS"
    continue
  fi

  # Set-equivalence, in BOTH directions, whenever the input is in play — a fleet
  # with no `!` that sets it anyway is held to the same standard, so the list
  # cannot quietly drift away from the filter it is required to mirror.
  # `negatives` is legitimately EMPTY here — that is exactly the no-`!` fleet that
  # sets the input anyway, the case this block exists to hold — and under `set -u`
  # bash 3.2 (macOS's /bin/bash, which this suite is run with locally) a plain
  # `"${negatives[@]}"` on an empty array is an unbound-variable FATAL that kills
  # the whole run before a single comparison. `${a[@]+"${a[@]}"}` expands to
  # nothing instead; preflight.sh guards its own empty-array expansions for the
  # same reason. `positives` cannot be empty today, but it is spelled the same way
  # so a future all-negative filter cannot reintroduce the abort.
  if [[ -n "$pathspecs" ]]; then
    if pathspec_diag="$(compare_pathspecs "$pathspecs" ${positives[@]+"${positives[@]}"} ${negatives[@]+"${negatives[@]}"})"; then
      ok "${file}: WATCHED_PATHSPECS mirrors the \`paths:\` filter, exclusions included"
    else
      bad "${file}: \`paths:\` filter and WATCHED_PATHSPECS disagree — the pathspec list MUST mirror the filter, exclusions included, or the staleness test asks a different question than the trigger
${pathspec_diag}"
    fi

    # --- and the one way the two spellings can be textually equal yet select
    # DIFFERENT sets ---------------------------------------------------------
    # glob_exclusion_deep_matches (above) carries the reasoning and the skip
    # rules; the day its precondition stops holding, this fails loudly rather
    # than the fleet silently going wrong.
    for neg in ${negatives[@]+"${negatives[@]}"}; do
      neg="${neg#!}"
      # Split again here only to NAME the parts in the messages below; the
      # function does its own splitting and the verdict is entirely its call.
      negbase="${neg##*/}"
      if [[ "$neg" == */* ]]; then negdir="${neg%/*}"; else negdir="."; fi
      # All four verdicts are reported. A skip is NOT silence: the one thing this
      # block must never do is leave a reader unable to tell an exclusion it
      # CLEARED from one it never looked at, so an unmeasured shape and a failed
      # walk each say so in their own words.
      glob_rc=0
      deep="$(glob_exclusion_deep_matches "$REPO_ROOT" "$neg")" || glob_rc=$?
      case "$glob_rc" in
        0)
          if [[ -z "$deep" ]]; then
            ok "${file}: no path matching '${negbase}' below the top level of ${negdir} — its \`!\`/\`:(exclude)\` pair still select the same set"
          else
            bad "${file}: '${negdir}' now holds a path matching '${negbase}' in a SUBDIRECTORY ($(echo "$deep" | tr '\n' ' ')), where the filter's \`!${neg}\` and the pathspec's \`:(exclude)${neg}\` stop agreeing — the filter's \`*\` does not cross \`/\` but git's does, so the trigger fires on that file while the staleness diff excludes it, and the run re-points having compared nothing that moved. Narrow the exclusion to the top level, or move those tests back up"
          fi
          ;;
        1)
          skip "${file}: '!${neg}' and \`:(exclude)${neg}\` cannot diverge this way — they select the same set at every depth, so there is nothing to measure"
          ;;
        2)
          skip "${file}: '!${neg}' is UNMEASURED — this walk cannot decide whether it and \`:(exclude)${neg}\` select the same set, so nothing here asserts that they do"
          ;;
        *)
          bad "${file}: the glob-flatness measurement for '!${neg}' FAILED (status ${glob_rc}) — it neither cleared the exclusion nor reported a divergence. A failed measurement must never read as a clean one, so this is a hard failure rather than a skip"
          ;;
      esac
    done
  fi

  expected=("${positives[@]}")

  # WATCHED_ASSETS is a LIST — one entry per line (preflight.sh parses it the
  # same way). Appending the whole value as a single element would compare a
  # two-line string against two separate filter entries and never match, so a
  # multi-asset fleet would fail this test no matter how correct it was.
  actual=("$watched")
  while IFS= read -r a; do [[ -n "$a" ]] && actual+=("$a"); done <<<"$assets"

  # Globs must have normalized away — anything left is a shape preflight.sh's
  # validate_path would reject at run time (silently verifying nothing).
  leftover=""
  for p in "${expected[@]}"; do
    case "$p" in *'*'*|*'?'*|*'['*|*/) leftover="$p" ;; esac
  done
  if [[ -n "$leftover" ]]; then
    bad "${file}: filter entry '${leftover}' does not reduce to a literal path — preflight.sh cannot watch it"
    continue
  fi

  exp_sorted="$(printf '%s\n' "${expected[@]}" | LC_ALL=C sort)"
  act_sorted="$(printf '%s\n' "${actual[@]}"   | LC_ALL=C sort)"

  if [[ "$exp_sorted" == "$act_sorted" ]]; then
    ok "${file}: WATCHED/WATCHED_ASSETS cover exactly the \`paths:\` filter ($(printf '%s ' "${expected[@]}"))"
  else
    bad "${file}: \`paths:\` filter and preflight inputs disagree
        paths:   $(echo "$exp_sorted" | tr '\n' ' ')
        watched: $(echo "$act_sorted" | tr '\n' ' ')"
  fi

  # --- credential ordering ---
  # preflight.sh must decide BEFORE the Cloud Code Bot token is minted, and the
  # token step must be gated on its verdict — otherwise a run that bumps nothing
  # still mints an org-wide contents/pull-requests/issues write token.
  pre_ln="$(grep -n '^      - name: Preflight' "$path" | head -1 | cut -d: -f1)"
  tok_ln="$(grep -n '^      - name: Generate Cloud Code Bot token' "$path" | head -1 | cut -d: -f1)"
  if [[ -z "$tok_ln" ]]; then
    bad "${file}: no Cloud Code Bot token step found — parser or layout changed"
  elif (( pre_ln > tok_ln )); then
    bad "${file}: mints the Cloud Code Bot token (line ${tok_ln}) BEFORE the preflight verdict (line ${pre_ln}) — a no-op run would still mint an org-wide write token"
  elif ! awk -v s="$tok_ln" 'NR > s && /^      - name: /{exit} NR > s && /steps\.preflight\.outputs\.proceed == .true./{found=1} END{exit !found}' "$path"; then
    bad "${file}: the token step is not gated on steps.preflight.outputs.proceed — it mints a write token on a run that bumps nothing"
  else
    ok "${file}: token is minted only after, and only if, preflight says proceed"
  fi

  # --- the owed-bump probe's three silent dependencies (BE-10008) ---
  # Before honoring a `Skip-caller-bump: true` trailer, preflight.sh asks whether
  # this fleet still owes a catch-up bump, by reading its own Actions run history
  # and keying on a step named EXACTLY $OWED_STEP_NAME — that step is `skipped` on
  # a declined run and `success` on a real one, which is the entire signal.
  #
  # All three of these fail SILENTLY GREEN, which is why they are asserted here
  # rather than left to convention. Rename the step, drop `actions: read`, or drop
  # the ambient `GH_TOKEN`, and the probe reads "cannot determine" forever: every
  # trailered skip on this fleet degrades into a bump. That is the SAFE direction
  # — status-quo churn, never pin drift — so no run turns red and no caller
  # breaks. Nothing else in this repo would ever notice the trailer had quietly
  # stopped working.
  if [[ -z "$OWED_STEP_NAME" ]]; then
    bad "${file}: could not read OWED_BUMP_STEP_NAME out of preflight.sh — the step-name contract cannot be checked"
  elif grep -qxF "      - name: ${OWED_STEP_NAME}" "$path"; then
    ok "${file}: names its bump step exactly '${OWED_STEP_NAME}'"
  else
    bad "${file}: has no step named exactly '${OWED_STEP_NAME}' — preflight.sh's owed-bump probe keys the whole \"did that run really bump?\" question on that name, so renaming it silently degrades every trailered skip on this fleet into a bump"
  fi

  # Workflow-level `permissions:` only. A job-level block REPLACES it wholesale,
  # so one that omitted `actions: read` would leave the probe unable to read the
  # history while this check passed on the top-level grant — assert there is no
  # second block rather than trying to reconcile two.
  if grep -q '^    permissions:' "$path"; then
    bad "${file}: declares a JOB-level permissions: block, which REPLACES the workflow-level one — this check reads only the workflow-level grant, so \`actions: read\` may be silently absent from the job that runs the preflight"
  elif awk '/^permissions:/{inp=1;next} inp && /^[^ ]/{inp=0} inp && /^  actions: read[ \t]*$/{found=1} END{exit !found}' "$path"; then
    ok "${file}: grants actions: read for the owed-bump probe"
  else
    bad "${file}: does not grant \`actions: read\` — the owed-bump probe cannot list this fleet's runs, so it reads every trailered push as indeterminate and bumps anyway"
  fi

  gh_token="$(parse_preflight_env "$path" GH_TOKEN)"
  if [[ "$gh_token" == *'github.token'* ]]; then
    ok "${file}: wires the ambient github.token into the Preflight step"
  else
    bad "${file}: the Preflight step has no \`GH_TOKEN: \${{ github.token }}\` env (parsed '${gh_token}') — the owed-bump probe's \`gh api\` calls would be unauthenticated, so it can never rule out an owed catch-up"
  fi
done

# --- parser self-test --------------------------------------------------------
# The loop above only sees the shapes the real entrypoints happen to use, so the
# parser's REJECTIONS are unexercised there — and a parser that reads a shape
# differently from preflight.sh is exactly how this test would certify a config
# the runtime misparses. These fixtures pin the divergences that matter.
echo
echo "== owed-bump step-name constant =="
if [[ -n "$OWED_STEP_NAME" ]]; then
  ok "preflight.sh defines OWED_BUMP_STEP_NAME ('${OWED_STEP_NAME}')"
else
  bad "preflight.sh no longer defines a single-quoted OWED_BUMP_STEP_NAME — the per-fleet step-name contract above degraded to a no-op"
fi

echo
echo "== parser self-test =="

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

# $1 = case name, $2 = the WATCHED_ASSETS lines (verbatim, already indented),
# $3 = expected parse output (newline-separated; "" = nothing parsed)
parser_case() {
  local name="$1" body="$2" want="$3" f="${FIXTURE_DIR}/wf.yml" got
  {
    printf '      - name: Preflight\n'
    printf '        env:\n'
    printf '          WATCHED: .github/workflows/x.yml\n'
    printf '%s\n' "$body"
    printf '      - name: Generate Cloud Code Bot token\n'
  } > "$f"
  got="$(parse_preflight_env "$f" WATCHED_ASSETS)"
  if [[ "$got" == "$want" ]]; then
    ok "parser: ${name}"
  else
    bad "parser: ${name} — got $(printf '%s' "$got" | tr '\n' '|'), want $(printf '%s' "$want" | tr '\n' '|')"
  fi
}

# The shape the cursor-review fleet actually uses.
parser_case 'a | block scalar yields one entry per line' \
'          WATCHED_ASSETS: |
            .github/cursor-review
            scripts/check-pr-size' \
'.github/cursor-review
scripts/check-pr-size'

# A single-line value is still a one-element list.
parser_case 'a single-line value yields one entry' \
'          WATCHED_ASSETS: .github/groom' \
'.github/groom'

# The README's own spelling — must not parse the comment as part of the path.
parser_case 'a trailing YAML comment is stripped from a single-line value' \
'          WATCHED_ASSETS: .github/groom   # omit for a single-path fleet' \
'.github/groom'

# A FOLDED scalar must NOT be read as a list: YAML joins those lines into one
# space-separated string, which is what preflight.sh would receive. Reading it as
# `>` (a bare, non-path value) is what makes the set-equality check below fail
# loudly instead of certifying a config the runtime resolves to nothing.
parser_case 'a folded > scalar is not honored as a block indicator' \
'          WATCHED_ASSETS: >
            .github/cursor-review
            scripts/check-pr-size' \
'>'

# Invalid block headers GitHub itself cannot parse must not be certified either.
for bad_header in '|0' '|++' '|12'; do
  parser_case "an invalid block header '${bad_header}' is not honored" \
"          WATCHED_ASSETS: ${bad_header}
            .github/cursor-review" \
"$bad_header"
done

# …while the valid modifiers still are.
for good_header in '|' '|-' '|+' '|2' '|2-' '|-2'; do
  parser_case "the valid block header '${good_header}' is honored" \
"          WATCHED_ASSETS: ${good_header}
            .github/cursor-review" \
'.github/cursor-review'
done

# A `#` line inside a block scalar is literal CONTENT, not a comment. Surfacing
# it is the point: preflight.sh's validate_path rejects it with an ::error::, so
# the two sides agree, and the set-equality check fails loudly rather than
# hiding a phantom watched path behind a green run.
parser_case 'a # line inside a block scalar is literal content, not a comment' \
'          WATCHED_ASSETS: |
            .github/cursor-review
            # scripts/check-pr-size' \
'.github/cursor-review
# scripts/check-pr-size'

# --- pathspec-equivalence self-test ------------------------------------------
# Same reasoning as the parser fixtures above, for the BE-7084 relaxation: every
# real entrypoint is correct by construction, so the loop only ever walks
# compare_pathspecs' SUCCESS path. These fixtures pin the rejections — the ones
# that matter are the two that would fail *green*, where the pathspec list and
# the filter quietly ask different questions.
echo
echo "== pathspec-equivalence self-test =="

# $1 = case name, $2 = expected verdict (ok|mismatch), $3 = WATCHED_PATHSPECS
# value, $4.. = filter entries (positives normalized, negatives verbatim)
pathspec_case() {
  local name="$1" want_verdict="$2" specs="$3"; shift 3
  local got_verdict=ok
  compare_pathspecs "$specs" "$@" >/dev/null || got_verdict=mismatch
  if [[ "$got_verdict" == "$want_verdict" ]]; then
    ok "pathspecs: ${name}"
  else
    bad "pathspecs: ${name} — got ${got_verdict}, want ${want_verdict}"
  fi
}

# The shape both fleets this ticket migrated actually use.
pathspec_case 'the pr-size shape matches' ok \
'.github/workflows/pr-size.yml
scripts/check-pr-size
:(exclude)scripts/check-pr-size/*_test.go' \
  '.github/workflows/pr-size.yml' 'scripts/check-pr-size' '!scripts/check-pr-size/*_test.go'

# Order is irrelevant — the comparison is a SET comparison, and an entrypoint
# must not be able to fail this test by listing its pathspecs in a sane order
# that happens to differ from its filter's.
pathspec_case 'order does not matter' ok \
':(exclude)scripts/check-pr-size/*_test.go
scripts/check-pr-size
.github/workflows/pr-size.yml' \
  '.github/workflows/pr-size.yml' 'scripts/check-pr-size' '!scripts/check-pr-size/*_test.go'

# THE case this relaxation exists to keep catching. Dropping the exclusion from
# the pathspec list leaves the trigger excluding `*_test.go` while the staleness
# test still compares it — a test-only commit then starts no run, and the next
# real run reads the surface as changed and skips. Silent, and green.
pathspec_case 'a dropped exclusion is caught' mismatch \
'.github/workflows/pr-size.yml
scripts/check-pr-size' \
  '.github/workflows/pr-size.yml' 'scripts/check-pr-size' '!scripts/check-pr-size/*_test.go'

# The other direction: an exclusion the filter does not have. The staleness test
# would then ignore a path the trigger fires on, so the run that commit starts
# re-points every caller having compared nothing that moved.
pathspec_case 'an extra exclusion is caught' mismatch \
'.github/workflows/pr-size.yml
scripts/check-pr-size
:(exclude)scripts/check-pr-size/*_test.go
:(exclude)scripts/check-pr-size/README.md' \
  '.github/workflows/pr-size.yml' 'scripts/check-pr-size' '!scripts/check-pr-size/*_test.go'

# A missing POSITIVE is the classic under-verification: the list no longer covers
# the reusable workflow itself, so a commit touching only it reads as unchanged.
pathspec_case 'a missing positive is caught' mismatch \
'scripts/check-pr-size
:(exclude)scripts/check-pr-size/*_test.go' \
  '.github/workflows/pr-size.yml' 'scripts/check-pr-size' '!scripts/check-pr-size/*_test.go'

# The exclusion's glob is kept VERBATIM on both sides. Normalizing `*_test.go`
# away the way a positive `x/**` is normalized would widen the exclusion to the
# whole tool directory — the fleet would then never bump for any change at all —
# so the two spellings must NOT compare equal.
pathspec_case 'an exclusion normalized to its parent dir is caught' mismatch \
'.github/workflows/pr-size.yml
scripts/check-pr-size
:(exclude)scripts/check-pr-size' \
  '.github/workflows/pr-size.yml' 'scripts/check-pr-size' '!scripts/check-pr-size/*_test.go'

# A `!` entry written into the list as-is, rather than translated to git's
# `:(exclude)` magic, is not an exclusion to git at all — it is a literal path
# named `!…`, which matches nothing.
pathspec_case 'a raw ! entry is not accepted as an exclusion' mismatch \
'.github/workflows/pr-size.yml
scripts/check-pr-size
!scripts/check-pr-size/*_test.go' \
  '.github/workflows/pr-size.yml' 'scripts/check-pr-size' '!scripts/check-pr-size/*_test.go'

# A fleet with no exclusions at all is still held to equivalence when it sets the
# input — that is what stops the list drifting once it exists.
pathspec_case 'a non-excluding fleet still has to match' mismatch \
'.github/workflows/groom.yml' \
  '.github/workflows/groom.yml' '.github/groom'

# The call site above reaches compare_pathspecs for such a fleet with an EMPTY
# `negatives` array, which under `set -u` on bash 3.2 aborts the entire run
# rather than failing one case. `pathspec_case` cannot reproduce that (its args
# are already flattened), so drive the guarded expansion itself: on an
# unprotected `"${empty[@]}"` this subshell dies and the case reports failure.
empty_negs=()
if guard_out="$(compare_pathspecs '.github/workflows/groom.yml
.github/groom' '.github/workflows/groom.yml' '.github/groom' ${empty_negs[@]+"${empty_negs[@]}"} 2>&1)"; then
  ok "pathspecs: an empty negatives array expands to nothing (bash 3.2 \`set -u\`)"
else
  bad "pathspecs: an empty negatives array did not expand cleanly — ${guard_out:-compare_pathspecs returned mismatch}"
fi

# --- the two spellings of a DIRECTORY-wide exclusion ---
# `!x/**` and `:(exclude)x` select the same set in the filter and in git alike,
# and the second is what the README documents and pr-risk's inline guard already
# uses. Holding the negation strictly verbatim would have failed that documented
# config the moment pr-risk migrates onto preflight (BE-6475) — a test failure on
# a CORRECT config, which is the worst kind.
pathspec_case "pr-risk's documented directory exclusion matches its \`!x/**\` filter" ok \
'.github/workflows/pr-risk.yml
scripts/pr-risk
:(exclude)scripts/pr-risk/tests
:(exclude)scripts/pr-risk/README.md' \
  '.github/workflows/pr-risk.yml' 'scripts/pr-risk' \
  '!scripts/pr-risk/tests/**' '!scripts/pr-risk/README.md'

# The `/**` spelling of that same exclusion is equally correct, so it passes too
# — the normalization is applied to BOTH sides, not just the filter's.
pathspec_case 'the /** spelling of a directory exclusion also matches' ok \
'.github/workflows/pr-risk.yml
scripts/pr-risk
:(exclude)scripts/pr-risk/tests/**' \
  '.github/workflows/pr-risk.yml' 'scripts/pr-risk' '!scripts/pr-risk/tests/**'

# But widening a directory exclusion to its PARENT is still caught: only the
# trailing `/**` is stripped, so this is not "normalization", it is a different
# exclusion that would swallow the whole tool.
pathspec_case 'widening a directory exclusion to its parent is caught' mismatch \
'.github/workflows/pr-risk.yml
scripts/pr-risk
:(exclude)scripts/pr-risk' \
  '.github/workflows/pr-risk.yml' 'scripts/pr-risk' '!scripts/pr-risk/tests/**'

# --- the glob-flatness guard -------------------------------------------------
# Same reasoning again, for glob_exclusion_deep_matches: the real entrypoints are
# all flat by construction, so the loop above only ever walks its CLEAN path and
# its skip paths — nothing in this file would otherwise exercise a rejection, and
# nothing would notice the guard silently classifying an exclusion wrong. These
# fixtures drive all FOUR verdicts against trees built for the purpose, and every
# one of them is non-vacuous: each fails on the implementation it replaced.
echo

GUARD_ROOT="${FIXTURE_DIR}/guard"
# `deep`: a `/**`-excluded directory that HAS a subdirectory (the BE-15254 shape
# the old guard mis-reported), plus a `*_test.go` one directory down (the real
# divergence) and a literal README to stand in for a non-glob exclusion.
# `flat`: the same exclusion with nothing below the top level.
# `prefix`: the two halves of the basename-vs-path bug — `test_dir/b.sh`, which
# git's `:(exclude)x/test_*.sh` DOES exclude while the filter does not (a real
# divergence a basename test misses), and `sub/test_a.sh`, which neither excludes
# (a false divergence a basename test invents).
# `qmark`: `?` crossing `/`, the way git's fnmatch lets it.
# `many`: FOUR deep matches, so the cap on the reported list is exercised.
mkdir -p "${GUARD_ROOT}/deep/x/tests/fixtures" "${GUARD_ROOT}/deep/x/sub" \
         "${GUARD_ROOT}/flat/x" "${GUARD_ROOT}/prefix/x/test_dir" \
         "${GUARD_ROOT}/prefix/x/sub" "${GUARD_ROOT}/qmark/x/foo" \
         "${GUARD_ROOT}/many/x/sub"
# …and a `.git` holding a file that matches the glob the bare-root fixture below
# uses, which is the only shape whose walk starts high enough to see it.
mkdir -p "${GUARD_ROOT}/deep/.git"
touch "${GUARD_ROOT}/deep/.git/z_test.go"
touch "${GUARD_ROOT}/deep/x/tests/fixtures/a.txt" \
      "${GUARD_ROOT}/deep/x/tests/README.md" \
      "${GUARD_ROOT}/deep/x/sub/b_test.go" \
      "${GUARD_ROOT}/flat/x/b_test.go" \
      "${GUARD_ROOT}/prefix/x/test_dir/b.sh" \
      "${GUARD_ROOT}/prefix/x/sub/test_a.sh" \
      "${GUARD_ROOT}/qmark/x/foo/bar.go" \
      "${GUARD_ROOT}/many/x/sub/a_test.go" \
      "${GUARD_ROOT}/many/x/sub/b_test.go" \
      "${GUARD_ROOT}/many/x/sub/c_test.go" \
      "${GUARD_ROOT}/many/x/sub/d_test.go"
# `dironly`: a DIRECTORY whose own name matches the glob, BELOW the top level (at
# the top level `-mindepth 2` would skip it anyway, and the fixture would prove
# nothing). It can never make a `paths:` trigger and a `git diff` pathspec
# disagree — only files are ever compared — so it must not be reported.
mkdir -p "${GUARD_ROOT}/dironly/x/sub/sub_test.go"
touch "${GUARD_ROOT}/dironly/x/sub/keep.txt"

# $1 = case name, $2 = expected verdict
# (clean|caught|equivalent|unmeasured|error), $3 = fixture root, $4 = the negation
# with its leading `!` already stripped, $5 = for `caught`, the exact match list
# expected on stdout.
guard_case() {
  local name="$1" want="$2" root="$3" neg="$4" want_out="${5-}" out rc=0 got
  out="$(glob_exclusion_deep_matches "$root" "$neg")" || rc=$?
  # Every status is named, and an UNPLANNED one is named too — as `status-N`,
  # which no fixture ever asks for and which therefore FAILS. Folding unknown
  # statuses into `skip` is how the skip fixtures below (the BE-15254 regression
  # among them) would keep passing if this function were renamed or deleted (127),
  # or aborted under `set -u` — only the clean and caught cases would notice.
  case "$rc" in
    0) if [[ -z "$out" ]]; then got=clean; else got=caught; fi ;;
    1) got=equivalent ;;
    2) got=unmeasured ;;
    3) got=error ;;
    *) got="status-${rc}" ;;
  esac
  if [[ "$got" != "$want" ]]; then
    bad "glob guard: ${name} — got ${got}, want ${want}$( [[ -n "$out" ]] && printf ' (matched: %s)' "$(echo "$out" | tr '\n' ' ')" )"
  elif [[ "$want" == caught && "$out" != "$want_out" ]]; then
    bad "glob guard: ${name} — caught, but reported '$(echo "$out" | tr '\n' ' ')' rather than '$(echo "$want_out" | tr '\n' ' ')'"
  else
    ok "glob guard: ${name}"
  fi
}

# THE regression (BE-15254). `**` matched the guard's file-glob test and
# `find -name '**'` matches every ordinary filename, so before the explicit rule
# this returned `./fixtures/a.txt` and the fleet failed its own contract test on a
# correct config, told to "narrow the exclusion to the top level" over a file glob
# it does not have. A `/**` exclusion selects the whole subtree in BOTH syntaxes.
guard_case 'a /** directory exclusion cannot diverge, subdirectories and all' \
  equivalent "${GUARD_ROOT}/deep" 'x/tests/**'

# …and `**` need not be the WHOLE basename to cross `/`. `!x/**_test.go` matches
# `sub/b_test.go` on both sides, so it is equally equivalent — while a basename
# test for exactly `**` let it fall through and fail the same false red.
guard_case 'a basename merely CONTAINING ** cannot diverge either' \
  equivalent "${GUARD_ROOT}/deep" 'x/**_test.go'

# The divergence the guard exists to catch: a `*_test.go` one directory down,
# where the filter's `*` stops and git's does not.
guard_case 'a file glob matching below the top level is caught' \
  caught "${GUARD_ROOT}/deep" 'x/*_test.go' 'sub/b_test.go'

# The same exclusion over a FLAT directory is the assertion the real entrypoints
# make on every run — clean, and distinguishable from a skip.
guard_case 'a file glob over a flat directory measures clean' \
  clean "${GUARD_ROOT}/flat" 'x/*_test.go'

# Clean as well one directory DOWN, when the only thing matching the glob there is
# a directory: that is why the walk is `-type f`. Matched as a path, `sub/sub_test.go`
# would be reported as a divergence, and no directory can make a `paths:` trigger
# and a `git diff` pathspec disagree.
guard_case 'a matching DIRECTORY below the top level is not a divergence' \
  clean "${GUARD_ROOT}/dironly" 'x/*_test.go'

# The pattern is matched against the PATH, not the basename — both halves of that
# bug in one fixture. `test_dir/b.sh` is the real divergence a basename test
# MISSES (`b.sh` never matches `test_*.sh`, yet git's `*` spans `dir/b` and
# excludes it); `sub/test_a.sh` is the false one it INVENTS (its basename matches,
# but the pathspec needs a literal `x/test_` prefix, so both syntaxes agree). Only
# the first may be reported.
guard_case 'a prefix-anchored glob is measured against the path, not the basename' \
  caught "${GUARD_ROOT}/prefix" 'x/test_*.sh' 'test_dir/b.sh'

# `?` is a glob in both syntaxes and crosses `/` in git's, so an exclusion
# carrying one is a measurement, not a literal filename to wave through.
guard_case 'a ? glob crosses / in git and is measured, not treated as a literal' \
  caught "${GUARD_ROOT}/qmark" 'x/foo?bar.go' 'foo/bar.go'

# The reported list is capped at 3 and sorted, so a tree with four divergences
# still names three of them deterministically rather than flooding the failure.
guard_case 'more than three deep matches are capped, in sorted order' \
  caught "${GUARD_ROOT}/many" 'x/*_test.go' 'sub/a_test.go
sub/b_test.go
sub/c_test.go'

# A literal FILE name carries no glob, so nothing can cross `/` and the two
# syntaxes cannot disagree about it.
guard_case 'a literal filename exclusion cannot diverge' \
  equivalent "${GUARD_ROOT}/deep" 'x/tests/README.md'

# A literal DIRECTORY is the opposite: `!x/tests` matches only a file named
# exactly that while `:(exclude)x/tests` drops the whole subtree, so it is NOT
# equivalent — and the filter half of that cannot be executed here, so the honest
# verdict is UNMEASURED rather than a certificate. (`!x/tests/**` is the spelling
# that IS equivalent, and the README documents that one.)
guard_case 'a literal directory exclusion is unmeasured, not certified equal' \
  unmeasured "${GUARD_ROOT}/deep" 'x/tests'

# A glob in the DIRECTORY half is unmeasured too — there is no single tree to
# walk, and git's `*` there spans levels the filter's cannot, so the `**` rule
# must NOT reach this shape: `!x/*/tests/**` genuinely can diverge.
guard_case 'a glob in the directory half is unmeasured, ** basename or not' \
  unmeasured "${GUARD_ROOT}/deep" 'x/*/tests/**'

# An absent directory is unmeasured, checked BEFORE the `**` rule — otherwise the
# regression fixture above would go on passing after its tree disappeared, having
# stopped exercising the `**` rule at all.
guard_case 'an absent directory is unmeasured, not a vacuous ** skip' \
  unmeasured "${GUARD_ROOT}/deep" 'x/gone/**'

# A bare entry with no `/` is anchored at the tree ROOT and measured there — the
# filter holds its `*` to the root while git's reaches every depth, which is the
# same divergence one level up. It is also the only shape that walks over `.git`,
# and `.git/z_test.go` must NOT be among the matches: nothing in there is visible
# to a `paths:` filter or to `git diff`, so reporting it would be a false red.
guard_case 'a bare root-level glob is measured against the tree root, .git aside' \
  caught "${GUARD_ROOT}/deep" '*_test.go' 'x/sub/b_test.go'

# A walk that FAILS must never read as a clean measurement: an unreadable subtree
# is the ERROR verdict, which the fleet loop turns into a hard failure rather than
# a skip. The `cd: … Permission denied` this prints on stderr is the other half of
# the point — the walk's diagnostic is no longer swallowed by a `2>/dev/null` that
# left an empty result looking like a clean one. Skipped as root, for whom the mode
# bits are advisory.
if [[ "$(id -u)" != 0 ]]; then
  mkdir -p "${GUARD_ROOT}/unreadable/x/sub"
  touch "${GUARD_ROOT}/unreadable/x/sub/e_test.go"
  chmod 000 "${GUARD_ROOT}/unreadable/x"
  guard_case 'a walk that cannot run reports ERROR, never clean' \
    error "${GUARD_ROOT}/unreadable" 'x/*_test.go'
  # Restore the mode so the EXIT trap's `rm -rf` can take the tree back out.
  chmod 755 "${GUARD_ROOT}/unreadable/x"
else
  skip "glob guard: the ERROR verdict is not exercised as root (mode bits advisory)"
fi

# --- comments inside the pathspec block ---
# preflight.sh's split_lines drops whole-line `#` comments from
# WATCHED_PATHSPECS, and the README invites pasting the `paths:` filter in "with
# its comments intact". Reading them as literal pathspecs here would fail a
# config the runtime accepts — the two sides disagreeing, which is the one thing
# this test exists to prevent. (WATCHED_ASSETS is the opposite case and stays
# opposite: preflight REJECTS a `#` line there, and the parser fixture above
# pins that it is passed through.)
pathspec_case 'a commented pathspec block matches the filter it mirrors' ok \
'# MIRRORS the paths: filter, exclusions included.
.github/workflows/pr-size.yml
scripts/check-pr-size
# ...minus the Go tests, which no pinned caller executes.
:(exclude)scripts/check-pr-size/*_test.go' \
  '.github/workflows/pr-size.yml' 'scripts/check-pr-size' '!scripts/check-pr-size/*_test.go'

# A trailing `#` is a legal filename character and is left alone — same rule as
# split_lines, so an entry ending in one still has to be mirrored.
pathspec_case 'only WHOLE-LINE comments are dropped' mismatch \
'.github/workflows/pr-size.yml
scripts/check-pr-size # not a comment' \
  '.github/workflows/pr-size.yml' 'scripts/check-pr-size'

# The freeze guard's trigger condition: an entrypoint that sets no
# WATCHED_PATHSPECS must parse as EMPTY, which is what makes the
# `!`-without-pathspecs branch above fire rather than silently comparing nothing.
got_empty="$(parse_preflight_env <(printf '      - name: Preflight\n        env:\n          WATCHED: .github/workflows/x.yml\n      - name: Generate Cloud Code Bot token\n') WATCHED_PATHSPECS)"
if [[ -z "$got_empty" ]]; then
  ok "pathspecs: an absent WATCHED_PATHSPECS parses as empty (the freeze guard fires)"
else
  bad "pathspecs: an absent WATCHED_PATHSPECS parsed as '${got_empty}' — the \`!\`-without-pathspecs freeze guard would never fire"
fi

echo
echo "== ${PASS} passed, ${FAIL} failed =="
(( FAIL == 0 ))
