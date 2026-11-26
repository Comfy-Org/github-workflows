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

    def test_content_line_starting_with_plus_plus_is_added(self):
        # GitHub's per-file `patch` fragments start at the first `@@` and never carry the
        # `+++`/`---` file-header lines a full multi-file diff would have before it. A real
        # added line whose own content starts with `++` (e.g. `++i, posthog.capture(...)`) is
        # rendered as `+++i, ...` and must still be scanned, not excluded as if it were a
        # file header.
        patch = "@@ -1 +1 @@\n+++i, posthog.capture('x')\n"
        self.assertEqual(lib.added_lines(patch), ["++i, posthog.capture('x')"])

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
            "+foo\n"
            "+bar\n"
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

    def test_deeper_subheadings_stay_inside_the_section(self):
        body = "## Cost impact\n### Priced unit\nevents\n### Baseline\n10k/day\n"
        self.assertTrue(lib.has_cost_impact_section(body))

    def test_shallower_heading_still_ends_a_deep_section(self):
        body = "### Cost impact\nsome text\n## Testing\nstuff"
        self.assertTrue(lib.has_cost_impact_section(body))
        self.assertFalse(lib.has_cost_impact_section(
            "### Cost impact\n## Testing\nstuff"))

    def test_tilde_fenced_example_does_not_count(self):
        body = "## Cost impact\n~~~\nExample only\n~~~\n"
        self.assertFalse(lib.has_cost_impact_section(body))

    def test_four_backtick_fenced_example_does_not_count(self):
        body = "## Cost impact\n````\nExample only\n````\n"
        self.assertFalse(lib.has_cost_impact_section(body))

    def test_heading_inside_fenced_example_is_not_the_real_heading(self):
        body = "```\n## Cost impact\nExample only\n```\n"
        self.assertFalse(lib.has_cost_impact_section(body))

    def test_unclosed_fence_does_not_leak_trailing_real_section(self):
        body = "## Cost impact\n```\nunclosed fence\n"
        self.assertFalse(lib.has_cost_impact_section(body))


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

    def test_guidance_names_the_configured_exempt_label(self):
        guidance = lib.failure_guidance("missing_cost_impact", "my-exempt")
        self.assertIn("`my-exempt` label", guidance)
        self.assertNotIn("cost-exempt", guidance)

    def test_guidance_omits_waiver_sentence_when_exemption_disabled(self):
        guidance = lib.failure_guidance("missing_cost_impact", "")
        self.assertNotIn("label instead", guidance)
        self.assertNotIn("cost-exempt", guidance)

    def test_guidance_without_exemption_unaffected_for_other_categories(self):
        self.assertEqual(
            lib.failure_guidance("changed_files_unavailable"),
            lib.failure_guidance("changed_files_unavailable", "some-label"),
        )

    def test_descriptions_pairwise_distinct(self):
        descriptions = {
            lib.failure_outcome(c, enforce=True, soft_fail=True).description
            for c in ("missing_cost_impact", "changed_files_unavailable")
        }
        self.assertEqual(len(descriptions), 2)


class FailingStateHonestyTests(unittest.TestCase):
    """A warn-only check must not emit the same signal for "you broke the contract" and
    "I could not look". Measured on a consumer repo three days into the warn-only pilot
    (2026-11-25): 8 of 10 red `Telemetry cost contract` statuses across 339 open PRs were
    the could-not-look case, and all 8 were substantively clean — indistinguishable at a
    glance from a real failing test, and pushing every one of those PRs to
    `mergeable_state: unstable`."""

    def test_cannot_verify_reports_error_not_failure(self):
        for enforce, soft_fail in ((True, True), (True, False), (False, True)):
            with self.subTest(enforce=enforce, soft_fail=soft_fail):
                self.assertEqual(
                    lib.failure_outcome("changed_files_unavailable", enforce, soft_fail).state,
                    "error")

    def test_real_violation_still_reports_failure(self):
        for enforce, soft_fail in ((True, True), (True, False), (False, True)):
            with self.subTest(enforce=enforce, soft_fail=soft_fail):
                self.assertEqual(
                    lib.failure_outcome("missing_cost_impact", enforce, soft_fail).state,
                    "failure")

    def test_the_two_categories_never_share_a_state(self):
        for enforce, soft_fail in ((True, True), (True, False), (False, True)):
            with self.subTest(enforce=enforce, soft_fail=soft_fail):
                self.assertNotEqual(
                    lib.failure_outcome("missing_cost_impact", enforce, soft_fail).state,
                    lib.failure_outcome("changed_files_unavailable", enforce, soft_fail).state)

    def test_silent_variant_is_still_green_for_both(self):
        # enforce:false + soft-fail:false is the documented SILENT rung; the honesty fix
        # must not promote it to a red state it never had.
        for category in ("missing_cost_impact", "changed_files_unavailable"):
            with self.subTest(category=category):
                self.assertEqual(
                    lib.failure_outcome(category, enforce=False, soft_fail=False).state,
                    "success")

    def test_cannot_verify_still_fails_closed_under_enforce(self):
        # `error` is non-success, so a required context still blocks. Distinguishable is
        # not the same as permissive, and this is the line that proves it.
        self.assertEqual(
            lib.failure_outcome("changed_files_unavailable", enforce=True,
                                soft_fail=True).exit_code, 1)


