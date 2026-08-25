#!/usr/bin/env bash
#
# check-org-repo-literals.sh — allowlist lint for org-prefixed repo literals.
#
# This repo is PUBLIC, and AGENTS.md's first convention is "never leak private
# caller names". That rule was convention-enforced only. This makes it
# CI-enforced, the same move the workflow-pins lint made for the
# `workflows_ref` default rule.
#
# ALLOWLIST, NEVER DENYLIST. A denylist grep would have to commit the private
# names into this public repo to match them — the lint would BE the leak. So
# every org-prefixed repo literal in the tracked tree is a finding unless the
# name is on the committed allowlist. Adding a name is an allowlist edit,
# which review sees.
#
# KNOWN LIMITATIONS — read these before trusting a green run:
#   1. Only ORG-PREFIXED literals are caught. A bare repo name written without
#      the org prefix cannot be linted without committing a denylist that is
#      itself the leak. Bare-name discipline stays with review and AGENTS.md.
#   2. The name class is ASCII and the scan runs in the C locale, so a name
#      whose tail is non-ASCII -- a U+2010 homoglyph dash, a U+017F long s, CJK
#      text -- is read only as far as its ASCII PREFIX, and a private name
#      spelled that way can clear on an allowlisted prefix. The class cannot
#      simply be widened to high bytes: this tree legitimately writes an
#      ellipsis, an em dash and CJK text flush against a real name in prose, so
#      widening turns those into findings. Handling it properly needs the
#      offset-aware scan `public-repo-hygiene` does; see limitation 5.
#   3. Both scan paths pass `-I`, so a tracked blob git classifies as BINARY is
#      never read: a UTF-16 file, a file carrying a stray NUL, or one
#      `.gitattributes` line (`*.md binary`) removes whole file types from the
#      scan and a green run says nothing about them. Text is the only surface
#      this lint claims.
#   4. Only a literal WRITTEN WHOLE is caught. An org literal assembled at run
#      time -- `printf '%s/%s' '<org>' '<name>'`, `"${ORG}/<name>"`, a name
#      split across a string concatenation -- never matches PATTERN, so it needs
#      no allowlist edit while the run still reports its scope clean. This is
#      not hypothetical here: this lint's own smoke tests use exactly that
#      spelling on purpose, so it is the house style for org literals in this
#      repo's workflow files. It is also fully human-readable in the source, so
#      review is what catches it.
#   5. Category 3 only. `.github/public-repo-hygiene/` is the rigorous,
#      org-wide implementation of this idea (it also covers ticket ids,
#      internal collaboration-tool links and the homoglyph case above). This
#      repo cannot adopt that caller as-is: it is that checker's own home, so
#      its tests and docs are full of deliberate fake-private fixtures, and it
#      references ticket ids by convention throughout. This lint is the subset
#      that this repo CAN enforce on itself today.
#   6. A team `@` is judged by POSITION, not by grammar. An `@` glued to the
#      tail of an identifier (`user@<org>/<name>`, email-shaped) still reads as
#      a package scope, so such a literal may additionally draw on the
#      `@`-scoped TEAM entries. Reaching a private name through it needs the
#      name spelled exactly like an allowlisted team slug.
#      `public-repo-hygiene` reads the `@` from the same position and shares
#      this residual.
#   7. `_` counts as identifier-CONTINUATION in the left boundary, so a literal
#      written directly after an underscore is not read as a reference at all:
#      the markdown-italic spelling `_<org>/<name>_` matches NOTHING. That is
#      the same accepted trade `public-repo-hygiene` makes, kept identical on
#      purpose so a name cannot pass one checker and fail the other.
#   8. A LEFT boundary is not an owner-name boundary. The org segment must
#      start a token, but `-` is not an identifier character and IS legal in a
#      GitHub owner name, so a DIFFERENT owner whose name ends in this one --
#      `Not-${ORG}/x` -- satisfies the boundary and is read as one of this
#      org's repos, reddening a required lint on a reference that has nothing
#      to do with us. Only the unhyphenated `Not${ORG}/x` spelling is excluded.
#      This is a FALSE POSITIVE, not a miss, and it is kept rather than fixed
#      so the boundary stays byte-identical to `public-repo-hygiene`'s
#      `REPO_REF_RE` -- a name must not pass one checker and fail the other.
#      Both spellings are pinned by the smoke tests. Widening the class is a
#      change to BOTH checkers, not to this one.
#   9. CONTENTS only. PATTERN is applied to what a tracked file CONTAINS, never
#      to the tracked PATH itself and never to a symlink's target string
#      (neither scan path reads one: `git grep` skips the non-regular worktree
#      entry, and `grep -r` does not follow a discovered link). A name
#      published as a directory component -- `docs/<org>/<name>/placeholder` --
#      or as a link target is therefore not a finding here.
#      `public-repo-hygiene` does scan a symlink's target string; neither
#      checker scans the tracked path itself.
#  10. Two ADJACENT literals scan as ONE. `grep -o` takes non-overlapping
#      matches, and the name class holds `.` and `-`, which are also characters
#      that can SEPARATE two literals -- so `<org>/public.<org>/private` is a
#      SINGLE match whose name reads `public.<org>`. The second name is never
#      compared, never printed, and the scan resumes at `/private`, which no
#      longer matches. It fails CLOSED, because a merged token always ends in
#      the org name and nothing on the allowlist does, so such a line is always
#      a finding. The residual is the REMEDY, not the miss: the finding quotes a
#      token that is not a repo, and the obvious way to silence something so
#      confusing is to allowlist the quoted token -- which would clear the
#      private reference with the run green. So the loader REJECTS an entry
#      ending in `.<org>` or `-<org>`, the only spelling that could do it.
#  11. The per-hit loop is LINEAR IN THE HIT COUNT, and `MAX_REPORTED` does not
#      bound it: past the cap a hit stops printing but is still folded,
#      normalized and compared against the list, because the reported count and
#      the exit status have to stay the true ones. Measured after the fold above
#      was replaced by a glob: ~5.4ms per hit past the cap (2000 hits in one
#      tracked file, 11.7s, against 2.1s at 200), so the caller's
#      `timeout-minutes: 10` is reached at roughly 110,000 hits in one scan --
#      and `$hits` buffers the whole scan before the loop starts. Documented
#      rather than capped: this tree's largest tracked file is under 600 lines,
#      and capping the scan would trade an unreachable timeout for a truncated
#      count, which is the one number a red run's summary turns on.
#
# TAMPER BOUNDARY: unlike the reusable checkers this repo publishes, this lint
# runs from the PR's own checkout, so a PR here can edit both the script and the
# allowlist. That is the same boundary the workflow-pins lint has and it is the
# point -- widening the allowlist is a reviewed diff. It is NOT a control against
# a hostile committer.
#
# Usage: bash .github/lint/check-org-repo-literals.sh [--root DIR]
#                                                     [--allowlist REPO/REL/PATH]
# Exit: 0 clean, 1 findings, 2 usage/setup error.

