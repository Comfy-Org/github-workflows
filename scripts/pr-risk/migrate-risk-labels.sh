#!/usr/bin/env bash

set -uo pipefail

usage() {
  cat <<'EOF'
Usage: migrate-risk-labels.sh OWNER/REPO [--apply]

Migrates PR label assignments:
  risk:R0 -> risk:low
  risk:R1 -> risk:medium
  risk:R2 -> risk:high
  risk:R3 -> risk:xhigh

Dry-run is the default. Pass --apply to add, verify, then remove each legacy label.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

REPO=""
APPLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option '$1'" ;;
    *) [ -z "$REPO" ] || die "only one repository may be specified"
       REPO="$1" ;;
  esac
  shift
done

[ -n "$REPO" ] || die "OWNER/REPO is required"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "bad repository '$REPO'"
command -v gh >/dev/null 2>&1 || die "gh not found on PATH"
command -v jq >/dev/null 2>&1 || die "jq not found on PATH"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/risk-label-migration.XXXXXX")" || die "mktemp failed"
trap 'rm -rf "$TMP"' EXIT
ERRF="$TMP/gh.err"

ghq() { gh "$@" 2>"$ERRF"; }
gherr() { tr '\n' ' ' <"$ERRF" | sed 's/[[:space:]]*$//'; }
enc() { jq -rn --arg value "$1" '$value | @uri'; }

list_prs() {
  local source="$1"
  ghq api --paginate \
    "repos/$REPO/issues?state=all&labels=$(enc "$source")&per_page=100" \
    --jq '.[] | select(.pull_request != null) | .number' | sort -n -u
}

read_labels() {
  local pr="$1"
  ghq api --paginate "repos/$REPO/issues/$pr/labels?per_page=100" --jq '[.[].name]' \
    | jq -sc 'add // []'
}

has_label() {
  local labels="$1" label="$2"
  jq -e --arg label "$label" \
    'map(ascii_downcase) | index($label | ascii_downcase) != null' \
    >/dev/null <<<"$labels"
}

migrate_pr() {
  local pr="$1" source="$2" target="$3" labels final

  ghq api -X POST "repos/$REPO/issues/$pr/labels" -f "labels[]=$target" >/dev/null \
    || { printf 'FAIL #%s: could not add %s: %s\n' "$pr" "$target" "$(gherr)" >&2; return 1; }

  labels="$(read_labels "$pr")" \
    || { printf 'FAIL #%s: could not verify %s: %s\n' "$pr" "$target" "$(gherr)" >&2; return 1; }
  has_label "$labels" "$target" \
    || { printf 'FAIL #%s: %s was not present after add; kept %s\n' "$pr" "$target" "$source" >&2; return 1; }

  if has_label "$labels" "$source"; then
    ghq api -X DELETE "repos/$REPO/issues/$pr/labels/$(enc "$source")" >/dev/null \
      || { printf 'FAIL #%s: could not remove %s: %s\n' "$pr" "$source" "$(gherr)" >&2; return 1; }
  fi

  final="$(read_labels "$pr")" \
    || { printf 'FAIL #%s: could not verify final labels: %s\n' "$pr" "$(gherr)" >&2; return 1; }
  if ! has_label "$final" "$target" || has_label "$final" "$source"; then
    printf 'FAIL #%s: final labels did not preserve %s and remove %s\n' \
      "$pr" "$target" "$source" >&2
    return 1
  fi
}

MAPPINGS=(
  'risk:R0=risk:low'
  'risk:R1=risk:medium'
  'risk:R2=risk:high'
  'risk:R3=risk:xhigh'
)

planned=0
migrated=0
failures=0
residual=0

for mapping in "${MAPPINGS[@]}"; do
  source="${mapping%%=*}"
  target="${mapping#*=}"
  list="$TMP/$(enc "$source").before"
  list_prs "$source" >"$list" \
    || { printf 'FAIL: could not list %s assignments: %s\n' "$source" "$(gherr)" >&2; exit 1; }
  count="$(wc -l <"$list" | tr -d ' ')"
  planned=$((planned + count))
  printf '%s -> %s: %s PR(s)\n' "$source" "$target" "$count"

  [ "$APPLY" = 1 ] || continue
  while IFS= read -r pr; do
    [ -n "$pr" ] || continue
    if migrate_pr "$pr" "$source" "$target"; then
      migrated=$((migrated + 1))
      printf 'migrated #%s: %s -> %s\n' "$pr" "$source" "$target" >&2
    else
      failures=$((failures + 1))
    fi
  done <"$list"

  remaining="$TMP/$(enc "$source").after"
  list_prs "$source" >"$remaining" \
    || { printf 'FAIL: could not check residual %s assignments: %s\n' "$source" "$(gherr)" >&2; exit 1; }
  left="$(wc -l <"$remaining" | tr -d ' ')"
  residual=$((residual + left))
  [ "$left" -eq 0 ] || printf 'FAIL: %s still exists on %s PR(s): %s\n' \
    "$source" "$left" "$(paste -sd, "$remaining")" >&2
done

if [ "$APPLY" != 1 ]; then
  printf 'dry-run: %s assignment(s); rerun with --apply\n' "$planned"
  exit 0
fi

printf 'complete: migrated=%s failures=%s residual=%s\n' "$migrated" "$failures" "$residual"
[ "$failures" -eq 0 ] && [ "$residual" -eq 0 ]
