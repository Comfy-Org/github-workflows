#!/usr/bin/env python3
"""The card after a push dismisses the approval, and the round it can start.

WHY IT EXISTS. The approver approves head X and the card says `pass` for X. A
push to Y dismisses that approval, and then nothing starts the next round on a
label-only caller — while the card still says "Approved", which a human reads
as an approval GitHub no longer counts, and an agent reads as "nothing to
re-run". So `dismiss-stale` now rewrites the card (`mark_card_stale`), and
with `auto_rereview_after_dismissal` the `auto-rereview` job relabels after a
debounce (`cmd_auto_rereview`), once per head, never past the cap.

Everything GitHub says is one fake (`FakeGitHub`), so each case reads as the PR
state it describes. Run:
python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py' -v
"""

import argparse
import importlib.util
import json
import os
import re
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(__file__)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AA = _load(os.path.join(HERE, "..", "auto-approve.py"), "auto_approve_rereview")
CARD = _load(os.path.join(HERE, "..", "..", "cursor-approve", "card.py"), "card_rereview")
WORKFLOW = os.path.join(HERE, "..", "..", "workflows", "cursor-review.yml")

X, Y, Z = "a" * 40, "b" * 40, "c" * 40
LOGIN = "cursor-approver"
POSTER = "cursor-bot[bot]"
CONSOLIDATED = AA._load_post_review().CONSOLIDATED_MARKER


def approval(rid, sha, state="APPROVED", base="main"):
    return {"id": rid, "user": {"login": LOGIN}, "state": state,
            "body": AA.render_body(AA.APPROVE, ["ok"], "low", [], sha, base)}


def round_review(rid):
    return {"id": rid, "user": {"login": POSTER}, "state": "COMMENTED",
            "body": f"{CONSOLIDATED}\n\nfindings", "submitted_at": f"2026-10-12T0{rid}:00:00Z"}


def pass_card(sha=X, round_no=2):
    body = CARD.render_round(round_no, 5, sha, "pass", "none", "Approved: no finding above `low`.", [], "", "")
    return CARD.with_axes_history(body, "", {"round": 1, "commit_sha": sha, "axes": {
        "design": {"verdict": "green", "headline": "Fine.", "summary": ""}}}, 3)


