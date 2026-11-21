import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lib


class ParsePatternsTests(unittest.TestCase):
    def test_single_pattern(self):
        patterns = lib.parse_patterns(r"statsd\.Gauge\(")
        self.assertEqual(len(patterns), 1)
        self.assertTrue(patterns[0].search("statsd.Gauge(foo)"))

    def test_blank_lines_and_comments_skipped(self):
        patterns = lib.parse_patterns("# a comment\n\nposthog\\.capture\\(\n\n")
        self.assertEqual(len(patterns), 1)

    def test_multiple_lines(self):
        patterns = lib.parse_patterns("foo\nbar")
        self.assertEqual(len(patterns), 2)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            lib.parse_patterns("")

    def test_only_comments_raises(self):
        with self.assertRaises(ValueError):
            lib.parse_patterns("# nothing here\n\n")

    def test_invalid_regex_raises(self):
        with self.assertRaises(ValueError):
            lib.parse_patterns("foo(")


class AddedLinesTests(unittest.TestCase):
    PATCH = (
        "@@ -1,3 +1,4 @@\n"
        " unchanged line\n"
        "-removed line\n"
        "+added line one\n"
        "+added line two\n"
    )

    def test_extracts_only_added_content(self):
        lines = lib.added_lines(self.PATCH)
        self.assertEqual(lines, ["added line one", "added line two"])

    def test_file_header_not_treated_as_added(self):
        patch = "--- a/x.go\n+++ b/x.go\n@@ -1 +1 @@\n+real_added_line\n"
        self.assertEqual(lib.added_lines(patch), ["real_added_line"])

    def test_empty_patch(self):
        self.assertEqual(lib.added_lines(""), [])
        self.assertEqual(lib.added_lines(None), [])


class FirstMatchTests(unittest.TestCase):
    def setUp(self):
        self.patterns = lib.parse_patterns(r"statsd\.Gauge\(" + "\n" + r"posthog\.capture\(")

    def test_matches_added_line(self):
        files = [{"filename": "a.go", "patch": "+statsd.Gauge(\"x\", 1)\n"}]
        match = lib.first_match(files, self.patterns)
        self.assertEqual(match, ("a.go", r"statsd\.Gauge\("))

    def test_no_match_on_removed_line_only(self):
        files = [{"filename": "a.go", "patch": "-statsd.Gauge(\"x\", 1)\n"}]
        self.assertIsNone(lib.first_match(files, self.patterns))

    def test_no_match_when_pattern_absent(self):
        files = [{"filename": "a.go", "patch": "+fmt.Println(\"hi\")\n"}]
        self.assertIsNone(lib.first_match(files, self.patterns))

    def test_file_without_patch_skipped(self):
        files = [{"filename": "binary.png"}]
        self.assertIsNone(lib.first_match(files, self.patterns))

    def test_second_pattern_matches_second_file(self):
        files = [
            {"filename": "a.go", "patch": "+fmt.Println(1)\n"},
            {"filename": "b.ts", "patch": "+posthog.capture('x')\n"},
        ]
        match = lib.first_match(files, self.patterns)
        self.assertEqual(match, ("b.ts", r"posthog\.capture\("))

    def test_empty_files_list(self):
        self.assertIsNone(lib.first_match([], self.patterns))

    def test_anchored_pattern_matches_a_later_added_line(self):
        # Regression: each added line is matched on its own. If lines were joined into one
        # blob, `^` would only bind to the blob's start (without re.MULTILINE), so an
        # anchored pattern would see line one and miss every added line after it.
        anchored = lib.parse_patterns(r"^\s*statsd\.Gauge\(")
        files = [{"filename": "a.go", "patch": (
            "+fmt.Println(1)\n"
            "+    statsd.Gauge(\"x\", 1)\n"
        )}]
        match = lib.first_match(files, anchored)
        self.assertEqual(match, ("a.go", r"^\s*statsd\.Gauge\("))

    def test_pattern_does_not_false_match_across_two_added_lines(self):
        # Regression: joining added lines into one blob would let a pattern containing `\s`
        # (which matches a literal newline) span two unrelated added lines and match content
        # nobody actually wrote on one line.
        spanning = lib.parse_patterns(r"foo\s+bar")
        files = [{"filename": "a.go", "patch": (
            "+some_call(foo)\n"
            "+bar_other_call()\n"
        )}]
        self.assertIsNone(lib.first_match(files, spanning))


class UnscannableFilesTests(unittest.TestCase):
    def test_missing_patch_with_additions_is_unscannable(self):
        files = [{"filename": "huge.go", "additions": 500, "status": "modified"}]
        self.assertEqual(lib.unscannable_files(files), ["huge.go"])

    def test_missing_patch_but_removed_is_not_unscannable(self):
        files = [{"filename": "gone.go", "additions": 0, "status": "removed"}]
        self.assertEqual(lib.unscannable_files(files), [])

    def test_missing_patch_with_zero_additions_is_not_unscannable(self):
        files = [{"filename": "binary.png", "additions": 0, "status": "modified"}]
        self.assertEqual(lib.unscannable_files(files), [])

    def test_file_with_patch_is_not_unscannable(self):
        files = [{"filename": "a.go", "patch": "+x\n", "additions": 1, "status": "modified"}]
        self.assertEqual(lib.unscannable_files(files), [])

    def test_multiple_unscannable_files_in_order(self):
        files = [
            {"filename": "a.go", "additions": 10, "status": "modified"},
            {"filename": "b.go", "patch": "+x\n", "additions": 1, "status": "modified"},
            {"filename": "c.go", "additions": 20, "status": "added"},
        ]
        self.assertEqual(lib.unscannable_files(files), ["a.go", "c.go"])

    def test_empty_files_list(self):
        self.assertEqual(lib.unscannable_files([]), [])


