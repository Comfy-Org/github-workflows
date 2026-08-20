"""Unit tests for the .coderabbit.yaml validator.

The fixtures below are deliberately built from the failure classes that actually
happened, not from invented ones:

  * an over-long `tone_instructions` — the 446-char field that had the whole
    config rejected on one repo, fixed twice in six days by two independent
    tickets because nothing detected it;
  * a top-level `tools:` block that belongs under `reviews:` — three org repos
    carried this at the time of writing, and on one of them it silently inverted
    a `golangci-lint: enabled: false` into the schema default of `true`;
  * a `reviews.tools.github-checks.timeout_ms` that upstream has since removed —
    live in six org repos at the time of writing, and the whole reason the
    unknown-key check cannot stop at the five objects the schema closes
    explicitly (see `ClosedByOmissionTest`).

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

# A key upstream REMOVED from `reviews.tools.github-checks`, still carried by six
# org repos. The object names only `enabled` and says nothing about the rest, so
# jsonschema has no keyword to fire on — this is the exact class the walk exists
# for, and it is a stale key doing nothing, not a hypothetical typo.
STALE_GITHUB_CHECKS = """\
reviews:
  tools:
    github-checks:
      enabled: true
      timeout_ms: 90000
"""

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
            with open(os.path.join(tmp, "configs.yaml"), "w", encoding="utf-8") as f:
                f.write(OVERLONG_TONE)
            # Default name is absent (and has no sibling spelling) -> pass;
            # pointing at the real file -> fail.
            code, _ = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
            self.assertEqual(code, 0)
            code, _ = self._run(
                tmp, ["--root", tmp, "--schema", SCHEMA_PATH, "--config", "configs.yaml"]
            )
            self.assertEqual(code, 1)

    def test_the_other_spelling_is_validated_not_reported_absent(self):
        # The quietest failure this checker can have: a repo on the `.coderabbit.yml`
        # spelling leaves `config_file` at its default, and every PR forever gets a
        # green check over a config nobody looked at. CodeRabbit honours both
        # spellings, so the present one is the config in effect.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".coderabbit.yml"), "w", encoding="utf-8") as f:
                f.write(OVERLONG_TONE)
            code, out = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
        self.assertEqual(code, 1, msg=out)
        self.assertIn("::warning::", out)
        self.assertIn(".coderabbit.yml", out)
        self.assertNotIn("absent — pass", out)

    def test_a_symlink_out_of_the_tree_is_refused_not_followed(self):
        # abspath normalizes `..` but does not resolve symlinks, while isfile()
        # and open() both follow them — so a config committed as a symlink would
        # otherwise clear the containment guard and have the TARGET's content
        # quoted into a public run log by a YAML error message.
        import tempfile

        with tempfile.TemporaryDirectory() as outside:
            secret = os.path.join(outside, "secret.yaml")
            with open(secret, "w", encoding="utf-8") as f:
                f.write("this: [is not: valid yaml\n")
            with tempfile.TemporaryDirectory() as tmp:
                os.symlink(secret, os.path.join(tmp, ".coderabbit.yaml"))
                code, out = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
        self.assertEqual(code, 2, msg=out)
        self.assertIn("outside the checked-out repo root", out)
        self.assertNotIn("is not valid yaml", out)

    def test_a_symlink_inside_the_tree_is_still_validated(self):
        # The guard is containment, not "no symlinks": a repo that keeps its
        # config behind an in-tree symlink is doing nothing wrong.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real.yaml")
            with open(real, "w", encoding="utf-8") as f:
                f.write(OVERLONG_TONE)
            os.symlink(real, os.path.join(tmp, ".coderabbit.yaml"))
            code, out = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
        self.assertEqual(code, 1, msg=out)

    def test_a_path_that_is_not_a_regular_file_exits_two(self):
        # A directory or a dangling symlink is "I could not check", never a pass.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".coderabbit.yaml"))
            code, out = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
            self.assertEqual(code, 2, msg=out)
            self.assertIn("not a regular file", out)

        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(os.path.join(tmp, "gone.yaml"), os.path.join(tmp, ".coderabbit.yaml"))
            code, out = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
            self.assertEqual(code, 2, msg=out)

    def test_an_absurdly_large_config_is_refused_before_parsing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".coderabbit.yaml"), "w", encoding="utf-8") as f:
                f.write("# " + ("x" * (checker.MAX_CONFIG_BYTES + 10)) + "\n")
            code, out = self._run(tmp, ["--root", tmp, "--schema", SCHEMA_PATH])
        self.assertEqual(code, 2, msg=out)
        self.assertIn("refusing to read it", out)

    def test_the_config_path_is_escaped_in_the_banner(self):
        # `config_file` is a workflow_call input a PR to the caller repo can edit;
        # a newline in it would emit a second, attacker-chosen workflow command
        # (`::stop-commands::` suppresses every annotation printed after it).
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._run(
                tmp,
                [
                    "--root",
                    tmp,
                    "--schema",
                    SCHEMA_PATH,
                    "--config",
                    "a.yaml\n::stop-commands::x",
                ],
            )
        self.assertNotIn("\n::stop-commands::", out)
        self.assertIn("%0A", out)
        self.assertEqual(code, 0, msg=out)

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


class ClosedByOmissionTest(unittest.TestCase):
    """Unknown keys inside objects the schema closes by saying nothing.

    Upstream declares `additionalProperties: false` on five objects and leaves 103
    more with a `properties` block and no statement about anything else. CodeRabbit
    strips an unrecognized key in either kind, so both must be reported — and the
    two halves must stay disjoint, since one fires only where the keyword is
    present and the other only where it is absent.
    """

    def test_a_typo_inside_reviews_is_flagged_at_its_own_line(self):
        findings, _ = checker.validate("reviews:\n  profil: chill\n", SCHEMA)
        self.assertEqual(len(findings), 1, msg=f"{messages(findings)}")
        severity, path, line, message = findings[0]
        self.assertEqual(severity, "warning")
        self.assertEqual(path, "reviews.profil")
        self.assertEqual(line, 2)
        self.assertIn("unknown key `profil`", message)
        self.assertIn("under `reviews`", message)

    def test_a_typo_inside_a_nested_tool_is_flagged_at_its_full_path(self):
        text = "reviews:\n  tools:\n    golangci-lint:\n      enabld: true\n"
        findings, _ = checker.validate(text, SCHEMA)
        self.assertEqual(len(findings), 1, msg=f"{messages(findings)}")
        _severity, path, line, _message = findings[0]
        self.assertEqual(path, "reviews.tools.golangci-lint.enabld")
        self.assertEqual(line, 4)

    def test_the_valid_fixture_stays_silent(self):
        # The walk's false-positive canary: a config using nothing but real keys
        # must produce nothing, or every enrolled repo gets a warning it cannot act on.
        findings, notes = checker.validate(VALID, SCHEMA)
        self.assertEqual(findings, [], msg=f"unexpected findings: {messages(findings)}")
        self.assertEqual(notes, [])

    def test_a_schema_valued_additional_properties_object_is_open(self):
        # `reviews.mutually_exclusive_groups` declares `additionalProperties` as a
        # SCHEMA (arrays of ≥2 strings), i.e. user-chosen group names are the
        # point. Only the literal `false` closes an object, and that case never
        # reaches the walk because jsonschema already owns it.
        text = "reviews:\n  mutually_exclusive_groups:\n    risk: [high, low]\n"
        findings, _ = checker.validate(text, SCHEMA)
        self.assertEqual(findings, [], msg=f"{messages(findings)}")

    def test_an_anyof_item_shape_is_open(self):
        # `knowledge_base.code_guidelines.filePatterns[]` is `anyOf: [string,
        # object]`. The item node names no `properties` of its own and the walk
        # does not descend into combinator branches, so it judges nothing here.
        text = (
            "knowledge_base:\n"
            "  code_guidelines:\n"
            "    filePatterns:\n"
            '      - files: "**/A.md"\n'
            '        applyTo: "**"\n'
        )
        findings, _ = checker.validate(text, SCHEMA)
        self.assertEqual(findings, [], msg=f"{messages(findings)}")

    def test_severity_follows_strict_unknown_keys(self):
        text = "reviews:\n  profil: chill\n"
        self.assertEqual(severities(checker.validate(text, SCHEMA)[0]), ["warning"])
        self.assertEqual(
            severities(checker.validate(text, SCHEMA, strict_unknown_keys=True)[0]),
            ["error"],
        )

    def test_a_stale_upstream_removed_key_is_flagged(self):
        # The regression anchor: `timeout_ms` was live in six org repos and
        # reported nowhere, in either mode, before this walk existed.
        findings, _ = checker.validate(STALE_GITHUB_CHECKS, SCHEMA)
        self.assertEqual(len(findings), 1, msg=f"{messages(findings)}")
        _severity, path, line, _message = findings[0]
        self.assertEqual(path, "reviews.tools.github-checks.timeout_ms")
        self.assertEqual(line, 5)

    def test_the_two_halves_never_report_the_same_key(self):
        # Disjoint by construction (keyword present vs absent), asserted on a
        # config that exercises both halves at once so a future refactor that
        # merged them would fail here rather than double-annotate a real PR.
        text = (
            "tools:\n"
            "  golangci-lint:\n"
            "    enabled: false\n"
            "reviews:\n"
            "  profil: chill\n"
            "  tools:\n"
            "    github-checks:\n"
            "      timeout_ms: 1\n"
        )
        findings, _ = checker.validate(text, SCHEMA)
        paths = [f[1] for f in findings]
        self.assertEqual(sorted(paths), sorted(set(paths)), msg=f"duplicated: {paths}")
        self.assertEqual(
            sorted(paths),
            [
                "reviews.profil",
                "reviews.tools.github-checks.timeout_ms",
                "tools",
            ],
        )

    def test_a_non_string_key_is_coerced_not_rendered_as_an_index(self):
        # YAML keys need not be strings. Uncoerced, `_format_path` would render
        # this as `reviews[1]` — a list index into an object — and `_key_line`
        # would compare an int to the composed node's string and find no line.
        findings, _ = checker.validate("reviews:\n  1: x\n", SCHEMA)
        self.assertEqual(len(findings), 1, msg=f"{messages(findings)}")
        _severity, path, line, _message = findings[0]
        self.assertEqual(path, "reviews.1")
        self.assertEqual(line, 2)

    def test_a_shape_disagreement_is_left_to_jsonschema(self):
        # `reviews.profile` is a string enum. A mapping there is a type error and
        # jsonschema says so; the walk must not add a second, differently-worded
        # complaint about the keys inside it.
        findings, _ = checker.validate("reviews:\n  profile:\n    nope: 1\n", SCHEMA)
        self.assertTrue(findings)
        # Every finding is jsonschema's own (its `type` message quotes the
        # instance, so `nope` legitimately appears in the text); what must NOT
        # exist is a second finding AT the inner key's path.
        self.assertEqual(set(severities(findings)), {"error"})
        self.assertNotIn("reviews.profile.nope", [f[1] for f in findings])

    def test_the_value_under_an_unknown_key_is_not_descended_into(self):
        # One finding for the unknown key, not one per leaf beneath it: the
        # schema names no subschema for it, so nothing under it is judgeable.
        text = "reviews:\n  profil:\n    a: 1\n    b: 2\n"
        findings, _ = checker.validate(text, SCHEMA)
        self.assertEqual([f[1] for f in findings], ["reviews.profil"])

    def test_findings_count_toward_the_shared_cap_and_note_the_truncation(self):
        # The cap is over BOTH halves. Half the unknown keys here are root-level
        # (jsonschema's) and half are nested (the walk's).
        root = "".join(f"unknown_root_{i}: 1\n" for i in range(60))
        nested = "reviews:\n" + "".join(f"  unknown_nested_{i}: 1\n" for i in range(60))
        findings, notes = checker.validate(root + nested, SCHEMA)
        self.assertEqual(len(findings), checker.MAX_FINDINGS)
        self.assertTrue(any("only the first" in n for n in notes), msg=f"{notes}")

    def test_a_bounded_walk_says_so_rather_than_reporting_clean(self):
        # A partial scan must never read like a completed one. Forced via the
        # budget rather than a megabyte fixture.
        original = checker.MAX_WALK_KEYS
        checker.MAX_WALK_KEYS = 1
        try:
            findings, notes = checker.validate(STALE_GITHUB_CHECKS, SCHEMA)
        finally:
            checker.MAX_WALK_KEYS = original
        self.assertEqual(findings, [])
        self.assertTrue(any("were NOT checked" in n for n in notes), msg=f"{notes}")

    def test_main_exits_zero_by_default_and_one_under_strict(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".coderabbit.yaml"), "w", encoding="utf-8") as f:
                f.write(STALE_GITHUB_CHECKS)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = checker.main(["--root", tmp, "--schema", SCHEMA_PATH])
            out = buf.getvalue()
            self.assertEqual(code, 0, msg=out)
            self.assertIn("::warning ", out)
            self.assertIn("timeout_ms", out)

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = checker.main(
                    ["--root", tmp, "--schema", SCHEMA_PATH, "--strict-unknown-keys"]
                )
            out = buf.getvalue()
            self.assertEqual(code, 1, msg=out)
            self.assertIn("::error ", out)


class OpennessTest(unittest.TestCase):
    """Facts about the VENDORED schema that decide what the walk reports.

    The walk's whole premise is that "declares `properties`, declares no opener"
    means closed. If a schema refresh starts using a keyword this file does not
    know, the refresh PR should say so out loud rather than quietly changing what
    the check means.
    """

    @staticmethod
    def _nodes(schema):
        """Every subschema reachable through properties / items / combinators."""
        seen = []

        def walk(node):
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return
            seen.append(node)
            for key in (
                "properties",
                "patternProperties",
                "$defs",
                "definitions",
                "dependentSchemas",
            ):
                for sub in (node.get(key) or {}).values():
                    walk(sub)
            for key in ("items", "additionalProperties", "propertyNames", "not",
                        "if", "then", "else", "unevaluatedProperties"):
                walk(node.get(key))
            for key in ("anyOf", "oneOf", "allOf"):
                walk(node.get(key))

        walk(schema)
        return seen

    def test_the_two_halves_partition_the_objects_that_declare_properties(self):
        explicit, omitted = [], []
        for node in self._nodes(SCHEMA):
            if not isinstance(node.get("properties"), dict):
                continue
            if any(k in node for k in checker._OPENER_KEYWORDS):
                explicit.append(node)
            else:
                omitted.append(node)
        # Not an arbitrary snapshot: this is the size of the gap being closed.
        self.assertEqual(len(explicit), 5)
        self.assertEqual(len(omitted), 103)

    def test_every_explicitly_open_object_uses_additional_properties(self):
        # The audit behind `_OPENER_KEYWORDS`: the schema uses one opener today,
        # and the wider list is future-proofing, not a description of it.
        for node in self._nodes(SCHEMA):
            if not isinstance(node.get("properties"), dict):
                continue
            used = [k for k in checker._OPENER_KEYWORDS if k in node]
            self.assertIn(used, ([], ["additionalProperties"]), msg=f"{used}")

    def test_the_document_root_is_the_explicitly_closed_case(self):
        self.assertIs(SCHEMA.get("additionalProperties"), False)

    def test_github_checks_names_only_enabled(self):
        # If upstream ever adds `timeout_ms` back, the regression anchor above
        # stops being a finding and this fails first, naming why.
        tools = SCHEMA["properties"]["reviews"]["properties"]["tools"]["properties"]
        self.assertEqual(list(tools["github-checks"]["properties"]), ["enabled"])


if __name__ == "__main__":
    unittest.main()
