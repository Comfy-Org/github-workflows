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
    def __init__(self, head=SHA, labels=(), reviews=(), head_after=None, base="main",
                 labels_after=None, read_error=False, pr_override=None, pr_after=None, post_reply=None):
        self.head, self.labels, self.reviews, self.base = head, list(labels), list(reviews), base
        self.head_after, self.labels_after = head_after, labels_after
        self.read_error, self.pr_override = read_error, pr_override
        self.pr_after, self.post_reply = pr_after, post_reply
        self.posted, self.dismissed, self.dismiss_messages, self.reads = [], [], [], 0

    def __call__(self, args, payload=None):
        if args[:3] == ["api", "-X", "POST"] and args[3].endswith("/reviews"):
            self.posted.append(payload)
            return json.dumps({"id": 555}) if self.post_reply is None else self.post_reply
        if args[:3] == ["api", "-X", "PUT"]:
            self.dismissed.append(args[3].split("/")[-2])
            self.dismiss_messages.append((payload or {}).get("message"))
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
        if self.read_error:
            raise RuntimeError("gh api: HTTP 502")
        if self.pr_after is not None and self.reads > 1:
            return json.dumps(self.pr_after)
        if self.pr_override is not None:
            return json.dumps(self.pr_override)
        head = self.head_after if (self.head_after and self.reads > 1) else self.head
        labels = self.labels_after if (self.labels_after is not None and self.reads > 1) else self.labels
        return json.dumps({"head": {"sha": head}, "base": {"ref": self.base},
                           "labels": [{"name": n} for n in labels]})


def prior_approval(sha="c" * 40, rid=42):
    return {"id": rid, "state": "APPROVED", "user": {"login": LOGIN},
            "body": aa.APPROVE_MARKER + f"\n<!-- cursor-review-auto-approve:sha={sha} -->"}