class CostImpactSectionTests(unittest.TestCase):
    def test_present_with_content(self):
        body = "Summary\n\n## Cost impact\n\nBills per event; forecast 1k/day.\n\n## Testing\n"
        self.assertTrue(lib.has_cost_impact_section(body))

    def test_case_insensitive_and_any_heading_depth(self):
        self.assertTrue(lib.has_cost_impact_section("### cost IMPACT\nsome text"))

    def test_absent(self):
        self.assertFalse(lib.has_cost_impact_section("## Summary\nnothing about cost"))

    def test_empty_section_does_not_count(self):
        body = "## Cost impact\n\n## Testing\nstuff"
        self.assertFalse(lib.has_cost_impact_section(body))

    def test_heading_with_only_whitespace_does_not_count(self):
        body = "## Cost impact\n   \n\n## Testing\n"
        self.assertFalse(lib.has_cost_impact_section(body))

    def test_section_at_end_of_body(self):
        body = "## Cost impact\nBills $X/month."
        self.assertTrue(lib.has_cost_impact_section(body))

    def test_empty_body(self):
        self.assertFalse(lib.has_cost_impact_section(""))
        self.assertFalse(lib.has_cost_impact_section(None))

    def test_unfilled_html_comment_placeholder_does_not_count(self):
        body = "## Cost impact\n\n<!-- priced unit, baseline, protected signal -->\n\n## Testing\n"
        self.assertFalse(lib.has_cost_impact_section(body))

    def test_multiline_html_comment_placeholder_does_not_count(self):
        body = "## Cost impact\n\n<!--\npriced unit\nbaseline\n-->\n\n## Testing\n"
        self.assertFalse(lib.has_cost_impact_section(body))

    def test_text_on_heading_line_does_not_count_as_content(self):
        self.assertFalse(lib.has_cost_impact_section("## Cost impact: TBD\n\n## Testing\n"))
        self.assertFalse(lib.has_cost_impact_section("## Cost impact (required)\n\n## Testing\n"))
        self.assertFalse(lib.has_cost_impact_section("## Cost impact ##\n\n## Testing\n"))

    def test_example_inside_fenced_block_does_not_count(self):
        body = "## Cost impact\n\n```\n## Cost impact\nBills $X/month.\n```\n\n## Testing\n"
        self.assertFalse(lib.has_cost_impact_section(body))

    def test_real_content_after_placeholder_comment_still_counts(self):
        body = ("## Cost impact\n\n<!-- priced unit, baseline, protected signal -->\n"
                "Bills per event; forecast 1k/day.\n\n## Testing\n")
        self.assertTrue(lib.has_cost_impact_section(body))

    def test_heading_requires_space_not_just_any_whitespace(self):
        self.assertFalse(lib.has_cost_impact_section("##\ncost impact\nsome text\n"))


class SoftFailEnabledTests(unittest.TestCase):
    def test_true_string(self):
        self.assertTrue(lib.soft_fail_enabled("true"))
        self.assertTrue(lib.soft_fail_enabled("TRUE"))

    def test_absent_is_false(self):
        self.assertFalse(lib.soft_fail_enabled(None))
        self.assertFalse(lib.soft_fail_enabled(""))

    def test_false_string(self):
        self.assertFalse(lib.soft_fail_enabled("false"))


class EnforceEnabledTests(unittest.TestCase):
    def test_true_string(self):
        self.assertTrue(lib.enforce_enabled("true"))
        self.assertTrue(lib.enforce_enabled("TRUE"))

    def test_absent_is_false(self):
        """An omitted `enforce` input is the documented warn-only pilot default, not a
        silent fail-closed gate."""
        self.assertFalse(lib.enforce_enabled(None))
        self.assertFalse(lib.enforce_enabled(""))

    def test_false_string(self):
        self.assertFalse(lib.enforce_enabled("false"))


class FailureOutcomeTests(unittest.TestCase):
    def test_enforce_fails_closed(self):
        outcome = lib.failure_outcome("missing_cost_impact", enforce=True, soft_fail=True)
        self.assertEqual(outcome.state, "failure")
        self.assertEqual(outcome.exit_code, 1)
        self.assertFalse(outcome.advisory)

    def test_warn_only_soft_fail_is_red_but_exits_zero(self):
        outcome = lib.failure_outcome("missing_cost_impact", enforce=False, soft_fail=True)
        self.assertEqual(outcome.state, "failure")
        self.assertEqual(outcome.exit_code, 0)
        self.assertTrue(outcome.advisory)

    def test_warn_only_silent_is_green(self):
        outcome = lib.failure_outcome("missing_cost_impact", enforce=False, soft_fail=False)
        self.assertEqual(outcome.state, "success")
        self.assertEqual(outcome.exit_code, 0)
        self.assertFalse(outcome.advisory)

    def test_unknown_category_raises(self):
        with self.assertRaises(KeyError):
            lib.failure_outcome("not_a_category", enforce=True, soft_fail=True)

    def test_every_category_has_guidance_and_headline(self):
        for category in ("missing_cost_impact", "changed_files_unavailable"):
            self.assertTrue(lib.failure_guidance(category))
            lib.failure_outcome(category, enforce=True, soft_fail=True)

    def test_descriptions_pairwise_distinct(self):
        descriptions = {
            lib.failure_outcome(c, enforce=True, soft_fail=True).description
            for c in ("missing_cost_impact", "changed_files_unavailable")
        }
        self.assertEqual(len(descriptions), 2)


if __name__ == "__main__":
    unittest.main()
