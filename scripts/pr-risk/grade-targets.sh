#!/usr/bin/env bash
# grade-targets.sh — grade and label N pull requests, one target at a time. The orchestration
# layer of the reusable pr-risk.yml workflow: it sequences the four per-PR steps (resolve the
# base ref, fetch that ref's override files, poll the grader until the rest of the check rollup
# settles, sync the one label) and computes nothing about risk itself. The tier comes from
# grade-pr-risk.sh; the label write is apply-risk-label.sh's.
#
# EXTRACTED from pr-risk.yml's inline job body, unchanged in behaviour, for two reasons:
#   * ONE code path now serves all three ways a grade can be asked for — the `pull_request`
#     event, a `workflow_dispatch` naming one PR, and a dispatched comma-separated batch. The
#     alternative was a second copy of the settle-poll and the override fetch for the by-number
#     path, and this repo already knows what two copies of one grader cost (grade-pr-risk.sh's
#     own header records that lesson).
#   * It is TESTABLE. As inline YAML the poll loop, the 404-vs-error override contract and the
#     per-target failure isolation had no unit coverage at all; tests/test_grade_targets.sh now
#     drives every branch of them with a stubbed `gh`.
#
# ONE UNREADABLE TARGET NEVER ABANDONS THE REST. Each target is attempted independently and its
# outcome recorded; the run's exit code reports whether ANY target failed, but only after every
# target has had its turn. A 30-PR backfill that hits one deleted PR still labels the other 29.
#
# Inputs (env):
#   REPO                  owner/name of the repo holding the PRs                     (required)
#   PR_NUMBERS            target PR numbers, comma/space/newline separated           (required)
#   BASE_REF              base ref for the SINGLE target, when the caller already has it from
#                         the event payload. Honoured ONLY when there is exactly one target;
#                         empty means "resolve it from the API per target".
#   MAP_PATH / RB_PATH    per-repo override paths, read from each target's BASE ref
#   TOOL_DIR              directory holding grade-pr-risk.sh + apply-risk-label.sh
#                         (default: beside this script)
#   FLEET_LOGINS BOT_LOGINS SELF_CONTEXT SELF_RUN_ID   passed through to the grader
#   WAIT_MINUTES          per-target settle wait (default 10)
#   JOB_TIMEOUT_MINUTES   the calling job's timeout-minutes (default 30) — the whole run is
#                         budgeted against it, not just each target. Two deadlines come out of
#                         it: the WAIT budget (how long targets may sleep for CI) and the JOB
#                         deadline (after which a new target is not STARTED at all). They are
#                         not the same thing — see main().
#   LABEL_MAP             passed through to apply-risk-label.sh
#   MAX_TARGETS           refuse a list longer than this (default 50)
#   RESULTS               per-target JSONL outcome file (default pr-risk-results.jsonl)
#   DRY_RUN               1 = grade and report, write no label
#   GH_TOKEN              token for gh
#   GITHUB_OUTPUT         when set, tier / waited / targets / graded / ungraded / failed /
#                         skipped are appended
#
# Exit: 0 = every target ended with a label in sync (a `risk:ungraded` label IS in sync — an
#       unreadable PR is a reported verdict, not a broken run, exactly as on the event path).
#       1 = at least one target could not be graded or labeled, after all of them were tried.
#       2 = usage/setup error: nothing was attempted.
#
# Deliberately bash (shebang), not zsh — CI runners and the test suite both exercise bash.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL_DIR="${TOOL_DIR:-$SELF_DIR}"
GRADER="$TOOL_DIR/grade-pr-risk.sh"
LABELER="$TOOL_DIR/apply-risk-label.sh"

