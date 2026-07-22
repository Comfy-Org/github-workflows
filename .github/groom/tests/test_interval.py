#!/usr/bin/env python3
"""Tests for the groom runtime cadence gate (BE-4004).

Core properties:
- A tick within GROOM_INTERVAL_DAYS of the last REAL groom run no-ops (skips);
  a tick at/after the interval runs.
- The interval-skip ticks in between do NOT reset the clock (only a run whose
  finder actually ran counts).
- `workflow_dispatch` always runs, regardless of the interval.
- The gate is fail-open: no history / an API error runs rather than skips.

The pure logic runs with no network; the history I/O is exercised via a stubbed
`gh` subprocess.

Run: python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
"""

import importlib.util
import json
import os
import unittest
from datetime import datetime, timezone

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "interval.py")
_spec = importlib.util.spec_from_file_location("groom_interval", _MODULE_PATH)
interval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(interval)


NOW = datetime(2026, 7, 21, 9, 17, 0, tzinfo=timezone.utc)


def iso(days_ago: float) -> str:
    """An ISO-8601 UTC timestamp `days_ago` days before NOW."""
    ts = NOW.timestamp() - days_ago * 86400.0
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Result:
    """A minimal subprocess.CompletedProcess stand-in."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def make_gh_stub(runs, jobs_by_run):
    """A `gh api` stub: routes /runs vs /runs/<id>/jobs by URL."""

    def _run(cmd, **kwargs):
        url = cmd[-1]
        if "/jobs" in url:
            run_id = url.split("/actions/runs/")[1].split("/jobs")[0]
            return Result(stdout=json.dumps({"jobs": jobs_by_run.get(run_id, [])}))
        return Result(stdout=json.dumps({"workflow_runs": runs}))

    return _run


def finder_job(conclusion="success"):
    return {"name": "groom / Audit — finder", "conclusion": conclusion}


class ParseIntervalDaysTest(unittest.TestCase):
    def test_unset_blank_garbage_default_to_weekly(self):
        for raw in (None, "", "   ", "not-a-number", "-3"):
            self.assertEqual(interval.parse_interval_days(raw), 7.0, raw)

    def test_numeric_values(self):
        self.assertEqual(interval.parse_interval_days("3"), 3.0)
        self.assertEqual(interval.parse_interval_days("1.5"), 1.5)
        self.assertEqual(interval.parse_interval_days("0"), 0.0)


class RunAuditedTest(unittest.TestCase):
    def test_finder_success_or_failure_counts(self):
        self.assertTrue(interval.run_audited([finder_job("success")]))
        self.assertTrue(interval.run_audited([finder_job("failure")]))

    def test_matches_job_id_form(self):
        # Robust to GitHub rendering the nested job by id rather than display name.
        self.assertTrue(interval.run_audited([{"name": "groom / audit_find", "conclusion": "success"}]))

    def test_skipped_or_missing_does_not_count(self):
        self.assertFalse(interval.run_audited([finder_job("skipped")]))
        self.assertFalse(interval.run_audited([finder_job("cancelled")]))
        self.assertFalse(interval.run_audited([finder_job(None)]))
        self.assertFalse(interval.run_audited([{"name": "groom / Gate", "conclusion": "success"}]))
        self.assertFalse(interval.run_audited([]))


class IntervalDecisionTest(unittest.TestCase):
    def test_within_interval_skips(self):
        d = interval.interval_decision(7.0, iso(3), NOW)
        self.assertFalse(d["should_run"])
        self.assertIn("skipped", d["reason"])

    def test_at_or_after_interval_runs(self):
        self.assertTrue(interval.interval_decision(7.0, iso(7), NOW)["should_run"])
        self.assertTrue(interval.interval_decision(7.0, iso(9.5), NOW)["should_run"])

    def test_no_prior_run_runs(self):
        self.assertTrue(interval.interval_decision(7.0, None, NOW)["should_run"])

    def test_zero_interval_disables_throttle(self):
        self.assertTrue(interval.interval_decision(0.0, iso(0.1), NOW)["should_run"])


class EvaluateTest(unittest.TestCase):
    def test_dispatch_always_runs_even_within_interval(self):
        # A recent real run exists, but a manual dispatch bypasses the gate.
        stub = make_gh_stub([{"id": 9, "status": "completed", "run_started_at": iso(0.5)}],
                            {"9": [finder_job("success")]})
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "workflow_dispatch", NOW, run=stub)
        self.assertTrue(d["should_run"])
        self.assertIn("dispatch", d["reason"])

    def test_skips_when_last_real_run_is_recent(self):
        runs = [
            {"id": 100, "status": "in_progress", "run_started_at": iso(0)},   # current, excluded
            {"id": 99, "status": "completed", "run_started_at": iso(1)},       # skip-tick
            {"id": 98, "status": "completed", "run_started_at": iso(2)},       # skip-tick
            {"id": 90, "status": "completed", "run_started_at": iso(3)},       # last REAL run
        ]
        jobs = {
            "99": [finder_job("skipped")],
            "98": [finder_job("skipped")],
            "90": [finder_job("success")],
        }
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=make_gh_stub(runs, jobs))
        # Last real run was 3 days ago; skip-ticks at 1 and 2 days must NOT reset it.
        self.assertFalse(d["should_run"])
        self.assertEqual(d["last_run_at"], iso(3))

    def test_runs_when_last_real_run_is_old(self):
        runs = [
            {"id": 100, "status": "in_progress", "run_started_at": iso(0)},
            {"id": 99, "status": "completed", "run_started_at": iso(1)},       # skip
            {"id": 90, "status": "completed", "run_started_at": iso(8)},       # last real, 8d ago
        ]
        jobs = {"99": [finder_job("skipped")], "90": [finder_job("success")]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=make_gh_stub(runs, jobs))
        self.assertTrue(d["should_run"])

    def test_no_history_runs(self):
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=make_gh_stub([], {}))
        self.assertTrue(d["should_run"])

    def test_api_error_fails_open(self):
        def boom(cmd, **kwargs):
            return Result(stdout="", returncode=1, stderr="boom")

        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=boom)
        self.assertTrue(d["should_run"])
        self.assertIn("fail-open", d["reason"])

    def test_only_skip_ticks_in_history_runs(self):
        # Every prior run was itself an interval-skip -> no real run found -> run.
        runs = [
            {"id": 99, "status": "completed", "run_started_at": iso(1)},
            {"id": 98, "status": "completed", "run_started_at": iso(2)},
        ]
        jobs = {"99": [finder_job("skipped")], "98": [finder_job("skipped")]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=make_gh_stub(runs, jobs))
        self.assertTrue(d["should_run"])


class FetchValidationTest(unittest.TestCase):
    def test_bad_repo_rejected(self):
        with self.assertRaises(ValueError):
            interval.fetch_workflow_runs("not-a-repo", "ci-groom.yml", run=make_gh_stub([], {}))

    def test_bad_workflow_file_rejected(self):
        with self.assertRaises(ValueError):
            interval.fetch_workflow_runs("o/r", "ci-groom", run=make_gh_stub([], {}))


if __name__ == "__main__":
    unittest.main()