def graphql(reviews):
    nodes = []
    for r in reviews:
        login = r["user"]["login"]
        bot = login.endswith("[bot]")
        nodes.append({"fullDatabaseId": str(r["id"]), "databaseId": r["id"], "state": r["state"],
                      "body": r["body"], "lastEditedAt": None, "submittedAt": r.get("submitted_at"), "url": "",
                      "author": {"__typename": "Bot" if bot else "User",
                                 "login": login[:-5] if bot else login}})
    return json.dumps({"data": {"repository": {"pullRequest": {"reviews": {
        "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}})


class FakeGitHub:
    def __init__(self, head=Y, card=None, reviews=None, labels=(), draft=False, author="dev",
                 timeline=None, card_updated="2026-10-12T10:00:00Z", fail_patch=False):
        self.pr = {"state": "open", "draft": draft, "user": {"login": author},
                   "head": {"sha": head}, "base": {"ref": "main"},
                   "labels": [{"name": n} for n in labels]}
        self.card = None if card is None else {"id": 77, "user": {"login": LOGIN}, "body": card,
                                               "updated_at": card_updated, "html_url": "https://github.com/o/r#c"}
        self.reviews = list(reviews or [])
        self.timeline = list(timeline or [])
        self.fail_patch = fail_patch
        self.patches, self.labels = [], []

    def __call__(self, args, payload=None):
        if args[:2] == ["api", "graphql"]:
            return graphql(self.reviews)
        if args[:3] == ["api", "--paginate", "--slurp"]:
            if "/comments" in args[3]:
                return json.dumps([[self.card]] if self.card else [[]])
            if "/timeline" in args[3]:
                return json.dumps([self.timeline])
        if args == ["api", "repos/o/r/pulls/1"]:
            return json.dumps(self.pr)
        if args[:3] == ["api", "-X", "PATCH"]:
            if self.fail_patch:
                raise RuntimeError("gh api failed: HTTP 502")
            self.patches.append(payload["body"])
            self.card["body"] = payload["body"]
            return json.dumps(self.card)
        if args[:3] == ["api", "-X", "PUT"]:
            return "{}"
        if args[:3] in (["api", "-X", "DELETE"], ["api", "-X", "POST"]) and "/labels" in args[3]:
            self.labels.append(args[2])
            return "{}"
        raise AssertionError(f"unexpected gh call {args}")


class Base(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.NamedTemporaryFile("w+", delete=False)
        self.addCleanup(os.unlink, self.out.name)
        self.printed = []

    def outputs(self):
        with open(self.out.name, encoding="utf-8") as f:
            return dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)

    def run_with(self, fake, fn, args):
        with mock.patch.object(AA, "gh", fake), mock.patch.object(AA, "emit", lambda *a: None), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": self.out.name}), \
                mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
            return fn(args)


class DismissalCardTest(Base):
    def dismiss(self, fake, auto="false", threshold="low", authors="", max_rounds="5", run_without_label="false",
                can_relabel="true"):
        args = argparse.Namespace(
            repo="o/r", pr_number="1", head_sha=fake.pr["head"]["sha"], approver_login=LOGIN, all_approvals=False,
            threshold=threshold, approve_authors=authors, max_rounds=max_rounds, poster_login=POSTER,
            review_label="cursor-review", auto_rereview=auto, auto_rereview_delay_minutes="10",
            can_relabel=can_relabel, run_without_label=run_without_label)
        return self.run_with(fake, AA.cmd_dismiss_stale, args)

    def test_a_dismissal_below_the_cap_marks_the_card_stale_with_both_shas(self):
        fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X), round_review(2)])
        self.assertEqual(self.dismiss(fake), 0)
        body = fake.patches[-1]
        self.assertEqual(CARD.card_state(body), "no_decision")
        self.assertIn("<!-- cursor-approve-next: relabel -->", body)
        self.assertIn(f"Approved `{X[:7]}`; the head moved to `{Y[:7]}`, which needs its own round.", body)
        # The round line and the axes history survive the rewrite.
        self.assertIn(f"### Cursor approve · round 2 of 5 · `{X[:7]}`", body)
        self.assertEqual(len(CARD.read_axes_sentinel(body)[0]), 1)
        self.assertEqual(CARD.stale_head(body), Y)
        self.assertEqual(CARD.reviewed_sha(body), X)
        self.assertEqual(self.outputs().get("auto_rereview"), "false")

    def test_a_dismissal_at_the_cap_hands_the_card_to_a_human(self):
        fake = FakeGitHub(card=pass_card(round_no=5), reviews=[approval(1, X)] + [round_review(i) for i in range(2, 7)])
        self.dismiss(fake, auto="true")
        body = fake.patches[-1]
        self.assertEqual(CARD.card_state(body), "capped")
        self.assertIn("<!-- cursor-approve-next: human -->", body)
        self.assertIn("round limit is reached", body)
        self.assertEqual(self.outputs().get("auto_rereview"), "false")

    def test_needs_human_review_is_capped_too(self):
        fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X)], labels=[AA.HUMAN_REVIEW_LABEL])
        self.dismiss(fake, auto="true")
        self.assertEqual(CARD.card_state(fake.patches[-1]), "capped")
        self.assertEqual(self.outputs().get("auto_rereview"), "false")

    def test_an_approval_the_ruleset_already_dismissed_still_counts(self):
        fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X, state="DISMISSED")])
        self.dismiss(fake)
        self.assertEqual(CARD.card_state(fake.patches[-1]), "no_decision")

    def test_a_dismissed_request_for_changes_is_not_an_approval(self):
        block = {"id": 1, "user": {"login": LOGIN}, "state": "DISMISSED",
                 "body": AA.render_body(AA.REQUEST_CHANGES, ["bad"], "low", [], X, "main")}
        fake = FakeGitHub(card=pass_card(), reviews=[block])
        self.dismiss(fake)
        self.assertEqual(fake.patches, [])

    def test_a_pass_card_without_an_approval_is_a_round_in_progress(self):
        # The pending and start cards also say `pass`: never rewrite those.
        fake = FakeGitHub(card=CARD.render_pending(1, 5, X, ""), reviews=[])
        self.dismiss(fake)
        self.assertEqual(fake.patches, [])

    def test_an_approval_still_on_the_head_leaves_the_card(self):
        fake = FakeGitHub(head=X, card=pass_card(), reviews=[approval(1, X)])
        self.dismiss(fake)
        self.assertEqual(fake.patches, [])

    def test_a_retarget_alone_asks_for_a_push(self):
        fake = FakeGitHub(head=X, card=pass_card(), reviews=[approval(1, X, base="old")])
        self.dismiss(fake, auto="true")
        body = fake.patches[-1]
        self.assertIn("<!-- cursor-approve-next: push_then_relabel -->", body)
        self.assertEqual(self.outputs().get("auto_rereview"), "false")
        # And it is stable: the next event on that head writes nothing.
        fake.patches.clear()
        self.dismiss(fake, auto="true")
        self.assertEqual(fake.patches, [])

    def test_only_the_approval_lane_gets_a_card(self):
        for kwargs in ({"threshold": ""}, {"authors": "someone-else"}):
            fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X)])
            self.dismiss(fake, **kwargs)
            self.assertEqual(fake.patches, [], kwargs)
        fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X)], author="Dev")
        self.dismiss(fake, authors="@dev, other")
        self.assertEqual(len(fake.patches), 1)

    def test_flag_off_rewrites_the_card_only(self):
        fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X)])
        self.dismiss(fake, auto="false")
        self.assertIn("Re-run the round", fake.patches[-1])
        self.assertEqual(self.outputs().get("auto_rereview"), "false")
        self.assertEqual(fake.labels, [])

    def test_flag_on_schedules_the_round_and_says_so(self):
        fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X)])
        self.dismiss(fake, auto="true")
        # The start time, from the moment the dismissal scheduled the wait.
        self.assertRegex(fake.patches[-1], r"a new round starts automatically at \d\d:\d\d UTC "
                                           r"(on \d{4}-\d\d-\d\d )?\(10 min after the latest push\)")
        self.assertIn("<!-- cursor-approve-next: relabel -->", fake.patches[-1])
        self.assertEqual(self.outputs().get("auto_rereview"), "true")
        self.assertEqual(self.outputs().get("rereview_head"), Y)
        self.assertEqual(fake.labels, [])  # the dismiss job never relabels itself

    def test_no_schedule_for_a_draft_a_veto_a_label_free_caller_or_no_relabel_token(self):
        cases = [({"draft": True}, {}), ({"labels": [AA.SKIP_REVIEW_LABEL]}, {}),
                 ({}, {"run_without_label": "true"}), ({}, {"can_relabel": "false"})]
        for fake_kw, dismiss_kw in cases:
            fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X)], **fake_kw)
            self.dismiss(fake, auto="true", **dismiss_kw)
            self.assertEqual(self.outputs().get("auto_rereview"), "false", (fake_kw, dismiss_kw))
            self.assertTrue(fake.patches, (fake_kw, dismiss_kw))

    def test_the_same_head_is_written_once_and_a_newer_one_rewrites(self):
        fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X)])
        self.dismiss(fake)
        self.dismiss(fake)
        self.assertEqual(len(fake.patches), 1)
        fake.pr["head"]["sha"] = Z
        self.dismiss(fake)
        self.assertEqual(len(fake.patches), 2)
        self.assertIn(f"Approved `{X[:7]}`; the head moved to `{Z[:7]}`", fake.patches[-1])
        self.assertEqual(CARD.stale_head(fake.patches[-1]), Z)

    def test_a_head_already_auto_rereviewed_is_left_to_its_round(self):
        fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X)])
        self.dismiss(fake, auto="true")
        fake.card["body"] = CARD.mark_auto_rereview(fake.card["body"], Y)
        fake.patches.clear()
        self.dismiss(fake, auto="true")
        self.assertEqual(fake.patches, [])
        self.assertEqual(self.outputs().get("auto_rereview"), "false")

    def test_a_failed_card_write_is_a_warning_and_schedules_nothing(self):
        fake = FakeGitHub(card=pass_card(), reviews=[approval(1, X)], fail_patch=True)
        self.assertEqual(self.dismiss(fake, auto="true"), 0)
        self.assertTrue(any("::warning::" in p for p in self.printed))
        self.assertEqual(self.outputs().get("auto_rereview"), "false")