class SplitUnifiedDiffTests(unittest.TestCase):
    """Recovery of a file whose per-file `patch` GitHub omitted, from the whole-PR diff."""

    TWO_FILE_DIFF = (
        "diff --git a/svc/metrics.go b/svc/metrics.go\n"
        "index f3c411d8fbab..b228a2350a75 100644\n"
        "--- a/svc/metrics.go\n"
        "+++ b/svc/metrics.go\n"
        "@@ -10,3 +10,4 @@ func start() {\n"
        " \tctx := context.Background()\n"
        "+\tmetricsClient.Incr(\"auth.grant_ended\", nil)\n"
        " \treturn nil\n"
        "diff --git a/svc/ingest.gen.go b/svc/ingest.gen.go\n"
        "index aaaaaaaaaaaa..bbbbbbbbbbbb 100644\n"
        "--- a/svc/ingest.gen.go\n"
        "+++ b/svc/ingest.gen.go\n"
        "@@ -4874,2 +4874,3 @@ func (e Body) Valid() bool {\n"
        " \tswitch e {\n"
        "+\tcase \"agent_free_use\":\n"
    )

    def test_splits_into_one_hunks_only_fragment_per_file(self):
        fragments = lib.split_unified_diff(self.TWO_FILE_DIFF)
        self.assertEqual(sorted(fragments), ["svc/ingest.gen.go", "svc/metrics.go"])
        for name, fragment in fragments.items():
            self.assertTrue(fragment.startswith("@@"), f"{name}: {fragment[:40]!r}")

    def test_fragment_shape_matches_the_api_patch_field(self):
        # The `diff --git`, `index`, `---` and `+++` header lines must all be gone: a
        # `+++ b/<path>` line survives `_ADDED_LINE_RE` as an added line whose content is
        # `++ b/<path>`, inventing content nobody wrote.
        for fragment in lib.split_unified_diff(self.TWO_FILE_DIFF).values():
            self.assertNotIn("diff --git", fragment)
            self.assertNotIn("+++ ", fragment)
            self.assertNotIn("--- ", fragment)
            self.assertNotIn("index ", fragment)

    def test_recovered_fragment_feeds_first_match_unchanged(self):
        # The whole point of the shape: detection code is untouched and consumes a
        # recovered fragment by exactly the same path as an API-supplied one.
        fragments = lib.split_unified_diff(self.TWO_FILE_DIFF)
        files = [{"filename": "svc/metrics.go", "additions": 1,
                  "patch": fragments["svc/metrics.go"]}]
        patterns = lib.parse_patterns(
            r"[A-Za-z_]*(?:[Dd]ata[Dd]og|[Mm]etrics?|[Ss]tats[Dd])[A-Za-z_]*"
            r"\.(Gauge|Count|Histogram|Distribution|Incr|Decr)\(")
        self.assertIsNotNone(lib.first_match(files, patterns))

    def test_no_phantom_added_lines_from_file_headers(self):
        for name, fragment in lib.split_unified_diff(self.TWO_FILE_DIFF).items():
            for line in lib.added_lines(fragment):
                self.assertFalse(line.startswith("+ b/"), f"{name}: phantom {line!r}")
                self.assertFalse(line.startswith("++ b/"), f"{name}: phantom {line!r}")

    def test_recovery_closes_the_unscannable_gap(self):
        # The end-to-end shape of the live defect: 1 of N files has additions but no API
        # patch, so the whole gate reported could-not-verify even though every other file
        # in the PR was readable. After splicing the recovered fragment there is nothing
        # unscannable left.
        files = [
            {"filename": "svc/metrics.go", "additions": 1, "status": "modified",
             "patch": "@@ -1,1 +1,2 @@\n+ok\n"},
            {"filename": "svc/ingest.gen.go", "additions": 1531, "status": "modified"},
        ]
        self.assertEqual(lib.unscannable_files(files), ["svc/ingest.gen.go"])
        fragments = lib.split_unified_diff(self.TWO_FILE_DIFF)
        files[1]["patch"] = fragments["svc/ingest.gen.go"]
        self.assertEqual(lib.unscannable_files(files), [])

    def test_binary_file_yields_no_fragment_so_it_stays_unscannable(self):
        # git renders no hunk for a binary, so recovery must NOT manufacture one — a real
        # binary has to keep failing closed.
        binary = ("diff --git a/logo.png b/logo.png\n"
                  "index aaaa..bbbb 100644\n"
                  "Binary files a/logo.png and b/logo.png differ\n")
        self.assertEqual(lib.split_unified_diff(binary), {})

    def test_rename_without_content_change_yields_no_fragment(self):
        rename = ("diff --git a/old.go b/new.go\n"
                  "similarity index 100%\n"
                  "rename from old.go\n"
                  "rename to new.go\n")
        self.assertEqual(lib.split_unified_diff(rename), {})

    def test_added_file_is_keyed_by_its_new_path(self):
        added = ("diff --git a/svc/new.go b/svc/new.go\n"
                 "new file mode 100644\n"
                 "index 000000000000..abcdef123456\n"
                 "--- /dev/null\n"
                 "+++ b/svc/new.go\n"
                 "@@ -0,0 +1,1 @@\n"
                 "+package svc\n")
        self.assertEqual(sorted(lib.split_unified_diff(added)), ["svc/new.go"])

    def test_deleted_file_yields_no_fragment(self):
        deleted = ("diff --git a/svc/gone.go b/svc/gone.go\n"
                   "deleted file mode 100644\n"
                   "index abcdef123456..000000000000\n"
                   "--- a/svc/gone.go\n"
                   "+++ /dev/null\n"
                   "@@ -1,1 +0,0 @@\n"
                   "-package svc\n")
        self.assertEqual(lib.split_unified_diff(deleted), {})

    def test_quoted_path_is_unquoted_to_match_the_api_filename(self):
        # git C-style-quotes a path with a space unless core.quotePath=false. The API
        # reports the UNQUOTED name, so a naive line[6:] would key the fragment
        # `a b.go"` and never match the entry it is meant to repair.
        quoted = ('diff --git "a/svc/a b.go" "b/svc/a b.go"\n'
                  "index aaaa..bbbb 100644\n"
                  '--- "a/svc/a b.go"\n'
                  '+++ "b/svc/a b.go"\n'
                  "@@ -1,1 +1,2 @@\n"
                  "+ok\n")
        self.assertEqual(sorted(lib.split_unified_diff(quoted)), ["svc/a b.go"])

    def test_content_line_resembling_a_diff_header_does_not_split(self):
        # A fixture or doc whose CONTENT contains `diff --git` must not be read as a new
        # file section, or the fragment after it is lost.
        tricky = ("diff --git a/docs/x.md b/docs/x.md\n"
                  "index aaaa..bbbb 100644\n"
                  "--- a/docs/x.md\n"
                  "+++ b/docs/x.md\n"
                  "@@ -1,1 +1,3 @@\n"
                  " intro\n"
                  "+    diff --git a/fake b/fake\n"
                  "+metricsClient.Incr(\"x\", nil)\n")
        fragments = lib.split_unified_diff(tricky)
        self.assertEqual(sorted(fragments), ["docs/x.md"])
        self.assertIn('metricsClient.Incr("x", nil)',
                      "\n".join(lib.added_lines(fragments["docs/x.md"])))

    def test_empty_and_garbage_input_are_safe(self):
        for text in ("", None, "not a diff at all\n", "@@ -1,1 +1,1 @@\n+orphan hunk\n"):
            with self.subTest(text=text):
                self.assertEqual(lib.split_unified_diff(text), {})


