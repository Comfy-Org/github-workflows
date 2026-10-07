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
    def __init__(self, head=SHA, labels=(), reviews=(), head_after=None):
        self.head, self.labels, self.reviews = head, list(labels), list(reviews)
        self.head_after = head_after
        self.posted, self.dismissed, self.reads = [], [], 0

    def __call__(self, args, payload=None):
        if args[:3] == ["api", "-X", "POST"] and args[3].endswith("/reviews"):
            self.posted.append(payload)
            return json.dumps({"id": 555})
        if args[:3] == ["api", "-X", "PUT"]:
            self.dismissed.append(args[3].split("/")[-2])
            return "{}"
        if "--paginate" in args:
            return json.dumps([self.reviews])
        self.reads += 1
        head = self.head_after if (self.head_after and self.reads > 1) else self.head
        return json.dumps({"head": {"sha": head}, "base": {"ref": "main"},
                           "labels": [{"name": n} for n in self.labels]})


def prior_approval():
    return {"id": 42, "state": "APPROVED", "user": {"login": LOGIN},
            "body": aa.APPROVE_MARKER + f"\n<!-- cursor-review-auto-approve:sha={'c' * 40} -->"}


class ApproveExternal(unittest.TestCase):
    def run_cmd(self, fake, verdicts, event=None):
        if event is None:
            event = "APPROVE" if all(v in ("green", "yellow") for v in verdicts.values()) and verdicts else "NONE"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "decision.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"event": event, "verdicts": verdicts, "axes": {}, "reasons": []}, f)
            out = os.path.join(tmp, "out")
            args = argparse.Namespace(repo="o/r", pr_number="1", commit_sha=SHA, axes=AXES, decision=path,
                                      approver_login=LOGIN, card_url="https://github.com/o/r/pull/1#issuecomment-9")
            with mock.patch.object(aa, "gh", fake), mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out}), \
                    mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
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

    def test_unreadable_decision_posts_nothing(self):
        self.assertFalse(aa.external_decision_approves(None, ["correctness"]))
        self.assertFalse(aa.external_decision_approves({"event": "APPROVE", "verdicts": "x"}, ["correctness"]))


if __name__ == "__main__":
    unittest.main()