set -euo pipefail

# Byte-wise, deterministic matching everywhere. `grep -i` case-folds according
# to the LOCALE, so in a UTF-8 locale it widens `[A-Za-z]` to reach U+017F and
# U+212A while in the POSIX locale it does not -- and the runner's locale is not
# something this lint should depend on. `LC_ALL=C` pins ASCII-only folding for
# both `grep` and the `tr` below, so a green run here means the same thing it
# means on CI.
export LC_ALL=C

# Both `cd -- "$root"` calls below take a caller-supplied, possibly RELATIVE
# path. With `CDPATH` exported, `cd` can land in a same-named directory that is
# not the one `[ -d "$root" ]` validated -- and it echoes the resolved absolute
# path on stdout, which lands inside a command substitution and reaches the hit
# loop as a bogus `file:line:match` line.
unset CDPATH

# `GIT_DIR`, `GIT_WORK_TREE` and `GIT_INDEX_FILE` OVERRIDE `git -C`, so with any
# of them set in the environment the probe and the scan below answer about a
# DIFFERENT repository while the OK line still reports the scope as `$root`
# (measured: `GIT_DIR=b/.git GIT_WORK_TREE=b git -C a grep -l -e ''` lists b's
# files, not a's). Same class of hazard as `CDPATH`, unset in the same place.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

# The config-INJECTION variables, unset for the same reason but against a
# hazard that fails the other way. `GIT_CONFIG_COUNT` with `GIT_CONFIG_KEY_n`/
# `GIT_CONFIG_VALUE_n`, and `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM`, can set
# `core.attributesFile` and so mark a subset of the tree `binary` -- which
# `git grep -I` then silently skips. That is limitation 3's hazard delivered
# through the environment rather than through a tracked `.gitattributes`, and
# the `-c grep.*`/`color.grep`/`core.quotePath` pins on the scan cannot cover it
# because the injected key is not one of the pinned ones.
#
# Unlike `GIT_DIR` this one fails OPEN: `first_scannable` still finds a file, so
# the guard clears, the scan reads LESS than the tree, and the OK line still
# claims "the tracked files under '$root'". `GIT_CONFIG_KEY_n`/`_VALUE_n` are
# inert once `GIT_CONFIG_COUNT` is gone (git reads the count first and then
# exactly that many pairs), so the three names below are the whole mechanism.
unset GIT_CONFIG_COUNT GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM

