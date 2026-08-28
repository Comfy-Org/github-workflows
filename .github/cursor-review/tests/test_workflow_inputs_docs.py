#!/usr/bin/env python3
"""Two-way drift check: declared workflow inputs vs the documented ones.

This is the loud alarm BE-4691 asked for. `cursor-review.yml`'s `blocking:`
input (and its whole Blocking gate job) was deleted by accident in #31 while
its documentation lived on in three places for weeks — a phantom input in the
docs is a broken caller for whoever copies it, because GitHub rejects an
unknown `workflow_call` input at startup with a zero-job `startup_failure` and
no logs. The reverse drift is quieter but real too: an input added to the
workflow and documented nowhere is a knob nobody can discover.

So this test pins set equality between:

* `on.workflow_call.inputs` in `.github/workflows/cursor-review.yml`,
* the "Configuration knobs" table in `.github/cursor-review/README.md`, and
* the "Inputs" table in `docs/callers/cursor-review.md`.

Deliberately parsed WITHOUT PyYAML, like test_workflow_job_isolation.py: this
repo is stdlib-only and CI installs no requirements. The workflow is uniformly
2-space indented and every input key sits alone on its 6-space line, which is
all the scanners below need — and each scanner's result is sanity-checked
(non-empty, contains `workflows_ref`) so a parser gone quiet fails instead of
passing vacuously.

Run: python3 .github/cursor-review/tests/test_workflow_inputs_docs.py
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = os.path.normpath(
    os.path.join(HERE, "..", "..", "workflows", "cursor-review.yml")
)
DIR_README = os.path.normpath(os.path.join(HERE, "..", "README.md"))
SETUP_GUIDE = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "docs", "callers", "cursor-review.md")
)

# An input declaration: the key alone on its 6-space line, directly under
# `    inputs:`. Sub-keys of an input (description/type/default) are 8-space,
# and folded description text deeper still, so none of them match.
INPUT_KEY = re.compile(r"^      ([A-Za-z0-9_-]+):\s*$")
# A knob-table row's first cell: `| `name` | ...`. Other tables' first cells
# are links or dotted names (`secrets.CURSOR_API_KEY`), which don't match the
# bare-name-then-closing-backtick shape — but the scans below still confine
# themselves to the named section so a future table can't leak in.
TABLE_KEY = re.compile(r"^\|\s*`([A-Za-z0-9_-]+)`\s*\|")
HEADING = re.compile(r"^#{2,3}\s")


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return f.read().split("\n")


def workflow_inputs():
    """Input names declared under on.workflow_call.inputs."""
    lines = read_lines(WORKFLOW)
    # Constrain to the pre-`jobs:` header so a 6-space key inside some job's
    # step mapping can never register as an input.
    head = lines[: lines.index("jobs:")]
    names, in_inputs = set(), False
    for line in head:
        if line == "    inputs:":
            in_inputs = True
            continue
        if in_inputs and re.match(r"^    \S", line):  # dedent: secrets:, etc.
            break
        if in_inputs:
            match = INPUT_KEY.match(line)
            if match:
                names.add(match.group(1))
    return names


def documented_inputs(path, heading):
    """First-cell backticked names of the table under `heading` in `path`."""
    names, in_section = set(), False
    for line in read_lines(path):
        if line.strip() == heading:
            in_section = True
            continue
        if in_section and HEADING.match(line):
            break
        if in_section:
            match = TABLE_KEY.match(line)
            if match:
                names.add(match.group(1))
    return names


class WorkflowInputsDocsTest(unittest.TestCase):
    def setUp(self):
        self.declared = workflow_inputs()
        self.dir_readme = documented_inputs(DIR_README, "## Configuration knobs")
        self.setup_guide = documented_inputs(SETUP_GUIDE, "## Inputs")
        # Guard the parsers: if any scanner silently stopped matching, the
        # equality assertions below would compare empty sets and pass.
        for label, found in (
            ("workflow declaration", self.declared),
            ("directory README knobs table", self.dir_readme),
            ("setup guide inputs table", self.setup_guide),
        ):
            self.assertIn(
                "workflows_ref",
                found,
                f"the {label} scanner lost `workflows_ref` — parser or file "
                "structure changed, every assertion here is now vacuous",
            )

    def test_every_documented_input_is_declared(self):
        # The #31 failure mode: docs outliving a deleted input. A caller who
        # copies a phantom input gets a zero-job startup_failure with no logs.
        for label, found in (
            ("`.github/cursor-review/README.md` knobs table", self.dir_readme),
            ("`docs/callers/cursor-review.md` inputs table", self.setup_guide),
        ):
            phantom = found - self.declared
            self.assertFalse(
                phantom,
                f"{label} documents inputs cursor-review.yml does not declare: "
                f"{sorted(phantom)} — deleting an input is a docs change too",
            )

    def test_every_declared_input_is_documented_in_both_tables(self):
        for label, found in (
            ("`.github/cursor-review/README.md` knobs table", self.dir_readme),
            ("`docs/callers/cursor-review.md` inputs table", self.setup_guide),
        ):
            missing = self.declared - found
            self.assertFalse(
                missing,
                f"cursor-review.yml declares inputs missing from {label}: "
                f"{sorted(missing)}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
