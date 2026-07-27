#!/usr/bin/env python3
"""Tests for the groom runtime cadence gate (BE-4004).

Core properties:
- A tick within GROOM_INTERVAL_DAYS of the last REAL groom run no-ops (skips);
  a tick at/after the interval runs.
- The interval-skip ticks in between do NOT reset the clock (only a run whose
  finder actually ran counts).
- `workflow_dispatch` always runs, regardless of the interval.
- The gate is fail-open: no history / an API error runs rather than skips.
- The volume gate's window normalizes through the SAME parser (blank/garbage/
  negative -> 7, floored at 1 whole day), so the two gates can't drift apart.

The pure logic runs with no network; the history I/O is exercised via a stubbed
`gh` subprocess.

Run: python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
"""

import contextlib
import importlib.util
import io
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

    def test_non_finite_defaults_to_weekly(self):
        # inf/nan parse as valid floats but would wedge the gate: a NaN
        # threshold makes every `>=` comparison False, so it silently never
        # runs again. Reject them like any other garbage input.
        for raw in ("inf", "-inf", "nan", "Infinity"):
            self.assertEqual(interval.parse_interval_days(raw), 7.0, raw)

    def test_numeric_values(self):
        self.assertEqual(interval.parse_interval_days("3"), 3.0)
        self.assertEqual(interval.parse_interval_days("1.5"), 1.5)
        self.assertEqual(interval.parse_interval_days("0"), 0.0)


