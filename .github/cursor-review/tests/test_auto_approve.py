#!/usr/bin/env python3
"""auto-approve.py: the decision table and the stale-review filter.

Every rule in the module docstring has a case here, because each one is a way an
approval could land on a PR nobody should have approved:

* threshold validation rejects `high`/`critical`/typos (exit 2, not a silent off);
* any finding above the threshold — or with an unrecognised severity — requests
  changes, never approves;
* an un-adjudicated (judge-degraded) round neither approves nor vetoes;
* an incomplete panel, an undelivered review, a moved head or base, or an open
  critical/high (or unbadged) thread from an earlier round withholds approval —
  `approve_max_failed_reviewers` tolerates only up to N `error` cells, and never
  a whole review type;
* an empty reviewed diff (every path excluded) withholds approval and
  withdraws an earlier one;
* dismissal touches only the approver's own marked reviews that are stale
  against the PR's LIVE head and recorded base — or, on a base retarget, every
  one of them — goes red over a stale marked approval it cannot act on, and the
  dismiss job runs on every event of an open PR whether or not
  `approve_max_severity` is still set.

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import argparse
import importlib.util
import json
import os
import re
import tempfile
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "auto-approve.py")
SPEC = importlib.util.spec_from_file_location("auto_approve", MODULE_PATH)
AA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AA)

WORKFLOW_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml")

SHA = "a" * 40
DIFF = "diff --git a/f.go b/f.go\n--- a/f.go\n+++ b/f.go\n@@ -1 +1 @@\n-a\n+b\n"
PANEL_OK = [{"model": "m", "review_type": "adversarial", "status": "ok"}]


def finding(sev):
    return {"file": "f.go", "line": 1, "severity": sev, "body": "x"}


# The real shape: 3 models × both review types.
PANEL_FULL = [{"model": m, "review_type": t, "status": "ok"}
              for m in ("m1", "m2", "m3") for t in ("adversarial", "edge-case")]


def panel_with(*errored, status="error"):
    """PANEL_FULL with each (model, review_type) in `errored` set to `status`."""
    return [dict(c, status=status) if (c["model"], c["review_type"]) in errored else dict(c) for c in PANEL_FULL]


def decide(threshold="medium", findings=(), panel=PANEL_OK, judge="ok", delivered=True,
           reviewed=SHA, live=SHA, threads=(), ungated=0, empty_diff=False, reviewed_base="main", live_base="main",
           max_failed=0):
    return AA.decide(threshold, list(findings), list(panel), judge, delivered, reviewed, live, list(threads), ungated,
                     empty_diff, reviewed_base, live_base, max_failed_reviewers=max_failed)


class ThresholdTest(unittest.TestCase):
    def test_allowed_values(self):
        for v in ("medium", "low", "nit", " Medium "):
            self.assertIn(AA.validate_threshold(v), AA.ALLOWED_THRESHOLDS)

    def test_rejects_high_critical_and_typos(self):
        for v in ("high", "critical", "med", "", "none"):
            with self.assertRaises(ValueError):
                AA.validate_threshold(v)


class DecideTest(unittest.TestCase):
    def test_approves_when_everything_at_or_below_threshold(self):
        event, _, _ = decide(findings=[finding("medium"), finding("low"), finding("nit")])
        self.assertEqual(event, AA.APPROVE)

    def test_approves_a_clean_round(self):
        self.assertEqual(decide()[0], AA.APPROVE)

    def test_requests_changes_on_finding_above_threshold(self):
        event, _, blocking = decide(threshold="low", findings=[finding("medium"), finding("nit")])
        self.assertEqual(event, AA.REQUEST_CHANGES)
        self.assertEqual(len(blocking), 1)

    def test_high_and_critical_always_block(self):
        for sev in ("high", "critical"):
            self.assertEqual(decide(findings=[finding(sev)])[0], AA.REQUEST_CHANGES)

    def test_unrecognised_severity_blocks(self):
        for sev in (None, "", "urgent", 3):
            self.assertEqual(decide(threshold="nit", findings=[finding(sev)])[0], AA.REQUEST_CHANGES)
            self.assertEqual(decide(threshold="medium", findings=[finding(sev)])[0], AA.REQUEST_CHANGES)

    def test_degraded_judge_neither_approves_nor_vetoes(self):
        self.assertEqual(decide(judge="error")[0], AA.NONE)
        self.assertEqual(decide(judge="error", findings=[finding("high")])[0], AA.NONE)

    def test_incomplete_panel_withholds(self):
        panel = PANEL_OK + [{"model": "m2", "review_type": "edge-case", "status": "error"}]
        self.assertEqual(decide(panel=panel)[0], AA.NONE)
        self.assertEqual(decide(panel=[])[0], AA.NONE)

    def test_untrusted_round_does_not_request_changes_either(self):
        panel = PANEL_OK + [{"model": "m2", "review_type": "edge-case", "status": "error"}]
        high = [finding("high")]
        self.assertEqual(decide(panel=panel, findings=high)[0], AA.NONE)
        self.assertEqual(decide(delivered=False, findings=high)[0], AA.NONE)
        self.assertEqual(decide(live="b" * 40, findings=high)[0], AA.NONE)

    def test_undelivered_review_withholds(self):
        self.assertEqual(decide(delivered=False)[0], AA.NONE)

    def test_moved_head_withholds(self):
        self.assertEqual(decide(live="b" * 40)[0], AA.NONE)
        self.assertEqual(decide(reviewed="", live="")[0], AA.NONE)

    def test_retargeted_base_withholds(self):
        # A retarget never moves the head, so only the base comparison sees it.
        self.assertEqual(decide(live_base="release")[0], AA.NONE)
        self.assertEqual(decide(live_base="release", findings=[finding("high")])[0], AA.NONE)
        self.assertEqual(decide(reviewed_base="")[0], AA.NONE)

    def test_open_prior_high_thread_withholds(self):
        for sev in ("critical", "high", None):
            self.assertEqual(decide(threads=[sev])[0], AA.NONE)

    def test_open_lower_threads_do_not_withhold(self):
        self.assertEqual(decide(threads=["medium", "low", "nit"])[0], AA.APPROVE)

    def test_prior_thread_gate_follows_the_threshold(self):
        self.assertEqual(decide(threshold="low", threads=["medium"])[0], AA.NONE)
        self.assertEqual(decide(threshold="nit", threads=["low"])[0], AA.NONE)
        self.assertEqual(decide(threshold="low", threads=["low", "nit"])[0], AA.APPROVE)

    def test_body_only_findings_withhold(self):
        # A finding demoted to the review body has no thread, so the open-thread
        # check could never see it in a later round.
        self.assertEqual(decide(ungated=1)[0], AA.NONE)
        self.assertEqual(decide(ungated=2, findings=[finding("high")])[0], AA.NONE)


    def test_empty_reviewed_diff_withholds(self):
        # A PR touching only diff_excludes paths: zero findings over nothing.
        event, reasons, _ = decide(empty_diff=True)
        self.assertEqual(event, AA.NONE)
        self.assertIn("reviewed diff is empty", reasons[0])


class MaxFailedReviewersTest(unittest.TestCase):
    """approve_max_failed_reviewers: tolerate up to N `error` cells, never more."""

    def test_default_zero_withholds_on_one_error(self):
        event, reasons, _ = decide(panel=panel_with(("m1", "edge-case")))
        self.assertEqual(event, AA.NONE)
        self.assertIn("1/6 panel reviewers did not complete", reasons[0])

    def test_one_tolerated_error_decides_normally(self):
        panel = panel_with(("m1", "edge-case"))
        event, reasons, _ = decide(panel=panel, max_failed=1)
        self.assertEqual(event, AA.APPROVE)
        self.assertIn("approved with 1/6 reviewers errored: m1:edge-case", reasons[0])
        # Decides in BOTH directions: a High still requests changes.
        event, reasons, blocking = decide(panel=panel, max_failed=1, findings=[finding("high")])
        self.assertEqual((event, len(blocking)), (AA.REQUEST_CHANGES, 1))
        self.assertIn("above `medium` (with 1/6 reviewers errored: m1:edge-case)", reasons[0])

    def test_a_no_decision_note_claims_no_decision(self):
        panel = panel_with(("m1", "edge-case"))
        event, reasons, _ = decide(panel=panel, max_failed=1, threads=["high"])
        self.assertEqual(event, AA.NONE)
        self.assertIn("earlier round (with 1/6 reviewers errored: m1:edge-case)", reasons[0])
        self.assertNotIn("decided", reasons[0])

    def test_a_clean_panel_carries_no_note(self):
        event, reasons, _ = decide(panel=PANEL_FULL, max_failed=1)
        self.assertEqual(event, AA.APPROVE)
        self.assertNotIn("errored", reasons[0])

    def test_more_errors_than_tolerated_withholds(self):
        panel = panel_with(("m1", "edge-case"), ("m2", "adversarial"))
        self.assertEqual(decide(panel=panel, max_failed=1)[0], AA.NONE)
        self.assertEqual(decide(panel=panel, max_failed=2)[0], AA.APPROVE)

    def test_a_whole_review_type_errored_withholds(self):
        # Both edge-case cells of a 2-model panel errored: within N, but no
        # completed edge-case review is left.
        panel = [c for c in panel_with(("m1", "edge-case"), ("m2", "edge-case")) if c["model"] != "m3"]
        event, reasons, _ = decide(panel=panel, max_failed=2)
        self.assertEqual(event, AA.NONE)
        self.assertIn("no completed `edge-case` review", reasons[0])
        # And on the full panel, every edge-case cell errored under a generous N.
        panel = panel_with(*[(m, "edge-case") for m in ("m1", "m2", "m3")])
        self.assertEqual(decide(panel=panel, max_failed=5)[0], AA.NONE)

    def test_a_review_type_absent_from_the_panel_withholds(self):
        # PANEL_OK has no edge-case cell at all; tolerating an adversarial error
        # must not approve over a pass that never ran.
        panel = PANEL_OK + [{"model": "m2", "review_type": "adversarial", "status": "error"}]
        self.assertEqual(decide(panel=panel, max_failed=1)[0], AA.NONE)

    def test_an_all_ok_panel_missing_a_review_type_withholds_once_tolerant(self):
        # No cell errored, but there is no edge-case pass: the floor holds under
        # any N > 0. N = 0 keeps the original rule (only `ok` is checked).
        event, reasons, _ = decide(panel=PANEL_OK, max_failed=1)
        self.assertEqual(event, AA.NONE)
        self.assertIn("no completed `edge-case` review", reasons[0])
        self.assertEqual(decide(panel=PANEL_OK)[0], AA.APPROVE)

    def test_a_non_error_bad_status_is_never_tolerated(self):
        for status in (None, "", "unknown", "skipped", "OK", "Error"):
            with self.subTest(status=status):
                self.assertEqual(decide(panel=panel_with(("m1", "edge-case"), status=status), max_failed=3)[0],
                                 AA.NONE)
        missing = panel_with()
        del missing[0]["status"]
        self.assertEqual(decide(panel=missing, max_failed=3)[0], AA.NONE)

    def test_a_non_dict_cell_is_never_tolerated(self):
        for cell in (None, "error", ["error"], 0):
            with self.subTest(cell=cell):
                self.assertEqual(decide(panel=PANEL_FULL + [cell], max_failed=3)[0], AA.NONE)

    def test_empty_panel_withholds(self):
        event, reasons, _ = decide(panel=[], max_failed=3)
        self.assertEqual((event, reasons[0]), (AA.NONE, "no panel metadata"))

    def test_other_trust_failures_still_withhold(self):
        panel = panel_with(("m1", "edge-case"))
        self.assertEqual(decide(panel=panel, max_failed=1, judge="error")[0], AA.NONE)
        self.assertEqual(decide(panel=panel, max_failed=1, delivered=False)[0], AA.NONE)

    def test_composes_with_approve_scope(self):
        # The errored-reviewer note stays on reasons[0]; the scope note keeps
        # riding as the second reason, as approve_scope documents.
        panel = panel_with(("m1", "edge-case"))
        event, reasons, _ = AA.decide("medium", [], panel, "ok", True, SHA, SHA, [], 0, False, "main", "main",
                                      scope={"scope": AA.SCOPE_FULL}, max_failed_reviewers=1)
        self.assertEqual(event, AA.APPROVE)
        self.assertIn("approved with 1/6 reviewers errored: m1:edge-case", reasons[0])
        self.assertTrue(reasons[1].startswith("approve_scope `full`"))

    def test_the_note_echoes_only_plain_tokens(self):
        panel = panel_with(("m1", "edge-case"))
        panel[1]["model"] = "@someone `x`\n"
        _, reasons, _ = decide(panel=panel, max_failed=1)
        self.assertIn("errored: ?:edge-case", reasons[0])

    def test_parse(self):
        for value, want in (("", 0), (None, 0), ("0", 0), (" 1 ", 1), ("2", 2), (3, 3), ("1.0", 1)):
            self.assertEqual(AA.parse_max_failed_reviewers(value), want, value)
        for value in ("-1", "1.5", "one", "inf", "nan", "1e2", "1_0", "+3", "0.99999999999999999"):
            with self.assertRaises(ValueError, msg=value):
                AA.parse_max_failed_reviewers(value)

    def test_review_types_match_the_workflow_matrix(self):
        with open(WORKFLOW_PATH, encoding="utf-8") as f:
            src = f.read()
        self.assertIn(f"review_type: [{', '.join(AA.PANEL_REVIEW_TYPES)}]", src)

    def test_the_workflow_passes_the_input_to_decide(self):
        with open(WORKFLOW_PATH, encoding="utf-8") as f:
            src = f.read()
        declared = src[src.index("\n      approve_max_failed_reviewers:\n"):]
        declared = declared[: declared.index("\n      max_rounds:\n")]
        self.assertIn("type: number", declared)
        self.assertIn("default: 0", declared)
        step = src[src.index("- name: Auto-approve decision"):]
        step = step[: step.index("\n\n  dismiss-stale-approval:")]
        self.assertIn("MAX_FAILED_REVIEWERS: ${{ inputs.approve_max_failed_reviewers }}", step)
        self.assertIn('--max-failed-reviewers "${MAX_FAILED_REVIEWERS:-0}"', step)


class ReviewedDiffTest(unittest.TestCase):
    def check(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
            f.write(text)
        try:
            return AA.reviewed_diff_is_empty(f.name)
        finally:
            os.unlink(f.name)

    def test_a_content_hunk_is_not_empty(self):
        self.assertFalse(self.check(DIFF))

    def test_no_hunk_is_empty(self):
        self.assertTrue(self.check(""))
        self.assertTrue(self.check("\n"))
        # Pure rename / binary: a header, but no content a reviewer could read.
        self.assertTrue(self.check("diff --git a/x b/y\nsimilarity index 100%\nrename from x\nrename to y\n"))
        self.assertTrue(self.check("diff --git a/i.png b/i.png\nBinary files a/i.png and b/i.png differ\n"))

    def test_missing_patch_is_empty(self):
        self.assertTrue(AA.reviewed_diff_is_empty("/nonexistent/pr-diff.patch"))


class ThreadSeverityTest(unittest.TestCase):
    def test_reads_post_review_badges(self):
        self.assertEqual(AA.thread_severity("🟠 **High** — something"), "high")
        self.assertEqual(AA.thread_severity("⚪ **Nit** — something"), "nit")

    def test_unbadged_is_none(self):
        self.assertIsNone(AA.thread_severity("just a comment"))
        self.assertIsNone(AA.thread_severity(""))


OLD = "1" * 40
NEW = "2" * 40


def graphql_reviews(reviews, *, has_next=False, cursor=None):
    """The GraphQL envelope `list_reviews` reads, built from REST-shaped stubs.

    Lets every test below keep writing a review the way the REST payload spelled
    it — and pins the two translations that payload does not have: the `[bot]`
    suffix GraphQL drops, and `edited`.
    """
    nodes = []
    for r in reviews:
        login = (r.get("user") or {}).get("login", "")
        bot = login.endswith("[bot]")
        nodes.append({
            "fullDatabaseId": str(r["id"]),
            "databaseId": r["id"],
            "state": r.get("state"),
            "body": r.get("body"),
            "lastEditedAt": "2026-10-06T12:00:00Z" if r.get("edited") else None,
            "submittedAt": r.get("submitted_at"),
            "url": r.get("html_url"),
            "author": {"__typename": "Bot" if bot else "User",
                       "login": login[: -len("[bot]")] if bot else login},
        })
    return json.dumps({"data": {"repository": {"pullRequest": {"reviews": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes,
    }}}}})


class ReviewsListTest(unittest.TestCase):
    """The GraphQL reviews list, which exists for `lastEditedAt` alone.

    Each case here is a way the switch away from REST could have broken the
    dismissal silently — the direction that exits green while withdrawing
    nothing.
    """

    def list_reviews(self, *pages):
        calls = []

        def fake_gh(args, payload=None):
            calls.append(args)
            return pages[len(calls) - 1]

        with mock.patch.object(AA, "gh", fake_gh):
            return AA.list_reviews("o/r", "7"), calls

    def test_a_bots_login_keeps_the_suffix_the_workflow_passes(self):
        # --approver-login is "${APP_SLUG}[bot]" / "github-actions[bot]", while
        # GraphQL answers "github-actions". Dropping this would compare the two
        # and match nothing, for every PR, forever.
        reviews, _ = self.list_reviews(graphql_reviews([
            {"id": 1, "user": {"login": "github-actions[bot]"}, "state": "APPROVED", "body": "b"},
        ]))
        self.assertEqual(reviews[0]["user"]["login"], "github-actions[bot]")

    def test_a_humans_login_is_untouched(self):
        reviews, _ = self.list_reviews(graphql_reviews([
            {"id": 2, "user": {"login": "a-human"}, "state": "COMMENTED", "body": "b"},
        ]))
        self.assertEqual(reviews[0]["user"]["login"], "a-human")

    def test_the_rest_id_survives_past_the_32_bit_range(self):
        # Live review ids are already past 2^31-1, and the dismissal endpoint is
        # REST, so the id this list carries has to be the full one.
        big = 5434892948
        reviews, _ = self.list_reviews(graphql_reviews([
            {"id": big, "user": {"login": "x"}, "state": "APPROVED", "body": "b"},
        ]))
        self.assertEqual(reviews[0]["id"], big)

    def test_an_edit_is_reported_and_an_unedited_review_is_not(self):
        reviews, _ = self.list_reviews(graphql_reviews([
            {"id": 1, "user": {"login": "x"}, "state": "APPROVED", "body": "b", "edited": True},
            {"id": 2, "user": {"login": "x"}, "state": "APPROVED", "body": "b"},
        ]))
        self.assertEqual([r["edited"] for r in reviews], [True, False])

    def test_every_page_is_read(self):
        reviews, calls = self.list_reviews(
            graphql_reviews([{"id": 1, "user": {"login": "x"}, "state": "APPROVED", "body": "b"}],
                            has_next=True, cursor="CUR"),
            graphql_reviews([{"id": 2, "user": {"login": "x"}, "state": "APPROVED", "body": "b"}]),
        )
        self.assertEqual([r["id"] for r in reviews], [1, 2])
        self.assertIn("cursor=null", " ".join(calls[0]))
        self.assertIn("cursor=CUR", " ".join(calls[1]))

    def test_a_page_with_no_cursor_to_follow_raises(self):
        # hasNextPage with no cursor: neither re-request page one until the job
        # times out, nor return the truncated list — reviews come back
        # oldest-first, so the dropped page holds the newest approval.
        page = graphql_reviews([{"id": 1, "user": {"login": "x"}, "state": "APPROVED", "body": "b"}],
                               has_next=True, cursor=None)
        with self.assertRaises(ValueError):
            self.list_reviews(page)

    def test_a_cursor_that_does_not_advance_raises(self):
        page = graphql_reviews([{"id": 1, "user": {"login": "x"}, "state": "APPROVED", "body": "b"}],
                               has_next=True, cursor="CUR")
        with self.assertRaises(ValueError):
            self.list_reviews(page, page)

    def test_a_null_reviews_connection_raises(self):
        with self.assertRaises(ValueError):
            self.list_reviews(json.dumps({"data": {"repository": {"pullRequest": {"reviews": None}}}}))

    def test_a_review_with_no_id_raises(self):
        # Not a None carried into the dismissal URL, which 404s as a misleading
        # "could not dismiss".
        page = json.dumps({"data": {"repository": {"pullRequest": {"reviews": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"fullDatabaseId": None, "databaseId": None, "state": "APPROVED", "body": "b",
                       "lastEditedAt": None, "author": {"__typename": "User", "login": "x"}}],
        }}}}})
        with self.assertRaises(ValueError):
            self.list_reviews(page)

    def test_a_shapeless_response_raises_instead_of_listing_nothing(self):
        # Both callers read an empty list as "nothing to dismiss" and exit green,
        # so this has to reach their error paths instead.
        with self.assertRaises(ValueError):
            self.list_reviews(json.dumps({"data": {"repository": {"pullRequest": None}}}))


class StaleReviewTest(unittest.TestCase):
    def review(self, rid, login="cursor-approver", state="APPROVED", commit=None, marker=True, sha=OLD, base=None,
               edited=False):
        # `commit_id` defaults to NEW on purpose: GitHub moves a still-valid
        # approval's commit_id to each new head, so only the SHA recorded in the
        # body says what was reviewed. `base=None` records no base (an approval
        # posted before the base was recorded).
        body = AA.render_body(AA.APPROVE, ["ok"], "low", [], sha, base or "") if marker else "ok"
        return {"id": rid, "user": {"login": login}, "state": state, "commit_id": commit or NEW,
                "body": body, "edited": edited}

    def test_dismisses_own_marked_off_head_reviews(self):
        reviews = [
            self.review(1),
            self.review(2, state="CHANGES_REQUESTED"),
            self.review(3, sha=NEW),
            self.review(4, login="a-human"),
            self.review(5, marker=False),
            self.review(6, state="COMMENTED"),
            self.review(7, state="DISMISSED"),
            self.review(8, login="Cursor-Approver"),
        ]
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", NEW), [1, 8])

    def test_moved_commit_id_does_not_hide_a_stale_approval(self):
        # The live failure: approval on OLD, GitHub reports commit_id=NEW after a push.
        self.assertEqual(AA.stale_reviews_to_dismiss([self.review(1, commit=NEW, sha=OLD)], "cursor-approver", NEW), [1])

    def test_marked_approval_without_recorded_sha_is_stale(self):
        r = self.review(1, commit=NEW)
        r["body"] = AA.APPROVE_MARKER + "\nlegacy"
        self.assertEqual(AA.stale_reviews_to_dismiss([r], "cursor-approver", NEW), [1])

    def test_body_records_the_reviewed_sha(self):
        self.assertIn(f"sha={OLD}", AA.render_body(AA.APPROVE, ["ok"], "low", [], OLD))
        self.assertNotIn("sha=", AA.render_body(AA.APPROVE, ["ok"], "low", [], "not-a-sha"))

    def test_a_push_never_clears_the_bots_change_request(self):
        # Under a label-triggered caller a push starts no new round, so only a
        # later round's own review may supersede the veto.
        reviews = [self.review(1, state="CHANGES_REQUESTED")]
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", NEW), [])

    def test_an_edited_approval_is_stale_even_when_its_marker_says_head(self):
        # The forge: a write-access user edits this identity's approval and
        # rewrites the recorded SHA to the live head. Trusting the marker would
        # keep that approval standing through every later push.
        reviews = [self.review(1, sha=NEW, edited=True)]
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", NEW), [1])

    def test_an_unedited_approval_on_head_still_stands(self):
        # The other half: distrusting edits must not withdraw the approvals this
        # workflow posts itself, which are never edited.
        reviews = [self.review(1, sha=NEW)]
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", NEW), [])

    def test_an_edited_approval_with_its_marker_removed_is_still_dismissed(self):
        # The cheaper forgery: delete the marker rather than rewrite the SHA.
        # Gating on the marker first would take the approval out of every filter.
        reviews = [self.review(1, marker=False, edited=True)]
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", NEW), [1])
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", None), [1])

    def test_an_unedited_unmarked_approval_is_still_untouched(self):
        # e.g. a manual approval by a human APPROVER_TOKEN identity.
        reviews = [self.review(1, marker=False)]
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", None), [])

    def test_another_logins_edited_unmarked_approval_is_not_unactionable(self):
        # A human editing their own approval is not a stale auto-approval, so it
        # must not turn the dismissal job red.
        reviews = [self.review(1, login="a-human", marker=False, edited=True)]
        self.assertEqual(AA.unactionable_stale_approvals(reviews, "cursor-approver", NEW), [])
        self.assertEqual(AA.unactionable_stale_approvals(reviews, "", NEW), [])

    def test_an_edited_change_request_is_still_not_dismissed(self):
        # Only APPROVALS are this filter's business; an edited veto is someone
        # tampering with a review that withholds merge, not one that grants it.
        reviews = [self.review(1, state="CHANGES_REQUESTED", edited=True)]
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", NEW), [])

    def test_none_head_selects_every_own_approval(self):
        reviews = [self.review(1, sha=NEW), self.review(2), self.review(3, state="CHANGES_REQUESTED")]
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", None), [1, 2])

    def test_recorded_base_round_trips(self):
        for ref in ("main", "release/1.x", "odd-->ref"):
            self.assertEqual(AA.recorded_base(self.review(1, base=ref)["body"]), ref)
        self.assertIsNone(AA.recorded_base(AA.APPROVE_MARKER))

    def test_off_base_approval_is_stale_on_head_too(self):
        reviews = [self.review(1, sha=NEW, base="main"), self.review(2, sha=NEW, base="release"),
                   self.review(3, sha=NEW)]  # 3 pre-dates the record: only --all-approvals reaches it
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", NEW, "release"), [1])

    def test_other_identities_stale_approvals_are_unactionable(self):
        reviews = [self.review(1), self.review(2, login="old-approver"), self.review(3, login="old-approver", sha=NEW),
                   self.review(4, login="a-human", marker=False)]
        self.assertEqual(AA.unactionable_stale_approvals(reviews, "cursor-approver", NEW), ["old-approver:2"])
        # No approver identity in this run: every stale marked approval is unactionable.
        self.assertEqual(AA.unactionable_stale_approvals(reviews, "", NEW), ["cursor-approver:1", "old-approver:2"])

    def run_dismiss(self, reviews, all_approvals=False, list_error=None, login="cursor-approver", live_base="main",
                    labels=None):
        # `labels=None` is a real PR's empty label list; pass a malformed value
        # (a number, a nameless label) to exercise the unreadable-labels path.
        labels = [] if labels is None else labels
        puts = []

        def fake_gh(args, payload=None):
            if args[:2] == ["api", "graphql"]:
                if list_error:
                    raise list_error
                return graphql_reviews(reviews)
            if args[:2] == ["api", "-X"]:
                puts.append((args[3], payload["message"]))
                return "{}"
            # The live PR: head NEW, even when the event that started the run carried an older one.
            return json.dumps({"head": {"sha": NEW}, "base": {"ref": live_base}, "labels": labels})

        args = argparse.Namespace(repo="o/r", pr_number="1", approver_login=login, all_approvals=all_approvals,
                                  head_sha=OLD)
        self.printed, self.emitted = [], []
        with mock.patch.object(AA, "gh", fake_gh), mock.patch.object(AA, "emit", self.emitted.append), \
                mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
            return AA.cmd_dismiss_stale(args), puts

    def test_push_keeps_an_on_head_approval(self):
        rc, puts = self.run_dismiss([self.review(1, sha=NEW), self.review(2)])
        self.assertEqual((rc, [p[0] for p in puts]), (0, ["repos/o/r/pulls/1/reviews/2/dismissals"]))

    def test_an_edited_on_head_approval_is_dismissed_end_to_end(self):
        # Through the GraphQL list: the edit has to survive the translation to
        # reach the staleness check, even with both markers forged to live state.
        rc, puts = self.run_dismiss([self.review(1, sha=NEW, base="main", edited=True)])
        self.assertEqual((rc, [p[0] for p in puts]), (0, ["repos/o/r/pulls/1/reviews/1/dismissals"]))

    def test_retarget_dismisses_the_on_head_approval_too(self):
        # `edited` with changes.base: the head did not move, the diff did.
        rc, puts = self.run_dismiss([self.review(1, sha=NEW), self.review(2)], all_approvals=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(puts), 2)
        self.assertTrue(all(m == AA.BASE_CHANGED_MESSAGE for _, m in puts))

    def test_unlistable_reviews_fail_red_without_a_traceback(self):
        rc, puts = self.run_dismiss([], list_error=RuntimeError("502"))
        self.assertEqual((rc, puts), (1, []))

    def test_a_later_event_redoes_a_cancelled_retarget_dismissal(self):
        # The retarget's own run was cancelled; a title edit (no BASE_FROM) runs
        # next and still withdraws the on-head approval recorded against the old base.
        rc, puts = self.run_dismiss([self.review(1, sha=NEW, base="main")], live_base="release")
        self.assertEqual((rc, [p[0] for p in puts]), (0, ["repos/o/r/pulls/1/reviews/1/dismissals"]))

    def test_an_unactionable_stale_approval_goes_red(self):
        # e.g. APPROVER_TOKEN unavailable on a Dependabot PR: the fallback
        # identity cannot dismiss the real approver's stale approval.
        rc, puts = self.run_dismiss([self.review(1, login="cursor-approver")], login="github-actions[bot]")
        self.assertEqual((rc, puts), (1, []))
        rc, puts = self.run_dismiss([self.review(1)], login="")
        self.assertEqual((rc, puts), (1, []))

    def test_nothing_to_check_is_clean_without_the_approver(self):
        rc, puts = self.run_dismiss([self.review(1, sha=NEW, base="main")], login="")
        self.assertEqual((rc, puts), (0, []))

    VETO = [{"name": "skip-cursor-review"}]

    def test_the_veto_label_withdraws_every_marked_approval(self):
        # The gate runs no round on a vetoed PR, so the `labeled` event's
        # dismissal is the only thing that withdraws an approval already standing.
        rc, puts = self.run_dismiss([self.review(1, sha=NEW, base="main"), self.review(2)], labels=self.VETO)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p[0] for p in puts),
                         ["repos/o/r/pulls/1/reviews/1/dismissals", "repos/o/r/pulls/1/reviews/2/dismissals"])
        self.assertTrue(all(m == AA.SKIP_REVIEW_WITHDRAWN_MESSAGE for _, m in puts))
        self.assertTrue(any("`skip-cursor-review`" in line and "cursor-approver" in line for line in self.emitted))

    def test_the_veto_label_matches_case_insensitively(self):
        rc, puts = self.run_dismiss([self.review(1, sha=NEW, base="main")], labels=[{"name": "Skip-Cursor-Review"}])
        self.assertEqual((rc, [m for _, m in puts]), (0, [AA.SKIP_REVIEW_WITHDRAWN_MESSAGE]))

    def test_the_veto_message_wins_over_a_retarget(self):
        rc, puts = self.run_dismiss([self.review(1, sha=NEW, base="main"), self.review(2)], all_approvals=True,
                                    labels=self.VETO)
        self.assertEqual((rc, len(puts)), (0, 2))
        self.assertTrue(all(m == AA.SKIP_REVIEW_WITHDRAWN_MESSAGE for _, m in puts))

    def test_an_unrelated_label_changes_nothing(self):
        rc, puts = self.run_dismiss([self.review(1, sha=NEW, base="main"), self.review(2)],
                                    labels=[{"name": "cursor-review"}])
        self.assertEqual((rc, puts), (0, [("repos/o/r/pulls/1/reviews/2/dismissals", AA.STALE_MESSAGE)]))
        self.assertFalse(any("::warning::" in line for line in self.printed))

    def test_another_identitys_on_head_approval_on_a_vetoed_pr_goes_red(self):
        rc, puts = self.run_dismiss([self.review(1, sha=NEW, base="main"),
                                     self.review(2, login="old-approver", sha=NEW, base="main")], labels=self.VETO)
        self.assertEqual((rc, [p[0] for p in puts]), (1, ["repos/o/r/pulls/1/reviews/1/dismissals"]))
        self.assertTrue(any(line.startswith("::error::") and "old-approver:2" in line for line in self.printed))

    def test_the_veto_leaves_an_unedited_unmarked_human_approval(self):
        rc, puts = self.run_dismiss([self.review(1, marker=False), self.review(2, login="a-human", marker=False)],
                                    labels=self.VETO)
        self.assertEqual((rc, puts), (0, []))

    def test_unreadable_labels_warn_and_fall_through_to_head_and_base(self):
        # Not red and no mass withdrawal: the job runs on every event, and the
        # next one redoes the check.
        for labels in (7, [{"name": 3}], [{"name": "skip-cursor-review"}, "skip-cursor-review"]):
            with self.subTest(labels=labels):
                rc, puts = self.run_dismiss([self.review(1, sha=NEW, base="main"), self.review(2)], labels=labels)
                self.assertEqual((rc, puts), (0, [("repos/o/r/pulls/1/reviews/2/dismissals", AA.STALE_MESSAGE)]))
                self.assertTrue(any(line.startswith("::warning::") and "skip-cursor-review" in line
                                    for line in self.printed))


class DismissJobTriggerTest(unittest.TestCase):
    """The workflow half of dismissal: when the job runs at all."""

    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW_PATH, encoding="utf-8") as f:
            text = f.read()
        job = re.search(r"^  dismiss-stale-approval:\n(.*?)^  \S", text, re.S | re.M).group(1)
        cls.job = job
        cls.cond = re.search(r"^    if: >-\n(.*?)^    \S", job, re.S | re.M).group(1)

    def test_not_gated_on_the_kill_switch(self):
        # Unsetting approve_max_severity must stop NEW approvals, not dismissal
        # of the ones already on the PR.
        self.assertNotIn("approve_max_severity", self.cond)

    def test_runs_on_every_action_of_an_open_pr(self):
        # Any event sharing a concurrency slot can cancel an in-flight
        # dismissal, so none may be filtered out by action — only closed PRs.
        # It is also the guard for the veto label: `labeled` must keep reaching
        # the job, or applying `skip-cursor-review` withdraws nothing.
        self.assertNotIn("github.event.action", self.cond)
        self.assertIn("github.event.pull_request.state == 'open'", self.cond)

    def test_a_missing_app_key_checks_instead_of_passing(self):
        self.assertNotIn("exit 0", self.job)
        self.assertIn('LOGIN=""', self.job)

    def test_a_retarget_dismisses_every_approval(self):
        self.assertIn("BASE_FROM: ${{ github.event.changes.base.ref.from }}", self.job)
        self.assertIn('if [ -n "$BASE_FROM" ]; then\n            ALL=(--all-approvals)', self.job)
        self.assertIn('"${ALL[@]}"', self.job)

    def test_decide_reads_the_reviewed_diff_and_base(self):
        with open(WORKFLOW_PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("--reviewed-diff /tmp/pr-diff.patch", text)
        self.assertIn("BASE_REF: ${{ github.event.pull_request.base.ref }}", text)
        self.assertIn('--base-ref "$BASE_REF"', text)


class ShapelessPayloadTest(unittest.TestCase):
    """No payload shape may reach a caller as a traceback.

    `read_pr` and `list_reviews` are both read behind
    `except (RuntimeError, ValueError)`. An AttributeError/TypeError/KeyError from
    a malformed body escapes that, so it bypasses every announced degradation —
    the exact outcome the announcements exist to prevent.
    """

    def read_pr_with(self, body):
        with mock.patch.object(AA, "gh", lambda *a, **k: body):
            return AA.read_pr("o/r", 1)

    def head_base_with(self, body):
        return AA.pr_head_base(self.read_pr_with(body))

    def test_a_well_formed_payload_is_read(self):
        self.assertEqual(self.head_base_with('{"head":{"sha":"abc"},"base":{"ref":"main"}}'), ("abc", "main"))

    def test_a_non_object_payload_is_a_value_error(self):
        # read_pr hands the whole payload to pr_head_base AND has_label, so the
        # top-level guard belongs in the read, not in either consumer.
        for body in ("null", "[]", '"s"', "7"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.read_pr_with(body)

    def test_a_nested_non_mapping_becomes_the_shapeless_value(self):
        # A truthy non-mapping head/base would make `.get` raise AttributeError.
        for body in ('{"head":"abc","base":3}', '{"head":[1],"base":["main"]}', '{"head":true}'):
            with self.subTest(body=body):
                self.assertEqual(self.head_base_with(body), ("", ""))

    def test_a_non_string_sha_never_reaches_lower(self):
        # It would survive `if not live_head` and then hit head_sha.lower().
        self.assertEqual(self.head_base_with('{"head":{"sha":12345},"base":{"ref":null}}'), ("", ""))

    def test_a_shapeless_payload_still_answers_has_label(self):
        # The other consumer of the same read: it must not crash on a payload
        # whose `labels` is not a list of objects.
        for body in ('{"labels":"x"}', '{"labels":[null,"y"]}', "{}"):
            with self.subTest(body=body):
                self.assertFalse(AA.has_label(self.read_pr_with(body), AA.HUMAN_REVIEW_LABEL))

    def list_reviews_with(self, body):
        with mock.patch.object(AA, "gh", lambda *a, **k: body):
            return AA.list_reviews("o/r", 1)

    def test_a_non_object_body_is_a_value_error(self):
        # `null`, an array or a string cannot answer `.get`; nor can a `data`,
        # `repository` or `pullRequest` of the wrong shape. Each would otherwise
        # escape as an AttributeError past both call sites, bypassing the
        # announced degradation AND DISMISS_PERMISSION_HINT.
        for body in ("null", "[]", '"s"', '{"message":"Not Found"}', '{"data":[]}',
                     '{"data":{"repository":"x"}}', '{"data":{"repository":{"pullRequest":[1]}}}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.list_reviews_with(body)

    def reviews_body(self, nodes):
        return json.dumps({"data": {"repository": {"pullRequest": {"reviews": {
            "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes,
        }}}}})

    def good_node(self, **over):
        node = json.loads(graphql_reviews([{"id": 1, "user": {"login": "x"}, "state": "APPROVED",
                                            "body": AA.APPROVE_MARKER}]))["data"]["repository"][
            "pullRequest"]["reviews"]["nodes"][0]
        node.update(over)
        return node

    def test_a_non_object_review_is_a_value_error_not_a_silent_drop(self):
        # Dropping it would be the one unannounced degradation: both callers read
        # a missing review as "nothing to dismiss" and exit green, and the
        # dropped node may be the newest approval.
        for bad in ("abc", None, 7):
            with self.subTest(node=bad), self.assertRaises(ValueError):
                self.list_reviews_with(self.reviews_body([bad, self.good_node()]))

    def test_a_non_list_nodes_field_is_a_value_error(self):
        with self.assertRaises(ValueError):
            self.list_reviews_with(self.reviews_body(5))

    def test_wrong_shaped_review_fields_are_coerced_before_the_staleness_scan(self):
        # `_stale_approvals` runs outside both callers' `try`, so a non-mapping
        # author or a non-string body/state there would escape as a traceback.
        node = self.good_node(author="mallory", body=5, state=["APPROVED"])
        review = self.list_reviews_with(self.reviews_body([node]))[0]
        self.assertEqual((review["user"], review["body"], review["state"]), ({"login": ""}, "", ""))
        self.assertEqual(AA.stale_reviews_to_dismiss([review], "x", None), [])
        node = self.good_node(author={"__typename": "User", "login": 9})
        self.assertEqual(self.list_reviews_with(self.reviews_body([node]))[0]["user"], {"login": ""})

    def test_a_non_numeric_review_id_is_a_value_error(self):
        for rid in ({"x": 1}, [1], True, "abc"):
            with self.subTest(rid=rid), self.assertRaises(ValueError):
                self.list_reviews_with(self.reviews_body([self.good_node(fullDatabaseId=rid)]))

    def test_a_non_list_labels_field_answers_has_label(self):
        # A truthy non-iterable is the shape `or []` does not guard.
        for body in ('{"labels":5}', '{"labels":true}', '{"labels":{"name":"needs-human-review"}}',
                     '{"labels":[{"name":7}]}'):
            with self.subTest(body=body):
                self.assertFalse(AA.has_label(self.read_pr_with(body), AA.HUMAN_REVIEW_LABEL))

    def paginate_with(self, body):
        with mock.patch.object(AA, "gh", lambda *a, **k: body):
            return AA._paginate("repos/o/r/issues/1/timeline?per_page=100")

    def test_paginate_flattens_slurped_pages(self):
        self.assertEqual(self.paginate_with("[[1, 2], [3], []]"), [1, 2, 3])
        self.assertEqual(self.paginate_with("[]"), [])

    def test_paginate_rejects_every_other_shape_as_a_value_error(self):
        # Each would otherwise be a KeyError/TypeError past round-cap's fail-open
        # handler — a red `round-cap` that skips the whole panel.
        for body in ("null", '{"message":"x"}', '[{"message":"x"}]', "[[1], null]", "[[1], 7]", '"s"'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.paginate_with(body)


class AnnotationCauseTest(unittest.TestCase):
    """One line, whatever the error carries — see `annotation_cause`."""

    def test_no_error_uses_the_fallback(self):
        self.assertEqual(AA.annotation_cause(None, "nothing came back"), "nothing came back")

    def test_the_type_is_named_when_the_message_is_empty(self):
        # str(RuntimeError()) is "", which would render a bare "()".
        self.assertEqual(AA.annotation_cause(RuntimeError(), "x"), "RuntimeError")

    def test_multiline_stderr_collapses_to_one_line(self):
        got = AA.annotation_cause(RuntimeError("gh: HTTP 403\n::error::injected\nsee docs"), "x")
        self.assertEqual(got, "RuntimeError: gh: HTTP 403 ::error::injected see docs")

    def test_an_overlong_cause_is_clamped(self):
        # A proxy's HTML error page is kilobytes; the failure lists join one cause
        # per review id ahead of DISMISS_PERMISSION_HINT.
        got = AA.annotation_cause(RuntimeError("<html>" + "x" * 5000), "x")
        self.assertEqual(len(got), AA.ANNOTATION_CAUSE_MAX)
        self.assertTrue(got.endswith("…"))
        self.assertEqual(AA.annotation_cause(RuntimeError("short"), "x"), "RuntimeError: short")

    def test_percent_is_escaped_so_the_runner_cannot_decode_a_newline_back_in(self):
        # The runner percent-decodes %0A/%0D when it renders an annotation, so an
        # encoded sequence would undo the collapsing. Escape `%` first.
        got = AA.annotation_cause(RuntimeError("bad url http://x/a%0Ab%0D%0Ac"), "x")
        self.assertNotIn("%0A", got)
        self.assertNotIn("%0D", got)
        self.assertIn("%250A", got)


class DismissStaleHeadTest(unittest.TestCase):
    """`dismiss-stale` judges against the LIVE head, not the delivered event's.

    A queued or redelivered `synchronize` replays an older head, and dismissing an
    approval that is valid for the current head exits GREEN — nothing reports the
    loss — so this read is the only thing between a replay and a silently withdrawn
    approval. An unreadable head degrades to the event's, never to "".
    """

    def run_dismiss(self, live, event_head, reviews, live_base="main", all_approvals=False, raw=None):
        dismissed = []

        def fake_gh(args, payload=None):
            if args[:2] == ["api", "-X"] and args[2] == "PUT":
                dismissed.append(args[3])
                return "{}"
            if args[:2] == ["api", "graphql"]:
                return graphql_reviews(list(reviews))
            if raw is not None:
                return raw  # valid JSON of the wrong shape
            if isinstance(live, Exception):
                raise live
            # A real payload carries a base alongside the head, so the fixture does
            # too — without it `live_base` is "" on every path and the off-base
            # comparison is dead in this whole class. `live_base=""` is the
            # half-shapeless response, exercised deliberately below. Likewise its
            # (empty) labels, whose absence `dismiss-stale` warns about.
            return json.dumps({} if live is None else {"head": {"sha": live}, "base": {"ref": live_base},
                                                       "labels": []})

        args = argparse.Namespace(repo="o/r", pr_number="1", head_sha=event_head,
                                  approver_login="cursor-approver", all_approvals=all_approvals)
        self.printed = []
        with mock.patch.object(AA, "gh", fake_gh), mock.patch.object(AA, "emit", lambda *a: None), \
                mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
            rc = AA.cmd_dismiss_stale(args)
        return rc, dismissed

    def approval(self, rid, sha, base=""):
        return {"id": rid, "user": {"login": "cursor-approver"}, "state": "APPROVED",
                "body": AA.render_body(AA.APPROVE, ["ok"], "low", [], sha, base)}

    def test_a_replayed_older_event_does_not_dismiss_a_current_approval(self):
        # The race: the event carries OLD, the PR is really on NEW, and the approval
        # was recorded at NEW. Comparing against args.head_sha would withdraw it.
        self.assertEqual(self.run_dismiss(NEW, OLD, [self.approval(1, NEW)]), (0, []))

    def test_a_genuinely_stale_approval_is_still_dismissed(self):
        rc, dismissed = self.run_dismiss(NEW, NEW, [self.approval(1, OLD)])
        self.assertEqual((rc, len(dismissed)), (0, 1))

    def test_an_unreadable_head_falls_back_to_the_event_head(self):
        # Previous behaviour, deliberately: degrade to the delivered head rather
        # than skip the scan this job exists to perform.
        rc, dismissed = self.run_dismiss(RuntimeError("timeout"), NEW, [self.approval(1, OLD)])
        self.assertEqual((rc, len(dismissed)), (0, 1))

    def test_an_unreadable_head_still_keeps_an_approval_the_event_head_matches(self):
        self.assertEqual(self.run_dismiss(RuntimeError("x"), NEW, [self.approval(1, NEW)]), (0, []))

    def test_an_unreadable_head_never_falls_back_to_a_non_sha_event_head(self):
        # The recorded marker is always lowercase 40-hex, so an abbreviated SHA or
        # a ref name matches none and would withdraw every approval — exactly the
        # reason "" is refused.
        for event_head in (NEW[:7], "refs/heads/main", "z" * 40):
            with self.subTest(event_head=event_head):
                rc, dismissed = self.run_dismiss(RuntimeError("x"), event_head, [self.approval(1, NEW)])
                self.assertEqual((rc, dismissed), (1, []))

    def test_an_uppercase_event_head_is_still_a_usable_fallback(self):
        self.assertEqual(self.run_dismiss(RuntimeError("x"), ("a" * 40).upper(),
                                          [self.approval(1, "a" * 40)]), (0, []))

    def test_an_unreadable_head_with_no_event_head_goes_red(self):
        # Nothing to judge against: report it, never fall through to "" (which
        # would match no recorded SHA and dismiss everything).
        self.assertEqual(self.run_dismiss(RuntimeError("x"), "", [self.approval(1, NEW)]), (1, []))

    def warnings(self):
        return [line for line in self.printed if "::warning::" in line]

    def test_a_degraded_head_read_is_announced_rather_than_silent(self):
        # The dismissal below may be the right one or the replay this read exists to
        # catch — unreadable means UNKNOWN. Exiting green on it without a word is
        # what makes a wrongly withdrawn approval unattributable afterwards.
        rc, dismissed = self.run_dismiss(RuntimeError("timeout"), NEW, [self.approval(1, OLD)])
        self.assertEqual((rc, len(dismissed)), (0, 1))
        self.assertTrue(any("live PR head" in line for line in self.warnings()), self.printed)

    def test_a_degraded_read_names_its_cause_even_when_the_error_is_empty(self):
        # str(RuntimeError()) is "", so the cause has to carry the type as well or
        # the annotation reads "( )" and says nothing about what failed.
        self.run_dismiss(RuntimeError(), NEW, [self.approval(1, NEW)])
        self.assertTrue(any("RuntimeError" in line for line in self.warnings()), self.printed)

    def test_a_shapeless_head_read_is_announced_too(self):
        # The fallback that fires without raising, so an `except`-only diagnostic
        # would miss it.
        self.run_dismiss(None, NEW, [self.approval(1, NEW)])
        self.assertTrue(any("no head in the response" in line for line in self.warnings()), self.printed)

    def test_a_veto_needs_no_head(self):
        # Like a retarget: a read that carried the veto label but no head still
        # withdraws everything, rather than going red for want of a comparison.
        raw = json.dumps({"base": {"ref": "main"}, "labels": [{"name": "skip-cursor-review"}]})
        rc, dismissed = self.run_dismiss(None, "", [self.approval(1, NEW), self.approval(2, OLD)], raw=raw)
        self.assertEqual((rc, len(dismissed)), (0, 2))
        self.assertTrue(any("because the PR carries `skip-cursor-review`" in line for line in self.printed))

    def test_a_head_that_read_cleanly_warns_about_nothing(self):
        self.run_dismiss(NEW, NEW, [self.approval(1, OLD)])
        self.assertEqual(self.warnings(), [])

    def test_a_multiline_read_error_is_collapsed_into_one_annotation(self):
        # `gh` stderr is usually several lines. Interpolated raw, the tail falls out
        # of the annotation, and a continuation line starting with `::` is re-parsed
        # as a new workflow command — truncating this very diagnostic.
        self.run_dismiss(RuntimeError("HTTP 403\n::error::injected\nsee https://docs"), NEW,
                         [self.approval(1, NEW)])
        warned = self.warnings()
        self.assertEqual(len(warned), 1, self.printed)
        self.assertNotIn("\n", warned[0])
        self.assertIn("HTTP 403 ::error::injected see https://docs", warned[0])

    def test_a_live_head_without_a_base_keeps_every_recorded_approval(self):
        # The off-base twin of the shapeless-head bug, and the reason the head
        # fallback is not the only announced degradation: "" is not None and no
        # recorded base equals "", so an unguarded empty live base withdraws every
        # approval that recorded one — on head, green, and unannounced.
        rc, dismissed = self.run_dismiss(NEW, NEW, [self.approval(1, NEW, base="main")], live_base="")
        self.assertEqual((rc, dismissed), (0, []))
        self.assertTrue(any("no base ref" in line for line in self.warnings()), self.printed)

    def test_a_headless_read_still_compares_the_base_it_did_return(self):
        # The read SUCCEEDED but carried no head: the head falls back to the event's,
        # but a usable base must not be discarded with it, or an off-base approval
        # survives and the warning implies a base check skipped for no reason.
        rc, dismissed = self.run_dismiss("", NEW, [self.approval(1, NEW, base="old-base")], live_base="main")
        self.assertEqual((rc, len(dismissed)), (0, 1))
        warned = " ".join(self.warnings())
        self.assertIn("with the base check against the live base", warned)
        self.assertNotIn("without the base check", warned)

    def test_a_readable_base_still_dismisses_a_genuinely_retargeted_approval(self):
        # The guard above skips the base check; it must not disable it.
        rc, dismissed = self.run_dismiss(NEW, NEW, [self.approval(1, NEW, base="old-base")], live_base="main")
        self.assertEqual((rc, len(dismissed)), (0, 1))

    def test_the_degraded_warning_does_not_name_a_head_when_all_approvals_is_set(self):
        # `--all-approvals` ignores the head, so naming a SHA as the comparison
        # basis would misdirect whoever investigates the mass withdrawal. The TAIL
        # matters as much as the basis: withdrawing a head-valid approval is the
        # deliberate purpose of a retarget, not a degradation to apologise for.
        rc, dismissed = self.run_dismiss(RuntimeError("timeout"), NEW, [self.approval(1, NEW)],
                                         all_approvals=True)
        self.assertEqual((rc, len(dismissed)), (0, 1))
        warned = " ".join(self.warnings())
        self.assertIn("--all-approvals", warned)
        self.assertNotIn(NEW, warned)
        self.assertNotIn("valid for the head as it stands now", warned)
        self.assertNotIn("without the base check, for this run", warned)

    def test_a_retarget_still_withdraws_everything_with_no_event_head(self):
        # `--all-approvals` needs no head to do its job, so the empty-head red gate
        # must not block the one event where every approval MUST be withdrawn.
        rc, dismissed = self.run_dismiss(RuntimeError("x"), "", [self.approval(1, NEW)], all_approvals=True)
        self.assertEqual((rc, len(dismissed)), (0, 1))

    def test_a_missing_base_under_all_approvals_does_not_claim_a_head_comparison(self):
        # "comparing on head alone" is false here: the head is discarded and every
        # marked approval goes, so the base check was moot rather than skipped.
        rc, dismissed = self.run_dismiss(NEW, NEW, [self.approval(1, NEW, base="main")],
                                         live_base="", all_approvals=True)
        self.assertEqual((rc, len(dismissed)), (0, 1))
        warned = " ".join(self.warnings())
        self.assertIn("--all-approvals", warned)
        self.assertNotIn("comparing on head alone", warned)

    def test_the_missing_base_warning_names_what_skipping_the_check_costs(self):
        # The guard fails OPEN — an off-base approval survives — so the annotation
        # has to say so rather than imply the run was complete.
        self.run_dismiss(NEW, NEW, [self.approval(1, NEW, base="main")], live_base="")
        self.assertTrue(any("SURVIVES" in line for line in self.warnings()), self.printed)

    def test_json_that_is_not_an_object_degrades_instead_of_tracebacking(self):
        # `null`, or the ARRAY the collection endpoint returns for an empty number:
        # `.get` on either raises AttributeError, which nothing catches, so it
        # would escape as a traceback and bypass the whole announced-degradation
        # path — including the shapeless fallback that exists for exactly this.
        for body in ("null", "[]", '"a string"'):
            with self.subTest(body=body):
                rc, dismissed = self.run_dismiss(None, NEW, [self.approval(1, NEW)], raw=body)
                self.assertEqual((rc, dismissed), (0, []))
                self.assertTrue(any("live PR head" in line for line in self.warnings()), self.printed)

    def test_a_shapeless_head_read_never_dismisses_everything(self):
        # read_head returning "" must not reach the filter: "" is not None, so it
        # would match no recorded SHA and withdraw every marked approval on the PR.
        self.assertEqual(self.run_dismiss(None, NEW, [self.approval(1, NEW)]), (0, []))

    def test_a_read_with_a_head_but_no_base_skips_the_base_check(self):
        # "" is not None: it would mismatch every recorded base and dismiss an
        # approval that is current on both head and base.
        approval = {"id": 1, "user": {"login": "cursor-approver"}, "state": "APPROVED",
                    "body": AA.render_body(AA.APPROVE, ["ok"], "low", [], NEW, "main")}
        self.assertEqual(self.run_dismiss(NEW, NEW, [approval]), (0, []))


class PostWriteRaceTest(unittest.TestCase):
    """A push between the head read and the POST must not leave our review standing."""

    def run_decide(self, heads, judge_status="ok", reviews=(), threads=lambda *a: [], diff=DIFF,
                   post_response='{"id": 99}'):
        calls = []
        heads = iter(heads)

        def fake_gh(args, payload=None):
            calls.append((args, payload))
            if args[:2] == ["api", "-X"] and args[2] == "POST":
                return post_response
            if args[:2] == ["api", "-X"] and args[2] == "PUT":
                return "{}"
            if args[:2] == ["api", "graphql"]:
                return graphql_reviews(list(reviews))
            head = next(heads)
            if isinstance(head, Exception):
                raise head
            head, base = head if isinstance(head, tuple) else (head, "main")
            return json.dumps({"head": {"sha": head}, "base": {"ref": base}})

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"findings": [], "panel": PANEL_OK}, f)
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as p:
            p.write(diff)
        args = argparse.Namespace(threshold="medium", findings=f.name, repo="o/r", pr_number="1",
                                  commit_sha=SHA, judge_status=judge_status, delivered="true",
                                  ungated="0", approver_login="cursor-approver", reviewed_diff=p.name,
                                  base_ref="main")
        with mock.patch.object(AA, "gh", fake_gh), \
                mock.patch.object(AA, "open_thread_severities", threads), \
                mock.patch.object(AA, "emit", lambda *a: None):
            rc = AA.cmd_decide(args)
        os.unlink(f.name)
        os.unlink(p.name)
        return rc, [c[0][2] for c in calls if c[0][:2] == ["api", "-X"]]

    def test_untrusted_round_withdraws_an_earlier_approval(self):
        # Round 1 approved this very head; a degraded re-run must not leave it counting.
        approval = {"id": 7, "user": {"login": "cursor-approver"}, "state": "APPROVED",
                    "commit_id": SHA, "body": AA.APPROVE_MARKER}
        rc, writes = self.run_decide([SHA], judge_status="error", reviews=[approval])
        self.assertEqual((rc, writes), (0, ["PUT"]))

    def test_excluded_only_pr_posts_nothing_and_withdraws(self):
        # Round 1 approved real content; a later round over an all-excluded diff
        # must neither approve nor leave that approval counting.
        approval = {"id": 7, "user": {"login": "cursor-approver"}, "state": "APPROVED",
                    "commit_id": SHA, "body": AA.APPROVE_MARKER}
        rc, writes = self.run_decide([SHA], reviews=[approval], diff="")
        self.assertEqual((rc, writes), (0, ["PUT"]))

    def test_unreadable_threads_fail_closed(self):
        def boom(*a):
            raise SystemExit(2)  # gate-unresolved's run_graphql on a query failure
        rc, writes = self.run_decide([SHA], threads=boom)
        self.assertEqual((rc, writes), (0, []))

    def test_unreadable_head_after_post_withdraws(self):
        rc, writes = self.run_decide([SHA, RuntimeError("timeout")])
        self.assertEqual((rc, writes), (0, ["POST", "PUT"]))

    def test_a_post_response_without_a_review_id_withdraws_and_goes_red(self):
        # `gh` exited 0, so the APPROVE may well have landed — but with no id the
        # race check cannot dismiss it. Each shape must reach the ambiguous-POST
        # path (withdraw by listing live reviews, exit 1), not escape as a
        # traceback after approve_gate already read `pass`.
        approval = {"id": 7, "user": {"login": "cursor-approver"}, "state": "APPROVED",
                    "body": AA.APPROVE_MARKER}
        for response in ("not json", "null", "[]", '{"node_id": "x"}', '{"id": null}'):
            with self.subTest(response=response):
                rc, writes = self.run_decide([SHA], reviews=[approval], post_response=response)
                self.assertEqual((rc, writes), (1, ["POST", "PUT"]))

    def test_head_unchanged_keeps_the_approval(self):
        rc, writes = self.run_decide([SHA, SHA])
        self.assertEqual((rc, writes), (0, ["POST"]))

    def test_head_moved_during_post_withdraws_it(self):
        rc, writes = self.run_decide([SHA, "b" * 40])
        self.assertEqual((rc, writes), (0, ["POST", "PUT"]))

    def test_base_retargeted_during_post_withdraws_it(self):
        rc, writes = self.run_decide([SHA, (SHA, "release")])
        self.assertEqual((rc, writes), (0, ["POST", "PUT"]))

    def test_base_retargeted_before_decide_posts_nothing(self):
        rc, writes = self.run_decide([(SHA, "release")])
        self.assertEqual((rc, writes), (0, []))

    def test_approval_records_the_reviewed_base(self):
        body = AA.render_body(AA.APPROVE, ["ok"], "medium", [], SHA, "main")
        self.assertIn(AA.APPROVE_MARKER, body)
        self.assertEqual(AA.recorded_base(body), "main")


class GhTest(unittest.TestCase):
    def test_a_wedged_call_is_a_runtime_error(self):
        import subprocess
        with mock.patch.object(AA.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 60)):
            with self.assertRaises(RuntimeError):
                AA.gh(["api", "x"])


class RenderBodyTest(unittest.TestCase):
    def test_model_values_cannot_inject_markdown(self):
        bad = {"file": "a`b\n## forged @someone", "line": "1\n# x", "severity": "**pwn** " * 50}
        body = AA.render_body(AA.REQUEST_CHANGES, ["1 finding"], "medium", [bad])
        self.assertIn("**unknown**", body)
        self.assertNotIn("\n## forged", body)
        self.assertNotIn("@someone", body)
        self.assertNotIn("pwn", body)
        self.assertIn(":?", body)


def read_outputs(path):
    """$GITHUB_OUTPUT as a dict; a later write of a key wins, as in Actions."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            key, _, value = line.rstrip("\n").partition("=")
            out[key] = value
    return out


