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


class ClassifyRunErrorTest(unittest.TestCase):
    """Unit-cover the delisted-model / failed-invocation classifier."""

    def test_cannot_use_model_stderr_wins(self):
        # The delisted-model fingerprint: non-zero exit, empty stdout, and a
        # "Cannot use this model: <id>" stderr. Must classify as error, and
        # surface the specific stderr line.
        msg = ef.classify_run_error(1, "Cannot use this model: kimi-k2.5\n", "")
        self.assertEqual(msg, "Cannot use this model: kimi-k2.5")

    def test_cannot_use_model_wins_even_with_stdout(self):
        # The marker is definitive (the model never ran), so it wins even if
        # some stray text landed on stdout.
        msg = ef.classify_run_error(1, "error: Cannot use this model: gone-model", "noise")
        self.assertEqual(msg, "Cannot use this model: gone-model")

    def test_nonzero_exit_and_empty_stdout_is_error(self):
        msg = ef.classify_run_error(2, "some transient failure\n", "   ")
        self.assertIsNotNone(msg)
        self.assertIn("status 2", msg)
        self.assertIn("some transient failure", msg)

    def test_nonzero_exit_but_findings_present_is_not_error(self):
        # A non-zero exit that still produced usable output must NOT be
        # discarded — leave it to the normal parse path.
        self.assertIsNone(ef.classify_run_error(1, "", json.dumps(FINDINGS)))

    def test_zero_exit_empty_is_not_error(self):
        # A clean exit with empty output is a genuine "found nothing" — stays
        # empty, not error.
        self.assertIsNone(ef.classify_run_error(0, "", ""))

    def test_unknown_exit_is_not_error(self):
        self.assertIsNone(ef.classify_run_error(None, "", ""))


class ParseExitCodeTest(unittest.TestCase):
    def test_integer_string(self):
        self.assertEqual(ef.parse_exit_code("1"), 1)

    def test_blank_and_none_are_unknown(self):
        self.assertIsNone(ef.parse_exit_code(""))
        self.assertIsNone(ef.parse_exit_code("  "))
        self.assertIsNone(ef.parse_exit_code(None))

    def test_non_integer_is_unknown(self):
        self.assertIsNone(ef.parse_exit_code("not-a-number"))


class MainEndToEndTest(unittest.TestCase):
    """Drive main() the way the workflow does, asserting the status field."""

    def _run(self, raw_text, exit_code=None, stderr_text=None):
        with tempfile.TemporaryDirectory() as d:
            raw_path = os.path.join(d, "raw.txt")
            out_path = os.path.join(d, "out.json")
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(raw_text)
            import sys

            argv = sys.argv
            new_argv = [
                "extract-findings.py",
                "--raw", raw_path,
                "--out", out_path,
                "--model", "judge-model",
                "--review-type", "judge",
            ]
            if exit_code is not None:
                new_argv += ["--exit-code", str(exit_code)]
            if stderr_text is not None:
                stderr_path = os.path.join(d, "stderr.txt")
                with open(stderr_path, "w", encoding="utf-8") as f:
                    f.write(stderr_text)
                new_argv += ["--stderr", stderr_path]
            sys.argv = new_argv
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

    def test_delisted_model_is_error_not_empty(self):
        # The core regression: a delisted model (empty stdout + non-zero exit +
        # "Cannot use this model:" stderr) must be `error`, never `empty`.
        record = self._run(
            "",
            exit_code=1,
            stderr_text="Cannot use this model: kimi-k2.5\n",
        )
        self.assertEqual(record["status"], "error")
        self.assertIn("Cannot use this model: kimi-k2.5", record["error"])
        self.assertEqual(record["findings"], [])

    def test_nonzero_exit_empty_output_is_error(self):
        record = self._run("", exit_code=137, stderr_text="killed\n")
        self.assertEqual(record["status"], "error")
        self.assertIn("status 137", record["error"])

    def test_empty_without_error_signals_stays_empty(self):
        # Passing the args but with a clean exit and no stderr must not change
        # the genuine found-nothing classification.
        record = self._run("", exit_code=0, stderr_text="")
        self.assertEqual(record["status"], "empty")

    def test_findings_survive_nonzero_exit(self):
        # A non-zero exit that still produced findings must not be discarded.
        raw = json.dumps(FINDINGS)
        record = self._run(raw, exit_code=1, stderr_text="")
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["findings"], FINDINGS)


if __name__ == "__main__":
    unittest.main()
