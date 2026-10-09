#!/usr/bin/env python3
"""Pins `cell_timeout_minutes` and the three caps DERIVED from it.

The per-cell agent cap used to be a literal `timeout-minutes: 15`, and the
three numbers that have to move with it — the cell's job cap, the judge's step
cap, the judge's job cap — were kept in line by a COMMENT asking the next
editor to bump them by hand ("COUPLED to `consolidate` below"). That is the
shape of every drift this repo has had: correct today, silently wrong after the
one edit nobody re-read.

Why the coupling matters rather than being tidiness: the judge reads EVERY
cell's findings at the same reasoning tier, so a cell cap raised past the
judge's leaves the judge adjudicating a panel it cannot finish reading, and a
judge JOB cap at or under its STEP cap turns a hung judge into a cancelled job,
which discards every panel cell instead of falling back to the degraded
union-of-findings review. A cell JOB cap at or under its step cap does the same
to the cell: the job dies before `Fail the leg when the cell did not submit`
can read the artifact back, so a dead cell reads as a cancelled run rather than
a red leg.

So the arithmetic now lives in the workflow as expressions, and this file pins
it from three directions:

  * the exact expression at each of the six sites (a re-hardcoded literal
    fails, which is the regression),
  * the ORDERING invariants, evaluated over the whole admitted range rather
    than at the default — a derivation that holds at 15 and inverts at 45 is
    the bug this is for,
  * the preflight bound check, by RUNNING its shell against real values, since
    `type: number` admits 0, -5 and 600 and the derived judge job cap doubles
    whatever gets through.

Deliberately parsed WITHOUT PyYAML, like its sibling suites: this repo is
stdlib-only and CI installs no requirements for these tests.

Run: python3 .github/cursor-review/tests/test_workflow_cell_timeout.py
"""

import os
import re
import subprocess
import unittest

WORKFLOW = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml")
)

FLOOR, CEILING, DEFAULT = 5, 45, 15

# site -> the expression that must be there. `None` for the step sites means
# "the bare input", spelled out per-site so a copy-paste between jobs fails.
CELL = "${{ inputs.cell_timeout_minutes }}"
CELL_JOB = "${{ inputs.cell_timeout_minutes + 15 }}"
JUDGE_STEP = "${{ inputs.cell_timeout_minutes * 2 }}"
JUDGE_JOB = "${{ inputs.cell_timeout_minutes * 2 + 10 }}"

# The values today's workflow had before the input existed. The default must
# reproduce them exactly, or this stopped being a no-op refactor.
HISTORICAL = {"cell_step": 15, "cell_job": 30, "judge_step": 30, "judge_job": 40}


def text():
    with open(WORKFLOW, encoding="utf-8") as fh:
        return fh.read()


def job_block(src, name):
    start = src.index("\n  %s:\n" % name) + 1
    rest = re.compile(r"^  [a-z0-9-]+:$", re.M).search(src, start + 1)
    return src[start : rest.start()] if rest else src[start:]


def step_block(src, name):
    start = src.index("      - name: %s\n" % name)
    nxt = src.find("\n      - name:", start + 10)
    return src[start:nxt] if nxt != -1 else src[start:]


def job_level_timeout(block):
    m = re.search(r"^    timeout-minutes: (.+)$", block, re.M)
    return m.group(1).strip() if m else None


def step_level_timeout(block):
    m = re.search(r"^        timeout-minutes: (.+)$", block, re.M)
    return m.group(1).strip() if m else None


def derive(cell):
    return {
        "cell_step": cell,
        "cell_job": cell + 15,
        "judge_step": cell * 2,
        "judge_job": cell * 2 + 10,
    }


class InputDeclaration(unittest.TestCase):
    def test_declared_as_a_number_defaulting_to_the_historical_cap(self):
        src = text()
        blk = src[src.index("      cell_timeout_minutes:") :][:2600]
        self.assertRegex(blk, r"\n        type: number\n")
        self.assertRegex(blk, r"\n        required: false\n")
        self.assertRegex(blk, r"\n        default: %d\n" % DEFAULT)

    def test_description_states_the_bounds_and_the_derivations(self):
        # The knob is unusable without knowing what else moves with it.
        src = text()
        blk = src[src.index("      cell_timeout_minutes:") :][:2600]
        self.assertIn("%d-%d" % (FLOOR, CEILING), blk)
        for frag in ("+ 15", "x 2", "+ 10"):
            self.assertIn(frag, blk, "description must spell out the derivations")


