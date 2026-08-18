#!/usr/bin/env bash
# classify-and-apply.sh — the side-effecting orchestration behind pr-area-label.yml's
# label-pr job: fetch the trusted taxonomy, ask the model for exactly one label, and apply
# it with targeted `area:*` operations. All the pure logic (validation, request-building,
# reply-parsing) lives in lib.sh, sourced below and unit-tested hermetically; this file is
# the network layer around it.
#
# Env:
#   GH_REPO            owner/name (github.repository)
#   GH_TOKEN           token with `pull-requests: write`
#   ANTHROPIC_API_KEY  classifier credential; EMPTY ⇒ skip (fail soft), never red the check
#   PR_NUMBER          the PR to classify
#   BASE_REF           sha to read the taxonomy from — the PR's BASE, never its head, so a
#                      PR cannot rewrite the rules that classify it
#   TAXONOMY_PATH      path of the taxonomy in the repo (default .github/area-labels.yml)
#   MODEL              classifier model (default claude-opus-4-8)
#   DRY_RUN            "true" ⇒ log the decision, apply nothing
#
# Requires yq, jq, gh, curl. `set -euo pipefail`, but every EXPECTED miss (no key, taxonomy
# absent on the base ref, malformed taxonomy, API failure, no valid area) is an explicit
# `exit 0` — the classifier is advisory and must not fail the PR check.

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$SELF_DIR/lib.sh"

: "${GH_REPO:?GH_REPO is required}"
: "${PR_NUMBER:?PR_NUMBER is required}"
: "${BASE_REF:?BASE_REF is required}"
TAXONOMY_PATH="${TAXONOMY_PATH:-.github/area-labels.yml}"
MODEL="${MODEL:-claude-opus-4-8}"
DRY_RUN="${DRY_RUN:-false}"

# Fail soft when the API key isn't configured yet — don't red the check.
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "::warning::ANTHROPIC_API_KEY not set; skipping area classification"
  exit 0
fi

# 1. Trusted taxonomy from the BASE ref. If it doesn't exist there yet (e.g. the PR that
#    first introduces the file), skip cleanly. Fetch raw bytes; a non-2xx exits non-zero.
if ! gh api "repos/${GH_REPO}/contents/${TAXONOMY_PATH}?ref=${BASE_REF}" \
       -H "Accept: application/vnd.github.raw+json" > taxonomy.yml 2>/dev/null \
   || ! yq -e '.labels | length' taxonomy.yml >/dev/null 2>&1; then
  echo "::warning::${TAXONOMY_PATH} not readable on base ref ${BASE_REF}; skipping (expected until it lands on the base branch)"
  exit 0
fi

NAMES=$(taxonomy_names taxonomy.yml)
VOCAB=$(taxonomy_vocab taxonomy.yml)

if ! validate_names "$NAMES"; then
  echo "::warning::taxonomy on base ref ${BASE_REF} is malformed (names must be unique area:[a-z0-9-]+); skipping"
  exit 0
fi
if ! validate_vocab "$VOCAB"; then
  echo "::warning::taxonomy on base ref ${BASE_REF} has a label missing guidance/description; skipping"
  exit 0
fi

# 2. PR context as DATA (title/body/paths/labels). Body truncated; the diff is deliberately
#    excluded (large, untrusted, unnecessary here).
gh pr view "$PR_NUMBER" --repo "$GH_REPO" \
  --json title,body,files,labels \
  -q '{title: .title, body: (.body // "" | .[0:4000]), files: [.files[].path], labels: [.labels[].name]}' \
  > pr.json

# 3. Ask the model for exactly one label. No tools, no token; output enum-constrained to the
#    vocabulary, so an injection in the PR text cannot produce anything but a valid label.
SYSTEM=$(build_system "$(repo_context taxonomy.yml)" "$VOCAB")
build_request "$MODEL" "$SYSTEM" "$NAMES" pr.json > req.json

HTTP=$(curl -sS -o resp.json -w '%{http_code}' \
  --connect-timeout 15 --max-time 120 --retry 2 --retry-connrefused \
  https://api.anthropic.com/v1/messages \
  -H "x-api-key: ${ANTHROPIC_API_KEY}" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  --data @req.json || echo "000")
if [ "$HTTP" != "200" ]; then
  # resp.json is untrusted API output; escape with %q and cap its size so a forged newline /
  # ::...:: sequence can't inject workflow commands.
  printf '::warning::classifier API call failed (HTTP %s): %q\n' "$HTTP" "$(head -c 2048 resp.json)"
  exit 0
fi

AREA=$(extract_area resp.json)
REASON=$(extract_reason resp.json)
if [ -z "$AREA" ] || ! is_known_area "$AREA" "$NAMES"; then
  echo "::warning::no valid area returned (got: '${AREA:-}')"
  exit 0
fi

# REASON is free-text model output derived from untrusted PR data; escape with %q so a forged
# newline can't inject ::warning::-style commands. AREA is enum-constrained, so it's safe raw.
if [ "$DRY_RUN" = "true" ]; then
  printf '[dry-run] would set %s — %q\n' "$AREA" "$REASON"
  exit 0
fi

# 4. Apply with TARGETED area:* operations only — never a full-set PUT, which would replace
#    the PR's whole label set from a stale read and drop concurrent non-area edits (the
#    workflow's concurrency group serializes only itself, not GitHub UI edits or other label
#    writers). Add the selected label FIRST (additive POST) so a failure here or in any later
#    delete can never leave the PR without an area label.
CURRENT_AREA=$(gh pr view "$PR_NUMBER" --repo "$GH_REPO" --json labels \
  -q '[.labels[].name | select(startswith("area:"))]')
if echo "$CURRENT_AREA" | jq -e --arg a "$AREA" '. == [$a]' >/dev/null; then
  echo "already labeled $AREA — nothing to do"
  exit 0
fi

jq -n --arg a "$AREA" '{labels: [$a]}' \
  | gh api --method POST "repos/${GH_REPO}/issues/${PR_NUMBER}/labels" --input - >/dev/null

# Then remove every OTHER area:* label. Re-read a FRESH snapshot after the POST so a label
# added concurrently between the no-op check and now is also cleaned up. URL-encode the name
# for the path. Tolerate only 404 (already gone); surface any other error rather than
# silently leaving a stale second area label.
gh pr view "$PR_NUMBER" --repo "$GH_REPO" --json labels \
  -q '[.labels[].name | select(startswith("area:"))]' \
  | jq -r --arg a "$AREA" '.[] | select(. != $a)' \
  | while IFS= read -r stale; do
      [ -n "$stale" ] || continue
      enc=$(jq -rn --arg s "$stale" '$s | @uri')
      if ! err=$(gh api --method DELETE "repos/${GH_REPO}/issues/${PR_NUMBER}/labels/${enc}" 2>&1); then
        if printf '%s' "$err" | grep -q "HTTP 404"; then
          printf 'stale label %q already gone\n' "$stale"
        else
          printf '::error::failed to remove stale label %q: %q\n' "$stale" "$err"
          exit 1
        fi
      fi
    done
printf 'set %s — %q\n' "$AREA" "$REASON"