class ApproveGateTest(unittest.TestCase):
    """decide_gate() names every approve_gate value the workflow can emit."""

    def gate(self, **kw):
        human = kw.pop("human_review", False)
        base = dict(threshold="medium", findings=[], panel=PANEL_OK, judge="ok", delivered=True,
                    reviewed=SHA, live=SHA, threads=[], ungated=0)
        base.update(kw)
        return AA.decide_gate(base["threshold"], list(base["findings"]), list(base["panel"]), base["judge"],
                              base["delivered"], base["reviewed"], base["live"], list(base["threads"]),
                              base["ungated"], human)

    def test_pass_is_approve(self):
        event, gate, _, _ = self.gate(findings=[finding("low")])
        self.assertEqual((event, gate), (AA.APPROVE, AA.GATE_PASS))

    def test_fail_on_request_changes(self):
        event, gate, _, _ = self.gate(findings=[finding("high")])
        self.assertEqual((event, gate), (AA.REQUEST_CHANGES, AA.GATE_FAIL))

    def test_fail_on_an_open_blocking_thread(self):
        event, gate, _, _ = self.gate(threads=["high"])
        self.assertEqual((event, gate), (AA.NONE, AA.GATE_FAIL))

    def test_untrusted_on_every_trust_failure(self):
        for kw in (dict(judge="degraded"), dict(panel=[{"status": "error"}]), dict(delivered=False),
                   dict(ungated=1), dict(live="b" * 40)):
            event, gate, _, _ = self.gate(findings=[finding("high")], **kw)
            self.assertEqual((event, gate), (AA.NONE, AA.GATE_UNTRUSTED), kw)

    def test_capped_when_the_pr_carries_needs_human_review(self):
        # Even a clean, trusted round: a PR handed to a human is never approved.
        event, gate, reasons, _ = self.gate(human_review=True)
        self.assertEqual((event, gate), (AA.NONE, AA.GATE_CAPPED))
        self.assertIn(AA.HUMAN_REVIEW_LABEL, reasons[0])

    def test_decide_keeps_its_three_tuple(self):
        self.assertEqual(len(decide()), 3)

    def test_the_five_values(self):
        self.assertEqual(set(AA.APPROVE_GATE_VALUES), {"pass", "fail", "untrusted", "capped", "off"})

    def test_workflow_output_expression_covers_capped_and_off(self):
        # `off` and the no-round `untrusted` are decided in the workflow-level
        # output expression, not in Python; pin that expression here.
        wf = os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml")
        with open(wf, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(
            "value: ${{ (jobs.round-cap.outputs.capped == 'true' || jobs.round-cap.outputs.labelled == 'true') "
            "&& 'capped' || "
            "inputs.approve_max_severity == '' && 'off' || jobs.post-review.outputs.approve_gate || 'untrusted' }}",
            text,
        )
        self.assertIn("approve_gate: ${{ steps.approve.outputs.approve_gate }}", text)
        self.assertIn("needs.round-cap.outputs.capped != 'true'", text)

    def test_workflow_round_and_max_rounds_outputs(self):
        # `round` is the last round when capped, the delivered round's number
        # otherwise (a delivered review is exactly what round_reviews counts),
        # and empty when no round landed; `max_rounds` is the applied cap.
        wf = os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml")
        with open(wf, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(
            "value: ${{ jobs.round-cap.outputs.capped == 'true' && jobs.round-cap.outputs.rounds || "
            "jobs.post-review.outputs.delivered == 'true' && jobs.round-cap.outputs.next_round || '' }}",
            text,
        )
        self.assertIn(
            "value: ${{ jobs.round-cap.outputs.max_rounds || (inputs.max_rounds > 0 && inputs.max_rounds) || 0 }}",
            text,
        )
        self.assertIn("next_round: ${{ steps.cap.outputs.next_round }}", text)
        self.assertIn("max_rounds: ${{ steps.cap.outputs.max_rounds }}", text)
        self.assertIn("delivered: ${{ steps.post.outputs.delivered }}", text)


class CmdDecideGateOutputTest(unittest.TestCase):
    """cmd_decide writes approve_gate on every path, including the I/O ones."""

    def run_decide(self, findings=(), labels=(), heads=(SHA, SHA), threshold="medium", post_error=None,
                   labels_after=None, reviews=(), put_error=None, panel=PANEL_OK, max_failed=None):
        heads = iter(heads)
        reads = []
        writes = []
        self.posted = []

        def fake_gh(args, payload=None):
            if args[:3] == ["api", "-X", "POST"]:
                if post_error:
                    raise RuntimeError(post_error)
                self.posted.append(payload)
                return json.dumps({"id": 99})
            if args[:3] == ["api", "-X", "PUT"]:
                if put_error:
                    raise RuntimeError(put_error)
                writes.append(args[3])
                return "{}"
            if args[:2] == ["api", "graphql"]:
                return graphql_reviews(list(reviews))
            names = labels_after if reads and labels_after is not None else labels
            reads.append(args)
            return json.dumps({"head": {"sha": next(heads)}, "base": {"ref": "main"},
                               "labels": [{"name": n} for n in names]})

        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, "c.json")
            dpath = os.path.join(d, "pr.patch")
            out = os.path.join(d, "out")
            open(out, "w").close()
            with open(fpath, "w") as f:
                json.dump({"findings": list(findings), "panel": list(panel)}, f)
            with open(dpath, "w") as f:
                f.write(DIFF)
            args = argparse.Namespace(threshold=threshold, findings=fpath, repo="o/r", pr_number="1",
                                      commit_sha=SHA, judge_status="ok", delivered="true",
                                      ungated="0", approver_login="cursor-approver",
                                      reviewed_diff=dpath, base_ref="main")
            if max_failed is not None:
                args.max_failed_reviewers = max_failed
            self.printed = []
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out}), \
                    mock.patch.object(AA, "gh", fake_gh), \
                    mock.patch.object(AA, "open_thread_severities", lambda *a: []), \
                    mock.patch.object(AA, "emit", lambda *a: None), \
                    mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
                rc = AA.cmd_decide(args)
            self.writes = writes
            return rc, read_outputs(out).get("approve_gate")

    def test_label_applied_during_the_post_withdraws_the_approval(self):
        self.assertEqual(self.run_decide(labels_after=["needs-human-review"]), (0, "capped"))
        self.assertEqual(self.writes, ["repos/o/r/pulls/1/reviews/99/dismissals"])

    def test_label_applied_during_a_request_changes_leaves_it(self):
        rc, gate = self.run_decide(findings=[finding("critical")], labels_after=["needs-human-review"])
        self.assertEqual((rc, gate, self.writes), (0, "fail", []))

    def test_pass(self):
        self.assertEqual(self.run_decide(), (0, "pass"))

    def test_max_failed_reviewers_reaches_the_gate(self):
        panel = panel_with(("m1", "edge-case"))
        self.assertEqual(self.run_decide(panel=panel), (0, "untrusted"))
        self.assertEqual(self.posted, [])
        self.assertEqual(self.run_decide(panel=panel, max_failed="1"), (0, "pass"))
        self.assertEqual([p["event"] for p in self.posted], [AA.APPROVE])
        self.assertIn("approved with 1/6 reviewers errored: m1:edge-case", self.posted[0]["body"])

    def test_an_invalid_max_failed_reviewers_fails_closed(self):
        for value in ("-1", "x", "1.5"):
            with self.subTest(value=value):
                self.assertEqual(self.run_decide(panel=panel_with(("m1", "edge-case")), max_failed=value),
                                 (2, "untrusted"))
                self.assertEqual(self.posted, [])

    def test_an_invalid_max_failed_reviewers_withdraws_an_earlier_approval(self):
        earlier = {"id": 7, "user": {"login": "cursor-approver"}, "state": "APPROVED",
                   "body": AA.render_body(AA.APPROVE, ["ok"], "low", [], SHA, "main")}
        self.assertEqual(self.run_decide(max_failed="x", reviews=[earlier]), (2, "untrusted"))
        self.assertEqual(self.writes, ["repos/o/r/pulls/1/reviews/7/dismissals"])

    def test_fail(self):
        self.assertEqual(self.run_decide(findings=[finding("critical")]), (0, "fail"))

    def test_needs_human_review_label_never_approves(self):
        rc, gate = self.run_decide(labels=["needs-human-review"])
        self.assertEqual((rc, gate), (0, "capped"))

    def test_head_moved_during_post_is_untrusted(self):
        self.assertEqual(self.run_decide(heads=(SHA, "b" * 40)), (0, "untrusted"))

    def test_a_failed_post_is_untrusted_and_withdraws_an_earlier_approval(self):
        # A REQUEST_CHANGES that never landed must not leave the last round's
        # approval standing (dismiss-stale keeps it while head and base match),
        # nor a `fail` gate over a verdict nobody can see.
        earlier = {"id": 7, "user": {"login": "cursor-approver"}, "state": "APPROVED",
                   "body": AA.render_body(AA.APPROVE, ["ok"], "low", [], SHA, "main")}
        rc, gate = self.run_decide(findings=[finding("critical")], post_error="HTTP 502", reviews=[earlier])
        self.assertEqual((rc, gate), (1, "untrusted"))
        self.assertEqual(self.writes, ["repos/o/r/pulls/1/reviews/7/dismissals"])

    def errors(self):
        return [line for line in self.printed if "::error::" in line]

    def test_a_multiline_post_error_stays_one_annotation(self):
        # `gh` stderr is several lines; raw, the tail falls out of the annotation
        # and a continuation line starting with `::` is re-parsed as a command.
        self.run_decide(findings=[finding("critical")], post_error="HTTP 502\n::error::injected\nsee docs")
        self.assertEqual(len(self.errors()), 1, self.printed)
        self.assertIn("HTTP 502 ::error::injected see docs", self.errors()[0])

    def test_a_failed_withdrawal_keeps_its_permission_hint_in_the_annotation(self):
        # The hint is appended AFTER the cause, so a multi-line cause would push
        # the remediation out of the annotation entirely.
        rc, _ = self.run_decide(heads=(SHA, "b" * 40), put_error="HTTP 403\nsee docs")
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.errors()), 1, self.printed)
        self.assertNotIn("\n", self.errors()[0])
        self.assertIn(AA.DISMISS_PERMISSION_HINT, self.errors()[0])

    def test_a_refused_self_approval_keeps_its_gate(self):
        # Not a broken round: the verdict stands, only the approver is the author.
        self.assertEqual(self.run_decide(post_error="Can not approve your own pull request"), (0, "pass"))

    def test_invalid_threshold_still_leaves_a_value(self):
        self.assertEqual(self.run_decide(threshold="high"), (2, "untrusted"))