REPO="${REPO:-}"
PR_NUMBERS="${PR_NUMBERS:-}"
BASE_REF="${BASE_REF:-}"
MAP_PATH="${MAP_PATH:-.github/risk.json}"
RB_PATH="${RB_PATH:-.github/risk-runbooks.json}"
FLEET_LOGINS="${FLEET_LOGINS:-}"
BOT_LOGINS="${BOT_LOGINS:-}"
SELF_CONTEXT="${SELF_CONTEXT:-}"
SELF_RUN_ID="${SELF_RUN_ID:-}"
WAIT_MINUTES="${WAIT_MINUTES:-10}"
JOB_TIMEOUT_MINUTES="${JOB_TIMEOUT_MINUTES:-30}"
LABEL_MAP="${LABEL_MAP:-}"
MAX_TARGETS="${MAX_TARGETS:-50}"
RESULTS="${RESULTS:-pr-risk-results.jsonl}"
DRY_RUN="${DRY_RUN:-0}"
# The retry/backoff constants are env-overridable ONLY so the suite can exercise the
# unreadable-PR, read-retry and settle-repeat branches without sleeping through the production
# backoff. CI passes none of them, so the values below are what runs in production.
MAX_UNREADABLE_TRIES="${MAX_UNREADABLE_TRIES:-4}"
READ_RETRY_BUDGET_SECONDS="${READ_RETRY_BUDGET_SECONDS:-150}"
POLL_DELAY_SECONDS="${POLL_DELAY_SECONDS:-15}"
READ_RETRY_TRIES="${READ_RETRY_TRIES:-3}"
READ_RETRY_DELAY_SECONDS="${READ_RETRY_DELAY_SECONDS:-10}"

# Set by the per-target helpers, read by their callers.
G_TIER=""
G_WAITED=0
TARGET_COUNT=1
OVERALL_DEADLINE=0
JOB_DEADLINE=0

log()  { printf '[grade-targets] %s\n' "$*" >&2; }
die()  { printf '[grade-targets] ERROR %s\n' "$*" >&2; exit 2; }

# SCRATCH FILES AND THE EXIT TRAP ARE CREATED LAZILY, on first use, and the trap is installed only
# by a DIRECT invocation. At file scope they were side effects of merely SOURCING this file, and
# the `trap ... EXIT` replaced the sourcing shell's own EXIT trap — so a suite that sources these
# helpers to drive them directly silently lost its `rm -rf "$SANDBOX"` cleanup and leaked both the
# sandbox and these temp files. The footer's "sourceable without side effects" claim is only true
# with this deferred.
GT_DIRECT=0
[ "${BASH_SOURCE[0]}" = "${0}" ] && GT_DIRECT=1
ERRF=""
LABELF=""
OUTF=""
init_scratch() {
  [ -z "$ERRF" ] || return 0
  ERRF="$(mktemp "${TMPDIR:-/tmp}/grade-targets-err.XXXXXX")"     || die "mktemp failed"
  LABELF="$(mktemp "${TMPDIR:-/tmp}/grade-targets-label.XXXXXX")" || die "mktemp failed"
  OUTF="$(mktemp "${TMPDIR:-/tmp}/grade-targets-out.XXXXXX")"     || die "mktemp failed"
  [ "$GT_DIRECT" = 1 ] && trap 'rm -f "$ERRF" "$LABELF" "$OUTF"' EXIT
  return 0
}
gherr() {
  [ -n "$ERRF" ] && [ -f "$ERRF" ] || return 0
  tr '\n' ' ' < "$ERRF" | sed 's/[[:space:]]*$//'
}

# ---- URL building ------------------------------------------------------------------------------
# EVERY INTERPOLATED VALUE BELOW IS A URL COMPONENT, so it is percent-encoded like one. Git branch
# names legally contain `#`, `&`, `+` and `%`, and consumer-supplied override paths can too: raw,
# a PR based on `fix/#123-thing` had its request truncated at the `#`, which arrives at the
# contents endpoint as an EMPTY `?ref=` — and an empty ref is not an error there, it silently
# resolves to the repository DEFAULT branch. That is precisely the "graded against rules nobody
# read" failure resolve_base_ref exists to prevent, reached by a different door (`&` splits off a
# bogus query param; `+` decodes to a space and 404s into the generic-default fallback). This is
# the same reason apply-risk-label.sh encodes label names before putting them in a path.
enc()      { jq -rn --arg s "$1" '$s | @uri'; }
# A path keeps its separators — `/` is structural here, not data — but each SEGMENT is encoded.
enc_path() { jq -rn --arg s "$1" '$s | split("/") | map(@uri) | join("/")'; }