class NormalizeCadenceDaysTest(unittest.TestCase):
    def test_unset_blank_garbage_negative_default_to_weekly(self):
        # Same degradation as the interval gate — the two share one knob.
        for raw in (None, "", "   ", "not-a-number", "-3"):
            self.assertEqual(interval.normalize_cadence_days(raw), 7, raw)

    def test_non_finite_default_to_weekly(self):
        # Would otherwise raise in the int() cast (ValueError for nan,
        # OverflowError for inf) instead of degrading like other bad inputs.
        for raw in ("inf", "-inf", "nan"):
            self.assertEqual(interval.normalize_cadence_days(raw), 7, raw)

    def test_zero_and_fractions_floor_to_one_whole_day(self):
        # 0 legitimately disables the interval throttle, but a 0-day merge
        # window would judge almost every repo quiescent — floor at 1.
        self.assertEqual(interval.normalize_cadence_days("0"), 1)
        self.assertEqual(interval.normalize_cadence_days("0.5"), 1)
        self.assertEqual(interval.normalize_cadence_days("1.9"), 1)

    def test_numeric_values_truncate_to_whole_days(self):
        self.assertEqual(interval.normalize_cadence_days("3"), 3)
        self.assertEqual(interval.normalize_cadence_days("7"), 7)
        self.assertEqual(interval.normalize_cadence_days("14.7"), 14)

    def test_cli_mode_prints_normalized_value_without_other_flags(self):
        # The volume gate shells out as `interval.py --normalize-cadence "$X"`,
        # with none of the gate's required flags — it must not error out.
        for raw, want in (("-3", "7"), ("0", "1"), ("3", "3"), ("", "7")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = interval.main(["--normalize-cadence", raw])
            self.assertEqual(rc, 0, raw)
            self.assertEqual(buf.getvalue().strip(), want, raw)

    def test_cli_mode_with_missing_value_falls_back_to_default(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = interval.main(["--normalize-cadence"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "7")


class RunAuditedTest(unittest.TestCase):
    def test_finder_success_or_failure_counts(self):
        self.assertTrue(interval.run_audited([finder_job("success")]))
        self.assertTrue(interval.run_audited([finder_job("failure")]))

    def test_bare_job_id_form_is_unknown_scope_and_counts_for_nothing(self):
        # groom.yml sets a `name:` on the finder job, so the jobs API renders the
        # DISPLAY name and the `(scoped: …)` marker is readable. The bare job-id
        # form is the hypothetical where it is not — and a name with no marker is
        # not evidence of a WHOLE-REPO run, only evidence that the name never went
        # through the `name:` expression. Attributing it to whole-repo is how a
        # scoped run would silently suppress the next full sweep, so it counts for
        # no scope at all and the gate falls through to its fail-open branch.
        idform = [{"name": "groom / audit_find", "conclusion": "success"}]
        self.assertFalse(interval.run_audited(idform))
        self.assertFalse(interval.run_audited(idform, "services/api"))

    def test_skipped_or_missing_does_not_count(self):
        self.assertFalse(interval.run_audited([finder_job("skipped")]))
        self.assertFalse(interval.run_audited([finder_job("cancelled")]))
        self.assertFalse(interval.run_audited([finder_job(None)]))
        self.assertFalse(interval.run_audited([{"name": "groom / Gate", "conclusion": "success"}]))
        self.assertFalse(interval.run_audited([]))


class ScopedRunDoesNotResetCadence(unittest.TestCase):
    """THE headline assertion for BE-4757, and its symmetric twin.

    `workflow_dispatch` bypasses the interval gate by design, so a manual
    path-scoped groom REACHES the finder. If that run then counted as "the last
    real groom", the next scheduled WHOLE-REPO tick would be suppressed for a
    full GROOM_INTERVAL_DAYS — a partial audit stamping "done" over the full one.

    groom.yml renames the finder job `Audit — finder (scoped: <path>)` when
    `path` is set (the runs API does not return a run's dispatch inputs, so the
    job name is the only per-run signal both sides can see). The PATH is in the
    marker, so the clock is per-scope in BOTH directions: the scoped run is
    invisible to a whole-repo tick, and a permanently scoped caller still finds
    its own prior runs instead of failing open and re-billing every tick.
    """

    def scoped_finder_job(self, conclusion="success", path="services/api"):
        return {"name": f"groom / Audit — finder {interval.scoped_job_marker(path)}",
                "conclusion": conclusion}

    def test_scoped_finder_job_is_not_a_whole_repo_groom(self):
        self.assertFalse(interval.run_audited([self.scoped_finder_job()]))
        self.assertFalse(interval.run_audited([self.scoped_finder_job("failure")]))

    def test_scoped_run_does_not_mask_a_whole_repo_run_in_the_same_list(self):
        # Belt-and-suspenders on the loop's ordering: an unscoped finder job
        # anywhere in the list still counts.
        self.assertTrue(interval.run_audited([self.scoped_finder_job(), finder_job("success")]))

    def test_a_scoped_tick_counts_its_OWN_prior_run(self):
        # The other half: without this, a caller pinning `path` permanently has
        # every finder job excluded, never finds a countable run, fails open on
        # every tick, and re-bills the audit daily regardless of interval_days.
        self.assertTrue(interval.run_audited([self.scoped_finder_job()], "services/api"))
        self.assertTrue(interval.run_audited([self.scoped_finder_job("failure")], "services/api"))

    def test_a_scoped_tick_ignores_a_DIFFERENT_scope_and_the_whole_repo_sweep(self):
        self.assertFalse(interval.run_audited([self.scoped_finder_job(path="packages/ui")], "services/api"))
        self.assertFalse(interval.run_audited([finder_job("success")], "services/api"))

    def test_scopes_differing_only_in_CASE_are_separate_clocks(self):
        # Paths are case-sensitive on the Linux runner and the path charset admits
        # both cases, so `services/api` and `services/API` are two directories and
        # two scopes. Matching the marker case-INSENSITIVELY would collapse them
        # onto one clock and let a run of either suppress the other's due tick —
        # the silent under-run this module refuses. The mismatch must instead read
        # as "no prior run of this scope", i.e. fail open.
        upper = [self.scoped_finder_job(path="services/API")]
        self.assertFalse(interval.run_audited(upper, "services/api"))
        self.assertFalse(interval.run_audited([self.scoped_finder_job(path="services/api")], "services/API"))
        # …and the exact-case match still counts, so the fix costs nothing.
        self.assertTrue(interval.run_audited(upper, "services/API"))

    def test_permanently_scoped_caller_gets_a_real_cadence(self):
        # End-to-end: a `path: services/api` caller ran its scoped audit 2 days
        # ago on a 7-day interval. The scheduled tick must SKIP — the cadence knob
        # has to work for the configuration the `path` input advertises.
        runs = [{"id": "2", "status": "completed", "run_started_at": iso(2.0)}]
        jobs = {"2": [self.scoped_finder_job()]}
        decision = interval.evaluate(
            "o/r", "ci-groom.yml", "9", 7.0, "schedule", NOW,
            "services/api", run=make_gh_stub(runs, jobs),
        )
        self.assertFalse(decision["should_run"], decision["reason"])
        self.assertEqual(decision["last_run_at"], iso(2.0))

    def test_groom_yml_produces_exactly_the_marker_this_module_matches(self):
        # The producer (a YAML expression) and the consumer (this module) live in
        # different files and can only be kept honest by pinning the literal.
        wf = os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "groom.yml")
        with open(wf, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("format(' (scoped: {0})', needs.gate.outputs.path)", text)
        self.assertEqual(interval.scoped_job_marker("services/api"), " (scoped: services/api)".strip())
        # …and the gate must actually hand the tick's scope to the gate script.
        # `--path=…`, not `--path …`: a directory named `-foo` is valid per
        # scope.py's charset and argparse would read the bare form as an option.
        self.assertIn('--event-name "$EVENT_NAME" --path="$GROOM_PATH"', text)

    def test_a_whole_repo_sweep_does_not_satisfy_a_scoped_callers_cadence(self):
        # …and the converse: the scoped unit's clock is its own, so a whole-repo
        # sweep yesterday leaves the scoped tick DUE (fail-open, no prior run).
        runs = [{"id": "2", "status": "completed", "run_started_at": iso(0.5)}]
        jobs = {"2": [finder_job("success")]}
        decision = interval.evaluate(
            "o/r", "ci-groom.yml", "9", 7.0, "schedule", NOW,
            "services/api", run=make_gh_stub(runs, jobs),
        )
        self.assertTrue(decision["should_run"], decision["reason"])

    def test_scheduled_tick_after_a_scoped_run_is_still_DUE(self):
        # End-to-end through `evaluate`: a scoped run YESTERDAY, a real whole-repo
        # groom 8 days ago, interval 7. The scheduled tick must RUN — the scoped
        # run must not have re-anchored the clock to yesterday.
        runs = [
            {"id": "3", "status": "completed", "run_started_at": iso(0.5)},   # scoped dispatch
            {"id": "2", "status": "completed", "run_started_at": iso(8.0)},   # last real groom
        ]
        jobs = {"3": [self.scoped_finder_job()], "2": [finder_job("success")]}
        decision = interval.evaluate(
            "o/r", "ci-groom.yml", "9", 7.0, "schedule", NOW, run=make_gh_stub(runs, jobs)
        )
        self.assertTrue(decision["should_run"], decision["reason"])
        self.assertEqual(decision["last_run_at"], iso(8.0))

    def test_control_an_unscoped_run_yesterday_DOES_suppress_the_tick(self):
        # The same fixture with the recent run UNSCOPED must skip — otherwise the
        # assertion above would pass for the wrong reason (a broken gate).
        runs = [
            {"id": "3", "status": "completed", "run_started_at": iso(0.5)},
            {"id": "2", "status": "completed", "run_started_at": iso(8.0)},
        ]
        jobs = {"3": [finder_job("success")], "2": [finder_job("success")]}
        decision = interval.evaluate(
            "o/r", "ci-groom.yml", "9", 7.0, "schedule", NOW, run=make_gh_stub(runs, jobs)
        )
        self.assertFalse(decision["should_run"], decision["reason"])


class IntervalThresholdTest(unittest.TestCase):
    def test_full_day_intervals_lose_a_half_tick(self):
        self.assertEqual(interval.interval_threshold(7.0), 6.5)
        self.assertEqual(interval.interval_threshold(3.0), 2.5)
        self.assertEqual(interval.interval_threshold(1.0), 0.5)

    def test_sub_daily_intervals_cap_the_slack_at_half(self):
        # A 0.5-day slack would zero the bar (= run every tick) for a sub-daily
        # cadence on a sub-daily base cron; keep a proportional throttle instead.
        self.assertEqual(interval.interval_threshold(0.5), 0.25)
        self.assertEqual(interval.interval_threshold(0.25), 0.125)


class IntervalDecisionTest(unittest.TestCase):
    def test_within_interval_skips(self):
        d = interval.interval_decision(7.0, iso(3), NOW)
        self.assertFalse(d["should_run"])
        self.assertIn("skipped", d["reason"])

    def test_at_or_after_interval_runs(self):
        self.assertTrue(interval.interval_decision(7.0, iso(7), NOW)["should_run"])
        self.assertTrue(interval.interval_decision(7.0, iso(9.5), NOW)["should_run"])

    def test_cron_jitter_just_under_the_interval_still_runs(self):
        # The regression this tolerance exists for: GitHub fired the last real
        # run a few minutes late, so the due tick measures 6.99 days. Without the
        # half-tick slack this skips, the run slips a day, the clock re-anchors
        # on the later run, and the cadence drifts later every cycle.
        for days in (6.99, 6.75, 6.5):
            d = interval.interval_decision(7.0, iso(days), NOW)
            self.assertTrue(d["should_run"], days)

    def test_the_previous_daily_tick_still_skips(self):
        # The slack must not pull the run a whole tick early: the tick one day
        # before the due one sits at ~6.0 days, well under the 6.5 bar.
        for days in (6.0, 6.25, 6.49):
            d = interval.interval_decision(7.0, iso(days), NOW)
            self.assertFalse(d["should_run"], days)

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
