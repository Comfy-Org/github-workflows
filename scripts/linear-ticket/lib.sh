#!/usr/bin/env bash
# lib.sh — the pure, network-free core of the linear-ticket gate: pull candidate
# identifiers out of untrusted PR text, validate the team-key policy input, apply the
# team/state policy to Linear's attachment response, classify a Linear error as retryable
# or terminal, pick a failure category, and render the copy that explains it. Everything
# here is a function with no side effect beyond stdout, so tests/test_lib.sh can source
# this file and exercise the whole decision path against fixtures without a single API
# call. The side-effecting orchestration (resolve the PR, query Linear, publish the commit
# status, upsert the comment) lives in validate.sh, which sources this.
#
# The invariant this file underwrites (design §3): the ONLY thing that turns the check
# green is an attachment Linear returns for THIS PR's canonical html_url whose issue
# satisfies policy — filter_issues below. Candidate identifiers extracted from branch,
# title, and body (extract_candidates) are diagnostics ONLY; they explain why a check is
# red, they can never make it green. That is why the extractor is deliberately generic and
# the policy filter reads the resolved issue's real API `team.key` and `state.type`, never
# a prefix the author typed.
#
# Requires jq. No yq (there is no YAML to parse here) and no network.

# The hidden marker on the single PR comment this gate maintains. validate.sh finds its
# existing comment by this exact string, so it must never change casually.
# shellcheck disable=SC2034  # consumed by validate.sh, which sources this file
LINEAR_TICKET_MARKER='<!-- linear-ticket-check -->'

# The stable commit-status context branch protection requires. Publishing is done in
# validate.sh; the name lives here so the tests can pin it.
# shellcheck disable=SC2034  # consumed by validate.sh, which sources this file
LINEAR_TICKET_CONTEXT='linear-ticket'

# extract_candidates — read untrusted text on stdin, print up to 20 unique UPPERCASE
# candidate identifiers (one per line, first-seen order). The pattern is the design's
# case-insensitive [A-Z][A-Z0-9]*-\d+. These are diagnostics only: they let a failure
# comment say "you referenced BE-1234 but Linear has not linked it", never a pass. The
# 20-cap means author-controlled text cannot fan out into an unbounded diagnostic query.
extract_candidates() {
  # grep exits 1 on no match; that is not an error here, so swallow it. tr upper-cases
  # (so `be-1` and `BE-1` dedupe together), awk keeps first-seen order, head enforces the
  # cap. The leading letter class is spelled out rather than relying on grep -i so the
  # intent is explicit and locale-independent.
  { grep -oE '[A-Za-z][A-Za-z0-9]*-[0-9]+' || true; } \
    | tr '[:lower:]' '[:upper:]' \
    | awk '!seen[$0]++' \
    | head -n 20
}

# normalize_team_keys <raw> — parse the caller's comma-separated `team-keys` input into a
# normalized, uppercase, de-duplicated comma string on stdout. rc 0 with EMPTY output for
# an empty/whitespace-only input (the "accept any visible team" default). rc 1 — rejecting
# the whole run as a caller misconfiguration — if any non-empty entry is malformed or a
# duplicate (design §5.1). Fail closed here rather than silently dropping a key, because a
# dropped key would quietly widen the policy the caller asked to narrow.
normalize_team_keys() {
  local raw="$1"
  local collapsed
  collapsed="$(printf '%s' "$raw" | tr -d '[:space:]')"
  [ -z "$collapsed" ] && { printf ''; return 0; }
  local seen="" part
  local IFS=','
  for part in $raw; do
    part="$(printf '%s' "$part" | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]')"
    [ -z "$part" ] && continue          # tolerate a stray "BE,,ENG" empty field
    printf '%s' "$part" | grep -qE '^[A-Z][A-Z0-9]*$' || return 1
    case ",$seen," in *,"$part",*) return 1 ;; esac   # duplicate -> reject
    seen="${seen:+$seen,}$part"
  done
  printf '%s' "$seen"
}

