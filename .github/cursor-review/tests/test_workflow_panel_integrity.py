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

import json
import os
import re
import shutil
import subprocess
import tempfile
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
    "needs.round-cap.outputs.capped != 'true'",
    "needs.diff-size.outputs.within_cap == 'true'",
    "needs.review.result != 'skipped'",
)

# The five causes the panel job must still be able to see. Each is the exact
# expression its step reads, so a renamed job output fails here rather than
# silently evaluating to the empty string (which every one of these treats as a
# pass) and turning the check into a permanent green.
UNTRUSTED_VALUES = ("OK_COUNT", "TOTAL", "JUDGE_STATUS", "DELIVERED", "UNGATED", "GATED")

# The jobs whose non-success means "nobody decided whether to review this PR".
# `preflight` and `ledger` are the subtle ones: the review matrix `needs:` BOTH,
# so a non-success in either leaves `needs.review.result == 'skipped'` —
# byte-identical to the deliberate no-panel branches — and gating on that alone
# minted a GREEN required check on a run where not one cell ever started.
#
# `ledger` reads like it cannot fail (every step in it is `continue-on-error`,
# for exactly this reason) — but `Ensure ledger artifact exists` carries none,
# its job cap sits above the sum of its step caps, and cancellation is neither.
# Rare is the wrong bar for a guard that hands out a green required check.
DECISION_RESULTS = (
    "needs.gate.result != 'success'",
    "needs.round-cap.result != 'success'",
    "needs.diff-size.result != 'success'",
    "needs.preflight.result != 'success'",
    "needs.ledger.result != 'success'",
)

