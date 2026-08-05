#!/usr/bin/env python3
"""Runtime cadence gate for the stateless groom CI run (BE-4004).

GitHub Actions `schedule:` cron is **static in the workflow file** — there is no
native "every N days" input. The standard pattern is therefore a **frequent base
cron + a runtime gate**: the caller fires on a daily cron, but the reusable
early-exits unless enough time has elapsed since the last real groom run. This
module is that gate — it decides, cheaply, whether a given tick should proceed to
the (expensive) finder or no-op.

The effective cadence is the repo Actions variable **`GROOM_INTERVAL_DAYS`**
(default `7` = today's weekly behavior), read fresh each run. Changing the
variable retunes cadence with no workflow-file edit — the same "live knob"
ergonomics as the per-repo caps.

**Last-run state is derived from GitHub Actions run history** — the GitHub-native
option that needs no net-new secret and no writable durable store (a repo
variable would need an extra `Variables: write` credential the run does not
otherwise carry, and a missing grant would fail *silently* into a daily
over-spend). A prior run "counts" as a real groom only if it actually reached the
finder (its `Audit — finder` job ran, i.e. was not `skipped` by this very gate)
AND actually spent the audit — a job that ended BEFORE its agent step (checkout,
asset load, prompt build) billed nothing, so it must not advance the clock
(BE-4814, see `run_audited`). Conversely a job that hung and tripped its
40-minute timeout billed the most of any outcome, so it must. The interval-skip
ticks in between never reset it either. Run history is durable across the
stateless CI runs and readable with only `actions: read`.
The clock is also **per scope** (groom.yml's `path` input, BE-4757): a run counts
only against a tick auditing the SAME scope. A path-scoped run must not stamp
"done" over the whole-repo audit it never performed, and — symmetrically — a
permanently path-scoped caller must still get a working cadence for ITS
directory rather than re-billing an audit every tick. See `_SCOPED_MARKER_PREFIX`.

The gate is **fail-open**, matching the volume gate: any error deriving the last
run (API hiccup, unparseable timestamp, no history) RUNS the audit rather than
silently skipping a due groom. `workflow_dispatch` **always** bypasses the gate —
a manual run is an explicit override.

A tick clears the gate a half-tick EARLY (`interval_threshold`): GitHub's cron
fires late by an unpredictable amount, so demanding a full `interval_days` on a
daily tick would skip at 6.99 days elapsed and ratchet the cadence a day later
every cycle. See `_TICK_TOLERANCE_DAYS`.

The pure decision logic (`run_audited`, `days_since`, `interval_threshold`,
`interval_decision`) is separated from the thin `gh` I/O shell
(`fetch_workflow_runs`, `fetch_run_jobs`) so it is fully unit-testable with no
network.

CLI (what the groom gate job calls):

    python3 .github/groom/interval.py \
        --repo owner/name --workflow-file ci-groom.yml \
        --current-run-id 123 --interval-days 7 --event-name schedule \
        --path ''            # the scope this tick would audit ('' = whole repo)

Prints a `{should_run, reason, interval_days, days_since, last_run_at}` decision
JSON to stdout; the gate step reads `.should_run`. Always exits 0 (the decision
is carried in the JSON, and any failure is folded into a fail-open `should_run`).
"""

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone

# A prior run only resets the cadence clock if it actually ran the finder — the
# gate names that job `Audit — finder` (job id `audit_find`), and in a called
# reusable the jobs API reports it as "<caller-job> / Audit — finder". Match on
# either the display-name or job-id form (case-insensitive) to be robust to how
# GitHub renders nested-reusable job names. An interval-skip tick has this job
# `skipped`, so it never matches the audited conclusions below.
_FINDER_JOB_HINTS = ("finder", "audit_find")

