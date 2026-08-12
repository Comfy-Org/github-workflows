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


def make_gh_stub(runs, jobs_by_run, jobs_by_attempt=None):
    """A `gh api` stub: routes /runs vs /runs/<id>[/attempts/<n>]/jobs by URL.

    `jobs_by_attempt` is keyed `"<run id>/<attempt>"` and answers the per-attempt
    endpoint; `jobs_by_run` answers the plain one (the LATEST attempt), exactly as
    the real API splits them.
    """
    jobs_by_attempt = jobs_by_attempt or {}

    def _run(cmd, **kwargs):
        url = cmd[-1]
        if "/jobs" in url:
            tail = url.split("/actions/runs/")[1].split("/jobs")[0]
            if "/attempts/" in tail:
                run_id, attempt = tail.split("/attempts/")
                return Result(stdout=json.dumps({"jobs": jobs_by_attempt.get(f"{run_id}/{attempt}", [])}))
            return Result(stdout=json.dumps({"jobs": jobs_by_run.get(tail, [])}))
        return Result(stdout=json.dumps({"workflow_runs": runs}))

    return _run


def agent_step(status="completed", conclusion="success", started_at=None, completed_at=None):
    """The finder's billed agent step, shaped as the runs-jobs API returns it."""
    step = {"name": interval.agent_step_name(), "number": 6, "status": status, "conclusion": conclusion}
    if started_at is not None:
        step["started_at"] = started_at
    if completed_at is not None:
        step["completed_at"] = completed_at
    return step


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

    def scoped_finder_job(self, conclusion="success", path="services/api", steps=None):
        job = {"name": f"groom / Audit — finder {interval.scoped_job_marker(path)}",
               "conclusion": conclusion}
        if steps is not None:
            job["steps"] = steps
        return job

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
        # A failure counts too, but (BE-4814) only with evidence the agent step
        # actually started — a bare `failure` with no steps payload is a
        # pre-agent death and must NOT count, scoped or not.
        billed = [pre_agent_step(conclusion="success"), agent_step()]
        self.assertTrue(interval.run_audited([self.scoped_finder_job("failure", steps=billed)], "services/api"))
        self.assertFalse(interval.run_audited([self.scoped_finder_job("failure")], "services/api"))

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
        # A `cancelled` step with no timestamp span is likewise unreached — the
        # spanned case is the opposite verdict, see CancelledAndTimedOutTest.
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

    def test_non_dict_entries_are_skipped_rather_than_raising(self):
        # The docstring promises the walk survives junk, so feed it genuinely
        # non-dict entries — a bare None, a string, a list — not just odd dicts.
        # A real payload never looks like this; the point is that the scan cannot
        # AttributeError its way into the upstream fail-open backstop.
        junk = [None, "Run finder", ["Run finder"], 7, {"name": ["Run finder"]}]
        self.assertFalse(interval.run_audited([finder_job("failure", junk)]))
        # ...and a valid entry AFTER the junk is still reached and honored.
        self.assertTrue(interval.run_audited([finder_job("failure", [*junk, agent_step()])]))
        # Junk at the job level, and a `steps[]` that isn't even a list.
        self.assertFalse(interval.run_audited([None, "job", 7]))
        self.assertFalse(interval.run_audited([finder_job("failure", "not-a-list")]))
        self.assertFalse(interval.run_audited("not-a-list"))

    def test_job_conclusion_is_normalized_like_the_step_fields(self):
        # The job conclusion and the step fields are compared against the same
        # kind of membership set, so they must be normalized the same way — a
        # casing/whitespace variance must not slip past one check but not the other.
        self.assertTrue(interval.run_audited([finder_job(" SUCCESS ")]))
        self.assertTrue(interval.run_audited([finder_job("Failure", [agent_step(conclusion=" Success ")])]))
        self.assertFalse(interval.run_audited([finder_job("Failure", [agent_step(conclusion=" SKIPPED ")])]))

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
        # And that step must carry no `if:`. A `success` is trusted on the job
        # conclusion ALONE, so a conditionally-skipped agent inside a succeeding
        # job would count as a spent audit and suppress the cadence for a full
        # interval while billing nothing. groom.yml warns about this in a comment;
        # this is the assertion that actually holds the line.
        step = finder_block[0].split(f"- name: {interval.agent_step_name()}\n", 1)[1]
        step = step.split("\n      - name:", 1)[0]
        self.assertNotRegex(step, r"(?m)^\s+if:\s", "the pinned agent step must not be conditional")

    def test_the_gate_job_is_time_bounded(self):
        # The gate walks run history (and, for re-run entries, per-attempt job
        # payloads) at a 30s per-call timeout, so its cost is data-dependent. It
        # is the cheap job, but it still needs a hard stop like every other job
        # in the file.
        wf = os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "groom.yml")
        with open(wf, encoding="utf-8") as f:
            text = f.read()
        gate = re.split(r"(?m)^  (?=[A-Za-z_][A-Za-z0-9_-]*:\s*$)", text)
        gate = [b for b in gate if b.startswith("gate:")]
        self.assertEqual(len(gate), 1, "could not isolate the gate job in groom.yml")
        self.assertRegex(gate[0], r"(?m)^    timeout-minutes: \d+$")