# ---- transient failures on the pre-grader reads ------------------------------------------------
# WHY THESE READS RETRY. Each target's base-ref read and its two override reads happen BEFORE the
# grader, which already retries this same failure class (rate limit, secondary rate limit,
# transient 5xx) four times with backoff, precisely so a blip does not become a durable verdict.
# Rate limits are GLOBAL rather than per-PR, so on a 50-PR backfill one secondary-rate-limit burst
# hit every remaining target at its very first hop and failed them wholesale — the inverse of the
# "one unreadable PR never abandons the rest" guarantee this file's header promises. Retrying here
# is what stops the batch's most-repeated read from being its least resilient one.
#
# A DEFINITIVE ANSWER IS NOT RETRIED. 404 (the path is absent, or no such PR), 401, 410 and 422 do
# not change on a second ask, and fetch_override needs the 404 verdict PROMPTLY to fall back to the
# shipped defaults. 403 is ambiguous — GitHub returns it both for a missing scope and for a
# secondary rate limit — so it is retried only when the message reads like a rate limit.
retryable_err() { # gh's stderr in $ERRF -> rc 0 when another attempt could plausibly differ
  local msg; msg="$(gherr)"
  case "$msg" in
    *"rate limit"*|*"Rate limit"*|*"secondary rate"*|*"abuse detection"*) return 0 ;;
    *"(HTTP 5"*|*"(HTTP 429)"*) return 0 ;;   # server side / explicit throttle
    *"(HTTP "*)                 return 1 ;;   # any other status is an answer, not a blip
    *)                          return 0 ;;   # no status at all: DNS, TLS, timeout, gh itself
  esac
}

retry_read() { # <outfile> <gh api args...> -> rc 0, else gh's rc with its stderr left in $ERRF
  init_scratch
  local out="$1"; shift
  local tries="$READ_RETRY_TRIES" attempt=1 delay="$READ_RETRY_DELAY_SECONDS" rc
  while :; do
    rc=0
    gh api "$@" > "$out" 2>"$ERRF" || rc=$?
    [ "$rc" -eq 0 ] && return 0
    retryable_err || return "$rc"
    [ "$attempt" -lt "$tries" ] || return "$rc"
    # A retry may never spend the time a LATER target needs: past the job's own deadline the
    # remaining targets are better reported un-attempted by number than started and cut off.
    [ "$JOB_DEADLINE" -eq 0 ] || [ "$(( $(date +%s) + delay ))" -lt "$JOB_DEADLINE" ] || return "$rc"
    log "read failed (attempt ${attempt}/${tries}) — retrying in ${delay}s: $(gherr)"
    sleep "$delay"
    attempt=$(( attempt + 1 ))
    delay=$(( delay * 2 )); [ "$delay" -le 60 ] || delay=60
  done
}

# ---- targets ---------------------------------------------------------------------------------
# A PR number is a positive integer with NO leading zeros. `007` is rejected rather than
# normalised: GitHub would resolve it to 7, so tolerating it would let `7,007` present as two
# targets for one PR and label it twice, the second write racing the first.
parse_targets() { # <raw> -> one validated, de-duplicated number per line
  local raw="$1" norm n seen=" " nums=() fields=()
  norm="$(tr ',\n\t' '   ' <<<"$raw")"
  read -ra fields <<<"$norm"
  for n in "${fields[@]}"; do
    [ -n "$n" ] || continue
    [[ "$n" =~ ^[1-9][0-9]*$ ]] \
      || die "bad PR number '$n' in PR_NUMBERS — want positive integers, comma-separated"
    case "$seen" in *" $n "*) log "target $n listed twice — grading it once"; continue ;; esac
    seen="$seen$n "
    nums+=("$n")
  done
  [ "${#nums[@]}" -gt 0 ] \
    || die "no PR numbers to grade. On the event path PR_NUMBERS comes from the pull_request payload; on a workflow_dispatch the caller must supply pr_number or pr_numbers. Refusing to exit 0 on an empty target list — a dispatch that silently grades nothing is worse than no dispatch."
  [ "${#nums[@]}" -le "$MAX_TARGETS" ] \
    || die "${#nums[@]} targets exceeds MAX_TARGETS=$MAX_TARGETS — split the backfill into batches. A list this long cannot finish inside the job's timeout, and a run cancelled mid-batch leaves an arbitrary prefix labeled with no record of where it stopped."
  printf '%s\n' "${nums[@]}"
}