ORG='Comfy-Org'
# Deliberately assembled rather than written whole: a literal org-prefixed name
# anywhere in this file would be a finding against this very lint.
#
# Matched case-INSENSITIVELY (`grep -i`), because GitHub resolves owner names
# case-insensitively: a lowercased org spelling reaches the same repository, so
# matching only the canonical spelling would leave a one-keystroke bypass of a
# default-deny control. The name class is already both cases, so `-i` widens
# nothing but the org segment.
#
# The org segment must START A TOKEN. `grep -E` has no lookbehind, so the
# boundary is written as an alternation and the character in front of the org is
# CONSUMED by the match (the loop below drops it again). Without it `grep -o`
# reads straight into the middle of a longer token, so `Not${ORG}/whatever` --
# a DIFFERENT owner -- is extracted as one of this org's repos and reddens CI on
# a reference that has nothing to do with us. The boundary class is
# `public-repo-hygiene`'s `REPO_REF_RE` lookbehind verbatim, so the two agree on
# what counts as a reference: every real spelling is preceded by a separator
# (`/` in a URL, whitespace, a quote, or the `@` of a team handle), and none of
# those is in the class. The class treats `_` as identifier-continuation, so the
# markdown-italic spelling `_${ORG}/<name>_` is NOT read as a reference -- the
# same accepted trade that checker makes, kept identical on purpose so a name
# cannot pass one checker and fail the other.
#
# The token boundary is NOT an owner-name boundary, and the difference is
# limitation 8: `-` is legal in a GitHub owner name but is not an identifier
# character, so only the UNHYPHENATED `Not${ORG}/x` is excluded here --
# `Not-${ORG}/x`, an equally real different owner, still satisfies the
# alternation and is read as one of ours. Kept for byte-parity with that
# checker rather than fixed here; widening the class is a change to both.
#
# The `@` of a team handle is that boundary character rather than part of the
# pattern. An `@`-prefixed match is a CODEOWNERS team handle or an npm/GitHub
# Packages scope, and team handles are allowlisted separately from repo names
# (see `@`-entries below); without the split the two share one namespace, so a
# team slug allowlisted for a CODEOWNERS fixture would also clear a literal
# reference to a private REPO of the same name. `public-repo-hygiene` splits
# them the same way and reads the `@` from the same position -- including its
# residual, which this shares: the `@` is judged by POSITION alone, so one glued
# to the tail of an identifier (`user@${ORG}/<name>`, email-shaped) still reads
# as a scope. Reaching a private name through that needs the name to be spelled
# exactly like an allowlisted TEAM slug.
PATTERN="(^|[^A-Za-z0-9_])${ORG}/[A-Za-z0-9_.-]+"

# Lowercased once for the `case` tests in the hit loop (matching is
# case-insensitive, and bash 3.2 has no `${var,,}`).
org_lc="$(printf '%s' "$ORG" | tr '[:upper:]' '[:lower:]')"

# The same org name as a case-insensitive GLOB — `Comfy-Org` becomes
# `[Cc][Oo][Mm][Ff][Yy]-[Oo][Rr][Gg]`. Built ONCE here (two forks total) so the
# hit loop can decide the `@`/boundary case with a `case` test instead of
# folding the match's head through `printf | tr`, which cost two forks on every
# hit. Letters become a two-character class; every other byte -- `-` here, which
# is literal outside a bracket expression -- is copied as-is, so the glob
# matches exactly the names the fold did and no others.
org_uc="$(printf '%s' "$ORG" | tr '[:lower:]' '[:upper:]')"
org_glob=''
i=0
while [ "$i" -lt "${#ORG}" ]; do
  lc_char="${org_lc:$i:1}"
  uc_char="${org_uc:$i:1}"
  if [ "$lc_char" = "$uc_char" ]; then
    org_glob="$org_glob$lc_char"
  else
    org_glob="${org_glob}[$uc_char$lc_char]"
  fi
  i=$((i + 1))
done

root='.'
allowlist_rel='.github/lint/org-repo-allowlist.txt'

while [ $# -gt 0 ]; do
  case "$1" in
    --root)
      [ $# -ge 2 ] || { echo "error: --root needs a directory" >&2; exit 2; }
      root="$2"
      shift 2
      ;;
    --allowlist)
      [ $# -ge 2 ] || { echo "error: --allowlist needs a file" >&2; exit 2; }
      allowlist_rel="$2"
      shift 2
      ;;
    -h|--help)
      # The whole leading comment block, minus the shebang, is the help text.
      sed -n '2,${/^[^#]/q;p;}' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "error: unknown argument '$1'" >&2
      exit 2
      ;;
  esac
done

[ -d "$root" ] || { echo "error: --root '$root' is not a directory" >&2; exit 2; }

# The allowlist is resolved against the REPO being scanned when it has one, and
# otherwise against this script's own repo — that is what lets the smoke test
# point --root at a bare fixture directory and still get the real allowlist.
# `|| exit 2`: this is the one setup step that could otherwise exit 1 -- the
# status the header documents as FINDINGS. If the `cd`/`pwd` fails (this
# script's own directory removed or replaced under it, a stale mount,
# `BASH_SOURCE[0]` resolving somewhere unenterable) the substitution returns
# non-zero, the assignment fails, and `set -e` would exit 1 with only bash's
# `cd:` message and no finding lines -- the same hole the `-r` guard below
# closes, on a line that runs ahead of every `-d`/`-f`/`-r` check.
script_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || {
  echo "error: cannot resolve this script's repo root from '${BASH_SOURCE[0]}'" >&2
  exit 2
}
if [ -f "$root/$allowlist_rel" ]; then
  allowlist="$root/$allowlist_rel"
