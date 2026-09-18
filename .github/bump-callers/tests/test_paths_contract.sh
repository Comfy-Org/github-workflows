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
# Every WATCHED_EXEC entry seen across every fleet, for the trigger-coverage
# check after the loop.
ALL_EXEC_ENTRIES=()
# ...and every POSITIVE `paths:` entry, VERBATIM — `coverage_probe` needs the
# `/**` suffix intact to tell a watched TREE from a watched FILE. For the
# second pass of that same check. The two are different questions: WATCHED_EXEC
# names individual FILES whose disappearance freezes a fleet, while a positive
# names the whole watched SURFACE — the tree a deep file can be added to, which
# is what the glob-flatness measurement walks.
ALL_POSITIVES=()
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

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

# WATCHED_EXEC as its RUNTIME reads it. preflight.sh's split_lines drops blank
# lines and whole-line `#` comments from WATCHED_EXEC — the same allowance
# WATCHED_PATHSPECS gets, so a fleet can paste its `paths:` filter in with the
# comments intact — while parse_preflight_env above hands block content through
# verbatim. It has to: WATCHED_ASSETS *rejects* a `#` line, so stripping one
# there would certify a path that resolves to nothing. Reading WATCHED_EXEC by
# that stricter rule would go wrong in the other direction, failing a commented
# list preflight.sh accepts. One function, so the difference is deliberate and
# pinned by a fixture rather than repeated inline.
parse_exec_list() { # $1 = the parsed WATCHED_EXEC value (newline-separated)
  local e
  while IFS= read -r e; do
    # Trim BEFORE the comment test, exactly as split_lines does — otherwise an
    # indented `#` line reads as a path here and as a comment at run time.
    e="${e#"${e%%[![:space:]]*}"}"
    e="${e%"${e##*[![:space:]]}"}"
    [[ -n "$e" ]] || continue
    case "$e" in '#'*) continue ;; esac
    printf '%s\n' "$e"
  done <<<"$1"
}


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

# Does one Actions `paths:` pattern select this path? Actions' globbing is NOT
# bash's: a single `*` does not cross `/`, only `**` does. Matching with the
# shell's own `[[ $p == $glob ]]` is therefore PERMISSIVE in the one direction a
# coverage check cannot afford — a filter narrowed to `.github/*` would still
# report `.github/groom/scope.py` as covered, because bash's `*` happily eats the
# slashes Actions would stop at. So translate the pattern to an anchored ERE that
# keeps the distinction rather than leaning on the glob.
#
# Only the three wildcards Actions' filter syntax defines are translated (`**`,
# `*`, `?`); every other regex metacharacter is escaped to its literal self, so a
# `.` in a filter entry matches a `.` and nothing else.
actions_glob_match() { # $1 = pattern, $2 = path
  local g="$1" path="$2" ere='^' i=0 n=${#1} c
  while (( i < n )); do
    c="${g:i:1}"
    case "$c" in
      '*') if [[ "${g:i:2}" == '**' ]]; then ere+='.*'; i=$((i+2)); else ere+='[^/]*'; i=$((i+1)); fi ;;
      '?') ere+='[^/]'; i=$((i+1)) ;;
      # Everything else is its literal self, so any ERE metacharacter among them
      # is escaped rather than honoured.
      [.+^]|'$'|'('|')'|'{'|'}'|'|'|'['|']'|\\) ere+="\\$c"; i=$((i+1)) ;;
      *) ere+="$c"; i=$((i+1)) ;;
    esac
  done
  ere+='$'
  # Unquoted RHS: this is a REGEX, and bash 3.2 (macOS's /bin/bash, which this
  # suite is run with locally) treats a quoted one as a literal string.
  [[ "$path" =~ $ere ]]
}

