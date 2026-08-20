#!/usr/bin/env bash

set -uo pipefail

REPO="${REPO:-}"
PR_NUMBER="${PR_NUMBER:-}"
RECORD="${RECORD:-}"
EVENT_NAME="${EVENT_NAME:-}"
EVENT_ACTION="${EVENT_ACTION:-}"
EVENT_LABEL="${EVENT_LABEL:-}"
COMMENT_BODY="${COMMENT_BODY:-}"
COMMENT_ID="${COMMENT_ID:-}"
COMMENT_URL="${COMMENT_URL:-}"
ACTOR="${ACTOR:-}"
ACTOR_ASSOCIATION="${ACTOR_ASSOCIATION:-}"
ALLOWED_ASSOCIATIONS="${ALLOWED_ASSOCIATIONS:-OWNER,MEMBER,COLLABORATOR}"
RUN_ID="${RUN_ID:-}"
NOW="${NOW:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
DRY_RUN="${DRY_RUN:-0}"

DISPUTE_LABEL="risk-dispute"
DISPUTE_PREFIX="${DISPUTE_LABEL}:"
DISPUTE_MARKER="ci-pr-risk-dispute:v1"

log() { printf '[risk-dispute] %s\n' "$*" >&2; }
die() { printf '[risk-dispute] ERROR %s\n' "$*" >&2; exit 2; }
fail() { printf '[risk-dispute] FAIL %s\n' "$*" >&2; exit 4; }

[ -n "$REPO" ] || die "REPO is required"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "bad REPO '$REPO'"
[[ "$PR_NUMBER" =~ ^[0-9]+$ ]] || die "bad PR_NUMBER '$PR_NUMBER'"
command -v jq >/dev/null 2>&1 || die "jq not found on PATH"
[ "$DRY_RUN" = 1 ] || command -v gh >/dev/null 2>&1 || die "gh not found on PATH"

trim() {
  sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' <<<"$1"
}

action=""
tier=""
reason=""
source=""
bootstrap=0
clear_scope="all"

case "$EVENT_NAME:$EVENT_ACTION" in
  issue_comment:created)
    [[ "$ALLOWED_ASSOCIATIONS" != *[[:space:]]* ]] \
      || die "ALLOWED_ASSOCIATIONS must be comma-separated with no spaces"
    case ",$ALLOWED_ASSOCIATIONS," in
      *",$ACTOR_ASSOCIATION,"*) ;;
      *) log "ignoring /risk-dispute from association '${ACTOR_ASSOCIATION:-unknown}'"; exit 0 ;;
    esac

    body="${COMMENT_BODY//$'\r'/}"
    first_line="${body%%$'\n'*}"
    remaining=""
    [[ "$body" != *$'\n'* ]] || remaining="${body#*$'\n'}"
    if [[ "$first_line" =~ ^/risk-dispute[[:space:]]+(R[0-3])([[:space:]]+(.*))?$ ]]; then
      action="set"
      tier="${BASH_REMATCH[1]}"
      reason="$(trim "${BASH_REMATCH[3]:-}")"
      if [ -n "$(trim "$remaining")" ]; then
        [ -z "$reason" ] || reason+=$'\n'
        reason+="$(trim "$remaining")"
      fi
    elif [[ "$first_line" =~ ^/risk-dispute[[:space:]]+clear[[:space:]]*$ ]]; then
      action="clear"
    elif [[ "$first_line" =~ ^/risk-dispute([[:space:]]+(.*))?$ ]]; then
      reason="$(trim "${BASH_REMATCH[2]:-}")"
      [[ "$reason" =~ ^R[0-9]+([[:space:]]|$) ]] \
        && die "bad tier; use R0, R1, R2 or R3"
      if [ -n "$(trim "$remaining")" ]; then
        [ -z "$reason" ] || reason+=$'\n'
        reason+="$(trim "$remaining")"
      fi
      action="set"
    else
      die "bad command; use '/risk-dispute [optional reason]', '/risk-dispute R0..R3 [optional reason]' or '/risk-dispute clear'"
    fi
    source="comment"
    ;;
  pull_request:labeled)
    if [[ "$EVENT_LABEL" =~ ^risk-dispute:(R[0-3])$ ]]; then
      tier="${BASH_REMATCH[1]}"
    elif [ "$EVENT_LABEL" != "$DISPUTE_LABEL" ]; then
      exit 0
    fi
    action="set"
    source="label"
    ;;
  pull_request:unlabeled)
    if [[ "$EVENT_LABEL" =~ ^risk-dispute:(R[0-3])$ ]]; then
      tier="${BASH_REMATCH[1]}"
      clear_scope="tiered"
    elif [ "$EVENT_LABEL" = "$DISPUTE_LABEL" ]; then
      clear_scope="legacy"
    else
      exit 0
    fi
    action="clear"
    source="label"
    ;;
  pull_request:opened|pull_request:reopened|pull_request:ready_for_review)
    action="bootstrap"
    source="workflow"
    ;;
  pull_request:synchronize)
    action="expire"
    source="push"
    bootstrap=1
    ;;
  *) exit 0 ;;
