#!/usr/bin/env bash
#
# Staleness / decommission preflight for the bump-* caller fleets.
#
# Every `bump-*-callers.yml` entrypoint has to answer the same two questions
# before it hands control to bump-callers.sh:
#
#   1. Is this run STALE — i.e. has a *later* commit already touched the watched
#      surface, so that commit has its own run and will pin the newer content?
#   2. Has the watched surface been DECOMMISSIONED — deleted — so that pinning
#      callers to this SHA would break every one of them?
#
# Both questions used to be answered by an inline copy of this logic in each
# bump-* entrypoint. Eight near-copies is exactly the drift pattern this
# directory exists to prevent (see bump-callers.sh's header), and they HAD
# drifted: five skipped on a bare tip mismatch, which throws away the ONLY run
# for a change; bump-auto-label-callers.yml compared content but forgot to
# re-point the pin at the verified tip; bump-detect-unreviewed-merge-callers.yml
# was the hardened one; and bump-pr-risk-callers.yml grew a different hardening
# again (a `git rev-list` "did a later COMMIT touch a watched path" test plus an
# is-ancestor orphan check, and no re-point).
#
# This script is the one implementation, and it deliberately adopts the
# bump-detect-unreviewed-merge-callers.yml semantics (PR #117) — exact-refname
# tip parse, FETCH_HEAD verification, `$WATCHED`-variable deletion guard, and the
# NEW_SHA re-point — generalized to multi-path fleets. BE-6476 swapped the
# entrypoints over: SEVEN of the eight now run this script, and their inline
# copies are gone. The exception is bump-pr-risk-callers.yml, which stays on its
# own inline guard because its `paths:` filter carries `:(exclude)` entries that
# a single WATCHED_ASSETS string cannot express — see the COUPLED TO THE PATH
# FILTER note on the re-point below.
#
# Each consuming entrypoint runs this BEFORE it mints its Cloud Code Bot token
# and gates that step on `proceed` too, so a run this guard no-ops never mints an
# org-wide write credential. Keep that ordering when adding a fleet.
#
# What to do with bump-pr-risk-callers.yml's two extra checks was that swap's one
# open decision. BE-6670 made it, and both halves are settled:
#   * The is-ancestor DIRECTION GUARD is ADOPTED here, for every fleet (BE-6675).
#     It was never pr-risk-specific: without it, a main that moved BACKWARDS —
#     a force-push, a revert-reset, or a stale replica answering the tip lookup —
#     either reads as a stale re-run and freezes the whole fleet behind a green
#     run, or re-points every caller to the OLDER tip. See the guard below.
#   * The `git rev-list` "did a later COMMIT touch a watched path" test is
#     deliberately NOT ported. It exists because pr-risk has no re-point: there, a
#     land-then-revert nets to zero content change, so a content comparison calls
#     this run the only one for the change and pins callers BACKWARDS to
#     github.sha. The re-point below already answers that case — it pins the
#     verified TIP, which on a land-then-revert IS the revert commit, i.e.
#     forward. Porting rev-list on top would only add a stricter staleness
#     verdict that skips a run whose content the tip still needs pinned.
#     That decision is about the land-then-revert case, and it is NOT a claim
#     that an object comparison expresses everything a rev-list PATHSPEC can:
#     pr-risk's filter carries `:(exclude)` entries, and an over-broad watched
#     surface here has its own failure mode — see the COUPLED TO THE PATH FILTER
#     note on the re-point below. Swapping an excluding fleet onto this script
#     means narrowing its inputs to what the filter really watches (or keeping
#     it on its own guard), not adding rev-list.
#
# WATCHED_PATHSPECS / WATCHED_EXEC (BE-6676) are what let an EXCLUDING fleet make
# that swap without narrowing anything away. They express pr-risk's two shapes
# that one `WATCHED_ASSETS` string cannot:
#   * its `paths:` filter carries `:(exclude)` entries, so comparing the bare
#     `scripts/pr-risk` tree OID reads a test-only commit as "changed since" —
#     a FALSE stale that discards the fleet's only real run, the same freeze a
#     bare tip mismatch used to cause. A pathspec-aware `git diff` asks the
#     question the filter actually asks, exclusions included.
#   * its decommission probe is PER EXECUTED FILE (the graders), not per
#     directory: a commit deleting `grade-pr-risk.sh` while leaving `tests/` and
#     `README.md` behind satisfies a `-d scripts/pr-risk` probe and bumps every
#     caller onto a SHA where the tools are gone.
# Neither is rev-list: the staleness test is still a two-tree comparison (it
# needs no history, so it composes with — but does not require — the deepening
# below), and the land-then-revert verdict is still the re-point's.
#
# Required environment:
#   WATCHED        Repo-relative path of the watched reusable workflow file
#                  (e.g. .github/workflows/groom.yml). A LITERAL path, never a
#                  `paths:`-filter glob — see the shape validation below.
#   NEW_SHA        The candidate commit to pin callers to (normally github.sha).
#                  Must be a full 40-character lowercase SHA.
#   GITHUB_SHA     This run's own commit (provided by Actions). Must match HEAD.
#   GITHUB_OUTPUT  Step-output file (provided by Actions).
# Optional:
#   WATCHED_ASSETS Watched assets (a directory, or a file) for a fleet whose
#                  `paths:` filter has more than one entry — a
#                  NEWLINE-SEPARATED LIST of literal
#                  paths, one per line (blank lines ignored). The YAML spelling
#                  is a LITERAL block scalar — `|`, never the folded `>`, which
#                  joins the lines into one space-separated string that resolves
#                  to nothing (validate_path rejects that shape):
#
#                      WATCHED_ASSETS: |
#                        .github/cursor-review
#                        scripts/check-pr-size
#
#                  A block scalar has no comment syntax and takes no `- ` list
#                  dashes; such a line is literal content and is rejected too.
#                  A single-line value is just a one-element list, so every
#                  single-asset fleet's existing `WATCHED_ASSETS: .github/groom`
#                  spelling keeps working unchanged. Empty/unset means the fleet
#                  watches nothing beyond WATCHED. Each entry is validated,
#                  compared, and decommission-checked INDEPENDENTLY, with exactly
#                  the semantics a single asset had: any one of them absent or
#                  changed is enough to stop the bump.
#   WATCHED_PATHSPECS
#                  Newline-separated git PATHSPECS covering the surface the
#                  fleet's `paths:` filter watches — `:(exclude)` entries
#                  included, which is the whole point (pr-risk, and — since
#                  BE-7084 — pr-size and cursor-review, whose filters exclude
#                  `scripts/check-pr-size/*_test.go`). When set it REPLACES the
#                  WATCHED/WATCHED_ASSETS object
#                  comparison as the "changed since" test: a `git diff --quiet`
#                  between this run's commit and the verified tip, restricted to
#                  these pathspecs. It MUST MIRROR THE FLEET'S `paths:` FILTER,
#                  exclusions included — same coupling, and the same two failure
#                  modes in both directions, as the COUPLED TO THE PATH FILTER
#                  note on the re-point below. Two halves of that MUST are
#                  ENFORCED rather than left to convention, because both fail
#                  silently green: every positive entry has to select at least
#                  one tracked path (in either tree), and the list as a whole has
#                  to select WATCHED (and, when WATCHED_ASSETS is also set,
#                  something under EVERY ONE of its entries — the list input is
#                  per-entry here exactly as it is everywhere else, so a fleet
#                  that watches two directories cannot cover one and leave the
#                  other unverified). Unset leaves the OID comparison exactly
#                  as it was, so the fleets that do not set it do not change
#                  behaviour.
#   WATCHED_EXEC   Newline-separated repo-relative FILES that a pinned caller
#                  actually executes (e.g. pr-risk's three grader scripts). When
#                  set, each one is probed for deletion at the tip and — unless
#                  this run was re-pointed, which moves the pin target to that
#                  same tip — again in this run's own tree, IN ADDITION to
#                  WATCHED (and WATCHED_ASSETS, when set): a per-file
#                  decommission check for a fleet whose directory can outlive the
#                  scripts inside it. Plain repo-relative FILE paths only —
#                  pathspec magic belongs in WATCHED_PATHSPECS, and a directory
#                  is rejected (WATCHED_ASSETS is the input for one).
#
# All three list inputs are newline-separated so an entrypoint can write them as
# a YAML block scalar directly beneath the `paths:` filter they mirror. They do
# NOT share one comment rule, and the difference is deliberate rather than an
# oversight of the merge that brought them together:
#
#   * WATCHED_PATHSPECS / WATCHED_EXEC ignore blank lines and whole-line `#`
#     comments, so the fleet's `paths:` filter can be pasted in with its comments
#     intact — which is the point, since those two must MIRROR that filter.
#   * WATCHED_ASSETS ignores blank lines but REJECTS a `#` or `- ` line, because
#     a block scalar has no comment syntax: such a line is literal content, and
#     accepting it would mean "watching" a path that resolves to nothing. It is
#     a short hand-written list, not a pasted filter, so there is nothing to
#     preserve and everything to catch.
#
# They also differ on the empty value, for the same reason. WATCHED_PATHSPECS and
# WATCHED_EXEC reject a variable that is SET but contains no entries — that shape
# is a mis-wired expression, and reading it as "this fleet is simple" is exactly
# the silent under-check the rest of this script refuses to make. WATCHED_ASSETS
# does not, because for IT the empty value is a real answer with a real meaning
# ("this fleet watches nothing beyond WATCHED") that most fleets rely on; there
# is no mis-wiring to catch, only a single-path fleet to let through.
#
# Outputs (written to $GITHUB_OUTPUT on every exit-0 path):
#   proceed  "true"  → the caller should run bump-callers.sh
#            "false" → stale or decommissioned; the caller should do nothing
#   new_sha  the SHA to pin callers to — NEW_SHA, or the verified main tip when
#            this run was re-pointed forward (see the re-point block below)
#
# Exits non-zero ONLY for an input we cannot trust (malformed SHA, a glob-shaped,
# slash-terminated or non-repo-relative watched path, a set-but-blank or
# malformed list input, a pathspec list that selects nothing or does not reach
# WATCHED, a HEAD that is not GITHUB_SHA), a lookup we could not perform
# (failed ls-remote, failed fetch, unresolvable FETCH_HEAD, a rev-parse or a
# pathspec diff that failed rather than reporting absence/equality), or an answer
# that contradicts history
# (a fetched main tip that does not descend from this run's commit, or from the
# tip the ls-remote reported moments earlier). None of those is evidence of
# staleness — it fails loudly rather than silently no-opping the fleet.
#
# Run from the repository root (the final decommission check tests the run's own
# checked-out tree).
set -euo pipefail

