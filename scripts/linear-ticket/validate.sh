#!/usr/bin/env bash
# validate.sh — the side-effecting orchestration behind linear-ticket.yml. Sources lib.sh
# for every decision and owns only I/O: resolve the PR from the workflow_run event, refetch
# it, publish the `linear-ticket` commit status, query Linear's attachmentsForURL for the
# PR's canonical html_url, apply the policy gate, run one batched diagnostic query, and
# maintain exactly one marker PR comment.
#
# It runs in the PRIVILEGED workflow_run job, so every value derived from the PR (branch,
# title, body, labels, URL) is untrusted DATA: it is passed to jq/curl through argument
# arrays and GraphQL variables, never interpolated into a query or a shell word. No PR code
# is checked out or executed.
#
# Contract (all via env, set by linear-ticket.yml):
#   GH_TOKEN            caller GITHUB_TOKEN, for `gh api` (statuses:write, pull-requests:write)
#   GH_REPO            owner/repo (github.repository)
#   LINEAR_API_TOKEN   value placed verbatim into Linear's Authorization header
#   GITHUB_EVENT_PATH  workflow_run event payload
#   TEAM_KEYS          raw `team-keys` input (comma-separated; empty = any team)
#   EXEMPT_LABEL       exemption label name; empty disables exemption
#   REQUIRE_OPEN_ISSUE "true"/"false"
#   ENFORCE            "true" (fail closed) / "false" (warn-only: always green, same diagnosis)
#   RUN_URL            html_url of this workflow run, for the status target and comment
#   LINEAR_API_URL     optional override (default https://api.linear.app/graphql)
#   GITHUB_STEP_SUMMARY  written with the human-readable outcome
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
. "$SELF_DIR/lib.sh"

LINEAR_API_URL="${LINEAR_API_URL:-https://api.linear.app/graphql}"
SUMMARY_FILE="${GITHUB_STEP_SUMMARY:-/dev/null}"

# ── logging ──────────────────────────────────────────────────────────────────
log()  { printf '%s\n' "$*" >&2; }
err()  { printf '::error::%s\n' "$*" >&2; }
warn() { printf '::warning::%s\n' "$*" >&2; }
summary() { printf '%s\n' "$*" >>"$SUMMARY_FILE"; }

# ── GraphQL queries (static; the URL is the only variable) ────────────────────
# shellcheck disable=SC2016  # $url is a GraphQL variable, not a shell expansion
ATTACHMENTS_QUERY='query PullRequestAttachments($url: String!) {
  attachmentsForURL(url: $url, first: 20) {
    nodes { id url issue { id identifier team { key } state { type } } }
  }
}'

# linear_post <body-file> — POST a JSON GraphQL request. Writes the response body to
# stdout and sets globals LINEAR_HTTP (status) and LINEAR_ERR_CODES (space-joined GraphQL
# error `extensions.code`s). rc 0 on any completed HTTP exchange (even a 4xx/5xx — the
# caller classifies it), rc 1 only on a transport failure (curl could not complete).
LINEAR_HTTP=""; LINEAR_ERR_CODES=""
linear_post() {
  local body_file="$1"
  local hdr_file; hdr_file="$(mktemp)"
  local out
  # --Authorization verbatim: a personal API key goes raw, an OAuth token as "Bearer ...".
  # The header value is set once here; docs tell the caller which form to store.
  out="$(curl -sS --max-time 30 -D "$hdr_file" -X POST "$LINEAR_API_URL" \
    -H "Authorization: ${LINEAR_API_TOKEN}" \
    -H 'Content-Type: application/json' \
    --data-binary "@${body_file}" 2>/dev/null)"
  local curl_rc=$?
  if [ "$curl_rc" -ne 0 ]; then
    rm -f "$hdr_file"
    LINEAR_HTTP=""; LINEAR_ERR_CODES=""
    return 1
  fi
  LINEAR_HTTP="$(awk 'toupper($1) ~ /^HTTP/ {code=$2} END {print code}' "$hdr_file")"
  # Log rate-limit headroom for the pilot (design §8), best-effort.
  grep -iE '^x-ratelimit-(requests|complexity)-(remaining|reset):' "$hdr_file" >&2 || true
  rm -f "$hdr_file"
  LINEAR_ERR_CODES="$(printf '%s' "$out" | jq -r '[.errors[]?.extensions.code // empty] | join(" ")' 2>/dev/null || true)"
  printf '%s' "$out"
}