esac

ERRF="$(mktemp "${TMPDIR:-/tmp}/risk-dispute-err.XXXXXX")" || die "mktemp failed"
trap 'rm -f "$ERRF"' EXIT
ghq() { gh "$@" 2>"$ERRF"; }
gherr() { tr '\n' ' ' <"$ERRF" | sed 's/[[:space:]]*$//'; }
enc() { jq -rn --arg s "$1" '$s | @uri'; }
color_for() {
  case "$1" in
    R0) echo 0e8a16 ;; R1) echo fbca04 ;; R2) echo d93f0b ;; R3) echo b60205 ;;
    *) echo cfd3d7 ;;
  esac
}

description_for() {
  if [ "$1" = legacy ]; then
    echo "Human disagreement with PR risk grade; tier unspecified"
  else
    echo "Human PR risk assessment for grader calibration"
  fi
}

ensure_dispute_labels() {
  local labels candidate candidate_tier
  labels="$(ghq api --paginate "repos/$REPO/labels?per_page=100" --jq '[.[].name]' \
             | jq -sc 'add // []')" \
    || fail "could not read repository labels on $REPO: $(gherr)"
  for candidate_tier in legacy R0 R1 R2 R3; do
    if [ "$candidate_tier" = legacy ]; then
      candidate="$DISPUTE_LABEL"
    else
      candidate="${DISPUTE_PREFIX}${candidate_tier}"
    fi
    if jq -e --arg candidate "$candidate" \
      'map(ascii_downcase) | index($candidate | ascii_downcase) != null' \
      >/dev/null <<<"$labels"; then
      continue
    fi
    [ "$DRY_RUN" = 1 ] && { log "DRY RUN — would create '$candidate'"; continue; }
    ghq api -X POST "repos/$REPO/labels" -f name="$candidate" \
      -f color="$(color_for "$candidate_tier")" \
      -f description="$(description_for "$candidate_tier")" >/dev/null \
      || fail "could not create '$candidate': $(gherr)"
  done
}

if [ "$action" = bootstrap ] || [ "$bootstrap" = 1 ]; then
  ensure_dispute_labels
  [ "$action" != bootstrap ] || exit 0
fi

current="$(ghq api --paginate "repos/$REPO/issues/$PR_NUMBER/labels?per_page=100" --jq '[.[].name]' \
           | jq -sc 'add // []')" \
  || fail "could not read labels on $REPO#$PR_NUMBER: $(gherr)"

disputes="$(jq -c '[.[] | select(test("^risk-dispute:R[0-3]$"; "i"))]' <<<"$current")" \
  || fail "could not inspect dispute labels"
dispute_count="$(jq 'length' <<<"$disputes")" || fail "could not count dispute labels"
legacy_count="$(jq '[.[] | select(ascii_downcase == "risk-dispute")] | length' <<<"$current")" \
  || fail "could not inspect the legacy dispute label"

