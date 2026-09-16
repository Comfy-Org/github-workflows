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
README = os.path.normpath(os.path.join(HERE, "..", "README.md"))

# An input declaration: the key alone on its 6-space line, directly under
# `    inputs:`. Sub-keys of an input (description/type/default) are 8-space,
# and folded description text deeper still, so none of them match.
INPUT_KEY = re.compile(r"^      ([A-Za-z0-9_-]+):\s*$")
# A table row's first cell: `| `name` | ...`.
TABLE_KEY = re.compile(r"^\|\s*`([A-Za-z0-9_-]+)`\s*\|")
HEADING = re.compile(r"^#{2,}\s")
# An input's `default:` / `required:` sub-keys — 8-space, one level under the
# 6-space input key — used to pin the guide's Default column to the real value.
INPUT_DEFAULT = re.compile(r"^        default:\s*(.*?)\s*$")
INPUT_REQUIRED = re.compile(r"^        required:\s*(\S+)\s*$")
# A `with:` mapping in an example caller (at any indent), and a mapping key
# directly under it. A placeholder scalar VALUE (`<same-sha>`, a login) never
# starts a line, so MAPPING_KEY never mistakes one for an input.
WITH_LINE = re.compile(r"^(\s*)with:\s*$")
MAPPING_KEY = re.compile(r"^\s*([A-Za-z0-9_-]+):")


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


def strip_comment_prefix(line):
    """Drop the leading `# ` from a full-line YAML comment while preserving the
    example's own indentation, so relative-indent parsing still works. A bare
    `#` becomes empty."""
    if line.startswith("# "):
        return line[2:]
    if line == "#":
        return ""
    return line


