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
        # The assertion this suite exists for. Both labels must sit in the SAME
        # `&& 'trigger'` disjunction — one of them moved into its own slot (the
        # shape draft PR #58 used) silently restores the bug.
        disjunction = trigger_disjunction(self.group)
        self.assertIsNotNone(
            disjunction,
            "no `(...) && 'trigger'` disjunction in the group expression: %r"
            % self.group,
        )
        self.assertIn(
            "inputs.review_label",
            disjunction,
            "the trigger label is not in the `trigger` slot's disjunction",
        )
        self.assertIn(
            "'skip-cursor-review'",
            disjunction,
            "the skip-cursor-review veto label is not in the SAME disjunction "
            "as the trigger label — applying it mid-flight would land in a "
            "different slot and cancel nothing",
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
        for path in sorted(glob.glob(os.path.join(WORKFLOWS_DIR, "*.yml"))):
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