elif [ -f "$script_repo_root/$allowlist_rel" ]; then
  allowlist="$script_repo_root/$allowlist_rel"
else
  echo "error: allowlist '$allowlist_rel' not found under '$root' or '$script_repo_root'" >&2
  exit 2
fi
# A regular but UNREADABLE allowlist clears the `-f` tests above, and the
# redirection that reads it below would then fail under `set -e` with status 1 --
# the status documented as FINDINGS, so a caller could not tell an unusable setup
# from a completed scan that found something. Every other setup failure here is
# exit 2; so is this one.
[ -r "$allowlist" ] || { echo "error: allowlist '$allowlist' is not readable" >&2; exit 2; }

# Three shapes of entry are rejected outright rather than loaded, because each
# of them reads as an ordinary allowlist line while doing something other than
# what its author meant.
#
# A GLOB (`*`, `**`, `?*`, `[a-z]*`, `secret-*`) is the first. Note what this
# guard is and is NOT today: the comparisons below are exact (`[ "$lower" =
# "$known" ]`), so a `*` entry already clears nothing but a literal `*` -- no
# entry reaches the comparison AS A PATTERN any more. The rejection is
# DEFENCE-IN-DEPTH against that comparison going back to a `case` glob, kept so
# that a later reader "simplifying" `=` into one does not silently re-arm every
# metacharacter on the list. It is also why an earlier revision's approach --
# testing each entry against two fixed nonsense probe names -- was dropped: two
# probes cannot decide breadth in general (`[!qz]*`, `[a-p]*` and `[!q-z]*`
# match NEITHER probe and still clear almost the whole namespace), whereas
# rejecting the metacharacters is deterministic and free. Enumerating a fixture
# family rather than globbing it is already the documented house style.
#
# An entry containing `/` is the second, and unlike the glob it is a live
# foot-gun rather than a latent one. The comparisons below run against the BARE
# name (`${lower#*/}`), so `<org>/foo` can never equal an entry -- yet `<org>/foo`
# is exactly the spelling the tool invites, because the finding line quotes the
# whole literal and the summary says to add it to the allowlist. Accepting it
# would yield an inert line and a rerun still insisting the name is not on a
# file that visibly contains it. Rejected with the spelling that does work.
#
# An entry whose name ENDS in `.<org>` or `-<org>` is the third; see limitation
# 10. `grep -o` takes non-overlapping matches and the name class contains the
# characters that also separate two adjacent literals, so
# `<org>/public-name.<org>/private-name` is ONE match whose name reads
# `public-name.<org>` -- and the second name is never compared, never printed,
# and the scan resumes past it. It fails closed today (that merged token is on
# no list), but the obvious way to silence so confusing a finding is to
# allowlist the token it quotes, and THAT would clear the private reference with
# CI green. Making the merged token unallowlistable removes the foot-gun; a real
# repo name in this shape is a namespace collision with the merge artefact and
# has to be spelled some other way regardless.

