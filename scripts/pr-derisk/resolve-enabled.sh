#!/usr/bin/env bash
# resolve-enabled.sh — decide whether pr-derisk answers `/derisk` on this repo, and say who decided.
#
# Same two layers, same precedence and the same degrade direction as pr-risk's resolver
# (scripts/pr-risk/resolve-enabled.sh):
#
#   vars.DERISK_CONFIG   `{"enabled": true}` switches a caller on with no `with:` change;
#   (operational)        `{"enabled": false}` is a kill switch that needs no PR at all. The `vars`
#                        context inside a reusable resolves against the CALLING repository.
#
#   inputs.enabled       the reviewed default, pinned in the caller's workflow file. Used whenever
#   (reviewed)           the variable does not say — absent, blank, not an object, or carrying an
#                        `enabled` that is not a boolean.
#
# DEGRADE TOWARD THE REVIEWED VALUE, NEVER TOWARD OFF. A malformed variable must not be the reason
# a repo silently stops answering: "the variable is broken" and "the operator switched it off" are
# different states and must not look alike.
#
# NOT SHARED WITH pr-risk's COPY, deliberately. What differs between them is not the logic — it is
# the variable NAME and every sentence of the operator-facing summary, which are the product of a
# resolver. Sharing one file would mean passing that prose in as parameters, and a shared resolver
# would also tie the two workflows' caller fleets together: a wording fix in one would re-bump the
# other's pins for no behaviour change. The behaviour is pinned by tests on both sides instead.
#
# Env in:  INPUT_ENABLED (the reusable's `enabled` input), DERISK_CONFIG (the caller's variable).
# Env out: GITHUB_OUTPUT gets `enabled=true|false`; GITHUB_STEP_SUMMARY explains a disabled run.
#          Both are optional so the script is runnable — and testable — outside Actions.
set -uo pipefail

warn() { printf '::warning::%s\n' "$1" >&2; }

reviewed="${INPUT_ENABLED:-false}"
# Anything that is not exactly `true` is off. The input arrives as a workflow-call boolean, so
# this is belt-and-braces against a caller forwarding a string expression into it.
[ "$reviewed" = true ] || reviewed=false

enabled="$reviewed"
decided_by="the caller's reviewed \`enabled:\` input"

raw="${DERISK_CONFIG:-}"
if [ -n "${raw//[[:space:]]/}" ]; then
  if ! jq -e 'type == "object"' >/dev/null 2>&1 <<<"$raw"; then
    warn "vars.DERISK_CONFIG is set but is not a JSON object — ignoring it and using the reviewed input (enabled=${reviewed}). Expected {\"enabled\": true}."
  elif jq -e 'has("enabled")' >/dev/null 2>&1 <<<"$raw"; then
    # Read the value as TEXT and test the text. `jq -e` is not usable here: its exit code reflects
    # the TRUTHINESS of the output, so a perfectly valid `{"enabled": false}` exits 1 and would be
    # mistaken for a parse failure — silently disarming the kill switch, which is the half of this
    # lever that has to work when something is going wrong.
    val="$(jq -r '.enabled | if type == "boolean" then tostring else "" end' <<<"$raw" 2>/dev/null)"
    if [ "$val" = true ] || [ "$val" = false ]; then
      enabled="$val"
      # shellcheck disable=SC2016  # markdown backticks for the log line, not a substitution
      decided_by='`vars.DERISK_CONFIG`'
    else
      warn "vars.DERISK_CONFIG has an \`enabled\` key but it is not a boolean — ignoring it and using the reviewed input (enabled=${reviewed}). Use true/false, not \"true\"."
    fi
  fi
fi

[ -z "${GITHUB_OUTPUT:-}" ] || echo "enabled=$enabled" >> "$GITHUB_OUTPUT"
echo "pr-derisk enabled=$enabled (decided by ${decided_by})"

if [ "$enabled" != true ] && [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  # UNLIKE pr-risk, there is no dispatch exemption to describe: every run of this workflow is
  # already an explicit, attributed act by someone with write access (they typed `/derisk`), so
  # `enabled` is the ONE switch and a disabled run is a plain no-op with no second path to
  # qualify. The summary says exactly that and nothing more.
  # shellcheck disable=SC2016  # the body is literal markdown; backticks are code spans
  {
    echo "## \`/derisk\` is DISABLED for this repository"
    echo
    echo "No plan was computed and no comment was posted or updated — any \`/derisk\` comment"
    echo "already on this pull request is left exactly as the last enabled run left it."
    echo
    echo "Decided by ${decided_by}. To switch it on, either set the repository variable"
    echo '`DERISK_CONFIG` to `{"enabled": true}` (takes effect on the next run, no PR needed), or'
    echo 'pass `enabled: true` in the caller'"'"'s `with:` block.'
  } >> "$GITHUB_STEP_SUMMARY"
fi