# The job whose `needs:` list DEFINES which jobs are decision jobs. Pinned as a
# relationship rather than a name list, because the way this fail-open was
# re-opened twice was a job being added to the matrix's `needs:` and nobody
# adding it here.
MATRIX_JOB = "review"

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
    "needs.consolidate.outputs.panel_inconsistent",
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

    def test_a_red_leg_does_not_cancel_its_sibling_cells(self):
        # The other half of "failing here costs the panel nothing", and the only
        # half that is not local to the step. With `fail-fast` left at its
        # DEFAULT of true, the first cell to exit non-zero CANCELS every sibling
        # still running — so the leg check would destroy the panel it exists to
        # measure, landing six cells as one red leg and five cancelled ones, and
        # `Aggregate panel findings` would undercount a matrix that was merely
        # short before. Nothing in this matrix ever failed deliberately until
        # the leg check was added (a cell that did not submit exited GREEN), so
        # this setting was inert to the panel until this change made it
        # load-bearing. That is exactly why it is pinned rather than assumed.
        self.assertIn(
            "      fail-fast: false",
            code_lines(self.review),
            "the `review` matrix lost `fail-fast: false` — a cell that does not "
            "submit now fails its leg deliberately and would CANCEL the sibling "
            "cells, turning a short panel into no panel at all",
        )


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
            ("panel_inconsistent", "steps.aggregate.outputs.panel_inconsistent"),
            ("degraded", "steps.consolidated.outputs.degraded"),
            ("judge_status", "steps.consolidated.outputs.judge_status"),
        ):
            self.assertIn(
                "      %s: ${{ %s }}" % (key, source),
                self.consolidate,
                f"`consolidate` no longer exposes `{key}` from `{source}`",
            )

    def test_the_aggregate_step_still_writes_both_counts(self):
        # The counting lives in aggregate-panel.py (behaviour tested in
        # test_aggregate_panel.py); the step must still hand it GITHUB_OUTPUT.
        self.assertIn('aggregate-panel.py"', self.consolidate)
        self.assertIn('--github-output "$GITHUB_OUTPUT"', self.consolidate)
        script = os.path.join(os.path.dirname(WORKFLOW), "..", "cursor-review", "aggregate-panel.py")
        with open(script, encoding="utf-8") as f:
            source = f.read()
        for written in ('g.write(f"ok_count={ok}\\n")', 'g.write(f"total={total}\\n")',
                        'g.write(f"panel_inconsistent='):
            self.assertIn(written, source)

    def test_the_aggregate_step_reads_the_matrix_results(self):
        # The leg-result cross-check: without these the script sees its
        # defaults (`success` / `skipped`) and can never flag a forgery.
        step = "\n".join(code_lines(step_named(self.jobs["consolidate"], "Aggregate panel findings")))
        self.assertIn("REVIEW_RESULT: ${{ needs.review.result }}", step)
        self.assertIn("DIRECT_RESULT: ${{ needs.review-openai-direct.result }}", step)
        self.assertIn('--review-result "$REVIEW_RESULT"', step)
        self.assertIn('--direct-result "${DIRECT_RESULT:-skipped}"', step)

    def test_the_consolidated_file_carries_panel_inconsistent(self):
        # From the step output, written before the judge ran in this job.
        step = "\n".join(code_lines(step_named(self.jobs["consolidate"], "Build consolidated findings file")))
        self.assertIn("PANEL_INCONSISTENT: ${{ steps.aggregate.outputs.panel_inconsistent }}", step)
        self.assertIn('"panel_inconsistent": panel_inconsistent', step)

    def test_auto_approve_reads_the_job_output_not_only_the_file(self):
        # consolidated.json is built after the `--trust` judge ran in the same
        # job, so the decision takes the pre-judge step output directly.
        post = self.jobs["post-review"]
        step = "\n".join(code_lines(step_named(post, "Auto-approve decision")))
        self.assertIn("PANEL_INCONSISTENT: ${{ needs.consolidate.outputs.panel_inconsistent }}", step)
        self.assertIn('--panel-inconsistent "$PANEL_INCONSISTENT"', step)


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

    def test_an_inconsistent_panel_is_a_failing_cause(self):
        self.assertIn("PANEL_INCONSISTENT: ${{ needs.consolidate.outputs.panel_inconsistent }}", self.body)
        self.assertRegex(
            self.body,
            r'if \[ "\$PANEL_INCONSISTENT" = "true" \]; then\n\s+echo "::error::A findings artifact may not be its own'
            r'[^\n]*"\n\s+causes=\$\(\(causes \+ 1\)\)',
        )

    def test_a_discarded_incremental_block_is_not_a_failing_cause(self):
        # `incremental_subset` is `false` only when the incremental block was
        # built and then FAILED its byte-for-byte subset check — at which point
        # `diff-size` discards the block whole and the panel reviews the full
        # reviewed diff alone. That is byte-identical input to a run which never
        # built a block at all (round 1, every "could not build one" branch),
        # and those report `true`. So the panel is provably just as whole, and
        # reddening a check callers are told to mark REQUIRED over input
        # indistinguishable from a normal run is a merge-blocking false
        # positive. It must annotate and NOT increment `causes`.
        #
        # Pinned because this job was written while that output was still
        # unpublished, when a `false` was believed to mean the cells had been
        # prioritized onto out-of-scope hunks. Publishing it inverted the
        # meaning without touching a line of this job, so nothing but a test
        # stops the next change from re-arming the false positive.
        lines = self.body.split("\n")
        starts = [
            i
            for i, line in enumerate(lines)
            if 'if [ "$INCREMENTAL_SUBSET" = "false" ]' in line
        ]
        self.assertEqual(
            len(starts),
            1,
            "expected exactly one INCREMENTAL_SUBSET branch in `%s`" % PANEL_JOB,
        )

        block = []
        for line in lines[starts[0]:]:
            block.append(line)
            if line.strip() == "fi":
                break
        else:  # pragma: no cover - the branch always closes
            self.fail("the INCREMENTAL_SUBSET branch is never closed by `fi`")
        block = "\n".join(block)

        self.assertNotIn(
            "causes=$((causes + 1))",
            block,
            "a discarded incremental block must not fail `Panel integrity`: the "
            "block is thrown away and the panel reads the full reviewed diff",
        )
        self.assertIn("::warning::", block)
        self.assertNotIn("::error::", block)

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

    # Jobs that need this one for ORDERING only. `auto-retry` (BE-19526)
    # relabels the PR, which cancels whatever of the run is still going, so it
    # must run after this check has reported — but under `!cancelled()`, so a
    # red or skipped check never stops it. Pinned below, not just allowed.
    ORDERING_ONLY = ("auto-retry",)

    def test_it_gates_nothing(self):
        # Advisory: red here must not stop the review from posting, or a short
        # panel would cost the PR the findings it DID produce.
        for name, lines in self.jobs.items():
            if name == PANEL_JOB:
                continue
            needs = job_scalar(lines, "needs") or ""
            if name in self.ORDERING_ONLY:
                cond = job_scalar(lines, "if") or ""
                self.assertIn("!cancelled()", cond, f"`{name}` must run whatever `{PANEL_JOB}` concluded")
                self.assertNotIn(PANEL_JOB, cond, f"`{name}` must not read `{PANEL_JOB}`'s result")
                continue
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
        self.declared_needs = {
            part.strip()
            for part in (job_scalar(self.panel, "needs") or "").strip("[]").split(",")
            if part.strip()
        }

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

    def test_it_needs_the_decision_jobs_so_it_can_read_their_results(self):
        # `needs.<job>.result` evaluates to the empty string unless the job is
        # declared in `needs:` — and '' != 'success' is TRUE, so dropping one
        # from the list would make this check red on EVERY run rather than
        # failing open. Loud, but still wrong, and pinned so they stay declared.
        for job in ("preflight", "ledger"):
            self.assertIn(job, self.declared_needs)

    def test_every_job_the_matrix_needs_is_guarded_here(self):
        # THE invariant, and the one both regressions broke. The review matrix
        # skips when ANY job it `needs:` does not succeed, and a skipped matrix
        # is indistinguishable from the deliberate no-panel branches — so every
        # job in the matrix's `needs:` has to be a decision job here, in this
        # job's `needs:` AND in the guard's condition. Asserting the RELATIONSHIP
        # rather than today's four names is what makes the next job added to the
        # matrix fail this suite instead of silently re-opening the fail-open.
        matrix_needs = {
            part.strip()
            for part in (job_scalar(self.jobs[MATRIX_JOB], "needs") or "")
            .strip("[]")
            .split(",")
            if part.strip()
        }
        self.assertTrue(
            matrix_needs,
            f"could not read `{MATRIX_JOB}`'s `needs:` — the invariant below is "
            "asserting over nothing",
        )
        guard = "\n".join(code_lines(step_named(self.panel, UNDECIDED_STEP)))
        for job in sorted(matrix_needs):
            self.assertIn(
                job,
                self.declared_needs,
                f"`{MATRIX_JOB}` needs `{job}` but `{PANEL_JOB}` does not: a "
                f"non-success `{job}` skips the matrix, leaving "
                "`needs.review.result == 'skipped'` and a GREEN required check "
                "over a run where not one cell reviewed",
            )
            self.assertIn(
                "needs.%s.result != 'success'" % job,
                guard,
                f"`{UNDECIDED_STEP}` does not fail closed on a non-success "
                f"`{job}`, which skips the review matrix",
            )
            self.assertIn(
                "needs.%s.result != 'success'" % job,
                self.condition,
                f"`{PANEL_JOB}`'s `if:` does not run on a non-success `{job}`, "
                "so the check SKIPS — and GitHub counts a skipped required "
                "check as passing",
            )

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