MARKER = AA._load_post_review().CONSOLIDATED_MARKER
BOT = "cursor-bot[bot]"


def review(rid, at, login=BOT, body=MARKER + "\n…", state="COMMENTED"):
    return {"id": rid, "user": {"login": login}, "body": body, "submitted_at": at, "state": state,
            "html_url": f"https://github.com/o/r/pull/1#pullrequestreview-{rid}"}


def unlabeled(at, name="needs-human-review"):
    return {"event": "unlabeled", "label": {"name": name}, "created_at": at}


class RoundCounterTest(unittest.TestCase):
    def test_counts_only_the_posters_marked_reviews(self):
        reviews = [
            review(1, "2026-01-01T00:00:00Z"),
            review(2, "2026-01-02T00:00:00Z", state="DISMISSED"),  # a spent round still counts
            review(3, "2026-01-03T00:00:00Z", login="mallory"),  # marker, wrong author
            review(4, "2026-01-04T00:00:00Z", body="LGTM"),  # author, no marker
            review(5, "2026-01-05T00:00:00Z", body="x " + MARKER),  # marker not at the start
            review(6, "2026-01-06T00:00:00Z", login="Cursor-Bot[bot]"),  # login is case-insensitive
        ]
        self.assertEqual([r["id"] for r in AA.round_reviews(reviews, BOT, None, MARKER)], [1, 2, 6])

    def test_a_non_round_banner_is_recognised_in_a_crlf_stored_body(self):
        # GitHub stores bodies with CRLF line endings; the banners carry `\n`
        # anchors, so an unnormalised match would count a round that reviewed
        # nothing toward max_rounds.
        failed = review(1, "2026-01-01T00:00:00Z",
                        body=(MARKER + AA.NON_ROUND_BANNERS[0] + "details").replace("\n", "\r\n"))
        empty = review(2, "2026-01-02T00:00:00Z",
                       body=(MARKER + AA.NON_ROUND_BANNERS[1] + "details").replace("\n", "\r\n"))
        real = review(3, "2026-01-03T00:00:00Z", body=(MARKER + "\nfindings").replace("\n", "\r\n"))
        self.assertEqual([r["id"] for r in AA.round_reviews([failed, empty, real], BOT, None, MARKER)], [3])

    def test_unlabel_resets_the_count(self):
        reviews = [review(i, f"2026-01-0{i}T00:00:00Z") for i in range(1, 6)]
        timeline = [
            {"event": "labeled", "label": {"name": "needs-human-review"}, "created_at": "2026-01-01T12:00:00Z"},
            unlabeled("2026-01-02T12:00:00Z"),
            unlabeled("2026-01-03T12:00:00Z"),  # the most recent removal wins
            unlabeled("2026-01-04T18:00:00Z", name="cursor-review"),  # another label: ignored
        ]
        since = AA.last_unlabeled_at(timeline, AA.HUMAN_REVIEW_LABEL)
        self.assertEqual(since, "2026-01-03T12:00:00Z")
        self.assertEqual([r["id"] for r in AA.round_reviews(reviews, BOT, since, MARKER)], [4, 5])

    def test_no_unlabel_event_means_no_reset(self):
        self.assertIsNone(AA.last_unlabeled_at([{"event": "labeled", "label": {"name": "needs-human-review"},
                                                 "created_at": "2026-01-01T00:00:00Z"}], AA.HUMAN_REVIEW_LABEL))

    def test_cap_comment_is_once_per_cap(self):
        mine = {"user": {"login": BOT}, "body": AA.ROUND_CAP_MARKER + "\n…", "created_at": "2026-01-02T00:00:00Z"}
        forged = dict(mine, user={"login": "mallory"})
        self.assertTrue(AA.cap_comment_posted([mine], BOT, None))
        self.assertFalse(AA.cap_comment_posted([forged], BOT, None))
        # A comment from before the last reset belongs to the previous cap.
        self.assertFalse(AA.cap_comment_posted([mine], BOT, "2026-01-03T00:00:00Z"))


