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


class CheckDirTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

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

    def test_this_repos_own_workflows_pass(self):
        # The real forcing function: the checked-in tree must stay clean.
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows")
        errors, checked, _ = cwp.check_dir(os.path.normpath(root))
        self.assertEqual(errors, [], errors)
        for name in ("cursor-review.yml", "groom.yml", "agents-md-integrity.yml"):
            self.assertIn(name, checked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
