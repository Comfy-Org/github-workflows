#!/usr/bin/env python3
"""Tests for the groom runtime cadence gate (BE-4004).

Core properties:
- A tick within GROOM_INTERVAL_DAYS of the last REAL groom run no-ops (skips);
  a tick at/after the interval runs.
- The interval-skip ticks in between do NOT reset the clock (only a run whose
  finder actually ran counts).
- A FAILED finder job counts only when its billed agent step (`Run finder`) ran,
  so a flaky pre-agent step (checkout, `npm install`) can't burn a whole cycle
  (BE-4809) — with a fail-SAFE fallback when the payload carries no step data,
  and judged across ALL of a run's attempts so a re-run can't erase the spend.
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
    """A `gh api` stub: routes /runs vs /runs/<id>/jobs by URL.

    Paginates the jobs response the way the real endpoint does, so the walk in
    `fetch_run_jobs` is exercised (and can't silently spin on page 2).
    """

    def _run(cmd, **kwargs):
        url = cmd[-1]
        if "/jobs" in url:
            run_id = url.split("/actions/runs/")[1].split("/jobs")[0]
            all_jobs = jobs_by_run.get(run_id, [])
            page = int(re.search(r"[?&]page=(\d+)", url).group(1)) if "page=" in url else 1
            size = interval._JOBS_PAGE_SIZE
            return Result(stdout=json.dumps({"jobs": all_jobs[(page - 1) * size:page * size]}))
        return Result(stdout=json.dumps({"workflow_runs": runs}))

    return _run


def finder_job(conclusion="success", steps=None):
    """A finder job as the jobs API renders it.

    `steps=None` omits the array entirely — the fail-safe shape every pre-BE-4809
    fixture (and any truncated/changed payload) has, which still counts.
    """
    job = {"name": "groom / Audit — finder", "conclusion": conclusion}
    if steps is not None:
        job["steps"] = steps
    return job


def step(name, conclusion="success"):
    return {"name": name, "status": "completed", "conclusion": conclusion, "number": 1}


# The steps the finder job runs BEFORE the billed agent (see groom.yml's
# `audit_find`) — any of these can flake and fail the job having spent nothing.
PRE_AGENT_STEPS = [
    step("Set up job"),
    step("Checkout target repo (clean default branch)"),
    step("Load groom assets (briefs)"),
    step("Build finder prompt"),
    step("Install Claude Code"),
    step("Lock the clone read-only"),
]


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

    def test_matches_job_id_form(self):
        # Robust to GitHub rendering the nested job by id rather than display name.
        self.assertTrue(interval.run_audited([{"name": "groom / audit_find", "conclusion": "success"}]))

    def test_skipped_or_missing_does_not_count(self):
        self.assertFalse(interval.run_audited([finder_job("skipped")]))
        self.assertFalse(interval.run_audited([finder_job("cancelled")]))
        self.assertFalse(interval.run_audited([finder_job(None)]))
        self.assertFalse(interval.run_audited([{"name": "groom / Gate", "conclusion": "success"}]))
        self.assertFalse(interval.run_audited([]))


class FailedFinderSpentTheAgentTest(unittest.TestCase):
    """A `failure` job counts only if the billed agent step actually ran (BE-4809)."""

    def test_failure_after_the_agent_ran_still_counts(self):
        # The money was spent; the job died at a later step (filing, artifact
        # upload). Re-running on the very next daily tick would double-bill.
        for agent_conclusion in ("success", "failure", "cancelled"):
            job = finder_job("failure", PRE_AGENT_STEPS + [
                step("Run finder", agent_conclusion),
                step("Assert candidates", "failure"),
            ])
            self.assertTrue(interval.run_audited([job]), agent_conclusion)

    def test_failure_before_the_agent_does_not_count(self):
        # A flaky `npm install` (or either checkout) fails the job having spent
        # nothing — the tick must stay due rather than eat a whole interval.
        truncated = PRE_AGENT_STEPS[:-2] + [step("Install Claude Code", "failure")]
        self.assertFalse(interval.run_audited([finder_job("failure", truncated)]))

    def test_failure_with_the_agent_step_skipped_does_not_count(self):
        # GitHub renders every step after a failing one as `skipped` rather than
        # omitting it, so the step being present is not evidence that it ran.
        steps = PRE_AGENT_STEPS[:-1] + [
            step("Lock the clone read-only", "failure"),
            step("Run finder", "skipped"),
        ]
        self.assertFalse(interval.run_audited([finder_job("failure", steps)]))
        self.assertFalse(interval.run_audited([finder_job("failure", PRE_AGENT_STEPS + [step("Run finder", None)])]))

    def test_failure_without_step_data_counts(self):
        # Fail-SAFE, deliberately the opposite bias to the elapsed-time logic: no
        # usable `steps` (API shape change, truncated payload) keeps today's
        # behavior, because re-billing a genuinely-spent audit is the expensive
        # direction.
        self.assertTrue(interval.run_audited([finder_job("failure")]))          # key absent
        self.assertTrue(interval.run_audited([finder_job("failure", [])]))      # empty array
        self.assertTrue(interval.run_audited([{"name": "groom / Audit — finder",
                                               "conclusion": "failure", "steps": None}]))

    def test_success_is_unaffected_by_step_data(self):
        # A successful job cannot have succeeded without the agent, so the step
        # array is never consulted — including a (nonsensical) truncated one.
        self.assertTrue(interval.run_audited([finder_job("success")]))
        self.assertTrue(interval.run_audited([finder_job("success", PRE_AGENT_STEPS)]))
        self.assertTrue(interval.run_audited([finder_job("success", PRE_AGENT_STEPS + [step("Run finder")])]))

    def test_a_spent_failure_still_counts_when_an_unspent_one_precedes_it(self):
        # Job order must not decide the answer: one matching job that spent the
        # agent is enough, even behind a matching job that didn't.
        jobs = [
            finder_job("failure", [step("Install Claude Code", "failure")]),
            finder_job("failure", PRE_AGENT_STEPS + [step("Run finder")]),
        ]
        self.assertTrue(interval.run_audited(jobs))

    def test_a_neighbouring_step_is_not_mistaken_for_the_agent(self):
        # Step names come back verbatim from the YAML, so the match is EXACT: a
        # substring match would let `Rerun finder` (or a `Post Run finder`) stand
        # in for the billed step and count a failure that spent nothing.
        for decoy in ("Rerun finder", "Run finder prompt lint", "Post Run finder"):
            job = finder_job("failure", [step("Set up job"), step(decoy, "failure")])
            self.assertFalse(interval.run_audited([job]), decoy)

    def test_the_agent_step_name_is_matched_case_and_space_insensitively(self):
        for rendered in ("Run finder", "run finder", "RUN FINDER", "  Run finder  "):
            job = finder_job("failure", [step(rendered), step("Assert candidates", "failure")])
            self.assertTrue(interval.run_audited([job]), rendered)

    def test_unspent_failure_leaves_the_tick_due_end_to_end(self):
        # The behavior that matters: yesterday's run died in `npm install`, so
        # today's tick must still groom rather than wait out the interval.
        runs = [
            {"id": 100, "status": "in_progress", "run_started_at": iso(0)},
            {"id": 99, "status": "completed", "run_started_at": iso(1)},
        ]
        jobs = {"99": [finder_job("failure", [step("Install Claude Code", "failure")])]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=make_gh_stub(runs, jobs))
        self.assertTrue(d["should_run"])
        self.assertIsNone(d["last_run_at"])


# --- a deliberately small, reformat-tolerant scan of one job's step names ------
#
# Used only by the pin test below. It is NOT a YAML parser (the test job installs
# no PyYAML — see .github/workflows/test-groom-scripts.yml), so it is written to
# tolerate the benign edits a workflow file actually attracts: reindentation,
# quoting a step name, a trailing `# comment`. A rename of the step — the thing
# the pin exists to catch — still fails it loudly.
_JOB_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*):\s*(?:#.*)?$")
_STEP_NAME_RE = re.compile(r"^\s*-\s*name:\s*(\S.*?)\s*$")


def yaml_scalar(raw):
    """A YAML scalar as written -> its value: unquote, else drop an inline comment."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
        return raw[1:-1]
    return re.sub(r"\s+#.*$", "", raw).strip()


def job_step_names(lines, job_id):
    """Every `- name:` value inside `<job_id>:`, at whatever indentation it lives."""
    names, indent = [], None
    for line in lines:
        key = _JOB_KEY_RE.match(line)
        if indent is None:
            if key and key.group(2) == job_id:
                indent = len(key.group(1))
            continue
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and len(line) - len(line.lstrip()) <= indent:
            break  # dedented back out of the job block
        name = _STEP_NAME_RE.match(line)
        if name:
            names.append(yaml_scalar(name.group(1)))
    return names


class JobStepNameScanTest(unittest.TestCase):
    """The pin test's scanner survives benign reformats of groom.yml."""

    def test_tolerates_reindent_quoting_and_inline_comments(self):
        for label, body in {
            "as written": ["  audit_find:", "    steps:", "      - name: Run finder", "  next_job:"],
            "reindented": ["    audit_find:", "      steps:", "        - name: Run finder", "    next_job:"],
            "single-quoted": ["  audit_find:", "      - name: 'Run finder'", "  next_job:"],
            "double-quoted": ["  audit_find:", '      - name: "Run finder"', "  next_job:"],
            "inline comment": ["  audit_find:", "      - name: Run finder  # the billed agent", "  next_job:"],
            "job key commented": ["  audit_find:  # phase 1", "      - name: Run finder", "  next_job:"],
        }.items():
            self.assertEqual(job_step_names(body, "audit_find"), ["Run finder"], label)

    def test_stops_at_the_next_job_and_ignores_other_jobs(self):
        body = [
            "  gate:",
            "      - name: Run finder",          # a same-named step in ANOTHER job
            "  audit_find:",
            "      - name: Build finder prompt",
            "  audit_verify:",
            "      - name: Run verifier",
        ]
        self.assertEqual(job_step_names(body, "audit_find"), ["Build finder prompt"])

    def test_a_renamed_step_is_visible(self):
        body = ["  audit_find:", "      - name: Run the finder agent", "  next_job:"]
        self.assertEqual(job_step_names(body, "audit_find"), ["Run the finder agent"])


class AgentStepNamePinTest(unittest.TestCase):
    """Pin the step name this module matches to the one groom.yml produces.

    Producer (`.github/workflows/groom.yml`) and consumer (this module) live in
    different files, so a rename of the step would otherwise silently degrade the
    gate back to counting every failed job as a spent audit — no test failing.
    """

    def _audit_find_step_names(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "groom.yml")
        with open(path, encoding="utf-8") as f:
            return job_step_names(f.read().splitlines(), "audit_find")

    def test_groom_yml_names_exactly_the_agent_step_this_module_matches(self):
        names = self._audit_find_step_names()
        self.assertIn("Run finder", names, "groom.yml's audit_find job no longer has a `Run finder` step")
        matched = [n for n in names if n.strip().lower() in interval._AGENT_STEP_NAMES]
        self.assertEqual(matched, ["Run finder"],
                         f"_AGENT_STEP_NAMES must match exactly the agent step, got {matched} from {names}")

    def test_the_pre_agent_step_fixtures_are_real_groom_yml_steps(self):
        # PRE_AGENT_STEPS drives every "failed before the agent" case above; if it
        # drifts from groom.yml those tests stop describing the real job.
        names = self._audit_find_step_names()
        for s in PRE_AGENT_STEPS:
            if s["name"] == "Set up job":
                continue  # runner-generated, never in the YAML
            self.assertIn(s["name"], names)


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

    def test_bad_repo_rejected_for_jobs_too(self):
        with self.assertRaises(ValueError):
            interval.fetch_run_jobs("not-a-repo", 1, run=make_gh_stub([], {}))


class FetchRunJobsAcrossAttemptsTest(unittest.TestCase):
    """The jobs walk must see EVERY attempt, not just the latest (BE-4809)."""

    def _recording_stub(self, pages):
        urls = []

        def _run(cmd, **kwargs):
            urls.append(cmd[-1])
            return Result(stdout=json.dumps({"jobs": pages[len(urls) - 1] if len(urls) <= len(pages) else []}))

        return _run, urls

    def test_asks_for_all_attempts(self):
        # `filter=latest` (the endpoint default) hides earlier attempts, which is
        # what makes a re-run erase an already-billed agent step.
        stub, urls = self._recording_stub([[finder_job("success")]])
        interval.fetch_run_jobs("o/r", 99, run=stub)
        self.assertIn("filter=all", urls[0])

    def test_pages_until_a_short_page(self):
        full = [finder_job("success") for _ in range(interval._JOBS_PAGE_SIZE)]
        stub, urls = self._recording_stub([full, [finder_job("failure")]])
        jobs = interval.fetch_run_jobs("o/r", 99, run=stub)
        self.assertEqual(len(jobs), interval._JOBS_PAGE_SIZE + 1)
        self.assertEqual(len(urls), 2)
        self.assertIn("page=2", urls[1])

    def test_page_walk_is_capped(self):
        full = [finder_job("success") for _ in range(interval._JOBS_PAGE_SIZE)]
        stub, urls = self._recording_stub([full] * (interval._MAX_JOB_PAGES + 3))
        interval.fetch_run_jobs("o/r", 99, run=stub)
        self.assertEqual(len(urls), interval._MAX_JOB_PAGES)

    def test_an_early_attempt_that_spent_the_agent_still_counts(self):
        # Attempt 1 ran the billed agent and died at filing; a manual re-run then
        # flaked in `npm install`. The audit WAS spent — today's tick must skip,
        # not re-bill it. (`filter=all` returns both attempts' jobs.)
        runs = [
            {"id": 100, "status": "in_progress", "run_started_at": iso(0)},
            {"id": 99, "status": "completed", "run_started_at": iso(1)},
        ]
        jobs = {"99": [
            finder_job("failure", [step("Install Claude Code", "failure")]),        # attempt 2
            finder_job("failure", PRE_AGENT_STEPS + [step("Run finder"),
                                                     step("File issues", "failure")]),  # attempt 1
        ]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=make_gh_stub(runs, jobs))
        self.assertFalse(d["should_run"])
        self.assertEqual(d["last_run_at"], iso(1))


if __name__ == "__main__":
    unittest.main()