def inline(cid, sev, path="a.py", line=3):
    return {"id": cid, "path": path, "line": line, "body": f"🟠 **{sev}** — body"}


class CapFindingsTest(unittest.TestCase):
    def test_lists_open_findings_above_threshold(self):
        comments = [inline(1, "High"), inline(2, "Low"), inline(3, "Critical"), inline(4, "Medium"),
                    {"id": 5, "path": "b.py", "line": 1, "body": "no badge"}]
        got = AA.cap_findings(comments, {"1", "2", "4", "5"}, "low")
        self.assertEqual(got, [("high", "a.py", 3), ("medium", "a.py", 3), ("unknown", "b.py", 1)])

    def test_unreadable_threads_filter_nothing_and_empty_threshold_lists_all(self):
        got = AA.cap_findings([inline(1, "Nit"), inline(2, "High")], None, "")
        self.assertEqual([g[0] for g in got], ["nit", "high"])

    def test_comment_neutralises_model_paths(self):
        body = AA.render_cap_comment(5, 5, [("high", "a`b\n## forged @someone", 3)], "medium", "javascript:x")
        self.assertTrue(body.startswith(AA.ROUND_CAP_MARKER))
        self.assertNotIn("\n## forged", body)
        self.assertNotIn("@someone", body)
        self.assertNotIn("javascript:", body)


