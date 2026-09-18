#!/usr/bin/env python3
"""Structural regression tests for cursor-review.yml's panel-integrity signals.

A reviewer leg whose `cursor-agent` never submits — overwhelmingly the
15-minute `Run cursor review` step cap, absorbed by its `continue-on-error` —
used to leave the pre-seeded `{"status": "error"}` artifact and exit GREEN.
`Aggregate panel findings` counted it (`Panel: 2/6 cells contributed findings.`)
and only short-circuited at zero, and `post-review.py` demoted findings it could
not anchor to the review body and emitted `ungated_findings=<n>` as a job output
nothing consumed. None of that reached a CHECK-RUN CONCLUSION, which is the only
surface an automated merge gate reads: measured on one consumer repo over 92
panel runs, 38 runs had at least one errored leg, 52 of 552 cells errored, and
every leg check in every one of those runs reported `success` (BE-15554).

Two jobs answer that now and neither is visible in a diff — a deleted step, a
`continue-on-error: true` added to the leg check, a `needs:` entry dropped from
`panel-integrity`, or a fifth gate condition appearing on its `if:` would each
leave a workflow that parses, lints and runs, and would each restore a green
rollup over a short panel. So the shape is pinned here.

Deliberately parsed WITHOUT PyYAML, like its sibling suites: this repo is
stdlib-only and CI installs no requirements for these tests, so a `yaml` import
would simply not run. The workflow is uniformly 2-space indented, which is all
the block splitter below needs.

Run: python3 .github/cursor-review/tests/test_workflow_panel_integrity.py
"""

import os
import re
import unittest

WORKFLOW = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml"
    )
)

JOB_HEADER = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
STEP_HEADER = re.compile(r"^      - ")
STEP_NAME = re.compile(r"^      -\s+name:\s*(\S.*)$")
COMMENT = re.compile(r"^\s*#")

# The leg check and the panel job, by the names their check runs / step titles
# carry. Renaming either is a caller-visible change (a required context name),
# so the literals are pinned rather than matched loosely.
LEG_STEP = "Fail the leg when the cell did not submit"
UPLOAD_STEP = "Upload findings artifact"
PANEL_JOB = "panel-integrity"
PANEL_CONTEXT = "Panel integrity"
UNDECIDED_STEP = "Fail if the panel decision itself did not complete"
REPORT_STEP = "Report panel integrity"

# Every condition `consolidate` gates on must also gate `panel-integrity`, or it
# reports on runs where no panel was ever supposed to happen. They are NESTED
# inside the upstream-failure disjunct rather than ANDed at the top level (see
# `UndecidedRunFailsClosedTest`), so these are substring assertions on purpose.
GATE_CONDITIONS = (
    "needs.gate.outputs.should_run == 'true'",
    "needs.gate.outputs.already_reviewed != 'true'",
    "needs.diff-size.outputs.within_cap == 'true'",
    "needs.review.result != 'skipped'",
)

# The five causes the panel job must still be able to see. Each is the exact
# expression its step reads, so a renamed job output fails here rather than
# silently evaluating to the empty string (which every one of these treats as a
# pass) and turning the check into a permanent green.
UNTRUSTED_VALUES = ("OK_COUNT", "TOTAL", "JUDGE_STATUS", "DELIVERED", "UNGATED", "GATED")

# The three jobs whose non-success means "nobody decided whether to review this
# PR". `preflight` is the subtle one: the review matrix `needs:` it, so a failed
# preflight leaves `needs.review.result == 'skipped'` — byte-identical to the
# deliberate no-panel branches — and gating on that alone minted a GREEN
# required check on a run where not one cell ever started.
DECISION_RESULTS = (
    "needs.gate.result != 'success'",
    "needs.diff-size.result != 'success'",
    "needs.preflight.result != 'success'",
)

# Causes that read a producing job's RESULT rather than its outputs. An output
# is the empty string when its job failed/was skipped/was cancelled, and every
# output-shaped cause treats empty as a pass.
RESULT_CAUSES = (
    "needs.consolidate.result",
    "needs.post-review.result",
)