CONTEXT_REF = re.compile(r"\b(?:needs|inputs|steps|vars|matrix|github|secrets)(?:\.[A-Za-z0-9_-]+)+")


def evaluate(expression, context):
    """Evaluate an expression built only from context reads, string literals,
    `always()`, `==`/`!=`, `&&`/`||` and parentheses.

    `context` maps a dotted read (`needs.gate.result`, `inputs.<name>`,
    `steps.<id>.outputs.<name>`, `matrix.<key>`) to its value. A read it does
    not list is '' — exactly what GitHub hands back for a FAILED or SKIPPED
    job's outputs. `&&`/`||` return an operand, in GitHub as in Python, so
    `a == 'true' && inputs.b || ''` yields a string. Anything outside that
    small grammar fails the translation loudly rather than evaluating to a
    guess.
    """
    expr = expression.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    py = CONTEXT_REF.sub(lambda match: repr(context.get(match.group(0), "")), expr)
    py = py.replace("always()", "True").replace("&&", " and ").replace("||", " or ")
    leftover = re.sub(r"'[^']*'|\b(True|False|and|or)\b|==|!=|[()\s]", "", py)
    if leftover:  # pragma: no cover - only on a grammar this helper does not know
        raise AssertionError("cannot evaluate expression remainder %r" % leftover)
    return eval(py, {"__builtins__": {}}, {})  # noqa: S307 - grammar checked above


def evaluate_if(condition, needs):
    """Evaluate a job `if:` against `needs`, which maps a job to
    `{"result": ..., "outputs": {...}}`. A job absent from it, or an output it
    does not declare, reads as '' (see `evaluate`)."""
    context = {}
    for job, entry in needs.items():
        context["needs.%s.result" % job] = entry.get("result", "")
        for name, value in entry.get("outputs", {}).items():
            context["needs.%s.outputs.%s" % (job, name)] = value
    return evaluate(condition, context)


class RoundCapIsADecisionJobTest(unittest.TestCase):
    """`round-cap` (the `max_rounds` stop) sits upstream of `diff-size`.

    A CAPPED round-cap skips `diff-size` by design, and a skipped `diff-size`
    is what the fail-closed disjunct reads as "undecided" — so without its own
    `capped` disjunct this check went RED on every capped head, telling the
    caller to re-run a job that would just hit the cap again. A round-cap that
    did NOT succeed is the opposite case: it skips the panel with nobody having
    decided anything, so it must still go red rather than skip to green.

    Evaluated, not substring-matched: the bug was a correct-looking expression
    whose nesting produced the wrong branch.
    """

    def setUp(self):
        self.panel = split_jobs(read_workflow())[PANEL_JOB]
        self.condition = job_scalar(self.panel, "if")
        self.guard_if = None
        for line in code_lines(step_named(self.panel, UNDECIDED_STEP)):
            if line.strip().startswith("if:"):
                self.guard_if = line.strip()[len("if:"):].strip()
        self.assertIsNotNone(self.condition)
        self.assertIsNotNone(self.guard_if)

    def fresh(self, **overrides):
        needs = {
            "gate": {"result": "success", "outputs": {"should_run": "true", "already_reviewed": "false"}},
            "round-cap": {"result": "success", "outputs": {"capped": "false"}},
            "diff-size": {"result": "success", "outputs": {"within_cap": "true"}},
            "preflight": {"result": "success"},
            "ledger": {"result": "success"},
            "review": {"result": "success"},
            "consolidate": {"result": "success"},
            "post-review": {"result": "success"},
        }
        needs.update(overrides)
        return needs

    def test_a_whole_run_reports_and_does_not_trip_the_guard(self):
        needs = self.fresh()
        self.assertTrue(evaluate_if(self.condition, needs))
        self.assertFalse(evaluate_if(self.guard_if, needs))

    def test_a_capped_head_skips_like_the_other_deliberate_stops(self):
        needs = self.fresh(**{
            "round-cap": {"result": "success", "outputs": {"capped": "true"}},
            "diff-size": {"result": "skipped"},
            "preflight": {"result": "skipped"},
            "ledger": {"result": "success"},
            "review": {"result": "skipped"},
            "consolidate": {"result": "skipped"},
            "post-review": {"result": "skipped"},
        })
        self.assertFalse(
            evaluate_if(self.condition, needs),
            f"`{PANEL_JOB}` runs on a `max_rounds`-capped head, where its guard "
            "reads the deliberately skipped `diff-size` as a failure and goes red",
        )

    def test_max_rounds_off_leaves_capped_empty_and_still_reports(self):
        # `max_rounds: 0` skips the counting step, so `capped` is '' — not
        # 'true', and the panel runs as normal.
        needs = self.fresh(**{"round-cap": {"result": "success", "outputs": {}}})
        self.assertTrue(evaluate_if(self.condition, needs))
        self.assertFalse(evaluate_if(self.guard_if, needs))

    def test_a_failed_round_cap_is_red_not_skipped(self):
        for result in ("failure", "cancelled"):
            needs = self.fresh(**{
                "round-cap": {"result": result},
                "diff-size": {"result": "skipped"},
                "preflight": {"result": "skipped"},
                "review": {"result": "skipped"},
                "consolidate": {"result": "skipped"},
                "post-review": {"result": "skipped"},
            })
            self.assertTrue(evaluate_if(self.condition, needs), result)
            self.assertTrue(evaluate_if(self.guard_if, needs), result)

    def test_the_other_deliberate_stops_still_skip(self):
        unlabelled = self.fresh(**{
            "gate": {"result": "success", "outputs": {"should_run": "false"}},
            "round-cap": {"result": "skipped"},
            "diff-size": {"result": "skipped"},
        })
        over_cap = self.fresh(**{
            "diff-size": {"result": "success", "outputs": {"within_cap": "false"}},
            "review": {"result": "skipped"},
        })
        for needs in (unlabelled, over_cap):
            self.assertFalse(evaluate_if(self.condition, needs))

    def test_the_guard_names_round_cap_in_its_message(self):
        body = "\n".join(code_lines(step_named(self.panel, UNDECIDED_STEP)))
        self.assertIn("ROUND_CAP_RESULT: ${{ needs.round-cap.result }}", body)
        self.assertIn("${ROUND_CAP_RESULT}", body)