if [ "$action" = expire ] && [ "$dispute_count" -eq 0 ] && [ "$legacy_count" -eq 0 ]; then
  exit 0
fi

if [ "$action" = set ]; then
  if [ -n "$tier" ]; then
    target="${DISPUTE_PREFIX}${tier}"
    target_count="$(jq --arg target "$target" '[.[] | select(ascii_downcase == ($target | ascii_downcase))] | length' <<<"$disputes")" \
      || fail "could not inspect the target dispute label"
    needs_sync=0
    [ "$dispute_count" -eq 1 ] && [ "$target_count" -eq 1 ] && [ "$legacy_count" -eq 0 ] \
      || needs_sync=1
  else
    target="$DISPUTE_LABEL"
    needs_sync=0
    [ "$legacy_count" -eq 1 ] || needs_sync=1
  fi
  if [ "$needs_sync" = 1 ]; then
    if [ "$DRY_RUN" != 1 ]; then
      if ! probe_err="$(gh api "repos/$REPO/labels/$(enc "$target")" 2>&1 >/dev/null)"; then
        case "$probe_err" in
          *"HTTP 404"*|*"Not Found"*)
            ghq api -X POST "repos/$REPO/labels" -f name="$target" \
              -f color="$(color_for "$tier")" \
              -f description="$(description_for "${tier:-legacy}")" >/dev/null \
              || fail "could not create '$target': $(gherr)"
            ;;
          *) fail "could not inspect '$target': $(tr '\n' ' ' <<<"$probe_err")" ;;
        esac
      fi
    fi
    if [ -n "$tier" ]; then
      desired="$(jq -c --arg target "$target" \
        '[.[] | select(test("^risk-dispute(?::R[0-3])?$"; "i") | not)] + [$target]' <<<"$current")" \
        || fail "could not build the tiered dispute label set"
    else
      desired="$(jq -c --arg target "$target" '. + [$target]' <<<"$current")" \
        || fail "could not add the legacy dispute label"
    fi
  else
    desired="$current"
  fi
else
  case "$clear_scope" in
    legacy)
      desired="$(jq -c '[.[] | select(ascii_downcase != "risk-dispute")]' <<<"$current")" ;;
    tiered)
      desired="$(jq -c '[.[] | select(test("^risk-dispute:R[0-3]$"; "i") | not)]' <<<"$current")" ;;
    *)
      desired="$(jq -c '[.[] | select(test("^risk-dispute(?::R[0-3])?$"; "i") | not)]' <<<"$current")" ;;
  esac || fail "could not clear dispute labels"
fi

