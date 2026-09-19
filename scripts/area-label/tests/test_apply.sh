#!/usr/bin/env bash
# test_apply.sh — hermetic tests for classify-and-apply.sh, the side-effecting half of the
# classifier. `gh` and `curl` are STUBBED, so there is no network and no API key: the stub
# `gh` keeps the PR's label set in a file and mutates it on POST/DELETE exactly as the real
# endpoints would, which is what makes the end-state assertions below meaningful rather than
# just a transcript diff.
#
# What this covers that tests/test_lib.sh cannot: the label SET arithmetic — one classified
# area plus the matched path sub-labels, the no-op short-circuit, and the cleanup that
# retires a sub-label once a PR stops touching its paths while leaving the matched ones and
# every non-area label alone.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SELF_DIR/../classify-and-apply.sh"
[ -f "$SCRIPT" ] || { echo "FATAL: $SCRIPT not found" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found on PATH" >&2; exit 2; }
command -v yq >/dev/null 2>&1 || { echo "FATAL: yq not found on PATH" >&2; exit 2; }

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/area-apply-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n     got: %s\n' "$1" "${2:-}"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2')" "$3"; fi; }
has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "$2" ;; esac; }
hasnt() { case "$2" in *"$3"*) bad "$1" "$2" ;; *) ok "$1" ;; esac; }

# ── stubs ─────────────────────────────────────────────────────────────────────
STUB="$SANDBOX/bin"; mkdir -p "$STUB"

# Fake `gh`: serves the taxonomy and the PR payload from fixtures, and treats $STATE_LABELS
# as the PR's live label set so POST/DELETE actually move it.
cat > "$STUB/gh" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >> "$CALL_LOG"
if [ "${1:-}" = "pr" ] && [ "${2:-}" = "view" ]; then
  # Two distinct reads: the classifier's PR payload, and the current label set.
  case "$*" in
    *title,body,files,labels*) cat "$FIXTURE_PR"; exit 0 ;;
    *) jq -c '[.[] | select(startswith("area:"))]' "$STATE_LABELS"; exit 0 ;;
  esac
fi
if [ "${1:-}" = "api" ]; then
  method=GET; path=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --method) method="$2"; shift 2; continue ;;
      repos/*)  path="$1" ;;
    esac
    shift
  done
  case "$method:$path" in
    GET:*contents*) cat "$FIXTURE_TAXONOMY"; exit 0 ;;
    POST:*/labels)
      add=$(jq -r '.labels[]' -)
      while IFS= read -r l; do
        [ -n "$l" ] || continue
        jq --arg l "$l" '. + [$l] | unique_by(.)' "$STATE_LABELS" > "$STATE_LABELS.tmp" \
          && mv "$STATE_LABELS.tmp" "$STATE_LABELS"
      done <<< "$add"
      exit 0 ;;
    DELETE:*/labels/*)
      name=$(printf '%s' "${path##*/labels/}" | sed 's/%3A/:/g')
      jq --arg l "$name" 'map(select(. != $l))' "$STATE_LABELS" > "$STATE_LABELS.tmp" \
        && mv "$STATE_LABELS.tmp" "$STATE_LABELS"
      exit 0 ;;
  esac
fi
exit 0
EOF

# Fake `curl`: honours -o <file> by dropping the canned classifier reply there, and prints
# the HTTP code the real call's -w would.
cat > "$STUB/curl" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
out=""; take=0
for a in "$@"; do
  [ "$take" = 1 ] && { out="$a"; take=0; continue; }
  [ "$a" = "-o" ] && take=1
done
[ -n "$out" ] && cp "$FIXTURE_RESP" "$out"
printf '%s' "${STUB_HTTP:-200}"
EOF
chmod +x "$STUB/gh" "$STUB/curl"

# ── fixtures ──────────────────────────────────────────────────────────────────
TAXONOMY="$SANDBOX/taxonomy.yml"
cat > "$TAXONOMY" <<'YAML'
repo_context: |
  A monorepo of backend services.
labels:
  - name: "area:api"
    color: "1d76db"
    description: "The API service"
  - name: "area:ci"
    color: "5319e7"
    description: "CI workflows"
sub_labels:
  - name: "area:router"
    color: "7057ff"
    description: "Router: the model-routing layer inside the API service"
    paths:
      - "services/api/router*/**"
YAML

BAD_TAXONOMY="$SANDBOX/bad-taxonomy.yml"
cat > "$BAD_TAXONOMY" <<'YAML'
labels:
  - name: "area:api"
    color: "1d76db"
    description: "The API service"
