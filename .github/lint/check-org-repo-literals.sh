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
#      offset-aware scan `public-repo-hygiene` does; see limitation 4.
#   3. Both scan paths pass `-I`, so a tracked blob git classifies as BINARY is
#      never read: a UTF-16 file, a file carrying a stray NUL, or one
#      `.gitattributes` line (`*.md binary`) removes whole file types from the
#      scan and a green run says nothing about them. Text is the only surface
#      this lint claims.
#   4. Category 3 only. `.github/public-repo-hygiene/` is the rigorous,
#      org-wide implementation of this idea (it also covers ticket ids,
#      internal collaboration-tool links and the homoglyph case above). This
#      repo cannot adopt that caller as-is: it is that checker's own home, so
#      its tests and docs are full of deliberate fake-private fixtures, and it
#      references ticket ids by convention throughout. This lint is the subset
#      that this repo CAN enforce on itself today.
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
# The optional leading `@` is CAPTURED, not skipped: an `@`-prefixed match is a
# CODEOWNERS team handle or an npm/GitHub Packages scope, and team handles are
# allowlisted separately from repo names (see `@`-entries below). Without the
# capture the two share one namespace, so a team slug allowlisted for a
# CODEOWNERS fixture would also clear a literal reference to a private REPO of
# the same name. `public-repo-hygiene` splits the two for the same reason.
PATTERN="@?${ORG}/[A-Za-z0-9_.-]+"

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
script_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -f "$root/$allowlist_rel" ]; then
  allowlist="$root/$allowlist_rel"
elif [ -f "$script_repo_root/$allowlist_rel" ]; then
  allowlist="$script_repo_root/$allowlist_rel"
else
  echo "error: allowlist '$allowlist_rel' not found under '$root' or '$script_repo_root'" >&2
  exit 2
fi

# Two names no allowlist entry has any business matching. An entry that clears
# BOTH is not an allowlist line, it is an off switch: `*`, `**`, `?*` and
# `[a-z]*` all read as ordinary-looking edits while clearing every name that
# reaches the `case` below. Testing behaviour rather than spelling is what makes
# the guard cover the spellings nobody thought to enumerate; requiring BOTH
# probes keeps a narrow-but-odd glob from tripping it.
OVERBROAD_PROBE_A='zzq0probe-not-a-real-name-9f3d'
OVERBROAD_PROBE_B='qx7probe.not-a-real-name-2b8'

# Allowlist entries are shell globs, matched case-insensitively (GitHub
# resolves owner/repo names case-insensitively, so a case variant of an
# allowlisted PUBLIC name is the same repo — and no private name is on this
# list under any casing, so folding case cannot clear one).
#
# An entry written with a leading `@` is a TEAM/scope entry: it clears only an
# `@`-prefixed literal. A plain entry clears either spelling, because
# `@<org>/<name>` is also how npm and GitHub Packages write a package scope for
# a repo of that name — the same asymmetry `public-repo-hygiene` documents.
patterns=()
pattern_count=0
team_patterns=()
team_pattern_count=0
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
  # shellcheck disable=SC2254  # $entry is intentionally a glob, not a literal
  case "$OVERBROAD_PROBE_A" in
    $entry)
      # shellcheck disable=SC2254
      case "$OVERBROAD_PROBE_B" in
        $entry)
          echo "error: allowlist '$allowlist' entry '$line' matches arbitrary names, which allows every name" >&2
          exit 2
          ;;
      esac
      ;;
  esac
  if [ "$is_team" -eq 1 ]; then
    team_patterns+=("$entry")
    team_pattern_count=$((team_pattern_count + 1))
  else
    patterns+=("$entry")
    pattern_count=$((pattern_count + 1))
  fi
done < "$allowlist"

# Counted rather than `${#patterns[@]}`: bash 3.2 (still /bin/bash on macOS)
# treats an empty array as unset under `set -u`, so the guard would abort with
# `unbound variable` instead of this message. The `for` loops below are only
# reached with a non-empty array for the same reason.
if [ "$((pattern_count + team_pattern_count))" -eq 0 ]; then
  echo "error: allowlist '$allowlist' has no entries" >&2
  exit 2
fi

# Tracked files only when there is a work tree (build output and anything
# untracked is out of scope by construction); a plain recursive grep otherwise,
# which is the path the smoke test's fixture directory takes.
hits=''
scan_status=0
if git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # `--is-inside-work-tree` is true for any directory NESTED under a work tree,
  # not just its root, and the `.` pathspec resolves relative to `$root`. So a
  # `--root` below the top scans only that subtree — and if the subtree holds no
  # tracked files, `git grep` matches nothing, exits 1, and that is
  # indistinguishable from a clean tree. Fail closed instead, and report the
  # scope actually scanned rather than claiming the whole tracked tree.
  scope="the tracked files under '$root'"
  first_tracked="$(git -C "$root" ls-files -- . 2>/dev/null | head -n 1 || true)"
  if [ -z "$first_tracked" ]; then
    echo "error: no tracked files under '$root' — refusing to report a clean tree" >&2
    exit 2
  fi
  # The allowlist file itself is NOT excluded. Its entries are bare names that
  # cannot match PATTERN anyway, so an exclusion would buy only one thing:
  # letting its free-text rationale comments go unchecked, so that a line like
  # `foo  # mirrors <org>/<private>` would publish an unapproved name with CI
  # green. The non-git path below never excluded it either.
  hits="$(git -C "$root" grep -oiInE -- "$PATTERN" -- .)" || scan_status=$?
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

findings=0
while IFS= read -r hit; do
  [ -n "$hit" ] || continue
  # `file:line:match` — the match holds no colon, and neither does the line
  # number, so both come off the RIGHT. Cutting the file off the LEFT instead
  # would mangle any path that contains a colon.
  match="${hit##*:}"
  where="${hit%:*}"
  file="${where%:*}"
  file="${file#./}"
  lineno="${where##*:}"
  # An `@`-prefixed literal may additionally draw on the team/scope entries.
  is_at=0
  qualified="$match"
  case "$qualified" in
    @*) is_at=1; qualified="${qualified#@}" ;;
  esac
  name="${qualified#*/}"

  lower="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
  lower="$(trim_trailing_dots "$lower")"
  lower="${lower%.git}"
  lower="$(trim_trailing_dots "$lower")"
  # A reference left naming nothing (`<org>/.`, `<org>/.git`) is not a finding.
  [ -n "$lower" ] || continue

  allowed=0
  if [ "$pattern_count" -gt 0 ]; then
    for p in "${patterns[@]}"; do
      # shellcheck disable=SC2254  # $p is intentionally a glob, not a literal
      case "$lower" in
        $p) allowed=1; break ;;
      esac
    done
  fi
  if [ "$allowed" -eq 0 ] && [ "$is_at" -eq 1 ] && [ "$team_pattern_count" -gt 0 ]; then
    for p in "${team_patterns[@]}"; do
      # shellcheck disable=SC2254  # $p is intentionally a glob, not a literal
      case "$lower" in
        $p) allowed=1; break ;;
      esac
    done
  fi

  if [ "$allowed" -eq 0 ]; then
    findings=$((findings + 1))
    echo "$file:$lineno: $match is not on $allowlist_rel"
    echo "::error file=$file,line=$lineno::unapproved ${ORG} repo literal: $match"
  fi
done <<< "$hits"

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
