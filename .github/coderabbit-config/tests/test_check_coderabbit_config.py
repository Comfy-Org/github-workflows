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
from contextlib import contextmanager, redirect_stdout

import yaml

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


@contextmanager
def budget(keys=None, depth=None):
    """Shrink the walk's own limits so a bail-out is testable without a huge fixture."""
    saved = (checker.MAX_WALK_KEYS, checker.MAX_WALK_DEPTH)
    if keys is not None:
        checker.MAX_WALK_KEYS = keys
    if depth is not None:
        checker.MAX_WALK_DEPTH = depth
    try:
        yield
    finally:
        checker.MAX_WALK_KEYS, checker.MAX_WALK_DEPTH = saved


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

    def test_a_non_string_key_is_not_rendered_as_an_index(self):
        # YAML keys need not be strings. Left as the loaded int, `_format_path`
        # would render this as `reviews[1]` — a list index into an object. The
        # sibling test below covers the keys `str()` alone still got wrong.
        findings, _ = checker.validate("reviews:\n  1: x\n", SCHEMA)
        self.assertEqual(len(findings), 1, msg=f"{messages(findings)}")
        _severity, path, line, _message = findings[0]
        self.assertEqual(path, "reviews.1")
        self.assertEqual(line, 2)

    def test_a_key_is_named_by_its_source_spelling_not_its_loaded_value(self):
        # `safe_load` resolves plain `true`/`on`/`yes` to True, `null` to None and
        # `0x10` to 16, so `str()` on the loaded key produced `reviews.True` /
        # `reviews.None` / `reviews.16` — paths naming a key that appears NOWHERE
        # in the file — and then found no line to correct the impression, because
        # the lookup compared "True" against the node's "true".
        for source, expected in (
            ("true", "reviews.true"),
            ("on", "reviews.on"),
            ("null", "reviews.null"),
            ("0x10", "reviews.0x10"),
            ('"true"', "reviews.true"),
        ):
            with self.subTest(source=source):
                findings, _ = checker.validate(f"reviews:\n  {source}: x\n", SCHEMA)
                self.assertEqual(len(findings), 1, msg=f"{messages(findings)}")
                _severity, path, line, _message = findings[0]
                self.assertEqual(path, expected)
                self.assertEqual(line, 2)

    def test_a_generic_key_gets_no_did_you_mean_rather_than_three_wrong_ones(self):
        # `enabled` sits at ~72 places in the schema. The hint was written for the
        # root-level `tools:` case, where the one other path is almost certainly
        # the intended home; for a generic name the alphabetically-first three
        # were three wrong homes (`issue_enrichment.*` for `reviews.tools`).
        findings, _ = checker.validate("reviews:\n  tools:\n    enabled: true\n", SCHEMA)
        self.assertEqual(len(findings), 1, msg=f"{messages(findings)}")
        self.assertNotIn("Did you mean", findings[0][3])

    def test_the_motivating_misplaced_block_still_gets_its_hint(self):
        # The regression guard on the test above: suppressing generic names must
        # not suppress the case the hint exists for.
        findings, _ = checker.validate(MISPLACED_TOOLS, SCHEMA)
        hinted = [f for f in findings if "Did you mean `reviews.tools`?" in f[3]]
        self.assertEqual(len(hinted), 1, msg=f"{messages(findings)}")

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
        self.assertTrue(any("were dropped" in n for n in notes), msg=f"{notes}")

    def test_a_bounded_walk_says_so_rather_than_reporting_clean(self):
        # A partial scan must never read like a completed one. Forced via the
        # budget rather than a megabyte fixture.
        with budget(keys=1):
            findings, _notes = checker.validate(STALE_GITHUB_CHECKS, SCHEMA)
        self.assertEqual(len(findings), 1, msg=f"{messages(findings)}")
        severity, path, _line, message = findings[0]
        self.assertEqual(severity, "warning")
        self.assertEqual(path, "(document root)")
        self.assertIn("were NOT checked", message)

    def test_a_bounded_walk_fails_under_strict_rather_than_exiting_zero(self):
        # A note does not reach the exit code, so a walk that gave up early used
        # to exit 0 even for a repo that asked for unknown keys to FAIL — a green
        # check over keys nothing looked at. Warn-only mode is unchanged.
        with budget(keys=1):
            warn, _ = checker.validate(STALE_GITHUB_CHECKS, SCHEMA)
            strict, _ = checker.validate(
                STALE_GITHUB_CHECKS, SCHEMA, strict_unknown_keys=True
            )
        self.assertEqual(severities(warn), ["warning"])
        self.assertEqual(severities(strict), ["error"])

    def test_a_depth_cutoff_is_reported_like_an_exhausted_budget(self):
        # Both are "I did not see the whole document"; only one used to be said.
        with budget(depth=0):
            findings, _ = checker.validate(STALE_GITHUB_CHECKS, SCHEMA)
        self.assertEqual(len(findings), 1, msg=f"{messages(findings)}")
        self.assertIn("were NOT checked", findings[0][3])

    def test_a_budget_landing_exactly_on_the_last_key_is_not_partial(self):
        # The flag is set where the walk actually bails, not derived from the
        # leftover budget: a document walked in FULL on its last unit of budget
        # is complete, and saying otherwise cries wolf on every exact fit.
        text = "reviews:\n  profil: chill\n"
        with budget(keys=4):  # two node visits + two keys
            exact, _ = checker.validate(text, SCHEMA)
        with budget(keys=3):
            short, _ = checker.validate(text, SCHEMA)
        self.assertEqual([f[1] for f in exact], ["reviews.profil"])
        self.assertEqual([f[1] for f in short], ["(document root)"])

    def test_list_traversal_is_charged_against_the_walk_budget(self):
        # Elements recurse without naming a key, so a key-only budget left array
        # traversal outside the bound entirely — and a YAML-aliased document far
        # under the 512 KiB cap can alias one inner list into thousands of outer
        # entries, each charging 1 while everything below it is visited free.
        text = (
            "a: &a [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]\n"
            "b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
            "c: [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
        )
        nested = {"type": "array", "items": {"type": "array", "items": {
            "type": "array", "items": {"type": "object", "properties": {}}}}}
        data = yaml.safe_load(text)
        with budget(keys=50):  # far below the 1000+ element visits below `c`
            _findings, bounded = checker._walk_unknown_keys(
                {"c": data["c"]},
                {"type": "object", "properties": {"c": nested}},
                None,
                {},
                "warning",
                100,
            )
        self.assertTrue(bounded)

    def test_one_error_fanning_out_still_respects_the_findings_cap(self):
        # `islice` bounds the ERROR count, not the finding count: a closed
        # mapping reports every extra key in ONE error, which `_extra_keys`
        # splits into one finding each. 150 unknown root keys used to emit 150
        # annotations into a public run log with no truncation note at all.
        text = "".join(f"unknown_root_{i}: 1\n" for i in range(checker.MAX_FINDINGS + 50))
        findings, notes = checker.validate(text, SCHEMA)
        self.assertEqual(len(findings), checker.MAX_FINDINGS)
        self.assertTrue(any("were dropped" in n for n in notes), msg=f"{notes}")

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