: "${WATCHED:?WATCHED is required}"
: "${NEW_SHA:?NEW_SHA is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
WATCHED_ASSETS="${WATCHED_ASSETS-}"

# --- input shape validation --------------------------------------------------
# A watched path is a LITERAL repo-relative path, never the glob from the fleet's
# `paths:` filter. Every instruction to "widen these inputs to match the fleet's
# path filter" points a maintainer straight at `.github/groom/**`, and a glob
# resolves to NOTHING here: `[[ -d '.github/groom/**' ]]` is false and
# `git rev-parse 'HEAD:.github/groom/**'` returns empty. A trailing slash
# (`.github/groom/`) does the same. Either one would make every comparison
# silently verify nothing and turn the whole fleet into a permanent no-op behind
# a green run — reject the shape instead of reporting it as a decommission.
validate_path() { # $1 = input name, $2 = value ("" = unset, skip), $3 = optional glob hint
  [[ -n "$2" ]] || return 0
  local hint="${3-pass the directory itself, e.g. .github/groom, not .github/groom/**}"
  if [[ "$2" == *'*'* || "$2" == *'?'* || "$2" == *'['* ]]; then
    echo "::error::$1 must be a literal path, not a glob (got '$2') — $hint"
    exit 1
  fi
  if [[ "$2" == */ ]]; then
    echo "::error::$1 must not end in a slash (got '$2') — a trailing slash resolves to nothing, so the comparison would silently verify nothing"
    exit 1
  fi
  # A `|` block scalar has NO comment syntax and takes no `- ` list dashes — such
  # a line is literal CONTENT, so it arrives here as a watched path that resolves
  # to nothing, the same silent-decommission failure the checks above exist to
  # prevent. Tested BEFORE the whitespace rule below purely for the diagnosis:
  # `- .github/groom` trips both, and "you wrote a YAML list" is the message that
  # tells the maintainer what to change.
  if [[ "$2" == '#'* || "$2" == -* ]]; then
    echo "::error::$1 must be a literal path, not a comment or a list item (got '$2') — a block scalar ('|') has no comment syntax and takes no '- ' dashes, so such a line is literal content that resolves to nothing"
    exit 1
  fi
  # Internal whitespace is what a FOLDED YAML scalar delivers. `WATCHED_ASSETS: >`
  # over two lines folds to the ONE string `.github/cursor-review scripts/check-pr-size`,
  # which carries no glob and no trailing slash and so sails past every check
  # above — then resolves to nothing, exactly like the glob does. The multi-entry
  # spelling is a `|` BLOCK scalar; reject the folded one by shape rather than
  # letting it become a silent decommission.
  if [[ "$2" == *[[:space:]]* ]]; then
    echo "::error::$1 must be a literal path with no whitespace (got '$2') — a folded YAML scalar ('>') joins a multi-entry value into one space-separated string that resolves to nothing; use a block scalar ('|') with one path per line"
    exit 1
  fi
  # Repo-relative, and only repo-relative. Every watched path is checked TWICE —
  # once inside a tree (`git rev-parse "<tip>:$p"`) and once against the
  # filesystem (`[[ -f "$p" ]]`) — and the two halves disagree about an absolute
  # or `../` path: no tree contains one, while the local probe happily resolves
  # it OUTSIDE the checkout. `/etc/hosts` would then read as decommissioned at
  # the tip and present locally, so the verdict turns on whether main happened to
  # move. Reject the shape instead.
  case "$2" in
    /*|../*|*/../*|*/..|..)
      echo "::error::$1 must be a repo-relative path inside the checkout (got '$2') — an absolute or ../ path is absent from every tree while the local probe can still find it on disk, so the two halves of the same check would disagree"
      exit 1
      ;;
  esac
}
validate_path WATCHED "$WATCHED"