# The concrete path a coverage check should probe on behalf of a watched-surface
# entry. An `X/**` entry names a TREE, and the property both passes below claim
# is that a file DEEP inside that tree fires the trigger — not that the bare
# directory name matches something. Probing the root alone gets that wrong in
# BOTH directions: `.github/groom` is selected by a `.github/*` filter that would
# never select `.github/groom/scope.py` (false green), while a fleet watching
# exactly a filter tree root reduces to `tools`, which `tools/**` does not match
# at all because the pattern needs the literal `tools/` prefix (false red, with a
# remediation message that stays red after you follow it). A descendant probe is
# right in both. Anything else — a literal file, a `*` glob inside a directory —
# names the path itself, which is already the thing whose change must trigger.
coverage_probe() { # $1 = a `paths:` entry
  case "$1" in
    '**')   printf '__probe__/__deep__/__file__' ;;
    */'**') printf '%s/__deep__/__file__' "${1%/\*\*}" ;;
    *)      printf '%s' "$1" ;;
  esac
}

# Which of $2… does a `paths:` filter select NONE of? Prints the uncovered
# entries, one per line and in the order given; returns 0 when every entry is
# covered, 1 otherwise.
#
# Extracted from the trigger-coverage block at the bottom for the same reason
# compare_pathspecs is: the real filter covers the real entries BY CONSTRUCTION,
# so nothing in this file would otherwise exercise a single rejection — a filter
# narrowed back to the entrypoints would have to reach main before anything
# noticed the check could still fail. One copy also keeps the two passes below
# (WATCHED_EXEC files, and the fleets' `paths:` positives) matching by the same
# rule rather than by two hand-copied loops free to drift apart.
uncovered_against_filter() { # $1 = filter entries (newline-separated), $2… = paths
  local patterns="$1"; shift
  local e pat probe sel rc=0 pats=()
  while IFS= read -r pat; do [[ -n "$pat" ]] && pats+=("$pat"); done <<<"$patterns"
  for e in "$@"; do
    probe="$(coverage_probe "$e")"
    # Actions reads the list IN ORDER and lets a later entry override an earlier
    # one, so a `!` takes paths back OUT of a tree an earlier positive selected
    # (and a later positive can re-include them). Reading every entry as an
    # independent positive and breaking on the first hit — which is what this
    # did — makes a `!` entry match nothing at all, so a filter that gained
    # `scripts/**` followed by `!scripts/pr-risk/**` would keep reporting that
    # surface covered: the false-green direction. Hence no `break`; the LAST
    # matching entry decides.
    sel=0
    for pat in ${pats[@]+"${pats[@]}"}; do
      if [[ "$pat" == '!'* ]]; then
        actions_glob_match "${pat#!}" "$probe" && sel=0
      else
        actions_glob_match "$pat" "$probe" && sel=1
      fi
    done
    (( sel )) || { printf '%s\n' "$e"; rc=1; }
  done
  return $rc
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

  # Split, and accumulated, HERE — above every per-fleet check that can `continue`
  # out of this iteration. A fleet whose preflight inputs are mid-edit is still a
  # fleet whose watched surface this suite's own trigger has to cover, and
  # dropping it would silently shrink the coverage check exactly when the file is
  # being changed. The `has_preflight` and empty-`WATCHED` guards below are the
  # mid-edit case itself, so this has to sit above them and not merely above the
  # rest: today both of those call `bad` first, so the run fails anyway, but a
  # future guard that merely `continue`s would shrink the coverage check in
  # silence. `ALL_POSITIVES` takes the entries VERBATIM (`compare_pathspecs`
  # needs the `normalize_glob`ed `positives`, coverage_probe needs the raw `/**`
  # to know a tree from a file). Same empty-array guard as everywhere else in
  # this file — `raw_positives` cannot be empty today, but an all-negative filter
  # would abort the whole run on bash 3.2 under `set -u` rather than fail one case.
  positives=() negatives=() raw_positives=()
  for p in "${filter[@]}"; do
    if [[ "$p" == '!'* ]]; then
      negatives+=("$p")
    else
      raw_positives+=("$p")
      positives+=("$(normalize_glob "$p")")
    fi
  done
  ALL_POSITIVES+=(${raw_positives[@]+"${raw_positives[@]}"})

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
    # A `paths:` filter's `*` does NOT cross `/`; a bare git pathspec's does
    # (git matches without FNM_PATHNAME unless `:(glob)` is asked for, and
    # preflight.sh rejects that magic outright). So `!x/*_test.go` and
    # `:(exclude)x/*_test.go` — which the comparison above pronounces equal, and
    # which ARE equal while `x` is flat — diverge the moment a matching file
    # appears in a SUBDIRECTORY of `x`: the filter still fires the trigger on it,
    # while the pathspec staleness diff already excluded it, so the run reads
    # "unchanged", re-points and fans a bump nothing asked for. That is the very
    # churn this exclusion exists to stop, leaking back in one directory down.
    # Nothing textual can catch it, so measure the tree instead: the day the
    # precondition stops holding, this fails loudly rather than the fleet
    # silently going wrong.
    for neg in ${negatives[@]+"${negatives[@]}"}; do
      neg="${neg#!}"
      negbase="${neg##*/}" negdir="${neg%/*}"
      # File globs only. A `/**` directory exclusion means "everything under
      # here" in both syntaxes at every depth, so it cannot diverge this way.
      case "$negbase" in *'*'*) ;; *) continue ;; esac
      case "$negdir" in *'*'*) continue ;; esac
      [[ "$negdir" != "$neg" && -d "${REPO_ROOT}/${negdir}" ]] || continue
      deep="$(cd "${REPO_ROOT}/${negdir}" && find . -mindepth 2 -name "$negbase" -print 2>/dev/null | head -3)"
      if [[ -z "$deep" ]]; then
        ok "${file}: no '${negbase}' below the top level of ${negdir} — its \`!\`/\`:(exclude)\` pair still select the same set"
      else
        bad "${file}: '${negdir}' now holds '${negbase}' in a SUBDIRECTORY ($(echo "$deep" | tr '\n' ' ')), where the filter's \`!${neg}\` and the pathspec's \`:(exclude)${neg}\` stop agreeing — the filter's \`*\` does not cross \`/\` but git's does, so the trigger fires on that file while the staleness diff excludes it, and the run re-points having compared nothing that moved. Narrow the exclusion to the top level, or move those tests back up"
      fi
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

  # --- WATCHED_EXEC: every named file must actually RESOLVE ------------------
  # These are hand-written literal paths — ~50 of them across ten fleets — and
  # nothing else in CI reads them. They are also the one input whose failure mode
  # is a SILENT FREEZE rather than a red run: preflight.sh probes each entry for
  # deletion, so a typo, or a rename applied to the repo but not to this list,
  # makes it take the decommission branch on every future run (one `::warning::`,
  # `proceed=false`) while the run itself stays green. The fleet quietly stops
  # bumping and consumer pins drift — the mirror of the false-healthy bump the
  # input exists to stop. Every OTHER preflight input is machine-enforced; this
  # closes the last hole.
  #
  # `git ls-files` and an EXACT match, not `[[ -f ]]`: preflight.sh resolves each
  # entry as a BLOB at the fetched tip (resolve_blob) as well as on disk, so a
  # path that exists locally but is untracked, or is ignored, is absent as far as
  # the probe is concerned. Exactness is what also rejects a DIRECTORY here —
  # `ls-files -- x` on a directory succeeds, listing what is under it — which is
  # the shape preflight.sh errors on at run time.
  #
  # This is also what keeps the two byte-identical `scripts/check-pr-size` blocks
  # (bump-pr-size-callers.yml and bump-cursor-review-callers.yml, asserted in
  # prose alone) in step: a rename applied to one list leaves the OTHER naming a
  # path that no longer resolves, and that fleet fails here.
  exec_entries=()
  while IFS= read -r e; do
    exec_entries+=("$e")
  done < <(parse_exec_list "$(parse_preflight_env "$path" WATCHED_EXEC)")

  # The rule this input now carries (BE-15253): a DIRECTORY cannot be its own
  # decommission probe, because it OUTLIVES the files inside it — `tests/` and a
  # `README.md` keep it resolving after every asset a pinned caller loads has
  # been deleted, so the probe reports the surface healthy and bumps every caller
  # onto a SHA where the scripts are gone. So any fleet watching a directory owes
  # a WATCHED_EXEC. The fleets that watch nothing beyond WATCHED are exempt:
  # preflight.sh probes WATCHED itself unconditionally, and a `.yml` file is its
  # own probe.
  watched_dirs=()
  while IFS= read -r a; do
    [[ -n "$a" ]] || continue
    [[ -d "${REPO_ROOT}/${a}" ]] && watched_dirs+=("$a")
  done <<<"$assets"

  if (( ${#exec_entries[@]} == 0 )); then
    if (( ${#watched_dirs[@]} > 0 )); then
      bad "${file}: watches the DIRECTORY $(printf '%s ' "${watched_dirs[@]}")but sets no WATCHED_EXEC — a directory outlives the files inside it (\`tests/\` and \`README.md\` keep it resolving), so the decommission probe would report the surface healthy and bump every caller onto a SHA where the scripts it loads are gone. Name the files whose absence breaks a pinned caller at run time"
    else
      ok "${file}: watches nothing beyond WATCHED, so no WATCHED_EXEC is owed"
    fi
  else
    exec_bad=""
    for e in "${exec_entries[@]}"; do
      if [[ "$(git -C "$REPO_ROOT" ls-files -z -- "$e" | tr -d '\0')" != "$e" ]]; then
        exec_bad="${exec_bad}${exec_bad:+ }${e}"
      fi
    done
    ALL_EXEC_ENTRIES+=("${exec_entries[@]}")
    if [[ -n "$exec_bad" ]]; then
      bad "${file}: WATCHED_EXEC names ${exec_bad} — not a tracked file at this commit. preflight.sh probes each entry for deletion, so this fleet takes the decommission branch on EVERY run: one \`::warning::\`, \`proceed=false\`, a green run, and no caller ever bumped again. Fix the path, or drop the entry if the file was genuinely retired"
    else
      ok "${file}: all ${#exec_entries[@]} WATCHED_EXEC entries resolve to tracked files"
    fi

    # Every entry must also sit UNDER a watched surface. One that does not is
    # never compared by the staleness test and never triggers this fleet's
    # `paths:` filter, so it can only ever contribute a freeze — a file outside
    # the filter can be deleted by a commit that starts no run of this fleet at
    # all, and the next unrelated run reads that deletion as a decommission.
    # WATCHED plus every WATCHED_ASSETS entry, whether that entry is a directory
    # or (hypothetically) a single file — an entry is covered by an exact match or
    # by sitting under one.
    surfaces=("$watched")
    while IFS= read -r a; do [[ -n "$a" ]] && surfaces+=("$a"); done <<<"$assets"
    stray=""
    for e in "${exec_entries[@]}"; do
      covered=""
      for a in "${surfaces[@]}"; do
        if [[ "$e" == "$a" || "$e" == "${a}/"* ]]; then covered=1; break; fi
      done
      [[ -n "$covered" ]] || stray="${stray}${stray:+ }${e}"
    done
    if [[ -n "$stray" ]]; then
      bad "${file}: WATCHED_EXEC names ${stray}, which is outside WATCHED and every WATCHED_ASSETS entry — nothing compares or triggers on it, so its eventual deletion lands via a commit that starts no run of this fleet and freezes it behind a \`::warning::\` on the next unrelated push. Watch the path, or drop the entry"
    else
      ok "${file}: every WATCHED_EXEC entry sits under a watched surface"
    fi
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

# --- WATCHED_EXEC list self-test ---------------------------------------------
# Every real entrypoint writes a comment-free block, so the loop above only ever
# walks parse_exec_list' pass-through path. What these fixtures pin is the ONE
# place it must diverge from the WATCHED_ASSETS parser sitting next to it — and
# the divergence is not symmetric, so getting it backwards fails in a different
# direction on each input.
echo
echo "== WATCHED_EXEC list self-test =="

exec_case() { # $1 = case name, $2 = raw value, $3 = expected entries
  local got
  got="$(parse_exec_list "$2")"
  if [[ "$got" == "$3" ]]; then
    ok "exec: $1"
  else
    bad "exec: $1 — got '$(echo "$got" | tr '\n' ' ')', want '$(echo "$3" | tr '\n' ' ')'"
  fi
}

# The shape every fleet actually uses.
exec_case 'a plain list passes through' \
'.github/workflows/groom.yml
.github/groom/ledger.py' \
'.github/workflows/groom.yml
.github/groom/ledger.py'

# THE divergence. preflight.sh drops a whole-line `#` from WATCHED_EXEC, so a
# commented list is a config the runtime accepts — reading it the strict
# WATCHED_ASSETS way would fail this test on a correct fleet, and the annotation
# would point at a "path" that is really a comment.
exec_case 'a whole-line # comment is dropped, as split_lines drops it' \
'# the briefs, loaded from the pinned ref
.github/groom/finder.md
  # indented, still a comment
.github/groom/verifier.md' \
'.github/groom/finder.md
.github/groom/verifier.md'

# ...and only a WHOLE-LINE one. `#` is a legal filename character, and
# split_lines leaves a trailing one alone, so an entry carrying one is a real
# path that still has to resolve. Dropping the tail here would hide a genuinely
# unresolvable entry behind a green run — the freeze this check exists to catch.
exec_case 'a trailing # is part of the path, not a comment' \
'.github/groom/ledger.py # not a comment' \
'.github/groom/ledger.py # not a comment'

# Blank lines are noise in a block scalar, never an entry: an empty string would
# resolve to nothing and read as a permanent decommission.
exec_case 'blank lines are not entries' \
'.github/groom/ledger.py

.github/groom/scope.py' \
'.github/groom/ledger.py
.github/groom/scope.py'

# An absent WATCHED_EXEC must parse as NO entries — that is what routes the three
# WATCHED-only fleets to the "no WATCHED_EXEC is owed" branch instead of failing
# them, and what makes the owed-WATCHED_EXEC check above fire for a directory
# fleet that sets none.
exec_case 'an absent value yields no entries' '' ''


# --- trigger-coverage self-test ----------------------------------------------
# Same reasoning as the two self-tests above: the real filter covers the real
# fleets by construction, so the block that follows only ever walks its CLEAN
# path. These fixtures drive BOTH directions through uncovered_against_filter,
# and through parse_push_paths — the same parser that reads the real files — so a
# `paths:` shape the parser stops understanding fails here too rather than
# quietly covering nothing.
echo
echo "== trigger-coverage self-test =="

# $1 = case name, $2 = expected verdict (covered|uncovered), $3 = the workflow
# whose `push:` `paths:` list is the FILTER, $4… = the fleet entrypoints whose
# positive entries must be covered by it.
cover_case() {
  local name="$1" want="$2" filter_file="$3"; shift 3
  local f p patterns out got=covered
  local positives=()
  patterns="$(parse_push_paths "$filter_file")"
  for f in "$@"; do
    while IFS= read -r p; do
      [[ -n "$p" && "$p" != '!'* ]] && positives+=("$p")
    done < <(parse_push_paths "$f")
  done
  # A fixture that parsed nothing asserts nothing — and would report `covered`,
  # the passing verdict, which is the one failure mode a coverage check must
  # never have.
  if (( ${#positives[@]} == 0 )); then
    bad "coverage: ${name} — parsed NO positives out of the fleet fixture(s), so this case asserts nothing"
    return
  fi
  # The mirror guard, for the same reason in the other direction: with no
  # patterns at all every entry reports uncovered, so an `uncovered` fixture
  # would pass for entirely the wrong reason — a typo'd filter_file path, or a
  # `paths:` shape parse_push_paths stops understanding, both read as a clean
  # rejection instead of a broken test.
  if [[ -z "$patterns" ]]; then
    bad "coverage: ${name} — parsed NO patterns out of the filter fixture, so an \`uncovered\` verdict would be vacuous"
    return
  fi
  out="$(uncovered_against_filter "$patterns" "${positives[@]}")" || got=uncovered
  if [[ "$got" == "$want" ]]; then
    ok "coverage: ${name}"
  else
    # A narrowed filter leaves every entry uncovered at once, so name a sample
    # rather than reprinting the whole roster into the failure — the same reason
    # the live check below caps its own list.
    local shown=()
    while IFS= read -r p; do [[ -n "$p" ]] && shown+=("$p"); done <<<"$out"
    bad "coverage: ${name} — got ${got}, want ${want}$( (( ${#shown[@]} > 0 )) && printf ' (%d uncovered, e.g. %s)' "${#shown[@]}" "$(printf '%s ' "${shown[@]:0:4}")" )"
  fi
}

COVER_DIR="${FIXTURE_DIR}/cover"
mkdir -p "$COVER_DIR"
# The filter narrowed back to the entrypoint-only shape it carried before it was
# widened to the two whole trees, against a fleet watching a tool tree under
# `scripts/`. This is exactly the shape that lets the PR CREATING the
# `!`/`:(exclude)` divergence merge without ever running the measurement, and it
# is what the check below exists to refuse.
cat > "${COVER_DIR}/test-bump-callers.yml" <<'YAML'
on:
  push:
    branches: [main]
    paths:
      - '.github/bump-callers/**'
YAML
cat > "${COVER_DIR}/bump-x-callers.yml" <<'YAML'
on:
  push:
    branches: [main]
    paths:
      - 'scripts/x/**'
      - '!scripts/x/tests/**'
YAML
cover_case 'a filter narrowed to .github/bump-callers misses a scripts/ fleet' \
  uncovered "${COVER_DIR}/test-bump-callers.yml" "${COVER_DIR}/bump-x-callers.yml"

# A filter that still NAMES the tree but only one level down. Actions' `*` stops
# at `/`, so `scripts/*` selects nothing under `scripts/x/` — and the PR adding
# the deep file still starts no run. This is the case a root-only match reported
# as covered (bash's `*` crosses `/`), and the one that makes the
# glob-flatness measurement able to lose its trigger without failing a contract.
cat > "${COVER_DIR}/filter-flat.yml" <<'YAML'
on:
  push:
    branches: [main]
    paths:
      - 'scripts/*'
YAML
cover_case "a depth-1 filter does not cover a tree fleet (Actions \`*\` stops at /)" \
  uncovered "${COVER_DIR}/filter-flat.yml" "${COVER_DIR}/bump-x-callers.yml"

# The mirror: a fleet watching EXACTLY a filter tree root. `scripts/**` covers
# `scripts/**` — obvious, and yet the root-only match said otherwise, because it
# compared the reduced `scripts` against a pattern needing the literal `scripts/`
# prefix. That false RED is the worse half of the two: the failure message tells
# a maintainer to add a tree that is already there.
cat > "${COVER_DIR}/bump-root-callers.yml" <<'YAML'
on:
  push:
    branches: [main]
    paths:
      - 'scripts/**'
YAML
cover_case 'a fleet watching exactly a filter tree root is covered' \
  covered "${WORKFLOWS}/test-bump-callers.yml" "${COVER_DIR}/bump-root-callers.yml"

# An exclusion in the FILTER must actually exclude. Every entry read as an
# independent positive makes a `!` line match nothing at all, so this pair — the
# whole tree, then the fleet's own subtree taken back out — reported covered
# while Actions selects none of it.
cat > "${COVER_DIR}/filter-excluding.yml" <<'YAML'
on:
  push:
    branches: [main]
    paths:
      - 'scripts/**'
      - '!scripts/x/**'
YAML
cover_case "a \`!\` exclusion in the filter uncovers the surface it removes" \
  uncovered "${COVER_DIR}/filter-excluding.yml" "${COVER_DIR}/bump-x-callers.yml"

# ...and the ordering that exclusion implies: Actions lets a LATER positive take
# the subtree back, so the same two lines in the other order are covered again.
# Pins that this applies the list in order rather than just honouring `!`.
cat > "${COVER_DIR}/filter-reinclude.yml" <<'YAML'
on:
  push:
    branches: [main]
    paths:
      - '!scripts/x/**'
      - 'scripts/**'
YAML
cover_case "a later positive re-includes what an earlier \`!\` removed" \
  covered "${COVER_DIR}/filter-reinclude.yml" "${COVER_DIR}/bump-x-callers.yml"

# ...and the REAL pair, driven through the same helper so the fixture above is
# non-vacuous in both directions: it fails on an implementation that reports
# everything uncovered just as surely as the fixture fails one that reports
# everything covered.
cover_case 'the real filter covers every real fleet entrypoint' \
  covered "${WORKFLOWS}/test-bump-callers.yml" "${FILES[@]}"


# --- this suite's OWN trigger must cover the tree it reads --------------------
# The WATCHED_EXEC check above asserts a property of the REPO TREE, not just of
# the entrypoints — so it is only worth anything if it runs on the PR that breaks
# it. A `paths:` filter listing only `bump-*-callers.yml` would leave the commit
# that renames `.github/groom/scope.py` (and forgets the list) untested: the
# guard would first speak on some unrelated later PR, long after the fleet had
# frozen. test-bump-callers.yml therefore watches `.github/**` and `scripts/**`
# rather than the watched surfaces one by one, because an enumeration of those is
# a roster and a roster copied into a filter drifts. That breadth is an
# ASSUMPTION about where this repo keeps its assets, and this is the check that
# stops it going stale: a fleet that ever watches a path outside those trees
# fails here rather than quietly losing its trigger.
echo
echo "== this suite's trigger covers the paths it reads =="

self_filter=()
while IFS= read -r line; do
  [[ -n "$line" ]] && self_filter+=("$line")
done < <(parse_push_paths "${WORKFLOWS}/test-bump-callers.yml")
if (( ${#self_filter[@]} == 0 )); then
  bad "test-bump-callers.yml: parsed NO push paths: entries — this check would pass vacuously"
else
  # Two fleets legitimately list the same file (pr-risk and pr-derisk both run
  # the graders), so de-duplicate — otherwise both the count and any failure
  # message repeat it.
  exec_uniq=()
  while IFS= read -r e; do
    [[ -n "$e" ]] && exec_uniq+=("$e")
  done < <(printf '%s\n' ${ALL_EXEC_ENTRIES[@]+"${ALL_EXEC_ENTRIES[@]}"} | LC_ALL=C sort -u)
  self_patterns="$(printf '%s\n' "${self_filter[@]}")"
  # Command substitution, NOT process substitution, and the rc is consumed: `<(…)`
  # discards the helper's exit status and confines any abort to a subshell, so a
  # helper that died (an unguarded expansion under `set -u`, a later change to the
  # pattern handling) would emit no lines, leave `uncovered` empty, and report the
  # PASSING verdict. Distinguishing "returned covered" from "produced nothing"
  # costs one variable; cover_case has always done it.
  uncovered=() uncov_rc=0
  uncov_out="$(uncovered_against_filter "$self_patterns" ${exec_uniq[@]+"${exec_uniq[@]}"})" || uncov_rc=$?
  while IFS= read -r e; do
    [[ -n "$e" ]] && uncovered+=("$e")
  done <<<"$uncov_out"
  if (( uncov_rc != 0 && ${#uncovered[@]} == 0 )); then
    bad "test-bump-callers.yml: the WATCHED_EXEC coverage helper failed (rc ${uncov_rc}) without naming an uncovered path — it aborted rather than answering, and an empty answer here reads as a clean pass"
  elif (( ${#uncovered[@]} > 0 )); then
    # A narrowed filter leaves whole trees uncovered at once, so report a sample
    # plus the count rather than fifty paths.
    bad "test-bump-callers.yml's \`paths:\` filter selects none of ${#uncovered[@]} WATCHED_EXEC paths, e.g. $(printf '%s ' "${uncovered[@]:0:4}")— a commit renaming or retiring one of those starts no run of this suite, so the WATCHED_EXEC check above cannot catch the list going stale and the fleet freezes silently. Add the tree to BOTH \`paths:\` lists in that workflow"
  else
    ok "test-bump-callers.yml triggers on all ${#exec_uniq[@]} distinct WATCHED_EXEC paths"
  fi

  # --- and the same question again, over every fleet's watched SURFACE --------
  # The pass above covers the files WATCHED_EXEC names. This one covers the
  # `paths:` positives themselves, because the other tree-reading assertion in
  # this suite — the glob-flatness measurement, which walks a `!` exclusion's
  # directory for a match one level down — is an assertion about those surfaces
  # and about nothing else. If a fleet ever watches a tree outside `.github/**`
  # and `scripts/**`, the PR that adds the deep file INTO that tree starts no run
  # of this suite, the measurement first speaks on some unrelated later PR, and
  # the `!`/`:(exclude)` divergence lands green in between. The fix is always the
  # same — widen the filter to the whole new tree, never enumerate the surfaces,
  # since an enumeration is the roster this directory exists to not keep.
  pos_uniq=()
  while IFS= read -r e; do
    [[ -n "$e" ]] && pos_uniq+=("$e")
  done < <(printf '%s\n' ${ALL_POSITIVES[@]+"${ALL_POSITIVES[@]}"} | LC_ALL=C sort -u)
  # Unlike WATCHED_EXEC, where zero entries across the fleet is a legitimate
  # answer, zero positives means every entrypoint above failed to parse — so it
  # is a vacuous pass, not a clean one, and says so.
  if (( ${#pos_uniq[@]} == 0 )); then
    bad "no fleet \`paths:\` positives were collected at all — every entrypoint failed to parse above, so this coverage check would pass vacuously"
  else
    # Same rc-consuming shape as the pass above, for the same reason.
    uncovered=() uncov_rc=0
    uncov_out="$(uncovered_against_filter "$self_patterns" ${pos_uniq[@]+"${pos_uniq[@]}"})" || uncov_rc=$?
    while IFS= read -r e; do
      [[ -n "$e" ]] && uncovered+=("$e")
    done <<<"$uncov_out"
    if (( uncov_rc != 0 && ${#uncovered[@]} == 0 )); then
      bad "test-bump-callers.yml: the \`paths:\` positives coverage helper failed (rc ${uncov_rc}) without naming an uncovered surface — it aborted rather than answering, and an empty answer here reads as a clean pass"
    elif (( ${#uncovered[@]} > 0 )); then
      bad "test-bump-callers.yml's \`paths:\` filter selects none of ${#uncovered[@]} fleet \`paths:\` positives, e.g. $(printf '%s ' "${uncovered[@]:0:4}")— a PR that adds a deep file under one of those surfaces creates the \`!\`/\`:(exclude)\` divergence without ever running the glob-flatness measurement; add the tree to BOTH \`paths:\` lists in that workflow"
    else
      ok "test-bump-callers.yml triggers on all ${#pos_uniq[@]} distinct fleet \`paths:\` positives"
    fi
  fi

  # The file's own header says the two lists are duplicated on purpose and must
  # stay identical — and both coverage passes above read the `push` one only
  # (parse_push_paths is anchored to `^  push:` precisely so a `pull_request:`
  # block can never answer for it), so the `pull_request` list could otherwise
  # drift out from under them unnoticed.
  pr_block="$(awk '/^  pull_request:/{p=1;next} p&&/^  [a-z_]+:/{exit} p&&/^      - /{sub(/^[ \t]+/,"");print}' "${WORKFLOWS}/test-bump-callers.yml")"
  push_block="$(awk '/^  push:/{p=1;next} p&&/^  [a-z_]+:/{exit} p&&/^      - /{sub(/^[ \t]+/,"");print}' "${WORKFLOWS}/test-bump-callers.yml")"
  if [[ -n "$pr_block" && "$pr_block" == "$push_block" ]]; then
    ok "test-bump-callers.yml's pull_request and push paths: lists are identical"
  else
    bad "test-bump-callers.yml's pull_request and push \`paths:\` lists differ — its header requires them identical, and the coverage check above reads only the push one, so the other could silently stop firing
        pull_request: $(echo "$pr_block" | tr '\n' ' ')
        push:         $(echo "$push_block" | tr '\n' ' ')"
  fi
fi


echo
echo "== ${PASS} passed, ${FAIL} failed =="
(( FAIL == 0 ))
