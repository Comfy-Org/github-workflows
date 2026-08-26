#!/usr/bin/env python3
"""Anchor-aware review posting: one out-of-diff finding must not cost every anchor.

THE FAILURE THIS PINS. `POST /pulls/{n}/reviews` takes `comments` as ONE unit: if a
single comment cites a line the diff does not carry, GitHub rejects the whole request
with HTTP 422 and post-review.py degrades to a body-only review. Observed in the field
on a 10-finding round — nine positions were inside the hunks, the tenth cited a real
line in a file the diff touched but outside every hunk in it, and ALL TEN lost their
anchors. Nothing was wrong with the nine, and nothing in the review said which one had
sunk them.

So the anchor set is computed from the reviewed diff before posting, and a finding that
cannot anchor is rendered in the body while the rest still anchor inline.

What is asserted, and why each case is here rather than assumed:

* the diff parser numbers the NEW side the way GitHub does — added and context lines
  advance it, removed lines do not, several hunks and several files stay independent,
  and a delete (`+++ /dev/null`) contributes no anchor at all;
* an unparseable hunk header DROPS its file rather than numbering the rest from a
  guess, because a wrong anchor set sends back the very 422 this avoids;
* the partition keeps the count honest: `Found N` stays the total across both halves,
  since a finding rendered in the body is still a finding;
* every unusable-diff path FAILS OPEN to all-inline (the pre-existing behaviour) —
  a diff we cannot read must never cost a finding an anchor that would have worked;
* the wholesale 422 fallback still exists for whatever slips past the filter, and
  still carries every finding.

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "post-review.py")
SPEC = importlib.util.spec_from_file_location("post_review", MODULE_PATH)
PR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PR)

# Two files, so a per-file anchor set has something to be wrong about. `app.py` has two
# hunks; `util.py` has one. Line numbers are the NEW side, exactly what a finding cites.
DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,3 +10,4 @@ def handler():
 context_at_10
+added_at_11
 context_at_12
 context_at_13
@@ -80,3 +81,3 @@ def other():
 context_at_81
-removed_does_not_advance
+added_at_82
 context_at_83
diff --git a/util.py b/util.py
index 3333333..4444444 100644
--- a/util.py
+++ b/util.py
@@ -5,1 +5,2 @@ def helper():
 context_at_5
+added_at_6
"""


def finding(path, line, severity="medium", body=None):
    return {
        "file": path,
        "line": line,
        "side": "RIGHT",
        "severity": severity,
        "body": body or f"finding on {path}:{line}",
    }


