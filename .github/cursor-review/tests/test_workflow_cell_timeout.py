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

So the arithmetic now lives in the workflow — in `preflight`'s shell, exposed
as job outputs, because Actions expressions have NO arithmetic operators:
`${{ inputs.x + 15 }}` is a parse error that fails the reusable at load for
every caller, default included. This file pins it from four directions:

  * the exact expression at each of the six sites (a re-hardcoded literal
    fails, which is the regression),
  * no `${{ }}` anywhere in the workflow uses `+`, `-`, `*` or `/` outside a
    string literal (the load-time failure above, which a text-only pin of the
    expression would happily keep green),
  * the ORDERING invariants, evaluated over the whole admitted range rather
    than at the default — a derivation that holds at 15 and inverts at 45 is
    the bug this is for,
  * the preflight bound check, by RUNNING its shell against real values, since
    `type: number` admits 0, -5 and 600 and the derived judge job cap doubles
    whatever gets through — and reading back the caps it writes to
    $GITHUB_OUTPUT, which the six sites consume.

Deliberately parsed WITHOUT PyYAML, like its sibling suites: this repo is
stdlib-only and CI installs no requirements for these tests.

Run: python3 .github/cursor-review/tests/test_workflow_cell_timeout.py
"""

import os
import re
import subprocess
import tempfile
import unittest

WORKFLOW = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml")
)

# The floor is 10, not lower: `Run cursor review` may start its last
# `resource_exhausted` retry as late as `--retry-window 300` (5 min) in, and
# that attempt still needs room for a healthy review.
FLOOR, CEILING, DEFAULT = 10, 45, 15
RETRY_WINDOW_S = 300
JUDGE_STEP_FLOOR = 30

# site -> the expression that must be there. `None` for the step sites means
# "the bare input", spelled out per-site so a copy-paste between jobs fails.
CELL = "${{ inputs.cell_timeout_minutes }}"
CELL_JOB = "${{ fromJSON(needs.preflight.outputs.cell_job_timeout) }}"
JUDGE_STEP = "${{ fromJSON(needs.preflight.outputs.judge_step_timeout) }}"
JUDGE_JOB = "${{ fromJSON(needs.preflight.outputs.judge_job_timeout) }}"

# preflight job output -> the step output it must forward.
PREFLIGHT_OUTPUTS = {
    "cell_job_timeout": "${{ steps.cell_timeout.outputs.cell_job }}",
    "judge_step_timeout": "${{ steps.cell_timeout.outputs.judge_step }}",
    "judge_job_timeout": "${{ steps.cell_timeout.outputs.judge_job }}",
}

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
        "judge_step": max(JUDGE_STEP_FLOOR, cell * 2),
        "judge_job": max(JUDGE_STEP_FLOOR, cell * 2) + 10,
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

    def test_consumers_need_preflight(self):
        # A `needs.preflight.outputs.*` read from a job that does not list
        # preflight in `needs:` evaluates to '' and fromJSON('') fails the job.
        for name in ("review", "review-openai-direct", "consolidate"):
            blk = job_block(self.src, name)
            m = re.search(r"^    needs: \[([^\]]*)\]", blk, re.M)
            self.assertIsNotNone(m, name)
            self.assertIn("preflight", [n.strip() for n in m.group(1).split(",")], name)

    def test_preflight_exposes_each_derived_cap(self):
        blk = job_block(self.src, "preflight")
        for out, expr in PREFLIGHT_OUTPUTS.items():
            self.assertRegex(blk, r"\n      %s: %s\n" % (out, re.escape(expr)))
        self.assertIn("        id: cell_timeout\n", step_block(blk, "Validate the cell timeout"))

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


ARITH = re.compile(r"[+*/]|(?<![\w.-])-(?![\w])|\s-\s")


def has_arithmetic(expr):
    # Operators inside a quoted string literal are just text, and `.*` is the
    # object filter (`labels.*.name`), not a multiplication.
    bare = re.sub(r"'(?:[^']|'')*'", "''", expr).replace(".*", "")
    return bool(ARITH.search(bare))


class NoExpressionArithmetic(unittest.TestCase):
    """Actions expressions have no `+ - * /`; one in `${{ }}` fails the load."""

    def test_no_arithmetic_operator_inside_any_expression(self):
        bad = []
        for lineno, line in enumerate(text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # YAML comments are never evaluated
            for expr in re.findall(r"\$\{\{(.*?)\}\}", line):
                if has_arithmetic(expr):
                    bad.append("%d: ${{%s}}" % (lineno, expr))
        self.assertEqual(bad, [], "arithmetic in an expression fails at load")

    def test_the_check_would_catch_the_original_bug(self):
        # Guards the check above against going vacuous.
        for expr in (" inputs.x + 15 ", " inputs.x * 2 ", " inputs.x * 2 + 10 ", " a - 1 ", " a / 2 "):
            with self.subTest(expr=expr):
                self.assertTrue(has_arithmetic(expr))
        # ...while hyphenated names, object filters and string text are not.
        for expr in (
            " needs.diff-size.outputs.within_cap == 'true' ",
            " toJSON(github.event.pull_request.labels.*.name) ",
            " fromJSON(inputs.runs_on || '\"a+b\"') ",
        ):
            with self.subTest(expr=expr):
                self.assertFalse(has_arithmetic(expr))


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
                # ...nor less than today's, whatever the cell cap: its
                # workload scales with the panel, not with the cell cap.
                self.assertGreaterEqual(d["judge_step"], HISTORICAL["judge_step"])

    def test_floor_leaves_a_late_retry_room_for_a_full_review(self):
        # A retry can start as late as the retry window; the attempt it starts
        # must still get at least as long as the window itself.
        self.assertGreaterEqual(FLOOR * 60 - RETRY_WINDOW_S, RETRY_WINDOW_S)

    def test_floor_constant_tracks_the_wrappers_retry_window(self):
        step = step_block(job_block(text(), "review"), "Run cursor review")
        self.assertIn("--retry-window %d " % RETRY_WINDOW_S, step)


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
        with tempfile.NamedTemporaryFile("r", suffix=".out") as out:
            r = subprocess.run(
                ["bash", "-c", self.script],
                env={**os.environ, "CELL_TIMEOUT": str(value), "GITHUB_OUTPUT": out.name},
                capture_output=True,
                text=True,
            )
            r.outputs = dict(
                line.split("=", 1) for line in out.read().splitlines() if "=" in line
            )
        return r

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

    def test_writes_the_derived_caps_the_jobs_consume(self):
        # These ARE the timeouts now; the notice above is only for humans.
        for cell in (FLOOR, 14, DEFAULT, 16, CEILING):
            d = derive(cell)
            with self.subTest(cell=cell):
                r = self.run_guard(cell)
                self.assertEqual(
                    r.outputs,
                    {
                        "cell_job": str(d["cell_job"]),
                        "judge_step": str(d["judge_step"]),
                        "judge_job": str(d["judge_job"]),
                    },
                )

    def test_rejected_values_write_no_caps(self):
        for v in (0, 600, "1.5"):
            with self.subTest(v=v):
                self.assertEqual(self.run_guard(v).outputs, {})

    def test_rejects_out_of_range(self):
        for v in (0, 5, FLOOR - 1, CEILING + 1, 600, "015", "08"):
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

    def test_rejects_digits_too_long_for_a_64_bit_integer(self):
        # `[ -lt ]` errors on these, and an erroring test inside `if` reads as
        # false, so without a length check both bounds would pass it.
        for v in ("99999999999999999999", "0" * 30 + "15"):
            with self.subTest(v=v):
                r = self.run_guard(v)
                self.assertEqual(r.returncode, 1, (v, r.stdout, r.stderr))
                self.assertIn("::error::", r.stdout)
                self.assertEqual(r.outputs, {})

    def test_the_guard_names_the_value_it_rejected(self):
        r = self.run_guard(600)
        self.assertIn("600", r.stdout)


if __name__ == "__main__":
    unittest.main()
