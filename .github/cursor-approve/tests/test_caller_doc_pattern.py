#!/usr/bin/env python3
"""Pins the `if:` guards on the caller pattern in docs/callers/cursor-approve.md.

cursor-review's called job goes RED whenever a panel cell errors: the leg fails
by design ("Fail the leg when the cell did not submit") and `Panel integrity`
fails alongside it. Neither is affected by `approve_max_failed_reviewers`, which
governs the `approve_gate` VERDICT and not the job's conclusion. So a caller
that chains the start phase and the axes off that job with a bare `needs:` has
them skipped by GitHub's default rule, and decide — which runs on `always()` —
reports every axis as "no result" and withholds.

That is not hypothetical: it is what the published pattern did. One errored
`edge-case` cell out of six took the axes out of a real run on a caller that had
set `approve_max_failed_reviewers: 1` precisely to tolerate it, and across the
81 preceding panel runs on that repo the axes had never once run.

A copied caller is as good as its snippet, and `!cancelled()` reads like a
stylistic flourish to anyone editing it later — a bare `if:` here parses, lints,
and silently withholds every approval. So the guards are pinned.

Deliberately parsed WITHOUT PyYAML, like its sibling suites: this repo is
stdlib-only and CI installs no requirements for these tests.

Run: python3 .github/cursor-approve/tests/test_caller_doc_pattern.py
"""

import os
import re
import unittest

DOC = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "docs", "callers", "cursor-approve.md"
    )
)

YAML_BLOCK = re.compile(r"^```yaml$(.*?)^```$", re.M | re.S)
JOB_HEADER = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
# `if:` through to the next key at the same indent (or the next job).
IF_KEY = re.compile(r"^    if:\s*(.*)$")
SAME_LEVEL_KEY = re.compile(r"^    [A-Za-z0-9_-]+:")

# Every job id the pattern documents that must not be skipped by a red
# cursor-review. Named explicitly rather than matched by prefix so a renamed or
# deleted block fails loudly instead of passing vacuously.
START = "approve-start"
AXES = (
    "axis-correctness",
    "axis-conformance",
    "axis-business",
    "axis-design",
    "axis-completeness",
)
DECIDE = "cursor-approve"


def _jobs():
    """{job_id: flattened `if:` expression} over every yaml block in the doc."""
    with open(DOC, encoding="utf-8") as fh:
        text = fh.read()
    out = {}
    for block in YAML_BLOCK.findall(text):
        lines = block.splitlines()
        job = None
        for i, line in enumerate(lines):
            header = JOB_HEADER.match(line)
            if header:
                job = header.group(1)
                continue
            if job is None:
                continue
            key = IF_KEY.match(line)
            if not key:
                continue
            parts = [key.group(1)]
            for cont in lines[i + 1:]:
                if SAME_LEVEL_KEY.match(cont) or JOB_HEADER.match(cont):
                    break
                parts.append(cont.strip())
            # `>-` / `|` block scalars fold to one line; comments are not part
            # of the expression.
            expr = " ".join(
                p for p in parts if p and not p.startswith("#") and p not in (">-", ">", "|", "|-")
            )
            out[job] = re.sub(r"\s+", " ", expr).strip()
    return out


class CallerPattern(unittest.TestCase):
    def setUp(self):
        self.jobs = _jobs()

    def test_every_documented_job_is_present(self):
        for job in (START, DECIDE) + AXES:
            self.assertIn(
                job,
                self.jobs,
                f"{job} has no `if:` in any yaml block of {DOC} — the pattern was "
                "renamed or deleted, so the guards below assert nothing.",
            )

    def test_start_phase_survives_a_red_cursor_review(self):
        expr = self.jobs[START]
        self.assertIn(
            "!cancelled()",
            expr,
            f"{START} must carry `!cancelled()`: a red panel cell puts the "
            f"caller's cursor-review job in `failure` and GitHub's default "
            f"`needs:` rule would skip this job, taking the axes with it. Got: {expr}",
        )
        self.assertNotIn(
            "always()",
            expr,
            f"{START} must use `!cancelled()`, not `always()` — a superseded "
            f"(cancelled) run must never go on to decide. Got: {expr}",
        )

    def test_axes_survive_a_red_cursor_review(self):
        for job in AXES:
            expr = self.jobs[job]
            self.assertIn(
                "!cancelled()",
                expr,
                f"{job} must carry `!cancelled()` for the same reason as "
                f"{START}. Got: {expr}",
            )

    def test_axes_still_require_a_successful_start_phase(self):
        # `!cancelled()` also lifts the implicit "approve-start succeeded"
        # requirement, so that half has to be restated. A start phase that
        # failed to withdraw cursor-review's standing approval must still skip
        # the axes, which decide reads as "no result".
        for job in AXES:
            expr = self.jobs[job]
            self.assertRegex(
                expr,
                r"needs\.approve-start\.result\s*==\s*'success'",
                f"{job} carries `!cancelled()`, which drops the implicit "
                f"requirement that {START} succeeded, so it must restate it as "
                f"`needs.approve-start.result == 'success'`. Got: {expr}",
            )

    def test_every_guarded_job_still_gates_on_approve_gate(self):
        # `approve_gate` is the only authority on whether a round may be
        # decided; it already fails closed (`untrusted`) on a degraded judge,
        # more errored cells than the caller tolerates, an undelivered review,
        # a moved head, or any upstream decision job that did not succeed.
        # Dropping it while keeping `!cancelled()` is what would actually
        # loosen the gate.
        for job in (START, DECIDE) + AXES:
            self.assertIn(
                "needs.cursor-review.outputs.approve_gate",
                self.jobs[job],
                f"{job} must still gate on cursor-review's `approve_gate` "
                f"output. Got: {self.jobs[job]}",
            )

    def test_decide_still_runs_on_always(self):
        # Unchanged by this pattern, and the reason a withheld round still
        # leaves a card: decide must run even when the axes skipped.
        self.assertIn("always()", self.jobs[DECIDE])


if __name__ == "__main__":
    unittest.main()