class RoundCapCommandTest(unittest.TestCase):
    """The cap path end to end, with gh stubbed by endpoint."""

    def run_cap(self, reviews, max_rounds="5", timeline=(), comments=(), label_exists=True, fail=(), labels=(),
                probe_error="HTTP 404"):
        writes = []

        def fake_gh(args, payload=None):
            graphql = args[:2] == ["api", "graphql"]
            # The reviews list is a GraphQL read; key it by its REST path so a
            # `fail` token such as "/reviews" still reaches it.
            path = "repos/o/r/pulls/1/reviews?per_page=100" if graphql else next(
                a for a in args if a.startswith("repos/"))
            method = args[2] if args[:2] == ["api", "-X"] else "GET"
            for f in fail:
                # A trailing `$` anchors the match to the end of the path.
                if (path.endswith(f[:-1]) if f.endswith("$") else f in path):
                    raise RuntimeError(f"boom {path}")
            if method != "GET":
                writes.append((method, path, payload))
                return "{}"
            if "/timeline" in path:
                return json.dumps([list(timeline)])
            if path.endswith("/reviews?per_page=100"):
                return graphql_reviews(list(reviews))
            if "/issues/1/comments" in path:
                return json.dumps([list(comments)])
            if "/reviews/" in path and path.endswith("/comments?per_page=100"):
                return json.dumps([[inline(11, "High"), inline(12, "Nit")]])
            if path == "repos/o/r/labels/needs-human-review":
                if not label_exists:
                    raise RuntimeError(probe_error)
                return "{}"
            if path == "repos/o/r/pulls/1":
                return json.dumps({"labels": [{"name": n} for n in labels]})
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            open(out, "w").close()
            args = argparse.Namespace(repo="o/r", pr_number="1", max_rounds=max_rounds, threshold="medium",
                                      poster_login=BOT)
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out}), \
                    mock.patch.object(AA, "gh", fake_gh), \
                    mock.patch.object(AA, "open_thread_ids", lambda *a: {"11", "12"}), \
                    mock.patch.object(AA, "emit", lambda *a: None):
                rc = AA.cmd_round_cap(args)
            return rc, read_outputs(out), writes

    FIVE = [review(i, f"2026-01-0{i}T00:00:00Z") for i in range(1, 6)]

    def test_under_the_cap_runs_the_panel_and_writes_nothing(self):
        rc, out, writes = self.run_cap(self.FIVE[:4])
        self.assertEqual((rc, out["capped"], out["rounds"], writes), (0, "false", "4", []))

    def test_at_the_cap_labels_and_comments_once(self):
        rc, out, writes = self.run_cap(self.FIVE, label_exists=False)
        self.assertEqual((rc, out["capped"]), (0, "true"))
        self.assertEqual([(m, p) for m, p, _ in writes], [
            ("POST", "repos/o/r/labels"),
            ("POST", "repos/o/r/issues/1/labels"),
            ("POST", "repos/o/r/issues/1/comments"),
        ])
        self.assertEqual(writes[1][2], {"labels": ["needs-human-review"]})
        body = writes[2][2]["body"]
        self.assertIn(AA.ROUND_CAP_MARKER, body)
        self.assertIn("**high**", body)
        self.assertNotIn("**nit**", body)  # at/below the threshold: not listed
        self.assertIn("pullrequestreview-5", body)  # the LATEST round
        # Nothing here approves: the only writes are the label and the comment.
        self.assertFalse(any("/reviews" in p for _, p, _ in writes))

    def test_existing_label_is_not_recreated(self):
        _, _, writes = self.run_cap(self.FIVE)
        self.assertNotIn(("POST", "repos/o/r/labels"), [(m, p) for m, p, _ in writes])

    def test_a_second_trigger_does_not_repost_the_comment(self):
        prior = {"user": {"login": BOT}, "body": AA.ROUND_CAP_MARKER, "created_at": "2026-01-06T00:00:00Z"}
        rc, out, writes = self.run_cap(self.FIVE, comments=[prior])
        self.assertEqual((rc, out["capped"]), (0, "true"))
        self.assertEqual([p for _, p, _ in writes], ["repos/o/r/issues/1/labels"])

    def test_unlabel_resets_the_cap(self):
        rc, out, writes = self.run_cap(self.FIVE, timeline=[unlabeled("2026-01-03T12:00:00Z")])
        self.assertEqual((rc, out["capped"], out["rounds"], writes), (0, "false", "2", []))

    def test_other_authors_marked_reviews_do_not_cap(self):
        forged = [review(i, f"2026-01-0{i}T00:00:00Z", login="mallory") for i in range(1, 9)]
        _, out, _ = self.run_cap(forged)
        self.assertEqual(out["capped"], "false")

    def test_zero_disables_without_any_read(self):
        rc, out, writes = self.run_cap(self.FIVE, max_rounds="0", fail=("repos/",))
        self.assertEqual((rc, out["capped"], writes), (0, "false", []))

    def test_unreadable_reviews_fail_open(self):
        rc, out, writes = self.run_cap(self.FIVE, fail=("/reviews",))
        self.assertEqual((rc, out["capped"], writes), (0, "false", []))

    def test_a_failed_label_write_fails_open(self):
        # Capped-but-unlabelled would be permanent (no label to remove = no
        # reset), so a label that cannot be applied runs the panel instead.
        rc, out, writes = self.run_cap(self.FIVE, fail=("issues/1/labels",))
        self.assertEqual((rc, out["capped"], out["labelled"]), (0, "false", "false"))
        self.assertFalse(any("/comments" in p for _, p, _ in writes))

    def test_a_failed_comment_stays_capped_and_goes_red(self):
        rc, out, writes = self.run_cap(self.FIVE, fail=("issues/1/comments",))
        self.assertEqual((rc, out["capped"], out["labelled"]), (1, "true", "true"))
        self.assertIn(("POST", "repos/o/r/issues/1/labels"), [(m, p) for m, p, _ in writes])

    def test_a_non_404_probe_does_not_create_but_still_applies(self):
        rc, out, writes = self.run_cap(self.FIVE, label_exists=False, probe_error="HTTP 403: Forbidden")
        self.assertEqual((rc, out["capped"]), (0, "true"))
        paths = [p for _, p, _ in writes]
        self.assertNotIn("repos/o/r/labels", paths)
        self.assertIn("repos/o/r/issues/1/labels", paths)

    def test_a_failed_create_still_applies(self):
        # A pull-requests-only token 403s the repo-side create (that needs
        # issues: write), and a concurrent run can 422 it; the add still runs.
        rc, out, writes = self.run_cap(self.FIVE, label_exists=False, fail=("repos/o/r/labels$",))
        self.assertEqual((rc, out["capped"]), (0, "true"))
        self.assertIn("repos/o/r/issues/1/labels", [p for _, p, _ in writes])

    def test_labelled_pr_under_the_cap_reports_labelled(self):
        rc, out, writes = self.run_cap(self.FIVE[:2], labels=["Needs-Human-Review"])
        self.assertEqual((rc, out["capped"], out["labelled"], writes), (0, "false", "true", []))

    def test_unreadable_labels_do_not_stop_the_count(self):
        rc, out, _ = self.run_cap(self.FIVE, fail=("repos/o/r/pulls/1$",))
        self.assertEqual((rc, out["capped"], out["labelled"]), (0, "true", "true"))

    def test_rounds_that_reviewed_nothing_do_not_count(self):
        failed = [review(i, f"2026-01-0{i}T00:00:00Z", body=MARKER + "\n\n⚠️ **Review failed**\n\n```x```")
                  for i in range(1, 4)]
        empty = [review(i, f"2026-01-0{i}T00:00:00Z",
                        body=MARKER + "\n\n⚠️ **Panel did not produce any findings.**\n\nEvery reviewer…")
                 for i in range(4, 7)]
        rc, out, writes = self.run_cap(failed + empty + self.FIVE[:2])
        self.assertEqual((rc, out["capped"], out["rounds"], writes), (0, "false", "2", []))

    def test_non_integral_or_non_finite_max_rounds_fails_open(self):
        for bad in ("2.9", "0.5", "inf", "nan", "-inf", "five"):
            with self.subTest(bad=bad):
                rc, out, writes = self.run_cap(self.FIVE, max_rounds=bad)
                self.assertEqual((rc, out["capped"], writes), (0, "false", []))

    def test_integral_float_max_rounds_is_accepted(self):
        _, out, _ = self.run_cap(self.FIVE, max_rounds="5.0")
        self.assertEqual(out["capped"], "true")

    def test_round_outputs_under_the_cap(self):
        # `next_round` is the number this run's round takes; `max_rounds` the
        # effective cap. Together they feed the workflow's `round` / `max_rounds`.
        _, out, _ = self.run_cap(self.FIVE[:2], max_rounds="5.0")
        self.assertEqual((out["rounds"], out["next_round"], out["max_rounds"]), ("2", "3", "5"))

    def test_round_outputs_at_the_cap(self):
        # Capped: the workflow reports `rounds` (the last round), not next_round.
        _, out, _ = self.run_cap(self.FIVE)
        self.assertEqual((out["capped"], out["rounds"], out["max_rounds"]), ("true", "5", "5"))

    def test_round_outputs_after_a_reset_count_from_the_removal(self):
        _, out, _ = self.run_cap(self.FIVE, timeline=[unlabeled("2026-01-03T12:00:00Z")])
        self.assertEqual((out["rounds"], out["next_round"]), ("2", "3"))

    def test_no_cap_reports_zero_and_no_round(self):
        for value in ("0", "", "five", "2.5"):
            with self.subTest(value=value):
                _, out, _ = self.run_cap(self.FIVE, max_rounds=value)
                self.assertEqual(out["max_rounds"], "0")
                self.assertNotIn("next_round", out)

    def test_unreadable_count_leaves_the_round_empty(self):
        # Fail-open: the cap is still configured, but no round number is guessed.
        _, out, _ = self.run_cap(self.FIVE, fail=("/reviews",))
        self.assertEqual(out["max_rounds"], "5")
        self.assertNotIn("next_round", out)
        self.assertNotIn("rounds", out)