# WATCHED_ASSETS is a newline-separated LIST. Parse it into an array before
# validating, so each entry gets the same glob / trailing-slash rejection a
# single asset used to get — a fleet whose second line is `.github/groom/**`
# must fail as loudly as one whose only line is.
# Surrounding whitespace is stripped per entry. A flow scalar
# (`WATCHED_ASSETS: .github/groom`) cannot carry it, but a block scalar can hold
# trailing spaces that are invisible in review, and ` .github/groom` resolves to
# nothing exactly like the glob above — silently verifying nothing is the failure
# this whole validation block exists to prevent, and there is no legitimate
# watched path with leading or trailing spaces.
asset_dirs=()
while IFS= read -r asset_line; do
  asset_line="${asset_line#"${asset_line%%[![:space:]]*}"}"
  asset_line="${asset_line%"${asset_line##*[![:space:]]}"}"
  [[ -n "$asset_line" ]] || continue
  validate_path WATCHED_ASSETS "$asset_line"
  asset_dirs+=("$asset_line")
done <<<"$WATCHED_ASSETS"

# --- the two newline-separated list inputs -----------------------------------
# Split on newlines, trim each entry, drop blank ones (a YAML block scalar always
# ends in one) and whole-line `#` comments. `mapfile` would do it in a line, but
# it is bash 4+ and this file is also run by hand on macOS's bash 3.2 — hence the
# read loop. On that shell `"${arr[@]}"` on an EMPTY array trips `set -u`, so
# every expansion of these two arrays below is guarded by a count check first.
# The array is deliberately NOT named `LINES`: bash manages that name itself
# (with `checkwinsize`, on by default since bash 5.0, it re-sets LINES/COLUMNS
# after an external command when a tty is attached), and assigning a scalar to an
# existing array writes index 0 — which would inject a bogus first entry into
# whichever list was parsed after the last git call.
PARSED_LINES=()
split_lines() { # $1 = raw value; result in $PARSED_LINES
  PARSED_LINES=()
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -n "$line" ]] || continue
    # Both the header and the README invite writing these lists as a YAML block
    # scalar mirroring the fleet's `paths:` filter — and those filters are
    # commented. Inside a block scalar a `#` is literal text, so a pasted comment
    # would arrive here as a pathspec/file that matches nothing. Whole-line only:
    # `#` is a legal filename character, so a trailing one is left alone.
    case "$line" in '#'*) continue ;; esac
    PARSED_LINES+=("$line")
  done <<< "$1"
}

# A variable that is SET but parses to zero entries is rejected, not read as
# unset. Every way that shape actually arises — `WATCHED_EXEC: ${{ inputs.x }}`
# with nothing bound, a block scalar whose body got deleted — means the
# entrypoint intended a check that would then silently not happen: an excluding
# fleet would fall back to the OID comparison it cannot use (permanent false
# stale), and a per-file fleet would fall back to a directory probe that its
# `tests/` dir keeps satisfying. Same not-evidence principle as everywhere else
# here: fail loudly rather than quietly verify less than the caller asked for.
require_entries() { # $1 = input name, $2 = entry count
  if (( $2 == 0 )); then
    echo "::error::$1 is set but contains no entries — either unset it or give it one path per line (a set-but-blank value would silently skip the check it was added for)"
    exit 1
  fi
}

