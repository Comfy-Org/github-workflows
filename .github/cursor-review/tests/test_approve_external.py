"""Decide-path tests for auto-approve.py `approve-external` (cursor-approve)."""

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("auto_approve", os.path.join(HERE, "..", "auto-approve.py"))
aa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aa)

SHA = "b" * 40
LOGIN = "approver-bot"
AXES = "correctness,conformance"


class FakeGitHub:
    def __init__(self, head=SHA, labels=(), reviews=(), head_after=None, base="main"):
        self.head, self.labels, self.reviews, self.base = head, list(labels), list(reviews), base
        self.head_after = head_after
        self.posted, self.dismissed, self.reads = [], [], 0

    def __call__(self, args, payload=None):
        if args[:3] == ["api", "-X", "POST"] and args[3].endswith("/reviews"):
            self.posted.append(payload)
            return json.dumps({"id": 555})
        if args[:3] == ["api", "-X", "PUT"]:
            self.dismissed.append(args[3].split("/")[-2])
            return "{}"
        if args[:2] == ["api", "graphql"]:
            # list_reviews reads through GraphQL (for `lastEditedAt`).
            nodes = [{"fullDatabaseId": str(r["id"]), "databaseId": r["id"], "state": r.get("state"),
                      "body": r.get("body"), "lastEditedAt": None, "submittedAt": None, "url": "",
                      "author": {"__typename": "User", "login": (r.get("user") or {}).get("login", "")}}
                     for r in self.reviews]
            return json.dumps({"data": {"repository": {"pullRequest": {"reviews": {
                "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}})
        self.reads += 1
        head = self.head_after if (self.head_after and self.reads > 1) else self.head
        return json.dumps({"head": {"sha": head}, "base": {"ref": self.base},
                           "labels": [{"name": n} for n in self.labels]})


def prior_approval(sha="c" * 40, rid=42):
    return {"id": rid, "state": "APPROVED", "user": {"login": LOGIN},
            "body": aa.APPROVE_MARKER + f"\n<!-- cursor-review-auto-approve:sha={sha} -->"}


class ApproveExternal(unittest.TestCase):
    def run_cmd(self, fake, verdicts, event=None, login=LOGIN, base_ref="main", threshold="", poster=""):
        if event is None:
            event = "APPROVE" if all(v in ("green", "yellow") for v in verdicts.values()) and verdicts else "NONE"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "decision.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"event": event, "verdicts": verdicts, "axes": {}, "reasons": []}, f)
            out = os.path.join(tmp, "out")
            args = argparse.Namespace(repo="o/r", pr_number="1", commit_sha=SHA, axes=AXES, decision=path,
                                      approver_login=login, base_ref=base_ref, card_url="https://github.com/o/r/pull/1#issuecomment-9",
                                      threshold=threshold, poster_login=poster)
            self.resolve_calls = []
            with mock.patch.object(aa, "gh", fake), mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out}), \
                    mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}), \
                    mock.patch.object(aa, "resolve_eligible_threads",
                                      lambda *a: self.resolve_calls.append(a) or {}):
                rc = aa.cmd_approve_external(args)
            with open(out, encoding="utf-8") as f:
                outcome = f.read().strip().split("\n")[-1].split("=", 1)[1]
        return rc, outcome

    def test_all_green_posts_one_approve(self):
        fake = FakeGitHub()
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(len(fake.posted), 1)
        review = fake.posted[0]
        self.assertEqual(review["event"], "APPROVE")
        self.assertEqual(review["commit_id"], SHA)
        self.assertIn(f"sha={SHA}", review["body"])
        self.assertIn("Approved, see the [cursor-approve card](https://github.com/o/r/pull/1#issuecomment-9).", review["body"])
        self.assertEqual(fake.dismissed, [])

    def test_one_red_posts_nothing_and_withdraws_prior_approval(self):
        fake = FakeGitHub(reviews=[prior_approval()])
        rc, outcome = self.run_cmd(fake, {"correctness": "red", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "not_approved"))
        self.assertEqual(fake.posted, [])
        self.assertEqual(fake.dismissed, ["42"])

    def test_missing_axis_posts_nothing(self):
        fake = FakeGitHub()
        rc, outcome = self.run_cmd(fake, {"correctness": "green"}, event="APPROVE")
        self.assertEqual((rc, outcome), (0, "not_approved"))
        self.assertEqual(fake.posted, [])

    def test_head_moved_posts_nothing(self):
        fake = FakeGitHub(head="d" * 40, reviews=[prior_approval()])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "superseded"))
        self.assertEqual(fake.posted, [])
        self.assertEqual(fake.dismissed, ["42"])

    def test_head_moved_during_post_withdraws(self):
        fake = FakeGitHub(head_after="d" * 40)
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "superseded"))
        self.assertEqual(fake.dismissed, ["555"])

    def test_needs_human_review_label_posts_nothing(self):
        fake = FakeGitHub(labels=[aa.HUMAN_REVIEW_LABEL])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "needs_human"))
        self.assertEqual(fake.posted, [])

    def test_late_superseded_decide_keeps_an_approval_on_the_live_head(self):
        # An older run's decide finishing after a newer run approved the new head
        # must withdraw only what is stale against that head.
        fake = FakeGitHub(head="d" * 40, reviews=[prior_approval(), prior_approval("d" * 40, 77)])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "superseded"))
        self.assertEqual(fake.dismissed, ["42"])

    def test_base_changed_since_the_axes_posts_nothing(self):
        fake = FakeGitHub(base="release", reviews=[prior_approval(SHA)])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"}, base_ref="main")
        self.assertEqual((rc, outcome), (0, "superseded"))
        self.assertEqual(fake.posted, [])

    def test_approval_records_the_axes_base(self):
        fake = FakeGitHub()
        self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertIn(f"base:{'main'.encode().hex()}", fake.posted[0]["body"])

    def test_empty_login_refuses(self):
        fake = FakeGitHub()
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"}, login="")
        self.assertEqual((rc, outcome), (2, "error"))
        self.assertEqual(fake.posted, [])

    def test_withdraw_dismisses_own_marked_approvals(self):
        fake = FakeGitHub(reviews=[prior_approval(SHA)])
        args = argparse.Namespace(repo="o/r", pr_number="1", approver_login=LOGIN)
        with mock.patch.object(aa, "gh", fake), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            self.assertEqual(aa.cmd_withdraw(args), 0)
        self.assertEqual(fake.dismissed, ["42"])
        args.approver_login = " "
        with mock.patch.object(aa, "gh", fake):
            self.assertEqual(aa.cmd_withdraw(args), 2)

    # --- the bot's own at-or-below-threshold threads, as cursor-review's decide ---

    def test_approve_resolves_the_posters_threads(self):
        fake = FakeGitHub()
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"},
                                   threshold="Low", poster="cr-bot[bot]")
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(self.resolve_calls, [("o/r", "1", "cr-bot[bot]", "low", SHA)])

    def test_none_resolves_nothing(self):
        fake = FakeGitHub()
        rc, outcome = self.run_cmd(fake, {"correctness": "red", "conformance": "green"},
                                   threshold="low", poster="cr-bot[bot]")
        self.assertEqual((rc, outcome), (0, "not_approved"))
        self.assertEqual(self.resolve_calls, [])

    def test_approval_withdrawn_after_post_resolves_nothing(self):
        fake = FakeGitHub(head_after="d" * 40)
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"},
                                   threshold="low", poster="cr-bot[bot]")
        self.assertEqual((rc, outcome), (0, "superseded"))
        self.assertEqual(self.resolve_calls, [])

    def test_superseded_and_needs_human_resolve_nothing(self):
        for fake in (FakeGitHub(head="d" * 40), FakeGitHub(labels=[aa.HUMAN_REVIEW_LABEL])):
            self.run_cmd(fake, {"correctness": "green", "conformance": "green"},
                         threshold="low", poster="cr-bot[bot]")
            self.assertEqual(self.resolve_calls, [])

    def test_no_threshold_resolves_nothing(self):
        rc, outcome = self.run_cmd(FakeGitHub(), {"correctness": "green", "conformance": "green"},
                                   poster="cr-bot[bot]")
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(self.resolve_calls, [])

    def test_invalid_threshold_keeps_the_approval_and_resolves_nothing(self):
        fake = FakeGitHub()
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"},
                                   threshold="bogus", poster="cr-bot[bot]")
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(len(fake.posted), 1)
        self.assertEqual(fake.dismissed, [])
        self.assertEqual(self.resolve_calls, [])

    def test_unreadable_decision_posts_nothing(self):
        self.assertFalse(aa.external_decision_approves(None, ["correctness"]))
        self.assertFalse(aa.external_decision_approves({"event": "APPROVE", "verdicts": "x"}, ["correctness"]))


if __name__ == "__main__":
    unittest.main()
