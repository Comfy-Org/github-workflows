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
# every `Comfy-Org/<name>` literal in the tracked tree is a finding unless
# <name> is on the committed allowlist. Adding a name is an allowlist edit,
# which review sees.
#
# KNOWN LIMITATIONS — read these before trusting a green run:
#   1. Only ORG-PREFIXED literals are caught. A bare repo name written without
#      the `Comfy-Org/` prefix cannot be linted without committing a denylist
#      that is itself the leak. Bare-name discipline stays with review and
#      AGENTS.md.
#   2. The name class is ASCII and the scan runs in the C locale, so a name
#      whose tail is non-ASCII -- a U+2010 homoglyph dash, a U+017F long s, CJK
#      text -- is read only as far as its ASCII PREFIX, and a private name
#      spelled that way can clear on an allowlisted prefix. Handling that needs
#      the offset-aware scan `public-repo-hygiene` does; see limitation 3.
#   3. Category 3 only. `.github/public-repo-hygiene/` is the rigorous,
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
# case-insensitively: `comfy-org/<private-repo>` reaches the same repository, so
# matching only the canonical spelling would leave a one-keystroke bypass of a
# default-deny control. The name class is already both cases, so `-i` widens
# nothing but the org segment.
PATTERN="${ORG}/[A-Za-z0-9_.-]+"

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

# Allowlist entries are shell globs, matched case-insensitively (GitHub
# resolves owner/repo names case-insensitively, so a case variant of an
# allowlisted PUBLIC name is the same repo — and no private name is on this
# list under any casing, so folding case cannot clear one).
patterns=()
pattern_count=0
# `|| [ -n "$line" ]` so a final entry with no trailing newline is not dropped.
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"                       # strip the trailing "why it's safe" comment
  line="${line#"${line%%[![:space:]]*}"}"  # ltrim
  line="${line%"${line##*[![:space:]]}"}"  # rtrim
  [ -n "$line" ] || continue
  # A bare `*` would clear every name and neuter the whole control. Reject it
  # loudly rather than letting one character pass as an ordinary allowlist edit.
  [ "$line" != '*' ] || { echo "error: allowlist '$allowlist' has a bare '*' entry, which allows every name" >&2; exit 2; }
  patterns+=("$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]')")
  pattern_count=$((pattern_count + 1))
done < "$allowlist"

# Counted rather than `${#patterns[@]}`: bash 3.2 (still /bin/bash on macOS)
# treats an empty array as unset under `set -u`, so the guard would abort with
# `unbound variable` instead of this message. The `for` below is only reached
# with a non-empty array for the same reason.
if [ "$pattern_count" -eq 0 ]; then
  echo "error: allowlist '$allowlist' has no entries" >&2
  exit 2
fi

# Tracked files only when there is a work tree (build output and anything
# untracked is out of scope by construction); a plain recursive grep otherwise,
# which is the path the smoke test's fixture directory takes.
hits=''
scan_status=0
if git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  hits="$(git -C "$root" grep -oiInE -- "$PATTERN" \
    -- . ":(exclude)$allowlist_rel")" || scan_status=$?
else
  # `cd` + a `.` root rather than grepping "$root" and stripping the prefix
  # afterwards: the strip would be a `sed` expression built from a path this
  # script does not control.
  hits="$(cd "$root" && grep -rIoiEn --exclude-dir=.git -- "$PATTERN" .)" || scan_status=$?
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
# suffix stuck behind the period. A GitHub slug can end in none of these, so
# every pass can only narrow the name.
trim_trailing_punctuation() {
  local value="$1"
  while [ -n "$value" ]; do
    case "$value" in
      *.|*-|*_) value="${value%?}" ;;
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
  name="${match#*/}"

  lower="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
  lower="$(trim_trailing_punctuation "$lower")"
  lower="${lower%.git}"
  lower="$(trim_trailing_punctuation "$lower")"
  # A reference left naming nothing (`<org>/.`, `<org>/.git`) is not a finding.
  [ -n "$lower" ] || continue

  allowed=0
  for p in "${patterns[@]}"; do
    # shellcheck disable=SC2254  # $p is intentionally a glob, not a literal
    case "$lower" in
      $p) allowed=1; break ;;
    esac
  done

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

echo "OK: every ${ORG} repo literal in the tracked tree is on $allowlist_rel"