watched_pathspecs=()
pathspec_positives=()
pathspec_excludes=()
if [[ -n "${WATCHED_PATHSPECS+set}" ]]; then
  split_lines "$WATCHED_PATHSPECS"
  require_entries WATCHED_PATHSPECS "${#PARSED_LINES[@]}"   # exits on 0, so the expansion below is safe
  watched_pathspecs=("${PARSED_LINES[@]}")
  # Pathspecs are the ONE input where glob syntax is legal — they are handed to
  # `git diff`, which does the matching itself. What is not legal is magic other
  # than `:(exclude)`: `:(glob)`, `:(icase)`, `:/`, and the `:!` shorthand all
  # change what the comparison means in ways nothing here validates, and a typo
  # in a magic prefix makes git treat the whole entry as a literal path that
  # matches nothing — an under-verifying comparison that always reads
  # "unchanged" and re-points on every stale re-run.
  for spec in "${watched_pathspecs[@]}"; do
    case "$spec" in
      ':(exclude)'*)
        if [[ "$spec" == ':(exclude)' ]]; then
          echo "::error::WATCHED_PATHSPECS entry ':(exclude)' names no path — write the path being excluded, e.g. ':(exclude)scripts/pr-risk/tests'"
          exit 1
        fi
        pathspec_excludes+=("$spec")
        ;;
      :*)
        echo "::error::WATCHED_PATHSPECS entry '$spec' uses unsupported pathspec magic — only ':(exclude)<path>' is accepted here (not ':!', ':(glob)' or ':/')"
        exit 1
        ;;
      '!'*)
        # `!path` is the negation syntax of an Actions `paths:` filter, not of a
        # git pathspec — and this input's whole instruction is "MIRROR the
        # filter, exclusions included", so it is the spelling a maintainer
        # actually pastes. git reads the `!` as an ordinary leading character, so
        # the entry excludes nothing AND matches nothing (measured), reinstating
        # the false-stale freeze this input exists to remove — while counting as
        # a positive, so the all-negative guard below would not fire either.
        echo "::error::WATCHED_PATHSPECS entry '$spec' starts with '!' — that is the paths: filter's negation syntax, not git's. git would read it as a literal path that matches nothing, so the exclusion would silently not happen. Write it as ':(exclude)${spec#!}'"
        exit 1
        ;;
      *) pathspec_positives+=("$spec") ;;
    esac
  done
  # git reads an all-negative pathspec as "everything EXCEPT these", which is the
  # widest possible watched surface — the precise over-broad shape that makes an
  # unrelated commit read as "changed since" and freezes the fleet. It is never
  # what a `paths:` filter means.
  if (( ${#pathspec_positives[@]} == 0 )); then
    echo "::error::WATCHED_PATHSPECS contains only ':(exclude)' entries — git reads that as EVERY path except those, so every unrelated commit would read as a watched change. Include the positive paths the fleet's \`paths:\` filter lists."
    exit 1
  fi
fi

watched_exec=()
if [[ -n "${WATCHED_EXEC+set}" ]]; then
  split_lines "$WATCHED_EXEC"
  require_entries WATCHED_EXEC "${#PARSED_LINES[@]}"        # exits on 0, so the expansion below is safe
  watched_exec=("${PARSED_LINES[@]}")
  for f in "${watched_exec[@]}"; do
    # These are probed with `git rev-parse "<tip>:$f"` and `[[ -f "$f" ]]`, and
    # neither expands anything: a glob, a trailing slash or a pathspec magic
    # prefix simply names a file that never exists, so every probe would report
    # a decommission and the fleet would no-op behind a green run for good.
    if [[ "$f" == :* ]]; then
      echo "::error::WATCHED_EXEC entry '$f' looks like a pathspec — WATCHED_EXEC takes plain repo-relative file paths, and ':(exclude)' magic is only legal in WATCHED_PATHSPECS"
      exit 1
    fi
    validate_path "WATCHED_EXEC entry" "$f" "name each executed file, e.g. scripts/pr-risk/grade-pr-risk.sh"
    # A DIRECTORY is the one wrong entry the two probes answer differently:
    # `git rev-parse "<tip>:scripts/pr-risk"` resolves the tree and reports it
    # present, while `[[ -f scripts/pr-risk ]]` is false and reports it
    # decommissioned — so the verdict would flip on whether main happened to
    # move. (The tip probe below independently insists on a blob, so the two can
    # never disagree; this rejects the mis-specification outright, with a message
    # that names the input meant for a directory.)
    if [[ -d "$f" ]]; then
      echo "::error::WATCHED_EXEC entry '$f' is a directory — WATCHED_EXEC names the FILES a pinned caller executes, one per line. Use WATCHED_ASSETS for a directory"
      exit 1
    fi
  done
fi

# NEW_SHA is the one value here that is never derived from a lookup — every check
# below validates GITHUB_SHA/HEAD, while NEW_SHA is emitted verbatim into
# $GITHUB_OUTPUT and handed to bump-callers.sh's pin rewrite. Its SHAPE is
# therefore load-bearing: a value containing a newline injects extra output lines
# (an injected `proceed=true` would win over the `proceed=false` this script
# wrote), and any non-SHA silently becomes what every caller in the fleet is
# pinned to.
require_sha() { # $1 = input name, $2 = value
  if [[ ! "$2" =~ ^[0-9a-f]{40}$ ]]; then
    # Strip CR/LF before logging: an untrusted multi-line value would otherwise
    # spread across the log as forged annotation lines of its own.
    echo "::error::$1 must be a full 40-character lowercase commit SHA (got '${2//[$'\n'$'\r']/ }')"
    exit 1
  fi
}
require_sha NEW_SHA "$NEW_SHA"
require_sha GITHUB_SHA "$GITHUB_SHA"

# The staleness decision is keyed off $GITHUB_SHA, but the "here" side of every
# comparison below is read from HEAD (and from the working tree by the final
# -f/-d guards) — nothing otherwise asserts the two agree. A consuming job whose
# `actions/checkout` uses a `ref:` override, or any earlier step that moves HEAD,
# would have this script compare main against itself: every comparison reads
# "unchanged", so every stale re-run proceeds and re-points.
if ! head_sha=$(git rev-parse --verify --quiet 'HEAD^{commit}'); then
  echo "::error::Could not resolve HEAD — preflight must run from the root of this run's own checkout"
  exit 1
fi
if [[ "$head_sha" != "$GITHUB_SHA" ]]; then
  echo "::error::HEAD ($head_sha) is not this run's commit GITHUB_SHA ($GITHUB_SHA) — the checkout must not use a ref: override. Refusing to compare main against itself."
  exit 1
fi

# Resolve a rev to an object id, distinguishing "absent from that tree"
# (rev-parse exit 1, empty result — a real answer this script acts on) from "the
# lookup itself failed" (any other status: a missing promisor object, a corrupt
# pack). A bare `|| true` collapses both into "absent", which turns a lookup we
# could not perform into a silent decommissioned/stale verdict — the same
# not-evidence anti-pattern the ls-remote guard below rejects.
RESOLVED=""
resolve_oid() { # $1 = rev; result in $RESOLVED, empty when absent from the tree
  local rc=0
  RESOLVED=$(git rev-parse --verify --quiet "$1") || rc=$?
  if (( rc > 1 )); then
    echo "::error::Could not look up $1 (git rev-parse exited $rc) — refusing to read a failed lookup as a deletion"
    exit 1
  fi
}

# The WATCHED_EXEC half of the same lookup, restricted to a regular file. Its
# other half is a literal `[[ -f ]]`, so anything that is not a blob has to read
# as absent here too — otherwise an entry naming a directory is "present" in the
# tree and "absent" on disk, and the verdict depends on whether main moved.
resolve_blob() { # $1 = rev; $RESOLVED empty when absent OR not a regular file
  resolve_oid "$1"
  [[ -n "$RESOLVED" ]] || return 0
  local kind
  if ! kind=$(git cat-file -t "$RESOLVED"); then
    echo "::error::Could not read the object type of $1 — refusing to read a failed lookup as a deletion"
    exit 1
  fi
  [[ "$kind" == "blob" ]] || RESOLVED=""
}

# What a pathspec list actually SELECTS, matched by `git diff` itself — the same
# matcher the staleness comparison is drawn with, so coverage can never be
# checked against looser rules than the verdict. Diffing a tree against the EMPTY
# tree lists every path in it the pathspecs select. (`git ls-tree` cannot stand
# in: it rejects `:(exclude)` outright — "pathspec magic not supported by this
# command" — and its default matching does not expand `**`. Both measured.)
EMPTY_TREE=""
MATCHED=""
match_paths() { # $1 = tree-ish, $2.. = pathspecs; result in $MATCHED
  local tree="$1"; shift
  local rc=0
  if [[ -z "$EMPTY_TREE" ]]; then
    if ! EMPTY_TREE=$(git hash-object -t tree /dev/null); then
      echo "::error::Could not compute the empty-tree object id — cannot check what WATCHED_PATHSPECS selects"
      exit 1
    fi
  fi
  MATCHED=$(git diff --name-only "$EMPTY_TREE" "$tree" -- "$@") || rc=$?
  if (( rc != 0 )); then
    echo "::error::Could not list the paths WATCHED_PATHSPECS selects at $tree (git diff exited $rc) — refusing to trust a comparison whose coverage could not be checked"
    exit 1
  fi
}

# Both outputs are written on EVERY exit-0 path, so a consuming step never reads
# an empty `new_sha` off a skip. NEW_SHA is deliberately a step OUTPUT and not a
# $GITHUB_ENV export: a step-level `env: NEW_SHA:` binding in the consuming step
# takes precedence over the job environment, so a $GITHUB_ENV write would be
# silently overridden by the very binding it is meant to correct.
emit() {
  printf 'proceed=%s\n' "$1" >> "$GITHUB_OUTPUT"
  printf 'new_sha=%s\n' "$2" >> "$GITHUB_OUTPUT"
}

# The main-only ref guard in the entrypoints cannot catch a manual RE-RUN of an
# older main run: github.ref is refs/heads/main but github.sha is that run's
# original (now stale) commit, and the bumper would force-repin every caller to
# it. So establish the current main tip first; the guard below decides whether
# this run is genuinely stale.
# Don't pipe into `cut`: the pipeline would report cut's status, so a failed
# ls-remote (network blip, remote hiccup) yields an EMPTY main_tip that then
# compares unequal to github.sha and silently no-ops the whole fleet bump as if
# the run were stale. A lookup we couldn't perform is not evidence of staleness
# — fail loudly.
# `--refs` plus an exact-refname match, not just the first line: git matches ref
# patterns at component boundaries, so a branch literally named
# `foo/refs/heads/main` also matches this pattern and could be the line a bare
# `%%\t*` parse consumes.
if ! ls_remote=$(git ls-remote --refs origin refs/heads/main); then
  echo "::error::Could not look up the current main tip (git ls-remote failed)"
  exit 1
fi
main_tip=$(awk '$2 == "refs/heads/main" { print $1; exit }' <<<"$ls_remote")
if [[ -z "$main_tip" ]]; then
  echo "::error::git ls-remote returned no SHA for refs/heads/main"
  exit 1
fi

repointed=""
if [[ "$main_tip" != "$GITHUB_SHA" ]]; then
  # main has moved on. That alone does NOT make this run stale: the push trigger
  # is path-filtered to the watched surface, so an unrelated commit landing in
  # the seconds between this run's trigger and this check starts NO run of its
  # own. Skipping on a bare SHA mismatch would discard the only run for this
  # change and leave every caller frozen — the exact pin-drift this fleet exists
  # to prevent. (That bare-mismatch skip is what five of the entrypoints did
  # before this script existed.)
  # What actually distinguishes a stale re-run is that the watched surface has
  # CHANGED since: then a later commit did touch a filtered path and does have
  # its own run, which will pin the newer content.
  # Fetch the BRANCH ref explicitly. `git fetch origin main` resolves the bare
  # name through the refspec rules, which consult `refs/tags/<name>` BEFORE
  # `refs/heads/<name>` — and this repo routinely creates and force-moves major
  # tags. A tag named `main` would shadow the branch, silently making an
  # arbitrary tagged commit the FETCH_HEAD that the blob comparison and the
  # re-point below both run against — and therefore what every caller gets
  # pinned to. `refs/heads/main` can only ever be the branch, which is also the
  # ref the exact-refname `ls-remote` match above resolved. (bump-pr-risk's copy
  # fetches a bare `main` — do not "align" with it; that is the shadowing bug.)
  #
  # Fetch REAL history, not just the tip. The direction guards below ask whether
  # one commit DESCENDS from another, and `git merge-base --is-ancestor` cannot
  # answer that against a `--depth=1` graft: a parentless FETCH_HEAD makes even a
  # legitimate forward move read as not-an-ancestor, so the naive one-liner would
  # hard-fail every re-point (verified empirically, spike BE-6670). Deepening is
  # what makes the guards sound. actions/checkout clones shallow, and
  # `--unshallow` errors out on an already-complete clone — hence the probe
  # rather than an unconditional flag.
  # Ask git whether the REPOSITORY is shallow; don't stat `$(git rev-parse
  # --git-dir)/shallow`. Inside a linked worktree `--git-dir` is that worktree's
  # own directory while the `shallow` marker lives in the COMMON git dir, so the
  # hand-rolled probe false-negatives there and skips the deepening entirely.
  # The verdict still comes out right today — a plain fetch into a shallow clone
  # sends the new commits down to the existing boundary, so HEAD stays reachable
  # (measured; test_preflight.sh's shallow_worktree case) — but that leaves the
  # guards resting on fetch's boundary behavior instead of on the `--unshallow`
  # this comment says makes them sound, and one refspec away from the shape that
  # does break it. `--is-shallow-repository` is correct in both layouts (and,
  # being a non-empty array either way, the form below is also safe under
  # `set -u` on bash 3.2, where an empty `"${arr[@]}"` is an unbound variable).
  # Cost: bounded by this repo's own history — a full clone of it measures well
  # under 2s today — and this branch only runs on a tip mismatch. Deliberately no
  # `--deepen` ceiling: a partial deepening cannot answer the ancestry question,
  # which is the whole point of fetching here. The caller job's `timeout-minutes`
  # is the backstop for an origin that hangs.
  fetch_args=(origin refs/heads/main)
  if [[ "$(git rev-parse --is-shallow-repository)" == "true" ]]; then
    fetch_args=(--unshallow "${fetch_args[@]}")
  fi
  if ! git fetch "${fetch_args[@]}"; then
    echo "::error::Could not fetch the current main tip to compare $WATCHED"
    exit 1
  fi
  # Prove FETCH_HEAD resolves to a real commit BEFORE reading objects out of it.
  # `git rev-parse --verify --quiet` returns empty both for "that path is absent
  # from this tree" and for "this revision could not be resolved at all" (a
  # partial fetch, an unexpected FETCH_HEAD state). Without this guard the second
  # case is indistinguishable from deletion and would exit 0 as "decommissioned"
  # — the same "a lookup we couldn't perform is not evidence" anti-pattern the
  # ls-remote guard above rejects, but silently no-opping the whole fleet. With
  # it, an empty tip_blob genuinely means absent-from-tree.
  if ! fetched_tip=$(git rev-parse --verify --quiet "FETCH_HEAD^{commit}"); then
    echo "::error::Fetched main but FETCH_HEAD does not resolve to a commit — cannot compare $WATCHED"
    exit 1
  fi
  # main can move in the seconds between the ls-remote and the fetch. Moving
  # FORWARD is a benign race: compare against — and re-point to — the tip whose
  # objects we actually read, and say so in the log. But "advanced" is a
  # DIRECTION, and it has to be measured rather than assumed: the fetch can
  # equally land on a commit OLDER than the one ls-remote reported moments
  # earlier (a force-push landing in that window, or a stale read replica
  # answering the fetch). The HEAD check below does NOT catch that — a rewind to
  # a commit that is still ahead of this run passes it, and the fleet is then
  # silently pinned to a tip we already know main was ahead of. So require the
  # observed tip to be an ancestor of the fetched one.
  # A `--is-ancestor` that cannot be performed at all (exit >1: the observed tip
  # is not even present locally after the fetch) lands here too, and correctly —
  # in a forward move the observed tip IS an ancestor of the fetched one and
  # therefore present, so its absence is itself evidence the move was not
  # forward. The message names both readings.
  if [[ "$fetched_tip" != "$main_tip" ]]; then
    if ! git merge-base --is-ancestor "$main_tip" "$fetched_tip"; then
      echo "::error::the fetched main tip ($fetched_tip) does not descend from the tip the lookup reported moments earlier ($main_tip) — main moved backwards, or a stale replica answered the fetch (or the ancestry could not be checked at all); refusing to compare or re-point"
      exit 1
    fi
    echo "main advanced from $main_tip to $fetched_tip between the tip lookup and the fetch — comparing against the fetched tip"
  fi
  main_tip="$fetched_tip"
  # Refuse to act on a tip that is not a descendant of this run's commit: a
  # force-push back, a revert-reset, or a stale replica answering the lookup.
  # Proceeding either way is wrong, and BOTH ways are silent today — if the
  # watched content DIFFERS at the older tip, the comparison below reports a
  # false "stale run/re-run; the newer commit has its own run" and exits green,
  # freezing every caller behind a run that will never come; if it MATCHES, the
  # re-point hands `new_sha` the OLDER tip and pins the whole fleet BACKWARDS.
  # Loud, not a green skip — a lookup that contradicts history is not evidence of
  # staleness. (Ported from bump-pr-risk-callers.yml, BE-6670.)
  #
  # Unlike resolve_oid, this deliberately does NOT split "not an ancestor" (exit
  # 1) from "the check itself errored" (exit >1). There the distinction is
  # load-bearing because the two verdicts DIVERGE — absence exits 0 and skips
  # silently, a failed lookup must not. Here they converge: both are an
  # `::error::` and exit 1, so splitting them would only reword a log line — and
  # the line already names both readings rather than asserting the diagnosis.
  #
  # Compare the OID we resolved and verified above, not the FETCH_HEAD ref: that
  # ref is mutable, and anything rewriting it in between (a retry wrapper, a
  # concurrent git operation in the same workspace, a hook) would let a commit
  # other than the one checked here be the one whose blobs are compared and
  # emitted as new_sha. Same reason the reads below take $fetched_tip.
  if ! git merge-base --is-ancestor "$head_sha" "$fetched_tip"; then
    echo "::error::the fetched main tip ($main_tip) does not descend from this run's commit $GITHUB_SHA — main moved backwards, or a stale replica answered (or the ancestry could not be checked at all); refusing to compare or re-point"
    exit 1
  fi
  resolve_oid "$fetched_tip:$WATCHED"
  tip_blob="$RESOLVED"
  # HEAD is this run's own checkout, so the lookup itself must succeed (a failure
  # exits inside resolve_oid). An EMPTY here_blob means $WATCHED is absent at
  # github.sha, which is the deletion-commit case the final guard handles — don't
  # let it fall into the "changed since" branch below and be reported as a stale
  # re-run, which would be a misleading log for a real decommission.
  resolve_oid "HEAD:$WATCHED"
  here_blob="$RESOLVED"
  if [[ -z "$here_blob" ]]; then
    echo "::warning::$WATCHED is absent at this run's own commit $GITHUB_SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
    emit false "$NEW_SHA"
    exit 0
  fi
  # Multi-path fleets pin further surfaces: the asset directories the reusable
  # loads its prompts/scripts/briefs from — or, for cursor-review, BUILDS the
  # check-pr-size classifier from — at run time. Compare each one's TREE OID the
  # same way, INDEPENDENTLY: any single entry differing is enough to make this a
  # stale re-run, and any single entry missing is enough to make it a
  # decommission. See the COUPLED TO THE PATH FILTER note on the re-point below.
  #
  # Every absent-at-HEAD check runs before any tip_gone verdict is acted on
  # (that is why this loop exits on the former but only RECORDS the latter),
  # preserving the single-asset ordering: a surface already gone at this run's
  # own commit is a decommission, not a "changed since".
  assets_tip_gone=""
  assets_changed=""
  if (( ${#asset_dirs[@]} > 0 )); then
    for asset_dir in "${asset_dirs[@]}"; do
      resolve_oid "$fetched_tip:$asset_dir"
      tip_asset="$RESOLVED"
      resolve_oid "HEAD:$asset_dir"
      here_asset="$RESOLVED"
      # Same reasoning as the here_blob guard above: an asset tree that is
      # already gone at this run's own commit is a decommission, not a
      # "changed since".
      if [[ -z "$here_asset" ]]; then
        echo "::warning::$asset_dir is absent at this run's own commit $GITHUB_SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
        emit false "$NEW_SHA"
        exit 0
      fi
      # First match wins for both, so the reported path is deterministic
      # (the entries are compared in the order the fleet lists them).
      if [[ -z "$tip_asset" ]] && [[ -z "$assets_tip_gone" ]]; then
        assets_tip_gone="$asset_dir"
      fi
      if [[ "$tip_asset" != "$here_asset" ]] && [[ -z "$assets_changed" ]]; then
        assets_changed="$asset_dir"
      fi
    done
  fi
  # ANY ONE watched surface being gone at the tip is a decommission — not all of
  # them. Retirement is normally staged (delete the reusable, clean up its asset
  # directories in later commits), so an AND here would let the common case fall
  # through to the "stale run/re-run" branch and exit green, suppressing the
  # ::warning:: below. It would also disagree with the local -f/-d guards at the
  # bottom of this script, which already treat any one surface missing as a
  # decommission — the same situation must not get two different verdicts
  # depending on which branch reached it.
  # WATCHED is reported ahead of the assets, and the assets in the order the
  # fleet lists them, so the named path is deterministic rather than incidental.
  tip_gone=""
  if [[ -z "$tip_blob" ]]; then
    tip_gone="$WATCHED"
  elif [[ -n "$assets_tip_gone" ]]; then
    tip_gone="$assets_tip_gone"
  fi
  # A per-file fleet's directory can OUTLIVE the scripts inside it: pr-risk's
  # `scripts/pr-risk` still exists once `tests/` and `README.md` are all that is
  # left, so a directory probe would report the surface healthy and bump every
  # caller onto a SHA where the graders it executes are gone. Probe the executed
  # files themselves. This runs BEFORE the changed-since test below on purpose —
  # a deletion also changes the watched surface, and reporting it as "a newer
  # commit has its own run" would swallow the ::warning:: that is the fleet's
  # only chance to say live callers are about to hard-fail at startup.
  gone_here=""
  if [[ -z "$tip_gone" ]] && (( ${#watched_exec[@]} > 0 )); then
    for f in "${watched_exec[@]}"; do
      resolve_blob "$fetched_tip:$f"
      if [[ -z "$RESOLVED" ]]; then
        tip_gone="$f"
        # WATCHED and WATCHED_ASSETS each get an explicit "absent at this run's
        # own commit" branch before their tip probe; an executed file needs the
        # same distinction for its ANNOTATION, or a file this run's own commit
        # deleted is reported as "no longer exists on main ($main_tip)" and sends
        # an operator to a SHA that had nothing to do with it. Only the wording
        # differs — the verdict is the same decommission either way.
        resolve_blob "HEAD:$f"
        if [[ -z "$RESOLVED" ]]; then
          gone_here=1
        fi
        break
      fi
    done
  fi
  if [[ -n "$tip_gone" ]]; then
    # ::warning:: not a bare echo: if the reusable was deleted while live callers
    # still pin it, they all hard-fail at startup and a silently-green run here
    # is the fleet's only chance to say so.
    if [[ -n "$gone_here" ]]; then
      echo "::warning::$tip_gone is absent at this run's own commit $GITHUB_SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
    else
      echo "::warning::$tip_gone no longer exists on main ($main_tip) — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
    fi
    emit false "$NEW_SHA"
    exit 0
  fi
  # The "changed since" test, in one of two shapes.
  #
  # WATCHED_PATHSPECS is the pathspec shape, for a fleet whose `paths:` filter
  # carries `:(exclude)` entries. Comparing objects cannot express an exclusion:
  # the bare `scripts/pr-risk` tree OID moves when a test-only commit lands, so
  # the OID comparison below would call this run stale and wait for a run that
  # the filter guarantees never started. `git diff` takes the exclusions
  # verbatim, so it asks exactly what the filter asks. It compares two TREES and
  # walks no history, so it does not depend on the deepening above (it composes
  # with it — both are true whether or not the checkout was shallow).
  #
  # A comparison that could not be performed is not a verdict, in EITHER
  # direction — same not-evidence principle as resolve_oid. Reading it as
  # "changed" freezes the fleet behind a run that never comes; reading it as
  # "unchanged" re-points every caller to a tip nothing was verified at.
  #
  # Both shapes NAME the surface that moved. For a fleet that has stopped
  # bumping, that line is the operator's only diagnostic, and "which of the
  # watched paths moved" is the whole question — so the pathspec shape reports it
  # too rather than leaving the fleets that need exclusions worse off than the
  # ones that do not.
  surface_changed=""
  changed_surface=""
  if (( ${#watched_pathspecs[@]} > 0 )); then
    # `git diff --quiet` exits 1 for "there is a diff", 0 for "there is none",
    # and 128 for a comparison it could not perform — an unreadable object, or a
    # pathspec git rejects outright (an absolute or `../` path, measured: 128).
    diff_rc=0
    git diff --quiet "$head_sha" "$fetched_tip" -- "${watched_pathspecs[@]}" || diff_rc=$?
    if (( diff_rc > 1 )); then
      echo "::error::Could not compare the watched pathspecs between $GITHUB_SHA and main ($main_tip) — git diff exited $diff_rc; refusing to read a failed comparison as either verdict"
      exit 1
    fi
    # `--quiet` cannot tell "nothing changed under these pathspecs" from "these
    # pathspecs select nothing at all": both exit 0. The second reads as
    # "unchanged" and re-points every caller to a tip at which NOTHING was
    # compared — the silent under-verification this script refuses everywhere
    # else (a typo'd WATCHED_ASSETS at least fails loudly, via an empty OID) —
    # and it is one typo, one directory rename, or one positive fully swallowed
    # by an `:(exclude)` away. git is the only thing that knows what a pathspec
    # matches, so ask it, with the exclusions applied exactly as the comparison
    # applies them. Checked on BOTH sides: a path this commit deletes, or one
    # only the tip has yet, is still a live watched path — only an entry that
    # matches in NEITHER tree is a dead one.
    for spec in "${pathspec_positives[@]}"; do
      spec_args=("$spec")
      if (( ${#pathspec_excludes[@]} > 0 )); then
        spec_args+=("${pathspec_excludes[@]}")
      fi
      match_paths "$head_sha" "${spec_args[@]}"
      spec_hits="$MATCHED"
      if [[ -z "$spec_hits" ]]; then
        match_paths "$fetched_tip" "${spec_args[@]}"
        spec_hits="$MATCHED"
      fi
      if [[ -z "$spec_hits" ]]; then
        echo "::error::WATCHED_PATHSPECS entry '$spec' selects no tracked path at $GITHUB_SHA or at main ($main_tip) — a pathspec that matches nothing makes the comparison report 'unchanged' having compared nothing, and every caller would be re-pointed on the strength of it. Fix the path, or drop the entry if an ':(exclude)' now covers it entirely"
        exit 1
      fi
    done
    # ...and the list has to actually COVER the surfaces the object comparison
    # covered unconditionally before this input existed. Only convention binds
    # the header's "it MUST MIRROR the fleet's `paths:` filter" otherwise, and an
    # entrypoint that leaves the reusable itself out of the list reads a later
    # WATCHED-only commit as "surface unchanged" — so this stale run re-points
    # and bumps in parallel with the run that commit started for itself.
    match_paths "$head_sha" "${watched_pathspecs[@]}"
    covered=""
    while IFS= read -r matched_path; do
      if [[ "$matched_path" == "$WATCHED" ]]; then covered=1; break; fi
    done <<<"$MATCHED"
    if [[ -z "$covered" ]]; then
      echo "::error::WATCHED_PATHSPECS does not select WATCHED ($WATCHED) — the reusable workflow is the one surface every fleet compares unconditionally, and a list that omits it reads a commit touching only it as 'unchanged'. Add it (or the pathspec covering it), and check no ':(exclude)' swallows it"
      exit 1
    fi
    # EVERY entry, not the list as a whole. WATCHED_ASSETS is a list (BE-7045),
    # and a fleet that watches two directories — cursor-review watches
    # .github/cursor-review and scripts/check-pr-size — must not be able to cover
    # one and leave the other silently unverified. That is the same
    # any-one-of-them reasoning the decommission and OID comparisons already use
    # per entry; checking the list as a whole would be strictly weaker than the
    # comparison this input supersedes.
    if (( ${#asset_dirs[@]} > 0 )); then
      for asset_dir in "${asset_dirs[@]}"; do
        covered=""
        while IFS= read -r matched_path; do
          # An asset entry may be a FILE as well as a directory (see the header),
          # and a file is selected as ITSELF, never as a `<entry>/` prefix — so a
          # prefix-only test would fail every file asset with a "covers nothing"
          # error that the pathspec list cannot actually fix.
          if [[ "$matched_path" == "$asset_dir" || "$matched_path" == "$asset_dir"/* ]]; then covered=1; break; fi
        done <<<"$MATCHED"
        if [[ -z "$covered" ]]; then
          echo "::error::WATCHED_PATHSPECS selects nothing under WATCHED_ASSETS entry '$asset_dir' — setting both means the pathspec comparison SUPERSEDES the asset tree OID comparison, so a list that reaches none of that entry leaves it unverified. Cover it in the pathspec list, or drop it from WATCHED_ASSETS"
          exit 1
        fi
      done
    fi
    if (( diff_rc == 1 )); then
      surface_changed=1
      # `--quiet` told us THAT something moved; ask again for WHICH, under the
      # very same pathspecs (exclusions included), so the name reported can never
      # be a path the fleet does not actually watch. First path wins, for the
      # same determinism the OID branch gets from its listed order.
      changed_paths=$(git diff --name-only "$head_sha" "$fetched_tip" -- "${watched_pathspecs[@]}")
      changed_surface="${changed_paths%%$'\n'*}"
    fi
  elif [[ "$tip_blob" != "$here_blob" ]] || [[ -n "$assets_changed" ]]; then
    surface_changed=1
    # Same WATCHED-then-assets-in-listed-order precedence as the decommission
    # verdict above, so the reported path is deterministic.
    changed_surface="$assets_changed"
    [[ "$tip_blob" == "$here_blob" ]] || changed_surface="$WATCHED"
  fi
  if [[ -n "$surface_changed" ]]; then
    echo "github.sha $GITHUB_SHA is behind main ($main_tip) and the watched surface changed since ($changed_surface) — stale run/re-run; the newer commit has its own run. Nothing to bump"
    emit false "$NEW_SHA"
    exit 0
  fi
  # Pin callers to the VERIFIED TIP, not to this run's stale github.sha. We have
  # just proved every watched object is byte-identical at both, so the tip is the
  # same reusable content at a commit that is actually current — pinning the
  # older SHA would hand every caller a non-tip commit (and, on a
  # land-then-revert, re-pin them backwards).
  #
  # COUPLED TO THE PATH FILTER — this is only sound because every entry in the
  # fleet's `paths:` trigger is covered by the comparison above. A single-path
  # fleet passes WATCHED alone; a fleet whose filter also watches asset
  # directories MUST list EVERY one of them in WATCHED_ASSETS, or the comparison
  # silently under-verifies and callers get pinned to a tip whose other relevant
  # content was never compared. Today that is agents-md-integrity
  # (.github/agents-md-integrity/**), groom (.github/groom/**), pr-size
  # (scripts/check-pr-size/**) and cursor-review, which watches TWO
  # (.github/cursor-review/** for the review prompts/scripts and
  # scripts/check-pr-size/** for the classifier it builds at run time).
  #
  # A fleet whose filter carries `:(exclude)` entries needs WATCHED_PATHSPECS on
  # top, because no set of WATCHED_ASSETS paths can express an exclusion — the
  # asset tree OID moves for an excluded commit just as it does for a watched
  # one. Today that is pr-risk, plus pr-size and cursor-review, which since
  # BE-7084 both exclude `scripts/check-pr-size/*_test.go` (a pinned caller
  # builds and runs that tool; it never runs `go test`, so a test-only commit is
  # pure churn to every consumer). THE SAME COUPLING BINDS IT, and more
  # literally: the pathspec list must MIRROR the fleet's `paths:` filter,
  # exclusions included. Read the entrypoint's `paths:` rather than trusting this
  # list, and if you widen a fleet's filter again, widen the inputs here in the
  # same change — test_paths_contract.sh fails the build if you don't, and for an
  # excluding fleet it checks the two lists are EQUIVALENT, not merely present.
  #
  # The inputs must not be WIDER than the filter either, and that direction is
  # the one an excluding fleet gets wrong. A commit touching only an EXCLUDED
  # path (pr-risk's `scripts/pr-risk/tests`, its `README.md`) starts no run of
  # its own — but it does change the tree OID of an over-broad WATCHED_ASSETS,
  # so the comparison above reports "the watched surface changed since" and this
  # run skips green as a stale re-run, waiting on a later run that will never
  # exist. That freezes the fleet exactly as a bare tip mismatch used to. An
  # exclusion is therefore a reason to carry the filter's exclusions into
  # WATCHED_PATHSPECS (or to narrow the inputs), never to point WATCHED_ASSETS at
  # the whole directory. Dropping one `:(exclude)` line from the pathspec list
  # reinstates that freeze exactly — which is why they live next to each other.
  echo "main moved to $main_tip since $GITHUB_SHA, but the watched surface is unchanged — this run is still the only one for that change; pinning callers to $main_tip and proceeding"
  NEW_SHA="$main_tip"
  repointed=1
fi

# The push path filter also matches a commit that DELETES the reusable workflow;
# bumping callers to a SHA where it is gone would break every caller. Deletion
# means decommissioning — no-op.
# Test "$WATCHED", not a second copy of the literal path: two literals drift
# apart on a rename, and the stale one would name a file that never exists,
# making this test always true and the whole fleet a permanent silent no-op.
if [[ ! -f "$WATCHED" ]]; then
  echo "::warning::$WATCHED absent at this SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
  emit false "$NEW_SHA"
  exit 0
fi
# Every watched asset gets the same test, for the same reason: ANY one of them
# missing at this SHA means a caller pinned here would load a surface that is
# gone. Same first-match-wins ordering as the tip comparison above.
# `-e`, not `-d`: nothing else here requires an asset to be a DIRECTORY —
# validate_path accepts a file path, and both tree-OID comparisons above resolve
# a blob just as happily as a tree. A `-d` here would let a fleet watching a
# single file (e.g. `scripts/check-pr-size/go.mod`) pass every comparison and
# then trip this guard on every run, reporting a permanent no-op as a
# decommission that never happened.
if (( ${#asset_dirs[@]} > 0 )); then
  for asset_dir in "${asset_dirs[@]}"; do
    if [[ ! -e "$asset_dir" ]]; then
      echo "::warning::$asset_dir absent at this SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
      emit false "$NEW_SHA"
      exit 0
    fi
  done
fi
# The local half of the per-file probe above: the deleting commit is often the
# tip itself, in which case none of the "main moved" comparisons ran at all. A
# `-d` probe on the parent directory would pass here for the same reason it
# would there — the directory outlives the scripts — so test each executed file.
#
# Skipped once this run has been RE-POINTED, because then this checkout is no
# longer the SHA callers are about to be pinned to: the loop above already
# proved every executed file exists at $main_tip, which is what `new_sha` now
# carries. An executed file that is absent HERE but present THERE — added
# between github.sha and the tip, or deleted here and restored — is not a
# decommission at the pin target, and reading this tree would discard a
# legitimate bump over it. (WATCHED and WATCHED_ASSETS need no such guard: the
# comparison above covers both by construction, so a presence difference between
# the two commits would have exited as "changed since" long before here.)
if [[ -z "$repointed" ]] && (( ${#watched_exec[@]} > 0 )); then
  for f in "${watched_exec[@]}"; do
    if [[ ! -f "$f" ]]; then
      echo "::warning::$f absent at this SHA — treating as decommissioned and bumping nothing. If any caller still pins it, retire those callers."
      emit false "$NEW_SHA"
      exit 0
    fi
  done
fi

emit true "$NEW_SHA"
