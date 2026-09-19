#!/usr/bin/env python3
"""Structural regression tests for cursor-review.yml's `workflows_ref` guard.

Every job that checks this directory out runs a `Require a pinned workflows_ref`
step, and those steps are COPY-PASTED on purpose: `workflow_call` has no
anchors, no `uses: ./…` for a step, and no job-level `run:`, so eight literal
copies is the only spelling available. Copies drift — one job gets a hardening
the other seven miss, and the hole is invisible in a diff because the file still
contains the guard. So the copies are pinned byte-for-byte here instead.

What this suite asserts:

  * exactly EIGHT guard steps. The count is asserted, not derived, so adding a
    ninth guarded job is an explicit opt-in that makes an author read this file;
  * all eight `env:` + `run:` blocks are byte-identical to each other;
  * the block carries the BE-15927 equality guard — the `job.workflow_sha`
    binding, the mismatch comparison, and a hard `exit` in that branch. Empty
    and 40-hex are covered by `check_workflow_pins.py`; equality is not, because
    that lint only judges whether a ref is non-empty and immutable; and
  * the exempt `Prior-review ledger` resolve step ALSO reads `job.workflow_sha`
    and does NOT exit on a mismatch. That job must never fail (the review matrix
    `needs:` it), so an `exit` creeping in there would take every review down.

Deliberately parsed WITHOUT PyYAML, like test_workflow_job_isolation.py next
door: this repo is stdlib-only and CI installs no requirements for this suite,
so a yaml import would simply not run. A round-trip would also destroy exactly
what is under test — this is a test about the literal bytes of eight blocks.

Run: python3 .github/cursor-review/tests/test_workflow_ref_guard_parity.py
"""

import os
import re
import unittest

WORKFLOW = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml"
    )
)

# The guard steps. `.strip()`-compared so the assertion is about the step name,
# not about the indentation of whichever job happens to hold it.
GUARD_STEP_NAME = "- name: Require a pinned workflows_ref"
EXPECTED_GUARDS = 8

# The ledger's exempt resolver, addressed by its step id rather than its name so
# a comment rewrite above it cannot move the anchor.
LEDGER_STEP_ID = "id: resolve_ref"

WORKFLOW_SHA_BINDING = "WORKFLOW_SHA: ${{ job.workflow_sha }}"
EXIT_RE = re.compile(r"^\s*exit\s+[1-9]", re.MULTILINE)


def read_lines():
    with open(WORKFLOW, encoding="utf-8") as fh:
        return fh.read().split("\n")


def step_block(lines, start):
    """The lines of the step opening at `lines[start]`, up to the next sibling.

    A step ends at the first non-blank line indented no deeper than its own `-`.
    Blank lines inside a step (this workflow separates comment paragraphs with
    them) are only a terminator when what follows them dedents, so they are
    looked past rather than treated as the end.
    """
    indent = len(lines[start]) - len(lines[start].lstrip())
    i = start + 1
    while i < len(lines):
        if not lines[i].strip():
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines) or (len(lines[j]) - len(lines[j].lstrip())) <= indent:
                break
            i = j
            continue
        if (len(lines[i]) - len(lines[i].lstrip())) <= indent:
            break
        i += 1
    return lines[start:i]


def guard_blocks():
    """The `env:` + `run:` body of every guard step, one string each.

    The step's leading comment block is dropped on purpose: parity is about the
    BEHAVIOUR being identical. Keeping the prose in would make a per-job comment
    (a legitimate thing to want) fail a test that exists to catch a per-job
    SCRIPT.
    """
    lines = read_lines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == GUARD_STEP_NAME]
    blocks = []
    for start in starts:
        block = step_block(lines, start)
        env_at = next(
            (i for i, ln in enumerate(block) if ln.strip() == "env:"),
            None,
        )
        assert env_at is not None, "guard step at line %d has no env: block" % (
            start + 1,
        )
        blocks.append("\n".join(ln.strip() for ln in block[env_at:]))
    return starts, blocks


def ledger_resolve_block():
    lines = read_lines()
    hit = [i for i, ln in enumerate(lines) if ln.strip() == LEDGER_STEP_ID]
    assert len(hit) == 1, "expected exactly one `%s` step" % LEDGER_STEP_ID
    # Walk back to the `- name:` line that opens the step holding that id.
    start = hit[0]
    while start >= 0 and not lines[start].lstrip().startswith("- name:"):
        start -= 1
    assert start >= 0, "`%s` is not inside a named step" % LEDGER_STEP_ID
    return "\n".join(step_block(lines, start))


