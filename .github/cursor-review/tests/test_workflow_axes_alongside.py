#!/usr/bin/env python3
"""Structural tests for cursor-review.yml's `axes_alongside_panel` jobs.

With the input off NOTHING may change: every job it adds must be skipped, which
holds only while `inputs.axes_alongside_panel` is a top-level conjunct of each
such job's `if:` — an `||` beside it would run the job anyway. Nothing fails
when that slips, so it is pinned here, along with the wiring the design rests
on: the start phase never says `pass` and needs `defer_approval`, a round the
panel itself will review and the stale-approval dismissal ahead of it, the axes
need a SUCCESSFUL start and take its validated axis list, none of them gates the
panel's own checks, the close-out reads the same gate and axis results the
workflow reports, and no nested job asks for a scope the documented caller
does not grant (that fails every caller at startup, input on or off).

Parsed without PyYAML, like test_workflow_job_isolation.py next door.

Run: python3 .github/cursor-review/tests/test_workflow_axes_alongside.py
"""

import importlib.util
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.normpath(os.path.join(HERE, "..", "..", "workflows"))

_spec = importlib.util.spec_from_file_location(
    "job_isolation", os.path.join(HERE, "test_workflow_job_isolation.py"))
iso = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iso)

NEW_JOBS = ("approve-start-early", "axis-correctness", "axis-conformance", "axes-closeout")
FLAG = "inputs.axes_alongside_panel"
# The documented cursor-review caller grants exactly these.
CALLER_GRANT = {"contents": {"read"}, "pull-requests": {"read", "write"}}


def read(name):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as f:
        return f.read()


def job_key(job_lines, key):
    """A job-level `key:` value, with a folded (`>-`) block joined onto one line."""
    out, inside = None, False
    for line in iso.code_lines(job_lines):
        if inside:
            if line.startswith("      ") or not line.strip():
                out += " " + line.strip()
                continue
            break
        match = re.match(r"^    %s:\s*(.*)$" % re.escape(key), line)
        if match:
            value = match.group(1).strip()
            if value in (">-", ">", "|", "|-"):
                out, inside = "", True
            else:
                return value
    return out.strip() if out is not None else None


def unwrap(expr):
    expr = expr.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    return expr


def top_level(expr, op):
    """`expr` split on `op` outside parentheses and quotes."""
    parts, depth, quote, start, i = [], 0, False, 0, 0
    while i < len(expr):
        ch = expr[i]
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
        elif not quote and depth == 0 and expr.startswith(op, i):
            parts.append(expr[start:i].strip())
            start = i + len(op)
            i += len(op)
            continue
        i += 1
    parts.append(expr[start:].strip())
    return parts


def with_value(job_lines, key):
    """A `with:`/`env:` entry's value inside a job (first match)."""
    for line in iso.code_lines(job_lines):
        match = re.match(r"^\s+%s:\s*(.*)$" % re.escape(key), line)
        if match:
            return match.group(1).strip()
    return None


def output_value(text, name):
    match = re.search(r"^      %s:\n(?:        .*\n)*?        value: (.*)$" % re.escape(name), text, re.MULTILINE)
    return match.group(1).strip() if match else None