# The subset of the above that proves we are reading a RENDERED DISPLAY name, and
# can therefore read the scope marker off it. The bare job-id form (`audit_find`)
# carries no marker — not because it is whole-repo, but because the name never
# went through groom.yml's `name:` expression at all — so it is treated as
# UNKNOWN scope and counts for nothing. See `run_audited`.
_DISPLAY_NAME_HINTS = ("finder",)

# The cadence clock is PER SCOPE (groom.yml's `path` input, BE-4757). The signal
# has to survive the runs API, which does NOT return a run's dispatch INPUTS — so
# groom.yml renames the finder job itself when `path` is set (`Audit — finder
# (scoped: services/api)`). Job names are the one per-run discriminator both
# sides can see.
#
# Both directions matter:
#
# * A scoped run must not reset the WHOLE-REPO clock. `workflow_dispatch`
#   deliberately bypasses this gate, so without the marker a manual
#   `path: services/api` run would reach the finder, become "the last real
#   groom", and suppress the next scheduled whole-repo tick for a full
#   GROOM_INTERVAL_DAYS — a PARTIAL audit stamping "done" over the full one.
# * A permanently scoped caller (an explicitly documented pattern: pin `path` in
#   `with:` to groom one directory as its own unit) must still HAVE a clock. With
#   a scope-blind exclusion every one of its finder jobs is invisible, the gate
#   fails open on every tick, and the billed audit re-runs daily no matter what
#   GROOM_INTERVAL_DAYS says — the cadence knob silently defeated for exactly the
#   configuration the `path` input advertises.
#
# So the marker carries the path, and a tick counts only a prior run of its OWN
# scope. The path charset (`scope.py:_COMPONENT_RE` plus `/`) is deliberately
# narrow, so it embeds in a job name without escaping concerns.
_SCOPED_MARKER_PREFIX = "(scoped:"

# Collision direction is deliberate everywhere here: an unrecognised, truncated
# or ambiguous job name (a caller job id long enough to push the marker past
# GitHub's name rendering, say) makes a real run stop counting, i.e. groom runs
# MORE often. That is the same fail-open bias as every other branch in this
# module — never the silent under-run.

# A finder job that reached `success` spent the (billed) audit — nothing else to
# check, the agent step is upstream of every step that could still fail.
_AUDITED_CONCLUSIONS = {"success"}

# The two endings that definitively spent NOTHING, so they never even reach the
# evidence path: `skipped` is this very gate's own interval-skip, and a null
# conclusion means the run is not finished. Deliberately a DENYLIST — every other
# ending may or may not have billed, and is resolved by the per-step evidence in
# `agent_step_started`: if the agent step ran, the audit was spent no matter how
# the job was finally stamped. An allowlist here would have to enumerate the whole
# API vocabulary (`failure`, `timed_out`, `cancelled`, and the rarer `neutral` /
# `stale` / `action_required`), and any conclusion it forgot would silently mean
# "billed, but never counted" — i.e. re-spent on every subsequent daily tick.
#
# `timed_out` and `cancelled` are the EXPENSIVE members: the finder job carries
# `timeout-minutes: 40`, so an agent that hangs bills the full window and *then*
# trips the job timeout — the single costliest outcome this module sees.
#
# The evidence requirement itself is BE-4814. Counting a failure that spent money
# but died at a LATER step (e.g. the JSON assert) keeps it from re-spending on the
# very next daily tick — that half is wanted. But the finder job can also die far
# BEFORE the agent: checkout, the asset load, the prompt build, the runner itself.
# Those spend nothing, and counting them advances the cadence clock, so a typo'd
# input or a broken caller goes quiet for a whole GROOM_INTERVAL_DAYS window
# instead of recurring (and being noticed) daily. This gate is fail-OPEN
# everywhere else; that was the one branch that failed closed.
_NEVER_AUDITED_CONCLUSIONS = {"skipped", ""}