CAUSE_READS = (
    "needs.consolidate.outputs.ok_count",
    "needs.consolidate.outputs.total",
    "needs.consolidate.outputs.degraded",
    "needs.post-review.outputs.delivered",
    "needs.post-review.outputs.ungated_findings",
    "needs.diff-size.outputs.incremental_subset",
)


def read_workflow():
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read().split("\n")


def split_jobs(lines):
    """{job name: [lines]} for the top-level jobs: mapping."""
    try:
        start = lines.index("jobs:") + 1
    except ValueError:  # pragma: no cover - the file always has one
        raise AssertionError("cursor-review.yml has no top-level `jobs:` key")

    jobs, name, body = {}, None, []
    for line in lines[start:]:
        match = JOB_HEADER.match(line)
        if match:
            if name:
                jobs[name] = body
            name, body = match.group(1), []
            continue
        if name is not None:
            body.append(line)
    if name:
        jobs[name] = body
    return jobs


def split_steps(job_lines):
    """[(name, [lines])] for each `- name:` step in a job body.

    A step with no `name:` on its header line comes back as `None`, which is
    enough for the ORDERING assertions below without pretending to name it.
    """
    steps, name, current = [], None, None
    for line in job_lines:
        if STEP_HEADER.match(line):
            if current is not None:
                steps.append((name, current))
            match = STEP_NAME.match(line)
            name = match.group(1).strip().strip("'\"") if match else None
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        steps.append((name, current))
    return steps


def code_lines(block):
    """Lines with whole-line comments dropped.

    Every comment block in this file NAMES the steps, jobs and outputs asserted
    below — the leg step's own rationale quotes `status=error`, and the panel
    job's header quotes `ungated_findings` — so a raw-text scan would stay green
    with the code itself deleted. That is the exact failure this suite exists to
    prevent, so every assertion runs over code only.
    """
    return [line for line in block if not COMMENT.match(line)]


def job_scalar(job_lines, key):
    """The scalar value of a job-level `    <key>:`, or None when absent."""
    prefix = "    %s:" % key
    for line in code_lines(job_lines):
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def step_named(job_lines, wanted):
    for name, body in split_steps(job_lines):
        if name == wanted:
            return body
    return None


def step_order(job_lines):
    return [name for name, _ in split_steps(job_lines)]


class LegFailsWhenTheCellDidNotSubmitTest(unittest.TestCase):
    def setUp(self):
        self.jobs = split_jobs(read_workflow())
        # Guard the parser: a splitter that silently stopped matching would make
        # every assertion below pass vacuously.
        for expected in ("review", "consolidate", "post-review", PANEL_JOB):
            self.assertIn(expected, sorted(self.jobs), f"job splitter lost `{expected}`")
        self.review = self.jobs["review"]

    def test_the_leg_check_exists(self):
        self.assertIsNotNone(
            step_named(self.review, LEG_STEP),
            f"the `review` job lost its `{LEG_STEP}` step — a cell that never "
            "submits is green again and the rollup lies about the panel",
        )

    def test_the_leg_check_runs_even_when_an_earlier_step_failed(self):
        # Without `if: always()` the step is SKIPPED on exactly the runs it
        # exists for: `Run cursor review` absorbs its own timeout, but a failed
        # checkout, CLI install or prompt build fails the job at that step and
        # everything after it is skipped by default.
        body = code_lines(step_named(self.review, LEG_STEP))
        self.assertIn(
            "        if: always()",
            body,
            f"`{LEG_STEP}` is not `if: always()`",
        )

    def test_the_leg_check_runs_after_the_artifact_upload(self):
        # Order is the whole reason failing here is free. Before the upload, a
        # red leg would take `Upload findings artifact` down with it, the cell
        # would vanish from the panel entirely, and `Aggregate panel findings`
        # would undercount the matrix — strictly worse than the green-leg bug.
        order = step_order(self.review)
        self.assertIn(UPLOAD_STEP, order)
        self.assertIn(LEG_STEP, order)
        self.assertLess(
            order.index(UPLOAD_STEP),
            order.index(LEG_STEP),
            f"`{LEG_STEP}` must come AFTER `{UPLOAD_STEP}`, or a non-submitting "
            "cell loses its artifact instead of merely being reported",
        )

    def test_the_leg_check_is_not_itself_absorbed(self):
        body = code_lines(step_named(self.review, LEG_STEP))
        self.assertFalse(
            any("continue-on-error" in line for line in body),
            f"`{LEG_STEP}` carries continue-on-error — it can no longer turn "
            "the leg red, which is its only job",
        )
        self.assertTrue(
            any(line.strip() == "exit 1" for line in body),
            f"`{LEG_STEP}` no longer exits non-zero",
        )

    def test_the_run_step_still_absorbs_its_own_cap(self):
        # The premise of the split: the cap stays absorbed so the upload still
        # runs, and the LEG check is what turns the cell red. Dropping
        # `continue-on-error` from `Run cursor review` would fail the job at the
        # timeout, skip the upload, and take the cell out of the panel.
        body = code_lines(step_named(self.review, "Run cursor review"))
        self.assertIn("        continue-on-error: true", body)