# Allowlist entries are LITERAL names compared case-insensitively (GitHub
# resolves owner/repo names case-insensitively, so a case variant of an
# allowlisted PUBLIC name is the same repo — and no private name is on this
# list under any casing, so folding case cannot clear one).
#
# An entry written with a leading `@` is a TEAM/scope entry: it clears only an
# `@`-prefixed literal. A plain entry clears either spelling, because
# `@<org>/<name>` is also how npm and GitHub Packages write a package scope for
# a repo of that name — the same asymmetry `public-repo-hygiene` documents.
entries=()
entry_count=0
team_entries=()
team_entry_count=0
# `|| [ -n "$line" ]` so a final entry with no trailing newline is not dropped.
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"                       # strip the trailing "why it's safe" comment
  line="${line#"${line%%[![:space:]]*}"}"  # ltrim
  line="${line%"${line##*[![:space:]]}"}"  # rtrim
  [ -n "$line" ] || continue
  entry="$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]')"
  is_team=0
  case "$entry" in
    @*) is_team=1; entry="${entry#@}" ;;
  esac
  [ -n "$entry" ] || { echo "error: allowlist '$allowlist' has an entry that is a bare '@'" >&2; exit 2; }
  case "$entry" in
    *[][*?]*)
      echo "error: allowlist '$allowlist' entry '$line' contains a glob metacharacter ([, ], * or ?); entries are literal names, and a glob can allow every name" >&2
      exit 2
      ;;
  esac
  case "$entry" in
    */*)
      echo "error: allowlist '$allowlist' entry '$line' contains '/'; entries are bare repo NAMES compared against the name alone, so an entry written as '$ORG/<name>' can never match anything — write just '<name>'" >&2
      exit 2
      ;;
  esac
  case "$entry" in
    *[.-]"$org_lc")
      echo "error: allowlist '$allowlist' entry '$line' ends in '.$ORG' or '-$ORG'; that is the shape of a MERGED literal (see limitation 10, '<org>/a.<org>/b' scans as one match) and allowlisting it would clear the second, unexamined name with the run green" >&2
      exit 2
      ;;
  esac
  if [ "$is_team" -eq 1 ]; then
    team_entries+=("$entry")
    team_entry_count=$((team_entry_count + 1))
  else
    entries+=("$entry")
    entry_count=$((entry_count + 1))
  fi
done < "$allowlist"

# Counted rather than `${#entries[@]}`: bash 3.2 (still /bin/bash on macOS)
# treats an empty array as unset under `set -u`, so the guard would abort with
# `unbound variable` instead of this message. The `for` loops below are only
# reached with a non-empty array for the same reason.
if [ "$((entry_count + team_entry_count))" -eq 0 ]; then
  echo "error: allowlist '$allowlist' has no entries" >&2
  exit 2
fi

# Tracked files only when there is a work tree (build output and anything
# untracked is out of scope by construction); a plain recursive grep otherwise,
# which is the path the smoke test's fixture directory takes.
hits=''
scan_status=0
# The OUTPUT is tested, not the exit status: `rev-parse --is-inside-work-tree`
# answers on stdout and exits 0 while printing `false` for a bare repo and for a
# path under `.git`. On the status test those roots took the git branch, where
# `git grep` cannot run, and died with the misleading `no scannable tracked text
# file` instead of using the fallback that exists for exactly them.
if [ "$(git -C "$root" rev-parse --is-inside-work-tree 2>/dev/null)" = true ]; then
  # `--is-inside-work-tree` is true for any directory NESTED under a work tree,
  # not just its root, and the `.` pathspec resolves relative to `$root`. So a
  # `--root` below the top scans only that subtree — and if the subtree holds no
  # tracked files, `git grep` matches nothing, exits 1, and that is
  # indistinguishable from a clean tree. Fail closed instead, and report the
  # scope actually scanned rather than claiming the whole tracked tree.
  #
  # The probe has to be THE SAME READ the scan makes, not merely a read of the
  # same tree. `git ls-files` enumerates the INDEX, which counts blobs the scan
  # below skips: a scope whose tracked files are all binary (one `*.md binary`
  # attribute line, a UTF-16 subtree, `--root assets/`) or all empty has a
  # non-empty index while `git grep -I` reads none of it, so an index probe
  # would clear and the scan would then exit 1 -- "no matches" -- and print the
  # clean-tree OK line for a tree it never read. `git grep -Il -e ''` lists
  # exactly the files the scan can open, which is also what the fallback's
  # `grep -rIl -e ''` probe lists, so both paths give a binary-only tree the
  # same verdict. `-E` is passed for the same reason the scan passes it: to
  # override a `grep.patternType` that would reinterpret the empty pattern.
  #
  # `|| true` and an emptiness test rather than a status test, because `head`
  # closing the pipe early makes `git grep` die of SIGPIPE and `pipefail` would
  # read that as a scan error on a perfectly good tree.
  scope="the tracked files under '$root'"
  first_scannable="$(git -C "$root" grep -IlE -e '' -- . 2>/dev/null | head -n 1 || true)"
  if [ -z "$first_scannable" ]; then
    echo "error: no scannable tracked text file under '$root' — refusing to report a clean tree" >&2
    exit 2
  fi
  # The allowlist file itself is NOT excluded. Its entries are bare names that
  # cannot match PATTERN anyway, so an exclusion would buy only one thing:
  # letting its free-text rationale comments go unchecked, so that a line like
  # `foo  # mirrors <org>/<private>` would publish an unapproved name with CI
  # green. The non-git path below never excluded it either.
  #
  # The `-c grep.*` pins are the same move as `LC_ALL=C` above: `git grep`
  # honours `grep.column`, `grep.lineNumber` and `grep.fullName` from system or
  # global config and from `GIT_CONFIG_*`, and `grep.column=true` alone turns the
  # output into `file:line:col:match`, which the right-anchored parse below reads
  # as `file:line` + a column-as-line-number. Findings would still fire (the match
  # text is authoritative) but every printed location and `::error` annotation
  # would point at the wrong place. `-E` is passed explicitly for the same reason
  # and overrides `grep.patternType`.
  #
  # `color.grep` and `core.quotePath` reshape the same output and are pinned
  # alongside them: `color.grep=always` injects ANSI bytes into the match text,
  # which corrupts the lowercased name so an allowlisted entry stops matching,
  # and `core.quotePath=true` (git's DEFAULT) emits a path holding a non-ASCII
  # byte quoted and octal-escaped, so the `::error file=` annotation would name
  # a path that does not exist -- while the fallback `grep -r` prints it raw.
  #
  # `grep.fullName=TRUE` is the pinned value, not `false`. Of the two stable
  # settings only one keeps a subtree run's locations usable: with `false` git
  # prints paths relative to the CWD, which `git -C "$root"` has set to `$root`,
  # so `--root docs` reports a hit in `docs/x.md` as `x.md` and emits
  # `::error file=x.md` -- an annotation GitHub resolves against the repo root
  # and then drops. `true` prints it relative to the repo top, which is the
  # frame the annotation is read in. Both settings are equally immune to the
  # `file:line:col` parse regression the rest of these pins exist for (neither
  # adds a field), so pinning the correct one costs nothing.
  hits="$(git -C "$root" -c grep.column=false -c grep.lineNumber=true -c grep.fullName=true -c color.grep=false -c core.quotePath=false grep -oiInE -- "$PATTERN" -- .)" || scan_status=$?
else
  # `cd` + a `.` root rather than grepping "$root" and stripping the prefix
  # afterwards: the strip would be a `sed` expression built from a path this
  # script does not control.
  #
  # `|| exit 3` rather than `&&`: a failed `cd` would otherwise exit 1, which is
  # grep's "no matches" status, and the guard below would wave it through as a
  # clean tree. `[ -d "$root" ]` above does not cover it (a directory with no
  # execute bit, or a TOCTOU replacement between the two).
  scope="'$root'"
  # The same "refusing to report a clean tree" guard the git path has, for the
  # same reason: `grep` exits 1 both for "no matches" and for a root that holds
  # nothing this scan can read, so a stale or mistyped `--root` would otherwise
  # print the clean-tree OK line having scanned nothing -- fail-open on exactly
  # the path the git branch fails closed on. `-rIl -e ''` enumerates precisely
  # what the scan below can read: every file it would open, with the same `-I`
  # excluding the binaries it would skip. An empty result therefore means there
  # was no scannable file, whether because the tree is empty, because everything
  # in it is binary, or because the root could not be entered at all.
  #
  # `|| true` and an emptiness test rather than a status test: `head` closing the
  # pipe early makes `grep` die of SIGPIPE, and under `set -o pipefail` that
  # status would read as a scan error on a perfectly good tree. Any real failure
  # still lands on empty output here, which is exit 2 either way.
  first_scannable="$(
    cd -- "$root" 2>/dev/null || exit 0   # unenterable: no scannable file, same verdict
    grep -rIl --exclude-dir=.git -e '' . 2>/dev/null | head -n 1 || true
  )"
  if [ -z "$first_scannable" ]; then
    echo "error: no readable text file under '$root' — refusing to report a clean tree" >&2
    exit 2
  fi
  hits="$(cd -- "$root" || exit 3; grep -rIoiEn --exclude-dir=.git -- "$PATTERN" .)" || scan_status=$?
fi
# Both tools exit 1 for "no matches" and >1 for a real error. Swallowing the
# latter with `|| true` would report a clean tree because the scan never ran --
# a guard that fails OPEN is worse than no guard, since the green run reads as
# coverage.
if [ "$scan_status" -gt 1 ]; then
  echo "error: the scan failed (exit $scan_status) — refusing to report a clean tree" >&2
  exit 2
fi

# Trailing sentence punctuation the name class swallowed, then a `.git` suffix
# (`Foo.git` still references the repo `Foo`), then punctuation again --
# `<org>/foo.git.` needs both passes, and stripping `.git` first leaves the
# suffix stuck behind the period.
#
# ONLY `.` is stripped. A GitHub slug may not END in a period, so dropping one
# can only narrow the name. `-` and `_` are different: both are LEGAL as a
# slug's last character, so stripping them would WIDEN the match — a literal for
# a private repo named `<allowlisted>-` or `<allowlisted>_` would normalize onto
# the allowlisted entry and clear this default-deny check.
#
# The peel is quadratic in the length of the run, so it is only safe because
# MAX_NAME below bounds its input first. There is no cheap pure-bash
# alternative: `${value%%"${value##*[!.]}"}`, the obvious two-expansion
# rewrite, is quadratic in bash as well (measured: on a 200 KB run of periods
# both forms run past two minutes).
trim_trailing_dots() {
  local value="$1"
  while [ -n "$value" ]; do
    case "$value" in
      *.) value="${value%?}" ;;
      *) break ;;
    esac
  done
  printf '%s' "$value"
}

# GitHub workflow-command escaping. A tracked PATH is untrusted input to the
# runner -- it reaches both output lines below -- and `,`/`:` in it would
# otherwise corrupt the `file=`/`line=` properties so the annotation points
# nowhere, while `%`/CR would corrupt the message. `%` must be replaced FIRST or
# it re-escapes the escapes.
escape_data() {
  local value="$1"
  value="${value//'%'/%25}"
  value="${value//$'\r'/%0D}"
  value="${value//$'\n'/%0A}"
  printf '%s' "$value"
}
escape_property() {
  local value
  value="$(escape_data "$1")"
  value="${value//':'/%3A}"
  value="${value//','/%2C}"
  printf '%s' "$value"
}

# A tracked tree can hold an unbounded number of unapproved literals, and every
# one of them prints twice. Past this many the count keeps rising (the summary
# below still reports the true total, and the exit status is still 1) but the
# per-finding lines stop, so a badly-seeded allowlist floods neither the public
# run log nor the annotation list. `public-repo-hygiene` caps the same two ways
# (`MAX_FINDINGS_PER_FILE`/`MAX_FINDINGS_TOTAL`).
MAX_REPORTED=200

# GitHub's own repo-name limit; see the cap in the loop below.
MAX_NAME=100

# How many bytes NORMALIZATION is allowed to peel off a name before the length
# test gives up on it. Normalization only ever removes a SUFFIX -- trailing
# periods, then at most one `.git`, then trailing periods again -- so a name
# that is over the limit AS WRITTEN can still normalize onto an allowlisted
# one, and testing the length first is what forced `<org>/<98-char>.git`, or a
# 100-character name followed by a sentence period, into `oversize`. That
# matters because `oversize` skips BOTH allowlist comparisons, so the finding
# named a remedy that could not silence it.
#
# The test therefore runs after the strips, and this bounds their input: 8 bytes
# is `.git` plus a sentence period plus an ellipsis, which covers every way a
# real reference gets punctuated in prose. A longer run of periods is still
# `oversize` -- fail-closed, and it is what keeps `trim_trailing_dots`, whose
# peel is quadratic in the run it removes, off unbounded input.
MAX_STRIP=8

findings=0
while IFS= read -r hit; do
  [ -n "$hit" ] || continue
  # `file:line:match` — the line number holds no colon, so both come off the
  # RIGHT. Cutting the file off the LEFT instead would mangle any path that
  # contains a colon.
  #
  # `${hit%:*}` and then an OFFSET, rather than the equivalent `${hit##*:}`:
  # `##` re-matches `*:` against a progressively shorter prefix, so it is
  # QUADRATIC in the length of the match text it removes, and the name class is
  # unbounded. Measured on one tracked line of `${ORG}/foo` + 400 KB of name
  # characters: 62 seconds here, against 65 for the whole run. `%` and
  # `${var:offset}` are both linear, and split at exactly the same colon.
  where="${hit%:*}"
  match="${hit:$((${#where} + 1))}"
  # The boundary character PATTERN consumes may ITSELF be a colon: `uses:`,
  # `repository:` and prose like `see:${ORG}/x` are all real spellings, and each
  # puts a fourth colon on the line, shifting the right-anchored split by one
  # field. A line number is never empty, so an empty trailing field is that case
  # and nothing else.
  if [ -z "${where##*:}" ]; then
    match=":$match"
    where="${where%:}"
  fi
  file="${where%:*}"
  file="${file#./}"
  lineno="${where##*:}"

  # A line number is always DIGITS, so anything else means this record is not a
  # `file:line:match` triple and the split above produced three fabrications.
  # The way that happens is a tracked path containing a NEWLINE: both scan paths
  # print the path raw (`core.quotePath=false` on the git side, `grep -r` on the
  # other), so one hit arrives as two records and the leading fragment yields a
  # file that does not exist, a `line=` that is a path, and a "literal" cut out
  # of the filename. Reporting it would also put the raw `:`/`,` of a path into
  # the `file=`/`line=` properties that `escape_property` exists to protect.
  #
  # Refused rather than escaped or skipped: escaping would still publish the
  # fabricated finding, and skipping would drop a record on a parse this script
  # has already lost confidence in. Exit 2 is the same "refusing to report"
  # verdict every other unusable-scan branch here returns.
  case "$lineno" in
    ''|*[!0-9]*)
      echo "error: could not parse the scan output — expected 'file:line:match', got a non-numeric line field. A tracked path containing a newline splits one record in two; rename it, or narrow --root past it." >&2
      exit 2
      ;;
  esac

  # `grep -E` has no lookbehind, so a match that did not start at column 0
  # carries the boundary character in front of the literal. Drop it -- and read
  # the team/scope namespace off it first, because an `@` in that position is
  # the `@` of a CODEOWNERS handle or a package scope, which may additionally
  # draw on the team entries. The `@` stays on `$match` so the finding quotes
  # the literal as written and "add it to the allowlist" stays a copy-paste.
  #
  # Decided from a BOUNDED head slice (`@` + org + `/` is all it takes), not
  # from the whole match folded to lower case: the match text is unbounded, and
  # this runs on every hit including the oversize ones the block below skips.
  #
  # The case-insensitive comparison is a GLOB built once from `$ORG`
  # (`[Cc][Oo]...`), not a `printf | tr` fold. The fold cost two forks on EVERY
  # hit -- including every hit past `MAX_REPORTED`, which prints nothing and so
  # buys only the count -- and it was the larger half of the per-hit cost.
  # Measured on a fixture of 2000 unapproved literals in one tracked file:
  # 18.4s before, 11.7s after (8.6ms -> 5.4ms per hit past the printing cap).
  # The glob is exact (each letter becomes a
  # two-character class, every other byte is copied literally), so it decides
  # the same three cases the fold did.
  match_head="${match:0:$((${#ORG} + 2))}"
  is_at=0
  case "$match_head" in
    $org_glob/*) ;;                     # column 0 — no boundary was consumed
    "@"$org_glob/*) is_at=1 ;;
    *) match="${match#?}" ;;
  esac

  # COST bound, applied before anything folds or forks on the WHOLE match text.
  # A match longer than `org + '/' + MAX_NAME` (plus the `@` a scope keeps)
  # cannot spell a name of MAX_NAME characters or fewer, so the exact
  # `${#name}` test below would reach the same verdict; this only skips the work
  # of getting there. Deliberately coarse — the exact rule stays in one place.
  lower=''
  oversize=0
  if [ "${#match}" -gt "$((MAX_NAME + MAX_STRIP + ${#ORG} + 2))" ]; then
    oversize=1
  fi

  if [ "$oversize" -eq 0 ]; then
    lower="$(printf '%s' "$match" | tr '[:upper:]' '[:lower:]')"
    name="${lower#*/}"

    # A GitHub repo slug is at most MAX_NAME characters, so a name longer than
    # that AFTER normalization is not a reference to a real repository under any
    # normalization and therefore cannot be on the allowlist — it is reported
    # without being compared at all. The `MAX_STRIP` headroom is what lets the
    # strips run first: peeling is the only thing that can bring an over-long
    # name back under the limit, and skipping it turned `<org>/<98-char>.git`
    # into an `oversize` finding whose stated remedy — the allowlist — is
    # skipped for exactly that class. Past the headroom the peel is refused
    # rather than run, because `trim_trailing_dots`'s `${value%?}` is quadratic
    # in the run it removes: unbounded, one tracked line of `${ORG}/foo`
    # followed by a long run of periods ran for over two minutes, which would
    # burn the caller's whole `timeout-minutes` and turn a would-be finding into
    # an inconclusive run.
    #
    # Both bounds fail CLOSED — an over-long name is always reported, never
    # cleared — so neither can let a name through. For scale: the longest
    # literal really in this tree is 29 characters.
    if [ "${#name}" -gt "$((MAX_NAME + MAX_STRIP))" ]; then
      oversize=1
    else
      lower="$(trim_trailing_dots "$name")"

      lower="${lower%.git}"
      lower="$(trim_trailing_dots "$lower")"
      # A reference left naming nothing (`<org>/.`, `<org>/.git`) is not a
      # finding.
      [ -n "$lower" ] || continue
      # The exact test, now that the strips have run and there is a real name to
      # measure. Only a name that is STILL over the limit after normalization is
      # unallowlistable.
      if [ "${#lower}" -gt "$MAX_NAME" ]; then
        oversize=1
      fi
    fi
  fi

  allowed=0
  if [ "$oversize" -eq 0 ] && [ "$entry_count" -gt 0 ]; then
    for known in "${entries[@]}"; do
      if [ "$lower" = "$known" ]; then allowed=1; break; fi
    done
  fi
  if [ "$allowed" -eq 0 ] && [ "$oversize" -eq 0 ] && [ "$is_at" -eq 1 ] && [ "$team_entry_count" -gt 0 ]; then
    for known in "${team_entries[@]}"; do
      if [ "$lower" = "$known" ]; then allowed=1; break; fi
    done
  fi

  if [ "$allowed" -eq 0 ]; then
    findings=$((findings + 1))
    # Bound the literal before it is printed. The name class is unbounded, so
    # one tracked line of `<org>/` followed by a long run of name characters is
    # a SINGLE match whose text is most of the file -- printed once to stdout
    # and once more as an `::error` annotation. `public-repo-hygiene` bounds the
    # same token for the same reason (`_bounded`, 200 chars); the cut is
    # display-only, so what is compared against the allowlist is still the whole
    # name and a truncated finding can never be a cleared one.
    if [ "$findings" -le "$MAX_REPORTED" ]; then
      shown="$match"
      if [ "${#shown}" -gt 200 ]; then
        shown="${shown:0:200}... (truncated)"
      fi
      # `unapproved:` is a CONSTANT prefix, and it is load-bearing: without it
      # this line starts with the tracked path, so a tracked file named
      # `::stop-commands::x.md` would emit a workflow COMMAND at column zero
      # that suppresses every `::error` annotation after it. A path cannot
      # reach column zero here.
      if [ "$oversize" -eq 1 ]; then
        why="is longer than GitHub's ${MAX_NAME}-character repo-name limit, so it cannot be allowlisted"
      else
        why="is not on $allowlist_rel"
      fi
      echo "unapproved: $file:$lineno: $shown $why"
      echo "::error file=$(escape_property "$file"),line=$lineno::unapproved ${ORG} repo literal: $(escape_data "$shown") $(escape_data "$why")"
    fi
  fi
done <<< "$hits"

if [ "$findings" -gt "$MAX_REPORTED" ]; then
  echo "note: $((findings - MAX_REPORTED)) further finding(s) were counted but not printed (cap: $MAX_REPORTED)." >&2
fi

if [ "$findings" -gt 0 ]; then
  cat >&2 <<MSG

$findings unapproved ${ORG} repo literal(s) found.

If the name is a PUBLIC repo, a documentation example or a test fixture, add it
to $allowlist_rel with a trailing '#' comment saying why it is safe. If it is a
private repo, remove the reference — this repo is public.
MSG
  exit 1
fi

echo "OK: every ${ORG} repo literal in $scope is on $allowlist_rel"