class GuardParityTest(unittest.TestCase):
    def test_exactly_eight_guard_steps(self):
        starts, _ = guard_blocks()
        self.assertEqual(
            len(starts),
            EXPECTED_GUARDS,
            "cursor-review.yml has %d `%s` steps, expected %d. A new guarded job "
            "is welcome — bump EXPECTED_GUARDS here and confirm its guard is a "
            "byte-for-byte copy of the others. A REMOVED one is the case this "
            "count exists to catch." % (len(starts), GUARD_STEP_NAME, EXPECTED_GUARDS),
        )

    def test_all_guard_blocks_are_byte_identical(self):
        starts, blocks = guard_blocks()
        for line_no, block in zip(starts[1:], blocks[1:]):
            self.assertEqual(
                block,
                blocks[0],
                "the guard step at line %d differs from the one at line %d. The "
                "eight copies are load-bearing: a hardening applied to one job "
                "and not the rest leaves the others silently weaker."
                % (line_no + 1, starts[0] + 1),
            )

    def test_guard_binds_job_workflow_sha_through_env(self):
        _, blocks = guard_blocks()
        self.assertIn(WORKFLOW_SHA_BINDING, blocks[0])
        # Through `env:`, never interpolated into the script body — the same
        # rule `check_workflow_pins.py` enforces for WORKFLOWS_REF, and for the
        # same reason: an interpolated value is substituted into the shell
        # source before it runs.
        self.assertNotIn("${{ job.workflow_sha }}", blocks[0].split("run: |")[1])

    def test_guard_binds_the_bare_input_not_the_fallback(self):
        # `check_workflow_pins.py` reads the STRENGTH of a guard off this
        # binding: `inputs.workflows_ref || job.workflow_sha` proves only that
        # the OR expression is non-empty, so a guard written that way would stop
        # covering the sibling `ref: ${{ inputs.workflows_ref }}` checkouts in
        # the same job. These jobs are not exempt; keep the binding bare.
        _, blocks = guard_blocks()
        self.assertIn("WORKFLOWS_REF: ${{ inputs.workflows_ref }}", blocks[0])
        self.assertNotIn("inputs.workflows_ref || job.workflow_sha", blocks[0])

    def test_guard_fails_on_a_mismatch(self):
        _, blocks = guard_blocks()
        body = blocks[0]
        self.assertIn('elif [ "$REF_LC" != "$SHA" ]; then', body)
        tail = body.split('elif [ "$REF_LC" != "$SHA" ]; then', 1)[1]
        # The exit must sit in THAT branch — before the `fi` that closes it.
        branch = tail.split("\nfi", 1)[0]
        self.assertRegex(
            branch,
            EXIT_RE,
            "the mismatch branch does not exit non-zero, so a split pin would "
            "only annotate the run and the review would proceed anyway",
        )

    def test_guard_only_warns_when_job_workflow_sha_is_unavailable(self):
        # A runner older than v2.334.0 supplies no `job.workflow_sha`. That is
        # "could not evaluate", not "mismatch" — failing there would take the
        # review down over a property nothing measured. (public-repo-hygiene.yml
        # fails closed in the same spot on purpose; see the comment there.)
        _, blocks = guard_blocks()
        empty_branch = blocks[0].split('if [ -z "$SHA" ]; then', 1)[1].split(
            "\nelif", 1
        )[0]
        self.assertIn("::warning::", empty_branch)
        self.assertNotRegex(empty_branch, EXIT_RE)

    def test_guard_compares_case_insensitively(self):
        # Hex is case-insensitive and the 40-hex shape test here only WARNS, so
        # an upper-case-but-CORRECT ref reaches the comparison. Normalising only
        # $SHA (which is what public-repo-hygiene.yml does, because its shape
        # test hard-fails first) would fail all eight jobs of a caller whose two
        # pins agree.
        _, blocks = guard_blocks()
        self.assertIn("REF_LC=\"$(printf '%s' \"$REF\" | tr 'A-Z' 'a-z')\"", blocks[0])


class LedgerExemptionTest(unittest.TestCase):
    def test_ledger_resolve_reads_job_workflow_sha(self):
        self.assertIn(WORKFLOW_SHA_BINDING, ledger_resolve_block())

    def test_ledger_warns_on_a_mismatch(self):
        block = ledger_resolve_block()
        self.assertIn('[ "$REF_LC" != "$SHA" ]', block)
        mismatch = block.split('[ "$REF_LC" != "$SHA" ]', 1)[1]
        self.assertIn("::warning::", mismatch.split("\n          fi", 1)[0])

    def test_ledger_never_exits(self):
        # The review matrix `needs:` this job, so ANY non-zero exit here skips
        # every review job — the exact failure the exemption exists to prevent.
        # `continue-on-error: true` on the step covers a crash, not a deliberate
        # exit that a later refactor might move out from under it.
        self.assertNotRegex(ledger_resolve_block(), EXIT_RE)

    def test_ledger_keeps_its_fallback_binding(self):
        # The ledger's WORKFLOWS_REF is the one place the `|| job.workflow_sha`
        # fallback is sanctioned (it is why this job is exempt from the guard).
        # Losing it would make an omitted input resolve to '' and skip the asset
        # checkout on every run.
        self.assertIn(
            "WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}",
            ledger_resolve_block(),
        )


if __name__ == "__main__":
    unittest.main()