class ConsolidateExposesPanelCountsTest(unittest.TestCase):
    def setUp(self):
        self.jobs = split_jobs(read_workflow())
        self.consolidate = "\n".join(code_lines(self.jobs["consolidate"]))

    def test_the_panel_counts_are_job_outputs(self):
        # `Aggregate panel findings` has always PRINTED these. A job output is
        # what makes them readable outside the job's own log.
        for key, source in (
            ("ok_count", "steps.aggregate.outputs.ok_count"),
            ("total", "steps.aggregate.outputs.total"),
            ("degraded", "steps.consolidated.outputs.degraded"),
            ("judge_status", "steps.consolidated.outputs.judge_status"),
        ):
            self.assertIn(
                "      %s: ${{ %s }}" % (key, source),
                self.consolidate,
                f"`consolidate` no longer exposes `{key}` from `{source}`",
            )

    def test_the_aggregate_step_still_writes_both_counts(self):
        for written in ('g.write(f"ok_count={ok}\\n")', 'g.write(f"total={len(panel)}\\n")'):
            self.assertIn(written, self.consolidate)


class PanelIntegrityJobTest(unittest.TestCase):
    def setUp(self):
        self.jobs = split_jobs(read_workflow())
        self.panel = self.jobs[PANEL_JOB]
        self.body = "\n".join(code_lines(self.panel))

    def test_it_publishes_the_documented_context_name(self):
        # Callers mark `<caller job id> / Panel integrity` required, and
        # docs/callers/cursor-review.md names that string. Renaming the job's
        # `name:` silently un-requires the check on every repo that did.
        self.assertEqual(job_scalar(self.panel, "name"), PANEL_CONTEXT)

    def test_it_needs_every_job_it_reads(self):
        needs = job_scalar(self.panel, "needs")
        self.assertIsNotNone(needs, f"`{PANEL_JOB}` declares no `needs:`")
        # Compare PARSED names, not the raw scalar: `review` is a substring of
        # `post-review`, so a plain `in` check would stay green with `review`
        # dropped from the list — the one entry whose loss this test most needs
        # to catch, since `needs.review.result` is what keeps the job from
        # reporting on a fork.
        declared = {part.strip() for part in needs.strip("[]").split(",") if part.strip()}
        for job in ("gate", "diff-size", "review", "consolidate", "post-review"):
            self.assertIn(
                job,
                declared,
                f"`{PANEL_JOB}` dropped `{job}` from needs — its outputs then "
                "evaluate to the empty string, which every cause treats as a pass",
            )

    def test_it_reports_on_cancellation(self):
        # `always()`, not `!cancelled()`: GitHub counts a SKIPPED required check
        # as PASSING, so a cancelled run that skipped this job would mint a green
        # context over a panel that never finished — the same fail-open the
        # blocking gate documents at its own `if:`.
        condition = job_scalar(self.panel, "if")
        self.assertIsNotNone(condition)
        self.assertIn("always()", condition)
        self.assertNotIn("!cancelled()", condition)

    def test_it_is_gated_exactly_like_consolidate(self):
        # Reporting on a run where the panel was never supposed to happen — no
        # trigger label, already reviewed, over the diff-size cap, panel skipped
        # — would be red on every PR that deliberately skips the review.
        condition = job_scalar(self.panel, "if")
        for gate in GATE_CONDITIONS:
            self.assertIn(gate, condition, f"`{PANEL_JOB}` lost gate `{gate}`")

    def test_it_holds_no_permissions(self):
        # An ABSENT block is not "no permissions": a workflow_call reusable
        # INHERITS the caller job's, and the documented caller grants
        # `pull-requests: write`.
        self.assertEqual(job_scalar(self.panel, "permissions"), "{}")

    def test_it_is_bounded(self):
        self.assertEqual(job_scalar(self.panel, "timeout-minutes"), "5")

    def test_it_reads_every_cause(self):
        for read in CAUSE_READS:
            self.assertIn(
                "${{ %s }}" % read,
                self.body,
                f"`{PANEL_JOB}` no longer reads `{read}`",
            )

    def test_it_fails_and_annotates(self):
        self.assertIn("exit 1", self.body)
        self.assertIn("::error::", self.body)
        self.assertIn("::notice::Panel integrity:", self.body)

    def test_it_flattens_untrusted_values_before_annotating(self):
        # JUDGE_STATUS comes from the judge agent's own tool output and the
        # counts follow panel-cell artifacts — all written inside jobs that run
        # `cursor-agent --trust` over PR code. A newline reaching a
        # ::workflow command:: line forges a second command, so every one of
        # these must be interpolated through `flatten` and nowhere else.
        #
        # Two things this had to get right, and originally did not:
        #
        # 1. The pattern matches the BARE `$VAR` / `${VAR`, with no leading
        #    quote. Requiring a `"` immediately before the `$` only ever matched
        #    the already-safe `$(flatten "$VAR")` form — so the unsafe form this
        #    test exists to catch (`status=$JUDGE_STATUS`, a non-quote character
        #    before the `$`) never matched at all, while `hits` stayed non-zero
        #    from the safe occurrences and the assertion passed VACUOUSLY over a
        #    real workflow-command injection.
        # 2. Every line that ECHOES is checked, not only lines containing `::`.
        #    The runner parses workflow commands on every stdout line, so an
        #    untrusted value echoed on a plain log line is the same hole. Lines
        #    that merely TEST a value (`if [ "$OK_COUNT" != "$TOTAL" ]`) reach no
        #    stdout and are correctly left alone.
        self.assertIn("flatten()", self.body)
        for var in UNTRUSTED_VALUES:
            pattern = re.compile(r"\$\{?%s\b" % var)
            hits = 0
            for line in self.body.split("\n"):
                if "echo " not in line:  # only echoed lines reach the log
                    continue
                for match in pattern.finditer(line):
                    hits += 1
                    # `$(flatten "$VAR"` and `$(flatten "${VAR:-…}"` both leave
                    # `…$(flatten "` before the match; the bare `$(flatten $VAR`
                    # form leaves `…$(flatten `. Strip the optional quote, then
                    # require the call.
                    prefix = line[: match.start()].rstrip('"')
                    self.assertTrue(
                        prefix.endswith("flatten "),
                        f"`{PANEL_JOB}` echoes ${var} without flatten(): a "
                        "newline in it forges a second workflow command"
                        f"\n    {line.strip()}",
                    )
            self.assertTrue(hits, f"`{PANEL_JOB}` no longer reports ${var}")

    def test_it_gates_nothing(self):
        # Advisory: red here must not stop the review from posting, or a short
        # panel would cost the PR the findings it DID produce.
        for name, lines in self.jobs.items():
            if name == PANEL_JOB:
                continue
            needs = job_scalar(lines, "needs") or ""
            self.assertNotIn(
                PANEL_JOB,
                needs,
                f"job `{name}` needs `{PANEL_JOB}` — the check is advisory and "
                "must gate no other job",
            )