# build_graphql_body <query> [<url>] — a GraphQL request body with the PR URL passed as a
# VARIABLE, never interpolated into the query text (design §4).
build_graphql_body() {
  local query="$1"
  if [ "$#" -ge 2 ]; then
    jq -n --arg q "$query" --arg url "$2" '{query: $q, variables: {url: $url}}'
  else
    jq -n --arg q "$query" '{query: $q}'
  fi
}

# ── commit status ─────────────────────────────────────────────────────────────
# publish_status <sha> <state> <description>
publish_status() {
  local sha="$1" state="$2" description="$3"
  gh api -X POST "repos/${GH_REPO}/statuses/${sha}" \
    -f "state=${state}" \
    -f "context=${LINEAR_TICKET_CONTEXT}" \
    -f "description=${description}" \
    -f "target_url=${RUN_URL:-}" >/dev/null 2>&1 \
    || warn "Failed to publish '${state}' status on ${sha}"
}

# ── marker comment ─────────────────────────────────────────────────────────────
# find_marker_comment <pr-number> — echo the id of the existing marker comment, or empty.
find_marker_comment() {
  gh api --paginate "repos/${GH_REPO}/issues/${1}/comments" \
    --jq ".[] | select(.body | contains(\"${LINEAR_TICKET_MARKER}\")) | .id" 2>/dev/null \
    | head -n1
}

# upsert_marker_comment <pr-number> <body-file> — create or update the single marker
# comment. Comment I/O is best-effort: a failure is warned, never fatal (design §6).
upsert_marker_comment() {
  local pr="$1" body_file="$2" existing
  existing="$(find_marker_comment "$pr")"
  if [ -n "$existing" ]; then
    gh api -X PATCH "repos/${GH_REPO}/issues/comments/${existing}" \
      -F "body=@${body_file}" >/dev/null 2>&1 || warn "Failed to update marker comment ${existing}"
  else
    gh api -X POST "repos/${GH_REPO}/issues/${pr}/comments" \
      -F "body=@${body_file}" >/dev/null 2>&1 || warn "Failed to create marker comment"
  fi
}

# delete_marker_comment <pr-number> — remove the marker comment after a pass/exempt run.
delete_marker_comment() {
  local pr="$1" existing
  existing="$(find_marker_comment "$pr")"
  [ -z "$existing" ] && return 0
  gh api -X DELETE "repos/${GH_REPO}/issues/comments/${existing}" >/dev/null 2>&1 \
    || warn "Failed to delete marker comment ${existing}"
}

# current_head_sha <pr-number> — the PR's head SHA right now, for the supersession recheck.
current_head_sha() {
  gh api "repos/${GH_REPO}/pulls/${1}" --jq '.head.sha' 2>/dev/null
}

# ── terminal outcomes ──────────────────────────────────────────────────────────
# In warn-only mode every outcome publishes SUCCESS but the summary/comment still show the
# verdict enforce mode would have produced (design §7). Before any terminal status write we
# refetch the PR head SHA and bail if it moved — a newer run must win (design §6).
VALIDATED_SHA=""; PR_NUMBER=""

finish_pass() {
  local identifiers="$1"
  summary "## linear-ticket: ✅ pass"
  summary ""
  summary "Linear has linked this PR to: **${identifiers}**"
  delete_marker_comment "$PR_NUMBER"
  guard_supersession || return 0
  publish_status "$VALIDATED_SHA" success "Linked Linear issue: ${identifiers}"
  exit 0
}