class NonRoundBannerParityTest(unittest.TestCase):
    """NON_ROUND_BANNERS must match what post-review.py actually renders."""

    def test_each_banner_is_in_post_review(self):
        path = os.path.join(os.path.dirname(__file__), "..", "post-review.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for banner in AA.NON_ROUND_BANNERS:
            with self.subTest(banner=banner):
                # post-review writes them as `\n\n<banner>\n\n` inside f-strings.
                self.assertIn(banner.strip("\n").replace("\n", "\\n"), src)
                self.assertIn("\\n\\n" + banner.strip("\n") + "\\n\\n", src)


POSTER = "cloud-code-bot[bot]"
GATE = AA._load_gate_unresolved()


def bot(login=POSTER):
    """A GraphQL author: a Bot's login comes back WITHOUT the `[bot]` suffix."""
    if login.endswith("[bot]"):
        return {"__typename": "Bot", "login": login[: -len("[bot]")]}
    return {"__typename": "User", "login": login}


def thread(tid, sev="low", author=POSTER, repliers=(), resolved=False, outdated=False,
           marker=True, total=None):
    badge = f"🔵 **{sev.capitalize()}** — x" if sev else "no badge here"
    review_body = GATE.CONSOLIDATED_MARKER + "\n…" if marker else "a human review"
    authors = [bot(author)] + [bot(r) for r in repliers]
    return {
        "id": tid, "isResolved": resolved, "isOutdated": outdated,
        "comments": {"nodes": [{"author": authors[0], "body": badge,
                                "pullRequestReview": {"body": review_body}}]},
        "participants": {"totalCount": len(authors) if total is None else total,
                         "nodes": [{"author": a} for a in authors]},
    }


class AutoResolvePlanTest(unittest.TestCase):
    """plan_thread_resolution: which threads an approval may resolve."""

    def plan(self, threads, threshold="low", poster=POSTER, honour_non_gating=False):
        todo, counts = AA.plan_thread_resolution(threads, poster, threshold, honour_non_gating)
        return [t["id"] for t, _ in todo], counts

    def test_approvable_state_resolves_only_eligible_bot_threads(self):
        ids, counts = self.plan([
            thread("ok-low", "low"), thread("ok-nit", "nit"),
            thread("human-started", "low", author="alice"),
            thread("human-replied", "low", repliers=["alice"]),
            thread("other-bot-replied", "low", repliers=["dependabot[bot]"]),
            thread("wrong-author", "low", author="github-actions[bot]"),
            thread("already-resolved", "low", resolved=True),
            thread("not-consolidated", "low", marker=False),
            thread("outdated-low", "low", outdated=True),
        ])
        self.assertEqual(ids, ["ok-low", "ok-nit", "outdated-low"])
        self.assertEqual(counts, {"resolved": 3, "skipped-human": 2, "skipped-unbadged": 0,
                                  "skipped-above-threshold": 0, "skipped-non-gating": 0})

    def test_bot_own_replies_do_not_disqualify(self):
        ids, _ = self.plan([thread("t", "low", repliers=[POSTER])])
        self.assertEqual(ids, ["t"])

    def test_a_user_sharing_the_bot_name_is_not_the_bot(self):
        # GraphQL drops `[bot]`; a User literally named `cloud-code-bot` must not match.
        t = thread("t", "low")
        t["comments"]["nodes"][0]["author"] = {"__typename": "User", "login": "cloud-code-bot"}
        self.assertEqual(self.plan([t])[0], [])

    def test_any_live_thread_above_threshold_resolves_nothing(self):
        for blocker in (thread("hi", "high"), thread("med", "medium"), thread("bare", None),
                        thread("human-hi", "high", author="alice")):
            with self.subTest(blocker=blocker["id"]):
                ids, counts = self.plan([thread("ok", "low"), thread("ok2", "nit"), blocker])
                self.assertEqual(ids, [])
                self.assertEqual(counts["resolved"], 0)

    def test_resolved_or_outdated_blocker_does_not_block(self):
        # decide() ignores those too, so the PR is approvable.
        ids, counts = self.plan([thread("ok", "low"), thread("hi", "high", resolved=True)])
        self.assertEqual(ids, ["ok"])
        ids, counts = self.plan([thread("ok", "low"), thread("hi", "high", outdated=True)])
        self.assertEqual((ids, counts["skipped-above-threshold"]), (["ok"], 1))

    def test_threshold_is_respected(self):
        ids, counts = self.plan([thread("low", "low"), thread("nit", "nit")], threshold="nit")
        # `low` is above a `nit` threshold — the PR is not approvable, so nothing.
        self.assertEqual(ids, [])

    def test_unbadged_bot_thread_is_never_resolved(self):
        # Outdated, so it does not block — but it is still not resolved.
        ids, counts = self.plan([thread("bare", None, outdated=True), thread("ok", "low")])
        self.assertEqual((ids, counts["skipped-unbadged"]), (["ok"], 1))

    def test_unseen_comments_leave_the_thread(self):
        self.assertEqual(self.plan([thread("t", "low", total=101)])[0], [])

    def test_deleted_author_leaves_the_thread(self):
        t = thread("t", "low", repliers=["alice"])
        t["participants"]["nodes"][1]["author"] = None
        self.assertEqual(self.plan([t])[0], [])

    def test_no_poster_resolves_nothing(self):
        self.assertEqual(self.plan([thread("t", "low")], poster="")[0], [])


def graphql_fake(log, threads, fail_on=(), live=None):
    """A `gh api graphql` stand-in for the resolver's three calls. The re-read
    answers from `live[tid]` when given (the thread as it is NOW), else from the
    planned snapshot. Each call is logged as (kind, thread id, body)."""
    by_id = {t["id"]: t for t in threads}
    live = live or {}

    def fake(args):
        query = args[3]
        tid = next(a.split("=", 1)[1] for a in args if a.startswith("threadId="))
        if "addPullRequestReviewThreadReply" in query:
            kind = "reply"
        elif "resolveReviewThread" in query:
            kind = "resolve"
        else:
            kind = "recheck"
        body = next((a.split("=", 1)[1] for a in args if a.startswith("body=")), None)
        log.append((kind, tid, body))
        if (kind, tid) in fail_on:
            raise RuntimeError("HTTP 502")
        if (kind, tid, "errors") in fail_on:
            return json.dumps({"errors": [{"message": "nope"}]})
        if (kind, tid, "null") in fail_on:
            return "null"
        if kind == "recheck":
            now = live.get(tid, by_id.get(tid, {}))
            return json.dumps({"data": {"node": {"isResolved": now.get("isResolved", False),
                                                 "participants": now.get("participants")}}})
        return json.dumps({"data": {}})
    return fake


class AutoResolveCommandTest(unittest.TestCase):
    """cmd_decide: resolution only on a standing APPROVE; failures never fatal."""

    def run_decide(self, threads, findings=(), heads=(SHA, SHA), fail_on=(), poster=POSTER,
                   thread_error=None, labels_after=(), live=None):
        heads = iter(heads)
        self.mutations = []
        self.writes = []
        reads = []
        fake_graphql = graphql_fake(self.mutations, threads, fail_on, live)

        def fake_gh(args, payload=None):
            if args[:2] == ["api", "graphql"]:
                return fake_graphql(args)
            if args[:3] == ["api", "-X", "POST"]:
                self.writes.append("POST")
                return json.dumps({"id": 99})
            if args[:3] == ["api", "-X", "PUT"]:
                self.writes.append("PUT")
                return "{}"
            if args[:2] == ["api", "--paginate"]:
                return json.dumps([[]])
            labels = labels_after if reads else ()
            reads.append(args)
            return json.dumps({"head": {"sha": next(heads)}, "base": {"ref": "main"},
                               "labels": [{"name": n} for n in labels]})

        def fake_iter(owner, name, pr):
            if thread_error:
                raise thread_error
            return iter(threads)

        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, "c.json")
            dpath = os.path.join(d, "pr.patch")
            with open(fpath, "w") as f:
                json.dump({"findings": list(findings), "panel": PANEL_OK}, f)
            with open(dpath, "w") as f:
                f.write(DIFF)
            args = argparse.Namespace(threshold="low", findings=fpath, repo="o/r", pr_number="1",
                                      commit_sha=SHA, judge_status="ok", delivered="true",
                                      ungated="0", approver_login="cursor-approver",
                                      reviewed_diff=dpath, base_ref="main", poster_login=poster)
            with mock.patch.object(AA, "gh", fake_gh), \
                    mock.patch.object(AA, "open_thread_severities", lambda *a: []), \
                    mock.patch.object(AA, "_load_gate_unresolved", lambda: GATE), \
                    mock.patch.object(GATE, "iter_threads", fake_iter), \
                    mock.patch.object(AA, "emit", lambda *a: None):
                return AA.cmd_decide(args)

    def test_approval_rechecks_resolves_then_replies_on_each_eligible_thread(self):
        rc = self.run_decide([thread("T1", "low"), thread("T2", "nit"), thread("H", "low", author="alice")])
        self.assertEqual(rc, 0)
        self.assertEqual([(k, t) for k, t, _ in self.mutations],
                         [("recheck", "T1"), ("resolve", "T1"), ("reply", "T1"),
                          ("recheck", "T2"), ("resolve", "T2"), ("reply", "T2")])
        reply = self.mutations[2][2]
        self.assertIn(AA.AUTO_RESOLVE_MARKER, reply)
        self.assertIn("Resolved by auto-approve: Low finding, at or below the `low` threshold, "
                      f"on commit {SHA[:7]}.", reply)

    def test_above_threshold_finding_resolves_nothing(self):
        rc = self.run_decide([thread("T1", "low")], findings=[finding("medium")])
        self.assertEqual((rc, self.mutations), (0, []))

    def test_open_above_threshold_thread_resolves_nothing(self):
        # decide() itself withholds approval (real open_thread_severities would
        # see the High); the resolver re-checks the threads it reads independently.
        rc = self.run_decide([thread("T1", "low"), thread("H", "high")])
        self.assertEqual((rc, self.mutations), (0, []))

    def test_moved_head_after_the_approval_resolves_nothing(self):
        rc = self.run_decide([thread("T1", "low")], heads=(SHA, "b" * 40))
        self.assertEqual((rc, self.writes, self.mutations), (0, ["POST", "PUT"], []))

    def test_human_review_label_after_the_approval_resolves_nothing(self):
        rc = self.run_decide([thread("T1", "low")], labels_after=["needs-human-review"])
        self.assertEqual((rc, self.writes, self.mutations), (0, ["POST", "PUT"], []))

    def test_a_failing_mutation_does_not_stop_the_others_or_the_approval(self):
        rc = self.run_decide([thread("T1", "low"), thread("T2", "low"), thread("T3", "low")],
                             fail_on={("resolve", "T1"), ("reply", "T2", "errors")})
        self.assertEqual(rc, 0)
        self.assertEqual(self.writes, ["POST"])  # the approval stands, nothing dismissed
        # A failed resolve posts NO reply: a reply from the approver would make
        # the still-open thread look human-touched to every later round.
        self.assertEqual([(k, t) for k, t, _ in self.mutations],
                         [("recheck", "T1"), ("resolve", "T1"),
                          ("recheck", "T2"), ("resolve", "T2"), ("reply", "T2"),
                          ("recheck", "T3"), ("resolve", "T3"), ("reply", "T3")])

    def test_a_human_reply_after_the_snapshot_stops_that_thread(self):
        rc = self.run_decide([thread("T1", "low"), thread("T2", "low")],
                             live={"T1": thread("T1", "low", repliers=["alice"])})
        self.assertEqual(rc, 0)
        self.assertEqual([(k, t) for k, t, _ in self.mutations],
                         [("recheck", "T1"), ("recheck", "T2"), ("resolve", "T2"), ("reply", "T2")])

    def test_a_thread_resolved_since_the_snapshot_is_left_alone(self):
        rc = self.run_decide([thread("T1", "low")], live={"T1": thread("T1", "low", resolved=True)})
        self.assertEqual([(k, t) for k, t, _ in self.mutations], [("recheck", "T1")])
        self.assertEqual(rc, 0)

    def test_a_non_object_graphql_body_is_not_fatal(self):
        rc = self.run_decide([thread("T1", "low"), thread("T2", "low")],
                             fail_on={("recheck", "T1", "null")})
        self.assertEqual((rc, self.writes), (0, ["POST"]))
        self.assertIn(("resolve", "T2"), [(k, t) for k, t, _ in self.mutations])

    def test_consecutive_failures_stop_the_loop(self):
        threads = [thread(f"T{i}", "low") for i in range(6)]
        rc = self.run_decide(threads, fail_on={("resolve", f"T{i}") for i in range(6)})
        self.assertEqual(rc, 0)
        self.assertEqual([t for k, t, _ in self.mutations if k == "resolve"],
                         [f"T{i}" for i in range(AA.MAX_CONSECUTIVE_FAILURES)])

    def test_a_success_resets_the_failure_streak(self):
        threads = [thread(f"T{i}", "low") for i in range(6)]
        rc = self.run_decide(threads, fail_on={("resolve", "T0"), ("resolve", "T1"), ("resolve", "T3"),
                                               ("resolve", "T4")})
        self.assertEqual(rc, 0)
        self.assertEqual([t for k, t, _ in self.mutations if k == "resolve"], [f"T{i}" for i in range(6)])

    def test_one_round_resolves_at_most_the_cap(self):
        threads = [thread(f"T{i}", "low") for i in range(AA.MAX_AUTO_RESOLVE + 5)]
        self.run_decide(threads)
        self.assertEqual(sum(1 for k, _, _ in self.mutations if k == "resolve"), AA.MAX_AUTO_RESOLVE)

    def test_an_unloadable_thread_module_is_not_fatal(self):
        def boom():
            raise RuntimeError("gate-unresolved.py missing")
        with mock.patch.object(AA, "_load_gate_unresolved", boom), mock.patch.object(AA, "emit", lambda *a: None):
            counts = AA.resolve_eligible_threads("o/r", 1, POSTER, "low", SHA)
        self.assertEqual(counts["resolved"], 0)

    def test_unreadable_threads_are_not_fatal(self):
        rc = self.run_decide([], thread_error=SystemExit(2))
        self.assertEqual((rc, self.writes, self.mutations), (0, ["POST"], []))

    def test_no_poster_login_resolves_nothing(self):
        rc = self.run_decide([thread("T1", "low")], poster="")
        self.assertEqual((rc, self.mutations), (0, []))

    def test_counts_are_logged(self):
        lines = []
        threads = [thread("T1", "low"), thread("H", "low", repliers=["alice"]),
                   thread("U", None, outdated=True), thread("M", "medium", outdated=True)]
        fake = graphql_fake([], threads)
        with mock.patch.object(AA, "emit", lines.append), \
                mock.patch.object(AA, "_load_gate_unresolved", lambda: GATE), \
                mock.patch.object(GATE, "iter_threads", lambda *a: iter(threads)), \
                mock.patch.object(AA, "gh", lambda args, payload=None: fake(args)):
            counts = AA.resolve_eligible_threads("o/r", 1, POSTER, "low", SHA)
        self.assertEqual(counts, {"resolved": 1, "skipped-human": 1, "skipped-unbadged": 1,
                                  "skipped-above-threshold": 1, "skipped-non-gating": 0,
                                  "failed": 0, "deferred": 0})
        self.assertIn("resolved 1, skipped-human 1, skipped-unbadged 1, skipped-above-threshold 1", lines[-1])