def fenced_blocks(lines):
    """Content lines of every ``` fenced code block, one list per block."""
    blocks, current, inside = [], [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            if inside:
                blocks.append(current)
                current = []
            inside = not inside
            continue
        if inside:
            current.append(line)
    return blocks


def with_keys(lines):
    """Mapping keys directly under each `with:` in `lines`.

    Relative-indentation only, no YAML parser: the first non-blank line after a
    `with:` fixes the child indent, and keys at exactly that indent are
    collected until the block dedents back to (or past) the `with:` line. So
    `uses:` / `secrets:`, which sit at the `with:` indent, end the block rather
    than count as inputs.
    """
    keys = set()
    i, n = 0, len(lines)
    while i < n:
        opener = WITH_LINE.match(lines[i])
        if not opener:
            i += 1
            continue
        base = len(opener.group(1))
        child = None
        j = i + 1
        while j < n:
            line = lines[j]
            if not line.strip() or line.lstrip().startswith("#"):
                j += 1
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base:  # dedent to a sibling (secrets:, next job key)
                break
            if child is None:
                child = indent
            if indent == child:
                key = MAPPING_KEY.match(line)
                if key:
                    keys.add(key.group(1))
            j += 1
        i = j
    return keys


def example_with_keys():
    """`with:` keys from the two example callers this repo ships: the fenced
    YAML under the guide's ## Caller heading, and the `# `-prefixed example in
    the workflow header comment. Returns {source_label: set_of_keys}."""
    guide_lines = []
    for block in fenced_blocks(read_lines(SETUP_GUIDE)):
        guide_lines.extend(block)
    header_comment = [
        strip_comment_prefix(line)
        for line in read_lines(WORKFLOW)
        if line.startswith("#")
    ]
    return {
        "docs/callers/refresh-reviewers.md ## Caller": with_keys(guide_lines),
        "refresh-reviewers.yml header comment": with_keys(header_comment),
    }


def workflow_input_defaults():
    """(`{name: raw default:}`, `{names marked required: true}`) from the
    workflow. Required inputs carry no default (`workflows_ref`)."""
    lines = read_lines(WORKFLOW)
    if "jobs:" not in lines:
        raise AssertionError(
            f"no top-level `jobs:` line in {WORKFLOW} — file moved or its "
            "structure changed; the input scan cannot be bounded"
        )
    head = lines[: lines.index("jobs:")]
    defaults, required, current, in_inputs = {}, set(), None, False
    for line in head:
        if line == "    inputs:":
            in_inputs = True
            continue
        if not in_inputs:
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^    \S", line):  # dedent out of inputs: (secrets:, etc.)
            break
        key = INPUT_KEY.match(line)
        if key:
            current = key.group(1)
            continue
        if current is None:
            continue
        default = INPUT_DEFAULT.match(line)
        if default:
            defaults[current] = default.group(1)
            continue
        req = INPUT_REQUIRED.match(line)
        if req and req.group(1) == "true":
            required.add(current)
    return defaults, required


def documented_input_defaults(path, heading):
    """`{input key: raw Default-column cell}` from the table under `heading`."""
    result, in_section = {}, False
    for line in read_lines(path):
        if line.strip() == heading:
            in_section = True
            continue
        if in_section and HEADING.match(line):
            break
        if in_section and line.lstrip().startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            key = re.fullmatch(r"`([A-Za-z0-9_-]+)`", cells[0])
            if key:
                result[key.group(1)] = cells[1]
    return result


# Canonical form for the required-with-no-default input, shared by both sides
# of the Default-column comparison so `workflows_ref`'s `— (**required**)` cell
# and the workflow's `required: true` compare equal.
_REQUIRED = "\x00required-no-default"


def canonical_guide_default(cell):
    text = cell.strip()
    if "**required**" in text:
        return _REQUIRED
    return text.strip("`").strip()


def canonical_workflow_default(name, defaults, required):
    if name in defaults:
        return defaults[name].strip()
    if name in required:
        return _REQUIRED
    return None


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

    def test_example_with_blocks_are_declared_inputs(self):
        # The two copy-paste callers (guide ## Caller fence, workflow header
        # comment) pass inputs via `with:`. A phantom key there is a zero-job
        # startup_failure for whoever copies it — the #31 failure, one level in
        # from the Inputs table the two methods above already police. Subset,
        # not equality: an example legitimately shows only a couple of inputs.
        for label, keys in example_with_keys().items():
            # Guard: each example genuinely names inputs incl. workflows_ref, so
            # a scanner gone quiet fails loudly instead of asserting {} ⊆ {}.
            self.assertIn(
                "workflows_ref",
                keys,
                f"the {label} `with:` scanner found no workflows_ref — the "
                "example block moved or the parser broke; this subset check is "
                "now vacuous",
            )
            undeclared = keys - self.declared
            self.assertFalse(
                undeclared,
                f"the {label} example passes `with:` inputs refresh-reviewers."
                f"yml does not declare: {sorted(undeclared)} — a phantom input "
                "there is a zero-job startup_failure for whoever copies it",
            )

    def test_readme_knob_table_has_no_phantom_knob(self):
        # `.github/refresh-reviewers/README.md`'s "Knob defaults (and why)"
        # table is intentionally partial and uses combined-cell rows
        # (`| `top_k` / `floor` |`) the TABLE_KEY regex correctly does NOT
        # capture, so assert ONLY the phantom direction — a knob named there but
        # not declared. Set-equality would be permanently red (the table omits
        # many inputs by design).
        documented = documented_inputs(README, "## Knob defaults (and why)")
        # Guard: a heading rename would empty this and make the check vacuous.
        self.assertTrue(
            documented,
            "the README `## Knob defaults (and why)` scanner found no bare-name "
            "knob rows — the heading was renamed or the table reshaped",
        )
        phantom = documented - self.declared
        self.assertFalse(
            phantom,
            "`.github/refresh-reviewers/README.md` Knob defaults table names "
            f"knobs refresh-reviewers.yml does not declare: {sorted(phantom)}",
        )

    def test_guide_default_column_matches_workflow(self):
        # Both assertions above are name-only, so the guide's Default column can
        # silently drift from the workflow's real `default:`. Pin it.
        defaults, required = workflow_input_defaults()
        guide = documented_input_defaults(SETUP_GUIDE, "## Inputs")
        # Guard both scanners: the workflow side must have parsed real defaults
        # and the required-only workflows_ref; the guide side must show its cell.
        self.assertTrue(
            defaults,
            "workflow_input_defaults found no `default:` lines — parser or file "
            "structure changed; the Default-column check is now vacuous",
        )
        self.assertIn(
            "workflows_ref",
            required,
            "workflow_input_defaults lost the required `workflows_ref` — parser "
            "or file structure changed; the Default-column check is now vacuous",
        )
        self.assertIn(
            "workflows_ref",
            guide,
            "the guide Inputs table Default scanner lost `workflows_ref` — "
            "parser or table shape changed; this check is now vacuous",
        )
        for name in sorted(self.declared):
            with self.subTest(input=name):
                self.assertIn(
                    name,
                    guide,
                    f"`{name}` is declared but absent from the guide Inputs "
                    "table Default column",
                )
                want = canonical_workflow_default(name, defaults, required)
                got = canonical_guide_default(guide[name])
                self.assertEqual(
                    got,
                    want,
                    f"guide Default column for `{name}` is {guide[name]!r} but "
                    "refresh-reviewers.yml has "
                    + (
                        "required: true (no default)"
                        if name in required
                        else f"default: {defaults.get(name)!r}"
                    ),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