class ApproveExternal(unittest.TestCase):
    def run_cmd(self, fake, verdicts, event=None, login=LOGIN, base_ref="main", threshold="", poster="", **extra):
        if event is None:
            event = "APPROVE" if all(v in ("green", "yellow") for v in verdicts.values()) and verdicts else "NONE"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "decision.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"event": event, "verdicts": verdicts, "axes": {}, "reasons": []}, f)
            out = os.path.join(tmp, "out")
            args = argparse.Namespace(repo="o/r", pr_number="1", commit_sha=SHA, axes=AXES, decision=path,
                                      approver_login=login, base_ref=base_ref, card_url="https://github.com/o/r/pull/1#issuecomment-9",
                                      threshold=threshold, poster_login=poster, **extra)
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

    def test_an_approval_withdraws_a_standing_request_for_changes(self):
        # BE-19489: a no-decision round left a standing REQUEST_CHANGES; the
        # deferred approval that finally lands withdraws it.
        block = {"id": 77, "state": "CHANGES_REQUESTED", "user": {"login": LOGIN},
                 "body": aa.APPROVE_MARKER + "\nNo decision this round."}
        fake = FakeGitHub(reviews=[block])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(fake.dismissed, ["77"])
        self.assertEqual(fake.dismiss_messages, [aa.APPROVED_LATER_MESSAGE])

    def test_a_red_axis_posts_a_standing_request_for_changes(self):
        # BE-19489 under defer_approval: the passing cursor-review round already
        # dismissed every earlier block, so withholding here must leave one.
        fake = FakeGitHub()
        rc, outcome = self.run_cmd(fake, {"correctness": "red", "conformance": "green"}, threshold="low")
        self.assertEqual((rc, outcome), (0, "not_approved"))
        self.assertEqual([p["event"] for p in fake.posted], ["REQUEST_CHANGES"])
        review = fake.posted[0]
        self.assertEqual(review["commit_id"], SHA)
        self.assertIn(aa.APPROVE_MARKER, review["body"])
        self.assertIn(f"<!-- cursor-review-auto-approve:sha={SHA} -->", review["body"])
        card = aa._load_card()
        # The card's own reasons and next step, word for word.
        self.assertEqual(card.decide_reasons("not_approved", {"reasons": []}), ["no decision was reached"])
        self.assertIn("- no decision was reached", review["body"])
        self.assertIn(f"**Next step:** {card.DECIDE_NEXT_TEXT['not_approved']}", review["body"])
        self.assertIn("_Threshold: `low`", review["body"])

    def test_the_block_carries_the_axis_reasons_sanitized(self):
        fake = FakeGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "decision.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"event": "NONE", "verdicts": {"correctness": "red", "conformance": "green"},
                           "reasons": ["red on correctness @team <!-- x -->"]}, f)
            args = argparse.Namespace(repo="o/r", pr_number="1", commit_sha=SHA, axes=AXES, decision=path,
                                      approver_login=LOGIN, base_ref="main", card_url="", threshold="",
                                      poster_login="")
            with mock.patch.object(aa, "gh", fake), \
                    mock.patch.dict(os.environ, {"GITHUB_OUTPUT": os.path.join(tmp, "out"), "GITHUB_STEP_SUMMARY": ""}):
                self.assertEqual(aa.cmd_approve_external(args), 0)
        body = fake.posted[0]["body"]
        self.assertIn("red on correctness", body)
        self.assertNotIn("<!-- x", body)
        self.assertNotRegex(body, r"@(?!\u200b)")
        self.assertNotIn("_Threshold", body)  # no threshold given → no threshold line

    def test_vetoed_and_own_pr_post_no_block(self):
        fake = FakeGitHub(labels=[aa.SKIP_REVIEW_LABEL])
        rc, outcome = self.run_cmd(fake, {"correctness": "red", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "vetoed"))
        self.assertEqual(fake.posted, [])

        class OwnPR(FakeGitHub):
            def __call__(self, args, payload=None):
                if args[:3] == ["api", "-X", "POST"]:
                    self.posted.append(payload)
                    raise RuntimeError("gh api failed: Can not approve your own pull request")
                return super().__call__(args, payload)

        fake = OwnPR()
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "own_pr"))
        self.assertEqual([p["event"] for p in fake.posted], ["APPROVE"])

    def test_a_newer_approval_landing_during_the_post_takes_the_block_back(self):
        fake = FakeGitHub(head="d" * 40)
        with mock.patch.object(aa, "own_approval_on", side_effect=[False, True]):
            rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "superseded"))
        self.assertEqual([p["event"] for p in fake.posted], ["REQUEST_CHANGES"])
        self.assertEqual(fake.dismissed, ["555"])
        self.assertEqual(fake.dismiss_messages, [aa.NEWER_APPROVAL_MESSAGE])

    def test_a_late_superseded_decide_does_not_veto_a_newer_approval(self):
        newer = "d" * 40
        fake = FakeGitHub(head=newer, reviews=[prior_approval(sha=newer, rid=43)])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "superseded"))
        self.assertEqual(fake.posted, [])
        self.assertEqual(fake.dismissed, [])

    def test_a_non_approval_leaves_a_standing_request_for_changes(self):
        block = {"id": 77, "state": "CHANGES_REQUESTED", "user": {"login": LOGIN},
                 "body": aa.APPROVE_MARKER + "\nNo decision this round."}
        fake = FakeGitHub(reviews=[block])
        rc, outcome = self.run_cmd(fake, {"correctness": "red", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "not_approved"))
        # Replaced, not stacked: the new block (555) is posted, then the old one goes.
        self.assertEqual([p["event"] for p in fake.posted], ["REQUEST_CHANGES"])
        self.assertEqual(fake.dismissed, ["77"])
        self.assertEqual(fake.dismiss_messages, [aa.STANDING_REPLACED_MESSAGE])

    def test_one_red_blocks_and_withdraws_prior_approval(self):
        fake = FakeGitHub(reviews=[prior_approval()])
        rc, outcome = self.run_cmd(fake, {"correctness": "red", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "not_approved"))
        self.assertEqual([p["event"] for p in fake.posted], ["REQUEST_CHANGES"])
        self.assertEqual(fake.dismissed, ["42"])

    def test_missing_axis_blocks(self):
        fake = FakeGitHub()
        rc, outcome = self.run_cmd(fake, {"correctness": "green"}, event="APPROVE")
        self.assertEqual((rc, outcome), (0, "not_approved"))
        self.assertEqual([p["event"] for p in fake.posted], ["REQUEST_CHANGES"])

    def test_head_moved_approves_nothing_and_blocks(self):
        fake = FakeGitHub(head="d" * 40, reviews=[prior_approval()])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "superseded"))
        self.assertEqual([p["event"] for p in fake.posted], ["REQUEST_CHANGES"])
        self.assertEqual(fake.dismissed, ["42"])

    def test_head_moved_during_post_withdraws(self):
        fake = FakeGitHub(head_after="d" * 40)
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "superseded"))
        self.assertEqual(fake.dismissed, ["555"])

    def test_needs_human_review_label_approves_nothing_and_blocks(self):
        fake = FakeGitHub(labels=[aa.HUMAN_REVIEW_LABEL])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "needs_human"))
        self.assertEqual([p["event"] for p in fake.posted], ["REQUEST_CHANGES"])

    # --- the skip-cursor-review veto, re-read live at decide time ---

    def test_skip_label_vetoes_all_green_and_withdraws_prior_approval(self):
        fake = FakeGitHub(labels=["bug", aa.SKIP_REVIEW_LABEL], reviews=[prior_approval()])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"},
                                   threshold="low", poster="cr-bot[bot]")
        self.assertEqual((rc, outcome), (0, "vetoed"))
        self.assertEqual(fake.posted, [])
        self.assertEqual(fake.dismissed, ["42"])
        self.assertEqual(self.resolve_calls, [])

    def run_with_event_labels(self, fake, event_labels):
        with tempfile.TemporaryDirectory() as tmp:
            event = os.path.join(tmp, "event.json")
            with open(event, "w", encoding="utf-8") as f:
                json.dump({"action": "labeled", "label": {"name": "cursor-review"},
                           "pull_request": {"number": 1, "head": {"sha": SHA},
                                            "labels": [{"name": n} for n in event_labels]}}, f)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": event}):
                return self.run_cmd(fake, {"correctness": "green", "conformance": "green"})

    def test_skip_label_is_read_live_not_from_the_event_payload(self):
        # Live and payload labels disagree in both directions; only the live
        # read may decide. The run started on `labeled: cursor-review` and the
        # veto landed mid-axes: only the live read carries it.
        fake = FakeGitHub(labels=["cursor-review", aa.SKIP_REVIEW_LABEL])
        self.assertEqual(self.run_with_event_labels(fake, ["cursor-review"]), (0, "vetoed"))
        self.assertEqual(fake.posted, [])
        # The veto was removed mid-axes: a stale payload must not keep it.
        fake = FakeGitHub(labels=["cursor-review"])
        self.assertEqual(self.run_with_event_labels(fake, ["cursor-review", aa.SKIP_REVIEW_LABEL]),
                         (0, "approved"))
        self.assertEqual(len(fake.posted), 1)

    def test_skip_label_outranks_a_moved_head(self):
        # A late decide seeing both a moved head and the veto must withdraw the
        # approval already standing on the live head, not only stale ones.
        fake = FakeGitHub(head="d" * 40, labels=[aa.SKIP_REVIEW_LABEL],
                          reviews=[prior_approval(), prior_approval("d" * 40, 77)])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "vetoed"))
        self.assertEqual(fake.posted, [])
        self.assertEqual(sorted(fake.dismissed), ["42", "77"])

    def test_malformed_label_entries_fail_closed(self):
        # Entries `has_label` would skip cannot rule the veto out.
        for labels in (["skip-cursor-review"], [{"name": None}], [{"name": "bug"}, 7]):
            fake = FakeGitHub(pr_override={"head": {"sha": SHA}, "base": {"ref": "main"}, "labels": labels})
            rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
            self.assertEqual((rc, outcome), (1, "error"), labels)
            self.assertEqual([p["event"] for p in fake.posted], ["REQUEST_CHANGES"], labels)

    def test_skip_label_match_is_exact(self):
        fake = FakeGitHub(labels=["skip-cursor-review-later", "no-skip-cursor-review"])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(len(fake.posted), 1)

    def test_label_read_failure_approves_nothing_and_blocks(self):
        fake = FakeGitHub(read_error=True, reviews=[prior_approval()])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (1, "error"))
        self.assertEqual([p["event"] for p in fake.posted], ["REQUEST_CHANGES"])
        self.assertEqual(fake.dismissed, ["42"])

    def test_missing_label_list_fails_closed(self):
        # A PR payload with no `labels` cannot prove the veto absent.
        for labels in (None, "skip-cursor-review", 1):
            pr = {"head": {"sha": SHA}, "base": {"ref": "main"}}
            if labels is not None:
                pr["labels"] = labels
            fake = FakeGitHub(pr_override=pr)
            rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
            self.assertEqual((rc, outcome), (1, "error"), labels)
            self.assertEqual([p["event"] for p in fake.posted], ["REQUEST_CHANGES"], labels)

    def test_skip_label_applied_during_post_withdraws(self):
        fake = FakeGitHub(labels_after=[aa.SKIP_REVIEW_LABEL])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"},
                                   threshold="low", poster="cr-bot[bot]")
        self.assertEqual((rc, outcome), (0, "vetoed"))
        self.assertEqual(len(fake.posted), 1)
        self.assertEqual(fake.dismissed, ["555"])
        self.assertEqual(fake.dismiss_messages, [aa.SKIP_REVIEW_MESSAGE])
        self.assertEqual(self.resolve_calls, [])

    def test_skip_label_during_post_withdraws_every_own_approval(self):
        # The veto is PR-wide: an approval this identity already has standing on
        # the same head goes too, not only the one just posted.
        fake = FakeGitHub(labels_after=[aa.SKIP_REVIEW_LABEL], head_after="d" * 40,
                          reviews=[prior_approval(SHA, 77)])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
        self.assertEqual((rc, outcome), (0, "vetoed"))
        self.assertEqual(fake.dismissed, ["555", "77"])

    def test_unreadable_labels_after_post_withdraws_under_its_own_cause(self):
        fake = FakeGitHub(pr_after={"head": {"sha": SHA}, "base": {"ref": "main"}},
                          reviews=[prior_approval(SHA, 77)])
        rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"},
                                   threshold="low", poster="cr-bot[bot]")
        self.assertEqual((rc, outcome), (1, "error"))
        self.assertEqual(fake.dismissed, ["555", "77"])
        self.assertEqual(fake.dismiss_messages, [aa.LABELS_UNREADABLE_MESSAGE] * 2)
        self.assertEqual(self.resolve_calls, [])

    def test_post_without_a_review_id_withdraws_and_fails(self):
        for reply in ("<html>proxy error</html>", "null", "{}"):
            fake = FakeGitHub(post_reply=reply, reviews=[prior_approval(SHA, 77)])
            rc, outcome = self.run_cmd(fake, {"correctness": "green", "conformance": "green"})
            self.assertEqual((rc, outcome), (1, "error"), reply)
            self.assertEqual(fake.dismissed, ["77"], reply)

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
        self.assertEqual(self.resolve_calls, [("o/r", "1", "cr-bot[bot]", "low", SHA, False)])

    def test_approve_scope_reaches_the_resolver_as_honour_non_gating(self):
        # Only cursor-review's `approve_scope_effective` == delta honours the
        # non-gating marker; absent, empty, `full` or an unknown value do not.
        for extra, honour in (({}, False), ({"approve_scope": ""}, False), ({"approve_scope": "full"}, False),
                              ({"approve_scope": "bogus"}, False), ({"approve_scope": "delta"}, True),
                              ({"approve_scope": "DELTA"}, True)):
            with self.subTest(extra=extra):
                with mock.patch("builtins.print"):
                    rc, outcome = self.run_cmd(FakeGitHub(), {"correctness": "green", "conformance": "green"},
                                               threshold="low", poster="cr-bot[bot]", **extra)
                self.assertEqual((rc, outcome), (0, "approved"))
                self.assertEqual(self.resolve_calls, [("o/r", "1", "cr-bot[bot]", "low", SHA, honour)])

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