class DerivedSites(unittest.TestCase):
    def setUp(self):
        self.src = text()

    def test_cursor_cell_step_and_job(self):
        blk = job_block(self.src, "review")
        self.assertEqual(job_level_timeout(blk), CELL_JOB)
        self.assertEqual(step_level_timeout(step_block(blk, "Run cursor review")), CELL)

    def test_direct_cell_step_and_job(self):
        blk = job_block(self.src, "review-openai-direct")
        self.assertEqual(job_level_timeout(blk), CELL_JOB)
        self.assertEqual(
            step_level_timeout(step_block(blk, "Run direct-API review")), CELL
        )

    def test_judge_step_and_job(self):
        blk = job_block(self.src, "consolidate")
        self.assertEqual(job_level_timeout(blk), JUDGE_JOB)
        self.assertEqual(step_level_timeout(step_block(blk, "Run judge")), JUDGE_STEP)

    def test_no_literal_cap_survives_in_the_three_coupled_jobs(self):
        # The regression is one site re-hardcoded while the others stay
        # expressions — green CI, skewed panel.
        for name in ("review", "review-openai-direct", "consolidate"):
            blk = job_block(self.src, name)
            for lit in (15, 30, 40):
                self.assertNotIn(
                    "timeout-minutes: %d\n" % lit,
                    blk,
                    "%s re-hardcoded a cap; derive it from cell_timeout_minutes" % name,
                )


class DerivationInvariants(unittest.TestCase):
    def test_default_reproduces_the_historical_caps_exactly(self):
        self.assertEqual(derive(DEFAULT), HISTORICAL)

    def test_every_admitted_value_keeps_the_ordering(self):
        # Checked across the range, not at the default: a derivation that holds
        # at 15 and inverts at 45 is exactly what this guards.
        for cell in range(FLOOR, CEILING + 1):
            d = derive(cell)
            with self.subTest(cell=cell):
                # A job must outlive its own step, or a dead step becomes a
                # cancelled job and the artifact is never read back.
                self.assertGreater(d["cell_job"], d["cell_step"])
                self.assertGreater(d["judge_job"], d["judge_step"])
                # The judge must never have less model budget than one cell.
                self.assertGreaterEqual(d["judge_step"], d["cell_step"])


class PreflightBoundCheck(unittest.TestCase):
    """Runs the guard's own shell, rather than asserting its text."""

    @classmethod
    def setUpClass(cls):
        blk = step_block(text(), "Validate the cell timeout")
        cls.script = blk.split("        run: |\n", 1)[1]
        cls.script = "".join(
            line[10:] if line.startswith(" " * 10) else line
            for line in cls.script.splitlines(keepends=True)
        )

    def run_guard(self, value):
        return subprocess.run(
            ["bash", "-c", self.script],
            env={**os.environ, "CELL_TIMEOUT": str(value)},
            capture_output=True,
            text=True,
        )

    def test_it_runs_before_any_cell_spends(self):
        src = text()
        blk = job_block(src, "preflight")
        self.assertLess(
            blk.index("- name: Validate the cell timeout"),
            blk.index("- name: Define panel models"),
            "the bound check must precede the panel fan-out it protects",
        )

    def test_accepts_the_whole_admitted_range(self):
        for v in (FLOOR, DEFAULT, CEILING):
            with self.subTest(v=v):
                r = self.run_guard(v)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_reports_every_derived_cap_it_accepted(self):
        # The derived numbers are invisible in a diff now, so the run has to
        # say what they came out as.
        r = self.run_guard(DEFAULT)
        for v in HISTORICAL.values():
            self.assertIn(str(v), r.stdout, r.stdout)

    def test_rejects_out_of_range(self):
        for v in (0, FLOOR - 1, CEILING + 1, 600):
            with self.subTest(v=v):
                r = self.run_guard(v)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("::error::", r.stdout)

    def test_rejects_what_type_number_still_admits(self):
        # `type: number` does not mean "positive whole number".
        for v in ("-5", "1.5", "", "15m", "1e3"):
            with self.subTest(v=v):
                r = self.run_guard(v)
                self.assertEqual(r.returncode, 1, (v, r.stdout, r.stderr))
                self.assertIn("::error::", r.stdout)

    def test_the_guard_names_the_value_it_rejected(self):
        r = self.run_guard(600)
        self.assertIn("600", r.stdout)


if __name__ == "__main__":
    unittest.main()
