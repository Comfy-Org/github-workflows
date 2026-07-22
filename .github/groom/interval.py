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
finder (its `Audit — finder` job ran, i.e. was not `skipped` by this very gate),
so the interval-skip ticks in between never reset the clock. Run history is
durable across the stateless CI runs and readable with only `actions: read`.

The gate is **fail-open**, matching the volume gate: any error deriving the last
run (API hiccup, unparseable timestamp, no history) RUNS the audit rather than
silently skipping a due groom. `workflow_dispatch` **always** bypasses the gate —
a manual run is an explicit override.

The pure decision logic (`run_audited`, `days_since`, `interval_decision`) is
separated from the thin `gh` I/O shell (`fetch_workflow_runs`, `fetch_run_jobs`)
so it is fully unit-testable with no network.

CLI (what the groom gate job calls):

    python3 .github/groom/interval.py \
        --repo owner/name --workflow-file ci-groom.yml \
        --current-run-id 123 --interval-days 7 --event-name schedule

Prints a `{should_run, reason, interval_days, days_since, last_run_at}` decision
JSON to stdout; the gate step reads `.should_run`. Always exits 0 (the decision
is carried in the JSON, and any failure is folded into a fail-open `should_run`).
"""

import argparse
import json
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

# A finder job that reached success OR failure spent the (billed) audit, so both
# count as a real run: counting a failure keeps a run that spent money but died
# at a later step (e.g. filing) from re-spending on the very next daily tick.
# `skipped` (the interval-skip case), `cancelled`, and a null conclusion do not.
_AUDITED_CONCLUSIONS = {"success", "failure"}

# Default cadence when GROOM_INTERVAL_DAYS is unset/blank/garbage — 7 days keeps
# the documented weekly behavior (AC: unset variable stays weekly, matching today).
_DEFAULT_INTERVAL_DAYS = 7.0

# owner/name only — no path segments or URL metacharacters that could redirect
# the `gh api` endpoint (mirrors ledger.py's guard).
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# A workflow file basename the API accepts as a workflow id, e.g. `ci-groom.yml`.
_WORKFLOW_FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")

# Bound each `gh api` call so a stalled connection can't hang the gate until the
# coarse job timeout (mirrors ledger.py).
_FETCH_TIMEOUT_SECONDS = 30

# How many recent runs to scan back for the last real groom before giving up
# (fail-open). Far more than any sane interval's worth of daily skip-ticks.
_MAX_RUNS_SCANNED = 100


def parse_interval_days(raw, default: float = _DEFAULT_INTERVAL_DAYS):
    """Parse the GROOM_INTERVAL_DAYS value; fall back to the weekly default.

    Unset/blank/negative/non-numeric all fall back to `default` (7 = weekly) so a
    misconfigured variable degrades to today's behavior rather than disabling the
    gate. A value of exactly 0 is honored (0 = no throttle, run every tick).
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
    if value < 0:
        return default
    return value


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


def run_audited(jobs) -> bool:
    """True if a run's jobs show the finder actually ran (not an interval-skip)."""
    for job in jobs or []:
        name = (job.get("name") or "").lower()
        if any(hint in name for hint in _FINDER_JOB_HINTS) and job.get("conclusion") in _AUDITED_CONCLUSIONS:
            return True
    return False


def interval_decision(interval_days: float, last_run_iso, now: datetime) -> dict:
    """Pure gate decision, given the last real run (or None) and now.

    - No prior real run -> run (first groom, fail-open).
    - `interval_days <= 0` -> run (throttle disabled).
    - `days_since(last) >= interval_days` -> run; else skip cheaply.
    """
    if interval_days <= 0:
        return {"should_run": True, "reason": f"interval_days={interval_days:g} (<=0) — throttle disabled, running.",
                "days_since": None, "last_run_at": last_run_iso}
    if not last_run_iso:
        return {"should_run": True, "reason": "no prior groom run found in history — running (fail-open).",
                "days_since": None, "last_run_at": None}
    elapsed = days_since(last_run_iso, now)
    if elapsed >= interval_days:
        return {"should_run": True,
                "reason": f"{elapsed:.2f} days since last run >= interval {interval_days:g} — running.",
                "days_since": round(elapsed, 2), "last_run_at": last_run_iso}
    return {"should_run": False,
            "reason": f"skipped: {elapsed:.2f} days since last run < interval {interval_days:g}.",
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


def fetch_run_jobs(repo: str, run_id, run=subprocess.run):
    """The jobs of one workflow run (single page)."""
    if not _REPO_RE.match(repo or ""):
        raise ValueError(f"invalid repo {repo!r}: expected owner/name")
    payload = _api_json([f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"], run)
    return payload.get("jobs", []) if isinstance(payload, dict) else []


def find_last_audited_run_at(repo, workflow_file, current_run_id, run=subprocess.run):
    """`run_started_at` of the most recent completed run that ran the finder.

    Walks the caller workflow's runs newest-first, skips the current run and any
    still-in-progress run, and returns the first whose finder job actually ran.
    Returns None if none is found within the scanned window (-> fail-open run).
    """
    current = str(current_run_id) if current_run_id is not None else None
    for wf_run in fetch_workflow_runs(repo, workflow_file, run=run):
        if current is not None and str(wf_run.get("id")) == current:
            continue
        if wf_run.get("status") != "completed":
            continue
        jobs = fetch_run_jobs(repo, wf_run.get("id"), run=run)
        if run_audited(jobs):
            return wf_run.get("run_started_at")
    return None


def evaluate(repo, workflow_file, current_run_id, interval_days, event_name, now, run=subprocess.run) -> dict:
    """Full gate decision, folding dispatch bypass + fail-open around the pure logic."""
    if event_name == "workflow_dispatch":
        return {"should_run": True, "reason": "workflow_dispatch — interval gate bypassed (manual override).",
                "interval_days": interval_days, "days_since": None, "last_run_at": None}
    try:
        last_run_iso = find_last_audited_run_at(repo, workflow_file, current_run_id, run=run)
    except Exception as exc:  # noqa: BLE001 — any failure to read history must fail OPEN, never skip a due groom.
        return {"should_run": True, "reason": f"could not read run history ({exc}) — running (fail-open).",
                "interval_days": interval_days, "days_since": None, "last_run_at": None}
    decision = interval_decision(interval_days, last_run_iso, now)
    decision["interval_days"] = interval_days
    return decision


def main(argv=None):
    parser = argparse.ArgumentParser(description="Groom runtime cadence gate (GROOM_INTERVAL_DAYS).")
    parser.add_argument("--repo", required=True, help="owner/name of the target repo")
    parser.add_argument("--workflow-file", required=True, help="caller workflow basename, e.g. ci-groom.yml")
    parser.add_argument("--current-run-id", required=True, help="this run's id, to exclude it from history")
    parser.add_argument("--interval-days", default="", help="raw GROOM_INTERVAL_DAYS value (blank -> default 7)")
    parser.add_argument("--event-name", default="schedule", help="github.event_name (workflow_dispatch bypasses)")
    parser.add_argument("--now", default=None, help="override 'now' as an ISO-8601 UTC timestamp (for testing)")
    parser.add_argument("--out", help="write the decision JSON here (also printed to stdout)")
    args = parser.parse_args(argv)

    interval_days = parse_interval_days(args.interval_days)
    now = parse_iso8601_utc(args.now) if args.now else datetime.now(timezone.utc)

    try:
        decision = evaluate(
            args.repo, args.workflow_file, args.current_run_id,
            interval_days, args.event_name, now,
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
