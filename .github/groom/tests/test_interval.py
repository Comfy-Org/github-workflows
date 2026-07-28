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
import re
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


def agent_step(status="completed", conclusion="success"):
    """The finder's billed agent step, shaped as the runs-jobs API returns it."""
    return {"name": interval.agent_step_name(), "number": 6, "status": status, "conclusion": conclusion}


def pre_agent_step(name="Checkout target repo (clean default branch)", conclusion="failure"):
    return {"name": name, "number": 2, "status": "completed", "conclusion": conclusion}


def finder_job(conclusion="success", steps=None):
    """A finder job. `steps=None` omits `steps[]` entirely (the API can, too)."""
    job = {"name": "groom / Audit — finder", "conclusion": conclusion}
    if steps is not None:
        job["steps"] = steps
    return job


def billed_finder_job(conclusion="failure"):
    """A finder job that DID reach the agent — a post-agent failure, i.e. spent."""
    return finder_job(conclusion, [pre_agent_step(conclusion="success"), agent_step()])


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
    def test_finder_success_counts(self):
        self.assertTrue(interval.run_audited([finder_job("success")]))

    def test_finder_failure_counts_only_with_agent_evidence(self):
        # BE-4814: a failure that reached the agent spent the audit and counts; a
        # failure with no evidence the agent ever started does NOT (see
        # PreAgentFailureTest for the full matrix).
        self.assertTrue(interval.run_audited([billed_finder_job()]))
        self.assertFalse(interval.run_audited([finder_job("failure")]))

    def test_matches_job_id_form(self):
        # Robust to GitHub rendering the nested job by id rather than display name.
        self.assertTrue(interval.run_audited([{"name": "groom / audit_find", "conclusion": "success"}]))

    def test_skipped_or_missing_does_not_count(self):
        self.assertFalse(interval.run_audited([finder_job("skipped")]))
        self.assertFalse(interval.run_audited([finder_job("cancelled")]))
        self.assertFalse(interval.run_audited([finder_job(None)]))
        self.assertFalse(interval.run_audited([{"name": "groom / Gate", "conclusion": "success"}]))
        self.assertFalse(interval.run_audited([]))