class DeferApprovalTest(unittest.TestCase):
    """`--defer-approval true`: the gate is reported, but only cursor-approve approves."""

    def run_decide(self, defer, findings=(), reviews=(), fail_put=False, pr_reads=()):
        self.writes = []
        self.resolved = []
        later_reads = list(pr_reads)  # PR payloads after the first read, in order

        def fake_gh(args, payload=None):
            if args[:2] == ["api", "-X"]:
                self.writes.append((args[2], args[3], payload))
                if args[2] == "PUT" and fail_put:
                    raise RuntimeError("gh api failed: 403 dismissals restricted")
                return '{"id": 99}' if args[2] == "POST" else "{}"
            if args[:2] == ["api", "graphql"]:
                return graphql_reviews(list(reviews))
            pr = {"head": {"sha": SHA}, "base": {"ref": "main"}}
            if getattr(fake_gh, "read", False) and later_reads:
                pr = later_reads.pop(0)
            fake_gh.read = True
            return json.dumps(pr)

        with tempfile.TemporaryDirectory() as d:
            fpath, dpath, out = (os.path.join(d, n) for n in ("c.json", "pr.patch", "out"))
            with open(fpath, "w") as f:
                json.dump({"findings": list(findings), "panel": PANEL_OK}, f)
            with open(dpath, "w") as f:
                f.write(DIFF)
            open(out, "w").close()
            args = argparse.Namespace(threshold="low", findings=fpath, repo="o/r", pr_number="1",
                                      commit_sha=SHA, judge_status="ok", delivered="true",
                                      ungated="0", approver_login="cursor-approver",
                                      reviewed_diff=dpath, base_ref="main", poster_login=POSTER)
            if defer is not None:
                args.defer_approval = defer
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out}), \
                    mock.patch.object(AA, "gh", fake_gh), \
                    mock.patch.object(AA, "open_thread_severities", lambda *a: []), \
                    mock.patch.object(AA, "resolve_eligible_threads", lambda *a: self.resolved.append(a)), \
                    mock.patch.object(AA, "emit", lambda *a: None):
                rc = AA.cmd_decide(args)
            return rc, read_outputs(out).get("approve_gate")

    def test_a_passing_round_posts_nothing_and_resolves_nothing(self):
        rc, gate = self.run_decide("true")
        self.assertEqual((rc, gate, self.writes, self.resolved), (0, AA.GATE_PASS, [], []))

    def test_earlier_marked_approvals_by_the_approver_are_withdrawn(self):
        own = {"id": 7, "user": {"login": "cursor-approver"}, "state": "APPROVED",
               "commit_id": SHA, "body": AA.APPROVE_MARKER}
        human = {"id": 8, "user": {"login": "alice"}, "state": "APPROVED", "commit_id": SHA, "body": "lgtm"}
        rc, gate = self.run_decide("true", reviews=[own, human])
        self.assertEqual((rc, gate), (0, AA.GATE_PASS))
        self.assertEqual([(m, path) for m, path, _ in self.writes],
                         [("PUT", "repos/o/r/pulls/1/reviews/7/dismissals")])
        self.assertEqual(self.writes[0][2]["message"], AA.DEFERRED_MESSAGE)
        self.assertEqual(self.resolved, [])

    def test_a_failed_withdrawal_downgrades_the_gate(self):
        own = {"id": 7, "user": {"login": "cursor-approver"}, "state": "APPROVED",
               "commit_id": SHA, "body": AA.APPROVE_MARKER}
        rc, gate = self.run_decide("true", reviews=[own], fail_put=True)
        self.assertEqual((rc, gate), (1, AA.GATE_UNTRUSTED))

    def test_own_marked_change_requests_are_dismissed(self):
        own = {"id": 5, "user": {"login": "cursor-approver"}, "state": "CHANGES_REQUESTED",
               "commit_id": SHA, "body": AA.APPROVE_MARKER}
        edited = dict(own, id=6, edited=True)
        unmarked = dict(own, id=9, body="please fix")
        other = dict(own, id=10, user={"login": "alice"})
        rc, gate = self.run_decide("true", reviews=[own, edited, unmarked, other])
        self.assertEqual((rc, gate), (0, AA.GATE_PASS))
        self.assertEqual([(m, path) for m, path, _ in self.writes],
                         [("PUT", "repos/o/r/pulls/1/reviews/5/dismissals")])
        self.assertEqual(self.writes[0][2]["message"], AA.PASSED_MESSAGE)

    def test_a_failed_change_request_dismissal_is_red_but_keeps_the_gate(self):
        own = {"id": 5, "user": {"login": "cursor-approver"}, "state": "CHANGES_REQUESTED",
               "commit_id": SHA, "body": AA.APPROVE_MARKER}
        rc, gate = self.run_decide("true", reviews=[own], fail_put=True)
        self.assertEqual((rc, gate), (1, AA.GATE_PASS))

    def test_a_change_since_the_read_downgrades_the_gate(self):
        moved = {"head": {"sha": "b" * 40}, "base": {"ref": "main"}}
        retargeted = {"head": {"sha": SHA}, "base": {"ref": "dev"}}
        labelled = {"head": {"sha": SHA}, "base": {"ref": "main"},
                    "labels": [{"name": AA.HUMAN_REVIEW_LABEL}]}
        for pr, want in ((moved, AA.GATE_UNTRUSTED), (retargeted, AA.GATE_UNTRUSTED),
                         (labelled, AA.GATE_CAPPED), ("not a dict", AA.GATE_UNTRUSTED)):
            with self.subTest(pr=pr):
                rc, gate = self.run_decide("true", pr_reads=[pr])
                self.assertEqual((rc, gate, self.writes), (0, want, []))

    def test_defer_is_read_case_and_whitespace_insensitively(self):
        for defer in ("True", " true ", "TRUE"):
            with self.subTest(defer=defer):
                rc, gate = self.run_decide(defer)
                self.assertEqual((rc, gate, self.writes, self.resolved), (0, AA.GATE_PASS, [], []))

    def test_request_changes_is_still_posted(self):
        rc, gate = self.run_decide("true", findings=[finding("high")])
        self.assertEqual((rc, gate), (0, AA.GATE_FAIL))
        self.assertEqual([(m, p["event"]) for m, _, p in self.writes], [("POST", AA.REQUEST_CHANGES)])

    def test_off_still_approves_and_resolves(self):
        for defer in (None, "", "false"):
            with self.subTest(defer=defer):
                rc, gate = self.run_decide(defer)
                self.assertEqual((rc, gate), (0, AA.GATE_PASS))
                self.assertEqual([(m, p["event"]) for m, _, p in self.writes], [("POST", AA.APPROVE)])
                self.assertEqual(len(self.resolved), 1)

    def test_the_workflow_passes_the_input_to_decide(self):
        with open(WORKFLOW_PATH, encoding="utf-8") as f:
            src = f.read()
        declared = src[src.index("\n      defer_approval:\n"):]
        declared = declared[: declared.index("\n      max_rounds:\n")]
        self.assertIn("type: boolean", declared)
        self.assertIn("default: false", declared)
        step = src[src.index("- name: Auto-approve decision"):]
        step = step[: step.index("\n\n  dismiss-stale-approval:")]
        self.assertIn("DEFER_APPROVAL: ${{ inputs.defer_approval }}", step)
        self.assertIn('--defer-approval "$DEFER_APPROVAL"', step)