# filter_issues <nodes-json> <team-keys-csv> <require-open> — THE GATE. Given the
# attachmentsForURL `.nodes` array (each node carries `.issue{identifier,team{key},
# state{type}}`), print the identifiers of issues that satisfy policy, one per line, sorted
# unique. Empty output == nothing passes. Policy:
#   * team    — keep an issue iff team-keys is empty OR its API-returned team.key is in the
#               allow-list. The list is matched against the RESOLVED key, never a prefix.
#   * state   — when require-open == "true", reject state.type completed/canceled; backlog,
#               unstarted, started, and triage pass. A missing state.type does not block.
# "Any issue remains" is the rule: a PR linked to several tickets passes when at least one
# linked issue satisfies policy (design §5).
filter_issues() {
  local nodes="$1" keys="$2" require_open="$3"
  local keys_json='[]'
  [ -n "$keys" ] && keys_json="$(printf '%s' "$keys" | jq -R 'split(",")')"
  printf '%s' "$nodes" | jq -r \
    --argjson keys "$keys_json" \
    --arg require_open "$require_open" '
    [ (.[]? | .issue) | select(. != null)
      | select( ($keys | length) == 0 or ((.team.key // "") as $k | $keys | index($k)) )
      | select( $require_open != "true"
                or ((.state.type // "") | (. != "completed" and . != "canceled")) )
      | .identifier ]
    | unique[]'
}

# count_linked <nodes-json> — how many attachments carry a non-null issue, i.e. how many
# real Linear issues are linked to this PR (before policy). Distinguishes "nothing is
# linked yet" (retry / not-linked copy) from "something is linked but fails policy".
count_linked() {
  printf '%s' "$1" | jq '[ (.[]? | .issue) | select(. != null) ] | length'
}

# classify_linear_error <http-status> <graphql-error-codes> — "retryable" or "terminal".
# Linear signals rate limiting as HTTP 400 with GraphQL error code RATELIMITED (design
# §8), so the code string is checked first. 408/429/5xx are transient transport/server
# faults. Everything else — auth, schema, malformed — is terminal and fails closed as an
# infrastructure error, never as an invalid ticket.
classify_linear_error() {
  local http="$1" codes="$2"
  case "$codes" in *RATELIMITED*) echo retryable; return 0 ;; esac
  case "$http" in
    408 | 429 | 500 | 502 | 503 | 504) echo retryable; return 0 ;;
  esac
  echo terminal
}

# select_failure_category <infra-error> <linked-count> <referenced-count> — pick the one
# category that explains a red check, in priority order (design §5 step 6):
#   infra_error       — Linear could not be queried (auth/schema/exhausted retries).
#   policy_mismatch    — issue(s) ARE linked to this PR but every one fails team/state policy.
#   exists_not_linked  — no link, but an identifier was REFERENCED in branch/title/body.
#   no_candidate       — no link and no identifier referenced anywhere.
# The boundary between the last two is whether an identifier was referenced at all; the
# batched diagnostic lookup (count_resolved_candidates) only enriches the DETAIL line ("…and
# at least one resolves to a real Linear issue"), it does not move the category — so the copy
# can never say "no identifier detected" while the detail lists one.
select_failure_category() {
  local infra="$1" linked="$2" referenced="$3"
  if [ "$infra" = "true" ]; then echo infra_error; return 0; fi
  if [ "$linked" -gt 0 ]; then echo policy_mismatch; return 0; fi
  if [ "$referenced" -gt 0 ]; then echo exists_not_linked; return 0; fi
  echo no_candidate
}

# failure_guidance <category> — the fixed, category-specific paragraph shown in the PR
# comment and job summary. Specifics (identifiers, candidate list, rerun link) are appended
# by validate.sh; this keeps the reusable copy testable and consistent across repos.
failure_guidance() {
  case "$1" in
    no_candidate)
      cat <<'EOF'
No linked Linear issue was found for this PR, and no issue identifier was detected in the
branch name, title, or body. Link a Linear issue by any supported method — put its
identifier (e.g. `BE-1234`) in the branch name, PR title, or body (`Closes BE-1234`), or
paste this PR's URL into the issue in Linear.
EOF
      ;;
    exists_not_linked)
      cat <<'EOF'
An issue identifier was referenced, but Linear has not linked that issue to this PR yet. A
referenced identifier in text is not a link. Either use a supported auto-link (identifier
in the branch name, title, or a `Closes`/`Fixes`/`Resolves` line in the body) or paste this
PR's canonical URL into the issue in Linear. Automatic links are created a few seconds
after the PR event, so a brand-new link may just need a re-run.
EOF
      ;;
    policy_mismatch)
      cat <<'EOF'
A Linear issue is linked to this PR, but no linked issue satisfies this repository's policy
— every linked issue is either in a completed/canceled state or belongs to a team this
check does not accept. Link an issue from an accepted team that is not closed, or move the
existing issue back to an open state.
EOF
      ;;
    infra_error)
      cat <<'EOF'
This check could not be completed because Linear could not be queried (authentication,
schema, timeout, or rate-limit exhaustion). This is an infrastructure error, not a verdict
on your ticket — it fails closed on purpose so a broken credential cannot silently disable
the control. Re-run the check; if it keeps failing, contact the repository owners.
EOF
      ;;
    *)
      echo "Unknown failure category: $1" >&2
      return 1
      ;;
  esac
}

# build_diagnostic_query — read newline candidate identifiers on stdin, emit ONE aliased
# GraphQL query (`c0: issueSearch(...) c1: ...`) that resolves them all in a single request
# (design §8: at most one batched diagnostic query). rc 1 if there are no candidates.
# Candidates from extract_candidates are strictly [A-Z0-9-]+, so embedding them in a
# double-quoted GraphQL string literal is safe; the tr/grep below re-assert that invariant
# so a future caller cannot smuggle a quote through.
build_diagnostic_query() {
  local aliases="" id i=0
  while IFS= read -r id; do
    [ -z "$id" ] && continue
    printf '%s' "$id" | grep -qE '^[A-Z0-9-]+$' || continue
    aliases="${aliases}  c${i}: issueSearch(query: \"${id}\", first: 1) { nodes { identifier } }"$'\n'
    i=$((i + 1))
  done
  [ "$i" -eq 0 ] && return 1
  printf 'query {\n%s}\n' "$aliases"
}

# count_resolved_candidates <diagnostic-response-json> — how many aliased issueSearch
# results returned at least one node. Best-effort: a malformed/absent `.data` counts as 0,
# so a diagnostic hiccup degrades to the "no_candidate" copy rather than erroring.
count_resolved_candidates() {
  printf '%s' "$1" | jq '
    (.data // {}) | [ to_entries[] | select(.value.nodes | (. // []) | length > 0) ] | length
  ' 2>/dev/null || echo 0
}