class CancelledAndTimedOutTest(unittest.TestCase):
    """The most EXPENSIVE ending must count, and it is not `failure`.

    The finder job carries `timeout-minutes: 40`. An agent that hangs bills that
    whole window and then trips the job timeout, so the job ends `timed_out` (or
    `cancelled`, if someone kills the run) and GitHub stamps the in-flight agent
    step `cancelled`. Reading either level as "never started" forgets the single
    costliest audit there is and re-spends it on tomorrow's tick, every tick.

    The conclusion alone cannot tell a cancellation that caught a RUNNING agent
    from one that finalized a step never reached; the timestamps can, so an
    elapsed span is the evidence, and no span stays fail-open (not audited).
    """

    def cancelled_mid_flight(self):
        """The agent step as GitHub stamps it when a 40-minute run is killed."""
        return agent_step(conclusion="cancelled",
                          started_at="2026-07-21T09:00:00Z", completed_at="2026-07-21T09:40:00Z")

    def test_timed_out_job_with_an_in_flight_agent_is_a_spent_audit(self):
        job = finder_job("timed_out", [pre_agent_step(conclusion="success"), self.cancelled_mid_flight()])
        self.assertTrue(interval.run_audited([job]))

    def test_cancelled_job_with_an_in_flight_agent_is_a_spent_audit(self):
        for conclusion in ("cancelled", "canceled"):
            job = finder_job(conclusion, [pre_agent_step(conclusion="success"), self.cancelled_mid_flight()])
            self.assertTrue(interval.run_audited([job]), conclusion)

    def test_cancelled_job_that_died_before_the_agent_is_not_spent(self):
        # The half that must not regress with the widened conclusion set: a run
        # cancelled during checkout bills nothing, so it still may not advance the
        # clock. Both unreached shapes, under both endings.
        for conclusion in ("timed_out", "cancelled"):
            queued = finder_job(conclusion, [pre_agent_step(), agent_step(status="queued", conclusion=None)])
            self.assertFalse(interval.run_audited([queued]), conclusion)
            skipped = finder_job(conclusion, [pre_agent_step(), agent_step(conclusion="skipped")])
            self.assertFalse(interval.run_audited([skipped]), conclusion)
            self.assertFalse(interval.run_audited([finder_job(conclusion)]), conclusion)

    def test_a_cancelled_agent_step_without_a_span_stays_fail_open(self):
        # No timestamps, one timestamp, or a zero-width span (both stamped at the
        # same cancellation instant) are all "no evidence" — the unreached step
        # GitHub finalized on the way down looks exactly like that.
        instant = "2026-07-21T09:00:00Z"
        for started, completed in ((None, None), (instant, None), (None, instant),
                                   (instant, instant), ("garbage", instant)):
            step = agent_step(conclusion="cancelled", started_at=started, completed_at=completed)
            self.assertFalse(interval.run_audited([finder_job("timed_out", [step])]), (started, completed))

    def test_any_ending_that_is_not_skipped_or_unfinished_takes_the_evidence_path(self):
        # The rule is a DENYLIST, not an enumeration of endings: if the agent step
        # ran, the audit was spent no matter how the job was finally stamped. An
        # allowlist would have to name the whole API vocabulary, and anything it
        # forgot would read as "billed but never counted", i.e. re-spent daily.
        for conclusion in ("failure", "timed_out", "cancelled", "canceled",
                           "neutral", "stale", "action_required", "something_new"):
            self.assertTrue(interval.run_audited([finder_job(conclusion, [agent_step()])]), conclusion)
            self.assertFalse(interval.run_audited([finder_job(conclusion, [pre_agent_step()])]), conclusion)

    def test_the_two_endings_that_spent_nothing_never_reach_the_evidence_path(self):
        # `skipped` is this gate's own interval-skip and a null conclusion means
        # unfinished — neither can have billed, whatever the steps payload claims.
        for conclusion in (None, "", "skipped"):
            self.assertFalse(interval.run_audited([finder_job(conclusion, [agent_step()])]), conclusion)

    def test_a_timed_out_agent_does_not_re_spend_on_the_next_tick(self):
        # End-to-end, the harm this closes: yesterday's run hung and burned the
        # full 40-minute timeout. Today's tick must SKIP — the audit was paid.
        runs = [
            {"id": 100, "status": "in_progress", "run_started_at": iso(0)},
            {"id": 99, "status": "completed", "run_started_at": iso(1)},
            {"id": 90, "status": "completed", "run_started_at": iso(8)},
        ]
        jobs = {"99": [finder_job("timed_out", [pre_agent_step(conclusion="success"),
                                                self.cancelled_mid_flight()])],
                "90": [finder_job("success")]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=make_gh_stub(runs, jobs))
        self.assertFalse(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(1))


