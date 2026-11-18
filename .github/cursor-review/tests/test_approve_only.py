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
import contextlib
import importlib.util
import os
import re
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(__file__)

_UNSET = object()  # run_stage: "do not patch AA.compare" vs "patch it to return None"


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
        self.emitted = []

    def outputs(self):
        with open(self.out.name, encoding="utf-8") as f:
            return dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)

    def run_stage(self, fake, stage, head=Y, threshold="medium", authors="", max_runs="10", threads=(),
                  max_rounds="5", editors=(), paths="", compare=_UNSET):
        args = argparse.Namespace(stage=stage, repo="o/r", pr_number="1", head_sha=head, approver_login=LOGIN,
                                  review_label="", threshold=threshold, approve_authors=authors,
                                  max_runs=max_runs, max_rounds=max_rounds, delay_minutes="10",
                                  approve_only_paths=paths, poster_login=POSTER)
        reader = (mock.Mock(side_effect=RuntimeError("graphql 502")) if threads is None
                  else mock.Mock(return_value=list(threads)))
        history = (mock.Mock(side_effect=RuntimeError("graphql 502")) if editors is None
                   else mock.Mock(return_value=list(editors)))
        # `approve_only_paths` reads the compare API; stub it unless a case opts
        # in (the default-empty path never calls it, so it is left untouched).
        compare_cm = (mock.patch.object(AA, "compare", mock.Mock(return_value=compare))
                      if compare is not _UNSET else contextlib.nullcontext())
        with mock.patch.object(AA, "gh", fake), mock.patch.object(CARD, "gh", fake), \
                mock.patch.object(AA, "_load_card", lambda: CARD), \
                mock.patch.object(AA, "open_thread_severities", reader), \
                mock.patch.object(AA, "card_foreign_editors", history), \
                mock.patch.object(AA, "emit", lambda *a: self.emitted.extend(a)), \
                compare_cm, \
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

    def test_a_card_anyone_else_edited_or_whose_history_is_unreadable_is_out_of_the_lane(self):
        # Write access can edit the card and plant LANE_MARKER after a failed
        # panel; only its edit history says who touched it.
        self.run_stage(self.fake(), "check", editors=["dev"])
        self.assertEqual(self.outputs()["eligible"], "false")
        self.run_stage(self.fake(), "check", editors=None)
        self.assertEqual(self.outputs()["eligible"], "false")
        self.run_stage(self.fake(), "start", editors=["dev"])
        self.assertEqual(self.outputs()["go"], "false")

    def test_the_last_round_of_max_rounds_or_an_unnumbered_round_is_not_re_checked(self):
        self.run_stage(self.fake(card=red_card(round_no=5)), "check")
        self.assertEqual(self.outputs()["eligible"], "false", "round 5 of 5 is the human's")
        self.run_stage(self.fake(card=red_card(round_no=4)), "check")
        self.assertEqual(self.outputs()["eligible"], "true")
        self.run_stage(self.fake(card=red_card(round_no="")), "check", max_rounds="0")
        self.assertEqual(self.outputs()["eligible"], "false", "`max_rounds: 0` writes `round ?`")

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

    def test_the_cap_is_clamped_to_the_heads_the_record_keeps(self):
        # Above RECHECK_HEADS_KEPT the record stops growing, so the cap could
        # never be reached and re-checks would be unbounded.
        with mock.patch.object(AA, "_load_card", lambda: CARD):
            self.assertEqual(AA._approve_only_cap(argparse.Namespace(max_runs="999")), CARD.RECHECK_HEADS_KEPT)
            self.assertEqual(AA._approve_only_cap(argparse.Namespace(max_runs="7")), 7)
        heads = [f"{i:040x}" for i in range(CARD.RECHECK_HEADS_KEPT)]
        body = red_card()
        for h in heads:
            body = CARD.mark_recheck_started(body, "2", 999, h)
        fake = self.fake(card=body)
        self.run_stage(fake, "check", max_runs="999")
        self.assertEqual(self.outputs()["eligible"], "false")

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
                mock.patch.object(AA, "_load_card", lambda: CARD), mock.patch.object(AA, "emit", lambda *a: self.emitted.extend(a)), \
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

    def test_an_unnumbered_round_promises_no_re_check(self):
        # `max_rounds: 0` writes `round ?`, which approve-only never re-checks.
        start = CARD.render_start("", "", X, AXES, "pass", "")
        self.assertTrue(CARD.in_axes_lane(start))
        fake = FakeGitHub(card=start, reviews=[REREVIEW.approval(10, X)], labels=("cursor-review",))
        args = argparse.Namespace(
            repo="o/r", pr_number="1", head_sha=Y, approver_login=LOGIN, all_approvals=False, threshold="low",
            approve_authors="", max_rounds="0", poster_login=POSTER, review_label="", auto_rereview="false",
            auto_rereview_delay_minutes="10", can_relabel="true", run_without_label="false", carry_forward="false",
            event_name="pull_request_target", approve_only="true", approve_only_delay_minutes="10")
        with mock.patch.object(AA, "gh", fake), mock.patch.object(CARD, "gh", fake), \
                mock.patch.object(AA, "_load_card", lambda: CARD), mock.patch.object(AA, "emit", lambda *a: self.emitted.extend(a)), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": self.out.name}), \
                mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
            AA.cmd_dismiss_stale(args)
        self.assertNotIn("re-checks the new head automatically", fake.card["body"])