finish_exempt() {
  summary "## linear-ticket: ✅ exempt"
  summary ""
  summary "PR carries the \`${EXEMPT_LABEL}\` label — the Linear-ticket requirement is waived."
  delete_marker_comment "$PR_NUMBER"
  guard_supersession || return 0
  publish_status "$VALIDATED_SHA" success "Exempt via ${EXEMPT_LABEL} label"
  exit 0
}

# finish_fail <category> <detail-markdown>
finish_fail() {
  local category="$1" detail="$2"
  local guidance; guidance="$(failure_guidance "$category")"
  local verdict state short
  if [ "$ENFORCE" = "false" ]; then
    verdict="⚠️ warn-only (would fail: ${category})"; state="success"; short="warn-only: would fail (${category})"
  else
    verdict="❌ fail (${category})"; state="failure"; short="No linked Linear issue (${category})"
  fi

  # One marker comment carrying the same diagnosis in both modes.
  local body_file; body_file="$(mktemp)"
  {
    printf '%s\n\n' "$LINEAR_TICKET_MARKER"
    printf '### Linear ticket check — %s\n\n' "$verdict"
    printf '%s\n' "$guidance"
    [ -n "$detail" ] && printf '\n%s\n' "$detail"
    printf '\n---\n'
    printf 'After linking an issue, re-run this check from the [workflow run](%s) or edit the PR title/body to trigger a fresh run. ' "${RUN_URL:-}"
    # shellcheck disable=SC2016  # backticks are literal markdown for the label name, not a subshell
    printf 'A repository maintainer can waive the requirement by applying the `%s` label.\n' "${EXEMPT_LABEL:-linear-exempt}"
  } >"$body_file"
  upsert_marker_comment "$PR_NUMBER" "$body_file"
  rm -f "$body_file"

  summary "## linear-ticket: ${verdict}"
  summary ""
  summary "$guidance"
  [ -n "$detail" ] && summary "$detail"

  guard_supersession || return 0
  publish_status "$VALIDATED_SHA" "$state" "$short"
  [ "$ENFORCE" = "false" ] && exit 0
  exit 1
}

# guard_supersession — rc 0 to proceed with the terminal write, rc 1 to bail because the PR
# head advanced past the SHA we validated (a newer run owns the result now).
guard_supersession() {
  local now; now="$(current_head_sha "$PR_NUMBER")"
  if [ -n "$now" ] && [ "$now" != "$VALIDATED_SHA" ]; then
    log "PR head moved ${VALIDATED_SHA} -> ${now}; a newer run supersedes this one. Not writing a terminal status."
    return 1
  fi
  return 0
}

