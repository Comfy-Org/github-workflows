#!/usr/bin/env python3
"""`approve_only_on_push`: a push past a passing panel re-runs the axes alone.

WHY IT EXISTS. On a label-gated caller a push after a round starts nothing (or,
with `auto_rereview_after_dismissal`, a whole new panel round — and only when
the push dismissed an approval). When the round's PANEL passed and only the
axes held the approval back, the fix commit is re-judged by the axes alone:
cursor-review's `approve-only` job sets `approve_gate` to `pass` with no panel,
and the caller's cursor-approve jobs judge the pushed head. These cases pin
when that is allowed — read only from the approver's own card and the live PR
— and how the card records it.

GitHub is one fake (test_dismissal_rereview's `FakeGitHub`). Run:
python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py' -v
"""

import argparse
import importlib.util
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


AA = _load(os.path.join(HERE, "..", "auto-approve.py"), "auto_approve_approve_only")
CARD = _load(os.path.join(HERE, "..", "..", "cursor-approve", "card.py"), "card_approve_only")
REREVIEW = _load(os.path.join(HERE, "test_dismissal_rereview.py"), "rereview_harness_approve_only")
FakeGitHub = REREVIEW.FakeGitHub
WORKFLOW = os.path.join(HERE, "..", "..", "workflows", "cursor-review.yml")
APPROVE_WORKFLOW = os.path.join(HERE, "..", "..", "workflows", "cursor-approve.yml")

X, Y, Z = "a" * 40, "b" * 40, "c" * 40
LOGIN = REREVIEW.LOGIN
POSTER = REREVIEW.POSTER
AXES = ["correctness", "conformance"]
DECIDED_AT = "2026-10-12T08:00:00Z"


def red_card(sha=X, round_no=2):
    """The decide card of a round whose panel passed and whose axes said red."""
    decision = {"axes": {"correctness": {"verdict": "red", "headline": "A bug."},
                         "conformance": {"verdict": "green", "headline": "Fine."}}}
    return CARD.render_decide(round_no, 5, sha, AXES, decision, CARD.OUTCOME_NOT_APPROVED, "0", "")


def block(rid=9, sha=X, state="CHANGES_REQUESTED", at=DECIDED_AT):
    """The approver's standing request for changes — its last decision."""
    return {"id": rid, "user": {"login": LOGIN}, "state": state, "submitted_at": at, "body": "blocked", "commit_id": sha}


def labelled(at):
    return {"event": "labeled", "label": {"name": "cursor-review"}, "created_at": at}


class Base(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.NamedTemporaryFile("w+", delete=False)
        self.addCleanup(os.unlink, self.out.name)
        self.printed = []

    def outputs(self):
        with open(self.out.name, encoding="utf-8") as f:
            return dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)

    def run_stage(self, fake, stage, head=Y, threshold="medium", authors="", max_runs="10", threads=()):
        args = argparse.Namespace(stage=stage, repo="o/r", pr_number="1", head_sha=head, approver_login=LOGIN,
                                  review_label="", threshold=threshold, approve_authors=authors,
                                  max_runs=max_runs, delay_minutes="10")
        reader = (mock.Mock(side_effect=RuntimeError("graphql 502")) if threads is None
                  else mock.Mock(return_value=list(threads)))
        with mock.patch.object(AA, "gh", fake), mock.patch.object(CARD, "gh", fake), \
                mock.patch.object(AA, "_load_card", lambda: CARD), \
                mock.patch.object(AA, "open_thread_severities", reader), \
                mock.patch.object(AA, "emit", lambda *a: None), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": self.out.name}), \
                mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
            return AA.cmd_approve_only(args)

    def fake(self, card=None, labels=("cursor-review",), reviews=None, **kw):
        return FakeGitHub(card=red_card() if card is None else card, labels=labels,
                          reviews=[block()] if reviews is None else reviews, **kw)