class CapAndBudgetTest(unittest.TestCase):
    """The cap, the walk budget, and the key lookup under adversarial input.

    Every case here is a defect the review panel found in the FIRST fix for the
    unknown-key gap, not in the code that gap was found in — so each is pinned
    where a re-simplification would reintroduce it.
    """

    def test_a_file_rejecting_error_is_never_crowded_out_by_unknown_keys(self):
        # The root `additionalProperties` error has an empty `absolute_path`, so
        # it sorts FIRST, and it fans out to one finding per unknown key. A cap
        # that just sliced the list would drop the `maxLength` violation behind
        # 150 warnings — and exit 0 on a config CodeRabbit rejects WHOLE.
        text = "tone_instructions: " + ("x" * 300) + "\n"
        text += "".join(f"zzz{i}: 1\n" for i in range(150))
        findings, notes = checker.validate(text, SCHEMA)
        self.assertEqual(len(findings), checker.MAX_FINDINGS)
        self.assertTrue(any("were dropped" in n for n in notes), msg=f"{notes}")
        rejecting = [f for f in findings if "characters" in f[3]]
        self.assertEqual(len(rejecting), 1, msg="the maxLength error was capped away")
        self.assertIn("error", severities(findings))

    def test_the_unknown_key_fan_out_stops_working_at_the_cap(self):
        # The cap bounds the OUTPUT; this bounds the WORK. `k00000: 1` is ten
        # bytes, so ~50k unknown root keys fit inside MAX_CONFIG_BYTES, and
        # building a finding for each one used to rescan the whole mapping.
        text = "".join(f"k{i:05d}: 1\n" for i in range(20_000))
        self.assertLess(len(text), checker.MAX_CONFIG_BYTES)
        findings, notes = checker.validate(text, SCHEMA)
        self.assertEqual(len(findings), checker.MAX_FINDINGS)
        self.assertTrue(any("were dropped" in n for n in notes), msg=f"{notes}")

    def test_the_key_index_is_built_once_per_mapping(self):
        # The bound behind the test above: one construction pass per mapping, not
        # one per finding. Without the cache this is quadratic.
        node = yaml.compose("reviews:\n" + "".join(f"  k{i}: 1\n" for i in range(50)))
        reviews = node.value[0][1]
        first = checker._resolved_key_index(reviews)
        self.assertIs(checker._resolved_key_index(reviews), first)
        self.assertEqual(len(first), 50)

    def test_the_incomplete_scan_report_fits_under_the_cap(self):
        # It is a finding like any other, so a run that filled the cap and then
        # hit a walk bound must not emit MAX_FINDINGS + 1 alongside a note that
        # says MAX_FINDINGS are reported.
        text = "reviews:\n" + "".join(f"  unk{i}: 1\n" for i in range(150))
        with budget(keys=3):
            findings, _ = checker.validate(text, SCHEMA)
        self.assertLessEqual(len(findings), checker.MAX_FINDINGS)
        self.assertTrue(any("cannot vouch" in f[3] for f in findings), msg=f"{findings}")

    def test_the_incomplete_scan_report_outranks_an_unknown_key(self):
        # Which one the cap drops matters: losing an unknown key costs one
        # annotation, losing this one makes a partial run read as a complete one.
        tagged = [(checker._RANK_UNKNOWN, ("warning", f"k{i}", None, "x"))
                  for i in range(checker.MAX_FINDINGS)]
        tagged.append((checker._RANK_INCOMPLETE, ("warning", "(document root)", None, "vouch")))
        kept, capped = checker._apply_cap(tagged)
        self.assertTrue(capped)
        self.assertEqual(len(kept), checker.MAX_FINDINGS)
        self.assertIn("vouch", [f[3] for f in kept])

    def test_the_cap_keeps_survivors_in_emission_order(self):
        tagged = [(checker._RANK_UNKNOWN, ("warning", f"k{i}", None, "x"))
                  for i in range(checker.MAX_FINDINGS + 10)]
        kept, capped = checker._apply_cap(tagged)
        self.assertTrue(capped)
        self.assertEqual([f[1] for f in kept],
                         [f"k{i}" for i in range(checker.MAX_FINDINGS)])

    def test_a_suggestion_parent_matches_across_the_two_path_vocabularies(self):
        # `_format_path` spells an array element `[0]`; `_index_schema_paths`
        # spells it `[]`. Unnormalized, no candidate can share the parent of a
        # key under a list element and the parent-preference half never applies.
        index = {"enabled": [
            "reviews.path_instructions[].tools.golangci.enabled",
            "chat.enabled", "code_generation.enabled", "knowledge_base.enabled",
            "reviews.tools.a.enabled", "reviews.tools.b.enabled",
        ]}
        hint = checker._suggest_home(
            "enabled", index, "reviews.path_instructions[0].tools.enabled"
        )
        self.assertIn("reviews.path_instructions[].tools.golangci.enabled", hint)

    def test_a_suggestion_does_not_propose_the_offending_position_itself(self):
        # Self-exclusion also has to cross the two vocabularies.
        index = {"path": ["reviews.path_instructions[].path"]}
        self.assertEqual(
            checker._suggest_home("path", index, "reviews.path_instructions[0].path"), ""
        )

    def test_a_nan_key_is_still_found_in_the_composed_tree(self):
        # NaN is not equal to itself, so a plain dict lookup misses its own entry
        # and falls back to `str()` — the "names a key that is not in the file,
        # with no line" defect this lookup exists to prevent.
        findings, _ = checker.validate("reviews:\n  .nan: x\n", SCHEMA)
        self.assertEqual(len(findings), 1, msg=f"{messages(findings)}")
        self.assertEqual(findings[0][2], 2)
        self.assertIn(".nan", findings[0][1])

    def test_a_null_key_indexes_like_any_other(self):
        # Regression guard on the sentinel: None is an ordinary YAML key, and
        # must not be confused with "this key cannot be indexed".
        findings, _ = checker.validate("reviews:\n  null: x\n", SCHEMA)
        self.assertEqual([(f[1], f[2]) for f in findings], [("reviews.null", 2)])

    def test_a_merge_key_does_not_lose_the_keys_it_brings_in(self):
        # `safe_load` FLATTENS `<<: *anchor`, but the composed `reviews` node
        # holds only `<<` — so a merged-in key had no node, and fell back to
        # `str()` at `reviews.True` with no line.
        text = "base: &b\n  true: 1\nreviews:\n  <<: *b\n"
        findings, _ = checker.validate(text, SCHEMA)
        merged = [f for f in findings if f[1].startswith("reviews.")]
        self.assertEqual(len(merged), 1, msg=f"{messages(findings)}")
        self.assertEqual(merged[0][1], "reviews.true")
        self.assertEqual(merged[0][2], 2)

    def test_an_explicit_key_wins_over_a_merged_one(self):
        # YAML's own merge precedence, and the precedence `safe_load` applied.
        text = "base: &b\n  profil: merged\nreviews:\n  <<: *b\n  profil: explicit\n"
        findings, _ = checker.validate(text, SCHEMA)
        merged = [f for f in findings if f[1] == "reviews.profil"]
        self.assertEqual(len(merged), 1, msg=f"{messages(findings)}")
        self.assertEqual(merged[0][2], 5)  # the explicit key's line, not line 2

    def test_a_merge_nested_inside_an_anchor_is_expanded_too(self):
        # PyYAML's `flatten_mapping` is TRANSITIVE — it flattens the merged
        # mapping before splicing it in — so `profil` reaches `reviews` two merges
        # away and is a real key of the loaded document. Expanding only one level
        # indexed a literal `<<` instead, and the finding fell back to `str()`
        # with no line: the defect `_merge_entries` exists to fix, one deeper.
        text = "a: &a\n  profil: one\nb: &b\n  <<: *a\nreviews:\n  <<: *b\n"
        findings, _ = checker.validate(text, SCHEMA)
        merged = [f for f in findings if f[1].startswith("reviews.")]
        self.assertEqual(len(merged), 1, msg=f"{messages(findings)}")
        self.assertEqual(merged[0][1], "reviews.profil")
        self.assertEqual(merged[0][2], 2)  # `profil:` in the outermost anchor

    def test_a_nested_merge_does_not_index_the_merge_key_itself(self):
        # `<<` is not a key the loaded document has, so it must never reach the
        # index — one entry per real key, and nothing named `<<`.
        node = yaml.compose("a: &a\n  x: 1\nb: &b\n  <<: *a\n  y: 2\n")
        entries = checker._merge_entries(node.value[1][1])
        self.assertEqual([k.value for k, _v in entries], ["y", "x"])

    def test_a_key_under_a_merged_ANCESTOR_still_finds_its_line(self):
        # The ancestor arrives through the merge too, so the DESCENT has to go
        # through the index as well: `reviews.tools` is in the loaded document
        # and nowhere in the composed `reviews` node's own pairs, so a raw
        # text compare ended the descent at `<<` and lost the line.
        text = (
            "base: &b\n"
            "  tools:\n"
            "    htmlhint:\n"
            "      bogus: 1\n"
            "reviews:\n"
            "  <<: *b\n"
        )
        findings, _ = checker.validate(text, SCHEMA)
        hit = [f for f in findings if f[1].endswith("htmlhint.bogus")]
        self.assertEqual(len(hit), 1, msg=f"{messages(findings)}")
        self.assertEqual(hit[0][1], "reviews.tools.htmlhint.bogus")
        self.assertEqual(hit[0][2], 4)

    def test_a_descent_step_matches_the_key_safe_load_resolved(self):
        # `_descend`'s contract, pinned at the helper: a path part is a key
        # `safe_load` RESOLVED, so `on:` arrives as True — which a raw compare
        # against the node's source text `"on"` never matched, and which the
        # sequence branch would swallow as a list index (a bool IS an int).
        text = "reviews:\n  tools:\n    on:\n      bogus: 1\n"
        node = yaml.compose(text)
        step = checker._descend(checker._descend(node, "reviews"), "tools")
        self.assertIsNotNone(checker._descend(step, True))

    def test_a_self_referential_alias_does_not_hang_the_merge_expansion(self):
        # `&x [*x]` composes into a cyclic node graph.
        node = yaml.compose("a: &x [*x]\n")
        self.assertEqual(checker._merge_entries(node.value[0][1]), [])

    def test_a_deep_merge_chain_degrades_instead_of_crashing(self):
        # PR-controlled input: ~1500 `<<:` links is a few tens of KB, far inside
        # MAX_CONFIG_BYTES. The expansion here is recursive and `RecursionError`
        # is not a `yaml.YAMLError`, so unbounded it escapes `validate` and kills
        # an advisory check with a traceback. Past `_MAX_MERGE_DEPTH` the merge is
        # simply not expanded — a lost line number, not a crash.
        n = 1500
        text = (
            "a0: &a0\n  profil: 1\n"
            + "".join(f"a{i}: &a{i}\n  <<: *a{i - 1}\n" for i in range(1, n))
            + f"reviews:\n  <<: *a{n - 1}\n"
        )
        findings, _ = checker.validate(text, SCHEMA)  # must not raise
        self.assertEqual(len(findings), checker.MAX_FINDINGS)

    def test_a_merge_chain_inside_the_bound_still_resolves_its_line(self):
        # The bound is a backstop, not the working path: a chain a human might
        # actually write still gets its key node and its line.
        n = 20
        text = (
            "a0: &a0\n  profil: 1\n"
            + "".join(f"a{i}: &a{i}\n  <<: *a{i - 1}\n" for i in range(1, n))
            + f"reviews:\n  <<: *a{n - 1}\n"
        )
        findings, _ = checker.validate(text, SCHEMA)
        merged = [f for f in findings if f[1] == "reviews.profil"]
        self.assertEqual(len(merged), 1, msg=f"{messages(findings)}")
        self.assertEqual(merged[0][2], 2)  # `profil:` in the first anchor

    def test_a_merge_chain_the_loader_itself_cannot_build_exits_two(self):
        # `flatten_mapping` recurses per link too, and in this shape (anchors in a
        # lazily-constructed sequence, so `reviews` is flattened first) PyYAML
        # blows its own stack before this file gets a turn. "I could not check
        # this" must exit 2, like an oversized config — never 0, and never a
        # traceback.
        import tempfile

        n = 1500
        text = (
            "_anchors:\n  - &a0\n    profil: 1\n"
            + "".join(f"  - &a{i}\n    <<: *a{i - 1}\n" for i in range(1, n))
            + f"reviews:\n  <<: *a{n - 1}\n"
        )
        with self.assertRaises(checker.ConfigError):
            checker.validate(text, SCHEMA)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".coderabbit.yaml"), "w", encoding="utf-8") as f:
                f.write(text)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = checker.main(["--root", tmp, "--schema", SCHEMA_PATH])
            out = buf.getvalue()
        self.assertEqual(code, 2, msg=out)
        self.assertIn("::error::", out)

    def test_an_integer_mapping_key_is_not_read_as_a_list_index(self):
        # `reviews.mutually_exclusive_groups` accepts arbitrary property names, so
        # `1:` is a legal key there and a rejecting error below it arrives with an
        # INT path part while the node is a mapping. Typing the part rather than
        # the node sent the descent down the sequence branch: no line at all, and
        # a path (`...[1]`) naming a list position the file does not have.
        text = "reviews:\n  mutually_exclusive_groups:\n    1:\n      - only-one\n"
        findings, _ = checker.validate(text, SCHEMA)
        short = [f for f in findings if "too short" in f[3]]
        self.assertEqual(len(short), 1, msg=f"{messages(findings)}")
        self.assertEqual(short[0][1], "reviews.mutually_exclusive_groups.1")
        self.assertEqual(short[0][2], 4)

    def test_a_duplicate_key_is_annotated_on_the_one_safe_load_kept(self):
        # `construct_mapping` assigns per pair, so the LAST duplicate wins and the
        # error is about ITS value — annotating line 2's valid `chill` for a
        # complaint about line 3's `7` points the reader at the wrong line.
        text = "reviews:\n  profile: chill\n  profile: 7\n"
        findings, _ = checker.validate(text, SCHEMA)
        self.assertTrue(findings, msg="expected a type/enum error")
        self.assertEqual({f[2] for f in findings}, {3}, msg=f"{findings}")

    def test_the_later_of_two_merge_keys_wins_like_safe_load(self):
        # `flatten_mapping` accumulates a-then-b into ONE merge list and dict
        # assignment lets b win, so a first-wins pass over the merge groups in
        # document order picked a — the opposite of the loaded document.
        text = "a: &a\n  profil: 1\nb: &b\n  profil: 2\nreviews:\n  <<: *a\n  <<: *b\n"
        findings, _ = checker.validate(text, SCHEMA)
        merged = [f for f in findings if f[1] == "reviews.profil"]
        self.assertEqual(len(merged), 1, msg=f"{messages(findings)}")
        self.assertEqual(merged[0][2], 4)  # b's `profil:`, not a's on line 2

    def test_scalar_elements_are_charged_against_the_walk_budget(self):
        # Exempting a leaf from the BAIL-OUT FLAG is not the same as exempting it
        # from the BUDGET. The list branch recurses once per element with no
        # budget check of its own, so an uncharged leaf visit would let a single
        # aliased list of scalars cost one unit and buy unboundedly many calls —
        # `MAX_WALK_KEYS` would then bound non-empty containers, not work done.
        data = {"c": [1] * 50, "d": {"unk": 1}}
        schema = {
            "type": "object",
            "properties": {
                "c": {"type": "array", "items": {"type": "object", "properties": {}}},
                "d": {"type": "object", "properties": {}},
            },
        }
        with budget(keys=20):  # 6 units without the per-element charge, 56 with it
            findings, bounded = checker._walk_unknown_keys(
                data, schema, None, {}, "warning", 100
            )
        self.assertTrue(bounded, msg=f"{findings}")
        self.assertEqual(findings, [])

    def test_spending_the_last_budget_on_a_scalar_is_not_a_partial_walk(self):
        # The trailing `walk(child, ...)` for a KNOWN key whose value is a scalar
        # has nothing to inspect, so arriving there with an exhausted budget
        # leaves nothing unchecked — and must not report a fully walked document
        # as stopped early, which is a hard error under strict mode.
        text = "reviews:\n  profile: chill\n"
        with budget(keys=4):  # two node visits + two keys, nothing left over
            findings, _ = checker.validate(text, SCHEMA)
        self.assertEqual(findings, [], msg=f"{messages(findings)}")

    def test_an_incomplete_scan_message_names_what_the_counter_counts(self):
        # The budget is charged per node visit as well as per key, so a
        # list-heavy document exhausts it after far fewer than MAX_WALK_KEYS keys.
        with budget(keys=1):
            findings, _ = checker.validate(STALE_GITHUB_CHECKS, SCHEMA)
        self.assertIn("keys and nodes", findings[0][3])


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
        self.assertEqual(len(omitted), 104)

    def test_every_explicitly_open_object_uses_additional_properties(self):
        # The audit behind `_OPENER_KEYWORDS`: the schema uses one opener today,
        # and the wider list is future-proofing, not a description of it.
        for node in self._nodes(SCHEMA):
            if not isinstance(node.get("properties"), dict):
                continue
            used = [k for k in checker._OPENER_KEYWORDS if k in node]
            self.assertIn(used, ([], ["additionalProperties"]), msg=f"{used}")

    def test_the_schema_carries_no_ref(self):
        # `_walk_unknown_keys` claims the two unknown-key halves never report the
        # same key. That is a fact about THIS schema, not about the conditions:
        # jsonschema evaluates a document path against every applicable subschema,
        # so a node this walk judges closed (no opener of its OWN) whose `$ref`
        # target carried `additionalProperties: false` would be reported twice.
        # A combinator sibling cannot do it — the test above pins that a node
        # declaring `properties` never carries one — but a `$ref` could, so a
        # refresh that introduces one should fail HERE, where the docstring's
        # premise is written down, rather than start double-annotating real PRs.
        raw = json.dumps(SCHEMA)
        self.assertNotIn('"$ref"', raw)
        self.assertNotIn('"$dynamicRef"', raw)

    def test_a_rejecting_error_cannot_be_crowded_out_before_ranking(self):
        """The premise `_apply_cap` cannot enforce: the `islice` runs a stage earlier.

        `islice(iter_errors(...), MAX_FINDINGS + 1)` is unranked FIFO in schema
        order, so a file-rejecting error arriving past the cap is deleted before
        ranking ever sees it — the same exit-0-on-a-whole-file-rejection the rank
        exists to prevent. It needs MAX_FINDINGS *stripped* errors ahead of it
        (one rejecting error among them and the exit code is 1 regardless), and
        against THIS schema that cannot be assembled. Four facts make it so, each
        asserted below rather than summarized in a number — `maxItems` is NOT one
        of them as a multiplicity bound, because jsonschema keeps validating
        elements past it (a 150-element array really does raise 150 of them).
        """
        stripped_false, unbounded_multipliers, arrays_over_closed = [], [], []

        def walk(node, path, multiplied):
            if isinstance(node, list):
                for i, item in enumerate(node):
                    walk(item, f"{path}[{i}]", multiplied)
                return
            if not isinstance(node, dict):
                return
            for keyword in checker.STRIPPED_KEYWORDS:
                if node.get(keyword) is False:
                    (arrays_over_closed if multiplied else stripped_false).append(path)
            for key in ("properties", "$defs", "definitions", "dependentSchemas"):
                for name, sub in (node.get(key) or {}).items():
                    walk(sub, f"{path}.{key}.{name}", multiplied)
            for key in ("propertyNames", "not", "if", "then", "else"):
                walk(node.get(key), f"{path}.{key}", multiplied)
            for key in ("anyOf", "oneOf", "allOf"):
                walk(node.get(key), f"{path}.{key}", multiplied)
            # A schema-valued `additionalProperties` / `patternProperties` applies
            # to unboundedly many properties of ONE instance object, so anything
            # closed underneath is multiplied by nothing this bound can see.
            for key in ("additionalProperties", "patternProperties"):
                sub = node.get(key)
                subs = sub.values() if key == "patternProperties" and isinstance(sub, dict) else [sub]
                for one in subs:
                    if isinstance(one, dict):
                        before = len(stripped_false) + len(arrays_over_closed)
                        walk(one, f"{path}.{key}", multiplied)
                        if len(stripped_false) + len(arrays_over_closed) > before:
                            unbounded_multipliers.append(f"{path}.{key}")
            for key in ("items", "prefixItems", "unevaluatedItems", "contains"):
                if key in node:
                    before = len(stripped_false) + len(arrays_over_closed)
                    walk(node[key], f"{path}.{key}", True)
                    if len(stripped_false) + len(arrays_over_closed) > before:
                        arrays_over_closed.append((path, node))

        walk(SCHEMA, "$", False)

        # 1. `unevaluatedProperties` is the other stripped keyword and is absent,
        #    so `additionalProperties: false` is the only source.
        self.assertNotIn('"unevaluatedProperties"', json.dumps(SCHEMA))

        # 2. Nothing closed sits under a schema-valued `additionalProperties` or a
        #    `patternProperties` — one instance object could then raise arbitrarily
        #    many stripped errors with no rejecting error implied.
        self.assertEqual(unbounded_multipliers, [])

        # 3. Every array over a closed object declares `maxItems`, and declares it
        #    BEFORE `items` in key order. That is what actually saves this case:
        #    exceeding it raises a REJECTING `maxItems` error, and `iter_errors`
        #    walks a node's keywords in schema key order, so that error is emitted
        #    ahead of the element errors it would otherwise be buried under.
        for path, array in [x for x in arrays_over_closed if isinstance(x, tuple)]:
            keys = list(array.keys())
            self.assertIn("maxItems", keys, msg=path)
            self.assertLess(keys.index("maxItems"), keys.index("items"), msg=path)

        # 4. What is left is unmultiplied: at most one stripped error each.
        self.assertLess(len(stripped_false), checker.MAX_FINDINGS, msg=f"{stripped_false}")

    def test_the_document_root_is_the_explicitly_closed_case(self):
        self.assertIs(SCHEMA.get("additionalProperties"), False)

    def test_github_checks_names_only_enabled(self):
        # If upstream ever adds `timeout_ms` back, the regression anchor above
        # stops being a finding and this fails first, naming why.
        tools = SCHEMA["properties"]["reviews"]["properties"]["tools"]["properties"]
        self.assertEqual(list(tools["github-checks"]["properties"]), ["enabled"])


if __name__ == "__main__":
    unittest.main()