# ── main ───────────────────────────────────────────────────────────────────────
main() {
  for tool in gh jq curl; do
    command -v "$tool" >/dev/null 2>&1 || { err "$tool not found on PATH"; exit 1; }
  done
  [ -n "${GH_REPO:-}" ]           || { err "GH_REPO is required"; exit 1; }
  [ -n "${GITHUB_EVENT_PATH:-}" ] || { err "GITHUB_EVENT_PATH is required"; exit 1; }
  [ -f "$GITHUB_EVENT_PATH" ]     || { err "event payload not found at GITHUB_EVENT_PATH"; exit 1; }
  [ -n "${LINEAR_API_TOKEN:-}" ]  || { err "LINEAR_API_TOKEN secret is not set; failing closed (infrastructure error)"; exit 1; }

  # team-keys is a caller-config input: a malformed value is a misconfiguration, so fail the
  # run loudly rather than silently widening policy (design §5.1).
  local team_keys
  if ! team_keys="$(normalize_team_keys "${TEAM_KEYS:-}")"; then
    err "Invalid team-keys input '${TEAM_KEYS:-}': entries must be uppercase alphanumeric team keys, unique, comma-separated."
    exit 1
  fi

  # Only a completed run of the signal workflow should reach here; the caller's `if:` gates
  # this, but re-assert it so a mis-wired caller fails safe rather than validating garbage.
  local event="$GITHUB_EVENT_PATH"
  local wr_event; wr_event="$(jq -r '.workflow_run.event // ""' "$event")"
  local head_sha; head_sha="$(jq -r '.workflow_run.head_sha // ""' "$event")"
  [ -n "$head_sha" ] || { err "workflow_run.head_sha missing from event"; exit 1; }

  # Resolve exactly one open PR. Same-repo runs carry workflow_run.pull_requests; fork runs
  # do not, so fall back to the commit->PR association (GitHub-owned data either way). We
  # require exactly one OPEN PR, refetched through the API before any status write.
  local -a candidates=()
  local n
  while IFS= read -r n; do [ -n "$n" ] && candidates+=("$n"); done < <(
    jq -r '.workflow_run.pull_requests[]?.number // empty' "$event"
  )
  if [ "${#candidates[@]}" -eq 0 ]; then
    while IFS= read -r n; do [ -n "$n" ] && candidates+=("$n"); done < <(
      gh api "repos/${GH_REPO}/commits/${head_sha}/pulls" \
        --jq ".[] | select(.state==\"open\") | select(.base.repo.full_name==\"${GH_REPO}\") | .number" 2>/dev/null
    )
  fi

  # De-dup + keep only currently-open PRs.
  local -a open_prs=()
  local seen_pr=""
  for n in "${candidates[@]}"; do
    case " $seen_pr " in *" $n "*) continue ;; esac
    seen_pr="$seen_pr $n"
    local st; st="$(gh api "repos/${GH_REPO}/pulls/${n}" --jq '.state' 2>/dev/null || true)"
    [ "$st" = "open" ] && open_prs+=("$n")
  done

  if [ "${#open_prs[@]}" -ne 1 ]; then
    err "Expected exactly one open PR associated with ${head_sha}, found ${#open_prs[@]} (event=${wr_event}). Refusing to publish an ambiguous result."
    exit 1
  fi
  PR_NUMBER="${open_prs[0]}"

  # Refetch the PR: canonical html_url, current head SHA (the status target), labels, and the
  # untrusted text we mine for diagnostic candidates.
  local pr_json; pr_json="$(gh api "repos/${GH_REPO}/pulls/${PR_NUMBER}" 2>/dev/null)" \
    || { err "Could not fetch PR #${PR_NUMBER}"; exit 1; }
  local html_url; html_url="$(printf '%s' "$pr_json" | jq -r '.html_url')"
  VALIDATED_SHA="$(printf '%s' "$pr_json" | jq -r '.head.sha')"
  local pr_branch; pr_branch="$(printf '%s' "$pr_json" | jq -r '.head.ref // ""')"
  local pr_title;  pr_title="$(printf '%s' "$pr_json" | jq -r '.title // ""')"
  local pr_body;   pr_body="$(printf '%s' "$pr_json" | jq -r '.body // ""')"

  log "Validating PR #${PR_NUMBER} (${html_url}) at head ${VALIDATED_SHA}"
  publish_status "$VALIDATED_SHA" pending "Checking for a linked Linear issue…"

  # Exemption short-circuit.
  if [ -n "${EXEMPT_LABEL:-}" ]; then
    if printf '%s' "$pr_json" | jq -e --arg l "$EXEMPT_LABEL" '.labels[]?.name == $l | select(.)' >/dev/null 2>&1; then
      log "PR carries the '${EXEMPT_LABEL}' label — exempt."
      finish_exempt
    fi
  fi

  # ── the gate: attachmentsForURL(this PR), with bounded retry for the async-link race ──
  local body_file; body_file="$(mktemp)"
  build_graphql_body "$ATTACHMENTS_QUERY" "$html_url" >"$body_file"

  local -a backoff=(2 4 8 16)      # between five attempts
  local infra_error=false
  local nodes='[]'
  local attempt
  for attempt in 0 1 2 3 4; do
    local resp; resp="$(linear_post "$body_file")"; local post_rc=$?
    if [ "$post_rc" -ne 0 ]; then
      log "Linear request transport failure (attempt $((attempt+1)))"
    elif [ -n "$LINEAR_ERR_CODES" ] || { [ -n "$LINEAR_HTTP" ] && [ "$LINEAR_HTTP" -ge 400 ] 2>/dev/null; }; then
      local kind; kind="$(classify_linear_error "${LINEAR_HTTP:-0}" "${LINEAR_ERR_CODES:-}")"
      if [ "$kind" = "terminal" ]; then
        err "Linear returned a terminal error (HTTP ${LINEAR_HTTP:-?}, codes: ${LINEAR_ERR_CODES:-none}). Failing closed as an infrastructure error."
        infra_error=true
        break
      fi
      log "Linear returned a retryable error (HTTP ${LINEAR_HTTP:-?}, codes: ${LINEAR_ERR_CODES:-none}) on attempt $((attempt+1))"
    else
      # A clean response. Extract the attachment nodes.
      nodes="$(printf '%s' "$resp" | jq -c '.data.attachmentsForURL.nodes // []' 2>/dev/null || echo '[]')"
      local linked; linked="$(count_linked "$nodes")"
      if [ "$linked" -gt 0 ]; then
        break            # attachments present — evaluate policy, no reason to retry
      fi
      log "No attachment linked to this PR yet (attempt $((attempt+1)))"
    fi
    # Retry with backoff unless this was the last attempt.
    if [ "$attempt" -lt 4 ]; then
      sleep "${backoff[$attempt]}"
    fi
  done
  rm -f "$body_file"

  if [ "$infra_error" = false ]; then
    local passing; passing="$(filter_issues "$nodes" "$team_keys" "${REQUIRE_OPEN_ISSUE:-true}")"
    if [ -n "$passing" ]; then
      local joined; joined="$(printf '%s' "$passing" | paste -sd ',' - | sed 's/,/, /g')"
      log "PASS — linked issue(s): ${joined}"
      finish_pass "$joined"
    fi
  fi

  # ── failure path: diagnostics only (never turns red into green) ──
  local linked_count; linked_count="$(count_linked "$nodes")"
  local referenced_count=0 resolved_count=0
  local candidate_detail=""
  if [ "$infra_error" = false ] && [ "$linked_count" -eq 0 ]; then
    local candidates_list; candidates_list="$(printf '%s\n%s\n%s' "$pr_branch" "$pr_title" "$pr_body" | extract_candidates)"
    if [ -n "$candidates_list" ]; then
      referenced_count="$(printf '%s\n' "$candidates_list" | grep -c .)"
      local joined_candidates; joined_candidates="$(printf '%s' "$candidates_list" | paste -sd ',' - | sed 's/,/, /g')"
      local diag_query; if diag_query="$(printf '%s' "$candidates_list" | build_diagnostic_query)"; then
        local diag_file; diag_file="$(mktemp)"
        build_graphql_body "$diag_query" >"$diag_file"
        local diag_resp; diag_resp="$(linear_post "$diag_file")"
        rm -f "$diag_file"
        resolved_count="$(count_resolved_candidates "$diag_resp")"
      fi
      if [ "$resolved_count" -gt 0 ] 2>/dev/null; then
        candidate_detail="Referenced identifiers (not linked): ${joined_candidates} — at least one resolves to a real Linear issue; link it to this PR."
      else
        candidate_detail="Referenced identifiers (not linked): ${joined_candidates}"
      fi
    fi
  fi

  local category; category="$(select_failure_category "$infra_error" "$linked_count" "$referenced_count")"
  local detail="$candidate_detail"
  if [ "$category" = "policy_mismatch" ]; then
    local linked_ids; linked_ids="$(printf '%s' "$nodes" | jq -r '[ (.[]? | .issue) | select(.!=null) | .identifier ] | join(", ")')"
    detail="Linked but not accepted: ${linked_ids}"
  fi
  log "FAIL category=${category} linked=${linked_count} resolved=${resolved_count}"
  finish_fail "$category" "$detail"
}

main "$@"