class PreAgentFailureTest(unittest.TestCase):
    """THE headline assertion for BE-4814.

    The finder job can die long before the billed agent step — checkout, the
    asset load, the prompt build, a runner hiccup. Those runs spend nothing, so
    counting them as "this scope was audited" advances the cadence clock and
    suppresses the next `GROOM_INTERVAL_DAYS` worth of ticks: a typo'd input or a
    broken caller goes quiet for a week instead of recurring (and being noticed)
    daily. Everywhere else this gate fails OPEN; this was the one branch that
    failed closed.

    The rule is positive evidence, and every ambiguity resolves to NOT audited: a
    duplicated audit costs one run, a suppressed one hides a broken caller for a
    full interval.
    """

    def test_failure_before_the_agent_step_is_not_a_spent_audit(self):
        # The job died in checkout: the agent step is present but still `queued`.
        job = finder_job("failure", [pre_agent_step(), agent_step(status="queued", conclusion=None)])
        self.assertFalse(interval.run_audited([job]))

    def test_failure_with_the_agent_step_reported_as_skipped_is_not_spent(self):
        # The other shape GitHub uses for an unreached step: `completed`/`skipped`.
        job = finder_job("failure", [pre_agent_step(), agent_step(conclusion="skipped")])
        self.assertFalse(interval.run_audited([job]))
        cancelled = finder_job("failure", [pre_agent_step(), agent_step(conclusion="cancelled")])
        self.assertFalse(interval.run_audited([cancelled]))

    def test_failure_with_the_agent_step_truncated_out_of_steps_is_not_spent(self):
        # A job that died early can report only the steps it reached.
        job = finder_job("failure", [pre_agent_step()])
        self.assertFalse(interval.run_audited([job]))

    def test_failure_with_no_steps_at_all_falls_open(self):
        # Missing key, empty list — both read as "no evidence", i.e. re-run.
        self.assertFalse(interval.run_audited([finder_job("failure")]))
        self.assertFalse(interval.run_audited([finder_job("failure", [])]))

    def test_failure_after_the_agent_step_completed_IS_a_spent_audit(self):
        # The half that must NOT regress: a run that paid for the agent and then
        # died at a later step (the JSON assert, the artifact upload) still counts,
        # so it can't re-spend on tomorrow's tick. Both agent outcomes qualify —
        # an agent that ran and failed was still billed.
        self.assertTrue(interval.run_audited([billed_finder_job()]))
        agent_failed = finder_job("failure", [pre_agent_step(conclusion="success"),
                                              agent_step(conclusion="failure")])
        self.assertTrue(interval.run_audited([agent_failed]))
        in_progress = finder_job("failure", [agent_step(status="in_progress", conclusion=None)])
        self.assertTrue(interval.run_audited([in_progress]))

    def test_success_counts_regardless_of_the_steps_payload(self):
        # A success is trusted on the job conclusion alone — the agent step is
        # upstream of every step that could still fail, and no `if:` guards it.
        for steps in (None, [], [pre_agent_step()], [agent_step(conclusion="skipped")]):
            self.assertTrue(interval.run_audited([finder_job("success", steps)]), steps)

    def test_agent_step_is_matched_exactly_not_by_substring(self):
        # The same job carries `Build finder prompt` / `Assert finder produced
        # JSON`; neither is evidence that a token was billed.
        # `Post Run finder` is real — the runs API reports post-action steps that
        # way — and it is the one a substring match would swallow.
        for name in ("Build finder prompt", "Assert finder produced JSON",
                     "Run finder (retry)", "Post Run finder"):
            job = finder_job("failure", [{"name": name, "status": "completed", "conclusion": "failure"}])
            self.assertFalse(interval.run_audited([job]), name)

    def test_malformed_step_entries_are_no_evidence_and_do_not_crash(self):
        # A bare name with neither status nor conclusion is an unknown shape, not
        # proof the agent ran — no evidence resolves to NOT audited, like every
        # other ambiguity here. And the walk must not raise on junk entries: an
        # exception would be caught upstream and fail open anyway, but the cheap
        # history scan should not lean on that backstop.
        job = finder_job("failure", [{}, {"name": None}, {"name": interval.agent_step_name()}])
        self.assertFalse(interval.run_audited([job]))

    def test_a_pre_agent_failure_does_not_advance_the_cadence_clock(self):
        # End-to-end: yesterday's tick died in checkout (billing nothing) and the
        # last REAL groom was 8 days ago on a 7-day interval. The tick must RUN,
        # anchored on the 8-day-old run — not skip for another week.
        runs = [
            {"id": 100, "status": "in_progress", "run_started_at": iso(0)},   # current, excluded
            {"id": 99, "status": "completed", "run_started_at": iso(1)},       # died pre-agent
            {"id": 90, "status": "completed", "run_started_at": iso(8)},       # last REAL run
        ]
        jobs = {
            "99": [finder_job("failure", [pre_agent_step(), agent_step(status="queued", conclusion=None)])],
            "90": [finder_job("success")],
        }
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=make_gh_stub(runs, jobs))
        self.assertTrue(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(8))

    def test_a_post_agent_failure_still_throttles_the_next_tick(self):
        # The symmetric half: yesterday's run paid for the agent and died at the
        # JSON assert, so today's tick must still SKIP — no re-spend.
        runs = [
            {"id": 100, "status": "in_progress", "run_started_at": iso(0)},
            {"id": 99, "status": "completed", "run_started_at": iso(1)},
            {"id": 90, "status": "completed", "run_started_at": iso(8)},
        ]
        jobs = {"99": [billed_finder_job()], "90": [finder_job("success")]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=make_gh_stub(runs, jobs))
        self.assertFalse(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(1))

    def test_groom_yml_names_exactly_the_agent_step_this_module_matches(self):
        # The producer (a YAML `- name:`) and the consumer (this module) live in
        # different files and can only be kept honest by pinning the literal.
        # Scoped to the `audit_find:` job so the pin means what it claims: the
        # BILLED step is named this, not merely that the string appears somewhere.
        # (Matched as text rather than parsed — PyYAML is not stdlib and this repo
        # is stdlib-only, so a parse would add a CI dependency for a literal pin.)
        wf = os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "groom.yml")
        with open(wf, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(interval.agent_step_name(), "Run finder")
        finder_block = re.split(r"(?m)^  (?=[A-Za-z_][A-Za-z0-9_-]*:\s*$)", text)
        finder_block = [b for b in finder_block if b.startswith("audit_find:")]
        self.assertEqual(len(finder_block), 1, "could not isolate the audit_find job in groom.yml")
        # Exactly one step in that job carries the name — otherwise "did the agent
        # start?" stops having a single answer.
        self.assertEqual(finder_block[0].count(f"- name: {interval.agent_step_name()}\n"), 1)


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
