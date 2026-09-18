#!/usr/bin/env python3
"""Structural regression tests for cursor-review.yml's own concurrency group.

The reusable owns a workflow-level `concurrency:` group so that every caller
picks it up at its next pin bump, with no caller edit. Its whole point is the
SLOT rule: the trigger label and the `skip-cursor-review` veto label resolve to
the SAME `trigger` slot, which is what makes applying `skip-cursor-review`
mid-flight cancel the running panel — the Gate's documented
"skip-cursor-review wins" precedence, made true for in-flight runs and not only
for runs that have yet to start. Every other label gets its own
`format('label-{0}', …)` slot so an unrelated label add never kills a running
review.

None of that is visible in a diff: splitting the veto label into its own slot,
dropping `cancel-in-progress`, or renaming the group to something a caller
would plausibly also pick (which deadlocks that caller's run — it holds the
group while its `uses:` job waits on it) all leave a workflow that parses,
lints and runs. So the shape is pinned here.

Deliberately parsed WITHOUT PyYAML, for the same reason
`test_workflow_job_isolation.py` is: this repo is stdlib-only and CI installs
no requirements for this suite, so a `yaml` import would simply not run.

Run: python3 .github/cursor-review/tests/test_workflow_concurrency.py
"""

import glob
import os
import re
import unittest

WORKFLOWS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "workflows")
)
WORKFLOW = os.path.join(WORKFLOWS_DIR, "cursor-review.yml")

# The group name prefix. Deliberately NOT something a caller would pick: a
# caller declaring the reusable's own group deadlocks its own run.
GROUP_PREFIX = "cursor-review-reusable-"

COMMENT = re.compile(r"^\s*#")
TOP_LEVEL_KEY = re.compile(r"^([A-Za-z0-9_-]+):")


def read_workflow(path=WORKFLOW):
    with open(path, encoding="utf-8") as f:
        return f.read().split("\n")


def top_level_block(lines, key):
    """The lines of the top-level `<key>:` mapping, comments dropped.

    Returns None when the key is absent. Comments are dropped so a comment that
    merely MENTIONS `cancel-in-progress` or the group name can never satisfy an
    assertion below — the same trap `code_lines` guards in the sibling suite.
    """
    body, inside = [], False
    for line in lines:
        if inside:
            if line.strip() and not line.startswith(" "):
                break  # dedented back to another top-level key
            if not COMMENT.match(line):
                body.append(line)
            continue
        match = TOP_LEVEL_KEY.match(line)
        if match and match.group(1) == key:
            inside = True
    return body if inside else None


def mapping_value(block, key):
    """The scalar value of `  <key>:` in a top-level block, or None."""
    prefix = "  %s:" % key
    for line in block:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def trigger_disjunction(group):
    """The parenthesised condition immediately left of `&& 'trigger'`, or None.

    Balanced-paren walk rather than a regex: the condition nests (the
    run_without_label arm carries its own parenthesised action list), and a
    fixed-depth regex silently stops matching the moment someone adds another
    level — reporting "the slot rule is gone" when it is merely nested deeper.
    """
    marker = re.search(r"\)\s*&&\s*'trigger'", group)
    if not marker:
        return None
    close = group.index(")", marker.start())
    depth = 0
    for i in range(close, -1, -1):
        if group[i] == ")":
            depth += 1
        elif group[i] == "(":
            depth -= 1
            if depth == 0:
                return group[i + 1:close]
    return None


