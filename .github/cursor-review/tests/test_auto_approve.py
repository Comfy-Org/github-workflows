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


class StaleReviewTest(unittest.TestCase):
    def review(self, rid, login="cursor-approver", state="APPROVED", commit=None, marker=True, sha=OLD, base=None):
        # `commit_id` defaults to NEW on purpose: GitHub moves a still-valid
        # approval's commit_id to each new head, so only the SHA recorded in the
        # body says what was reviewed. `base=None` records no base (an approval
        # posted before the base was recorded).
        body = AA.render_body(AA.APPROVE, ["ok"], "low", [], sha, base or "") if marker else "ok"
        return {"id": rid, "user": {"login": login}, "state": state, "commit_id": commit or NEW, "body": body}

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
            if args[:2] == ["api", "--paginate"]:
                if list_error:
                    raise list_error
                return json.dumps([reviews])
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
            if args[:2] == ["api", "--paginate"]:
                return json.dumps([list(reviews)])
            if isinstance(live, Exception):
                raise live
            return json.dumps({} if live is None else {"head": {"sha": live}})

        args = argparse.Namespace(repo="o/r", pr_number="1", head_sha=event_head,
                                  approver_login="cursor-approver", all_approvals=False)
        with mock.patch.object(AA, "gh", fake_gh), mock.patch.object(AA, "emit", lambda *a: None):
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

    def test_an_unreadable_head_with_no_event_head_goes_red(self):
        # Nothing to judge against: report it, never fall through to "" (which
        # would match no recorded SHA and dismiss everything).
        self.assertEqual(self.run_dismiss(RuntimeError("x"), "", [self.approval(1, NEW)]), (1, []))

    def test_a_shapeless_head_read_never_dismisses_everything(self):
        # read_head returning "" must not reach the filter: "" is not None, so it
        # would match no recorded SHA and withdraw every marked approval on the PR.
        self.assertEqual(self.run_dismiss(None, NEW, [self.approval(1, NEW)]), (0, []))


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
            if args[:2] == ["api", "--paginate"]:
                return json.dumps([list(reviews)])
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
            "value: ${{ jobs.round-cap.outputs.capped == 'true' && 'capped' || "
            "inputs.approve_max_severity == '' && 'off' || jobs.post-review.outputs.approve_gate || 'untrusted' }}",
            text,
        )
        self.assertIn("approve_gate: ${{ steps.approve.outputs.approve_gate }}", text)
        self.assertIn("needs.round-cap.outputs.capped != 'true'", text)


class CmdDecideGateOutputTest(unittest.TestCase):
    """cmd_decide writes approve_gate on every path, including the I/O ones."""

    def run_decide(self, findings=(), labels=(), heads=(SHA, SHA), threshold="medium", post_error=None):
        heads = iter(heads)

        def fake_gh(args, payload=None):
            if args[:3] == ["api", "-X", "POST"]:
                if post_error:
                    raise RuntimeError(post_error)
                return json.dumps({"id": 99})
            if args[:3] == ["api", "-X", "PUT"]:
                return "{}"
            if args[:2] == ["api", "--paginate"]:
                return json.dumps([[]])
            return json.dumps({"head": {"sha": next(heads)}, "base": {"ref": "main"},
                               "labels": [{"name": n} for n in labels]})

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
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out}), \
                    mock.patch.object(AA, "gh", fake_gh), \
                    mock.patch.object(AA, "open_thread_severities", lambda *a: []), \
                    mock.patch.object(AA, "emit", lambda *a: None):
                rc = AA.cmd_decide(args)
            return rc, read_outputs(out).get("approve_gate")

    def test_pass(self):
        self.assertEqual(self.run_decide(), (0, "pass"))

    def test_fail(self):
        self.assertEqual(self.run_decide(findings=[finding("critical")]), (0, "fail"))

    def test_needs_human_review_label_never_approves(self):
        rc, gate = self.run_decide(labels=["needs-human-review"])
        self.assertEqual((rc, gate), (0, "capped"))

    def test_head_moved_during_post_is_untrusted(self):
        self.assertEqual(self.run_decide(heads=(SHA, "b" * 40)), (0, "untrusted"))

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

    def run_cap(self, reviews, max_rounds="5", timeline=(), comments=(), label_exists=True, fail=()):
        writes = []

        def fake_gh(args, payload=None):
            path = next(a for a in args if a.startswith("repos/"))
            method = args[2] if args[:2] == ["api", "-X"] else "GET"
            for f in fail:
                if f in path:
                    raise RuntimeError(f"boom {path}")
            if method != "GET":
                writes.append((method, path, payload))
                return "{}"
            if "/timeline" in path:
                return json.dumps([list(timeline)])
            if path.endswith("/reviews?per_page=100"):
                return json.dumps([list(reviews)])
            if "/issues/1/comments" in path:
                return json.dumps([list(comments)])
            if "/reviews/" in path and path.endswith("/comments?per_page=100"):
                return json.dumps([[inline(11, "High"), inline(12, "Nit")]])
            if path == "repos/o/r/labels/needs-human-review":
                if not label_exists:
                    raise RuntimeError("HTTP 404")
                return "{}"
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

    def test_a_failed_label_write_stays_capped_and_goes_red(self):
        rc, out, _ = self.run_cap(self.FIVE, fail=("issues/1/labels",))
        self.assertEqual((rc, out["capped"]), (1, "true"))


if __name__ == "__main__":
    unittest.main()
