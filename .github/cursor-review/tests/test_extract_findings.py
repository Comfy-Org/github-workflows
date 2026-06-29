#!/usr/bin/env python3
"""Regression tests for extract-findings.py JSON recovery.

The judge/consolidate step intermittently returns valid findings JSON wrapped
in a sentence or two of prose. Discarding the whole run on that (the BE-1916
parse_error class — hit on ComfyUI #487 and Alex's PR) is the bug these tests
guard against: prose-wrapped JSON must be recovered, not thrown away.

Run: python3 .github/cursor-review/tests/test_extract_findings.py
"""

import importlib.util
import json
import os
import tempfile
import unittest

# extract-findings.py has a hyphen, so import it by path rather than `import`.
_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "extract-findings.py")
_spec = importlib.util.spec_from_file_location("extract_findings", _MODULE_PATH)
ef = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ef)


# A real-finding payload reused across cases.
FINDINGS = [
    {
        "file": "internal/api/handler.go",
        "line": 42,
        "side": "RIGHT",
        "severity": "high",
        "body": "User-supplied filename reaches os.Open without traversal checks.",
    },
    {
        "file": "internal/worker/upload.go",
        "line": 118,
        "side": "RIGHT",
        "severity": "medium",
        "body": "Context is cancelled before the upload completes [see hunk], losing data.",
    },
]


def _findings(raw):
    """Mirror main()'s parse → coerce pipeline."""
    return ef.coerce_findings_list(ef.parse_json_findings(raw))


class ParseJsonFindingsTest(unittest.TestCase):
    def test_bare_array(self):
        self.assertEqual(_findings(json.dumps(FINDINGS)), FINDINGS)

    def test_empty_array(self):
        self.assertEqual(_findings("[]"), [])

    def test_prose_wrapped_array_487(self):
        # The #487 failure shape: a prose verdict, THEN the JSON array. The
        # prose even contains a bracket pair, which is what broke the old naive
        # first-`[`/last-`]` slice.
        raw = (
            "Based on my review of the actual code, I can adjudicate the two "
            "findings [both raised by multiple reviewers] as follows. Here is "
            "the consolidated result:\n\n" + json.dumps(FINDINGS) + "\n\n"
            "These are the only items that rise to the bar."
        )
        self.assertEqual(_findings(raw), FINDINGS)

    def test_fenced_json_block(self):
        raw = (
            "Sure — here are the findings:\n\n```json\n"
            + json.dumps(FINDINGS)
            + "\n```\nLet me know if you need more."
        )
        self.assertEqual(_findings(raw), FINDINGS)

    def test_fenced_block_no_lang(self):
        raw = "Result:\n\n```\n" + json.dumps(FINDINGS) + "\n```\n"
        self.assertEqual(_findings(raw), FINDINGS)

    def test_object_wrapped_findings(self):
        raw = "Here you go:\n" + json.dumps({"findings": FINDINGS})
        self.assertEqual(_findings(raw), FINDINGS)

    def test_brackets_inside_string_literals(self):
        # Brackets inside a body string must not confuse the balanced scanner.
        payload = [{"file": "a.py", "line": 1, "side": "RIGHT", "body": "uses arr[i] and m['k']"}]
        raw = "Verdict below.\n" + json.dumps(payload)
        self.assertEqual(_findings(raw), payload)

    def test_genuinely_malformed_returns_none(self):
        self.assertIsNone(_findings("I reviewed the code and found nothing actionable."))

    def test_truncated_json_returns_none(self):
        # An unterminated array can't be recovered — must fail, not half-parse.
        self.assertIsNone(_findings('[{"file": "a.py", "line": 1, "body": "oops"'))

    def test_scalar_is_not_findings(self):
        # A bare number is valid JSON but never a findings payload.
        self.assertIsNone(_findings("42"))

    def test_object_without_findings_key(self):
        self.assertIsNone(_findings('{"summary": "looks good", "count": 0}'))


class MainEndToEndTest(unittest.TestCase):
    """Drive main() the way the workflow does, asserting the status field."""

    def _run(self, raw_text):
        with tempfile.TemporaryDirectory() as d:
            raw_path = os.path.join(d, "raw.txt")
            out_path = os.path.join(d, "out.json")
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(raw_text)
            import sys

            argv = sys.argv
            sys.argv = [
                "extract-findings.py",
                "--raw", raw_path,
                "--out", out_path,
                "--model", "judge-model",
                "--review-type", "judge",
            ]
            try:
                ef.main()
            finally:
                sys.argv = argv
            with open(out_path, encoding="utf-8") as f:
                return json.load(f)

    def test_prose_wrapped_parses_ok(self):
        raw = "After review, my verdict is:\n\n" + json.dumps(FINDINGS)
        record = self._run(raw)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["findings"], FINDINGS)

    def test_malformed_is_parse_error(self):
        record = self._run("I could not find anything worth flagging.")
        self.assertEqual(record["status"], "parse_error")
        self.assertEqual(record["findings"], [])

    def test_empty_is_empty(self):
        record = self._run("   \n  ")
        self.assertEqual(record["status"], "empty")


if __name__ == "__main__":
    unittest.main()
