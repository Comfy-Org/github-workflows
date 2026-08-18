"""Unit tests for the .coderabbit.yaml validator.

The fixtures below are deliberately built from the failure classes that actually
happened, not from invented ones:

  * an over-long `tone_instructions` — the 446-char field that had the whole
    config rejected on one repo, fixed twice in six days by two independent
    tickets because nothing detected it;
  * a top-level `tools:` block that belongs under `reviews:` — three org repos
    carried this at the time of writing, and on one of them it silently inverted
    a `golangci-lint: enabled: false` into the schema default of `true`.

Every test validates against the REAL vendored schema rather than a toy one, so
a schema refresh that would change a verdict fails here first.
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import check_coderabbit_config as checker  # noqa: E402

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.v2.json"
)

with open(SCHEMA_PATH, "r", encoding="utf-8") as _f:
    SCHEMA = json.load(_f)

VALID = """\
language: en-US
tone_instructions: Be concise.
reviews:
  profile: chill
  request_changes_workflow: false
  tools:
    golangci-lint:
      enabled: false
  path_instructions:
    - path: "**/*.go"
      instructions: Prefer table-driven tests.
"""

# The real shape: the block is written at the document root instead of under
# `reviews:`, so CodeRabbit strips it and the disable never applies.
MISPLACED_TOOLS = """\
reviews:
  profile: chill
tools:
  # Project uses go vet + staticcheck, not golangci-lint
  golangci-lint:
    enabled: false
"""

OVERLONG_TONE = "tone_instructions: " + ("x" * 300) + "\n"

MALFORMED = """\
reviews:
  profile: chill
   path_filters:
  - "!**/*.lock"
