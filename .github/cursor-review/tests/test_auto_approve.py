#!/usr/bin/env python3
"""auto-approve.py: the decision table and the stale-review filter.

Every rule in the module docstring has a case here, because each one is a way an
approval could land on a PR nobody should have approved:

* threshold validation rejects `high`/`critical`/typos (exit 2, not a silent off);
* any finding above the threshold — or with an unrecognised severity — requests
  changes, never approves;
* an un-adjudicated (judge-degraded) round neither approves nor vetoes;
* an incomplete panel, an undelivered review, a moved head, or an open
  critical/high (or unbadged) thread from an earlier round withholds approval;
* dismissal touches only the approver's own marked reviews that are off-head.

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "auto-approve.py")
SPEC = importlib.util.spec_from_file_location("auto_approve", MODULE_PATH)
AA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AA)

SHA = "a" * 40
PANEL_OK = [{"model": "m", "review_type": "adversarial", "status": "ok"}]


def finding(sev):
    return {"file": "f.go", "line": 1, "severity": sev, "body": "x"}


def decide(threshold="medium", findings=(), panel=PANEL_OK, judge="ok", delivered=True,
           reviewed=SHA, live=SHA, threads=(), ungated=0):
    return AA.decide(threshold, list(findings), list(panel), judge, delivered, reviewed, live, list(threads), ungated)


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

    def test_a_truncated_page_does_not_loop_forever(self):
        # hasNextPage with no cursor to follow: stop, rather than re-request page
        # one until the job times out.
        reviews, calls = self.list_reviews(
            graphql_reviews([{"id": 1, "user": {"login": "x"}, "state": "APPROVED", "body": "b"}],
                            has_next=True, cursor=None),
        )
        self.assertEqual(([r["id"] for r in reviews], len(calls)), ([1], 1))

    def test_a_shapeless_response_raises_instead_of_listing_nothing(self):
        # Both callers read an empty list as "nothing to dismiss" and exit green,
        # so this has to reach their error paths instead.
        with self.assertRaises(ValueError):
            self.list_reviews(json.dumps({"data": {"repository": {"pullRequest": None}}}))


class StaleReviewTest(unittest.TestCase):
    def review(self, rid, login="cursor-approver", state="APPROVED", commit=None, marker=True, sha=OLD,
               edited=False):
        # `commit_id` defaults to NEW on purpose: GitHub moves a still-valid
        # approval's commit_id to each new head, so only the SHA recorded in the
        # body says what was reviewed.
        body = AA.render_body(AA.APPROVE, ["ok"], "low", [], sha) if marker else "ok"
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

    def test_an_edited_change_request_is_still_not_dismissed(self):
        # Only APPROVALS are this filter's business; an edited veto is someone
        # tampering with a review that withholds merge, not one that grants it.
        reviews = [self.review(1, state="CHANGES_REQUESTED", edited=True)]
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", NEW), [])

    def test_none_head_selects_every_own_approval(self):
        reviews = [self.review(1, commit="new"), self.review(2), self.review(3, state="CHANGES_REQUESTED")]
        self.assertEqual(AA.stale_reviews_to_dismiss(reviews, "cursor-approver", None), [1, 2])


class DismissStaleHeadTest(unittest.TestCase):
    """`dismiss-stale` judges against the LIVE head, not the delivered event's.

    A queued or redelivered `synchronize` replays an older head, and dismissing an
    approval that is valid for the current head exits GREEN — nothing reports the
    loss — so this read is the only thing between a replay and a silently withdrawn
    approval. An unreadable head degrades to the event's, never to "".
    """

    def run_dismiss(self, live, event_head, reviews):
        dismissed = []

        def fake_gh(args, payload=None):
            if args[:2] == ["api", "-X"] and args[2] == "PUT":
                dismissed.append(args[3])
                return "{}"
            if args[:2] == ["api", "graphql"]:
                return graphql_reviews(list(reviews))
            if isinstance(live, Exception):
                raise live
            return json.dumps({} if live is None else {"head": {"sha": live}})

        args = argparse.Namespace(repo="o/r", pr_number="1", head_sha=event_head,
                                  approver_login="cursor-approver")
        self.printed = []
        with mock.patch.object(AA, "gh", fake_gh), mock.patch.object(AA, "emit", lambda *a: None), \
                mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
            rc = AA.cmd_dismiss_stale(args)
        return rc, dismissed

    def approval(self, rid, sha):
        return {"id": rid, "user": {"login": "cursor-approver"}, "state": "APPROVED",
                "body": AA.render_body(AA.APPROVE, ["ok"], "low", [], sha)}

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

    def warnings(self):
        return [line for line in self.printed if "::warning::" in line]

    def test_a_degraded_head_read_is_announced_rather_than_silent(self):
        # The dismissal below may be the right one or the replay this read exists to
        # catch — unreadable means UNKNOWN. Exiting green on it without a word is
        # what makes a wrongly withdrawn approval unattributable afterwards.
        rc, dismissed = self.run_dismiss(RuntimeError("timeout"), NEW, [self.approval(1, OLD)])
        self.assertEqual((rc, len(dismissed)), (0, 1))
        self.assertTrue(any("live head" in line for line in self.warnings()), self.printed)

    def test_a_shapeless_head_read_is_announced_too(self):
        # The fallback that fires without raising, so an `except`-only diagnostic
        # would miss it.
        self.run_dismiss(None, NEW, [self.approval(1, NEW)])
        self.assertTrue(any("no head SHA" in line for line in self.warnings()), self.printed)

    def test_a_head_that_read_cleanly_warns_about_nothing(self):
        self.run_dismiss(NEW, NEW, [self.approval(1, OLD)])
        self.assertEqual(self.warnings(), [])

    def test_a_shapeless_head_read_never_dismisses_everything(self):
        # read_head returning "" must not reach the filter: "" is not None, so it
        # would match no recorded SHA and withdraw every marked approval on the PR.
        self.assertEqual(self.run_dismiss(None, NEW, [self.approval(1, NEW)]), (0, []))


class PostWriteRaceTest(unittest.TestCase):
    """A push between the head read and the POST must not leave our review standing."""

    def run_decide(self, heads, judge_status="ok", reviews=(), threads=lambda *a: []):
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
            return json.dumps({"head": {"sha": head}})

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"findings": [], "panel": PANEL_OK}, f)
        args = argparse.Namespace(threshold="medium", findings=f.name, repo="o/r", pr_number="1",
                                  commit_sha=SHA, judge_status=judge_status, delivered="true",
                                  ungated="0", approver_login="cursor-approver")
        with mock.patch.object(AA, "gh", fake_gh), \
                mock.patch.object(AA, "open_thread_severities", threads), \
                mock.patch.object(AA, "emit", lambda *a: None):
            rc = AA.cmd_decide(args)
        os.unlink(f.name)
        return rc, [c[0][2] for c in calls if c[0][:2] == ["api", "-X"]]

    def test_untrusted_round_withdraws_an_earlier_approval(self):
        # Round 1 approved this very head; a degraded re-run must not leave it counting.
        approval = {"id": 7, "user": {"login": "cursor-approver"}, "state": "APPROVED",
                    "commit_id": SHA, "body": AA.APPROVE_MARKER}
        rc, writes = self.run_decide([SHA], judge_status="error", reviews=[approval])
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


if __name__ == "__main__":
    unittest.main()