if [ "$desired" != "$current" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY RUN — would sync dispute labels to $(jq -c . <<<"$desired")"
  else
    jq -n --argjson labels "$desired" '{labels:$labels}' \
      | ghq api -X PUT "repos/$REPO/issues/$PR_NUMBER/labels" --input - >/dev/null \
      || fail "could not sync dispute labels on $REPO#$PR_NUMBER: $(gherr)"
  fi
fi

computed_tier=""
map_version=""
head_sha=""
if [ -n "$RECORD" ] && [ -s "$RECORD" ] && jq -e . "$RECORD" >/dev/null 2>&1; then
  computed_tier="$(jq -r '.risk.tier // ""' "$RECORD")"
  map_version="$(jq -r '.risk.map_version // ""' "$RECORD")"
  head_sha="$(jq -r '.head_sha // ""' "$RECORD")"
fi
[ -n "$computed_tier" ] || computed_tier="$(jq -r '[.[] | capture("^risk:(?<tier>R[0-3])$"; "i").tier | ascii_upcase][0] // ""' <<<"$current")"
if [ -z "$head_sha" ]; then
  head_sha="$(ghq api "repos/$REPO/pulls/$PR_NUMBER" --jq '.head.sha')" \
    || fail "could not read the PR head: $(gherr)"
fi

previous_tiers="$(jq -c '[.[] | capture("^risk-dispute:(?<tier>R[0-3])$"; "i").tier | ascii_upcase]' <<<"$disputes")" \
  || previous_tiers='[]'
if [ -n "$tier" ] && [ "$EVENT_NAME:$EVENT_ACTION" = pull_request:labeled ]; then
  previous_tiers="$(jq -c --arg tier "$tier" 'map(select(. != $tier))' <<<"$previous_tiers")" \
    || fail "could not record the previous dispute tiers"
elif [ -n "$tier" ] && [ "$EVENT_NAME:$EVENT_ACTION" = pull_request:unlabeled ]; then
  previous_tiers="$(jq -c --arg tier "$tier" 'if index($tier) then . else . + [$tier] end' <<<"$previous_tiers")" \
    || fail "could not record the previous dispute tiers"
fi
reason_json="null"
[ -z "$reason" ] || reason_json="$(jq -cn --arg reason "$reason" '$reason')"
human_json="null"
if [ "$action" = set ] && [ -n "$tier" ]; then
  human_json="$(jq -cn --arg tier "$tier" '$tier')"
fi

record="$(jq -cn \
  --arg action "$action" --arg repo "$REPO" --argjson pr "$PR_NUMBER" \
  --arg head_sha "$head_sha" --arg computed_tier "$computed_tier" \
  --argjson human_tier "$human_json" --argjson previous_tiers "$previous_tiers" \
  --argjson reason "$reason_json" --arg source "$source" --arg actor "$ACTOR" \
  --arg association "$ACTOR_ASSOCIATION" --arg comment_id "$COMMENT_ID" \
  --arg comment_url "$COMMENT_URL" --arg map_version "$map_version" \
  --arg run_id "$RUN_ID" --arg created_at "$NOW" \
  '{schema:1, action:$action, repo:$repo, pr:$pr, head_sha:$head_sha,
    computed_tier:(if $computed_tier == "" then null else $computed_tier end),
    human_tier:$human_tier, previous_tiers:$previous_tiers, reason:$reason,
    source:$source, actor:(if $actor == "" then null else $actor end),
    actor_association:(if $association == "" then null else $association end),
    source_comment_id:(if $comment_id == "" then null else $comment_id end),
    source_comment_url:(if $comment_url == "" then null else $comment_url end),
    map_version:(if $map_version == "" then null else $map_version end),
    run_id:(if $run_id == "" then null else $run_id end), created_at:$created_at}')" \
  || fail "could not build the audit record"

encoded="$(jq -rn --arg record "$record" '$record | @base64')" || fail "could not encode the audit record"
case "$action" in
  set)
    if [ -n "$tier" ]; then
      summary="Risk dispute recorded: grader \`${computed_tier:-unknown}\`, human \`${tier}\` on \`${head_sha:0:12}\`."
    else
      summary="Risk dispute recorded: grader \`${computed_tier:-unknown}\`, human tier unspecified on \`${head_sha:0:12}\`."
    fi
    ;;
  clear) summary="Risk dispute cleared on \`${head_sha:0:12}\`." ;;
  expire) summary="Risk dispute expired after a new push to \`${head_sha:0:12}\`." ;;
esac
if [ "$source" = comment ] && [ -n "$COMMENT_URL" ] && [ -n "$reason" ]; then
  summary+=" Reason: [command comment]($COMMENT_URL)."
elif [ "$action" = set ]; then
  summary+=" No reason supplied."
fi
body="<!-- ${DISPUTE_MARKER} ${encoded} -->
${summary}"

if [ "$DRY_RUN" = 1 ]; then
  printf '%s\n' "$body"
  exit 0
fi

jq -n --arg body "$body" '{body:$body}' \
  | ghq api -X POST "repos/$REPO/issues/$PR_NUMBER/comments" --input - >/dev/null \
  || fail "could not write the dispute audit record: $(gherr)"

log "$action recorded for $REPO#$PR_NUMBER"
