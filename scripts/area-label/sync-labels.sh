#!/usr/bin/env bash
# sync-labels.sh — reconcile the consumer repo's `area:*` labels to its taxonomy YAML.
# `gh label create --force` create-or-updates, so this is idempotent: edit the YAML, never
# the GitHub UI (the UI drifts, this heals). Runs on push to the consumer's default branch
# when the taxonomy changes (or a manual full-sync), where the checkout is trusted — so it
# reads the taxonomy from the working tree rather than the API.
#
# Env:
#   TAXONOMY_FILE  path to the taxonomy YAML in the checked-out consumer repo
#   GH_REPO        owner/name (github.repository)
#   GH_TOKEN       token with `issues: write` (labels are an issues API surface)
#
# Needs no ANTHROPIC key — label sync is deterministic. Requires yq + gh.

set -euo pipefail

: "${TAXONOMY_FILE:?TAXONOMY_FILE is required}"
: "${GH_REPO:?GH_REPO is required}"

if [ ! -f "$TAXONOMY_FILE" ]; then
  echo "::warning::taxonomy file $TAXONOMY_FILE not found; nothing to sync"
  exit 0
fi

count=$(yq '.labels | length' "$TAXONOMY_FILE")
if ! [[ "$count" =~ ^[0-9]+$ ]] || [ "$count" -eq 0 ]; then
  echo "::warning::taxonomy $TAXONOMY_FILE has no labels; nothing to sync"
  exit 0
fi

for i in $(seq 0 $((count - 1))); do
  name=$(yq ".labels[$i].name" "$TAXONOMY_FILE")
  color=$(yq ".labels[$i].color" "$TAXONOMY_FILE")
  desc=$(yq ".labels[$i].description" "$TAXONOMY_FILE")
  echo "syncing $name"
  gh label create "$name" --repo "$GH_REPO" --color "$color" --description "$desc" --force
done
