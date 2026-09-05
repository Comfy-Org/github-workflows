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

echo
echo "== ${PASS} passed, ${FAIL} failed =="
(( FAIL == 0 ))