class AutoResolveWiringTest(unittest.TestCase):
    """The workflow passes the findings poster's login to `decide`."""

    def test_decide_step_passes_poster_login(self):
        with open(WORKFLOW_PATH, encoding="utf-8") as f:
            src = f.read()
        step = src[src.index("auto-approve.py\" decide"):]
        step = step[: step.index("\n\n")]
        self.assertIn('--poster-login "$POSTER"', step)
        # Keyed on the token Post review used, not on the slug alone.
        self.assertIn('if [ -n "$BOT_TOKEN" ] && [ -n "$APP_SLUG" ]; then POSTER="${APP_SLUG}[bot]"; '
                      'else POSTER="github-actions[bot]"; fi', src)

    def test_auto_resolve_marker_matches_the_ledgers(self):
        # build-ledger.py reads gate-unresolved's copy to keep this reply out of
        # its answer count; the two must never drift.
        self.assertEqual(AA.AUTO_RESOLVE_MARKER, GATE.AUTO_RESOLVE_MARKER)

    def test_thread_query_fetches_what_the_resolver_needs(self):
        for field in ("participants: comments(first: 100)", "totalCount", "__typename", "\n          id\n"):
            with self.subTest(field=field):
                self.assertIn(field, GATE.QUERY)


if __name__ == "__main__":
    unittest.main()


INCREMENTAL_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "incremental-delta.patch")
LIVE_URL = "https://github.com/o/r/pull/1#discussion_r100"
RESOLVED_URL = "https://github.com/o/r/pull/1#discussion_r200"


def _fixture_text():
    with open(INCREMENTAL_FIXTURE, encoding="utf-8") as f:
        return f.read()


def _ledger(status="ok"):
    return {"status": status, "entries": [
        {"discussion_url": LIVE_URL, "anchored": True, "thread": {"resolved": False}},
        {"discussion_url": RESOLVED_URL, "anchored": True, "thread": {"resolved": True}},
    ]}


class ApproveScopeTest(unittest.TestCase):
    """approve_scope: rounds 2+ gate on the delta since the last reviewed commit."""

    def scope(self, requested="delta", state="built", text=None, ledger=None):
        return AA.resolve_scope(requested, state, _fixture_text() if text is None else text,
                                _ledger() if ledger is None else ledger)

    def gate(self, findings, scope, threads=()):
        return AA.decide_gate("low", findings, PANEL_OK, "ok", True, SHA, SHA, list(threads),
                              0, False, False, "main", "main", scope)

    def test_hunk_ranges_reads_new_side_of_the_fixture(self):
        self.assertEqual(AA.hunk_ranges(_fixture_text()), {"app/handler.py": [(10, 14), (43, 43)]})

    def test_round_one_with_delta_behaves_like_full(self):
        scope = self.scope(state="none")
        self.assertEqual(scope["scope"], "full")
        outside = {"severity": "medium", "file": "app/other.py", "line": 3}
        event, gate, reasons, blocking = self.gate([outside], scope)
        self.assertEqual((event, gate, blocking), ("REQUEST_CHANGES", "fail", [outside]))
        self.assertIn("first round", reasons[-1])

    def test_medium_outside_delta_and_low_inside_approves(self):
        scope = self.scope()
        outside = {"severity": "medium", "file": "app/other.py", "line": 3}
        inside_low = {"severity": "low", "file": "app/handler.py", "line": 11}
        event, gate, reasons, blocking = self.gate([outside, inside_low], scope,
                                                   threads=[("medium", True), ("low", False)])
        self.assertEqual((event, gate, blocking), ("APPROVE", "pass", []))
        self.assertIn("1 non-gating", reasons[-1])
        self.assertTrue(AA.non_gating(outside, "low", scope))
        self.assertFalse(AA.non_gating(inside_low, "low", scope))

    def test_non_gating_medium_thread_is_posted_marked_and_left_unresolved(self):
        post_review = AA._load_post_review()
        scope = self.scope()
        outside = {"severity": "medium", "file": "app/other.py", "line": 3, "body": "issue"}
        inside_low = {"severity": "low", "file": "app/handler.py", "line": 11, "body": "nit"}
        enriched = post_review.normalize_comments(
            [outside, inside_low], None, lambda f: AA.non_gating(f, "low", scope))
        bodies = {e["comment"]["path"]: e["comment"]["body"] for e in enriched}
        self.assertTrue(AA.is_non_gating_thread(bodies["app/other.py"]))
        self.assertIn("Outside this round's changes: not blocking auto-approve", bodies["app/other.py"])
        self.assertFalse(AA.is_non_gating_thread(bodies["app/handler.py"]))
        medium = thread("medium", "medium")
        medium["comments"]["nodes"][0]["body"] = bodies["app/other.py"]
        ids, counts = AutoResolvePlanTest.plan(self, [medium, thread("low", "low")], honour_non_gating=True)
        self.assertEqual(ids, ["low"])
        self.assertEqual(counts[AA.SKIP_NON_GATING], 1)

    def test_model_text_cannot_carry_the_marker(self):
        post_review = AA._load_post_review()
        sneaky = {"severity": "medium", "file": "a.py", "line": 1, "body": f"x\n{AA.NON_GATING_MARKER}"}
        body = post_review.normalize_comments([sneaky])[0]["comment"]["body"]
        self.assertFalse(AA.is_non_gating_thread(body))

    def test_marker_constants_match_post_review(self):
        post_review = AA._load_post_review()
        self.assertEqual(post_review.NON_GATING_MARKER, AA.NON_GATING_MARKER)
        self.assertEqual(post_review.NON_GATING_NOTE, AA.NON_GATING_NOTE)

    def test_medium_inside_delta_requests_changes(self):
        inside = {"severity": "medium", "file": "app/handler.py", "line": 12}
        event, gate, reasons, blocking = self.gate([inside], self.scope())
        self.assertEqual((event, gate, blocking), ("REQUEST_CHANGES", "fail", [inside]))
        self.assertIn("inside this round's changes", reasons[-1])

    def test_high_outside_delta_requests_changes(self):
        high = {"severity": "high", "file": "app/other.py", "line": 3}
        event, gate, _, blocking = self.gate([high], self.scope())
        self.assertEqual((event, gate, blocking), ("REQUEST_CHANGES", "fail", [high]))

    def test_unrecognised_severity_outside_delta_still_gates(self):
        odd = {"severity": "weird", "file": "app/other.py", "line": 3}
        self.assertEqual(self.gate([odd], self.scope())[0], "REQUEST_CHANGES")

    def test_reraise_of_unresolved_medium_requests_changes(self):
        repeat = {"severity": "medium", "file": "app/other.py", "line": 3, "repeat_of": LIVE_URL}
        event, gate, reasons, _ = self.gate([repeat], self.scope())
        self.assertEqual((event, gate), ("REQUEST_CHANGES", "fail"))
        self.assertIn("re-raise", reasons[-1])
        resolved = dict(repeat, repeat_of=RESOLVED_URL)
        self.assertEqual(self.gate([resolved], self.scope())[0], "APPROVE")

    def test_discarded_or_missing_block_fails_closed_to_full(self):
        outside = {"severity": "medium", "file": "app/other.py", "line": 3}
        for kwargs in ({"state": "discarded"}, {"state": "unavailable"}, {"state": ""},
                       {"ledger": {"status": "unknown"}}):
            with self.subTest(**kwargs):
                scope = self.scope(**kwargs)
                self.assertEqual(scope["scope"], "full")
                self.assertIn("fails closed", scope["note"])
                self.assertEqual(self.gate([outside], scope)[0], "REQUEST_CHANGES")
        self.assertEqual(AA.resolve_scope("delta", "built", None, _ledger())["scope"], "full")
        self.assertEqual(AA.resolve_scope("delta", "built", "", None)["scope"], "full")

    def test_empty_delta_rebase_fails_closed_to_full(self):
        # #357 review: an empty block made every non-severe finding non-gating, and
        # the rebase that empties it also outdates the earlier threads the
        # open-thread check would otherwise have counted.
        for text in ("", "diff --git a/bin.png b/bin.png\nBinary files a/bin.png and b/bin.png differ\n"):
            with self.subTest(text=text):
                scope = self.scope(text=text)
                self.assertEqual(scope["scope"], "full")
                self.assertIn("no new-side hunk", scope["note"])
                old = {"severity": "medium", "file": "app/handler.py", "line": 11}
                self.assertEqual(self.gate([old], scope)[:2], ("REQUEST_CHANGES", "fail"))
                self.assertFalse(AA.non_gating(old, "low", scope))

    def test_unparseable_section_path_fails_closed_to_full(self):
        # #357 review: git C-quotes an odd path and parse_paths rejects it; dropping
        # the section recorded no ranges, so findings in it failed open.
        quoted = ('diff --git "a/app/we\\"ird.py" "b/app/we\\"ird.py"\n'
                  '--- "a/app/we\\"ird.py"\n+++ "b/app/we\\"ird.py"\n@@ -1 +1 @@\n-a\n+b\n')
        self.assertIsNone(AA.hunk_ranges(_fixture_text() + quoted))
        scope = self.scope(text=_fixture_text() + quoted)
        self.assertEqual(scope["scope"], "full")
        self.assertIn("could not be parsed", scope["note"])
        odd = {"severity": "medium", "file": 'app/we"ird.py', "line": 1}
        self.assertEqual(self.gate([odd], scope)[:2], ("REQUEST_CHANGES", "fail"))

    def test_finding_without_usable_file_gates(self):
        scope = self.scope()
        for path in (None, "", 7, ["a"]):
            with self.subTest(path=path):
                f = {"severity": "medium", "file": path, "line": 3}
                self.assertEqual(AA.gating_reason(f, scope), "no usable file anchor")
                self.assertFalse(AA.non_gating(f, "low", scope))
                self.assertEqual(self.gate([f], scope)[0], "REQUEST_CHANGES")

    def test_untrusted_reasons_are_never_read_as_the_scope_note(self):
        scope = self.scope()
        event, gate, reasons, _ = AA.decide_gate("low", [], PANEL_OK, "error", True, SHA, "b" * 40, [],
                                                 0, False, False, "main", "main", scope)
        self.assertEqual(gate, "untrusted")
        self.assertGreater(len(reasons), 1)
        self.assertEqual(AA.scope_note_of(reasons), "")
        body = AA.render_body(event, reasons, "low", [])
        self.assertNotIn("_Scope:", body)
        _, _, scoped, _ = self.gate([], scope)
        self.assertTrue(AA.scope_note_of(scoped).startswith("approve_scope `delta`"))
        self.assertIn("_Scope: approve_scope `delta`", AA.render_body("APPROVE", scoped, "low", []))

    def test_marked_thread_blocks_resolution_unless_the_round_was_delta(self):
        marked = thread("marked", "medium")
        marked["comments"]["nodes"][0]["body"] += f"\n\n{AA.NON_GATING_NOTE}\n{AA.NON_GATING_MARKER}"
        ids, counts = AutoResolvePlanTest.plan(self, [marked, thread("low", "low")])
        self.assertEqual((ids, counts[AA.RESOLVED]), ([], 0))
        ids, _ = AutoResolvePlanTest.plan(self, [marked, thread("low", "low")], honour_non_gating=True)
        self.assertEqual(ids, ["low"])

    def test_marked_thread_is_never_resolved_even_under_a_looser_threshold(self):
        marked = thread("marked", "medium")
        marked["comments"]["nodes"][0]["body"] += f"\n\n{AA.NON_GATING_NOTE}\n{AA.NON_GATING_MARKER}"
        self.assertEqual(AA.classify_thread(marked, POSTER, "medium", GATE), (AA.SKIP_NON_GATING, "medium"))
        for honour in (False, True):
            ids, counts = AutoResolvePlanTest.plan(self, [marked], threshold="medium", honour_non_gating=honour)
            self.assertEqual((ids, counts[AA.SKIP_NON_GATING]), ([], 1))

    def test_nested_marker_cannot_reform_after_stripping(self):
        post_review = AA._load_post_review()
        m = AA.NON_GATING_MARKER
        nested = m[:len(m) // 2] + m + m[len(m) // 2:]
        for text in (nested, m[:5] + nested + m[5:], f"x\n{nested}\ny", AA.NON_GATING_NOTE,
                     AA.NON_GATING_NOTE[:9] + AA.NON_GATING_NOTE + AA.NON_GATING_NOTE[9:]):
            with self.subTest(text=text):
                sneaky = {"severity": "medium", "file": "a.py", "line": 1, "body": text}
                body = post_review.normalize_comments([sneaky])[0]["comment"]["body"]
                self.assertNotIn(m, body)
                self.assertNotIn(AA.NON_GATING_NOTE, body)
                self.assertFalse(AA.is_non_gating_thread(body))

    def test_full_scope_matches_todays_behaviour(self):
        scope = self.scope(requested="full")
        outside = {"severity": "medium", "file": "app/other.py", "line": 3}
        for findings, threads in (([outside], []), ([], [("medium", True)]), ([], ["low"]), ([], [])):
            with self.subTest(findings=findings, threads=threads):
                legacy = AA.decide_gate("low", findings, PANEL_OK, "ok", True, SHA, SHA,
                                        [t[0] if isinstance(t, tuple) else t for t in threads],
                                        0, False, False, "main", "main")
                scoped = self.gate(findings, scope, threads)
                self.assertEqual(scoped[:2], legacy[:2])
                self.assertEqual(scoped[2][0], legacy[2][0])
                self.assertEqual(scoped[3], legacy[3])

    def test_scope_validation(self):
        # Empty is fail-closed `full`; only the workflow input's default picks delta.
        self.assertEqual(AA.validate_scope(""), "full")
        self.assertEqual(AA.validate_scope("delta"), "delta")
        self.assertEqual(AA.validate_scope("FULL"), "full")
        with self.assertRaises(ValueError):
            AA.validate_scope("partial")

    def test_workflow_input_defaults_to_delta_and_reaches_both_steps(self):
        with open(WORKFLOW_PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertRegex(text, r"\n      approve_scope:\n(?:        .*\n)+?        default: delta\n")
        self.assertEqual(text.count("APPROVE_SCOPE: ${{ inputs.approve_scope }}"), 2)
        self.assertEqual(text.count("incremental_state=none"), 1)
        self.assertEqual(text.count("incremental_state=built"), 1)
        self.assertEqual(text.count("incremental_state=discarded"), 1)