def top_level_or_operands(disjunction):
    """`disjunction` split on its TOP-LEVEL `||`, each operand unwrapped once.

    Substring containment over the whole disjunction is not enough to pin the
    slot rule: moving the veto comparison INTO the nested
    `(inputs.run_without_label && …)` arm, or swapping the `||` between the two
    label comparisons for an `&&`, keeps every substring present while the veto
    label stops reaching `trigger` on a label-gated caller. Both mutations
    change the top-level operand LIST, so that is what the tests below assert
    on. Depth tracking is what makes it a top-level split — the
    run_without_label arm carries `||`s of its own, nested one level deeper.
    """
    operands, depth, start = [], 0, 0
    i = 0
    while i < len(disjunction):
        char = disjunction[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0 and disjunction[i:i + 2] == "||":
            operands.append(disjunction[start:i])
            i += 2
            start = i
            continue
        i += 1
    operands.append(disjunction[start:])
    return [unwrap(o) for o in operands]


def unwrap(operand):
    """`operand` stripped, with ONE redundant enclosing paren pair removed."""
    operand = operand.strip()
    if operand.startswith("(") and operand.endswith(")"):
        depth = 0
        for i, char in enumerate(operand):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and i != len(operand) - 1:
                    return operand  # the parens are not a single outer pair
        return operand[1:-1].strip()
    return operand


class WorkflowConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.block = top_level_block(read_workflow(), "concurrency")
        self.assertIsNotNone(
            self.block,
            "cursor-review.yml has no workflow-level `concurrency:` key — "
            "without it skip-cursor-review cannot cancel an in-flight panel",
        )
        self.group = mapping_value(self.block, "group")
        self.assertIsNotNone(self.group, "`concurrency:` declares no `group:`")

    def test_cancel_in_progress_is_true(self):
        # A group with cancel-in-progress false QUEUES the veto behind the panel
        # it is meant to kill, which is worse than no group at all.
        self.assertEqual(
            mapping_value(self.block, "cancel-in-progress"),
            "true",
            "`concurrency.cancel-in-progress` must be true — the veto cancels "
            "the in-flight panel, it does not queue behind it",
        )

    def test_group_is_namespaced_away_from_caller_groups(self):
        self.assertTrue(
            self.group.startswith(GROUP_PREFIX),
            "the group must start with %r so no caller picks the same name; "
            "got %r" % (GROUP_PREFIX, self.group),
        )

    def test_group_is_keyed_per_pr(self):
        # A group that is not per-PR serializes/cancels ACROSS PRs.
        self.assertIn("github.event.pull_request.number", self.group)

    def test_trigger_label_and_veto_label_share_one_slot(self):
        # The assertion this suite exists for. Both labels must be TOP-LEVEL
        # alternatives of the same `&& 'trigger'` disjunction — one of them
        # moved into its own slot (the shape draft PR #58 used), or demoted
        # into the nested run_without_label arm, or `||`-to-`&&`'d, silently
        # restores the bug while leaving both substrings in the expression.
        disjunction = trigger_disjunction(self.group)
        self.assertIsNotNone(
            disjunction,
            "no `(...) && 'trigger'` disjunction in the group expression: %r"
            % self.group,
        )
        operands = top_level_or_operands(disjunction)

        trigger_arms = [o for o in operands if "inputs.review_label" in o]
        self.assertEqual(
            len(trigger_arms),
            1,
            "expected exactly ONE top-level `||` operand comparing against "
            "inputs.review_label; got %r from %r" % (operands, disjunction),
        )
        self.assertIn(
            "github.event.label.name == inputs.review_label",
            trigger_arms[0],
            "the trigger label's operand does not compare label.name to "
            "inputs.review_label: %r" % trigger_arms[0],
        )

        veto_arms = [o for o in operands if "'skip-cursor-review'" in o]
        self.assertEqual(
            len(veto_arms),
            1,
            "the skip-cursor-review veto label must be its OWN top-level `||` "
            "operand of the `trigger` disjunction — nested inside another arm "
            "(or joined with `&&`) it no longer reaches `trigger` on a "
            "label-gated caller, and applying it mid-flight cancels nothing; "
            "got %r from %r" % (operands, disjunction),
        )
        self.assertEqual(
            veto_arms[0],
            "github.event.label.name == 'skip-cursor-review'",
            "the veto operand must be an unconditional label comparison, not "
            "%r — any extra conjunct is a condition under which the veto "
            "silently stops cancelling the panel" % veto_arms[0],
        )
        self.assertIsNot(
            veto_arms[0],
            trigger_arms[0],
            "the trigger and veto comparisons collapsed into one operand",
        )

    def test_trigger_label_disjunct_is_guarded_against_an_empty_review_label(self):
        # `review_label` is `required: false`, so a caller can pass ''. GitHub
        # coerces a MISSING `github.event.label` and '' alike in a mixed
        # comparison, so an unguarded `label.name == inputs.review_label` is
        # TRUE on every non-label event — `pull_request_review_thread` included
        # — dragging them into `trigger`, where resolving a thread cancels a
        # running panel.
        disjunction = trigger_disjunction(self.group)
        trigger_arms = [
            o for o in top_level_or_operands(disjunction)
            if "inputs.review_label" in o
        ]
        self.assertTrue(
            trigger_arms and "inputs.review_label != \'\'" in trigger_arms[0],
            "the trigger-label operand must be guarded by "
            "`inputs.review_label != \'\'`; got %r" % (trigger_arms or None,),
        )

    def test_every_other_label_gets_its_own_namespaced_slot(self):
        # Without the format() namespacing, a label literally named `trigger`
        # (or `Trigger` — group names are case-insensitive) would reach the
        # shared slot and cancel a running panel.
        self.assertIn(
            "format('label-{0}'",
            self.group,
            "the non-trigger branch must namespace the label name via "
            "format('label-{0}', …)",
        )

    def test_no_other_workflow_shares_the_reusable_group(self):
        # The deadlock guard. Any other workflow here — above all this repo's
        # own ci-cursor-review.yml caller — declaring the same group would hold
        # it while its `uses:` job waits to acquire it, hanging until timeout.
        offenders = []
        # BOTH extensions: GitHub loads `.yaml` workflows too, so a caller
        # added here as `.yaml` would otherwise pass this guard and still
        # deadlock its own run.
        paths = glob.glob(os.path.join(WORKFLOWS_DIR, "*.yml"))
        paths += glob.glob(os.path.join(WORKFLOWS_DIR, "*.yaml"))
        for path in sorted(paths):
            if os.path.abspath(path) == os.path.abspath(WORKFLOW):
                continue
            with open(path, encoding="utf-8") as f:
                if GROUP_PREFIX in f.read():
                    offenders.append(os.path.basename(path))
        self.assertEqual(
            offenders,
            [],
            "these workflows reference %r, which deadlocks a caller against "
            "the reusable's own group: %s" % (GROUP_PREFIX, ", ".join(offenders)),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