sub_labels:
  - name: "router"          # not an area:* slug
    color: "7057ff"
    description: "Router"
    paths: ["services/api/router*/**"]
YAML

ROUTER_PR="$SANDBOX/pr-router.json"
printf '%s' '{"title":"feat(router): queue","body":"","files":["services/api/routerqueue/q.go"],"labels":[]}' > "$ROUTER_PR"
PLAIN_PR="$SANDBOX/pr-plain.json"
printf '%s' '{"title":"fix(api): db","body":"","files":["services/api/db/schema.go"],"labels":[]}' > "$PLAIN_PR"

RESP="$SANDBOX/resp-api.json"
printf '%s' '{"content":[{"type":"text","text":"{\"area\":\"area:api\",\"reason\":\"API service\"}"}]}' > "$RESP"

# run <labels-json> <pr-fixture> [env assignments…] — execute the script in a fresh work dir
# against a given starting label set; leaves $OUT (stdout+stderr) and $LABELS (end state).
run() {
  local start="$1" pr="$2"; shift 2
  local work="$SANDBOX/work"; rm -rf "$work"; mkdir -p "$work"
  printf '%s' "$start" > "$work/labels.json"
  : > "$work/calls.log"
  OUT=$(cd "$work" && env PATH="$STUB:$PATH" \
    STATE_LABELS="$work/labels.json" CALL_LOG="$work/calls.log" \
    FIXTURE_TAXONOMY="$TAXONOMY" FIXTURE_PR="$pr" FIXTURE_RESP="$RESP" \
    GH_REPO="Example-Org/repo" PR_NUMBER="7" BASE_REF="deadbeef" \
    ANTHROPIC_API_KEY="test-key" "$@" bash "$SCRIPT" 2>&1)
  LABELS=$(jq -c 'sort' "$work/labels.json")
  CALLS=$(cat "$work/calls.log")
}

echo "— a PR touching router files gets BOTH labels —"
run '[]' "$ROUTER_PR"
eq "classified area and sub-label are both applied" '["area:api","area:router"]' "$LABELS"
has "the log names the full end state" "$OUT" "set area:api + area:router"
has "the match is reported before the model call" "$OUT" "path sub-labels matched: area:router"
hasnt "nothing is deleted on a clean apply" "$CALLS" "--method DELETE"

echo "— a PR that touches no router file gets only the classified area —"
run '[]' "$PLAIN_PR"
eq "only the classified area is applied" '["area:api"]' "$LABELS"
hasnt "no sub-label is mentioned" "$OUT" "area:router"

echo "— idempotence —"
run '["area:api","area:router"]' "$ROUTER_PR"
eq "the correct end state is left alone" '["area:api","area:router"]' "$LABELS"
has "and reported as a no-op" "$OUT" "nothing to do"
hasnt "with no write at all" "$CALLS" "--method"

echo "— the sub-label is retired when the PR stops touching those paths —"
run '["area:api","area:router"]' "$PLAIN_PR"
eq "a no-longer-matching sub-label is removed" '["area:api"]' "$LABELS"

echo "— a misclassified PR is corrected without losing the sub-label —"
run '["area:ci","area:router"]' "$ROUTER_PR"
eq "the wrong area goes, the matched sub-label stays" '["area:api","area:router"]' "$LABELS"

echo "— non-area labels are never touched —"
run '["bug","area:ci"]' "$ROUTER_PR"
eq "unrelated labels survive the cleanup" '["area:api","area:router","bug"]' "$LABELS"

echo "— dry run —"
run '[]' "$ROUTER_PR" DRY_RUN=true
eq "nothing is written" '[]' "$LABELS"
has "but the full decision is printed" "$OUT" "[dry-run] would set area:api + area:router"

echo "— fail-soft paths —"
run '[]' "$ROUTER_PR" ANTHROPIC_API_KEY=""
eq "no API key writes nothing" '[]' "$LABELS"
has "and warns" "$OUT" "skipping area classification"

FIXTURE_SWAP="$TAXONOMY"; TAXONOMY="$BAD_TAXONOMY"
run '["area:api"]' "$ROUTER_PR"
eq "a malformed sub_labels block writes nothing" '["area:api"]' "$LABELS"
has "and names the gate that refused it" "$OUT" "malformed sub_labels"
TAXONOMY="$FIXTURE_SWAP"

run '[]' "$ROUTER_PR" STUB_HTTP=500
eq "a failed classifier call writes nothing" '[]' "$LABELS"
has "and warns" "$OUT" "classifier API call failed"

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