# ---- the base ref ----------------------------------------------------------------------------
# WHY AN UNRESOLVED REF IS FATAL RATHER THAN DEFAULTED. The ref is interpolated into the override
# read below as `contents/${p}?ref=${base}`, and an EMPTY ref is not an error to that endpoint —
# GitHub resolves it to the repository's DEFAULT branch. So a base ref we failed to read would
# silently fetch some other branch's .github/risk.json (or fall back to the generic default when
# the PR's real base carries an override the default branch does not) and grade the PR against
# rules nobody read. That is the same failure the non-404 guard in fetch_override exists to
# prevent, arriving by a different door. Stacked PRs make it concrete: base refs on live PRs in
# the pilot repo include feature branches, not just `main`.
resolve_base_ref() { # <num> -> ref on stdout, rc 1 (reason already annotated on stderr)
  init_scratch
  local num="$1" ref
  if ! retry_read "$OUTF" "repos/${REPO}/pulls/${num}" --jq '.base.ref'; then
    echo "::error::could not read the base ref of ${REPO}#${num}: $(gherr). NOT grading it against the default branch's rules." >&2
    return 1
  fi
  ref="$(tr -d '\n' < "$OUTF")"
  case "$ref" in
    ""|null)
      echo "::error::the base ref of ${REPO}#${num} read back empty — refusing to fall through to the repository default branch, which would grade this PR against another branch's rules." >&2
      return 1 ;;
  esac
  printf '%s' "$ref"
}

# ---- the per-repo overrides -------------------------------------------------------------------
# Read from the target's BASE ref, so the PR being graded cannot edit the rules that judge it.
# Absent (a genuine 404) falls back to the shipped defaults; present-but-invalid is left for the
# grader's structural validation to reject loudly — a repo that commits a corrupt map must see
# red, not silent generic grading.
#
# ONLY A 404 MEANS ABSENT. Treating any non-zero exit as "no override" meant a 403 rate-limit, a
# 5xx or a network blip silently graded the PR against the generic default map instead of the
# repo's sharpened one — a LOWER tier computed from an input nobody read, which is the
# confident-answer-from-an-unread-source failure the unknown contract forbids everywhere else. So
# the status code is captured and anything that is not 200-or-404 fails the target.
#
# A 404 FROM THIS ENDPOINT IS TWO DIFFERENT ANSWERS. "the path is not in that tree" is the benign
# one this fallback is for; "no commit found for the ref" is NOT — it means the base branch was
# deleted or renamed (reachable on a by-number re-grade of an old PR), and treating it as "no
# override" grades the PR confidently against rules nobody read, the same failure the non-404 guard
# was written to stop. GitHub distinguishes them in the message body, so this does too.
fetch_override() { # <path> <outfile> <base-ref> -> prints the outfile, or nothing when absent
  init_scratch
  local p="$1" out="$2" base="$3"
  if retry_read "$out" "repos/${REPO}/contents/$(enc_path "$p")?ref=$(enc "$base")" \
       -H "Accept: application/vnd.github.raw"; then
    echo "using ${p} from ${base}" >&2
    printf '%s' "$out"
  elif grep -qi 'no commit found for the ref' "$ERRF"; then
    rm -f "$out"
    echo "::error::the ref '${base}' does not resolve in ${REPO} (${p} was requested from it): $(gherr). NOT falling back to the generic default: a 404 for the REF is not a 404 for the FILE, and grading against the default branch's rules is exactly what re-reading the base ref exists to prevent." >&2
    return 1
  elif grep -q '(HTTP 404)' "$ERRF"; then
    rm -f "$out"   # the redirect already created it empty; nothing must read it
    echo "no ${p} on ${base} — using the generic default" >&2
  else
    echo "::error::could not read ${p} from ${base}: $(gherr). NOT falling back to the generic default: that would grade this PR against rules nobody read." >&2
    return 1
  fi
}