class AutoRereviewTest(Base):
    def stale_card(self, head=Y):
        body = CARD.render_dismissed(pass_card(), X, head, 5, False, "x", "")
        return CARD.with_axes_history(body, pass_card())

    def rereview(self, fake, head=Y, max_rounds="5"):
        args = argparse.Namespace(repo="o/r", pr_number="1", head_sha=head, approver_login=LOGIN,
                                  poster_login=POSTER, review_label="cursor-review", max_rounds=max_rounds)
        return self.run_with(fake, AA.cmd_auto_rereview, args)

    def test_fires_once_after_the_delay_when_the_head_is_unchanged(self):
        fake = FakeGitHub(card=self.stale_card(), reviews=[approval(1, X)])
        self.assertEqual(self.rereview(fake), 0)
        self.assertEqual(fake.labels, ["DELETE", "POST"])
        self.assertEqual(CARD.rereview_heads(fake.card["body"]), [Y])
        self.assertIn("was started automatically", fake.card["body"])
        # A second waiter (another event scheduled one too) finds the record.
        self.assertEqual(self.rereview(fake), 0)
        self.assertEqual(fake.labels, ["DELETE", "POST"])

    def test_a_newer_push_during_the_wait_means_no_relabel(self):
        fake = FakeGitHub(head=Z, card=self.stale_card(), reviews=[approval(1, X)])
        self.rereview(fake, head=Y)
        self.assertEqual((fake.labels, fake.patches), ([], []))

    def test_nothing_fires_in_flight_capped_draft_or_vetoed(self):
        in_flight = [{"event": "labeled", "label": {"name": "cursor-review"}, "created_at": "2026-10-12T10:05:00Z"}]
        cases = {
            "in flight": FakeGitHub(card=self.stale_card(), timeline=in_flight),
            "capped": FakeGitHub(card=self.stale_card(), reviews=[round_review(i) for i in range(1, 6)]),
            "draft": FakeGitHub(card=self.stale_card(), draft=True),
            "skip label": FakeGitHub(card=self.stale_card(), labels=[AA.SKIP_REVIEW_LABEL]),
            "human label": FakeGitHub(card=self.stale_card(), labels=[AA.HUMAN_REVIEW_LABEL]),
            "closed": FakeGitHub(card=self.stale_card()),
            "a round rewrote the card": FakeGitHub(card=pass_card(Y)),
            "already started for this head": FakeGitHub(card=CARD.mark_auto_rereview(self.stale_card(), Y)),
        }
        cases["closed"].pr["state"] = "closed"
        for name, fake in cases.items():
            self.assertEqual(self.rereview(fake), 0, name)
            self.assertEqual((fake.labels, fake.patches), ([], []), name)

    def test_a_label_applied_before_the_card_was_written_is_not_in_flight(self):
        earlier = [{"event": "labeled", "label": {"name": "cursor-review"}, "created_at": "2026-10-12T09:00:00Z"}]
        fake = FakeGitHub(card=self.stale_card(), timeline=earlier)
        self.rereview(fake)
        self.assertEqual(fake.labels, ["DELETE", "POST"])

    def test_no_record_no_relabel(self):
        fake = FakeGitHub(card=self.stale_card(), fail_patch=True)
        self.assertEqual(self.rereview(fake), 0)
        self.assertEqual(fake.labels, [])