class AnchorParsingTest(unittest.TestCase):
    def test_numbers_the_new_side_like_github(self):
        anchors = PR.anchorable_lines(DIFF)
        self.assertEqual(anchors["app.py"], {10, 11, 12, 13, 81, 82, 83})
        self.assertEqual(anchors["util.py"], {5, 6})

    def test_removed_lines_do_not_advance_the_new_side(self):
        # 82 is the ADDED line after a removal; if `-` advanced the counter the added
        # line would be numbered 83 and every later anchor would be off by one.
        self.assertIn(82, PR.anchorable_lines(DIFF)["app.py"])
        self.assertNotIn(84, PR.anchorable_lines(DIFF)["app.py"])

    def test_deleted_file_contributes_no_anchors(self):
        diff = "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-was_here\n-and_here\n"
        self.assertEqual(PR.anchorable_lines(diff), {})

    def test_unparseable_hunk_header_drops_the_file(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ this is not a hunk header @@\n+added\n"
        self.assertEqual(PR.anchorable_lines(diff).get("x.py"), set())

    def test_no_newline_marker_is_not_content(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n context_1\n+added_2\n\\ No newline at end of file\n"
        self.assertEqual(PR.anchorable_lines(diff)["x.py"], {1, 2})


class PartitionTest(unittest.TestCase):
    def test_in_diff_anchors_and_out_of_diff_goes_to_the_body(self):
        enriched = PR.normalize_comments(
            [finding("app.py", 11), finding("app.py", 500), finding("util.py", 6)]
        )
        inline, body_only = PR.partition_by_anchor(enriched, PR.anchorable_lines(DIFF))
        self.assertEqual(
            sorted((c["comment"]["path"], c["comment"]["line"]) for c in inline),
            [("app.py", 11), ("util.py", 6)],
        )
        self.assertEqual([c["comment"]["line"] for c in body_only], [500])

    def test_a_file_absent_from_the_diff_cannot_anchor(self):
        enriched = PR.normalize_comments([finding("never_touched.py", 11)])
        inline, body_only = PR.partition_by_anchor(enriched, PR.anchorable_lines(DIFF))
        self.assertEqual(inline, [])
        self.assertEqual(len(body_only), 1)

    def test_no_diff_supplied_keeps_everything_inline(self):
        enriched = PR.normalize_comments([finding("app.py", 500)])
        inline, body_only = PR.partition_by_anchor(enriched, None)
        self.assertEqual(len(inline), 1)
        self.assertEqual(body_only, [])


class LoadAnchorsFailsOpenTest(unittest.TestCase):
    def test_missing_path_argument(self):
        self.assertIsNone(PR.load_anchors(None))

    def test_unreadable_file(self):
        self.assertIsNone(PR.load_anchors("/nonexistent/pr-diff.patch"))

    def test_empty_diff(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "empty.patch")
            open(p, "w", encoding="utf-8").close()
            self.assertIsNone(PR.load_anchors(p))

    def test_diff_with_no_hunks(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nohunks.patch")
            with open(p, "w", encoding="utf-8") as f:
                f.write("Binary files a/logo.png and b/logo.png differ\n")
            self.assertIsNone(PR.load_anchors(p))

    def test_a_real_diff_returns_a_map(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pr.patch")
            with open(p, "w", encoding="utf-8") as f:
                f.write(DIFF)
            self.assertEqual(PR.load_anchors(p)["util.py"], {5, 6})


class EndToEndPostTest(unittest.TestCase):
    """Drive main() with a stubbed `gh` and read the payload it would have sent."""

    def run_main(self, findings, with_diff=True, post_returncode=0, stderr="", summaries=None):
        """Return the POSTed payloads. Pass `summaries` (a list) to collect step-summary writes."""
        posted = []

        def fake_post(repo, pr_number, payload):
            posted.append(json.loads(payload))
            return subprocess.CompletedProcess(
                args=["gh"], returncode=post_returncode, stdout="", stderr=stderr
            )

        def fake_summary(markdown, note=None):
            if summaries is not None:
                summaries.append(markdown)

        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, "consolidated.json")
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump({"findings": findings, "panel": [{"model": "m", "review_type": "adversarial", "status": "ok"}]}, f)
            argv = [
                "post-review.py",
                "--findings", fpath,
                "--pr-number", "1",
                "--repo", "o/r",
                "--commit-sha", "deadbeef",
            ]
            if with_diff:
                dpath = os.path.join(d, "pr-diff.patch")
                with open(dpath, "w", encoding="utf-8") as f:
                    f.write(DIFF)
                argv += ["--diff", dpath]
            with mock.patch.object(PR, "gh_post_review", side_effect=fake_post), \
                 mock.patch.object(PR.sys, "argv", argv), \
                 mock.patch.object(PR, "write_step_summary", side_effect=fake_summary):
                try:
                    PR.main()
                except SystemExit:
                    pass
        return posted

    def test_the_field_regression_nine_anchor_one_lands_in_the_body(self):
        # The observed shape: ten findings, one citing a line outside every hunk.
        findings = [finding("app.py", ln) for ln in (10, 11, 12, 13, 81, 82, 83)]
        findings += [finding("util.py", 5), finding("util.py", 6)]
        findings += [finding("util.py", 166, severity="low", body="cites a line no hunk carries")]
        posted = self.run_main(findings)
        self.assertEqual(len(posted), 1, "one POST, no wholesale fallback")
        payload = posted[0]
        self.assertEqual(len(payload["comments"]), 9)
        self.assertNotIn(166, [c["line"] for c in payload["comments"]])
        # ...and the tenth is still reported, in the body, named by file:line.
        self.assertIn("`util.py:166`", payload["body"])
        self.assertIn("cites a line no hunk carries", payload["body"])

    def test_the_headline_count_covers_both_halves(self):
        findings = [finding("app.py", 11), finding("app.py", 999)]
        payload = self.run_main(findings)[0]
        self.assertIn("Found **2** finding(s)", payload["body"])
        self.assertEqual(len(payload["comments"]), 1)

    def test_without_diff_every_finding_is_sent_inline(self):
        # The pre-existing behaviour, unchanged: this is what makes the new path opt-in.
        findings = [finding("app.py", 11), finding("app.py", 999)]
        payload = self.run_main(findings, with_diff=False)[0]
        self.assertEqual(len(payload["comments"]), 2)
        self.assertNotIn("could not be anchored inline", payload["body"])

    def test_a_422_still_degrades_wholesale_and_keeps_every_finding(self):
        findings = [finding("app.py", 11), finding("app.py", 999)]
        posted = self.run_main(
            findings, post_returncode=1, stderr="gh: Unprocessable Entity (HTTP 422)"
        )
        self.assertEqual(len(posted), 2, "inline attempt, then the body-only fallback")
        fallback = posted[1]
        self.assertNotIn("comments", fallback)
        self.assertIn("`app.py:11`", fallback["body"])
        self.assertIn("`app.py:999`", fallback["body"])

    def test_all_findings_unanchorable_posts_a_body_only_review_not_a_422(self):
        findings = [finding("elsewhere.py", 7), finding("elsewhere.py", 8)]
        payload = self.run_main(findings)[0]
        self.assertEqual(payload["comments"], [])
        self.assertIn("Found **2** finding(s)", payload["body"])
        self.assertIn("`elsewhere.py:7`", payload["body"])
        self.assertNotIn(
            "All findings had invalid file/line references",
            payload["body"],
            "they were valid — they just could not anchor",
        )



class HeaderSpoofingTest(unittest.TestCase):
    """A content line that reads like a header must not be parsed as one."""

    def test_added_line_that_renders_as_a_new_file_header(self):
        # The added line's TEXT is `++ b/evil.py`; the diff renders it `+++ b/evil.py`.
        # Without the hunk's line budget it is taken for a new-file header, and every
        # later line in app.py is numbered under `evil.py` — the wrong-position 422.
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,3 @@\n"
            " context_1\n"
            "++ b/evil.py\n"
            "+after\n"
        )
        anchors = PR.anchorable_lines(diff)
        self.assertEqual(anchors, {"app.py": {1, 2, 3}})
        self.assertNotIn("evil.py", anchors)

    def test_added_line_that_renders_as_a_dev_null_header(self):
        # `++ /dev/null` would otherwise blank `path` and silently drop every
        # remaining anchor in the file.
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,3 @@\n"
            " context_1\n"
            "++ /dev/null\n"
            "+after\n"
        )
        self.assertEqual(PR.anchorable_lines(diff)["app.py"], {1, 2, 3})

    def test_a_second_file_after_a_spoofing_line_still_parses(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,2 @@\n"
            " context_1\n"
            "++ b/evil.py\n"
            "diff --git a/util.py b/util.py\n"
            "--- a/util.py\n"
            "+++ b/util.py\n"
            "@@ -9,0 +9,1 @@\n"
            "+added_at_9\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"app.py": {1, 2}, "util.py": {9}})


class LineSplittingTest(unittest.TestCase):
    """Only LF advances git's numbering; splitlines() breaks on much more."""

    def test_a_form_feed_inside_content_does_not_shift_later_anchors(self):
        # PEP 8 sanctions \f as a Python section separator. splitlines() would cut
        # ` a\fb` into two lines, count the fragment `b` as content, and push every
        # later anchor in the file one line down.
        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,2 +1,3 @@\n"
            " before\x0cafter\n"
            "+added_at_2\n"
            " context_at_3\n"
        )
        self.assertEqual(PR.anchorable_lines(diff)["x.py"], {1, 2, 3})

    def test_a_line_separator_inside_content_does_not_drop_the_file(self):
        diff = (
            "diff --git a/x.js b/x.js\n"
            "--- a/x.js\n"
            "+++ b/x.js\n"
            "@@ -1,1 +1,2 @@\n"
            "+const s = '\u2028';\n"
            " tail\n"
        )
        self.assertEqual(PR.anchorable_lines(diff)["x.js"], {1, 2})


class HeaderPathDecodingTest(unittest.TestCase):
    def test_c_quoted_path_is_unquoted(self):
        # core.quotePath defaults ON, so a non-ASCII name arrives quoted and escaped.
        diff = (
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1,0 +1,1 @@\n"
            "+added\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"caf\u00e9.py": {1}})

    def test_c_quoted_path_with_an_embedded_quote(self):
        diff = (
            '--- "a/we\\"ird.py"\n'
            '+++ "b/we\\"ird.py"\n'
            "@@ -1,0 +1,1 @@\n"
            "+added\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {'we"ird.py': {1}})

    def test_a_trailing_space_in_a_path_is_preserved(self):
        # .strip() would key this file as "sp.py" and no finding in it could anchor.
        diff = "--- a/sp.py \n+++ b/sp.py \n@@ -1,0 +1,1 @@\n+added\n"
        self.assertEqual(PR.anchorable_lines(diff), {"sp.py ": {1}})

    def test_a_crlf_diff_keeps_its_paths_clean(self):
        diff = "--- a/x.py\r\n+++ b/x.py\r\n@@ -1,0 +1,1 @@\r\n+added\r\n"
        self.assertEqual(PR.anchorable_lines(diff), {"x.py": {1}})


class UnusableVersusAnchorlessTest(unittest.TestCase):
    """"No anchors" is a real answer; "not a diff" is the only fail-open case."""

    def test_text_with_no_file_header_is_unusable(self):
        self.assertIsNone(PR.anchorable_lines("Binary files a/l.png and b/l.png differ\n"))
        self.assertIsNone(PR.anchorable_lines("not a diff at all\n"))

    def test_a_mode_only_diff_parses_to_an_empty_map(self):
        # Real `git diff` output for `chmod +x`: a `diff --git` marker and nothing to
        # anchor to. Failing open here would send every finding inline into a 422.
        diff = (
            "diff --git a/run.sh b/run.sh\n"
            "old mode 100644\n"
            "new mode 100755\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {})

    def test_a_delete_only_diff_parses_to_an_empty_map(self):
        # Not None: there is genuinely nowhere to anchor, so every finding belongs in
        # the body. Failing open here is precisely the 422 this change exists to avoid.
        diff = "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-was_here\n-and_here\n"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "del.patch")
            with open(p, "w", encoding="utf-8") as f:
                f.write(diff)
            self.assertEqual(PR.load_anchors(p), {})

    def test_a_dropped_file_does_not_fail_open(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ nonsense @@\n+added\n"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "drop.patch")
            with open(p, "w", encoding="utf-8") as f:
                f.write(diff)
            anchors = PR.load_anchors(p)
        self.assertEqual(anchors, {"x.py": set()})
        enriched = PR.normalize_comments([finding("x.py", 1)])
        inline, body_only = PR.partition_by_anchor(enriched, anchors)
        self.assertEqual(inline, [])
        self.assertEqual(len(body_only), 1)


class RenderCodeRefTest(unittest.TestCase):
    def test_at_signs_in_a_path_cannot_fire_a_mention(self):
        ref = PR.render_code_ref("src/@acme/team.py", 3)
        self.assertNotIn("@a", ref)
        self.assertIn("@\u200b", ref)

    def test_backticks_in_a_path_cannot_close_the_span(self):
        ref = PR.render_code_ref("a`b.py", 7)
        self.assertTrue(ref.startswith("``") and ref.endswith("``"))
        self.assertIn("a`b.py:7", ref)

    def test_newlines_in_a_path_cannot_forge_sections(self):
        ref = PR.render_code_ref("a.py\n## Forged", 1)
        self.assertNotIn("\n", ref)

    def test_an_ordinary_path_renders_as_a_plain_code_span(self):
        self.assertEqual(PR.render_code_ref("app.py", 11), "`app.py:11`")

    def test_a_hostile_path_is_neutralized_in_the_posted_body(self):
        items = PR.normalize_comments([finding("a`b@acme.py", 4)])
        md = PR.render_body_only_findings(items)
        self.assertNotIn("@a", md)
        self.assertIn("``a`b@\u200bacme.py:4``", md)


class BodyBudgetTest(unittest.TestCase):
    def test_a_short_body_is_untouched(self):
        self.assertEqual(PR.clamp_review_body("hello"), "hello")

    def test_an_oversize_body_is_cut_under_the_limit(self):
        clamped = PR.clamp_review_body("x" * 2000, limit=400)
        self.assertLessEqual(len(clamped), 400)
        self.assertIn("truncated here", clamped)

    def test_the_cut_holds_even_when_the_limit_is_shorter_than_the_note(self):
        self.assertEqual(len(PR.clamp_review_body("x" * 2000, limit=10)), 10)

    def test_many_long_demoted_findings_stay_postable(self):
        # Each body is under review-output-mcp.py's 20,000-char cap, but ten of them
        # in the review BODY blow past GitHub's 65,536-char limit — a 422 on size,
        # which the wholesale fallback (strictly larger) cannot rescue.
        findings = [
            finding("elsewhere.py", 100 + i, body="z" * 19000) for i in range(10)
        ]
        posted = EndToEndPostTest().run_main(findings)
        self.assertEqual(len(posted), 1)
        self.assertLessEqual(len(posted[0]["body"]), PR.MAX_REVIEW_BODY_CHARS)

    def test_a_clamped_post_actually_writes_the_summary_it_promises(self):
        # The clamp note says "the full text is in the job summary of this run". On the
        # SUCCESS path nothing else writes one, so without this the cut findings are
        # absent from both the PR and the summary while the header still counts them.
        findings = [
            finding("elsewhere.py", 100 + i, body="z" * 19000) for i in range(10)
        ]
        summaries = []
        posted = EndToEndPostTest().run_main(findings, summaries=summaries)
        self.assertIn("truncated here", posted[0]["body"])
        self.assertEqual(len(summaries), 1, "the promised job summary was written")
        self.assertGreater(len(summaries[0]), PR.MAX_REVIEW_BODY_CHARS, "and it is whole")
        for i in range(10):
            self.assertIn(f"elsewhere.py:{100 + i}", summaries[0])

    def test_a_short_review_writes_no_summary(self):
        # The summary is a degradation channel, not a mirror: an intact review must
        # not double-post itself into the run summary on every green round.
        summaries = []
        EndToEndPostTest().run_main([finding("app.py", 11)], summaries=summaries)
        self.assertEqual(summaries, [])

    def test_the_clamp_cuts_the_least_urgent_finding_in_the_fallback(self):
        # The fallback body used to be review_body (ending with the DEMOTED half) plus
        # the inline half appended after it, so clamping from the end dropped the
        # findings that had just lost their anchors while a demoted nit survived.
        findings = [finding("app.py", 11, severity="critical", body="C" * 19000)]
        findings += [finding("elsewhere.py", 7, severity="low", body="L" * 19000)]
        findings += [finding("app.py", 12, severity="high", body="H" * 19000)]
        findings += [finding("elsewhere.py", 8, severity="medium", body="M" * 19000)]
        posted = EndToEndPostTest().run_main(
            findings, post_returncode=1, stderr="gh: Unprocessable Entity (HTTP 422)"
        )
        body = posted[1]["body"]
        self.assertIn("truncated here", body)
        # Severity order now holds across BOTH halves, so the cut lands on the low.
        # Before, the demoted half came first and the inline half was appended after
        # it, so the high (inline) was dropped whole while the low (demoted) survived.
        self.assertIn("C" * 19000, body, "the critical survives")
        self.assertIn("H" * 19000, body, "so does the high")
        self.assertIn("M" * 19000, body, "and the medium")
        self.assertNotIn("L" * 19000, body, "the low is what gets cut")

    def test_no_finding_is_rendered_twice_in_the_fallback(self):
        posted = EndToEndPostTest().run_main(
            [
                finding("app.py", 11, body="anchored one"),
                finding("elsewhere.py", 7, body="demoted one"),
            ],
            post_returncode=1,
            stderr="gh: Unprocessable Entity (HTTP 422)",
        )
        body = posted[1]["body"]
        self.assertEqual(body.count("anchored one"), 1)
        self.assertEqual(body.count("demoted one"), 1)


class FallbackSuffixTest(unittest.TestCase):
    def test_no_inline_comments_means_no_second_identical_post(self):
        # Every finding already demoted, then the POST fails for an unrelated reason.
        # With no inline half to drop, the "fallback" body is byte-identical to the
        # request that just failed: it cannot fix a size or malformed-body rejection,
        # and if GitHub committed the write before erroring it publishes a DUPLICATE
        # review. Degrade to the summary and let the step go red instead.
        summaries = []
        posted = EndToEndPostTest().run_main(
            [finding("elsewhere.py", 7)],
            post_returncode=1,
            stderr="gh: Server Error (HTTP 500)",
            summaries=summaries,
        )
        self.assertEqual(len(posted), 1, "no second POST of the same body")
        self.assertEqual(len(summaries), 1, "but the review is still delivered")
        self.assertIn("elsewhere.py:7", summaries[0])
        self.assertNotIn("Inline comments could not be anchored", summaries[0])

    def test_lost_inline_comments_still_carry_the_note(self):
        posted = EndToEndPostTest().run_main(
            [finding("app.py", 11)],
            post_returncode=1,
            stderr="gh: Unprocessable Entity (HTTP 422)",
        )
        self.assertIn("Inline comments could not be anchored", posted[1]["body"])


class DiffReadingTest(unittest.TestCase):
    """load_anchors must not let Python's line-ending translation edit the diff."""

    def load(self, raw: bytes):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pr-diff.patch")
            with open(path, "wb") as f:
                f.write(raw)
            return PR.load_anchors(path)

    def test_a_lone_cr_inside_a_content_line_does_not_split_it(self):
        # Universal-newline mode (the default) rewrites a bare \r to \n before the
        # parser can see it, splitting one added line into two: the tail has no
        # +/-/space prefix, so it trips the desync arm and the whole file is dropped.
        # A mixed-ending file or a minified asset produces exactly this.
        raw = (
            b"--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,3 @@\n"
            b" context_1\n+added_2\rstill_line_2\n+added_3\n"
        )
        self.assertEqual(self.load(raw)["x.py"], {1, 2, 3})

    def test_a_crlf_diff_still_numbers_and_keys_correctly(self):
        # With newline="" the \r survives to the parser — which is what makes
        # header_new_path's rstrip("\r") load-bearing in production, not just in a
        # direct-call unit test.
        raw = (
            b"--- a/x.py\r\n+++ b/x.py\r\n@@ -1,1 +1,2 @@\r\n"
            b" context_1\r\n+added_2\r\n"
        )
        self.assertEqual(self.load(raw), {"x.py": {1, 2}})


class QuotePathModesTest(unittest.TestCase):
    """A quoted header path must decode under BOTH of git's quoting modes."""

    def test_octal_escapes_decode(self):
        # core.quotePath ON (the default): every non-ASCII byte is octal-escaped.
        self.assertEqual(PR.header_new_path('+++ "b/caf\\303\\251.py"'), "café.py")

    def test_verbatim_utf8_beside_an_escaped_quote_survives(self):
        # core.quotePath OFF: git still quotes a name containing a `"`, but leaves the
        # UTF-8 bytes alone. The old latin-1 round-trip turned that é into U+FFFD, so
        # the key stopped matching the path findings cite and the file went silent.
        self.assertEqual(PR.header_new_path('+++ "b/café\\"x.py"'), 'café"x.py')

    def test_an_astral_character_beside_an_escape_survives(self):
        # And this one used to raise UnicodeEncodeError and drop the file entirely.
        self.assertEqual(PR.header_new_path('+++ "b/\U0001F389\\"x.py"'), '\U0001F389"x.py')

    def test_escaped_backslash_and_control_characters(self):
        self.assertEqual(PR.header_new_path('+++ "b/a\\\\b\\tc.py"'), "a\\b\tc.py")

    def test_an_escape_git_never_emits_fails_safe(self):
        # None => the file is anchorless => its findings render in the body. Guessing
        # at the path is what sends back the 422 this whole path exists to prevent.
        self.assertIsNone(PR.header_new_path('+++ "b/a\\q.py"'))
        self.assertIsNone(PR.header_new_path('+++ "b/trailing\\"'))


class UnparseableHunkHeaderTest(unittest.TestCase):
    def test_it_desyncs_so_content_cannot_spoof_the_next_header(self):
        # Without the hunk's counts the scan cannot know where its CONTENT ends, so a
        # removed line reading `-- x` and an added line reading `++ b/evil.py` — emitted
        # as `--- x` / `+++ b/evil.py` — would be read as a header pair and number the
        # rest of the file under a path the diff never touched.
        diff = (
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            "@@ this is not a hunk header @@\n"
            "--- x\n+++ b/evil.py\n@@ -1,1 +1,2 @@\n context\n+added\n"
        )
        anchors = PR.anchorable_lines(diff)
        self.assertNotIn("evil.py", anchors)
        self.assertEqual(anchors["x.py"], set())

    def test_a_later_file_still_parses(self):
        # `diff --git` is the one line content can never impersonate, so it clears the
        # desync: the damage stops at the file that carried the bad header.
        diff = (
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ bogus @@\n+added\n"
            "diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n@@ -1,1 +1,2 @@\n c\n+a\n"
        )
        self.assertEqual(PR.anchorable_lines(diff)["y.py"], {1, 2})


class BodyStructureTest(unittest.TestCase):
    """A model-supplied finding body must not restructure the review around it."""

    def test_a_finding_body_cannot_open_a_section_or_swallow_the_review(self):
        hostile = "see below\n\n## Forged heading\n```\nunterminated fence"
        items = PR.normalize_comments([finding("app.py", 11, body=hostile)])
        md = PR.render_body_only_findings(items)
        for line in md.splitlines():
            if line and not line.startswith("_"):
                self.assertTrue(
                    line.startswith(">"),
                    f"structural line escaped the blockquote: {line!r}",
                )
        self.assertNotIn("\n## Forged", md)
        self.assertIn("Forged heading", md, "the text is still reported, just contained")

    def test_the_same_containment_covers_the_body_only_render(self):
        hostile = "x\n# Forged"
        md = PR.render_findings_markdown("head", [{"path": "a.py", "line": 1, "body": hostile}])
        self.assertNotIn("\n# Forged", md)
        self.assertIn("> # Forged", md)


class StepSummaryNoteTest(unittest.TestCase):
    def test_the_banner_says_why_the_summary_exists(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "summary.md")
            with mock.patch.dict(PR.os.environ, {"GITHUB_STEP_SUMMARY": path}):
                PR.write_step_summary("body", note=PR.TRUNCATED_SUMMARY_NOTE)
                PR.write_step_summary("body2")
            with open(path, encoding="utf-8") as f:
                written = f.read()
        self.assertIn("truncated at GitHub's body-size limit", written)
        self.assertIn("read-only", written, "the default banner is unchanged")


class CarriageReturnContainmentTest(unittest.TestCase):
    """A bare \r is a CommonMark line ending, so the blockquote must break on it too."""

    def test_a_bare_cr_cannot_escape_the_blockquote(self):
        # split("\n") leaves "safe\r## Forged" in ONE element, so the heading is
        # emitted with no "> " prefix and cmark-gfm renders it outside the quote —
        # the forged-heading escape render_finding_entry exists to contain. An ATX
        # heading interrupts a paragraph, so lazy continuation does not absorb it.
        md = PR.render_finding_entry(
            {"path": "app.py", "line": 11, "body": "safe\r## Forged heading"}
        )
        for line in md.splitlines():
            self.assertTrue(
                line.startswith(">"),
                f"structural line escaped the blockquote: {line!r}",
            )
        self.assertIn("Forged heading", md, "the text is still reported, just contained")

    def test_a_crlf_body_does_not_leave_a_stray_cr_in_the_quote(self):
        md = PR.render_finding_entry({"path": "a.py", "line": 1, "body": "one\r\ntwo"})
        self.assertNotIn("\r", md)
        self.assertEqual(md.splitlines()[-1], "> two")

    def test_an_unterminated_fence_after_a_cr_is_still_confined(self):
        md = PR.render_finding_entry(
            {"path": "a.py", "line": 1, "body": "x\r```\nswallows the rest"}
        )
        for line in md.splitlines():
            self.assertTrue(line.startswith(">"), f"escaped: {line!r}")


class BudgetUnderflowTest(unittest.TestCase):
    """A hunk whose declared +count is too SMALL must not re-open the header test."""

    def test_overflow_content_desyncs_so_a_later_pair_cannot_spoof(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+added_at_1\n"
            "+overflow_past_the_declared_count\n"
            "--- x\n"
            "+++ b/evil.py\n"
            "@@ -1,0 +1,3 @@\n"
            "+e1\n+e2\n+e3\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"app.py": {1}})

    def test_the_pair_landing_exactly_on_the_boundary_is_refused_too(self):
        # The seam the desync alone leaves open: the miscount is exactly two lines, so
        # the overflow lines ARE the header pair and no other content line fires the
        # desync. git always emits `diff --git ` before a file's header pair, so a pair
        # reached from inside a hunk region of git's own output is content.
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+added_at_1\n"
            "--- x\n"
            "+++ b/other.py\n"
            "@@ -1,0 +1,3 @@\n"
            "+e1\n+e2\n+e3\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"app.py": {1}})

    def test_a_prefix_less_multi_file_diff_still_parses_every_file(self):
        # No `diff --git` anywhere, so the gate above must NOT fire: a concatenated
        # `diff -u` legitimately starts its next file straight after hunk content, and
        # demoting those findings would be the fail-CLOSED direction this avoids.
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+x1\n"
            "--- a/y.py\n"
            "+++ b/y.py\n"
            "@@ -1,0 +1,2 @@\n"
            "+y1\n+y2\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"x.py": {1}, "y.py": {1, 2}})

    def test_overflow_desyncs_a_prefix_less_diff_the_git_header_gate_cannot_cover(self):
        # With no `diff --git` anywhere the saw_git_header gate is deliberately off, so
        # the overflow desync is the ONLY thing standing between a miscounted hunk and
        # a `-- z` / `++ b/evil.py` pair numbering lines under a file the diff never
        # touched. Mirrors the too-LARGE-count arm, which drops its file the same way.
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+x1\n"
            "+overflow_past_the_declared_count\n"
            "--- z\n"
            "+++ b/evil.py\n"
            "@@ -1,0 +1,2 @@\n"
            "+e1\n+e2\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"x.py": {1}})

    def test_a_no_newline_marker_on_a_spent_budget_is_not_a_desync(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+only\n"
            "\\ No newline at end of file\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+also\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"a.py": {1}, "b.py": {1}})


class StepSummaryBudgetTest(unittest.TestCase):
    """Actions discards an oversize step-summary upload WHOLE, so budget the write."""

    def write(self, markdown, note=PR.TRUNCATED_SUMMARY_NOTE):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "summary.md")
            with mock.patch.dict(PR.os.environ, {"GITHUB_STEP_SUMMARY": path}):
                PR.write_step_summary(markdown, note=note)
            with open(path, "rb") as f:
                return f.read()

    def test_a_normal_summary_is_written_whole(self):
        written = self.write("the review body")
        self.assertIn(b"the review body", written)
        self.assertNotIn(b"truncated here", written)

    def test_an_oversize_summary_is_cut_instead_of_discarded(self):
        written = self.write("x" * (PR.MAX_STEP_SUMMARY_BYTES * 2))
        self.assertLess(len(written), 1024 * 1024, "must stay under the 1 MiB cap")
        self.assertIn("truncated here", written.decode("utf-8"))

    def test_the_cap_is_counted_in_bytes_not_characters(self):
        # 600k non-ASCII characters are 1.2 MB — under any char-based budget, over the
        # byte cap that actually applies.
        written = self.write("\u00e9" * 600_000)
        self.assertLess(len(written), 1024 * 1024)

    def test_the_cut_never_splits_a_character(self):
        written = self.write("\u00e9" * 600_000)
        written.decode("utf-8")  # raises if a multi-byte sequence was severed


class ErrorReviewBudgetTest(unittest.TestCase):
    """--error-message is unbounded CLI/model text on the judge-failure path."""

    def post(self, message, returncode=0):
        posted, summaries = [], []

        def fake_post(repo, pr_number, payload):
            posted.append(json.loads(payload))
            return subprocess.CompletedProcess(
                args=["gh"], returncode=returncode, stdout="", stderr="gh: Server Error"
            )

        with mock.patch.object(PR, "gh_post_review", side_effect=fake_post), \
             mock.patch.object(PR, "write_step_summary", side_effect=lambda m, note=None: summaries.append(m)):
            try:
                PR.post_error_review("o/r", "1", "deadbeef", "## head", message)
            except SystemExit:
                pass
        return posted, summaries

    def test_an_unbounded_error_message_stays_postable(self):
        posted, _ = self.post("boom " * 50_000)
        body = posted[0]["body"]
        self.assertLessEqual(len(body), PR.MAX_REVIEW_BODY_CHARS)
        self.assertIn("Re-trigger by removing", body, "the instruction survives the cut")

    def test_a_short_error_message_is_untouched(self):
        posted, _ = self.post("judge exited 3")
        self.assertIn("```\njudge exited 3\n```", posted[0]["body"])

    def test_a_fenced_error_message_cannot_close_the_fence_early(self):
        posted, _ = self.post("stack:\n```\nnot the end")
        body = posted[0]["body"]
        self.assertIn("````\nstack:", body, "the fence outgrows the longest run inside")
        self.assertIn("````\n\nRe-trigger", body)

    def test_a_failed_error_post_still_delivers_the_text(self):
        _, summaries = self.post("judge exited 3", returncode=1)
        self.assertEqual(len(summaries), 1)
        self.assertIn("judge exited 3", summaries[0])


class FallbackFailureDeliveryTest(unittest.TestCase):
    def test_both_posts_failing_still_writes_the_review_to_the_summary(self):
        # The no-inline branch already writes the summary before exiting; this branch
        # carries MORE content (it has an inline half) and used to raise SystemExit
        # with the review gone from the PR and the summary both.
        summaries = []
        posted = EndToEndPostTest().run_main(
            [finding("app.py", 11), finding("util.py", 900)],
            post_returncode=1,
            stderr="gh: Unprocessable Entity (HTTP 422)",
            summaries=summaries,
        )
        self.assertEqual(len(posted), 2, "primary + wholesale fallback both attempted")
        self.assertEqual(len(summaries), 1, "and the text is still delivered")
        self.assertIn("app.py:11", summaries[0])
        self.assertIn("util.py:900", summaries[0])
        self.assertIn("Inline comments could not be anchored", summaries[0])


class HeaderPairPositionTest(unittest.TestCase):
    """git emits exactly ONE `--- `/`+++ ` pair per `diff --git `; a second is content."""

    def test_a_pair_between_the_honoured_header_and_the_first_hunk_is_refused(self):
        # The seam a hunk-region test leaves open: an honoured `+++` clears the region
        # flag and only the next `@@` sets it again, so a pair landing BETWEEN them was
        # honoured even in git-output mode — `evil.py` keys merged into the map.
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "--- x\n"
            "+++ b/evil.py\n"
            "@@ -1,0 +1,3 @@\n"
            "+e1\n+e2\n+e3\n"
        )
        self.assertNotIn("evil.py", PR.anchorable_lines(diff))

    def test_a_prefixless_concatenated_diff_still_parses_file_after_file(self):
        # The gate is `saw_git_header`-only for exactly this shape: no `diff --git`
        # anywhere, so consecutive header pairs are legitimate.
        diff = (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+a1\n"
            "--- a/util.py\n"
            "+++ b/util.py\n"
            "@@ -9,0 +9,1 @@\n"
            "+u9\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"app.py": {1}, "util.py": {9}})

    def test_normal_multi_file_git_output_is_unaffected(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+a1\n"
            "diff --git a/util.py b/util.py\n"
            "--- a/util.py\n"
            "+++ b/util.py\n"
            "@@ -9,0 +9,2 @@\n"
            "+u9\n+u10\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"app.py": {1}, "util.py": {9, 10}})


class BudgetOverrunTest(unittest.TestCase):
    """Each prefix is consumed against ITS OWN side's counter, not `either side left`."""

    def test_a_too_large_new_count_does_not_fabricate_an_anchor(self):
        # `+++ b/y.py` starts with `+`, so on a too-large `+count` it was counted as an
        # added line: it FABRICATED `x.py:2` and swallowed y.py's header entirely.
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,0 +1,3 @@\n"
            "+x1\n"
            "--- a/y.py\n"
            "+++ b/y.py\n"
            "@@ -1,0 +1,2 @@\n"
            "+y1\n"
            "+y2\n"
        )
        anchors = PR.anchorable_lines(diff)
        self.assertEqual(anchors.get("x.py"), {1}, "no fabricated x.py:2")

    def test_a_plus_line_on_a_spent_new_side_records_no_anchor(self):
        # Entered on `pending_old > 0 or pending_new > 0`, so with `-1,2 +1,1` a second
        # `+` still recorded an anchor while `pending_new` went negative.
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,1 @@\n"
            "+kept\n"
            "+extra\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"app.py": {1}})

    def test_a_well_formed_hunk_is_unaffected(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,3 @@\n"
            " ctx\n"
            "-gone\n"
            "+new_a\n"
            "+new_b\n"
        )
        self.assertEqual(PR.anchorable_lines(diff), {"app.py": {1, 2, 3}})


class BacktickFenceBudgetTest(unittest.TestCase):
    """The fence is emitted TWICE, so an unbounded run blows the body on delimiters."""

    def test_a_degenerate_backtick_run_keeps_the_body_terminated(self):
        posted, _ = ErrorReviewBudgetTest().post("`" * (PR.MAX_ERROR_MESSAGE_CHARS + 10))
        body = posted[0]["body"]
        self.assertLessEqual(len(body), PR.MAX_REVIEW_BODY_CHARS)
        fence = "`" * PR.MAX_FENCE_CHARS
        self.assertIn(fence, body, "a bounded fence is still opened")
        self.assertTrue(
            body.rstrip().endswith("`cursor-review` label."),
            "the closing fence and the re-trigger line survive the clamp",
        )
        self.assertEqual(body.count(fence + "\n"), 2, "opened and closed, nothing more")

    def test_a_run_under_the_cap_still_out_fences_normally(self):
        posted, _ = ErrorReviewBudgetTest().post("stack:\n" + "`" * 10 + "\nnot the end")
        body = posted[0]["body"]
        self.assertIn("`" * 11 + "\nstack:", body)


class SurrogateSafeSummaryTest(unittest.TestCase):
    """json accepts a lone surrogate; encoding one must not kill the delivery channel."""

    def test_a_lone_surrogate_does_not_break_the_step_summary(self):
        markdown = json.loads(r'"a \ud800 b"')
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "summary.md")
            with mock.patch.dict(PR.os.environ, {"GITHUB_STEP_SUMMARY": path}):
                PR.write_step_summary(markdown)
            with open(path, "rb") as f:
                written = f.read()
        written.decode("utf-8")  # raises if an unencodable surrogate got through
        self.assertIn(b"a ", written)
        self.assertIn(b" b", written)

    def test_clamp_to_bytes_is_surrogate_safe(self):
        text = json.loads(r'"\ud800"') * 10
        PR.clamp_to_bytes(text, 5).encode("utf-8")


class ErrorReviewSummaryContractTest(unittest.TestCase):
    """The error path owes the same truncation contract as the review paths."""

    def test_a_clamped_but_successful_post_writes_the_whole_text_to_the_summary(self):
        # The message budget alone rarely trips the body clamp, so drive it from the
        # other side: a header big enough that header + bounded message overflows.
        posted, summaries = [], []

        def fake_post(repo, pr_number, payload):
            posted.append(json.loads(payload))
            return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="", stderr="")

        with mock.patch.object(PR, "gh_post_review", side_effect=fake_post), \
             mock.patch.object(PR, "write_step_summary",
                               side_effect=lambda m, note=None: summaries.append(m)):
            PR.post_error_review("o/r", "1", "deadbeef", "h" * 30_000, "boom " * 20_000)

        self.assertLessEqual(len(posted[0]["body"]), PR.MAX_REVIEW_BODY_CHARS)
        self.assertEqual(len(summaries), 1, "clamp note points at a summary that exists")
        self.assertGreater(
            len(summaries[0]), len(posted[0]["body"]), "the summary copy is the whole text"
        )
        self.assertIn("Re-trigger by removing", summaries[0])

    def test_an_unclamped_successful_post_writes_no_summary(self):
        _, summaries = ErrorReviewBudgetTest().post("judge exited 3")
        self.assertEqual(summaries, [])


class NoFindingsDeliveryTest(unittest.TestCase):
    """The fourth exit path: a failed no-findings POST still owes the text somewhere."""

    def test_a_failed_no_findings_post_writes_the_panel_summary_to_the_job_summary(self):
        summaries = []
        posted = EndToEndPostTest().run_main(
            [], post_returncode=1, stderr="gh: Server Error (HTTP 500)", summaries=summaries
        )
        self.assertEqual(len(posted), 1)
        self.assertEqual(len(summaries), 1, "the one artifact saying no review happened")
        self.assertIn("No high-signal findings", summaries[0])


if __name__ == "__main__":
    unittest.main()