"""


def severities(findings):
    return [f[0] for f in findings]


def messages(findings):
    return [f[3] for f in findings]


class ValidateTest(unittest.TestCase):
    def test_valid_config_produces_nothing(self):
        findings, notes = checker.validate(VALID, SCHEMA)
        self.assertEqual(findings, [], msg=f"unexpected findings: {messages(findings)}")
        self.assertEqual(notes, [])

    def test_overlong_tone_instructions_fails_with_actual_and_permitted(self):
        findings, _ = checker.validate(OVERLONG_TONE, SCHEMA)
        self.assertEqual(severities(findings), ["error"])
        message = messages(findings)[0]
        self.assertIn("tone_instructions", message)
        self.assertIn("300 characters", message)
        self.assertIn("250", message)

    def test_overlong_tone_instructions_still_fails_without_strict(self):
        # The severity split must not be read as "strict mode is what makes this
        # check bite" — a maxLength violation is file-rejecting either way.
        findings, _ = checker.validate(OVERLONG_TONE, SCHEMA, strict_unknown_keys=False)
        self.assertEqual(severities(findings), ["error"])

    def test_misplaced_tools_warns_by_default_and_names_the_root_key(self):
        findings, _ = checker.validate(MISPLACED_TOOLS, SCHEMA)
        self.assertEqual(severities(findings), ["warning"])
        _severity, path, line, message = findings[0]
        self.assertEqual(path, "tools")
        self.assertIn("unknown key `tools`", message)
        self.assertIn("document root", message)
        # The whole point of the hint: name where it belongs.
        self.assertIn("reviews.tools", message)
        # And point at the key itself, not at the file.
        self.assertEqual(line, 3)

    def test_misplaced_tools_fails_under_strict_unknown_keys(self):
        findings, _ = checker.validate(MISPLACED_TOOLS, SCHEMA, strict_unknown_keys=True)
        self.assertEqual(severities(findings), ["error"])
        self.assertIn("unknown key `tools`", messages(findings)[0])

    def test_malformed_yaml_fails_with_a_line(self):
        findings, _ = checker.validate(MALFORMED, SCHEMA)
        self.assertEqual(severities(findings), ["error"])
        _severity, _path, line, message = findings[0]
        self.assertIn("not valid YAML", message)
        self.assertIsNotNone(line)

    def test_malformed_yaml_fails_even_with_unknown_keys_relaxed(self):
        findings, _ = checker.validate(MALFORMED, SCHEMA, strict_unknown_keys=False)
        self.assertEqual(severities(findings), ["error"])

    def test_type_error_fails(self):
        # A wrong-typed enum field trips both `type` and `enum`; both are
        # file-rejecting, so assert the severity of every finding rather than
        # their count.
        findings, _ = checker.validate("reviews:\n  profile: 7\n", SCHEMA)
        self.assertTrue(findings)
        self.assertEqual(set(severities(findings)), {"error"})
        self.assertTrue(all("reviews.profile" in m for m in messages(findings)))

    def test_enum_error_fails(self):
        findings, _ = checker.validate("reviews:\n  profile: spicy\n", SCHEMA)
        self.assertEqual(severities(findings), ["error"])
        self.assertIn("permitted values", messages(findings)[0])

    def test_nested_maxlength_reports_the_full_path_and_a_line(self):
        body = "x" * 21000
        text = (
            "reviews:\n"
            "  path_instructions:\n"
            '    - path: "**/*.go"\n'
            f"      instructions: {body}\n"
        )
        findings, _ = checker.validate(text, SCHEMA)
        self.assertEqual(severities(findings), ["error"])
        _severity, path, line, message = findings[0]
        self.assertEqual(path, "reviews.path_instructions[0].instructions")
        self.assertEqual(line, 4)
        self.assertIn("20000", message)

    def test_empty_document_is_a_note_not_a_finding(self):
        findings, notes = checker.validate("# only a comment\n", SCHEMA)
        self.assertEqual(findings, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("empty document", notes[0])

    def test_multiple_unknown_keys_are_reported_individually(self):
        findings, _ = checker.validate("tools: {}\nnope: 1\n", SCHEMA)
        self.assertEqual(sorted(f[1] for f in findings), ["nope", "tools"])

    def test_non_mapping_document_fails(self):
        findings, _ = checker.validate("- a\n- b\n", SCHEMA)
        self.assertEqual(severities(findings), ["error"])

    def test_workflow_command_injection_is_escaped(self):
        # A key carrying a newline would otherwise close the annotation line and
        # emit a second, author-chosen workflow command into a public log.
        evil = '"bad\n::stop-commands::tok": 1\n'
        findings, _ = checker.validate(evil, SCHEMA)
        self.assertTrue(findings)
        buf = io.StringIO()
        with redirect_stdout(buf):
            checker._emit(findings, [], ".coderabbit.yaml")
        out = buf.getvalue()
        self.assertNotIn("\n::stop-commands::tok", out)
        self.assertIn("%0A", out)


class SchemaTest(unittest.TestCase):
    """Assertions about the VENDORED schema itself.

    These are the facts the checker's value rests on. If a schema refresh changes
    one, the refresh PR should say so out loud rather than quietly redefining
    what the check means.
    """

    def test_root_is_closed(self):
        # Everything about the unknown-key severity depends on this.
        self.assertIs(SCHEMA.get("additionalProperties"), False)

    def test_tone_instructions_is_capped_at_250(self):
        self.assertEqual(SCHEMA["properties"]["tone_instructions"]["maxLength"], 250)

    def test_tools_lives_under_reviews(self):
        self.assertIn("tools", SCHEMA["properties"]["reviews"]["properties"])
        self.assertNotIn("tools", SCHEMA["properties"])

    def test_golangci_lint_defaults_to_enabled(self):
        # The reason a stripped root `tools:` block is a behaviour change and not
        # a cosmetic one.
        tools = SCHEMA["properties"]["reviews"]["properties"]["tools"]["properties"]
        self.assertIs(tools["golangci-lint"]["properties"]["enabled"]["default"], True)


class LoadSchemaTest(unittest.TestCase):
    def test_rejects_a_redirect_stub(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('"<html>Moved</html>"')
            path = f.name
        with self.assertRaises(checker.ConfigError):
            checker.load_schema(path)
        os.unlink(path)

    def test_reports_a_digest(self):
        _schema, digest = checker.load_schema(SCHEMA_PATH)
        self.assertEqual(len(digest), 64)


class MainTest(unittest.TestCase):
    def _run(self, tmp, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = checker.main(argv)
        return code, buf.getvalue()

    def test_absent_file_passes_and_says_so(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
        self.assertEqual(code, 0)
        self.assertIn("nothing to validate", out)
        self.assertIn("absent", out)

    def test_valid_file_exits_zero(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".coderabbit.yaml"), "w", encoding="utf-8") as f:
                f.write(VALID)
            code, out = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
        self.assertEqual(code, 0, msg=out)

    def test_misplaced_tools_exits_zero_by_default_and_one_under_strict(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".coderabbit.yaml"), "w", encoding="utf-8") as f:
                f.write(MISPLACED_TOOLS)
            code, out = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
            self.assertEqual(code, 0, msg=out)
            self.assertIn("::warning ", out)
            code, out = self._run(
                tmp, ["--root", tmp, "--schema", SCHEMA_PATH, "--strict-unknown-keys"]
            )
            self.assertEqual(code, 1)
            self.assertIn("::error ", out)

    def test_overlong_tone_exits_one(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".coderabbit.yaml"), "w", encoding="utf-8") as f:
                f.write(OVERLONG_TONE)
            code, out = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
        self.assertEqual(code, 1)
        self.assertIn("::error ", out)

    def test_custom_config_path_is_honored(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".coderabbit.yml"), "w", encoding="utf-8") as f:
                f.write(OVERLONG_TONE)
            # Default name is absent -> pass; pointing at the real file -> fail.
            code, _ = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
            self.assertEqual(code, 0)
            code, _ = self._run(
                tmp, ["--root", tmp, "--schema", SCHEMA_PATH, "--config", ".coderabbit.yml"]
            )
            self.assertEqual(code, 1)

    def test_unreadable_schema_exits_two(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._run(
                tmp, ["--root", tmp, "--schema", os.path.join(tmp, "nope.json")]
            )
        # 2, not 1: "I could not run the check" must never look like "the config
        # is fine" OR like "the config is broken".
        self.assertEqual(code, 2)
        self.assertIn("::error::", out)


if __name__ == "__main__":
    unittest.main()