class CardMarkersTest(unittest.TestCase):
    def test_the_auto_rereview_record_survives_a_later_round_card(self):
        stale = CARD.mark_auto_rereview(CARD.render_dismissed(pass_card(), X, Y, 5, False, "x", ""), Y)
        later = CARD.render_round(3, 5, Y, "pass", "none", "Approved.", [], "", "")
        carried = CARD.with_rereview_history(later, stale)
        self.assertEqual(CARD.rereview_heads(carried), [Y])
        self.assertEqual(CARD.stale_head(carried), "")  # a round's card is not stale

    def test_the_record_is_bounded(self):
        body = ""
        heads = [f"{i:040x}" for i in range(30)]
        for h in heads:
            body = CARD.mark_auto_rereview(body or CARD.render_dismissed("", X, h, 5, False, "x", ""), h)
        self.assertEqual(CARD.rereview_heads(body), heads[-CARD.REREVIEW_HEADS_KEPT:])
        self.assertEqual(len(re.findall("cursor-approve:auto-rereview", body)), 1)

    def test_every_dismissed_card_is_a_valid_contract(self):
        for capped, same, vetoed in [(c, s, v) for c in (0, 1) for s in (0, 1) for v in (0, 1)]:
            body = CARD.render_dismissed(pass_card(), X, Y, 5, bool(capped), "x", "", bool(same), bool(vetoed))
            self.assertIn(CARD.card_state(body), CARD.STATES)
            self.assertTrue(body.startswith(CARD.CARD_MARKER))

    def test_both_approval_renderers_match_the_dismissed_approval_test(self):
        for body in (AA.render_body(AA.APPROVE, ["ok"], "low", [], X, "main"),
                     AA.render_external_body("", X, "main")):
            self.assertTrue(any(line in body for line in AA.APPROVAL_BODY_LINES), body)
        for body in (AA.render_body(AA.REQUEST_CHANGES, ["no"], "low", [], X, "main"),
                     AA.render_standing_body("No decision.", [], "Re-run.", "low", X, "main")):
            self.assertFalse(any(line in body for line in AA.APPROVAL_BODY_LINES), body)

    def test_author_opt_in_mirrors_the_gate(self):
        self.assertTrue(AA.author_opted_in("", "anyone"))
        self.assertTrue(AA.author_opted_in("  ", "anyone"))
        self.assertTrue(AA.author_opted_in("@Dev, other", "dev"))
        self.assertFalse(AA.author_opted_in("other", "dev"))
        self.assertFalse(AA.author_opted_in(",", "dev"))
        self.assertFalse(AA.author_opted_in("@", "dev"))
        self.assertFalse(AA.author_opted_in("dev", ""))