class FlattenEscapesWorkflowCommandsTest(unittest.TestCase):
    """`flatten()` must defeat the runner's OWN unescaping, not just newlines.

    The runner unescapes `%25`, `%0D` and `%0A` inside a workflow command's
    message before rendering it. So deleting real newlines is only half the
    defence: a value carrying the literal six characters `%0A` arrives here
    perfectly single-line and the runner puts the newline back, forging the
    second command `tr -d` was meant to prevent.

    Only `JUDGE_STATUS` can actually carry one today — it is whatever string
    the judge agent's tool wrote as `status`, unvalidated — but the function is
    the shared chokepoint for every value the step echoes, so it is pinned
    here rather than at the one call site that needs it. Every other checker in
    this repo already escapes `%` before annotating (`check_agents_md.py`,
    `check_workflow_pins.py`, `check-org-repo-literals.sh`); this is the same
    escape, and the suite executes the real line rather than pattern-matching
    it.
    """

    def setUp(self):
        self.jobs = split_jobs(read_workflow())
        self.assertIn(PANEL_JOB, self.jobs, "job splitter lost the panel job")
        report = step_named(self.jobs[PANEL_JOB], REPORT_STEP)
        self.assertIsNotNone(report, f"`{REPORT_STEP}` is gone")
        definitions = [
            line.strip()
            for line in code_lines(report)
            if line.strip().startswith("flatten()")
        ]
        self.assertEqual(
            len(definitions),
            1,
            f"expected exactly one `flatten()` definition in `{REPORT_STEP}`, "
            f"found {len(definitions)}",
        )
        self.definition = definitions[0]

    def _flatten(self, value):
        """Run the workflow's ACTUAL flatten() line against `value`."""
        bash = shutil.which("bash")
        self.assertIsNotNone(bash, "bash is required to execute flatten()")
        script = self.definition + '\nflatten "$1"\n'
        done = subprocess.run(
            [bash, "-c", script, "flatten-test", value],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            done.returncode, 0, f"flatten() failed: {done.stderr}"
        )
        return done.stdout

    def test_it_escapes_percent_so_the_runner_cannot_re_expand_a_newline(self):
        # The attack: no real newline anywhere in the input, so `tr -d` is a
        # no-op, and the runner's unescaping supplies the line break.
        out = self._flatten("error%0A::stop-commands::tok")
        self.assertNotIn(
            "%0A::",
            out,
            "flatten() passed a live `%0A` through: the runner unescapes it "
            "into a real newline and the following `::stop-commands::` becomes "
            "a genuine workflow command",
        )
        self.assertIn(
            "%250A",
            out,
            "flatten() must escape `%` to `%25`, which renders the payload as "
            "the inert literal text `%0A`",
        )
        # The escape must not silently eat the value it is protecting.
        self.assertTrue(
            out.startswith("error"), f"flatten() mangled the real status: {out!r}"
        )

    def test_it_still_strips_real_newlines(self):
        # The original guarantee, kept: a genuine newline must be DELETED, not
        # escaped back into one by the runner.
        out = self._flatten("ok\n::error::forged\r\nmore")
        self.assertNotIn("\n", out.rstrip("\n"))
        self.assertNotIn("\r", out)
        self.assertNotIn("%0A", out)
        self.assertNotIn("%0D", out)

    def test_it_still_clamps_long_values(self):
        out = self._flatten("A" * 500).rstrip("\n")
        self.assertLessEqual(
            len(out), 64, "flatten() no longer clamps: an agent controls this string"
        )

    def test_the_clamp_cannot_split_an_escape_it_created(self):
        # `cut` must run BEFORE the `%` escape. Were it after, a `%25` produced
        # at the boundary could be truncated to a bare `%2`/`%` — and, worse,
        # a clamp applied to already-escaped text makes the 64-char budget
        # depend on attacker-chosen content.
        out = self._flatten("%" * 100).rstrip("\n")
        self.assertNotIn("%2\n", out)
        self.assertEqual(
            out,
            "%25" * 64,
            "each of the 64 clamped `%` must survive as a WHOLE `%25`",
        )

    def test_every_value_the_step_echoes_goes_through_it(self):
        # Belt-and-braces against the fix being bypassed rather than reverted:
        # a future cause that echoes a raw `$VAR` reintroduces the hole even
        # with flatten() intact. `test_it_flattens_untrusted_values_before_
        # annotating` pins the same property structurally; this asserts the
        # definition it relies on is the escaping one.
        self.assertIn("%25", self.definition, "flatten() no longer escapes `%`")
        self.assertIn("tr -d", self.definition, "flatten() no longer strips newlines")