# The billed agent step inside the finder job, matched EXACTLY (after strip)
# against the `steps[]` the runs-jobs API returns per job. The literal lives here,
# next to the matcher that consumes it, so the producing `- name:` in groom.yml
# and this comparison cannot drift apart silently (`test_interval.py` pins both
# halves). Exact, not substring: the same job also has `Build finder prompt`,
# `Assert finder produced JSON` and `Upload finder candidates`, none of which is
# evidence that a single token was billed.
_AGENT_STEP_NAME = "Run finder"

# Step states that mean the agent step never STARTED. GitHub reports an unreached
# step either as still `queued` (job died before it) or as `completed` with a
# `skipped` conclusion, and which one you get depends on where the job died — so
# both forms are checked rather than trusting either shape. Anything else
# (in_progress, or completed with success/failure) means the agent ran and the
# audit is spent.
_UNSTARTED_STEP_STATUSES = {"queued", "waiting", "pending", "requested"}
_UNSTARTED_STEP_CONCLUSIONS = {"skipped"}

# `cancelled` is the ambiguous conclusion and must NOT be read as "never started":
# GitHub stamps an already-RUNNING step `cancelled` when the workflow is cancelled
# or the job trips its `timeout-minutes`, so a hung agent that billed for 40
# minutes lands here — reading that as unstarted forgets the most expensive audit
# there is. It equally stamps some unreached steps `cancelled` on the way down.
# The conclusion alone cannot separate them; the TIMESTAMPS can. A step that
# actually ran has a `started_at` strictly before its `completed_at`; an unreached
# one carries no span (null timestamps, or both stamped at the same cancellation
# instant). No demonstrable span -> no evidence -> not audited, which is the same
# fail-open direction every other branch here takes.
_CANCELLED_STEP_CONCLUSIONS = {"cancelled", "canceled"}

# Default cadence when GROOM_INTERVAL_DAYS is unset/blank/garbage — 7 days keeps
# the documented weekly behavior (AC: unset variable stays weekly, matching today).
_DEFAULT_INTERVAL_DAYS = 7.0

# Slack allowed on the elapsed-days bar, in days, to absorb scheduler jitter.
#
# GitHub fires `schedule:` cron LATE by an unpredictable amount (routinely
# minutes, sometimes tens of minutes), and the base cron is a DAILY tick, so the
# gap between two consecutive real runs is measured with that much noise while
# the only available answers are ~1 day apart. Requiring a FULL `interval_days`
# therefore drops a whole tick whenever the due tick happens to fire earlier
# relative to the anchor than the last real run did — last real run started Mon
# 09:31 (scheduler delay), next Monday's tick starts 09:19, elapsed 6.99 < 7, so
# it skips and the run slips to Tuesday. Worse, the clock re-anchors on THAT
# later run, so the cadence ratchets later every cycle (7 -> 8 -> 8 days...).
#
# Clearing the bar half a tick early absorbs the jitter. It cannot make two real
# runs land on consecutive daily ticks: those are a full ~1.0 day apart, which
# still exceeds the 0.5-day tolerance on any interval >= 1.
_TICK_TOLERANCE_DAYS = 0.5

# Floor for the volume gate's merge-activity window. The volume gate shares this
# same cadence knob, but a sub-1-day lookback is meaningless for "did anything
# merge?": 0 collapses the window to today-only, so a repo that merged nothing
# in the last few hours is judged quiescent. (Negative values never reach here —
# parse_interval_days already folds them into the default.)
_MIN_CADENCE_DAYS = 1

# owner/name only — no path segments or URL metacharacters that could redirect
# the `gh api` endpoint (mirrors ledger.py's guard).
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# A workflow file basename the API accepts as a workflow id, e.g. `ci-groom.yml`.
_WORKFLOW_FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")

# A run id is a bare positive integer — anything else came from a junk payload.
_RUN_ID_RE = re.compile(r"^[0-9]+$")

# Bound each `gh api` call so a stalled connection can't hang the gate until the
# coarse job timeout (mirrors ledger.py).
_FETCH_TIMEOUT_SECONDS = 30