class WorkflowWiringTest(unittest.TestCase):
    def setUp(self):
        with open(WORKFLOW, encoding="utf-8") as f:
            self.text = f.read()

    def test_inputs_default_off_and_ten_minutes(self):
        for name, default in (("auto_rereview_after_dismissal", "false"), ("auto_rereview_delay_minutes", "10")):
            block = self.text[self.text.index(f"      {name}:\n"):]
            block = block[:block.index("\n      ", block.index("default:"))]
            self.assertIn(f"default: {default}", block, name)

    def test_the_rereview_job_runs_only_when_the_dismiss_job_asks(self):
        job = self.text[self.text.index("  auto-rereview:\n"):]
        self.assertIn("needs: dismiss-stale-approval", job)
        self.assertIn("needs.dismiss-stale-approval.outputs.auto_rereview == 'true'", job)
        self.assertIn("auto-approve.py\" auto-rereview", job)
        self.assertIn("--head-sha \"$HEAD_SHA\"", job)

    def test_approver_token_promises_a_round_only_where_auto_rereview_accepts_it(self):
        # auto-rereview exits 1 on APPROVER_TOKEN off pull_request_target, so
        # the dismiss job must not schedule (or promise on the card) that round.
        step = self.text[self.text.index("          CAN_RELABEL=false\n          if [ -n \"$APPROVER_TOKEN\" ]; then"):]
        approver = step[:step.index("          elif [ -n \"$BOT_TOKEN\" ]; then")]
        self.assertIn('if [ "$EVENT_NAME" = "pull_request_target" ]; then CAN_RELABEL=true; fi', approver)
        self.assertNotIn("            CAN_RELABEL=true\n", approver)
        job = self.text[self.text.index("  auto-rereview:\n"):]
        self.assertIn('if [ -n "$APPROVER_TOKEN" ] && [ "$EVENT_NAME" != "pull_request_target" ]; then', job)

    def test_the_delay_clamp_matches_the_card_sentence(self):
        job = self.text[self.text.index("  auto-rereview:\n"):]
        self.assertIn(f"if [ \"$mins\" -gt {CARD.RETRY_DELAY_CAP} ]; then mins={CARD.RETRY_DELAY_CAP}; fi", job)


if __name__ == "__main__":
    unittest.main()
