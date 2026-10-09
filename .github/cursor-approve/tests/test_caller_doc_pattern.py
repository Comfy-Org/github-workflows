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

The guards are pinned as EXACT top-level conjuncts, not substrings: a `||` in
place of an `&&`, an `approve_gate != 'pass'`, or a bare truthy
`approve_gate` (which `fail`/`untrusted` would satisfy) all keep every
substring and all fail open. Every occurrence of a job across every yaml block
is checked, so a stale second copy cannot hide behind a fixed first one.

docs/callers/cursor-review.md's `next:` example is pinned too: it is the
canonical "chain a job off approve_gate" snippet and fails the same way.

Deliberately parsed WITHOUT PyYAML, like its sibling suites: this repo is
stdlib-only and CI installs no requirements for these tests.

Run: python3 .github/cursor-approve/tests/test_caller_doc_pattern.py
"""

import os
import re
import unittest

DOCS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs", "callers")
)
APPROVE_DOC = os.path.join(DOCS_DIR, "cursor-approve.md")
REVIEW_DOC = os.path.join(DOCS_DIR, "cursor-review.md")

YAML_BLOCK = re.compile(r"^```yaml$(.*?)^```$", re.M | re.S)
JOB_HEADER = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
IF_KEY = re.compile(r"^    if:\s*(.*)$")

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
NEXT = "next"

GATE = "needs.cursor-review.outputs.approve_gate"
START_CONJUNCTS = [
    "!cancelled()",
    f"({GATE} == 'pass' || {GATE} == 'capped')",
]
AXIS_CONJUNCTS = [
    "!cancelled()",
    "needs.approve-start.result == 'success'",
    f"{GATE} == 'pass'",
]
NEXT_CONJUNCTS = ["!cancelled()", f"{GATE} == 'pass'"]


def _jobs(path):
    """{job_id: [flattened `if:` expression, ...]} over every yaml block.

    A list per job, one entry per occurrence, so a job documented in two blocks
    is checked in both rather than the later copy silently winning.
    """
    with open(path, encoding="utf-8") as fh:
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
            first = key.group(1).strip()
            parts = []
            if first in (">-", ">", "|", "|-"):
                # A block scalar's content is everything indented deeper than
                # the key. A `#` line in there is CONTENT, not a comment, so it
                # is kept and then fails the exact-conjunct checks below.
                for cont in lines[i + 1:]:
                    if cont.strip() and len(cont) - len(cont.lstrip()) <= 4:
                        break
                    parts.append(cont.strip())
            else:
                parts.append(first)
            expr = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
            wrapped = re.fullmatch(r"\$\{\{(.*)\}\}", expr)
            if wrapped:
                expr = wrapped.group(1).strip()
            out.setdefault(job, []).append(expr)
    return out


def _conjuncts(expr):
    """Split on top-level `&&`; a top-level `||` is returned as a failure marker.

    Parenthesised groups are kept whole, so `a && (b || c)` is two conjuncts.
    """
    out, depth, cur, i = [], 0, "", 0
    while i < len(expr):
        ch = expr[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and expr.startswith("&&", i):
            out.append(cur.strip())
            cur, i = "", i + 2
            continue
        if depth == 0 and expr.startswith("||", i):
            return None
        cur += ch
        i += 1
    out.append(cur.strip())
    return out


class CallerPattern(unittest.TestCase):
    def setUp(self):
        self.jobs = _jobs(APPROVE_DOC)

    def _assert_conjuncts(self, job, exprs, expected):
        for expr in exprs:
            got = _conjuncts(expr)
            self.assertIsNotNone(
                got,
                f"{job}: a top-level `||` in its `if:` lets any one side run it "
                f"on its own, which fails open. Got: {expr}",
            )
            self.assertEqual(
                sorted(got),
                sorted(expected),
                f"{job}: `if:` must be exactly the conjunction of {expected}. "
                f"`!cancelled()` keeps a red panel cell from skipping it; the "
                f"rest restate what `!cancelled()` drops and keep the "
                f"`approve_gate` comparison exact. Got: {expr}",
            )

    def test_every_documented_job_is_present(self):
        for job in (START, DECIDE) + AXES:
            self.assertIn(
                job,
                self.jobs,
                f"{job} has no `if:` in any yaml block of {APPROVE_DOC} — the "
                "pattern was renamed or deleted, so the guards below assert nothing.",
            )

    def test_start_phase_survives_a_red_cursor_review(self):
        # `!cancelled()`, never `always()`: a superseded (cancelled) run must
        # never go on to decide. The exact-conjunct check rejects both a
        # missing guard and an `always()` in its place.
        self._assert_conjuncts(START, self.jobs[START], START_CONJUNCTS)

    def test_axes_survive_a_red_cursor_review_and_still_need_start(self):
        # `!cancelled()` also lifts the implicit "approve-start succeeded"
        # requirement, so that half has to be restated. A start phase that
        # failed to withdraw cursor-review's standing approval must still skip
        # the axes, which decide reads as "no result". And `approve_gate` is
        # the only authority on whether a round may be decided; dropping or
        # loosening it while keeping `!cancelled()` is what would actually
        # loosen the gate.
        for job in AXES:
            self._assert_conjuncts(job, self.jobs[job], AXIS_CONJUNCTS)

    def test_decide_still_runs_on_always_and_gates_on_pass(self):
        # Unchanged by this pattern, and the reason a withheld round still
        # leaves a card: decide must run even when the axes skipped.
        self._assert_conjuncts(
            DECIDE, self.jobs[DECIDE], ["always()", f"{GATE} == 'pass'"]
        )

    def test_cursor_review_doc_next_example_survives_a_red_cursor_review(self):
        jobs = _jobs(REVIEW_DOC)
        self.assertIn(
            NEXT,
            jobs,
            f"the `{NEXT}:` approve_gate example is gone from {REVIEW_DOC}; "
            "update this test to follow it rather than letting it pass vacuously.",
        )
        self._assert_conjuncts(NEXT, jobs[NEXT], NEXT_CONJUNCTS)


class Parser(unittest.TestCase):
    """The parser has to see the fail-open shapes the guards exist to catch."""

    def test_top_level_or_is_rejected(self):
        self.assertIsNone(_conjuncts(f"!cancelled() || {GATE} == 'pass'"))

    def test_parenthesised_or_stays_one_conjunct(self):
        self.assertEqual(
            _conjuncts(START_CONJUNCTS[0] + " && " + START_CONJUNCTS[1]),
            START_CONJUNCTS,
        )

    def test_bare_truthy_gate_is_not_the_comparison(self):
        self.assertNotEqual(
            sorted(_conjuncts(f"!cancelled() && {GATE}")), sorted(NEXT_CONJUNCTS)
        )


if __name__ == "__main__":
    unittest.main()