DIRECT_JOB = "review-openai-direct"
PANEL_MODELS_STEP = "Define panel models"
AGGREGATE_STEP = "Aggregate panel findings"
SEED_STEP = "Seed default findings artifact"
REVIEW_TYPES = ("adversarial", "edge-case")
ASSETS = os.path.normpath(os.path.join(os.path.dirname(WORKFLOW), "..", "cursor-review"))
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}")


def step_scalar(step_lines, key):
    """The scalar value of a step-level `        <key>:`, or None when absent."""
    prefix = "        %s:" % key
    for line in code_lines(step_lines):
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def block_mapping(lines, header, indent):
    """{key: raw value} for the one-line `key: value` entries under `header`.

    `header` is the whole line opening the block (`        env:` in a step,
    `    outputs:` in a job) and its entries sit `indent` spaces deep; the block
    ends at the first line indented less than that.
    """
    entry = re.compile(r"^%s([A-Za-z0-9_-]+):\s*(.*)$" % (" " * indent))
    mapping, inside = {}, False
    for line in code_lines(lines):
        if not inside:
            inside = line == header
        elif line.strip():
            if len(line) - len(line.lstrip()) < indent:
                break
            match = entry.match(line)
            if match:
                mapping[match.group(1)] = match.group(2).strip()
    return mapping


def render(value, context):
    """A one-line YAML value as the runner hands it on: unquoted, and every
    `${{ … }}` in it replaced by its value (a boolean becomes `true`/`false`)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]

    def substitute(match):
        result = evaluate(match.group(1), context)
        if isinstance(result, bool):
            return "true" if result else "false"
        return str(result)

    return EXPRESSION.sub(substitute, value)


def run_block(step_lines):
    """A step's `run: |` script, de-indented the way YAML hands it to bash."""
    script, inside = [], False
    for line in step_lines:
        if not inside:
            inside = line == "        run: |"
        elif line.strip() and not line.startswith(" " * 10):
            break
        else:
            script.append(line[10:])
    if not inside:
        raise AssertionError("the step has no `run: |` block")
    return "\n".join(script).rstrip("\n") + "\n"