class CardEditHistoryTest(unittest.TestCase):
    @staticmethod
    def page(logins, more=False, cursor=""):
        nodes = [{"editor": None if n is None else {"__typename": "User", "login": n}} for n in logins]
        return {"data": {"node": {"userContentEdits": {
            "pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}}}}

    def editors(self, pages, comment=None):
        calls = []

        def graphql(query, **variables):
            calls.append(variables)
            return pages[len(calls) - 1]
        with mock.patch.object(AA, "_graphql", graphql):
            return AA.card_foreign_editors({"node_id": "IC_x"} if comment is None else comment, LOGIN), calls

    def test_only_the_approver_ever_edited_it(self):
        self.assertEqual(self.editors([self.page([LOGIN, LOGIN.upper()])])[0], [])

    def test_a_bot_login_matches_with_or_without_its_suffix(self):
        with mock.patch.object(AA, "_graphql", lambda q, **v: self.page(["approver"])):
            self.assertEqual(AA.card_foreign_editors({"node_id": "IC_x"}, "approver[bot]"), [])

    def test_every_page_is_read_and_any_other_editor_or_a_ghost_is_reported(self):
        found, calls = self.editors([self.page([LOGIN], more=True, cursor="c1"), self.page(["Dev", None])])
        self.assertEqual(found, ["(unknown)", "dev"])
        self.assertEqual(calls[1].get("cursor"), "c1")

    def test_an_unreadable_history_raises(self):
        with self.assertRaises(ValueError):
            self.editors([], comment={})
        with self.assertRaises(ValueError):
            self.editors([{"data": {"node": None}}])


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
                mock.patch.object(AA, "_load_card", lambda: CARD), mock.patch.object(AA, "emit", lambda *a: self.emitted.extend(a)), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": self.out.name}), \
                mock.patch("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a)))):
            return AA.cmd_dismiss_stale(args)

    def rereview(self, fake, head=Y):
        args = argparse.Namespace(repo="o/r", pr_number="1", head_sha=head, approver_login=LOGIN,
                                  poster_login=POSTER, review_label="cursor-review", max_rounds="5")
        with mock.patch.object(AA, "gh", fake), mock.patch.object(CARD, "gh", fake), \
                mock.patch.object(AA, "_load_card", lambda: CARD), mock.patch.object(AA, "emit", lambda *a: self.emitted.extend(a)), \
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

    def test_both_stages_pass_the_path_scope_and_its_read_token(self):
        # Both the check and the start invocation forward the new input, and
        # each step offers FALLBACK_TOKEN so the compare API runs with the job's
        # contents: read rather than the reviews-scoped approver token.
        self.assertEqual(self.job.count('--approve-only-paths "$APPROVE_ONLY_PATHS"'), 2)
        self.assertEqual(self.job.count("APPROVE_ONLY_PATHS: ${{ inputs.approve_only_paths }}"), 2)
        self.assertEqual(self.job.count("FALLBACK_TOKEN: ${{ secrets.GITHUB_TOKEN }}"), 2)
        # The scope's anchor is read only from the panel poster's rounds.
        self.assertEqual(self.job.count('--poster-login "$POSTER_LOGIN"'), 2)
        self.assertEqual(self.job.count("POSTER_LOGIN: ${{ needs.dismiss-stale-approval.outputs.poster_login }}"), 2)
        self.assertIn("poster_login: ${{ steps.dismiss.outputs.poster_login }}", self.text)

    def test_approve_only_paths_defaults_empty(self):
        block = self.text[self.text.index("      approve_only_paths:\n"):]
        block = block[:block.index("\n      ", block.index("default:"))]
        self.assertIn("default: ''", block)
        self.assertIn("type: string", block)