# ---- grade one target, waiting for the rest of the rollup to settle ---------------------------
# Sets G_TIER and G_WAITED. rc 0 = there is a record to label (including the deliberate `unknown`
# one), rc 1 = the grader itself failed and nothing may be labeled.
#
# CHECKS SETTLE BEFORE THE LABEL DOES: the reversibility axis asks "did tests covering these
# lines actually run", and at event time the rest of the rollup is usually still running. The
# grading job excludes its own RUN from the rollup it reads, so what it waits out is the OTHER
# checks. On a workflow_dispatch the grading run's own check is attached to the dispatched ref,
# not to the PR's head commit, so there is nothing of ours in that rollup to wait out and a
# settled PR reads its true state on the first poll — which is what makes a LOW wait the right
# setting for a backfill, and why a low wait there does not reproduce the R2 floors that
# `wait_for_checks_minutes: 0` produces on the event path.
settle_grade() { # <num> <record-file> <map-override|""> <rb-override|""> <settle-deadline>
  local num="$1" record="$2" map_override="$3" rb_override="$4" deadline="$5"
  local args=( --repo "$REPO" --pr "$num" )
  [ -z "$SELF_CONTEXT" ] || args+=( --self-context "$SELF_CONTEXT" )
  [ -z "$SELF_RUN_ID" ]  || args+=( --self-run-id "$SELF_RUN_ID" )
  [ -z "$FLEET_LOGINS" ] || args+=( --fleet-logins "$FLEET_LOGINS" )
  [ -z "$BOT_LOGINS" ]   || args+=( --bot-logins "$BOT_LOGINS" )
  [ -z "$map_override" ] || args+=( --map "$map_override" )
  [ -z "$rb_override" ]  || args+=( --runbooks "$rb_override" )

  local empty_deadline pending state now delay nap settled_once rc
  # Grace for checks to REGISTER at all.
  empty_deadline=$(( $(date +%s) + 120 ))
  G_WAITED=0
  # Poll backoff: 15s doubling to 120s. The fixed 30s poll idled a runner up to the full wait on
  # every `synchronize` event, per PR, across the fleet, for a grade that is one API call —
  # increasing backoff cuts both runner minutes and API calls without changing settle behaviour.
  delay="$POLL_DELAY_SECONDS"
  settled_once=0
  # rc=3 (the PR could not be read) covers rate limits, secondary rate limits and transient 5xx
  # as well as a genuinely missing PR. Labeling `ungraded` on the first blip turns a momentary
  # hiccup into a durable verdict, so it is retried with backoff. This gets its OWN budget, not
  # the settle deadline: a transient API failure has nothing to do with how long the caller wants
  # to wait for CI, and sharing the deadline would leave `wait_for_checks_minutes: 0` with no
  # retry at all. It IS capped by the JOB's deadline — not by the wait budget, which a batch may
  # legitimately outlive while still grading — so one dead PR cannot get the run cancelled.
  local unreadable_tries=0 max_unreadable_tries="$MAX_UNREADABLE_TRIES" read_deadline read_delay=10
  read_deadline=$(( $(date +%s) + READ_RETRY_BUDGET_SECONDS ))
  [ "$JOB_DEADLINE" -eq 0 ] || [ "$read_deadline" -le "$JOB_DEADLINE" ] || read_deadline="$JOB_DEADLINE"
  while :; do
    rc=0
    bash "$GRADER" "${args[@]}" > "$record" || rc=$?
    case "$rc" in
      0|1) ;;                     # graded (1 = graded but unknown — still labeled)
      3)  unreadable_tries=$(( unreadable_tries + 1 ))
          if [ "$unreadable_tries" -lt "$max_unreadable_tries" ] && [ "$(date +%s)" -lt "$read_deadline" ]; then
            log "PR read failed (attempt ${unreadable_tries}/${max_unreadable_tries}) — retrying in ${read_delay}s"
            sleep "$read_delay"; G_WAITED=$(( G_WAITED + read_delay ))
            read_delay=$(( read_delay * 2 )); [ "$read_delay" -le 60 ] || read_delay=60
            continue
          fi
          log "PR unreadable via the API after ${unreadable_tries} attempt(s) — labeling ungraded"
          printf '{}' > "$record"
          break ;;
      *)  log "grader failed for ${REPO}#${num} (rc=$rc)"; G_TIER=""; return 1 ;;  # setup/usage bug
    esac
    pending="$(jq -r '.checks_pending_excl_self // false' "$record")"
    state="$(jq -r '.checks_state // "none"' "$record")"
    now="$(date +%s)"
    [ "$now" -lt "$deadline" ] || break
    if [ "$pending" = "true" ]; then
      settled_once=0
    elif [ "$state" = "none" ] && [ "$now" -lt "$empty_deadline" ]; then
      # An EMPTY rollup is checks that have not registered yet, not CI that finished — exiting
      # the poll here would apply a label that a check appearing a second later never revises.
      # Bounded by a SHORT grace window rather than the whole budget, so a repo with genuinely no
      # CI is not made to wait 25 minutes on every PR to arrive at the same honest R2.
      settled_once=0
    elif [ "$settled_once" -eq 0 ]; then
      # First settled reading. Require it to REPEAT before labeling: one transiently-quiet poll
      # is not a settled rollup.
      settled_once=1
    else
      break
    fi
    # CLAMP THE SLEEP TO WHAT REMAINS OF THE DEADLINE. The loop tests `now < deadline` and then
    # slept a full 15/30/60/120s, so a continuously-pending rollup overran the caller's wait by up
    # to 105s per target — and in a batch that overrun is charged to the LATER targets' budget,
    # pushing them into the un-attempted lane for time this target had no right to spend. The
    # backoff itself keeps doubling; only the nap is shortened.
    now="$(date +%s)"
    nap="$delay"
    [ "$(( now + nap ))" -le "$deadline" ] || nap=$(( deadline - now ))
    [ "$nap" -gt 0 ] || break
    sleep "$nap"; G_WAITED=$(( G_WAITED + nap ))
    delay=$(( delay * 2 )); [ "$delay" -le 120 ] || delay=120
  done
  G_TIER="$(jq -r '.risk.tier // "unknown"' "$record" 2>/dev/null)"
  # AN EMPTY TIER IS THE UNKNOWN LANE, NEVER A GRADED ONE. A record that is empty or not JSON at
  # all makes jq print nothing and exit 0, and apply-risk-label.sh maps an empty TIER to
  # `risk:ungraded` — so leaving G_TIER empty labeled the PR ungraded while this file recorded
  # `status: graded` with a null tier and the batch counters credited it as graded.
  [ -n "$G_TIER" ] || G_TIER=unknown
  return 0
}

