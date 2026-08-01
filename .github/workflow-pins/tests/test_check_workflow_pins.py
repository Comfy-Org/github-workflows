#!/usr/bin/env python3
"""Tests for check_workflow_pins.py — the `workflows_ref` default regression lint.

The lint is the only thing standing between this repo and a one-line
reintroduction of the BE-5546 hole (a `default: main` on `workflows_ref` lets a
caller SHA-pin `uses:` and still load mutable scripts). Its parsing is
text-level (no PyYAML in this repo), so the fixtures below pin the block
boundaries that text parsing is easy to get wrong: a `workflows_ref:` mentioned
in the header comment, a caller's `with:` value of the same name, and a
`default:` belonging to the NEXT input.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import check_workflow_pins as cwp  # noqa: E402


def _reusable(ref_block, extra_inputs="", header=""):
    """A minimal `on: workflow_call` workflow whose workflows_ref block varies."""
    return (
        "name: Fixture\n"
        + header
        + "\non:\n"
        "  workflow_call:\n"
        "    inputs:\n"
        "      some_other:\n"
        "        type: string\n"
        "        required: false\n"
        "        default: hello\n"
        "      workflows_ref:\n" + ref_block + extra_inputs + "\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )


PINNED = "        description: Ref to load scripts from.\n        type: string\n        required: true\n"
DEFAULTED = PINNED.replace("required: true", "required: false") + "        default: main\n"


class FindDefaultsTests(unittest.TestCase):
    def _find(self, text):
        return cwp.find_workflows_ref_defaults(text.split("\n"))

    def test_required_no_default_is_clean(self):
        self.assertEqual(self._find(_reusable(PINNED)), [])

    def test_default_is_reported_with_its_line_number(self):
        text = _reusable(DEFAULTED)
        hits = self._find(text)
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(text.split("\n")[hits[0] - 1].strip(), "default: main")

    def test_a_later_inputs_default_is_not_attributed_to_workflows_ref(self):
        # The block ends at the next key of the same indentation. A naive
        # "search forward for default:" would blame this one on workflows_ref.
        trailing = (
            "      verbosity:\n"
            "        type: string\n"
            "        required: false\n"
            "        default: quiet\n"
        )
        self.assertEqual(self._find(_reusable(PINNED, extra_inputs=trailing)), [])
        self.assertEqual(len(self._find(_reusable(DEFAULTED, extra_inputs=trailing))), 1)

    def test_header_comment_mentioning_the_input_is_ignored(self):
        header = (
            "# Caller pattern:\n"
            "#   with:\n"
            "#     workflows_ref: main   # <- prose, not a declaration\n"
            "#         default: main\n"
        )
        self.assertEqual(self._find(_reusable(PINNED, header=header)), [])

    def test_a_caller_workflow_is_not_a_reusable_workflow(self):
        # `workflows_ref:` here is a `with:` VALUE in a caller — nothing to check.
        caller = (
            "name: CI\n"
            "on:\n"
            "  pull_request:\n"
            "jobs:\n"
            "  review:\n"
            "    uses: Comfy-Org/github-workflows/.github/workflows/x.yml@abc  # v1\n"
            "    with:\n"
            "      workflows_ref: abc\n"
        )
        self.assertIsNone(self._find(caller))

    def test_reusable_without_the_input_is_skipped(self):
        text = (
            "name: Fixture\n"
            "on:\n"
            "  workflow_call:\n"
            "    inputs:\n"
            "      max_lines:\n"
            "        type: number\n"
            "        required: false\n"
            "        default: 200\n"
        )
        self.assertIsNone(self._find(text))

    def test_inline_on_forms_are_skipped(self):
        self.assertIsNone(self._find("name: F\non: [push]\njobs: {}\n"))
        self.assertIsNone(self._find("name: F\non: push\njobs: {}\n"))

    def test_quoted_on_key_is_still_parsed(self):
        # YAML 1.1 turns a bare `on` into True, so some repos quote the key.
        self.assertEqual(len(self._find(_reusable(DEFAULTED).replace("\non:", '\n"on":'))), 1)

    def test_a_trailing_comment_on_the_on_key_is_not_an_inline_trigger_list(self):
        # `on:  # triggers` is the block form. Reading the comment as an inline
        # value would drop the whole file from the lint while CI still says OK.
        text = _reusable(DEFAULTED).replace("\non:\n", "\non:  # when this runs\n")
        self.assertEqual(len(self._find(text)), 1)

    def test_an_inline_trigger_list_with_a_comment_is_still_skipped(self):
        self.assertIsNone(self._find("name: F\non: [push]  # only pushes\njobs: {}\n"))

    def test_flow_mapping_default_on_the_input_line_is_caught(self):
        # The one-line reintroduction: no child lines for a block scan to walk.
        text = _reusable("").replace(
            "      workflows_ref:\n",
            "      workflows_ref: {type: string, required: false, default: main}\n",
        )
        hits = self._find(text)
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("default: main", text.split("\n")[hits[0] - 1])

    def test_flow_mapping_without_a_default_is_clean(self):
        text = _reusable("").replace(
            "      workflows_ref:\n",
            "      workflows_ref: {type: string, required: true}\n",
        )
        self.assertEqual(self._find(text), [])

    def test_a_default_inside_a_folded_description_is_not_a_declaration(self):
        # `description: >-` continuation lines are indented DEEPER than the
        # input's own properties. This diff's own prose ("There is deliberately
        # no default: …") is one reflow away from starting such a line.
        folded = (
            "        description: >-\n"
            "          Ref to load scripts from. There is deliberately no\n"
            "          default: a floating default would defeat the pin.\n"
            "        type: string\n"
            "        required: true\n"
        )
        self.assertEqual(self._find(_reusable(folded)), [])

    def test_quoted_keys_do_not_hide_a_declaration_or_a_default(self):
        # Quoting any of these is valid Actions YAML and must not be an escape
        # hatch — the same one `on` already had.
        text = (
            _reusable(DEFAULTED)
            .replace("  workflow_call:", '  "workflow_call":')
            .replace("    inputs:", '    "inputs":')
            .replace("      workflows_ref:", '      "workflows_ref":')
            .replace("        default: main", '        "default": main')
        )
        self.assertEqual(len(self._find(text)), 1)


class GuardCoverageTests(unittest.TestCase):
    """Every `ref: ${{ inputs.workflows_ref }}` needs the guard in its own job.

    Dropping the default is only half the fix: a NEW job (or a new reusable
    workflow) that checks out at the ref without the guard reopens the `ref: ''`
    default-branch fallback, and the default-only lint stays green because it
    never declared a default to begin with.
    """

    GUARD = (
        "      - name: Require a pinned workflows_ref\n"
        "        env:\n"
        "          WORKFLOWS_REF: ${{ inputs.workflows_ref }}\n"
        "        run: |\n"
        "          exit 1\n"
    )
    CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref: ${{ inputs.workflows_ref }}\n"
    )
    # Same checkout, written as a one-line flow mapping — the shape that walked
    # past a `ref:`-at-line-start anchor while the hole stayed wide open.
    FLOW_CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        '        with: {repository: Comfy-Org/github-workflows, ref: "${{ inputs.workflows_ref }}"}\n'
    )

    def _jobs(self, *jobs):
        text = "name: F\non:\n  workflow_call:\njobs:\n"
        for i, steps in enumerate(jobs):
            text += "  job%d:\n    runs-on: ubuntu-latest\n    steps:\n%s" % (i, steps)
        return cwp.find_unguarded_ref_checkouts(text.split("\n"))

    def test_guarded_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.CHECKOUT), [])

    def test_unguarded_checkout_is_reported(self):
        self.assertEqual(len(self._jobs(self.CHECKOUT)), 1)

    def test_a_guard_after_the_checkout_does_not_count(self):
        self.assertEqual(len(self._jobs(self.CHECKOUT + self.GUARD)), 1)

    def test_a_guard_in_another_job_does_not_count(self):
        # Jobs run independently — job A's guard protects nothing in job B.
        self.assertEqual(len(self._jobs(self.GUARD, self.CHECKOUT)), 1)

    def test_each_job_is_judged_on_its_own_guard(self):
        self.assertEqual(self._jobs(self.GUARD + self.CHECKOUT, self.GUARD + self.CHECKOUT), [])

    def test_a_flow_mapping_checkout_is_not_an_escape_hatch(self):
        # `with: {…, ref: …}` on one line is the same unguarded checkout, and
        # anchoring on `ref:` at line start reported nothing at all for it.
        self.assertEqual(len(self._jobs(self.FLOW_CHECKOUT)), 1)

    def test_a_guarded_flow_mapping_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.FLOW_CHECKOUT), [])

    def test_a_sibling_flow_entry_is_not_read_as_the_ref(self):
        # `ref:` is pinned to a literal here; the input feeds a DIFFERENT key.
        # Matching greedily across the whole line would call this a ref use and
        # fail a workflow that never checks out at the input.
        step = (
            "      - name: Not a ref checkout\n"
            '        with: {ref: v1, path: "${{ inputs.workflows_ref }}"}\n'
        )
        self.assertEqual(self._jobs(step), [])

    # The same checkout again, with the value on the FOLLOWING line. Neither
    # same-line pattern sees an `inputs.` on the `ref:` line, so before the
    # continuation scan these reported nothing for a job with no guard at all.
    FOLDED_CHECKOUT = (
        "      - name: Load assets\n"
        "        uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref: >-\n"
        "            ${{ inputs.workflows_ref }}\n"
    )
    LITERAL_CHECKOUT = FOLDED_CHECKOUT.replace("ref: >-", "ref: |")
    PLAIN_CHECKOUT = FOLDED_CHECKOUT.replace("ref: >-", "ref:")

    def test_a_folded_scalar_checkout_is_not_an_escape_hatch(self):
        self.assertEqual(len(self._jobs(self.FOLDED_CHECKOUT)), 1)

    def test_a_literal_scalar_checkout_is_not_an_escape_hatch(self):
        self.assertEqual(len(self._jobs(self.LITERAL_CHECKOUT)), 1)

    def test_a_plain_multiline_checkout_is_not_an_escape_hatch(self):
        self.assertEqual(len(self._jobs(self.PLAIN_CHECKOUT)), 1)

    def test_a_guarded_multiline_checkout_passes(self):
        self.assertEqual(self._jobs(self.GUARD + self.FOLDED_CHECKOUT), [])

    def test_a_multiline_ref_pinned_to_a_literal_is_not_a_use(self):
        # The window a `ref:` key opens is not itself a finding: this checkout
        # never names the input, and failing it would fail a compliant workflow.
        step = (
            "      - name: Literal ref\n"
            "        with:\n"
            "          ref: >-\n"
            "            main\n"
        )
        self.assertEqual(self._jobs(step), [])

    def test_the_input_after_a_ref_scalar_closes_is_not_attributed_to_it(self):
        # `ref:` is pinned; the input feeds a LATER, shallower key. Running the
        # continuation scan past the scalar's end would blame it on the `ref:`.
        step = (
            "      - name: Literal ref\n"
            "        with:\n"
            "          ref: >-\n"
            "            main\n"
            "          path: ${{ inputs.workflows_ref }}\n"
        )
        self.assertEqual(self._jobs(step), [])

    def test_this_repos_own_workflows_guard_every_ref_checkout(self):
        root = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
        )
        seen = 0
        for name in ("cursor-review.yml", "groom.yml", "agents-md-integrity.yml"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                lines = f.read().split("\n")
            uses = [line for line in lines if cwp.is_ref_use(line)]
            self.assertTrue(uses, "%s: no ref checkout found — fixture drifted" % name)
            seen += len(uses)
            self.assertEqual(cwp.find_unguarded_ref_checkouts(lines), [], name)
        self.assertEqual(seen, 12, "expected the 12 guarded sites BE-5546 fixed")


class CheckDirTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_clean_dir_passes(self):
        self._write("good.yml", _reusable(PINNED))
        self._write("unrelated.yml", "name: F\non: [push]\njobs: {}\n")
        errors, checked, exempt_ok = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(errors, [])
        self.assertEqual(checked, ["good.yml"])
        self.assertEqual(exempt_ok, [])

    def test_defaulted_dir_fails_with_an_annotation(self):
        self._write("bad.yml", _reusable(DEFAULTED))
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith("::error file="), errors[0])
        self.assertIn("bad.yml", errors[0])
        self.assertIn("BE-5546", errors[0])
        self.assertEqual(checked, ["bad.yml"])

    def test_exempt_workflow_is_tolerated(self):
        self._write("legacy.yml", _reusable(DEFAULTED))
        errors, checked, exempt_ok = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(errors, [])
        self.assertEqual(exempt_ok, ["legacy.yml"])
        self.assertEqual(checked, ["legacy.yml"])

    def test_stale_exemption_fails_so_the_list_drains(self):
        self._write("legacy.yml", _reusable(PINNED))
        errors, _, exempt_ok = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("KNOWN_EXEMPT", errors[0])
        self.assertEqual(exempt_ok, [])

    def test_a_lost_declaration_is_an_error_not_a_silent_skip(self):
        # The file plainly USES the input, so a declaration must exist. If the
        # text parser cannot find it, the file is uncovered — which must look
        # different from "not applicable", not identical to it.
        self._write(
            "unparseable.yml",
            "name: F\n"
            "on:\n"
            "  workflow_call:\n"
            "jobs:\n"
            "  j:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: ${{ inputs.workflows_ref }}\n",
        )
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("NOT covering this file", errors[0])
        self.assertEqual(checked, [])

    def test_a_lost_declaration_is_caught_through_a_flow_mapping_use(self):
        # Same uncovered file, with its only use written in flow style. Before
        # the flow patterns this returned zero errors — the loudest failure the
        # checker has, silenced by a pair of braces.
        self._write(
            "unparseable.yml",
            "name: F\n"
            "on:\n"
            "  workflow_call:\n"
            "jobs:\n"
            "  j:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@abc\n"
            '        with: {repository: Comfy-Org/github-workflows, ref: "${{ inputs.workflows_ref }}"}\n',
        )
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("NOT covering this file", errors[0])
        self.assertEqual(checked, [])

    def test_a_lost_declaration_is_caught_through_a_block_scalar_use(self):
        # And once more with the value on the next line — the third spelling of
        # the same use, which has to stay just as loud as the other two.
        self._write(
            "unparseable.yml",
            "name: F\n"
            "on:\n"
            "  workflow_call:\n"
            "jobs:\n"
            "  j:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@abc\n"
            "        with:\n"
            "          ref: >-\n"
            "            ${{ inputs.workflows_ref }}\n",
        )
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("NOT covering this file", errors[0])
        self.assertEqual(checked, [])

    def test_an_unguarded_block_scalar_checkout_fails_the_lint(self):
        # End to end: a declared, default-free workflow whose only checkout is
        # written vertically and unguarded still exits non-zero.
        self._write(
            "leaky.yml",
            _reusable(PINNED).replace(
                "      - run: echo hi\n",
                "      - uses: actions/checkout@abc\n"
                "        with:\n"
                "          ref: >-\n"
                "            ${{ inputs.workflows_ref }}\n",
            ),
        )
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no empty-ref guard", errors[0])
        self.assertEqual(checked, ["leaky.yml"])

    def test_an_unrelated_workflow_is_still_a_silent_skip(self):
        self._write("unrelated.yml", "name: F\non: [push]\njobs: {}\n")
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual((errors, checked), ([], []))

    def test_an_unguarded_ref_checkout_fails_the_lint(self):
        self._write(
            "leaky.yml",
            _reusable(PINNED).replace(
                "      - run: echo hi\n",
                "      - uses: actions/checkout@abc\n"
                "        with:\n"
                "          ref: ${{ inputs.workflows_ref }}\n",
            ),
        )
        errors, checked, _ = cwp.check_dir(self.dir, exempt=frozenset())
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no empty-ref guard", errors[0])
        self.assertEqual(checked, ["leaky.yml"])

    def test_an_exempt_workflow_is_not_held_to_the_guard(self):
        # It still has its default, so an omitted input never reaches checkout
        # as '' — the guard only becomes required when the default goes.
        self._write(
            "legacy.yml",
            _reusable(DEFAULTED).replace(
                "      - run: echo hi\n",
                "      - uses: actions/checkout@abc\n"
                "        with:\n"
                "          ref: ${{ inputs.workflows_ref }}\n",
            ),
        )
        errors, _, exempt_ok = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(errors, [])
        self.assertEqual(exempt_ok, ["legacy.yml"])

    def test_an_exemption_for_a_missing_file_fails(self):
        # Rename or delete the workflow and the entry would otherwise survive
        # forever, pre-exempting whatever later reuses the filename.
        self._write("good.yml", _reusable(PINNED))
        errors, _, _ = cwp.check_dir(self.dir, exempt=frozenset({"renamed-away.yml"}))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("renamed-away.yml", errors[0])
        self.assertIn("KNOWN_EXEMPT", errors[0])

    def test_an_exemption_for_a_workflow_that_dropped_the_input_fails(self):
        self._write("legacy.yml", "name: F\non: [push]\njobs: {}\n")
        errors, _, _ = cwp.check_dir(self.dir, exempt=frozenset({"legacy.yml"}))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("KNOWN_EXEMPT", errors[0])

    def test_the_real_known_exempt_list_is_not_stale(self):
        # KNOWN_EXEMPT is checked against the real tree by the default run too;
        # this pins it so a rename cannot quietly widen the exemption.
        root = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
        )
        errors, checked, exempt_ok = cwp.check_dir(root)
        self.assertEqual(errors, [], errors)
        self.assertEqual(sorted(exempt_ok), sorted(cwp.KNOWN_EXEMPT))

    def test_this_repos_own_workflows_pass(self):
        # The real forcing function: the checked-in tree must stay clean.
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
        errors, checked, _ = cwp.check_dir(os.path.normpath(root))
        self.assertEqual(errors, [], errors)
        for name in ("cursor-review.yml", "groom.yml", "agents-md-integrity.yml"):
            self.assertIn(name, checked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
