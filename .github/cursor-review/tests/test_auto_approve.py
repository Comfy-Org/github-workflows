#!/usr/bin/env python3
"""auto-approve.py: the decision table and the stale-review filter.

Every rule in the module docstring has a case here, because each one is a way an
approval could land on a PR nobody should have approved:

* threshold validation rejects `high`/`critical`/typos (exit 2, not a silent off);
* any finding above the threshold — or with an unrecognised severity — requests
  changes, never approves;
* an un-adjudicated (judge-degraded) round neither approves nor vetoes;
* an incomplete panel, an undelivered review, a moved head or base, or an open
  critical/high (or unbadged) thread from an earlier round withholds approval;
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


def decide(threshold="medium", findings=(), panel=PANEL_OK, judge="ok", delivered=True,
           reviewed=SHA, live=SHA, threads=(), ungated=0, empty_diff=False, reviewed_base="main", live_base="main"):
    return AA.decide(threshold, list(findings), list(panel), judge, delivered, reviewed, live, list(threads), ungated,
                     empty_diff, reviewed_base, live_base)


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

    def run_dismiss(self, reviews, all_approvals=False, list_error=None, login="cursor-approver", live_base="main"):
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
            return json.dumps({"head": {"sha": NEW}, "base": {"ref": live_base}})

        args = argparse.Namespace(repo="o/r", pr_number="1", approver_login=login, all_approvals=all_approvals,
                                  head_sha=OLD)
        with mock.patch.object(AA, "gh", fake_gh), mock.patch.object(AA, "emit", lambda *a: None):
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

    def test_non_object_reviews_are_dropped_rather_than_iterated(self):
        body = json.dumps({"data": {"repository": {"pullRequest": {"reviews": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": ["abc", None, json.loads(graphql_reviews([{"id": 1, "user": {"login": "x"}}]))
                      ["data"]["repository"]["pullRequest"]["reviews"]["nodes"][0]],
        }}}}})
        self.assertEqual([r["id"] for r in self.list_reviews_with(body)], [1])


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
            # half-shapeless response, exercised deliberately below.
            return json.dumps({} if live is None else {"head": {"sha": live}, "base": {"ref": live_base}})

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

    def run_decide(self, heads, judge_status="ok", reviews=(), threads=lambda *a: [], diff=DIFF):
        calls = []
        heads = iter(heads)

        def fake_gh(args, payload=None):
            calls.append((args, payload))
            if args[:2] == ["api", "-X"] and args[2] == "POST":
                return json.dumps({"id": 99})
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
                   labels_after=None, reviews=(), put_error=None):
        heads = iter(heads)
        reads = []
        writes = []

        def fake_gh(args, payload=None):
            if args[:3] == ["api", "-X", "POST"]:
                if post_error:
                    raise RuntimeError(post_error)
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
                json.dump({"findings": list(findings), "panel": PANEL_OK}, f)
            with open(dpath, "w") as f:
                f.write(DIFF)
            args = argparse.Namespace(threshold=threshold, findings=fpath, repo="o/r", pr_number="1",
                                      commit_sha=SHA, judge_status="ok", delivered="true",
                                      ungated="0", approver_login="cursor-approver",
                                      reviewed_diff=dpath, base_ref="main")
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


if __name__ == "__main__":
    unittest.main()