class PathKeyIntegrityTests(unittest.TestCase):
    """Review-panel findings on the path parsing. Each is a way for recovery to key a
    fragment under the wrong name and then report a clean scan of a file it never read."""

    def test_trailing_space_in_a_filename_is_preserved(self):
        # A trailing space is a legal filename character and git does NOT quote for it, so
        # `strip()` collapsed `foo.js` and `foo.js ` onto one key with last-wins assignment:
        # an oversized `foo.js` carrying a billable call plus a small benign `foo.js ` would
        # mark the real file recovered-and-clean from the WRONG file's hunks.
        diff = ("diff --git a/svc/foo.js b/svc/foo.js\n"
                "--- a/svc/foo.js\n"
                "+++ b/svc/foo.js\n"
                "@@ -1,1 +1,2 @@\n"
                "+posthog.capture('billable')\n"
                "diff --git a/svc/foo.js  b/svc/foo.js \n"
                "--- a/svc/foo.js \n"
                "+++ b/svc/foo.js \n"
                "@@ -1,1 +1,2 @@\n"
                "+harmless\n")
        fragments = lib.split_unified_diff(diff)
        self.assertEqual(sorted(fragments), ["svc/foo.js", "svc/foo.js "])
        self.assertIn("posthog.capture", fragments["svc/foo.js"])
        self.assertIn("harmless", fragments["svc/foo.js "])

    def test_single_trailing_tab_separator_is_removed(self):
        # git appends a TAB after an unquoted name in the ---/+++ header. That one TAB must
        # go (or the key never matches the API filename), and nothing else may.
        diff = ("diff --git a/svc/a b.go b/svc/a b.go\n"
                "--- a/svc/a b.go\t\n"
                "+++ b/svc/a b.go\t\n"
                "@@ -1,1 +1,2 @@\n"
                "+ok\n")
        self.assertEqual(sorted(lib.split_unified_diff(diff)), ["svc/a b.go"])

    def test_octal_escaped_non_ascii_path_decodes_as_utf8(self):
        # Flagged by 6 of 6 reviewers. Under the default core.quotePath git renders a
        # non-ASCII path as octal-escaped UTF-8 BYTES; decoding the escapes straight to text
        # yields `cafÃ©.go`, which never matches the API's `café.go`, so recovery silently
        # never worked for a non-ASCII filename.
        diff = ('diff --git "a/svc/caf\\303\\251.go" "b/svc/caf\\303\\251.go"\n'
                '--- "a/svc/caf\\303\\251.go"\n'
                '+++ "b/svc/caf\\303\\251.go"\n'
                "@@ -1,1 +1,2 @@\n"
                "+ok\n")
        self.assertEqual(sorted(lib.split_unified_diff(diff)), ["svc/café.go"])

    def test_quoted_path_keeps_standard_c_escapes(self):
        diff = ('diff --git "a/svc/a\\tb\\\\c.go" "b/svc/a\\tb\\\\c.go"\n'
                '--- "a/svc/a\\tb\\\\c.go"\n'
                '+++ "b/svc/a\\tb\\\\c.go"\n'
                "@@ -1,1 +1,2 @@\n"
                "+ok\n")
        self.assertEqual(sorted(lib.split_unified_diff(diff)), ["svc/a\tb\\c.go"])

    def test_duplicate_path_fails_closed_rather_than_last_wins(self):
        # Two sections cannot legitimately claim one new path. A duplicate means a quoting
        # edge read wrong or file content that forged a section header, and the honest answer
        # is that the file was not read — so it yields NO fragment and stays unscannable.
        diff = ("diff --git a/svc/x.go b/svc/x.go\n"
                "--- a/svc/x.go\n"
                "+++ b/svc/x.go\n"
                "@@ -1,1 +1,2 @@\n"
                "+posthog.capture('real')\n"
                "diff --git a/svc/x.go b/svc/x.go\n"
                "--- a/svc/x.go\n"
                "+++ b/svc/x.go\n"
                "@@ -1,1 +1,2 @@\n"
                "+benign\n")
        self.assertEqual(lib.split_unified_diff(diff), {})
        # ...so the entry it would have repaired is still reported unscannable.
        files = [{"filename": "svc/x.go", "additions": 1, "status": "modified"}]
        self.assertEqual(lib.unscannable_files(files), ["svc/x.go"])

    def test_a_non_duplicate_neighbour_survives_a_duplicate(self):
        diff = ("diff --git a/svc/dup.go b/svc/dup.go\n+++ b/svc/dup.go\n@@ -1 +1 @@\n+a\n"
                "diff --git a/svc/dup.go b/svc/dup.go\n+++ b/svc/dup.go\n@@ -1 +1 @@\n+b\n"
                "diff --git a/svc/ok.go b/svc/ok.go\n+++ b/svc/ok.go\n@@ -1 +1 @@\n+c\n")
        self.assertEqual(sorted(lib.split_unified_diff(diff)), ["svc/ok.go"])


if __name__ == "__main__":
    unittest.main()
