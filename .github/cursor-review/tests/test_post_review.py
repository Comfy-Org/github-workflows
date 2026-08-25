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


if __name__ == "__main__":
    unittest.main()
