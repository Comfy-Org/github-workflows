#!/usr/bin/env bash
# lib.sh — the sourceable core of the pr-risk tooling: the scratch files, the retrying `gh`
# reads built on them, and the two resolvers that decide WHICH branch's rules judge a PR
# (`resolve_base_ref` / `fetch_override`). Everything here is a function or a plain variable
# assignment, so a script can source this file and get the helpers without also getting an
# entrypoint: grade-targets.sh sources it and adds main() plus the per-target orchestration;
# pr-derisk's collect-pr-inputs.sh sources it to resolve a `/derisk` target's rules through the
# SAME implementation that resolved them when pr-risk graded the PR.
#
# EXTRACTED from grade-targets.sh, unchanged in behaviour. That file was an executable
# entrypoint AND a library at once, and collect-pr-inputs.sh sourced the whole 619 lines of it
# for three helpers. Both of the workarounds that made the sourcing safe were properties of the
# file boundary rather than of the logic, and both are gone with the boundary:
#   * the EXIT trap that grade-targets.sh installed at file scope REPLACED the sourcing shell's
#     own trap, so a suite that sourced these helpers silently lost its cleanup. It took a
#     `GT_DIRECT` flag plus a lazy `init_scratch` to defer. Nothing sources grade-targets.sh
#     now, so its trap is installed unconditionally inside main() and the flag is gone;
#   * grade-targets.sh's file-scope `log`/`die` CAPTURED the sourcing script's own, so
#     collect-pr-inputs.sh had to define its diagnostics twice — once before the source for the
#     argument checks and once after to take them back. Hence `gt_log`/`gt_die` below: prefixed
#     names cannot collide with a caller's, so there is nothing to take back.
#
# NO TOP-LEVEL SIDE EFFECTS, and that is a tested property rather than a convention: no command
# runs, no scratch file is created and no trap is installed by sourcing this file.
# tests/test_grade_targets.sh phase 21 asserts it of grade-targets.sh, which sources this.
#
# Requires jq and gh on PATH. REPO must name the repo being read; the sourcing script's own
# input contract is what documents it.
#
# Deliberately bash (shebang), not zsh — CI runners and the test suite both exercise bash.

# ---- diagnostics -------------------------------------------------------------------------------
# PREFIXED NAMES, so sourcing this file cannot capture a caller's own `log`/`die` — which is what
# forced collect-pr-inputs.sh to define its diagnostics twice, before and after the source.
#
# The message PREFIX is still the sourcing script's, though, and that is the whole point of the
# variable: a retry line emitted while pr-derisk is collecting inputs must not read
# `[grade-targets]`, naming a script that did not emit it, in a run log the public can read. Set
# it BEFORE sourcing this file; it defaults to the script these helpers came from.
GT_LOG_PREFIX="${GT_LOG_PREFIX:-grade-targets}"
gt_log() { printf '[%s] %s\n' "$GT_LOG_PREFIX" "$*" >&2; }
gt_die() { printf '[%s] ERROR %s\n' "$GT_LOG_PREFIX" "$*" >&2; exit 2; }

# ---- configuration -----------------------------------------------------------------------------
REPO="${REPO:-}"
# The retry/backoff constants are env-overridable ONLY so the suite can exercise the read-retry
# branch without sleeping through the production backoff. CI passes neither, so the values below
# are what runs in production. (grade-targets.sh holds the settle-poll's own constants.)
READ_RETRY_TRIES="${READ_RETRY_TRIES:-3}"
READ_RETRY_DELAY_SECONDS="${READ_RETRY_DELAY_SECONDS:-10}"
# The job-wide deadline retry_read refuses to sleep past. 0 = no deadline, which is what a
# single-target caller like collect-pr-inputs.sh runs with; grade-targets.sh's main() computes a
# real one from the calling job's timeout before any target is attempted.
# UNCONDITIONAL, unlike the knobs above — sourcing this file RESETS it to 0. A deadline is a value
# only the sourcing script can compute (it needs that job's timeout), so it is set AFTER the source,
# which is what main() does; a value computed BEFORE the source would be silently discarded here.
# Deliberately not `${JOB_DEADLINE:-0}`: retry_read feeds this straight to `-eq`, so inheriting an
# unvalidated string from the ambient environment would turn a typo into an arithmetic error inside
# the retry loop rather than a clean default.
JOB_DEADLINE=0

# ---- scratch files -----------------------------------------------------------------------------
# CREATED LAZILY, on first use by a helper that needs one — never at file scope, where they were
# a side effect of merely sourcing. Cleanup belongs to whoever installs an EXIT trap over them:
# grade-targets.sh's main() does, and its footer no longer has to reason about being sourced.
ERRF=""
LABELF=""
OUTF=""
init_scratch() {
  [ -z "$ERRF" ] || return 0
  ERRF="$(mktemp "${TMPDIR:-/tmp}/grade-targets-err.XXXXXX")"     || gt_die "mktemp failed"
  # LABELF is the one scratch file no helper here touches — grade-targets.sh's process_target
  # writes the label sync's output into it. It is minted here anyway so that all three are
  # created, and cleaned up, as one set: a caller that installs the EXIT trap over "$ERRF"
  # "$LABELF" "$OUTF" must not have to know which of them some future helper started using.
  # shellcheck disable=SC2034
  LABELF="$(mktemp "${TMPDIR:-/tmp}/grade-targets-label.XXXXXX")" || gt_die "mktemp failed"
  OUTF="$(mktemp "${TMPDIR:-/tmp}/grade-targets-out.XXXXXX")"     || gt_die "mktemp failed"
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
# "one unreadable PR never abandons the rest" guarantee grade-targets.sh's header promises. Retrying here
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
    gt_log "read failed (attempt ${attempt}/${tries}) — retrying in ${delay}s: $(gherr)"
    sleep "$delay"
    attempt=$(( attempt + 1 ))
    delay=$(( delay * 2 )); [ "$delay" -le 60 ] || delay=60
  done
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
  # THE PATH MUST STAY UNDER `contents/`. enc_path percent-encodes each segment, but jq's `@uri`
  # leaves `.` alone by design, so a `..` segment survives encoding INTACT and the request becomes
  # `repos/OWNER/NAME/contents/../../x?ref=…` — a different endpoint once the dot segments resolve,
  # asked with the grading token. These paths arrive from the free-form `repo_map_path` /
  # `repo_runbooks_path` inputs, which neither pr-risk.yml nor pr-derisk.yml validates, so the URL
  # builder is the only place the SHAPE can be checked.
  # REFUSED, NEVER SILENTLY REWRITTEN. Stripping the dot segments and fetching what is left would
  # grade the PR against a file the repo did not ask for; falling through to the generic default
  # would grade it against rules nobody read. Both are the failure the 404-only contract below
  # exists to prevent, so an unusable path is an error on the target, exactly like a 5xx.
  case "$p" in
    "")                  echo "::error::the override path is empty — refusing to request the repository's contents root instead of a file." >&2; return 1 ;;
    /*)                  echo "::error::the override path '${p}' is absolute — it must be relative to the repository root." >&2; return 1 ;;
    ..|../*|*/..|*/../*) echo "::error::the override path '${p}' contains a '..' segment — refusing to build a contents URL that resolves outside the repository tree." >&2; return 1 ;;
  esac
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
