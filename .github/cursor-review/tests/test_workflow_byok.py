"""Structural regression tests for cursor-review.yml's `byok` wiring.

With `byok`, an EMPTY CURSOR_API_KEY means the PR author has no key of their
own. Two things must hold, and neither fails loudly if it regresses: the gate's
`should_run` must be vetoed for that author (or the panel runs and every cell
fails at preflight, with no comment telling them why), and the veto must be
limited to runs that would start a panel (or a push on a keyless author's PR
would skip the dismissal job that withdraws a stale approval). Plus the comment
job must only ever POST on `missing`.

Stdlib-only, reusing the job splitter from test_workflow_job_isolation.

Run: python3 .github/cursor-review/tests/test_workflow_byok.py
"""

import unittest

from test_workflow_job_isolation import read_workflow, split_jobs


def text(lines):
    return "\n".join(lines)


class WorkflowByokTest(unittest.TestCase):
    def setUp(self):
        self.jobs = split_jobs(read_workflow())
        self.gate = text(self.jobs["gate"])

    def test_gate_should_run_is_vetoed_by_a_missing_key(self):
        self.assertIn(
            "should_run: ${{ steps.check.outputs.should_run == 'true' && "
            "steps.byok.outputs.status != 'missing' && 'true' || 'false' }}",
            self.gate,
        )

    def test_key_check_runs_only_when_a_panel_would_start(self):
        self.assertIn(
            "if: inputs.byok && steps.check.outputs.should_run == 'true'", self.gate
        )

    def test_key_check_reads_presence_only(self):
        self.assertIn("KEY_PRESENT: ${{ secrets.CURSOR_API_KEY != '' }}", self.gate)

    def test_later_gate_steps_honour_the_veto(self):
        # The dedupe lookup and the Anthropic probe spend API calls; neither
        # should run for a round the veto already cancelled.
        for step_if in (
            "if: steps.check.outputs.should_run == 'true' && steps.byok.outputs.status != 'missing'\n",
            "if: steps.check.outputs.should_run == 'true' && steps.byok.outputs.status != 'missing' && steps.dup",
        ):
            self.assertIn(step_if, self.gate)

    def test_comment_job_runs_only_on_a_byok_verdict_and_posts_only_on_missing(self):
        job = text(self.jobs["byok-comment"])
        self.assertIn("inputs.byok", job)
        self.assertIn("needs.gate.outputs.byok_status == 'missing'", job)
        self.assertIn('elif [ "$BYOK_STATUS" = "missing" ]; then', job)
        self.assertEqual(job.count("-X POST"), 1)

    def test_comment_job_checks_out_nothing(self):
        self.assertNotIn("actions/checkout@", text(self.jobs["byok-comment"]))


if __name__ == "__main__":
    unittest.main()