# How many recent runs to scan back for the last real groom before giving up
# (fail-open). Far more than any sane interval's worth of daily skip-ticks.
_MAX_RUNS_SCANNED = 100

# How many attempts of a single run to read back before giving up (fail-open).
# Only reached for a run somebody re-ran by hand, which is already rare; the cap
# keeps a pathological re-run count from turning this cheap gate into a storm of
# API calls. See `audited_run_anchor`.
_MAX_ATTEMPTS_SCANNED = 5


def parse_interval_days(raw, default: float = _DEFAULT_INTERVAL_DAYS):
    """Parse the GROOM_INTERVAL_DAYS value; fall back to the weekly default.

    Unset/blank/negative/non-numeric all fall back to `default` (7 = weekly) so a
    misconfigured variable degrades to today's behavior rather than disabling the
    gate. A value of exactly 0 is honored (0 = no throttle, run every tick).
    `inf`/`nan` parse as valid floats but would silently wedge the gate (a NaN
    threshold makes every `>=` comparison false, so it never runs again) — reject
    them the same as any other garbage input.
    """
    if raw is None:
        return default
    text = str(raw).strip()
    if text == "":
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return value


def normalize_cadence_days(raw, default: float = _DEFAULT_INTERVAL_DAYS) -> int:
    """Whole-day volume-gate window derived from the SAME raw value as the interval.

    Both gates are wired to `GROOM_INTERVAL_DAYS` in the caller, so they must
    agree on what a given value means. Feeding the raw variable straight to
    `date -d` drifts from `parse_interval_days` in two reachable ways:

    - `-3` -> `date -d '-3 days ago'` is a FUTURE cutoff that matches no merged
      PR, so the volume gate skips EVERY run — silently disabling groom, where
      the interval gate had degraded safely to the weekly default.
    - `0` (a legitimate "no throttle" for the interval gate) collapses the
      merge window to today-only, so most ticks are judged quiescent.

    Routing through `parse_interval_days` first, then flooring at one whole day,
    keeps the two gates on one normalization. The floor takes nothing away: to
    run with no merge-activity throttle at all, set `volume_gate: false` — that
    is the input that expresses it, not a zero-width window.
    """
    return max(_MIN_CADENCE_DAYS, int(parse_interval_days(raw, default)))


