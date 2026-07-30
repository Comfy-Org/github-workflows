#!/usr/bin/env python3
"""Tests for config.py — the GROOM_CONFIG resolution layer (BE-5227).

The contract worth pinning is mostly about what CANNOT happen: a malformed
variable must not take groom offline, and a variable must not be able to reach
the locked security knobs. Most of these are therefore negative tests.
"""

import io
import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402


def resolve(defaults, config_raw="", dispatch_raw=""):
    """Resolve, discarding warnings. Returns the resolved knob dict."""
    with unittest.mock.patch("sys.stderr", new_callable=io.StringIO):
        return config.resolve(
            defaults=config.coerce_layer(
                config.parse_layer(json.dumps(defaults), label="d"), label="d", trusted=True
            ),
            config_raw=config_raw,
            dispatch_raw=dispatch_raw,
        )


def warnings_from(defaults, config_raw="", dispatch_raw=""):
    """Resolve and return the emitted warning text."""
    buf = io.StringIO()
    with unittest.mock.patch("sys.stderr", buf):
        config.resolve(
            defaults=config.coerce_layer(
                config.parse_layer(json.dumps(defaults), label="d"), label="d", trusted=True
            ),
            config_raw=config_raw,
            dispatch_raw=dispatch_raw,
        )
    return buf.getvalue()


class TestPrecedence(unittest.TestCase):
    def test_no_config_is_a_passthrough(self):
        """The whole back-compat promise: an unset variable changes nothing."""
        defaults = {"max_prs": "1", "dry_run": False, "max_findings": 12}
        self.assertEqual(resolve(defaults), defaults)

    def test_blank_and_whitespace_config_are_passthrough(self):
        defaults = {"max_prs": "1"}
        for raw in ("", "   ", "\n"):
            self.assertEqual(resolve(defaults, raw), defaults, repr(raw))

    def test_config_overrides_caller_defaults(self):
        got = resolve({"max_prs": "1"}, '{"max_prs": 3}')
        self.assertEqual(got["max_prs"], "3")

    def test_dispatch_beats_config(self):
        got = resolve({"max_prs": "1"}, '{"max_prs": 3}', '{"max_prs": 5}')
        self.assertEqual(got["max_prs"], "5")

    def test_untouched_defaults_survive_an_override(self):
        got = resolve({"max_prs": "1", "model": "claude-opus-5"}, '{"max_prs": 3}')
        self.assertEqual(got["model"], "claude-opus-5")

    def test_explicit_null_does_not_override(self):
        """`null` means "leave this knob alone", not "set it to None"."""
        got = resolve({"max_prs": "1"}, '{"max_prs": null}')
        self.assertEqual(got["max_prs"], "1")


class TestFailOpen(unittest.TestCase):
    """A bad variable must degrade to the caller's values, never abort."""

    def test_malformed_json_falls_back_entirely(self):
        defaults = {"max_prs": "1", "dry_run": False}
        self.assertEqual(resolve(defaults, "{not json"), defaults)

    def test_malformed_json_warns(self):
        self.assertIn("not valid JSON", warnings_from({"max_prs": "1"}, "{oops"))

    def test_non_object_json_falls_back(self):
        defaults = {"max_prs": "1"}
        for raw in ("[1,2,3]", '"a string"', "42", "null"):
            self.assertEqual(resolve(defaults, raw), defaults, raw)

    def test_one_bad_key_does_not_discard_the_good_ones(self):
        got = resolve({"max_prs": "1"}, '{"max_prs": 3, "max_findings": "abc"}')
        self.assertEqual(got["max_prs"], "3")
        self.assertNotIn("max_findings", got)

    def test_paths_is_not_a_config_knob(self):
        """Path scoping is #83's `path` input, backed by scope.py — not a config
        knob here. Pinned so re-adding it is a deliberate act, not a merge
        accident that ships two competing implementations of one feature."""
        self.assertNotIn("paths", config._OPERATIONAL_KEYS)
        out = warnings_from({}, '{"paths": ["src"]}')
        self.assertIn("unknown key", out)
        self.assertNotIn("paths", resolve({}, '{"paths": ["src"]}'))

    def test_unknown_key_warns_and_is_dropped(self):
        out = warnings_from({}, '{"maxprs": 3}')
        self.assertIn("unknown key", out)
        self.assertNotIn("maxprs", resolve({}, '{"maxprs": 3}'))

    def test_arithmetic_overflow_does_not_escape_as_a_traceback(self):
        """`int(float(x))` raises OverflowError, not ValueError, for these."""
        for raw in ('{"max_findings": 1e999}', '{"max_findings": "inf"}',
                    '{"max_findings": Infinity}', '{"max_findings": ' + "1" * 320 + "}"):
            self.assertNotIn("max_findings", resolve({}, raw), raw)

    def test_json_edge_cases_that_are_not_jsondecodeerror(self):
        """A 4300+-digit int and a deeply nested array both fit in a variable."""
        defaults = {"max_prs": "1"}
        for raw in ("{\"max_findings\": " + "1" * 4400 + "}", "[" * 200000 + "]" * 200000):
            self.assertEqual(resolve(defaults, raw), defaults, raw[:40])

    def test_cli_exits_zero_on_an_overflowing_value(self):
        """The end-to-end fail-open contract, not just the coercer."""
        with unittest.mock.patch("sys.stdout", io.StringIO()), \
             unittest.mock.patch("sys.stderr", io.StringIO()):
            rc = config.main(["--defaults-json", "{}", "--config-json", '{"max_findings": "inf"}'])
        self.assertEqual(rc, 0)