def cmp_data(filenames, status="ahead", behind=0, files=None):
    """A GitHub compare payload: `filenames` become added files with a patch."""
    listed = files if files is not None else [{"filename": n, "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}
                                              for n in filenames]
    return {"status": status, "ahead_by": len(listed), "behind_by": behind, "total_commits": 1, "files": listed}


def panel_round(rid, sha=X, login=POSTER, base_ref="main", merge_base=Z, **extra):
    """A consolidated review carrying post-review.py's round sentinel for `sha`.

    `base_ref` is the base branch the round reviewed against, hex-encoded as
    post-review.py writes it; "main" matches FakeGitHub's PR base, and "" models
    a round from before the base was recorded. `merge_base` defaults to Z, the
    live base SHA PathScopeEligibilityTest's fake PR carries."""
    marker = AA._load_post_review().CONSOLIDATED_MARKER
    encoded = base_ref.encode("utf-8").hex() if base_ref else ""
    sentinel = (f'<!-- cursor-review:round v1 '
                f'{{"base":"{Z}","base_ref":"{encoded}","head":"{sha}","merge_base":"{merge_base}"}} -->')
    return {"id": rid, "user": {"login": login}, "state": "COMMENTED", "commit_id": sha,
            "body": f"{marker}\n\n{sentinel}\n\nfindings", "submitted_at": "2026-10-12T07:00:00Z", **extra}


class PanelRoundHeadTest(unittest.TestCase):
    """The scope's anchor: the last panel round's commit, never the card's."""

    def test_the_newest_bot_round_names_its_commit(self):
        self.assertEqual(AA.panel_round_head([panel_round(3, X), panel_round(5, Z), block()], POSTER), Z)
        self.assertEqual(AA.panel_round_head([panel_round(5, Z), panel_round(3, X)], POSTER), Z, "order is by id")

    def test_no_round_a_human_forgery_or_an_edited_newest_round_reads_empty(self):
        self.assertEqual(AA.panel_round_head([block()], POSTER), "")
        self.assertEqual(AA.panel_round_head([panel_round(3, X, login="dev")], POSTER), "")
        self.assertEqual(AA.panel_round_head([panel_round(3, X), panel_round(5, Z, edited=True)], POSTER), "")
        # An edited newer review that lost its marker still rules out the older round.
        stripped = {"id": 5, "user": {"login": POSTER}, "state": "COMMENTED", "body": "gone", "edited": True}
        self.assertEqual(AA.panel_round_head([panel_round(3, X), stripped], POSTER), "")
        # An edited OLDER review, or another login's edit, does not.
        self.assertEqual(AA.panel_round_head([dict(stripped, id=1), panel_round(3, X)], POSTER), X)
        self.assertEqual(AA.panel_round_head([panel_round(3, X), dict(stripped, user={"login": "dev"})], POSTER), X)

    def test_only_the_configured_poster_anchors_it(self):
        # The marker is public: another bot's newer round must not move the
        # anchor past changes the panel never reviewed.
        reviews = [panel_round(3, X), panel_round(5, Z, login="other-app[bot]")]
        self.assertEqual(AA.panel_round_head(reviews, POSTER), X)
        self.assertEqual(AA.panel_round_head(reviews, POSTER.upper()), X, "logins compare case-insensitively")
        self.assertEqual(AA.panel_round_head(reviews, ""), "", "no poster: fail closed")
        # No bot App: the panel posts as github-actions[bot], which then counts.
        gha = [panel_round(3, X, login="github-actions[bot]"), panel_round(5, Z, login=POSTER)]
        self.assertEqual(AA.panel_round_head(gha, "github-actions[bot]"), X)

    def test_an_axes_only_recheck_does_not_move_it(self):
        # The card's "Reviewed commit" moves to each re-checked head; the panel's does not.
        reviews = [panel_round(3, X), block(rid=9, sha=Y), REREVIEW.approval(10, Y)]
        self.assertEqual(AA.panel_round_head(reviews, POSTER), X)


class PanelRoundBaseRefTest(unittest.TestCase):
    """The retarget check's anchor: the base the last panel round reviewed against."""

    def test_the_newest_round_names_its_base_decoded_from_hex(self):
        self.assertEqual(
            AA.panel_round_base_ref([panel_round(3, X, base_ref="old"), panel_round(5, Z, base_ref="main")], POSTER),
            "main", "the base comes from the SAME round as the commit — the newest")
        self.assertEqual(AA.panel_round_base_ref([panel_round(5, Z, base_ref="release/1.2")], POSTER), "release/1.2")

    def test_no_round_a_missing_base_a_forgery_or_an_edited_newest_reads_empty(self):
        self.assertEqual(AA.panel_round_base_ref([block()], POSTER), "", "no panel round")
        self.assertEqual(AA.panel_round_base_ref([panel_round(3, X, base_ref="")], POSTER), "",
                         "a round from before the base was recorded")
        self.assertEqual(AA.panel_round_base_ref([panel_round(3, X, login="dev")], POSTER), "", "not the poster")
        self.assertEqual(
            AA.panel_round_base_ref([panel_round(3, X), panel_round(5, Z, edited=True)], POSTER), "",
            "an edited newer round rules out the older one")

    def test_a_garbled_hex_base_reads_empty(self):
        forged = {"id": 5, "user": {"login": POSTER}, "state": "COMMENTED",
                  "body": f"{AA._load_post_review().CONSOLIDATED_MARKER}\n\n"
                          '<!-- cursor-review:round v1 {"base":"","base_ref":"zz","head":"","merge_base":""} -->\n'}
        self.assertEqual(AA.panel_round_base_ref([forged], POSTER), "")


class PathScopeUnitTest(unittest.TestCase):
    """`approve_only_paths_scope` in isolation — the compare/glob/divergence rules."""
    GLOBS = ["docs/**", "**/*.md"]

    def scope(self, data, globs=None):
        with mock.patch.object(AA, "compare", mock.Mock(return_value=data)):
            return AA.approve_only_paths_scope("o/r", X, Y, globs if globs is not None else self.GLOBS)

    def test_a_docs_only_push_is_in_scope(self):
        reason, summary = self.scope(cmp_data(["docs/a.md", "README.md", "pkg/notes.md"]))
        self.assertIsNone(reason)
        self.assertIn("3 changed file(s)", summary)
        self.assertIn("all within", summary)

    def test_a_code_file_falls_out_of_scope_and_is_named(self):
        reason, _ = self.scope(cmp_data(["docs/a.md", "src/main.go"]))
        self.assertIsNotNone(reason)
        self.assertIn("`src/main.go` is outside", reason)
        self.assertIn("2 changed file(s)", reason)

    def test_a_force_pushed_or_diverged_head_falls_through(self):
        for status, behind in (("diverged", 3), ("behind", 2), ("identical", 0)):
            reason, _ = self.scope(cmp_data(["docs/a.md"], status=status, behind=behind))
            self.assertIn("not a clean descendant", reason, status)

    def test_a_failed_compare_falls_through_never_open(self):
        reason, _ = self.scope(None)
        self.assertIn("could not be read", reason)

    def test_a_truncated_file_list_falls_through(self):
        big = [{"filename": f"docs/f{i}.md", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}
               for i in range(AA.COMPARE_FILE_CAP)]
        reason, _ = self.scope(cmp_data(None, files=big))
        self.assertIn("too many files", reason)

    def test_a_rename_needs_both_paths_in_scope(self):
        moved_out = [{"filename": "src/a.go", "previous_filename": "docs/a.md", "status": "renamed", "changes": 0}]
        reason, _ = self.scope(cmp_data(None, files=moved_out))
        self.assertIn("`src/a.go` is outside", reason)
        moved_within = [{"filename": "docs/b.md", "previous_filename": "docs/a.md", "status": "renamed", "changes": 0}]
        reason2, summary2 = self.scope(cmp_data(None, files=moved_within))
        self.assertIsNone(reason2)
        self.assertIn("all within", summary2)

    def test_no_reviewed_commit_recorded_falls_through(self):
        reason, _ = AA.approve_only_paths_scope("o/r", "", Y, self.GLOBS)
        self.assertIn("no panel round records the commit", reason)

    def test_a_missing_or_non_list_file_list_falls_through_but_an_empty_one_passes(self):
        for files in ("missing", None, {"filename": "docs/a.md"}, "docs/a.md"):
            data = {"status": "ahead", "ahead_by": 1, "behind_by": 0}
            if files != "missing":
                data["files"] = files
            reason, _ = self.scope(data)
            self.assertIn("listed no changed files", reason or "", repr(files))
        reason, summary = self.scope(cmp_data([]))
        self.assertIsNone(reason)
        self.assertIn("0 changed file(s)", summary)

    def test_a_rename_without_its_old_path_falls_through(self):
        for previous in (None, "", 7):
            entry = {"filename": "docs/b.md", "status": "renamed"}
            if previous is not None:
                entry["previous_filename"] = previous
            reason, _ = self.scope(cmp_data(None, files=[entry]))
            self.assertIn("old path could not be read", reason or "", repr(previous))

    def test_an_unprintable_out_of_scope_name_is_never_echoed(self):
        for name in ("x\n::error::Approved", "a`b.go", "x\r::stop-commands::t"):
            reason, summary = self.scope(cmp_data([name]))
            self.assertIn("a file with an unprintable name is outside", reason)
            for text in (reason, summary):
                self.assertNotIn("::", text)
                self.assertNotIn("\n", text)


class PathScopeEligibilityTest(Base):
    """The scope gate through `cmd_approve_only`: it decides eligibility, so a
    push out of scope leaves `eligible`/`go` false and a full round runs."""

    def fake(self, card=None, reviews=None, **kw):
        # The panel round on X the scope is anchored on, then its decision. The
        # live base tip is the round's merge base (Z): the base has not moved.
        fake = super().fake(card=card, reviews=[panel_round(3, X), block()] if reviews is None else reviews, **kw)
        fake.pr["base"]["sha"] = Z
        return fake

    def test_the_scope_is_anchored_on_the_panel_round_not_the_card(self):
        # The card says Y (an axes-only re-check moved it); the panel saw X.
        seen = []

        def cmp(repo, base, head):
            seen.append(base)
            return cmp_data(["docs/guide.md"])
        fake = self.fake(card=red_card(sha=Y), reviews=[panel_round(3, X), block(sha=Y)], head=Z)
        with mock.patch.object(AA, "compare", cmp):
            self.run_stage(fake, "check", head=Z, paths="docs/**")
        self.assertEqual(seen, [X])

    def test_no_readable_panel_round_falls_through(self):
        self.assertEqual(self.run_stage(self.fake(reviews=[block()]), "check", paths="docs/**",
                                        compare=cmp_data(["docs/guide.md"])), 0)
        self.assertEqual(self.outputs()["eligible"], "false")

    def test_a_docs_only_push_is_re_checked(self):
        fake = self.fake()
        self.assertEqual(self.run_stage(fake, "check", paths="docs/** **/*.md",
                                        compare=cmp_data(["docs/guide.md"])), 0)
        self.assertEqual(self.outputs()["eligible"], "true")
        self.assertEqual(self.run_stage(fake, "start", paths="docs/** **/*.md",
                                        compare=cmp_data(["docs/guide.md"])), 0)
        self.assertEqual(self.outputs()["go"], "true")

    def test_a_mixed_push_falls_through_to_a_full_round(self):
        fake = self.fake()
        self.assertEqual(self.run_stage(fake, "check", paths="docs/**",
                                        compare=cmp_data(["docs/guide.md", "lib/core.py"])), 0)
        self.assertEqual(self.outputs()["eligible"], "false")
        self.assertEqual(fake.patches, [], "a push out of scope announces nothing on the card")

    def test_a_force_pushed_head_falls_through(self):
        self.assertEqual(self.run_stage(self.fake(), "start", paths="docs/**",
                                        compare=cmp_data(["docs/guide.md"], status="diverged", behind=4)), 0)
        self.assertEqual(self.outputs()["go"], "false")

    def test_a_retarget_since_the_panel_round_falls_through(self):
        # Panel reviewed against `feature-x`; the PR now targets `main` (live base).
        # The head-to-head compare is still docs-only, but the diff against `main`
        # is one no panel round read — so the push needs a full round, not the axes.
        fake = self.fake(reviews=[panel_round(3, X, base_ref="feature-x"), block()])
        self.assertEqual(self.run_stage(fake, "check", paths="docs/**",
                                        compare=cmp_data(["docs/guide.md"])), 0)
        self.assertEqual(self.outputs()["eligible"], "false")
        self.assertEqual(fake.patches, [], "nothing is announced on the card")

    def test_a_panel_round_without_a_recorded_base_falls_through(self):
        # A round from before the base was recorded cannot prove it was not retargeted.
        self.assertEqual(self.run_stage(self.fake(reviews=[panel_round(3, X, base_ref=""), block()]),
                                        "check", paths="docs/**", compare=cmp_data(["docs/guide.md"])), 0)
        self.assertEqual(self.outputs()["eligible"], "false")

    def test_the_same_base_advancing_still_goes(self):
        # Out of scope by design: the base ref is unchanged, so a docs-only push is
        # re-checked even though `main` itself may have moved on since the round.
        self.assertEqual(self.run_stage(self.fake(), "check", paths="docs/**",
                                        compare=cmp_data(["docs/guide.md"])), 0)
        self.assertEqual(self.outputs()["eligible"], "true")

    def test_the_same_base_fast_forwarded_still_goes(self):
        # main moved on: the round's merge base Z is an ancestor of the new tip Y.
        seen = []

        def cmp(repo, base, head):
            seen.append((base, head))
            return cmp_data(["docs/guide.md"])
        fake = self.fake()
        fake.pr["base"]["sha"] = Y
        with mock.patch.object(AA, "compare", cmp):
            self.assertEqual(self.run_stage(fake, "check", head=Y, paths="docs/**"), 0)
        self.assertEqual(self.outputs()["eligible"], "true")
        self.assertIn((Z, Y), seen, "the merge base is checked against the live base tip")

    def test_a_base_rewritten_under_the_same_name_falls_through(self):
        # A stacked PR's lower branch rebased: same ref, but the round's merge base
        # is no longer on it, so the PR's own diff grew by commits no round read.
        def cmp(repo, base, head):
            if base == Z:
                return cmp_data([], status="diverged", behind=3)
            return cmp_data(["docs/guide.md"])
        fake = self.fake()
        fake.pr["base"]["sha"] = "d" * 40
        with mock.patch.object(AA, "compare", cmp):
            self.assertEqual(self.run_stage(fake, "check", paths="docs/**"), 0)
        self.assertEqual(self.outputs()["eligible"], "false")
        self.assertTrue(any("base branch was rewritten" in line for line in self.emitted), self.emitted)

    def test_an_unreadable_merge_base_compare_falls_through(self):
        fake = self.fake()
        fake.pr["base"]["sha"] = "d" * 40
        self.assertEqual(self.run_stage(fake, "check", paths="docs/**", compare=None), 0)
        self.assertEqual(self.outputs()["eligible"], "false")

    def test_a_round_without_a_merge_base_falls_through(self):
        fake = self.fake(reviews=[panel_round(3, X, merge_base=""), block()])
        self.assertEqual(self.run_stage(fake, "check", paths="docs/**", compare=cmp_data(["docs/guide.md"])), 0)
        self.assertEqual(self.outputs()["eligible"], "false")

    def test_an_unreadable_live_base_falls_through_and_says_so(self):
        fake = self.fake()
        del fake.pr["base"]["sha"]
        self.assertEqual(self.run_stage(fake, "check", paths="docs/**", compare=cmp_data(["docs/guide.md"])), 0)
        self.assertEqual(self.outputs()["eligible"], "false")
        self.assertTrue(any("live base could not be read" in line for line in self.emitted), self.emitted)

    def test_no_panel_round_is_named_as_such_not_as_a_missing_base(self):
        self.run_stage(self.fake(reviews=[block()]), "check", paths="docs/**", compare=cmp_data(["docs/guide.md"]))
        self.assertTrue(any("no panel round records the commit" in line for line in self.emitted), self.emitted)

    def test_an_unprintable_live_base_still_falls_through_without_crashing(self):
        # The base is the PR author's text; a mismatch on a ref carrying a newline
        # or a `::` command must still fall through, and never reach the log raw.
        fake = self.fake(reviews=[panel_round(3, X, base_ref="main"), block()])
        fake.pr["base"]["ref"] = "x\n::error::Approved"
        self.assertEqual(self.run_stage(fake, "check", paths="docs/**",
                                        compare=cmp_data(["docs/guide.md"])), 0)
        self.assertEqual(self.outputs()["eligible"], "false")

    def test_an_empty_scope_keeps_todays_behaviour_without_a_compare(self):
        # No compare stub: if the empty-scope path read the compare API, the
        # FakeGitHub would raise on the unexpected call.
        fake = self.fake()
        self.assertEqual(self.run_stage(fake, "check", paths=""), 0)
        self.assertEqual(self.outputs()["eligible"], "true")


class PrintableBaseTest(unittest.TestCase):
    """The base ref is echoed into the log and job summary only when it is safe."""

    def test_a_plain_branch_name_is_backticked(self):
        self.assertEqual(AA._printable_base("main"), "`main`")
        self.assertEqual(AA._printable_base("release/1.2"), "`release/1.2`")

    def test_a_ref_that_could_inject_is_replaced_by_a_placeholder(self):
        for ref in ("x\n::error::Approved", "a`b", "x\r::stop-commands::t", "", "a b"):
            self.assertEqual(AA._printable_base(ref), "a base with an unprintable name", repr(ref))


if __name__ == "__main__":
    unittest.main()