class EligibilityTest(Base):
    def test_a_red_axis_round_is_re_checked_on_the_next_push(self):
        fake = self.fake()
        self.assertEqual(self.run_stage(fake, "check"), 0)
        self.assertEqual(self.outputs()["eligible"], "true")
        self.assertIn("⏳ _Approver-only re-check of `bbbbbbb` starts", fake.card["body"])
        self.assertEqual(self.run_stage(fake, "start"), 0)
        out = self.outputs()
        self.assertEqual((out["go"], out["round"]), ("true", "2"))
        body = fake.card["body"]
        self.assertEqual(CARD.rechecks_this_round(body), [Y])
        self.assertIn("🔍 _Approver-only re-check 1 of 10 for `bbbbbbb`", body)
        self.assertNotIn("⏳", body)
        self.assertEqual(fake.labels, [], "a re-check never relabels: the panel does not run")

    def test_a_round_whose_panel_did_not_pass_needs_a_full_round(self):
        # cursor-review's own round card (any outcome) carries no lane marker.
        panel = CARD.render_round(2, 5, X, "changes_requested", "resolve_then_relabel",
                                  "Not approved: 1 finding above `medium`.", [], "Fix it.", "")
        self.assertEqual(self.run_stage(self.fake(card=panel), "check"), 0)
        self.assertEqual(self.outputs()["eligible"], "false")

    def test_no_card_or_a_capped_card_is_not_re_checked(self):
        self.run_stage(self.fake(card=""), "check")
        self.assertEqual(self.outputs()["eligible"], "false")
        capped = CARD.render_start(5, 5, X, AXES, "capped", "")
        self.assertNotIn(CARD.LANE_MARKER, capped)
        self.run_stage(self.fake(card=capped), "check")
        self.assertEqual(self.outputs()["eligible"], "false")

    def test_the_live_pr_must_still_want_it(self):
        cases = {
            "draft": dict(draft=True),
            "no review label": dict(labels=()),
            "veto": dict(labels=("cursor-review", "skip-cursor-review")),
            "hand-off": dict(labels=("cursor-review", "needs-human-review")),
            "head moved": dict(head=Z),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self.run_stage(self.fake(**kw), "check")
                self.assertEqual(self.outputs()["eligible"], "false", name)

    def test_auto_approve_must_be_on_for_this_author(self):
        self.run_stage(self.fake(), "check", threshold="")
        self.assertEqual(self.outputs()["eligible"], "false")
        self.run_stage(self.fake(), "check", authors="someone-else")
        self.assertEqual(self.outputs()["eligible"], "false")

    def test_an_open_thread_above_the_threshold_or_unreadable_threads_block(self):
        self.run_stage(self.fake(), "check", threads=[("high", False)])
        self.assertEqual(self.outputs()["eligible"], "false")
        self.run_stage(self.fake(), "check", threads=[(None, False)])
        self.assertEqual(self.outputs()["eligible"], "false", "an unbadged thread blocks, as decide's")
        self.run_stage(self.fake(), "check", threads=None)
        self.assertEqual(self.outputs()["eligible"], "false")
        self.run_stage(self.fake(), "check", threads=[("medium", False), ("low", True)])
        self.assertEqual(self.outputs()["eligible"], "true", "at or below the threshold does not block")

    def test_a_relabel_since_the_last_decision_is_a_full_round_in_flight(self):
        self.run_stage(self.fake(timeline=[labelled("2026-10-12T09:00:00Z")]), "check")
        self.assertEqual(self.outputs()["eligible"], "false")
        self.run_stage(self.fake(timeline=[labelled("2026-10-12T07:00:00Z")]), "check")
        self.assertEqual(self.outputs()["eligible"], "true", "the label that started the decided round is not in flight")
        self.run_stage(self.fake(reviews=[]), "check")
        self.assertEqual(self.outputs()["eligible"], "false", "no readable last decision fails closed")

    def test_a_head_a_panel_already_reviewed_or_an_approval_already_on_it(self):
        self.run_stage(self.fake(reviews=[block(), REREVIEW.round_review(5, Y)]), "check")
        self.assertEqual(self.outputs()["eligible"], "false")
        self.run_stage(self.fake(reviews=[block(), REREVIEW.approval(10, Y)]), "check")
        self.assertEqual(self.outputs()["eligible"], "false", "a carried-forward approval already stands")

    def test_the_start_stage_re_reads_everything(self):
        fake = self.fake()
        self.run_stage(fake, "check")
        self.assertEqual(self.outputs()["eligible"], "true")
        fake.pr["head"]["sha"] = Z  # a newer push landed during the wait
        self.run_stage(fake, "start")
        self.assertEqual(self.outputs()["go"], "false")
        self.assertEqual(CARD.rechecks_this_round(fake.card["body"]), [])

    def test_a_failed_record_means_no_re_check(self):
        fake = self.fake()
        fake.fail_patch = True
        self.run_stage(fake, "start")
        self.assertEqual(self.outputs()["go"], "false")


class CapTest(Base):
    def test_one_re_check_per_head_and_at_most_the_cap_per_panel_round(self):
        fake = self.fake()
        self.run_stage(fake, "start")
        self.assertEqual(self.outputs()["go"], "true")
        self.run_stage(fake, "start")
        self.assertEqual(self.outputs()["go"], "false", "the same head is never re-checked twice")
        fake.pr["head"]["sha"] = Z
        self.run_stage(fake, "start", head=Z, max_runs="1")
        self.assertEqual(self.outputs()["go"], "false", "at the cap a push is left to a relabel")
        self.run_stage(fake, "start", head=Z, max_runs="2")
        self.assertEqual(self.outputs()["go"], "true")
        self.assertEqual(CARD.rechecks_this_round(fake.card["body"]), [Y, Z])

    def test_an_unparsable_cap_re_checks_nothing(self):
        self.run_stage(self.fake(), "check", max_runs="ten")
        self.assertEqual(self.outputs()["eligible"], "false")

    def test_a_new_panel_round_resets_the_count(self):
        body = CARD.mark_recheck_started(red_card(round_no=2), "2", 1, Y)
        self.assertEqual(CARD.rechecks_this_round(body), [Y])
        # Round 3's card replaces it; upsert carries the record, which no
        # longer names the card's round.
        round3 = CARD.with_recheck_history(red_card(sha=Z, round_no=3), body)
        self.assertEqual(CARD.recheck_record(round3)[2], [Y])
        self.assertEqual(CARD.rechecks_this_round(round3), [])


class CardTest(unittest.TestCase):
    def test_only_cards_past_a_passing_panel_are_in_the_lane(self):
        self.assertTrue(CARD.in_axes_lane(CARD.render_start(2, 5, X, AXES, "pass", "")))
        self.assertTrue(CARD.in_axes_lane(red_card()))
        self.assertTrue(CARD.in_axes_lane(CARD.render_decide(2, 5, X, AXES, {}, CARD.OUTCOME_SUPERSEDED, "0", "")))
        self.assertFalse(CARD.in_axes_lane(CARD.render_decide(2, 5, X, AXES, {}, CARD.OUTCOME_HUMAN, "0", "")))
        self.assertFalse(CARD.in_axes_lane(CARD.render_pending(2, 5, X, "")))

    def test_the_lane_marker_cannot_be_forged_by_model_text(self):
        decision = {"axes": {"correctness": {"verdict": "green", "headline": "x\n" + CARD.LANE_MARKER}}}
        panel = CARD.render_round(2, 5, X, "changes_requested", "relabel", CARD.LANE_MARKER,
                                  [CARD.LANE_MARKER], "", "")
        self.assertFalse(CARD.in_axes_lane(panel))
        body = CARD.render_decide(2, 5, X, AXES, decision, CARD.OUTCOME_HUMAN, "0", "")
        self.assertFalse(CARD.in_axes_lane(body))

    def test_a_dismissed_card_keeps_the_lane_unless_handed_off(self):
        start = CARD.render_start(2, 5, X, AXES, "pass", "")
        self.assertTrue(CARD.in_axes_lane(CARD.render_dismissed(start, X, Y, 5, False, "relabel", "")))
        self.assertFalse(CARD.in_axes_lane(CARD.render_dismissed(start, X, Y, 5, True, "relabel", "")))
        self.assertFalse(CARD.in_axes_lane(CARD.render_dismissed(start, X, Y, 5, False, "x", "", vetoed=True)))
        panel = CARD.render_round(2, 5, X, "pass", "none", "Approved.", [], "", "")
        self.assertFalse(CARD.in_axes_lane(CARD.render_dismissed(panel, X, Y, 5, False, "relabel", "")))

    def test_the_start_and_decide_cards_of_a_re_check_say_so(self):
        existing = CARD.mark_recheck_started(red_card(), "2", 10, Y)
        start = CARD.with_recheck_history(CARD.render_start(2, 5, Y, AXES, "pass", ""), existing, Y)
        self.assertIn("🔍 _Approver-only re-check 1 of 10 for `bbbbbbb`", start)
        self.assertEqual(CARD.card_state(start), "pass")
        # A full round's card for another commit carries the record, no line.
        other = CARD.with_recheck_history(CARD.render_start(3, 5, Z, AXES, "pass", ""), existing, Z)
        self.assertNotIn("🔍", other)
        self.assertEqual(CARD.recheck_record(other)[2], [Y])

    def test_the_recheck_record_survives_cursor_reviews_round_card(self):
        existing = CARD.mark_recheck_started(red_card(), "2", 10, Y)
        panel = CARD.render_round(3, 5, Z, "pass", "none", "Approved.", [], "", "")
        carried = CARD.with_recheck_history(panel, existing)
        self.assertEqual(CARD.recheck_record(carried), ("2", 10, [Y]))

    def test_the_record_is_bounded_and_rejects_junk(self):
        heads = [f"{i:040x}" for i in range(80)]
        line = CARD.recheck_sentinel(2, 10, heads + ["nope"])
        self.assertEqual(CARD.recheck_record(line)[2], heads[-CARD.RECHECK_HEADS_KEPT:])
        self.assertEqual(CARD.recheck_sentinel("?", 10, heads), "")

    def test_every_card_is_still_a_valid_contract(self):
        for body in (CARD.mark_recheck_scheduled(red_card(), Y, 10), CARD.mark_recheck_started(red_card(), "2", 10, Y)):
            self.assertRegex(body, r"\A<!-- cursor-approve-card -->\n<!-- cursor-approve-state: [a-z_]+ -->\n"
                                   r"<!-- cursor-approve-next: [a-z_]+ -->\n")


class PriorRoundsTest(unittest.TestCase):
    def history(self, round_no, commit_sha):
        rounds = [{"round": 1, "commit_sha": Z, "axes": {"correctness": {"verdict": "red", "headline": "Old.", "summary": ""}}},
                  {"round": 2, "commit_sha": X, "axes": {"correctness": {"verdict": "red", "headline": "Bug.", "summary": ""}}}]
        card = {"id": 1, "user": {"login": LOGIN}, "body": CARD.with_axes_history(red_card(), "", None, None)
                + "\n" + CARD.axes_sentinel(rounds) + "\n"}
        args = argparse.Namespace(repo="o/r", pr_number="1", login=LOGIN, round=round_no, prior_rounds="3",
                                  commit_sha=commit_sha)
        with mock.patch.object(CARD, "list_comments", lambda *a: [card]):
            return [(r["round"], r["commit_sha"]) for r in CARD._prior_history(args)]

    def test_a_re_check_sees_its_own_rounds_verdict_on_the_commit_before(self):
        self.assertEqual(self.history("2", Y), [(1, Z), (2, X)])

    def test_a_re_run_on_the_same_commit_still_does_not_see_itself(self):
        self.assertEqual(self.history("2", X), [(1, Z)])
        self.assertEqual(self.history("2", ""), [(1, Z)])

    def test_the_start_phase_passes_the_commit(self):
        with open(APPROVE_WORKFLOW, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('--commit-sha "$COMMIT_SHA"', text)
        self.assertIn("COMMIT_SHA: ${{ inputs.commit_sha }}", text)


class DismissalTextTest(Base):
    def test_a_dismissed_lane_card_announces_the_re_check(self):
        start = CARD.render_start(2, 5, X, AXES, "pass", "")
        fake = FakeGitHub(card=start, reviews=[REREVIEW.approval(10, X)], labels=("cursor-review",))
        args = argparse.Namespace(
            repo="o/r", pr_number="1", head_sha=Y, approver_login=LOGIN, all_approvals=False, threshold="low",
            approve_authors="", max_rounds="5", poster_login=POSTER, review_label="", auto_rereview="true",
            auto_rereview_delay_minutes="10", can_relabel="true", run_without_label="false", carry_forward="false",
            event_name="pull_request_target", approve_only="true", approve_only_delay_minutes="10")
        with mock.patch.object(AA, "gh", fake), mock.patch.object(CARD, "gh", fake), \
                mock.patch.object(AA, "_load_card", lambda: CARD), mock.patch.object(AA, "emit", lambda *a: None), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": self.out.name}), \
                mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
            AA.cmd_dismiss_stale(args)
        body = fake.card["body"]
        self.assertIn("cursor-approve re-checks the new head automatically", body)
        # The fallback leads: the re-check is decided after this card is written.
        self.assertIn("If cursor-approve cannot re-check this push, remove and re-add the `cursor-review` label. "
                      "If it can,", body)
        self.assertTrue(CARD.in_axes_lane(body))
        # The round is still asked for: the workflow stands it down only when
        # `approve-only` actually takes the push.
        self.assertEqual(self.outputs().get("auto_rereview"), "true")


def panel_blocked_card(sha=X, round_no=2):
    """cursor-review's own round card for a round its PANEL blocked."""
    return CARD.render_round(round_no, 5, sha, "changes_requested", "resolve_then_relabel",
                             "Not approved: 1 finding(s) above `medium`.", ["1 finding(s) above `medium`"],
                             CARD.next_resolve_text(), "", [], "medium")


class PanelBlockedRereviewTest(Base):
    """A push on a round the PANEL blocked starts a full round itself — only
    with both `approve_only_on_push` and `auto_rereview_after_dismissal`."""

    def dismiss(self, fake, approve_only="true", auto="true", can_relabel="true", run_without_label="false",
                max_rounds="5"):
        args = argparse.Namespace(
            repo="o/r", pr_number="1", head_sha=fake.pr["head"]["sha"], approver_login=LOGIN, all_approvals=False,
            threshold="medium", approve_authors="", max_rounds=max_rounds, poster_login=POSTER,
            review_label="cursor-review", auto_rereview=auto, auto_rereview_delay_minutes="10",
            can_relabel=can_relabel, run_without_label=run_without_label, carry_forward="false",
            event_name="pull_request_target", approve_only=approve_only, approve_only_delay_minutes="10")
        with mock.patch.object(AA, "gh", fake), mock.patch.object(CARD, "gh", fake), \
                mock.patch.object(AA, "_load_card", lambda: CARD), mock.patch.object(AA, "emit", lambda *a: None), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": self.out.name}), \
                mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
            return AA.cmd_dismiss_stale(args)

    def rereview(self, fake, head=Y):
        args = argparse.Namespace(repo="o/r", pr_number="1", head_sha=head, approver_login=LOGIN,
                                  poster_login=POSTER, review_label="cursor-review", max_rounds="5")
        with mock.patch.object(AA, "gh", fake), mock.patch.object(CARD, "gh", fake), \
                mock.patch.object(AA, "_load_card", lambda: CARD), mock.patch.object(AA, "emit", lambda *a: None), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": self.out.name}), \
                mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
            return AA.cmd_auto_rereview(args)

    def blocked_fake(self, **kw):
        return FakeGitHub(card=panel_blocked_card(), reviews=[block(), REREVIEW.round_review(1, X)],
                          labels=("cursor-review",), **kw)

    def test_only_cursor_reviews_changes_requested_round_card_is_panel_blocked(self):
        self.assertTrue(CARD.panel_blocked(panel_blocked_card()))
        for body in (CARD.render_round(2, 5, X, "no_decision", "push_then_relabel", "No decision.", [], "x", ""),
                     CARD.render_round(2, 5, X, "capped", "human", "Capped.", [], "x", ""),
                     CARD.render_round(2, 5, X, "pass", "none", "Approved.", [], "", ""),
                     red_card()):
            self.assertFalse(CARD.panel_blocked(body))
        forged = CARD.render_round(2, 5, X, "no_decision", "relabel", CARD.PANEL_BLOCKED_MARKER,
                                   [CARD.PANEL_BLOCKED_MARKER], CARD.PANEL_BLOCKED_MARKER, "")
        self.assertFalse(CARD.panel_blocked(forged), "model text cannot forge the marker")

    def test_a_push_schedules_the_full_round_and_says_so(self):
        fake = self.blocked_fake()
        self.dismiss(fake)
        out = self.outputs()
        self.assertEqual((out.get("auto_rereview"), out.get("rereview_head")), ("true", Y))
        body = fake.card["body"]
        self.assertIn("the panel blocked the last round, so a new full round starts automatically", body)
        self.assertIn("<!-- cursor-approve-next: relabel -->", body)
        self.assertEqual(CARD.card_state(body), "changes_requested")
        self.assertEqual(CARD.stale_head(body), Y)
        self.assertTrue(CARD.panel_blocked(body))
        # …and the auto-rereview job relabels once, from the request for
        # changes (there is no approval to date it by).
        self.assertEqual(self.rereview(fake), 0)
        self.assertEqual(fake.labels, ["DELETE", "POST"])
        self.assertIn(Y, CARD.rereview_heads(fake.card["body"]))

    def test_either_flag_off_changes_nothing(self):
        for approve_only, auto in (("false", "true"), ("true", "false")):
            with self.subTest(approve_only=approve_only, auto=auto):
                fake = self.blocked_fake()
                self.dismiss(fake, approve_only=approve_only, auto=auto)
                self.assertEqual(fake.patches, [])
                self.assertEqual(self.outputs().get("auto_rereview"), "false")

    def test_no_round_for_a_hand_off_veto_draft_cap_or_label_free_caller(self):
        cases = {
            "hand-off": (dict(labels=("cursor-review", "needs-human-review")), {}),
            "veto": (dict(labels=("cursor-review", "skip-cursor-review")), {}),
            "draft": (dict(draft=True), {}),
            "no relabel token": ({}, dict(can_relabel="false")),
            "run_without_label": ({}, dict(run_without_label="true")),
            "at the cap": ({}, dict(max_rounds="1")),
        }
        for name, (fake_kw, kw) in cases.items():
            with self.subTest(name):
                labels = fake_kw.pop("labels", ("cursor-review",))
                fake = FakeGitHub(card=panel_blocked_card(), reviews=[block(), REREVIEW.round_review(1, X)],
                                  labels=labels, **fake_kw)
                self.dismiss(fake, **kw)
                self.assertNotEqual(self.outputs().get("auto_rereview"), "true", name)

    def test_the_head_must_have_moved_and_have_no_round_of_its_own(self):
        fake = FakeGitHub(head=X, card=panel_blocked_card(), reviews=[block()], labels=("cursor-review",))
        self.dismiss(fake)
        self.assertEqual(self.outputs().get("auto_rereview"), "false")
        fake = FakeGitHub(card=panel_blocked_card(), reviews=[block(), REREVIEW.round_review(5, Y)],
                          labels=("cursor-review",))
        self.dismiss(fake)
        self.assertEqual(self.outputs().get("auto_rereview"), "false")

    def test_an_untrusted_or_capped_round_never_restarts(self):
        for state, nxt in (("no_decision", "push_then_relabel"), ("capped", "human")):
            with self.subTest(state):
                card = CARD.render_round(2, 5, X, state, nxt, "x", [], "y", "")
                fake = FakeGitHub(card=card, reviews=[block()], labels=("cursor-review",))
                self.dismiss(fake)
                self.assertEqual(self.outputs().get("auto_rereview"), "false")
                self.assertEqual(fake.patches, [])

    def test_a_panel_blocked_card_is_never_re_checked_by_the_axes_alone(self):
        # The two paths are exclusive by state: no lane marker, no approve-only.
        self.run_stage(self.blocked_fake(), "check")
        self.assertEqual(self.outputs()["eligible"], "false")

    def test_no_readable_decision_means_no_relabel(self):
        fake = self.blocked_fake()
        self.dismiss(fake)
        fake.reviews = [REREVIEW.round_review(1, X)]
        self.rereview(fake)
        self.assertEqual(fake.labels, [])


class WorkflowWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as f:
            cls.text = f.read()
        start = cls.text.index("  approve-only:\n")
        cls.job = cls.text[start:cls.text.index("\n  auto-rereview:\n", start)]

    def test_inputs_default_off_and_ten_runs(self):
        for name, default in (("approve_only_on_push", "false"), ("approve_only_max_runs", "10")):
            block = self.text[self.text.index(f"      {name}:\n"):]
            block = block[:block.index("\n      ", block.index("default:"))]
            self.assertIn(f"default: {default}", block, name)

    def test_the_job_runs_only_on_a_push_under_pull_request_target(self):
        self.assertIn("inputs.approve_only_on_push", self.job)
        self.assertIn("github.event_name == 'pull_request_target'", self.job)
        self.assertIn("github.event.action == 'synchronize' || github.event.action == 'reopened'", self.job)
        self.assertIn("needs.dismiss-stale-approval.result == 'success'", self.job)
        self.assertIn("!inputs.run_without_label", self.job)

    def test_the_approver_gate_reads_go_and_the_round_reads_the_card(self):
        self.assertIn("jobs.approve-only.outputs.go == 'true' && 'pass'", self.text)
        self.assertIn("jobs.approve-only.outputs.go == 'true' && jobs.approve-only.outputs.round", self.text)

    def test_the_start_stage_runs_only_after_an_eligible_check_and_the_wait(self):
        steps = re.findall(r"- name: ([^\n]+)", self.job)
        self.assertEqual(steps[-3:], ["Is this push re-checked by the axes alone", "Wait before re-checking",
                                      "Re-check if it is still wanted"])
        self.assertEqual(self.job.count("if: steps.check.outputs.eligible == 'true'"), 2)

    def test_waiters_for_one_head_are_serialised_not_cancelled(self):
        self.assertIn("cursor-review-reusable-approve-only-${{ github.repository }}-"
                      "${{ github.event.pull_request.number }}-${{ github.event.pull_request.head.sha }}", self.job)
        self.assertIn("cancel-in-progress: false", self.job)


if __name__ == "__main__":
    unittest.main()