class TestLockedKeys(unittest.TestCase):
    """The security boundary: a variable must not reach these."""

    def test_every_locked_key_is_ignored(self):
        for key in config._LOCKED_KEYS:
            raw = json.dumps({key: True})
            self.assertNotIn(key, resolve({}, raw), f"{key} leaked through")

    def test_builder_cannot_be_enabled_by_variable(self):
        got = resolve({}, '{"builder": true}')
        self.assertNotIn("builder", got)

    def test_locked_key_warns_loudly_rather_than_silently_dropping(self):
        out = warnings_from({}, '{"builder": true}')
        self.assertIn("builder", out)
        self.assertIn("security boundary", out)

    def test_pr_size_limit_is_locked(self):
        self.assertNotIn("pr_size_limit", resolve({}, '{"pr_size_limit": 99999}'))

    def test_locked_key_does_not_block_sibling_operational_keys(self):
        got = resolve({}, '{"builder": true, "max_prs": 2}')
        self.assertEqual(got["max_prs"], "2")
        self.assertNotIn("builder", got)

    def test_reviewed_caller_defaults_keep_their_locked_values(self):
        """The defaults layer IS the workflow file — the thing the lock protects."""
        got = resolve({"builder": True, "pr_size_limit": 800})
        self.assertIs(got["builder"], True)
        self.assertEqual(got["pr_size_limit"], 800)

    def test_a_reviewed_default_does_not_fire_the_bypass_warning(self):
        """Warning on every normal run would desensitize operators to a real one."""
        out = warnings_from({"builder": True}, "")
        self.assertNotIn("security boundary", out)

    def test_the_variable_still_cannot_flip_a_reviewed_locked_value(self):
        got = resolve({"builder": False}, '{"builder": true}')
        self.assertIs(got["builder"], False)


class TestBoolCoercion(unittest.TestCase):
    def test_json_booleans(self):
        self.assertIs(resolve({}, '{"dry_run": true}')["dry_run"], True)
        self.assertIs(resolve({}, '{"dry_run": false}')["dry_run"], False)

    def test_string_booleans_any_case(self):
        for raw, want in (("true", True), ("TRUE", True), ("False", False)):
            got = resolve({}, json.dumps({"dry_run": raw}))
            self.assertIs(got["dry_run"], want, raw)

    def test_truthy_junk_is_refused_not_guessed(self):
        """"yes"/1 must NOT silently read as a boolean — that would flip filing."""
        for raw in ('{"dry_run": "yes"}', '{"dry_run": 1}', '{"dry_run": "on"}'):
            self.assertNotIn("dry_run", resolve({}, raw), raw)