class AxesAlongsidePanel(unittest.TestCase):
    def setUp(self):
        self.text = read("cursor-review.yml")
        self.jobs = iso.split_jobs(self.text.split("\n"))
        for name in NEW_JOBS:
            self.assertIn(name, sorted(self.jobs), f"job splitter lost `{name}`")

    def test_the_input_defaults_off(self):
        self.assertRegex(self.text, r"\n      axes_alongside_panel:\n(?:        .*\n)*?        type: boolean\n"
                                    r"        required: false\n        default: false\n")

    def test_off_skips_every_job_it_adds(self):
        flagged = sorted(n for n, body in self.jobs.items() if FLAG in (job_key(body, "if") or ""))
        self.assertEqual(flagged, sorted(NEW_JOBS))
        for name in NEW_JOBS:
            expr = unwrap(job_key(self.jobs[name], "if"))
            self.assertEqual(top_level(expr, "||"), [expr], f"`{name}`'s if: has a top-level ||")
            self.assertIn(FLAG, top_level(expr, "&&"), f"`{name}`: {FLAG} is not a top-level conjunct")

    def test_no_new_job_gates_the_panels_own_checks(self):
        for gate in ("panel-integrity", "blocking-gate"):
            needs = job_key(self.jobs[gate], "needs") or ""
            for name in NEW_JOBS:
                self.assertNotRegex(needs, r"\b%s\b" % re.escape(name), gate)

    def test_the_start_phase_is_pending_or_capped_never_pass(self):
        body = self.jobs["approve-start-early"]
        self.assertEqual(job_key(body, "uses"), "./.github/workflows/cursor-approve.yml")
        self.assertEqual(with_value(body, "phase"), "start")
        self.assertEqual(
            with_value(body, "approve_gate"),
            "${{ (needs.round-cap.outputs.capped == 'true' || needs.round-cap.outputs.labelled == 'true')"
            " && 'capped' || 'pending' }}")
        # The real next round: `card.py prior` keeps only rounds below it.
        self.assertIn("needs.round-cap.outputs.next_round", with_value(body, "round"))
        expr = top_level(unwrap(job_key(body, "if")), "&&")
        # `!cancelled()`: diff-size and preflight are skipped on the capped path.
        self.assertEqual(expr[0], "!cancelled()")
        for term in ("github.event_name == 'pull_request_target'", "needs.round-cap.result == 'success'",
                     "inputs.approve_max_severity != ''", "needs.gate.outputs.approve_author_ok != 'false'",
                     "needs.gate.outputs.should_run == 'true'", "needs.gate.outputs.already_reviewed != 'true'",
                     # Without it post-review approves on severity alone and nothing withdraws it.
                     "inputs.defer_approval",
                     "needs.dismiss-stale-approval.result != 'failure'",
                     "needs.dismiss-stale-approval.result != 'cancelled'"):
            self.assertIn(term, expr)
        # Axes only for a round the panel itself reviews; the hand-off card without them.
        self.assertIn("(needs.round-cap.outputs.capped == 'true' || needs.round-cap.outputs.labelled == 'true' || "
                      "(needs.diff-size.outputs.within_cap == 'true' && needs.preflight.result == 'success'))", expr)
        needs = job_key(body, "needs")
        for job in ("gate", "round-cap", "diff-size", "preflight", "dismiss-stale-approval"):
            self.assertRegex(needs, r"\b%s\b" % re.escape(job))

    def test_post_review_waits_for_the_early_start_card(self):
        # Otherwise a late start overwrites decide's round card with a pending one.
        self.assertRegex(job_key(self.jobs["post-review"], "needs"), r"\bapprove-start-early\b")

    def test_the_axes_need_a_successful_start_and_get_explicit_secrets(self):
        for axis in ("correctness", "conformance"):
            body = self.jobs[f"axis-{axis}"]
            self.assertEqual(job_key(body, "uses"), f"./.github/workflows/axis-{axis}.yml")
            expr = top_level(unwrap(job_key(body, "if")), "&&")
            self.assertEqual(expr[0], "!cancelled()")
            for term in ("needs.approve-start-early.result == 'success'",
                         "needs.round-cap.outputs.capped != 'true'", "needs.round-cap.outputs.labelled != 'true'"):
                self.assertIn(term, expr, axis)
            self.assertEqual(expr[-1], f"contains(format(',{{0}},', needs.approve-start-early.outputs.axes), ',{axis},')")
            lines = iso.code_lines(body)
            self.assertNotIn("    secrets: inherit", lines)
            self.assertIn("      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}", lines)
            self.assertIn("      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}", lines)

    def test_the_closeout_reads_the_gate_the_workflow_reports(self):
        workflow_gate = output_value(self.text, "approve_gate")
        self.assertTrue(workflow_gate)
        closeout = with_value(self.jobs["axes-closeout"], "APPROVE_GATE")
        self.assertEqual(closeout, workflow_gate.replace("jobs.", "needs."))
        needs = job_key(self.jobs["axes-closeout"], "needs")
        for job in re.findall(r"needs\.([a-z-]+)\.", closeout):
            self.assertIn(job, needs)
        expr = top_level(unwrap(job_key(self.jobs["axes-closeout"], "if")), "&&")
        # Not `always()`: a superseded run must not close out the newer run's card.
        self.assertEqual(expr[0], "!cancelled()")
        self.assertIn("needs.approve-start-early.result != 'skipped'", expr)
        axes_ok = with_value(self.jobs["axes-closeout"], "AXES_OK")
        self.assertEqual(axes_ok, output_value(self.text, "axes_ok").replace("jobs.", "needs."))
        for job in ("axis-correctness", "axis-conformance"):
            self.assertIn(f"needs.{job}.result", axes_ok)
            self.assertRegex(needs, r"\b%s\b" % re.escape(job))
        self.assertIn('--axes-ok "$AXES_OK"', "\n".join(self.jobs["axes-closeout"]))
        self.assertIn('if [ -z "$APPROVER_TOKEN" ]', "\n".join(self.jobs["axes-closeout"]))

    def test_axes_started_reports_the_start_jobs_result(self):
        self.assertEqual(output_value(self.text, "axes_started"), "${{ jobs.approve-start-early.result }}")

    def test_axes_ok_counts_an_unlisted_axis_but_not_a_failed_one(self):
        value = output_value(self.text, "axes_ok")
        for axis in ("correctness", "conformance"):
            self.assertIn(f"""contains(fromJSON('["success","skipped"]'), jobs.axis-{axis}.result)""", value)

    def test_cursor_approve_validates_and_exports_the_axes(self):
        text = read("cursor-approve.yml")
        self.assertRegex(text, r"\n    outputs:\n      axes:\n(?:        .*\n)*?        value: \$\{\{ jobs\.approve\.outputs\.axes \}\}\n")
        approve = iso.split_jobs(text.split("\n"))["approve"]
        self.assertIn("      axes: ${{ steps.axes.outputs.axes }}", iso.code_lines(approve))
        step = "\n".join(approve)
        step = step[step.index("- name: Check the axes"):step.index("- name: Check the approver token")]
        self.assertIn('== "pending"', step)
        self.assertIn('("correctness", "conformance")', step)

    def test_no_nested_job_asks_more_than_the_documented_caller_grants(self):
        # A nested job cannot exceed its caller's grant, and GitHub checks
        # that at startup for every job, whatever its `if:` says.
        files = ["cursor-review.yml", "cursor-approve.yml", "axis-correctness.yml", "axis-conformance.yml",
                 "cursor-axis-base.yml"]
        for name in files:
            jobs = iso.split_jobs(read(name).split("\n"))
            self.assertTrue(jobs, name)
            for job, body in jobs.items():
                perms = iso.job_permissions(body) or {}
                for scope, value in perms.items():
                    if (scope, value) == ("*", "{}"):  # the explicit empty block grants nothing
                        continue
                    self.assertIn(scope, CALLER_GRANT, f"{name}:{job} asks for `{scope}: {value}`")
                    self.assertIn(value, CALLER_GRANT[scope], f"{name}:{job} asks for `{scope}: {value}`")


if __name__ == "__main__":
    unittest.main()