class EarlierAttemptTest(unittest.TestCase):
    """A re-run must not erase the evidence that an earlier attempt billed.

    The plain jobs endpoint reports only a run's LATEST attempt. If attempt 1
    reached the agent and attempt 2 (a manual re-run) died in checkout, reading
    only the latest attempt forgets the paid audit and re-spends it next tick —
    the same bug BE-4814 fixes at the step level, one level up.
    """

    def runs(self, attempt_count):
        return [
            {"id": 100, "status": "in_progress", "run_started_at": iso(0)},
            {"id": 99, "status": "completed", "run_started_at": iso(1), "run_attempt": attempt_count},
            {"id": 90, "status": "completed", "run_started_at": iso(8)},
        ]

    def billed_attempt(self, days_ago):
        """A billed finder job stamped with when that attempt actually ran."""
        job = billed_finder_job()
        job["started_at"] = iso(days_ago)
        return [job]

    def test_an_earlier_attempt_that_billed_still_counts(self):
        latest = {"99": [finder_job("failure", [pre_agent_step()])], "90": [finder_job("success")]}
        by_attempt = {"99/1": self.billed_attempt(1), "99/2": latest["99"]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW,
                              run=make_gh_stub(self.runs(2), latest, by_attempt))
        self.assertFalse(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(1))

    def test_the_anchor_never_falls_back_to_the_re_run_timestamp(self):
        # The fallback chain for an earlier attempt is finder-job start ->
        # `created_at` -> give up. `run_started_at` is deliberately NOT the last
        # resort: it is the re-run time, so using it would date a week-old audit
        # to today and suppress the next full interval (fail-CLOSED). With neither
        # timestamp available the run is skipped and the scan moves to an older
        # one — here the 8-day-old real run, so the tick RUNS.
        runs = self.runs(2)
        runs[1]["run_started_at"] = iso(0.1)   # today's pre-agent re-run
        latest = {"99": [finder_job("failure", [pre_agent_step()])], "90": [finder_job("success")]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW,
                              run=make_gh_stub(runs, latest, {"99/1": [billed_finder_job()]}))
        self.assertTrue(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(8))

    def test_an_audited_latest_attempt_missing_run_started_at_still_anchors(self):
        # The latest-attempt branch shares the same fallbacks instead of dropping
        # the run: `run_started_at` -> finder-job start -> `created_at`.
        runs = self.runs(1)
        del runs[1]["run_started_at"]
        runs[1]["created_at"] = iso(1)
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW,
                              run=make_gh_stub(runs, {"99": self.billed_attempt(1),
                                                      "90": [finder_job("success")]}))
        self.assertFalse(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(1))

    def test_no_attempt_that_billed_still_falls_open(self):
        # Every attempt died pre-agent: nothing was spent, so the clock stays
        # anchored on the 8-day-old real run and today's tick RUNS.
        latest = {"99": [finder_job("failure", [pre_agent_step()])], "90": [finder_job("success")]}
        by_attempt = {"99/1": latest["99"], "99/2": latest["99"]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW,
                              run=make_gh_stub(self.runs(2), latest, by_attempt))
        self.assertTrue(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(8))

    def test_single_attempt_runs_make_no_extra_api_calls(self):
        # The common case must stay exactly as cheap as before: `run_attempt` 1
        # (or absent) must never hit the per-attempt endpoint.
        seen = []
        base = make_gh_stub(self.runs(1), {"99": [finder_job("failure", [pre_agent_step()])],
                                           "90": [finder_job("success")]})

        def spy(cmd, **kwargs):
            seen.append(cmd[-1])
            return base(cmd, **kwargs)

        interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=spy)
        self.assertFalse([u for u in seen if "/attempts/" in u], seen)

    def attempts_fetched(self, runs, latest, by_attempt=None):
        """Which attempt numbers the walk actually asked for, in request order."""
        seen = []
        base = make_gh_stub(runs, latest, by_attempt or {})

        def spy(cmd, **kwargs):
            seen.append(cmd[-1])
            return base(cmd, **kwargs)

        decision = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=spy)
        return [int(u.split("/attempts/")[1].split("/")[0]) for u in seen if "/attempts/" in u], decision

    def test_the_attempt_walk_is_bounded_AND_reads_the_NEWEST_earlier_attempts(self):
        # A pathological re-run count can't turn the cheap gate into a request
        # storm — but the cap must slide the window with `run_attempt`, not pin it
        # to the bottom. Asserting only the COUNT passes either way and would mask
        # a walk that scans attempts 1..4 of a 50-attempt run: a billed audit on a
        # recent attempt (49) would go unread and be re-spent on the next tick.
        latest = {"99": [finder_job("failure", [pre_agent_step()])], "90": [finder_job("success")]}
        fetched, _ = self.attempts_fetched(self.runs(50), latest)
        self.assertEqual(fetched, [49, 48, 47, 46], fetched)
        self.assertEqual(len(fetched), interval._MAX_ATTEMPTS_SCANNED - 1, fetched)
        # Under the cap, the walk simply reaches attempt 1 — nothing is skipped.
        fetched, _ = self.attempts_fetched(self.runs(3), latest)
        self.assertEqual(fetched, [2, 1], fetched)

    def test_a_billed_audit_on_a_recent_attempt_of_a_heavily_re_run_entry_counts(self):
        # The harm the direction bug caused, end to end: attempt 49 of 50 paid for
        # the agent, so today's tick must SKIP rather than re-spend it.
        latest = {"99": [finder_job("failure", [pre_agent_step()])], "90": [finder_job("success")]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW,
                              run=make_gh_stub(self.runs(50), latest, {"99/49": self.billed_attempt(1)}))
        self.assertFalse(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(1))

    def test_the_anchor_is_the_audited_attempt_not_the_re_run(self):
        # `run_started_at` tracks the LATEST attempt. If the paid attempt ran 8
        # days ago and someone re-ran the entry today (dying pre-agent), anchoring
        # on the run means "audited today" and suppresses the next full interval —
        # fail-CLOSED, the direction this gate exists to avoid. Anchor on the
        # audited attempt's own finder-job start instead, so the tick RUNS.
        runs = self.runs(2)
        runs[1]["run_started_at"] = iso(0.1)   # today's pre-agent re-run
        runs[1]["created_at"] = iso(8)
        latest = {"99": [finder_job("failure", [pre_agent_step()])], "90": [finder_job("success")]}
        billed = billed_finder_job()
        billed["started_at"] = iso(8)
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW,
                              run=make_gh_stub(runs, latest, {"99/1": [billed]}))
        self.assertTrue(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(8))

    def test_a_missing_attempt_timestamp_falls_back_to_the_older_anchor(self):
        # No finder-job `started_at`: fall back to the run's CREATION, not its
        # re-run time. An older anchor means more elapsed days, i.e. fail-open.
        runs = self.runs(2)
        runs[1]["run_started_at"] = iso(0.1)
        runs[1]["created_at"] = iso(8)
        latest = {"99": [finder_job("failure", [pre_agent_step()])], "90": [finder_job("success")]}
        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW,
                              run=make_gh_stub(runs, latest, {"99/1": [billed_finder_job()]}))
        self.assertTrue(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(8))

    def test_a_garbage_run_attempt_degrades_to_the_latest_attempt_only(self):
        latest = {"99": [billed_finder_job()], "90": [finder_job("success")]}
        for raw in (None, "", "lots", {}):
            runs = self.runs(1)
            runs[1]["run_attempt"] = raw
            d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW,
                                  run=make_gh_stub(runs, latest, {}))
            self.assertFalse(d["should_run"], (raw, d["reason"]))

    def test_a_run_entry_with_a_junk_id_is_skipped_not_fetched(self):
        # A missing/garbage `id` would interpolate into the URL and spend a doomed
        # round-trip. It must skip that ENTRY, not abort the scan — aborting would
        # fail open on the whole decision and forget the real history behind it.
        seen = []
        runs = [
            {"id": 100, "status": "in_progress", "run_started_at": iso(0)},
            {"status": "completed", "run_started_at": iso(1)},                  # no id
            {"id": "99; rm -rf /", "status": "completed", "run_started_at": iso(1)},
            {"id": 90, "status": "completed", "run_started_at": iso(8)},
        ]
        base = make_gh_stub(runs, {"90": [finder_job("success")]})

        def spy(cmd, **kwargs):
            seen.append(cmd[-1])
            return base(cmd, **kwargs)

        d = interval.evaluate("o/r", "ci-groom.yml", 100, 7.0, "schedule", NOW, run=spy)
        self.assertTrue(d["should_run"], d["reason"])
        self.assertEqual(d["last_run_at"], iso(8))       # the scan reached the real run
        self.assertFalse([u for u in seen if "None" in u or "rm -rf" in u], seen)

    def test_finder_job_started_at_scans_past_a_match_with_no_timestamp(self):
        stamped = finder_job("success")
        stamped["started_at"] = iso(3)
        jobs = [{"name": "groom / Gate", "started_at": iso(9)},
                finder_job("skipped"),                    # matches the hint, no timestamp
                stamped]
        self.assertEqual(interval.finder_job_started_at(jobs), iso(3))
        self.assertIsNone(interval.finder_job_started_at([finder_job("skipped")]))
        self.assertIsNone(interval.finder_job_started_at(None))


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
