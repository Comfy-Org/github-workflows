#!/usr/bin/env bash
# classify-and-apply.sh — the side-effecting orchestration behind pr-area-label.yml's
# label-pr job: fetch the trusted taxonomy, ask the model for exactly one label, match the
# taxonomy's optional deterministic path sub-labels against the PR's changed files, and apply
# the result with targeted `area:*` operations. All the pure logic (validation, glob matching,
# request-building, reply-parsing) lives in lib.sh, sourced below and unit-tested
# hermetically; this file is the network layer around it.
#
# The label set a PR ends with is EXACTLY ONE classified area plus zero or more sub-labels
# whose `paths:` globs matched. Sub-labels never reach the model: they are a path question,
# so they are decided from the changed-file list the API already returns.
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
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
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

# Optional deterministic path sub-labels. Gated BEFORE anything is written for the same
# reason the two gates above are: these names are added to — and exempted from — the `area:*`
# cleanup below, so a malformed entry could both add a label outside the area namespace and
# shield one from removal. `[]` (the key absent) passes; a bad entry skips the whole run
# rather than silently labeling under half a taxonomy.
SUB_LABELS=$(taxonomy_sub_labels taxonomy.yml)
if ! validate_sub_labels "$SUB_LABELS" "$NAMES"; then
  echo "::warning::taxonomy on base ref ${BASE_REF} has malformed sub_labels (names must be unique area:[a-z0-9-]+, disjoint from labels[], each with at least one path); skipping"
  exit 0
fi

# 2. PR context as DATA (title/body/paths/labels). Body truncated; the diff is deliberately
#    excluded (large, untrusted, unnecessary here). A transient gh/API failure fails soft.
if ! gh pr view "$PR_NUMBER" --repo "$GH_REPO" \
     --json title,body,files,labels \
     -q '{title: .title, body: (.body // "" | .[0:4000]), files: [.files[].path], labels: [.labels[].name]}' \
     > pr.json 2>/dev/null; then
  echo "::warning::could not read PR #${PR_NUMBER} metadata; skipping classification"
  exit 0
fi

# 3. Match the sub-labels against the changed files. Deterministic, no model, no API call —
#    pr.json already carries the path list the classifier is shown.
MATCHED=$(sub_labels_json "$(matched_sub_labels "$SUB_LABELS" "$(jq -c '.files' pr.json)")")
if [ "$MATCHED" != "[]" ]; then
  echo "path sub-labels matched: $(printf '%s' "$MATCHED" | jq -r 'join(", ")')"
fi

# 4. Ask the model for exactly one label. No tools, no token; output enum-constrained to the
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
  printf '[dry-run] would set %s%s — %q\n' "$AREA" "$(desired_suffix "$MATCHED")" "$REASON"
  exit 0
fi

# 5. Apply with TARGETED area:* operations only — never a full-set PUT, which would replace
#    the PR's whole label set from a stale read and drop concurrent non-area edits (the
#    workflow's concurrency group serializes only itself, not GitHub UI edits or other label
#    writers). Add the selected label FIRST (additive POST) so a failure here or in any later
#    delete can never leave the PR without an area label.
if ! CURRENT_AREA=$(gh pr view "$PR_NUMBER" --repo "$GH_REPO" --json labels \
     -q '[.labels[].name | select(startswith("area:"))]' 2>/dev/null); then
  echo "::warning::could not read current labels on PR #${PR_NUMBER}; skipping label apply"
  exit 0
fi
# The desired end state is the classified area PLUS every matched sub-label; anything else
# under `area:*` is stale. KEEP is that set — used for the no-op check, and again below as the
# cleanup's exemption list.
KEEP=$(jq -cn --arg a "$AREA" --argjson m "$MATCHED" '[$a] + $m')
if echo "$CURRENT_AREA" | jq -e --argjson keep "$KEEP" 'sort == ($keep | sort)' >/dev/null; then
  printf 'already labeled %s%s — nothing to do\n' "$AREA" "$(desired_suffix "$MATCHED")"
  exit 0
fi

# Add the selected label FIRST (additive POST — preserves non-area labels). A transient
# failure here means nothing was applied, so fail soft: the next run re-classifies.
if ! jq -n --arg a "$AREA" '{labels: [$a]}' \
     | gh api --method POST "repos/${GH_REPO}/issues/${PR_NUMBER}/labels" --input - >/dev/null 2>&1; then
  echo "::warning::could not add label ${AREA} to PR #${PR_NUMBER}; leaving labels unchanged"
  exit 0
fi

# Then add the matched sub-labels the PR doesn't already carry — additive, and separate from
# the POST above so a hiccup here can never cost the PR its classified area. sync-labels.sh
# creates these from the taxonomy when it lands on the default branch, so they exist with the
# right color/description by the time a PR matches one.
MISSING=$(jq -cn --argjson m "$MATCHED" --argjson cur "$CURRENT_AREA" '$m - $cur')
if [ "$MISSING" != "[]" ] \
   && ! jq -n --argjson l "$MISSING" '{labels: $l}' \
        | gh api --method POST "repos/${GH_REPO}/issues/${PR_NUMBER}/labels" --input - >/dev/null 2>&1; then
  printf '::warning::could not add path sub-label(s) %s to PR #%s; the next run reconciles\n' \
    "$(printf '%s' "$MISSING" | jq -r 'join(", ")')" "$PR_NUMBER"
fi

# Then remove every OTHER area:* label. Re-read a FRESH snapshot after the POST so a label
# added concurrently between the no-op check and now is also cleaned up. Everything in KEEP —
# the classified area AND the matched sub-labels — is exempt; a sub-label that NO LONGER
# matches is not in KEEP, so this loop is also what retires it when a PR stops touching those
# paths. The correct labels are already applied (add-first), so a cleanup hiccup can only
# leave a STALE EXTRA area label — a cosmetic state the next run reconciles — never an
# UNLABELED PR. So a non-404 delete error
# WARNS about the partial state rather than reding this advisory check. Read via a here-string
# (not a pipe) so the loop runs in THIS shell — a piped `while` is a subshell whose failures
# `set -e`/`pipefail` would surface as a red check. URL-encode the name for the path.
stale_area=$(gh pr view "$PR_NUMBER" --repo "$GH_REPO" --json labels \
  -q '[.labels[].name | select(startswith("area:"))]' 2>/dev/null \
  | jq -r --argjson keep "$KEEP" '.[] | . as $x | select($keep | index($x) | not)' || true)
while IFS= read -r stale; do
  [ -n "$stale" ] || continue
  enc=$(jq -rn --arg s "$stale" '$s | @uri')
  if err=$(gh api --method DELETE "repos/${GH_REPO}/issues/${PR_NUMBER}/labels/${enc}" 2>&1); then
    continue
  fi
  if printf '%s' "$err" | grep -q "HTTP 404"; then
    printf 'stale label %q already gone\n' "$stale"
  else
    # stale/err are untrusted (label name / API output); escape with %q so neither can forge
    # ::...:: workflow commands in the log. Partial state: AREA is applied, this stale remains.
    printf '::warning::set %q but could not remove stale label %q (%q); the next run reconciles\n' "$AREA" "$stale" "$err"
  fi
done <<< "$stale_area"
printf 'set %s%s — %q\n' "$AREA" "$(desired_suffix "$MATCHED")" "$REASON"