class UndecidedRunFailsClosedTest(unittest.TestCase):
    """The job must go RED, not skipped, when the decision jobs did not finish.

    Every gate condition on `panel-integrity` reads a job OUTPUT, and a job that
    FAILED has empty outputs. Gating the job on those outputs alone therefore
    SKIPS it exactly when `gate`'s dup-check API call errors or `diff-size`
    cannot build the diff — and GitHub counts a skipped required check as
    PASSING, so a caller that took this PR's advice and required `Panel
    integrity` would get a green merge gate over a run that never decided
    whether to review the PR at all. `Blocking gate` closes the same hole with
    the same two guards; this suite pins that they stay closed here too.
    """

    def setUp(self):
        self.jobs = split_jobs(read_workflow())
        self.panel = self.jobs[PANEL_JOB]
        self.condition = job_scalar(self.panel, "if")
        self.assertIsNotNone(self.condition)

    def test_the_job_runs_when_an_upstream_decision_job_failed(self):
        # Without EVERY disjunct the job is skipped on that failing path and the
        # guard step below can never fire, however it is written.
        for result in DECISION_RESULTS:
            self.assertIn(
                result,
                self.condition,
                f"`{PANEL_JOB}`'s `if:` no longer runs the job on `{result}` — an "
                "undecided run skips this check, and a skipped required check is "
                "a GREEN merge gate",
            )

    def test_the_guard_step_exists_and_is_first(self):
        order = step_order(self.panel)
        self.assertIn(
            UNDECIDED_STEP,
            order,
            f"`{PANEL_JOB}` lost its `{UNDECIDED_STEP}` guard — the job now runs "
            "on undecided runs and reports causes read from empty outputs",
        )
        self.assertIn(REPORT_STEP, order)
        self.assertLess(
            order.index(UNDECIDED_STEP),
            order.index(REPORT_STEP),
            f"`{UNDECIDED_STEP}` must come BEFORE `{REPORT_STEP}`: every cause "
            "there treats an empty output as a pass, so the report would answer "
            "the wrong question on a run that was never decided",
        )

    def test_the_guard_step_fires_on_every_failure_and_exits_nonzero(self):
        body = code_lines(step_named(self.panel, UNDECIDED_STEP))
        joined = "\n".join(body)
        for result in DECISION_RESULTS:
            self.assertIn(
                result,
                joined,
                f"`{UNDECIDED_STEP}` no longer fires on `{result}`",
            )
        self.assertFalse(
            any("continue-on-error" in line for line in body),
            f"`{UNDECIDED_STEP}` carries continue-on-error and can no longer fail the check",
        )
        self.assertTrue(
            any(line.strip() == "exit 1" for line in body),
            f"`{UNDECIDED_STEP}` no longer exits non-zero",
        )
        self.assertIn("::error::", joined)

    def test_it_needs_preflight_so_it_can_read_its_result(self):
        # `needs.preflight.result` evaluates to the empty string unless the job
        # is declared in `needs:` — and '' != 'success' is TRUE, so dropping it
        # from the list would make this check red on EVERY run rather than
        # failing open. Loud, but still wrong, and pinned so it stays declared.
        declared = {
            part.strip()
            for part in (job_scalar(self.panel, "needs") or "").strip("[]").split(",")
            if part.strip()
        }
        self.assertIn("preflight", declared)

    def test_the_causes_read_the_producing_jobs_results_not_only_outputs(self):
        # `ok_count`/`total` are BOTH empty when `consolidate` died, and
        # unset-vs-unset compares equal, so the completeness comparison alone
        # reports a whole panel over a job that never ran. Pair every cause with
        # its producer's result, and reject an empty count explicitly.
        report = "\n".join(code_lines(step_named(self.panel, REPORT_STEP)))
        for cause in RESULT_CAUSES:
            self.assertIn(
                "${{ %s }}" % cause,
                report,
                f"`{REPORT_STEP}` no longer reads `{cause}` — an empty output "
                "from a dead producer then reads as a pass",
            )
        self.assertIn(
            '[ -z "$OK_COUNT" ] || [ -z "$TOTAL" ]',
            report,
            f"`{REPORT_STEP}` no longer rejects empty cell counts: unset-vs-unset "
            'compares EQUAL, so it would print "whole panel" having counted nothing',
        )

    def test_the_deliberate_skips_are_still_skips(self):
        # The fail-closed disjunct must not swallow the intentional no-panel
        # branches: those are the ones where `gate` and `diff-size` both
        # SUCCEEDED and said no review was warranted, and being red on every
        # unlabelled PR is what would get this check un-required again.
        for gate in GATE_CONDITIONS:
            self.assertIn(gate, self.condition, f"`{PANEL_JOB}` lost gate `{gate}`")


if __name__ == "__main__":
    unittest.main(verbosity=2)