class TestNumericCoercion(unittest.TestCase):
    def test_max_prs_stays_a_string(self):
        """build_select owns the clamp; this layer must not pre-empt it."""
        self.assertIsInstance(resolve({}, '{"max_prs": 3}')["max_prs"], str)

    def test_numeric_string_accepted(self):
        self.assertEqual(resolve({}, '{"max_prs": "3"}')["max_prs"], "3")

    def test_bool_is_not_a_number(self):
        """bool is an int subclass in Python — reject it explicitly."""
        self.assertNotIn("max_prs", resolve({}, '{"max_prs": true}'))

    def test_negative_max_findings_refused(self):
        """A negative cap would make the [:cap] slice count from the END."""
        self.assertNotIn("max_findings", resolve({}, '{"max_findings": -3}'))

    def test_fractional_max_findings_floored(self):
        self.assertEqual(resolve({}, '{"max_findings": 12.9}')["max_findings"], 12)

    def test_zero_max_findings_is_valid(self):
        self.assertEqual(resolve({}, '{"max_findings": 0}')["max_findings"], 0)

    def test_interval_days_stays_a_string_for_interval_py(self):
        self.assertIsInstance(resolve({}, '{"interval_days": 3}')["interval_days"], str)

    def test_negative_fraction_is_not_floored_into_acceptance(self):
        """int() truncates toward zero, so -0.5 must not become an accepted 0."""
        self.assertNotIn("max_findings", resolve({}, '{"max_findings": -0.5}'))

    def test_non_finite_numeric_strings_refused(self):
        """inf/nan pass float() but degrade downstream in opposite directions."""
        for key in ("max_prs", "interval_days", "cadence"):
            for bad in ("inf", "-inf", "nan", "Infinity"):
                raw = json.dumps({key: bad})
                self.assertNotIn(key, resolve({}, raw), raw)

    def test_negative_numeric_string_refused_with_a_named_warning(self):
        """The downstream clamp would silently absorb it; name the key instead."""
        self.assertNotIn("max_prs", resolve({}, '{"max_prs": -5}'))
        self.assertIn("max_prs", warnings_from({}, '{"max_prs": -5}'))

    def test_zero_interval_days_still_honored(self):
        """0 = "no throttle" is a documented, valid value — not a negative."""
        self.assertEqual(resolve({}, '{"interval_days": 0}')["interval_days"], "0")


class TestProseSanitization(unittest.TestCase):
    """These land in the trusted brief, so bound them."""

    def test_newlines_collapsed(self):
        got = resolve({}, json.dumps({"themes": "dead code\nNEW INSTRUCTION"}))
        self.assertNotIn("\n", got["themes"])

    def test_prose_capped(self):
        got = resolve({}, json.dumps({"scope_desc": "x" * (config._PROSE_MAX + 500)}))
        self.assertLessEqual(len(got["scope_desc"]), config._PROSE_MAX)

    def test_non_string_prose_refused(self):
        self.assertNotIn("themes", resolve({}, '{"themes": 42}'))

    def test_blank_scope_desc_does_not_blank_the_brief(self):
        """It is spliced into "Scan {{SCOPE_DESC}}." — empty is unusable, not wider."""
        for raw in ('{"scope_desc": "   "}', '{"scope_desc": ""}', '{"scope_desc": "\\n"}'):
            got = resolve({"scope_desc": "the whole repository"}, raw)
            self.assertEqual(got["scope_desc"], "the whole repository", raw)

    def test_blank_themes_clears_a_pinned_theme_list(self):
        """Unlike scope_desc, an empty themes list is meaningful: no restriction."""
        got = resolve({"themes": "dead code"}, '{"themes": ""}')
        self.assertEqual(got["themes"], "")


class TestModelValidation(unittest.TestCase):
    def test_valid_model_id(self):
        self.assertEqual(resolve({}, '{"model": "claude-opus-5"}')["model"], "claude-opus-5")

    def test_flag_shaped_model_refused(self):
        """Leading `-` is the one shape worth attempting; refuse it."""
        self.assertNotIn("model", resolve({}, '{"model": "--dangerously-skip-permissions"}'))

    def test_model_with_shell_metacharacters_refused(self):
        for bad in ('{"model": "a b"}', '{"model": "a;b"}', '{"model": "a$(b)"}'):
            self.assertNotIn("model", resolve({}, bad), bad)


class TestScopeLabel(unittest.TestCase):
    """scope_label re-keys the whole dedup ledger, so it must announce itself."""

    def test_change_warns_about_the_ledger(self):
        out = warnings_from({"scope_label": "whole-repo"}, '{"scope_label": "services/ingest"}')
        self.assertIn("dedup signature", out)
        self.assertIn("re-propose", out)

    def test_same_value_does_not_warn(self):
        out = warnings_from({"scope_label": "whole-repo"}, '{"scope_label": "whole-repo"}')
        self.assertNotIn("dedup signature", out)

    def test_invalid_shape_refused(self):
        self.assertNotIn("scope_label", resolve({}, '{"scope_label": "has spaces!"}'))


class TestCliOutputShape(unittest.TestCase):
    def test_stdout_is_exactly_one_line_of_json(self):
        """It is written verbatim as one $GITHUB_OUTPUT line."""
        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf), unittest.mock.patch("sys.stderr", io.StringIO()):
            config.main([
                "--defaults-json", '{"max_prs": "1"}',
                "--config-json", '{"themes": "a\\nb", "paths": ["src"]}',
            ])
        out = buf.getvalue()
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIsInstance(json.loads(out), dict)

    def test_exits_zero_on_garbage_config(self):
        with unittest.mock.patch("sys.stdout", io.StringIO()), \
             unittest.mock.patch("sys.stderr", io.StringIO()):
            rc = config.main(["--defaults-json", "{}", "--config-json", "{broken"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