def parse_iso8601_utc(ts: str) -> datetime:
    """Parse a GitHub API timestamp (e.g. `2026-07-21T09:17:30Z`) as aware UTC."""
    text = (ts or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def days_since(then_iso: str, now: datetime) -> float:
    """Fractional days between an ISO timestamp and `now` (an aware datetime)."""
    then = parse_iso8601_utc(then_iso)
    return (now - then).total_seconds() / 86400.0


def scoped_job_marker(path: str) -> str:
    """The job-name marker groom.yml appends for a run scoped to `path`.

    Kept next to the matcher that reads it, so the producing expression in
    groom.yml and the consuming comparison cannot drift apart silently
    (`test_interval.py` pins both halves against this).
    """
    return f"(scoped: {path})"


def agent_step_name() -> str:
    """The groom.yml finder step whose start proves the (billed) audit happened.

    Kept next to the matcher that reads it, so the producing `- name:` in
    groom.yml and the consuming comparison cannot drift apart silently
    (`test_interval.py` pins both halves against this).
    """
    return _AGENT_STEP_NAME


def _text(value) -> str:
    """A trimmed string for any JSON value; non-strings (and None) read as blank.

    Every field this module reads off the API is compared as text, and the whole
    history scan is meant not to raise on a junk payload (the upstream handler
    would fail open anyway, but the cheap scan should not lean on that backstop).
    Funnelling each read through here makes a `None`, number, list or dict where a
    string was expected degrade to "no evidence" instead of an AttributeError.
    """
    return value.strip() if isinstance(value, str) else ""


def _step_has_elapsed_span(step) -> bool:
    """True if the step's timestamps prove it actually ran for some time.

    The discriminator for a `cancelled` step (see _CANCELLED_STEP_CONCLUSIONS):
    cancelled mid-flight (billed) vs finalized on the way down (never reached).
    Missing, unparseable or zero-width timestamps are no evidence, not proof of
    absence — same fail-open direction as the rest of the gate.
    """
    started, completed = _text(step.get("started_at")), _text(step.get("completed_at"))
    if not started or not completed:
        return False
    try:
        return parse_iso8601_utc(completed) > parse_iso8601_utc(started)
    except ValueError:
        return False


def agent_step_started(job) -> bool:
    """True if this job's `steps[]` show the billed agent step actually started.

    Positive evidence only, and fail-OPEN on every ambiguity: a missing, empty or
    unmatched `steps[]` reads as "the agent never ran", which makes the run NOT
    count and leaves the next tick due. A duplicated audit costs one run; a
    suppressed one hides a broken caller for a full interval.
    """
    if not isinstance(job, dict):
        return False
    steps = job.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        if _text(step.get("name")) != _AGENT_STEP_NAME:
            continue
        status = _text(step.get("status")).lower()
        conclusion = _text(step.get("conclusion")).lower()
        if status in _UNSTARTED_STEP_STATUSES or conclusion in _UNSTARTED_STEP_CONCLUSIONS:
            continue
        if conclusion in _CANCELLED_STEP_CONCLUSIONS:
            # Only the timestamps say whether this cancellation caught a running
            # agent (billed) or a step that was never reached (billed nothing).
            if _step_has_elapsed_span(step):
                return True
            continue
        if not status and not conclusion:
            # A name and nothing else says nothing about whether it ran. The API
            # always sends `status`, so this is the malformed/unknown-shape case:
            # no evidence -> not audited, same direction as every other branch.
            continue
        return True
    return False


def run_audited(jobs, scope_path: str = "") -> bool:
    """True if a run's jobs show a finder for `scope_path` actually ran (billed).

    `scope_path` is the scope of the tick being decided: "" for a whole-repo
    audit, else the audited directory. A run counts only against its OWN scope —
    a scoped run must leave the next scheduled whole-repo tick DUE, and a
    whole-repo sweep is not a substitute for a scoped caller's own cadence.

    A `success` job conclusion counts on its own. Every other ending except the
    two that spent nothing by definition (`skipped`, unfinished) counts only with
    positive evidence that the agent step started (BE-4814) — the job can end
    long before it (checkout, asset load, prompt build), and those runs bill
    nothing, so treating them as a spent audit would advance the cadence clock
    and hide the breakage for a whole interval.

    Never counted: an interval-skip (its finder job is `skipped`), and — for a
    whole-repo tick — a job matched only by the bare job-id hint, whose name
    never carried the scope marker and so cannot be ATTRIBUTED to a scope at
    all. Both fall through to "no prior run", which fails open.
    """
    want = scoped_job_marker(scope_path) if scope_path else ""
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        raw_name = job.get("name") or ""
        name = raw_name.lower()
        if not any(hint in name for hint in _FINDER_JOB_HINTS):
            continue
        # Normalized exactly like the step fields below it: the API returns these
        # lowercase today, but one normalization for both halves means a casing or
        # whitespace variance can never slip past only one of the two checks.
        conclusion = _text(job.get("conclusion")).lower()
        audited = conclusion in _AUDITED_CONCLUSIONS or (
            conclusion not in _NEVER_AUDITED_CONCLUSIONS and agent_step_started(job)
        )
        if not audited:
            continue
        if want:
            # The MARKER is compared case-SENSITIVELY (against the raw job name)
            # while the surrounding prose hints stay case-insensitive. Paths on
            # the Linux runner are case-sensitive and `_COMPONENT_RE` admits both
            # cases, so `services/api` and `services/API` are two distinct scopes
            # with two distinct clocks; folding case would collapse them onto one
            # and let a run of either silently suppress the other's due tick —
            # the silent under-run this module refuses. A case MISMATCH now reads
            # as "no prior run of this scope", i.e. fail-open, the same collision
            # direction as every other branch here.
            if want in raw_name:
                return True
        elif _SCOPED_MARKER_PREFIX not in name and any(h in name for h in _DISPLAY_NAME_HINTS):
            return True
    return False


def interval_threshold(interval_days: float) -> float:
    """The elapsed-days bar a tick must clear: the interval, less jitter slack.

    The tolerance is capped at HALF the interval so a sub-daily cadence driven by
    a sub-daily base cron (e.g. an hourly caller with `interval_days: 0.25`) keeps
    a real throttle instead of collapsing to a bar of 0 = "run every tick".
    """
    return interval_days - min(_TICK_TOLERANCE_DAYS, interval_days / 2.0)


def interval_decision(interval_days: float, last_run_iso, now: datetime) -> dict:
    """Pure gate decision, given the last real run (or None) and now.

    - No prior real run -> run (first groom, fail-open).
    - `interval_days <= 0` -> run (throttle disabled).
    - `days_since(last) >= interval_threshold(interval_days)` -> run; else skip
      cheaply. The bar is the interval less a half-tick of scheduler-jitter slack
      so late-firing cron can't drift the cadence a day later every cycle
      (see _TICK_TOLERANCE_DAYS).
    """
    if interval_days <= 0:
        return {"should_run": True, "reason": f"interval_days={interval_days:g} (<=0) — throttle disabled, running.",
                "days_since": None, "last_run_at": last_run_iso}
    if not last_run_iso:
        return {"should_run": True, "reason": "no prior groom run found in history — running (fail-open).",
                "days_since": None, "last_run_at": None}
    elapsed = days_since(last_run_iso, now)
    threshold = interval_threshold(interval_days)
    if elapsed >= threshold:
        return {"should_run": True,
                "reason": (f"{elapsed:.2f} days since last run >= {threshold:g} "
                           f"(interval {interval_days:g} less jitter slack) — running."),
                "days_since": round(elapsed, 2), "last_run_at": last_run_iso}
    return {"should_run": False,
            "reason": (f"skipped: {elapsed:.2f} days since last run < {threshold:g} "
                       f"(interval {interval_days:g} less jitter slack)."),
            "days_since": round(elapsed, 2), "last_run_at": last_run_iso}


def _api_json(args, run):
    """Run `gh api <args>` with a timeout and parse the stdout JSON."""
    try:
        result = run(
            ["gh", "api", *args],
            text=True,
            capture_output=True,
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh api timed out after {_FETCH_TIMEOUT_SECONDS}s: {args}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"gh api failed ({args}): {result.stderr.strip()}")
    text = (result.stdout or "").strip()
    if not text:
        return {}
    return json.loads(text)


def fetch_workflow_runs(repo: str, workflow_file: str, run=subprocess.run):
    """Recent runs of the caller workflow, newest first (single page)."""
    if not _REPO_RE.match(repo or ""):
        raise ValueError(f"invalid repo {repo!r}: expected owner/name")
    if not _WORKFLOW_FILE_RE.match(workflow_file or ""):
        raise ValueError(f"invalid workflow file {workflow_file!r}: expected a *.yml basename")
    payload = _api_json(
        [f"/repos/{repo}/actions/workflows/{workflow_file}/runs?per_page={_MAX_RUNS_SCANNED}"],
        run,
    )
    return payload.get("workflow_runs", []) if isinstance(payload, dict) else []


def fetch_run_jobs(repo: str, run_id, run=subprocess.run, attempt=None):
    """The jobs of one workflow run (single page).

    `attempt=None` asks the plain endpoint, which reports only the run's LATEST
    attempt; pass an attempt number to read an earlier one (see
    `audited_run_anchor`).
    """
    if not _REPO_RE.match(repo or ""):
        raise ValueError(f"invalid repo {repo!r}: expected owner/name")
    # Guarded like `repo`: a junk run payload missing `id` would otherwise
    # interpolate `None` into the path and spend a doomed round-trip to find out.
    if not _RUN_ID_RE.match(str(run_id or "")):
        raise ValueError(f"invalid run id {run_id!r}: expected a positive integer")
    base = f"/repos/{repo}/actions/runs/{run_id}"
    if attempt is not None:
        base = f"{base}/attempts/{int(attempt)}"
    payload = _api_json([f"{base}/jobs?per_page=100"], run)
    return payload.get("jobs", []) if isinstance(payload, dict) else []


def finder_job_started_at(jobs):
    """`started_at` of the finder job in one attempt's jobs payload, or None.

    Keeps scanning past a matching job that carries no usable timestamp rather
    than giving up on the first hint match — there is one finder job today, but a
    payload that listed a second must not lose the answer to ordering.
    """
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        if not any(hint in _text(job.get("name")).lower() for hint in _FINDER_JOB_HINTS):
            continue
        started = _text(job.get("started_at"))
        if started:
            return started
    return None


def audited_run_anchor(repo, wf_run, scope_path="", run=subprocess.run):
    """The timestamp to anchor the cadence clock on for a run, or None if unaudited.

    `run_audited` for `scope_path`, counting a spent audit on ANY of its
    attempts. The plain jobs endpoint returns only the latest attempt, so a
    re-run that dies before the agent would erase the evidence of an earlier
    attempt that DID reach it, and the already-paid audit would repeat on the
    next tick — the same forgotten-spend bug as a pre-agent failure, one level
    up. When the latest attempt shows nothing and the run has earlier ones,
    walk them back.

    Costs nothing in the normal case: `run_attempt` is 1 for every run nobody
    re-ran by hand, and the loop is skipped entirely.
    """
    run_id = wf_run.get("id")
    latest_jobs = fetch_run_jobs(repo, run_id, run=run)
    if run_audited(latest_jobs, scope_path):
        # `run_started_at` tracks the LATEST attempt, which is the audited one
        # here, so it is the right anchor — but share the earlier-attempt branch's
        # fallbacks rather than dropping the whole run when it is missing.
        return (_text(wf_run.get("run_started_at"))
                or finder_job_started_at(latest_jobs)
                or _text(wf_run.get("created_at"))
                or None)
    try:
        attempts = int(wf_run.get("run_attempt") or 1)
    except (TypeError, ValueError):
        attempts = 1
    # Newest-first, excluding the latest (already read above), and bounded so a
    # pathologically re-run entry can't turn the cheap gate into a request storm.
    # The window must slide with `attempts` — capping the START at
    # _MAX_ATTEMPTS_SCANNED would scan the OLDEST attempts of a heavily re-run
    # entry and miss a billed audit sitting on a recent one.
    for attempt in range(attempts - 1, max(0, attempts - _MAX_ATTEMPTS_SCANNED), -1):
        jobs = fetch_run_jobs(repo, run_id, run=run, attempt=attempt)
        if run_audited(jobs, scope_path):
            # `run_started_at` tracks the LATEST attempt, so anchoring on it here
            # would date a week-old paid audit to today's pre-agent re-run and
            # suppress the next full interval — the fail-CLOSED direction this
            # gate exists to avoid. So it is NOT in this chain at all, not even as
            # a last resort: prefer the audited attempt's own finder-job start,
            # then the run's creation (an older anchor means more elapsed days,
            # i.e. fail-open), and otherwise give up on this run entirely — the
            # scan moves to an older one, which is again the fail-open direction.
            return finder_job_started_at(jobs) or _text(wf_run.get("created_at")) or None
    return None


def find_last_audited_run_at(repo, workflow_file, current_run_id, scope_path="", run=subprocess.run):
    """`run_started_at` of the most recent completed run that ran the finder
    FOR `scope_path`.

    Walks the caller workflow's runs newest-first, skips the current run and any
    still-in-progress run, and returns the first whose finder job actually ran
    for this scope. Returns None if none is found within the scanned window
    (-> fail-open run).

    The anchor is the run's `run_started_at`, except where only an EARLIER attempt
    supplied the evidence — see `audited_run_anchor`.
    """
    current = str(current_run_id) if current_run_id is not None else None
    for wf_run in fetch_workflow_runs(repo, workflow_file, run=run):
        if not isinstance(wf_run, dict):
            continue
        # One junk entry skips that run rather than aborting the scan (which
        # would fail open on the whole decision and forget real history).
        if not _RUN_ID_RE.match(str(wf_run.get("id") or "")):
            continue
        if current is not None and str(wf_run.get("id")) == current:
            continue
        if wf_run.get("status") != "completed":
            continue
        anchor = audited_run_anchor(repo, wf_run, scope_path, run=run)
        if anchor:
            return anchor
    return None


def evaluate(repo, workflow_file, current_run_id, interval_days, event_name, now,
             scope_path="", run=subprocess.run) -> dict:
    """Full gate decision, folding dispatch bypass + fail-open around the pure logic."""
    if event_name == "workflow_dispatch":
        return {"should_run": True, "reason": "workflow_dispatch — interval gate bypassed (manual override).",
                "interval_days": interval_days, "days_since": None, "last_run_at": None}
    try:
        last_run_iso = find_last_audited_run_at(repo, workflow_file, current_run_id, scope_path, run=run)
    except Exception as exc:  # noqa: BLE001 — any failure to read history must fail OPEN, never skip a due groom.
        return {"should_run": True, "reason": f"could not read run history ({exc}) — running (fail-open).",
                "interval_days": interval_days, "days_since": None, "last_run_at": None}
    decision = interval_decision(interval_days, last_run_iso, now)
    decision["interval_days"] = interval_days
    return decision


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # Second, tiny mode — no API calls, no other flags required:
    #     python3 interval.py --normalize-cadence "$CADENCE"   # -> e.g. `7`
    # The volume gate shells out to this so BOTH gates derive their window from
    # this one parser instead of the volume gate feeding the raw Actions
    # variable straight to `date -d` (see normalize_cadence_days).
    if argv and argv[0] == "--normalize-cadence":
        print(normalize_cadence_days(argv[1] if len(argv) > 1 else ""))
        return 0

    parser = argparse.ArgumentParser(description="Groom runtime cadence gate (GROOM_INTERVAL_DAYS).")
    parser.add_argument("--repo", required=True, help="owner/name of the target repo")
    parser.add_argument("--workflow-file", required=True, help="caller workflow basename, e.g. ci-groom.yml")
    parser.add_argument("--current-run-id", required=True, help="this run's id, to exclude it from history")
    parser.add_argument("--interval-days", default="", help="raw GROOM_INTERVAL_DAYS value (blank -> default 7)")
    parser.add_argument("--event-name", default="schedule", help="github.event_name (workflow_dispatch bypasses)")
    parser.add_argument("--path", default="",
                        help="scope this tick would audit ('' = whole repo); the clock is per-scope")
    parser.add_argument("--now", default=None, help="override 'now' as an ISO-8601 UTC timestamp (for testing)")
    parser.add_argument("--out", help="write the decision JSON here (also printed to stdout)")
    args = parser.parse_args(argv)

    interval_days = parse_interval_days(args.interval_days)
    now = parse_iso8601_utc(args.now) if args.now else datetime.now(timezone.utc)

    try:
        decision = evaluate(
            args.repo, args.workflow_file, args.current_run_id,
            interval_days, args.event_name, now, (args.path or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001 — belt-and-suspenders: an unexpected bug fails OPEN.
        decision = {"should_run": True, "reason": f"gate error ({exc}) — running (fail-open).",
                    "interval_days": interval_days, "days_since": None, "last_run_at": None}

    payload = json.dumps(decision, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
    print(payload)
    print(decision["reason"], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
