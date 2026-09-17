#!/usr/bin/env python3
"""Two-way drift check: declared workflow inputs vs the documented ones.

The same loud alarm BE-4691 asked for, applied to the refresh-reviewers caller
guide. `cursor-review.yml`'s `blocking:` input (and its whole gate job) was
deleted by accident in #31 while its documentation lived on in three places for
weeks — a phantom input in the docs is a broken caller for whoever copies it,
because GitHub rejects an unknown `workflow_call` input at startup with a
zero-job `startup_failure` and no logs. The reverse drift is quieter but real
too: an input added to the workflow and documented nowhere is a knob nobody can
discover. The refresh-reviewers guide had no such pin until now.

So this test pins set equality between:

* `on.workflow_call.inputs` in `.github/workflows/refresh-reviewers.yml`, and
* the "Inputs" table in `docs/callers/refresh-reviewers.md`.

This is deliberately a TWO-set check. `.github/refresh-reviewers/README.md`'s
"Knob defaults (and why)" table is intentionally partial (it omits
`reviewer_config_path`, `map_exclude`, `extra_exclude_paths` and `workflows_ref`)
and uses combined-cell rows (`| `top_k` / `floor` |`), so the bare-name table
regex neither captures nor should capture it — including it would make this test
permanently red or vacuous.

Deliberately parsed WITHOUT PyYAML, like the cursor-review model
(test_workflow_inputs_docs.py): this repo is stdlib-only and CI installs no
requirements. The workflow is uniformly 2-space indented and every input key
sits alone on its 6-space line, which is all the scanners below need — and each
scanner's result is sanity-checked (non-empty, contains `workflows_ref`) so a
parser gone quiet fails instead of passing vacuously.

Run: python3 .github/refresh-reviewers/tests/test_inputs_docs.py
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = os.path.normpath(
    os.path.join(HERE, "..", "..", "workflows", "refresh-reviewers.yml")
)
SETUP_GUIDE = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "docs", "callers", "refresh-reviewers.md")
)

# An input declaration: the key alone on its 6-space line, directly under
# `    inputs:`. Sub-keys of an input (description/type/default) are 8-space,
# and folded description text deeper still, so none of them match.
INPUT_KEY = re.compile(r"^      ([A-Za-z0-9_-]+):\s*$")
# A table row's first cell: `| `name` | ...`.
TABLE_KEY = re.compile(r"^\|\s*`([A-Za-z0-9_-]+)`\s*\|")
HEADING = re.compile(r"^#{2,}\s")


def read_lines(path):
    # Tolerate CRLF: split on \n and drop a trailing \r so indent-anchored
    # matches (dedent break, INPUT_KEY) and `lines.index("jobs:")` are not
    # thrown off by a stray carriage return.
    with open(path, encoding="utf-8") as f:
        return [line.rstrip("\r") for line in f.read().split("\n")]


def workflow_inputs():
    """Input names declared under on.workflow_call.inputs."""
    lines = read_lines(WORKFLOW)
    # Constrain to the pre-`jobs:` header so a 6-space key inside some job's
    # step mapping can never register as an input.
    if "jobs:" not in lines:
        raise AssertionError(
            f"no top-level `jobs:` line in {WORKFLOW} — file moved or its "
            "structure changed; the input scan cannot be bounded"
        )
    head = lines[: lines.index("jobs:")]
    names, in_inputs = set(), False
    for line in head:
        if line == "    inputs:":
            in_inputs = True
            continue
        if in_inputs and (not line.strip() or line.lstrip().startswith("#")):
            # Blank or comment lines carry no indentation signal: a 4-space
            # comment is legal YAML inside `inputs:` and must not trip the
            # dedent break and silently truncate the scan.
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


class RefreshReviewersInputsDocsTest(unittest.TestCase):
    def setUp(self):
        self.declared = workflow_inputs()
        self.documented = documented_inputs(SETUP_GUIDE, "## Inputs")
        # Guard the parsers: if either scanner silently stopped matching, the
        # equality assertions below would compare empty sets and pass.
        for label, found in (
            ("workflow declaration", self.declared),
            ("setup guide inputs table", self.documented),
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
        phantom = self.documented - self.declared
        self.assertFalse(
            phantom,
            "`docs/callers/refresh-reviewers.md` inputs table documents inputs "
            f"refresh-reviewers.yml does not declare: {sorted(phantom)} — "
            "deleting an input is a docs change too",
        )

    def test_every_declared_input_is_documented(self):
        missing = self.declared - self.documented
        self.assertFalse(
            missing,
            "refresh-reviewers.yml declares inputs missing from "
            f"`docs/callers/refresh-reviewers.md` inputs table: {sorted(missing)}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