# ---- one target, end to end -------------------------------------------------------------------
record_result() { # <pr> <status> <tier> <label> <record|""> <waited> <base-ref> <note>
  jq -cn --argjson pr "$1" --arg status "$2" --arg tier "$3" --arg label "$4" \
         --arg record "$5" --argjson waited "$6" --arg base "$7" --arg note "$8" \
    '{pr:$pr, status:$status,
      tier:(if $tier == "" then null else $tier end),
      label:(if $label == "" then null else $label end),
      record:(if $record == "" then null else $record end),
      waited:$waited,
      base_ref:(if $base == "" then null else $base end),
      note:(if $note == "" then null else $note end)}' >> "$RESULTS"
}

process_target() { # <num> -> rc 0 = label in sync, rc 1 = failed
  init_scratch
  local num="$1" base map rb record label deadline now label_rc
  record="record-${num}.json"
  G_TIER=""; G_WAITED=0

  if [ -n "$BASE_REF" ] && [ "$TARGET_COUNT" -eq 1 ]; then
    # The event path: the payload already carries the base ref of the one PR being graded, so no
    # API call is spent re-reading it. NOT honoured for a supplied number — the target may be a
    # different PR than the event's (or there may be no event at all), and the event's base ref
    # would then point the override read at the wrong branch.
    base="$BASE_REF"
  else
    base="$(resolve_base_ref "$num")" || {
      record_result "$num" failed "" "" "" 0 "" "the base ref could not be resolved — NOT graded"
      return 1
    }
  fi

  map="$(fetch_override "$MAP_PATH" "repo-risk-map-${num}.json" "$base")" || {
    record_result "$num" failed "" "" "" 0 "$base" "the risk-map override could not be read from ${base} — NOT graded"
    return 1
  }
  rb="$(fetch_override "$RB_PATH" "repo-risk-runbooks-${num}.json" "$base")" || {
    record_result "$num" failed "" "" "" 0 "$base" "the runbook-registry override could not be read from ${base} — NOT graded"
    return 1
  }

  # Per-target settle budget, clamped to what the WHOLE run has left. Without the second clamp a
  # batch spends the early targets' full waits and is cancelled mid-sleep on a later one, which
  # is the failure the single-target clamp in main() was added for, one level up.
  now="$(date +%s)"
  deadline=$(( now + 60 * WAIT_MINUTES ))
  [ "$deadline" -le "$OVERALL_DEADLINE" ] || [ "$TARGET_COUNT" -eq 1 ] || deadline="$OVERALL_DEADLINE"

  settle_grade "$num" "$record" "$map" "$rb" "$deadline" || {
    record_result "$num" failed "" "" "" "$G_WAITED" "$base" "the grader exited with a setup error — nothing was labeled"
    return 1
  }
  log "${REPO}#${num}: graded tier ${G_TIER} (waited ${G_WAITED}s for the rollup, base ${base})"

  # The labeler prints the applied label on stdout and its own log on stderr; the rc has to be
  # read from the command itself, not from a pipeline, so its output goes to a file first.
  label_rc=0
  REPO="$REPO" PR_NUMBER="$num" TIER="$G_TIER" LABEL_MAP="$LABEL_MAP" DRY_RUN="$DRY_RUN" \
    bash "$LABELER" > "$LABELF" || label_rc=$?
  label="$(tail -n 1 "$LABELF")"
  if [ "$label_rc" -ne 0 ]; then
    record_result "$num" failed "$G_TIER" "" "$record" "$G_WAITED" "$base" "graded ${G_TIER} but the label write FAILED (rc=${label_rc}) — the PR still carries whatever label it had"
    return 1
  fi
  # `unknown` is a REPORTED verdict, not a failure: the event path labels it `risk:ungraded` and
  # keeps the check green, and a dispatch must not start disagreeing about that.
  case "$G_TIER" in
    ""|unknown|null)
      record_result "$num" ungraded unknown "$label" "$record" "$G_WAITED" "$base" "the PR could not be read via the API — labeled ungraded, which is NOT a low-risk verdict" ;;
    *)
      record_result "$num" graded "$G_TIER" "$label" "$record" "$G_WAITED" "$base" "" ;;
  esac
  return 0
}