def run_step(step_lines, context, workdir):
    """Execute a step for real: its `env:` rendered from `context`, its script
    run as the runner runs it (`bash -e`) with `/tmp/` re-rooted under
    `workdir`. Returns (CompletedProcess, {output: value} it wrote)."""
    script = run_block(step_lines)
    if "/tmp/" not in script:  # pragma: no cover - re-rooting would be a no-op
        raise AssertionError("the step no longer works under /tmp/; re-root its new path here")
    output = os.path.join(workdir, "github-output")
    open(output, "w", encoding="utf-8").close()
    env = {"PATH": os.environ.get("PATH", ""), "GITHUB_OUTPUT": output, "CURSOR_REVIEW_ASSETS": ASSETS}
    for name, value in block_mapping(step_lines, "        env:", 10).items():
        env[name] = render(value, context)
    done = subprocess.run(
        [shutil.which("bash"), "-e", "-c", script.replace("/tmp/", workdir + "/")],
        env=env,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    outputs = {}
    with open(output, encoding="utf-8") as f:
        for line in f:
            key, _, value = line.rstrip("\n").partition("=")
            outputs[key] = value
    return done, outputs


class DirectCellsGateWhenTheyReplaceTest(unittest.TestCase):
    """With `openai_direct_replaces_cursor_openai` on, the direct-API cells ARE
    the panel's OpenAI lane, so they must gate like the Cursor cells they
    replace: counted in ok_count/total, and a leg that goes red when its cell
    did not submit.

    That rests on a few one-line links across three jobs: `preflight` setting
    `openai_direct_counts=true`, `Aggregate panel findings` handing that flag
    and the direct model id to aggregate-panel.py, and the direct leg check's
    `exit 1`. Break any one and the cells quietly fall back to advisory — a
    failed one costs nothing against `approve_max_failed_reviewers` and its leg
    stays green — while the workflow still parses and lints, and
    test_aggregate_panel.py, which calls the script with the flags already set,
    still passes. So the chain is EXECUTED here, from the inputs to the counts
    and the leg's exit status, rather than pattern-matched.
    """

    DIRECT_MODEL = "gpt-6.1-sol"
    # Per lab, so the Anthropic twin below runs the same chain.
    JOB = DIRECT_JOB
    MODEL_INPUT = "openai_direct_model"
    REPLACE_INPUT = "openai_direct_replaces_cursor_openai"
    KEY_OUTPUT = "openai_key_present"
    DIRECT_OUTPUT = "openai_direct"
    VENDOR_PREFIX = "gpt-"

    @classmethod
    def setUpClass(cls):
        for tool in ("bash", "jq", "python3"):
            if shutil.which(tool) is None:
                raise AssertionError(f"`{tool}` is required to execute the workflow steps")
        jobs = split_jobs(read_workflow())
        cls.preflight = jobs["preflight"]
        cls.review = jobs[MATRIX_JOB]
        cls.direct = jobs[cls.JOB]
        cls.consolidate = jobs["consolidate"]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def step(self, job_lines, name):
        body = step_named(job_lines, name)
        self.assertIsNotNone(body, f"step `{name}` is gone")
        return body

    def preflight_outputs(self, replace, key_present="true"):
        """`needs.preflight.outputs.*` once `Define panel models` has run."""
        done, step_outputs = run_step(
            self.step(self.preflight, PANEL_MODELS_STEP),
            {
                "inputs." + self.MODEL_INPUT: self.DIRECT_MODEL,
                "inputs." + self.REPLACE_INPUT: replace,
                "needs.gate.outputs." + self.KEY_OUTPUT: key_present,
            },
            tempfile.mkdtemp(dir=self.tmp),
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        steps = {"steps.models.outputs.%s" % k: v for k, v in step_outputs.items()}
        return {
            "needs.preflight.outputs.%s" % name: render(value, steps)
            for name, value in block_mapping(self.preflight, "    outputs:", 6).items()
        }

    def downstream(self, replace):
        """The context the direct legs and `consolidate` evaluate against."""
        context = self.preflight_outputs(replace)
        context["inputs." + self.MODEL_INPUT] = self.DIRECT_MODEL
        return context

    def upload(self, panel, job_lines, cell_context, model, review_type, status):
        """Lay one cell's record out as `Download panel findings` unpacks it:
        a directory per artifact, named by that leg's own upload step."""
        name = block_mapping(self.step(job_lines, UPLOAD_STEP), "        with:", 10)["name"]
        artifact = os.path.join(panel, render(name, cell_context))
        os.makedirs(artifact)
        with open(os.path.join(artifact, "findings.json"), "w", encoding="utf-8") as f:
            json.dump({"model": model, "review_type": review_type, "status": status, "findings": []}, f)

    def aggregate(self, context, direct_statuses):
        """(ok_count, total, Cursor cell count) from `Aggregate panel findings`
        over every Cursor cell `ok` plus the direct cells given."""
        workdir = tempfile.mkdtemp(dir=self.tmp)
        panel = os.path.join(workdir, "panel")
        models = json.loads(context["needs.preflight.outputs.models"])
        for model in models:
            for rt in REVIEW_TYPES:
                self.upload(panel, self.review, {"matrix.model": model, "matrix.review_type": rt}, model, rt, "ok")
        for rt, status in direct_statuses.items():
            self.upload(panel, self.direct, {**context, "matrix.review_type": rt}, self.DIRECT_MODEL, rt, status)
        done, outputs = run_step(self.step(self.consolidate, AGGREGATE_STEP), context, workdir)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return int(outputs["ok_count"]), int(outputs["total"]), 2 * len(models)

    def leg(self, context, review_type, submitted):
        """The direct leg check for one cell, after `Seed default findings
        artifact` and — when `submitted` — the `ok` record a submission writes
        over the seed. None when the step is skipped, else its process."""
        context = {**context, "matrix.review_type": review_type}
        workdir = tempfile.mkdtemp(dir=self.tmp)
        done, _ = run_step(self.step(self.direct, SEED_STEP), context, workdir)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        if submitted:
            # The seed's path, re-rooted the same way run_step re-roots /tmp/.
            with open(os.path.join(workdir, "findings-out", "findings.json"), "w", encoding="utf-8") as f:
                json.dump({"model": self.DIRECT_MODEL, "review_type": review_type, "status": "ok", "findings": []}, f)
        check = self.step(self.direct, LEG_STEP)
        if not evaluate(step_scalar(check, "if"), context):
            return None
        return run_step(check, context, workdir)[0]

    def test_preflight_marks_replacing_cells_as_counted(self):
        replacing = self.preflight_outputs(replace=True)
        self.assertEqual(replacing["needs.preflight.outputs." + self.DIRECT_OUTPUT], "true")
        self.assertEqual(
            replacing["needs.preflight.outputs." + self.DIRECT_OUTPUT + "_counts"],
            "true",
            "preflight no longer marks REPLACING direct cells as counted, so every "
            "reader downstream treats them as advisory",
        )
        self.assertEqual(
            [m for m in json.loads(replacing["needs.preflight.outputs.models"]) if m.lower().startswith(self.VENDOR_PREFIX)],
            [],
            f"the Cursor {self.VENDOR_PREFIX}* cells were not dropped from the panel",
        )
        for label, outputs in (
            ("side by side", self.preflight_outputs(replace=False)),
            ("no API key", self.preflight_outputs(replace=True, key_present="false")),
        ):
            with self.subTest(label):
                self.assertEqual(outputs["needs.preflight.outputs." + self.DIRECT_OUTPUT + "_counts"], "false")

    def test_a_failed_replacing_cell_counts_against_the_panel(self):
        ok, total, cursor = self.aggregate(
            self.downstream(replace=True), {"adversarial": "ok", "edge-case": "error"}
        )
        self.assertEqual(
            (ok, total),
            (cursor + 1, cursor + 2),
            "the replacing direct cells are missing from ok_count/total, so a failed "
            "one is free against approve_max_failed_reviewers",
        )

    def test_side_by_side_cells_stay_out_of_the_count(self):
        ok, total, cursor = self.aggregate(
            self.downstream(replace=False), {"adversarial": "ok", "edge-case": "error"}
        )
        self.assertEqual(
            (ok, total),
            (cursor, cursor),
            "side-by-side direct cells are counted, so a comparison run can withhold approval",
        )

    def test_a_replacing_cell_that_did_not_submit_fails_its_leg(self):
        context = self.downstream(replace=True)
        self.assertEqual(
            render(job_scalar(self.direct, "continue-on-error") or "false", context),
            "false",
            f"`{self.JOB}` absorbs its own failure while it REPLACES the Cursor OpenAI lane",
        )
        for review_type in REVIEW_TYPES:
            with self.subTest(review_type):
                failed = self.leg(context, review_type, submitted=False)
                self.assertIsNotNone(failed, f"`{LEG_STEP}` is skipped on a replacing cell")
                self.assertNotEqual(
                    failed.returncode, 0, f"`{LEG_STEP}` passed a direct cell that never submitted"
                )
                self.assertIn("::error::", failed.stdout)
                passed = self.leg(context, review_type, submitted=True)
                self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)

    def test_a_side_by_side_leg_stays_advisory(self):
        context = self.downstream(replace=False)
        self.assertEqual(render(job_scalar(self.direct, "continue-on-error") or "false", context), "true")
        self.assertIsNone(
            self.leg(context, "adversarial", submitted=False),
            f"`{LEG_STEP}` fails an ADVISORY side-by-side cell",
        )

    def test_the_leg_check_follows_the_upload_and_spares_its_sibling(self):
        # Same shape as the `review` job's pins above: after the upload, so the
        # errored record still reaches the judge; never absorbed itself; and a
        # red adversarial leg must not cancel the edge-case one.
        order = step_order(self.direct)
        self.assertIn(UPLOAD_STEP, order)
        self.assertIn(LEG_STEP, order)
        self.assertLess(
            order.index(UPLOAD_STEP),
            order.index(LEG_STEP),
            f"`{LEG_STEP}` must come AFTER `{UPLOAD_STEP}` in `{DIRECT_JOB}`",
        )
        self.assertIsNone(
            step_scalar(self.step(self.direct, LEG_STEP), "continue-on-error"),
            f"`{self.JOB}`'s `{LEG_STEP}` carries continue-on-error and can no longer turn the leg red",
        )
        self.assertIn(
            "      fail-fast: false",
            code_lines(self.direct),
            f"`{self.JOB}` lost `fail-fast: false`: one red direct leg would cancel the other",
        )



class AnthropicDirectCellsGateWhenTheyReplaceTest(DirectCellsGateWhenTheyReplaceTest):
    """The same executed chain for `review-anthropic-direct`: its own inputs,
    its own preflight outputs and its own `--anthropic-direct-*` aggregate
    flags, so a broken Anthropic link fails here even with the OpenAI one
    intact."""

    DIRECT_MODEL = "claude-opus-5-5"
    JOB = "review-anthropic-direct"
    MODEL_INPUT = "anthropic_direct_model"
    REPLACE_INPUT = "anthropic_direct_replaces_cursor_anthropic"
    KEY_OUTPUT = "anthropic_key_present"
    DIRECT_OUTPUT = "anthropic_direct"
    VENDOR_PREFIX = "claude-"

    def test_the_judge_model_is_untouched(self):
        step = self.step(self.preflight, PANEL_MODELS_STEP)
        self.assertFalse(
            # `anthropic_direct`, not `anthropic`: the direct JUDGE's own
            # resolution in this step names ANTHROPIC_API_KEY beside
            # `judge_direct_model`, and is not the cells' replacement.
            any("judge" in line.lower() for line in code_lines(step) if "anthropic_direct" in line.lower()),
            "the Anthropic replacement reaches the judge model",
        )


class RecordTokenUsageTest(unittest.TestCase):
    """`Record token usage` in `review-anthropic-direct`, executed: the record
    it uploads carries the API's token counts and nothing model-written (the
    result JSON is model output steered by PR text) and no price, and a cell
    that left no result is marked unmeasured instead of failing the step."""

    CONTEXT = {
        "inputs.anthropic_direct_model": "claude-opus-5-5",
        "matrix.review_type": "edge-case",
        "github.run_attempt": "2",
    }

    def setUp(self):
        job = split_jobs(read_workflow())["review-anthropic-direct"]
        self.step = step_named(job, "Record token usage")
        self.upload = step_named(job, "Upload usage artifact")
        self.assertIsNotNone(self.step, "review-anthropic-direct lost its `Record token usage` step")
        self.assertIsNotNone(self.upload, "review-anthropic-direct lost its `Upload usage artifact` step")

    def record(self, result):
        """Run the step over `result` (None: no result file at all) and return
        (the usage record it wrote, the step's stdout)."""
        workdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, workdir)
        if result is not None:
            with open(os.path.join(workdir, "claude-result.json"), "w", encoding="utf-8") as f:
                f.write(result if isinstance(result, str) else json.dumps(result))
        done, _ = run_step(self.step, self.CONTEXT, workdir)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(os.path.join(workdir, "usage-out", "usage.json"), encoding="utf-8") as f:
            return json.load(f), done.stdout

    def test_copies_the_token_counts_and_nothing_else(self):
        record, stdout = self.record({
            "result": "MODEL-WRITTEN TEXT",
            "session_id": "session-1",
            "total_cost_usd": 1.2345,
            "usage": {
                "input_tokens": 26, "output_tokens": 5312, "cache_read_input_tokens": 397506,
                "cache_creation_input_tokens": 43925,
                "cache_creation": {"ephemeral_1h_input_tokens": 43925, "ephemeral_5m_input_tokens": 0},
            },
            "modelUsage": {
                "claude-opus-5-5": {"inputTokens": 26, "outputTokens": 5312, "cacheReadInputTokens": 397506,
                                    "cacheCreationInputTokens": 43925, "costUSD": 0.5372},
            },
            "num_turns": 12,
            "duration_ms": 65000,
        })
        self.assertEqual(
            (record["model"], record["review_type"], record["run_attempt"], record["measured"]),
            ("claude-opus-5-5", "edge-case", 2, True),
        )
        self.assertEqual(record["usage"], {
            "input_tokens": 26, "output_tokens": 5312, "cache_read_input_tokens": 397506,
            "cache_creation_input_tokens": 43925, "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 43925,
        })
        self.assertEqual(record["models"], {"claude-opus-5-5": {
            "input_tokens": 26, "output_tokens": 5312, "cache_read_input_tokens": 397506,
            "cache_creation_input_tokens": 43925,
        }})
        self.assertEqual((record["num_turns"], record["duration_ms"]), (12, 65000))
        text = json.dumps(record)
        for leaked in ("MODEL-WRITTEN TEXT", "session-1", "1.2345", "0.5372"):
            self.assertNotIn(leaked, text)
        self.assertNotIn("5312", stdout, "token counts belong in the artifact, not the public log")

    def test_a_cell_with_no_usable_result_is_unmeasured(self):
        for result in (None, "", "{truncated", "[]", {"usage": "n/a"}):
            with self.subTest(result=result):
                record, _ = self.record(result)
                self.assertFalse(record["measured"])
                self.assertNotIn("usage", record)

    def test_a_malformed_count_makes_the_record_unmeasured(self):
        # A guessed zero would read as a measured, free cell in a cost report.
        cases = {
            "a string": {"input_tokens": "900"},
            "a negative": {"output_tokens": -5},
            "a boolean": {"cache_read_input_tokens": True},
            "a float": {"cache_creation_input_tokens": 1.5},
            "a non-mapping cache split": {"cache_creation": ["not", "a", "mapping"]},
        }
        for label, usage in cases.items():
            with self.subTest(label):
                record, stdout = self.record({"usage": usage})
                self.assertFalse(record["measured"])
                self.assertNotIn("usage", record)
                self.assertIn("malformed:", stdout)
                self.assertNotIn("900", stdout, "values never reach the public log")

    def test_a_malformed_model_id_makes_the_record_unmeasured_and_is_not_echoed(self):
        record, stdout = self.record({
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {"not a model id; $(x)": {"inputTokens": 1}},
        })
        self.assertFalse(record["measured"])
        self.assertNotIn("$(x)", json.dumps(record) + stdout)

    def test_an_absent_count_reads_as_zero_and_stays_measured(self):
        # Older CLIs report no cache-write split; that is a measured zero.
        record, _ = self.record({"usage": {"input_tokens": 7, "output_tokens": 3}})
        self.assertTrue(record["measured"])
        self.assertEqual(record["usage"]["input_tokens"], 7)
        self.assertEqual(record["usage"]["ephemeral_1h_input_tokens"], 0)
        self.assertEqual(record["models"], {})

    def test_the_upload_cannot_reach_the_judge_or_fail_the_cell(self):
        name = block_mapping(self.upload, "        with:", 10)["name"]
        self.assertTrue(name.startswith("usage-direct-"), name)
        self.assertFalse(name.startswith("findings-"), "consolidate downloads `findings-*` as panel cells")
        self.assertEqual(step_scalar(self.upload, "continue-on-error"), "true")
        # A failed record step would turn a cell that submitted `ok` red, and
        # the aggregator would then flag the whole panel as inconsistent.
        self.assertEqual(step_scalar(self.step, "continue-on-error"), "true")

if __name__ == "__main__":
    unittest.main(verbosity=2)