# ---- main --------------------------------------------------------------------------------------
main() {
  init_scratch
  [ -n "$REPO" ] || die "REPO is required"
  [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "bad REPO '$REPO' (want owner/name)"
  [ -f "$GRADER" ]  || die "grader not found at $GRADER (set TOOL_DIR)"
  [ -f "$LABELER" ] || die "label script not found at $LABELER (set TOOL_DIR)"
  command -v jq >/dev/null 2>&1 || die "jq not found on PATH"
  command -v gh >/dev/null 2>&1 || die "gh not found on PATH"
  [[ "$JOB_TIMEOUT_MINUTES" =~ ^[1-9][0-9]*$ ]] || die "bad JOB_TIMEOUT_MINUTES '$JOB_TIMEOUT_MINUTES' (want a positive integer — it is the calling job's timeout-minutes, and 0 leaves no time to grade anything)"
  [[ "$MAX_TARGETS" =~ ^[1-9][0-9]*$ ]]    || die "bad MAX_TARGETS '$MAX_TARGETS'"

  # CLAMP THE WAIT TO THE JOB'S OWN BUDGET rather than trusting the input description. A caller
  # who passes 40 against a 30-minute timeout gets a job cancelled mid-sleep: the label step
  # never runs, the PR keeps whatever label the previous push left, and the check goes red for a
  # reason nobody would guess. 5 minutes of headroom is left for the label + summary steps.
  local max_wait started
  started="$(date +%s)"
  max_wait=$(( JOB_TIMEOUT_MINUTES > 5 ? JOB_TIMEOUT_MINUTES - 5 : 0 ))
  [ "$WAIT_MINUTES" -ge 0 ] 2>/dev/null || WAIT_MINUTES=10
  if [ "$WAIT_MINUTES" -gt "$max_wait" ]; then
    echo "::warning::wait_for_checks_minutes=${WAIT_MINUTES} exceeds what a ${JOB_TIMEOUT_MINUTES}-minute job can spend waiting — clamped to ${max_wait}"
    WAIT_MINUTES="$max_wait"
  fi
  # TWO DEADLINES, BECAUSE THEY ANSWER TWO DIFFERENT QUESTIONS.
  #   OVERALL_DEADLINE is the WAIT budget: how long targets may collectively SLEEP for CI. It
  #   clamps each target's settle wait so a batch does not spend the early targets' full waits.
  #   JOB_DEADLINE is when a new target may no longer be STARTED, and it is the JOB's own timeout
  #   minus headroom for the in-flight label write and the summary step.
  # Gating the start on the WAIT budget conflated them: a spent wait budget only means no remaining
  # target may sleep, and grading with zero wait is still a real grade (an honest R2 floor at
  # worst), so targets there was ample time to grade were reported un-attempted instead. At
  # JOB_TIMEOUT_MINUTES <= 5 the wait budget is 0 from the first instant, which made a batch record
  # every target `skipped` and grade NOTHING.
  OVERALL_DEADLINE=$(( started + 60 * max_wait ))
  JOB_DEADLINE=$(( started + 60 * JOB_TIMEOUT_MINUTES - 90 ))

  # parse_targets runs in a command substitution, so its `die` exits that SUBSHELL — the rc has
  # to be propagated here or a bad number would leave an empty target list and exit 0, which is
  # the silent no-op this whole surface exists to avoid.
  local raw_targets targets=()
  raw_targets="$(parse_targets "$PR_NUMBERS")" || exit 2
  mapfile -t targets <<<"$raw_targets"
  TARGET_COUNT="${#targets[@]}"
  : > "$RESULTS"
  log "grading ${TARGET_COUNT} target(s) in ${REPO}: ${targets[*]}"

  local num graded=0 ungraded=0 failed=0 skipped=0 total_waited=0 attempted=0
  for num in "${targets[@]}"; do
    # BUDGET STOP. A target that cannot START inside the job's budget is reported as un-attempted
    # rather than begun and cut off: a run cancelled mid-label is the one outcome with no signal
    # at all, because the summary step never renders either. THE FIRST TARGET IS EXEMPT — a run
    # that grades nothing while reporting every target as un-attempted is a silent no-op with
    # extra steps, and however short the budget there is always time for one grade to be tried.
    if [ "$attempted" -gt 0 ] && [ "$(date +%s)" -ge "$JOB_DEADLINE" ]; then
      log "::warning::${REPO}#${num} was NOT graded — the job's time budget is spent. Re-dispatch the remaining numbers."
      record_result "$num" skipped "" "" "" 0 "" "not attempted — the job's time budget was spent before this target's turn"
      skipped=$(( skipped + 1 ))
      continue
    fi
    attempted=$(( attempted + 1 ))
    if process_target "$num"; then
      case "$G_TIER" in ""|unknown|null) ungraded=$(( ungraded + 1 )) ;; *) graded=$(( graded + 1 )) ;; esac
    else
      failed=$(( failed + 1 ))
    fi
    total_waited=$(( total_waited + G_WAITED ))
  done

  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      # `tier` is the single-target tier, which is what the step summary heading reads; a batch
      # has no one tier and says so rather than picking one.
      if [ "$TARGET_COUNT" -eq 1 ]; then echo "tier=${G_TIER:-unknown}"; else echo "tier="; fi
      echo "waited=$total_waited"
      echo "targets=$TARGET_COUNT"
      echo "graded=$graded"
      echo "ungraded=$ungraded"
      echo "failed=$failed"
      echo "skipped=$skipped"
    } >> "$GITHUB_OUTPUT"
  fi

  log "done: ${graded} graded, ${ungraded} ungraded, ${failed} failed, ${skipped} not attempted (waited ${total_waited}s total)"
  [ "$(( failed + skipped ))" -eq 0 ]
}

# Sourceable without side effects — no temp file is created and no EXIT trap installed until a
# helper actually needs one (see init_scratch), so a suite that sources this file to exercise the
# helpers keeps its own EXIT cleanup. Only a direct invocation runs main.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
