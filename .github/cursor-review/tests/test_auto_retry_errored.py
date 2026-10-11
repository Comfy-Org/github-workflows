#!/usr/bin/env python3
"""Tests for `auto_retry_errored_rounds` — re-running a round the panel broke.

WHY IT EXISTS. Auto-retry (BE-19526) only ever covered the head/base races, and
`auto_retry_eligible`'s own docstring said why it could not cover anything
else: "a re-run would hit the same wall". The wall was the gate's same-commit
dedupe, and #407 removed it. The case that wall was blocking is the common one
— a round that could not decide because reviewer cells ERRORED, where the diff
is fine and there is nothing to push.

TWO THINGS ARE EASY TO GET WRONG HERE, so both are pinned hard.

A retry must not be offered for a cause a retry cannot fix. A structural
failure, or findings above the threshold, means the next round hits the same
wall whatever the panel does — so `EligibilityTest` walks the whole truth table
rather than the happy path.

The retry must not reach the HUMAN force path. `rereview_command` authorizes by
the comment's association; the retry posts no comment, and if it could satisfy
that path then any App's comment could force rounds. The retry's own signal is
the marked block `decide` already writes, checked for `user.type == "Bot"`, the
commit id, and recency — and capped, so a forged marker cannot loop.

Parsed WITHOUT PyYAML, like its sibling suites: this repo is stdlib-only and CI
installs no requirements for these tests.

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py' -v
"""

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(__file__)
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "auto-approve.py"))
WORKFLOW = os.path.normpath(
    os.path.join(HERE, "..", "..", "workflows", "cursor-review.yml")
)

CARD = os.path.normpath(
    os.path.join(HERE, "..", "..", "cursor-approve", "card.py")
)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


auto_approve = _load(SCRIPT, "auto_approve_retry")
card = _load(CARD, "card_retry")

HEAD_MOVED = auto_approve.REASON_HEAD_MOVED
BASE_CHANGED = auto_approve.REASON_BASE_CHANGED
STRUCTURAL = auto_approve.REASON_NOT_DELIVERED


def workflow_text():
    with io.open(WORKFLOW, encoding="utf-8") as f:
        return f.read()


def step_run(name):
    """The `run:` body of the step with this exact name."""
    text, cur, found = workflow_text(), None, []
    for line in text.splitlines():
        if re.match(r"^      - name: ", line):
            if cur is not None:
                found.append("\n".join(cur))
            cur = [line] if line.strip() == f"- name: {name}" else None
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        found.append("\n".join(cur))
    assert len(found) == 1, f"{name!r} matched {len(found)} steps"
    m = re.search(r"^        run: \|\n(.*)", found[0], re.S | re.M)
    body = []
    for line in m.group(1).splitlines():
        if line.strip() and not line.startswith("          "):
            break
        body.append(line[10:])
    return found[0], "\n".join(body)


class PanelReasonTest(unittest.TestCase):
    """The predicate must match what `panel_gate` actually produces."""

    def test_the_predicate_matches_a_real_short_panel_reason(self):
        # Generated, not hand-written: a hand-written reason is how the
        # producer and the matcher drift apart.
        panel = [
            {"review_type": "adversarial", "status": "ok"},
            {"review_type": "edge-case", "status": "error"},
        ]
        reason, _tolerated = auto_approve.panel_gate(panel, max_failed=0)
        self.assertIsNotNone(reason)
        self.assertTrue(auto_approve.is_panel_incomplete_reason(reason), reason)

    def test_the_variant_that_names_a_missing_review_type_also_matches(self):
        panel = [
            {"review_type": "adversarial", "status": "error"},
            {"review_type": "edge-case", "status": "error"},
        ]
        reason, _ = auto_approve.panel_gate(panel, max_failed=5)
        self.assertIsNotNone(reason)
        self.assertIn("leaving no completed", reason)
        self.assertTrue(auto_approve.is_panel_incomplete_reason(reason), reason)

    def test_unrelated_reasons_do_not_match(self):
        for reason in (HEAD_MOVED, BASE_CHANGED, STRUCTURAL,
                       "no panel metadata", "", None, 7):
            with self.subTest(reason=reason):
                self.assertFalse(auto_approve.is_panel_incomplete_reason(reason))


class EligibilityTest(unittest.TestCase):
    """The whole truth table, because the dangerous answers are the `True`s."""

    ERRORED = "2/6 " + auto_approve.PANEL_INCOMPLETE_SUFFIX

    def eligible(self, reasons, on):
        return auto_approve.auto_retry_eligible(reasons, retry_errored=on)

    # --- the pre-existing path, which must be untouched ---

    def test_head_moved_is_eligible_with_the_flag_off(self):
        self.assertTrue(self.eligible([HEAD_MOVED], False))
        self.assertTrue(self.eligible([HEAD_MOVED, BASE_CHANGED], False))

    def test_a_base_retarget_alone_is_never_eligible(self):
        # The head is unchanged, so the re-run would be deduped away — and the
        # errored path must not accidentally admit it either.
        self.assertFalse(self.eligible([BASE_CHANGED], False))
        self.assertFalse(self.eligible([BASE_CHANGED], True))

    # --- the new path ---

    def test_an_errored_panel_needs_the_flag(self):
        self.assertFalse(self.eligible([self.ERRORED], False))
        self.assertTrue(self.eligible([self.ERRORED], True))

    def test_errored_alongside_a_race_is_eligible(self):
        self.assertTrue(self.eligible([self.ERRORED, HEAD_MOVED], True))
        self.assertTrue(self.eligible([self.ERRORED, BASE_CHANGED], True))

    def test_a_cause_a_retry_cannot_fix_rules_it_out(self):
        # The important negative: the next round hits the same wall.
        self.assertFalse(self.eligible([self.ERRORED, STRUCTURAL], True))
        self.assertFalse(self.eligible([STRUCTURAL], True))
        self.assertFalse(
            self.eligible([self.ERRORED, "1 finding above the threshold"], True)
        )

    def test_the_panel_inconsistent_cause_is_not_retried(self):
        # A forger forges again; a human reading the red legs can tell.
        self.assertFalse(
            self.eligible([self.ERRORED, auto_approve.REASON_PANEL_INCONSISTENT], True)
        )

    def test_no_reasons_is_not_eligible(self):
        for reasons in ([], None, [""], ["   "]):
            with self.subTest(reasons=reasons):
                self.assertFalse(self.eligible(reasons, True))


class BudgetTest(unittest.TestCase):
    """The opt-in has to reach the budget, and the budget still fails closed."""

    class Args:
        def __init__(self, **kw):
            self.can_relabel = "true"
            self.approver_login = "bot[bot]"
            self.max_rounds = "5"
            self.round = "1"
            self.retry_errored_rounds = "true"
            self.repo = "o/r"
            self.pr_number = "1"
            self.__dict__.update(kw)

    ERRORED = "2/6 " + auto_approve.PANEL_INCOMPLETE_SUFFIX

    def setUp(self):
        self.calls = []
        self._real = auto_approve.list_reviews
        auto_approve.list_reviews = lambda repo, pr: self.calls.append((repo, pr)) or []
        self.addCleanup(setattr, auto_approve, "list_reviews", self._real)

    def test_the_flag_gates_the_errored_cause(self):
        self.assertTrue(
            auto_approve.auto_retry_budget_left(self.Args(), [self.ERRORED])
        )
        self.assertFalse(
            auto_approve.auto_retry_budget_left(
                self.Args(retry_errored_rounds="false"), [self.ERRORED]
            )
        )

    def test_only_an_explicit_true_enables_it(self):
        for value in ("", "yes", "1", "TRUE ", None):
            with self.subTest(value=value):
                expected = str(value).strip().lower() == "true"
                self.assertEqual(
                    expected,
                    auto_approve.auto_retry_budget_left(
                        self.Args(retry_errored_rounds=value), [self.ERRORED]
                    ),
                )

    def test_a_token_that_cannot_relabel_never_retries(self):
        self.assertFalse(
            auto_approve.auto_retry_budget_left(
                self.Args(can_relabel="false"), [self.ERRORED]
            )
        )

    def test_the_last_allowed_round_does_not_retry(self):
        # The retry would cap out and spend the budget on nothing.
        self.assertFalse(
            auto_approve.auto_retry_budget_left(
                self.Args(round="5", max_rounds="5"), [self.ERRORED]
            )
        )

    def test_a_spent_budget_does_not_retry(self):
        marked = [{"user": {"login": "bot[bot]"},
                   "body": "x\n" + auto_approve.AUTO_RETRY_MARKER}]
        auto_approve.list_reviews = lambda repo, pr: marked
        self.assertFalse(
            auto_approve.auto_retry_budget_left(self.Args(), [self.ERRORED])
        )


class ConstantParityTest(unittest.TestCase):
    """The gate hardcodes the marker; it must stay equal to the Python one."""

    def test_the_workflow_uses_the_same_marker(self):
        _block, run = step_run("Check for prior review on this commit")
        self.assertIn(auto_approve.AUTO_RETRY_MARKER, run)

    def test_the_marker_is_an_html_comment(self):
        # It rides in a review body a human reads; it must not be visible.
        self.assertTrue(auto_approve.AUTO_RETRY_MARKER.startswith("<!--"))
        self.assertTrue(auto_approve.AUTO_RETRY_MARKER.endswith("-->"))

    def test_one_retry_per_pr(self):
        self.assertEqual(1, auto_approve.MAX_AUTO_RETRIES)


class GateWiringTest(unittest.TestCase):
    """The two force paths stay separate, and the retry path is opt-in."""

    @classmethod
    def setUpClass(cls):
        cls.block, cls.body = step_run("Check for prior review on this commit")

    def test_the_input_reaches_the_step(self):
        self.assertIn(
            "AUTO_RETRY_ERRORED: ${{ inputs.auto_retry_errored_rounds }}", self.block
        )

    def test_the_retry_path_is_off_unless_enabled(self):
        self.assertIn('[ "${AUTO_RETRY_ERRORED:-}" = "true" ]', self.body)

    def test_the_retry_cannot_use_the_human_comment_path(self):
        # The human path scans PR comments and authorizes by association. Its
        # guard must depend on REREVIEW_COMMAND alone: if enabling auto-retry
        # could open that query, any App's comment could force rounds.
        lines = self.body.splitlines()
        idx = next(i for i, ln in enumerate(lines) if "FORCED=$(gh api" in ln)
        guard = next(
            ln for ln in reversed(lines[:idx])
            if ln.strip().startswith("if ") and ln.rstrip().endswith("then")
        )
        self.assertIn("REREVIEW_COMMAND", guard)
        self.assertNotIn("AUTO_RETRY_ERRORED", guard)

    def test_the_marker_check_is_permission_bounded(self):
        marker = self.body[self.body.index("RETRIED="):]
        self.assertIn('.user.type == "Bot"', marker)
        self.assertIn(".commit_id == $sha", marker)
        self.assertIn(".submitted_at > $since", marker)

    def test_the_forced_round_is_capped(self):
        self.assertIn('[ "$RETRIED" -le 1 ]', self.body)

    def test_a_non_numeric_count_cannot_force(self):
        self.assertIn("case \"$RETRIED\" in ''|*[!0-9]*) RETRIED=0 ;; esac", self.body)


class DelayTest(unittest.TestCase):
    """The wait: clamped, and never a reason the job fails."""

    @classmethod
    def setUpClass(cls):
        cls.block, cls.body = step_run("Wait before re-running")

    def test_it_only_runs_when_a_delay_is_configured(self):
        self.assertIn("inputs.auto_retry_delay_minutes != 0", self.block)

    def test_it_runs_before_the_relabel(self):
        text = workflow_text()
        self.assertLess(text.index("- name: Wait before re-running"),
                        text.index("- name: Remove and re-add the review label"))

    def test_the_job_timeout_covers_the_longest_wait(self):
        # The bug this pins: the relabel job budgeted 5 minutes, so any wait
        # past that killed the round the card had already promised — the one
        # failure mode where announcing the retry makes things worse than not
        # retrying at all.
        text = workflow_text()
        job = text[text.index("  auto-retry:"):]
        minutes = int(re.search(r"timeout-minutes: (\d+)", job).group(1))
        self.assertGreaterEqual(minutes, card.RETRY_DELAY_CAP)

    @unittest.skipIf(shutil.which("bash") is None, "bash not installed")
    def test_the_clamp(self):
        for given, want_sleep in (("0", 0), ("5", 300), ("30", 1800),
                                  ("31", 1800), ("999", 1800),
                                  ("abc", 0), ("", 0), ("-5", 0), ("1.5", 0)):
            with self.subTest(given=given):
                # Run the real body with `sleep` stubbed, so the test is fast
                # and the computed duration is observable.
                script = self.body.replace('sleep "$(( mins * 60 ))"',
                                          'echo "SLEPT=$(( mins * 60 ))"')
                proc = subprocess.run(
                    ["bash", "-c", script], capture_output=True, text=True,
                    env={**os.environ, "DELAY_MINUTES": given},
                )
                self.assertEqual(0, proc.returncode, proc.stderr)
                if want_sleep:
                    self.assertIn(f"SLEPT={want_sleep}", proc.stdout)
                else:
                    self.assertNotIn("SLEPT=", proc.stdout)
                    self.assertIn("immediately", proc.stdout)


class CardDelayTest(unittest.TestCase):
    """The advance notice: the card says the wait before the wait starts."""

    LABEL = "cursor-review"

    def test_no_delay_leaves_the_sentence_exactly_as_it_was(self):
        # Every caller that has not set the input reads this text, and
        # cursor-approve has no such input at all — so the zero case is a
        # compatibility assertion, not a formatting preference.
        self.assertEqual(
            card.next_auto_retry_text(self.LABEL),
            card.next_auto_retry_text(self.LABEL, 0),
        )
        self.assertNotIn("in about", card.next_auto_retry_text(self.LABEL, 0))

    def test_a_configured_delay_is_named(self):
        text = card.next_auto_retry_text(self.LABEL, 5)
        self.assertIn("in about 5 minutes", text)
        # Still the same next step, so an agent reading the card does not
        # conclude a human has to act.
        self.assertIn("being re-run automatically", text)
        self.assertIn("remove and re-add the `cursor-review` label", text)

    def test_one_minute_is_singular(self):
        self.assertIn("in about 1 minute (", card.next_auto_retry_text(self.LABEL, 1))

    def test_the_phrase_is_clamped_like_the_workflow(self):
        for given in (31, 999, 10 ** 9):
            with self.subTest(given=given):
                self.assertEqual(" in about 30 minutes",
                                 card.retry_delay_phrase(given))

    def test_an_unusable_delay_promises_no_wait(self):
        # The value is a caller input; it must not reach the sentence raw, and
        # a wait the workflow will not take must not be announced.
        for given in ("abc", "", None, -5, 0, "1.5", 0.5, [], "5; rm -rf /"):
            with self.subTest(given=given):
                self.assertEqual("", card.retry_delay_phrase(given))

    def test_the_card_cap_equals_the_workflow_cap(self):
        _block, body = step_run("Wait before re-running")
        self.assertIn(f'-gt {card.RETRY_DELAY_CAP} ]; then mins={card.RETRY_DELAY_CAP}',
                      body)

    def test_the_decide_step_passes_the_input(self):
        text = workflow_text()
        self.assertIn("RETRY_DELAY_MINUTES: ${{ inputs.auto_retry_delay_minutes }}", text)
        self.assertIn('--retry-delay-minutes "$RETRY_DELAY_MINUTES"', text)

    def test_the_text_helper_forwards_the_delay(self):
        seen = {}

        class Stub:
            @staticmethod
            def next_auto_retry_text(label, delay_minutes=0):
                seen.update(label=label, delay=delay_minutes)
                return "x"

        real = auto_approve._load_card
        auto_approve._load_card = lambda: Stub
        try:
            out = auto_approve._card_text("next_auto_retry_text", None, delay_minutes=7)
        finally:
            auto_approve._load_card = real
        self.assertEqual("x", out)
        self.assertEqual(7, seen["delay"])

    def test_an_older_card_without_the_keyword_degrades_to_empty(self):
        # The call site keeps its own fallback text, so a signature mismatch
        # must not raise out of the decision.
        class Stub:
            @staticmethod
            def next_auto_retry_text(label):
                return "x"

        real = auto_approve._load_card
        auto_approve._load_card = lambda: Stub
        try:
            self.assertEqual(
                "", auto_approve._card_text("next_auto_retry_text", None, delay_minutes=7))
        finally:
            auto_approve._load_card = real


@unittest.skipIf(shutil.which("jq") is None, "jq not installed")
class MarkerFilterTest(unittest.TestCase):
    """The real jq program for the retry signal, extracted and executed."""

    SHA = "abc123"
    SINCE = "2026-10-10T03:00:00Z"

    @classmethod
    def setUpClass(cls):
        _block, run = step_run("Check for prior review on this commit")
        seg = run[run.index("RETRIED=$(printf"):]
        start = seg.index("'[.[]") + 1
        cls.filter = seg[start:seg.index("' \\", start)]

    def review(self, body, user_type="Bot", sha=None, at="2026-10-10T04:00:00Z"):
        return {"commit_id": sha or self.SHA, "user": {"type": user_type},
                "submitted_at": at, "body": body}

    def count(self, reviews):
        proc = subprocess.run(
            ["jq", "--arg", "sha", self.SHA, "--arg", "since", self.SINCE,
             "--arg", "marker", auto_approve.AUTO_RETRY_MARKER, self.filter],
            input=json.dumps(reviews), capture_output=True, text=True,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        return int(proc.stdout.strip())

    def test_a_marked_block_newer_than_the_review_counts(self):
        self.assertEqual(
            1, self.count([self.review("blocked\n" + auto_approve.AUTO_RETRY_MARKER)])
        )

    def test_an_older_marked_block_does_not(self):
        # This is what stops one request re-firing forever.
        self.assertEqual(0, self.count([
            self.review("x\n" + auto_approve.AUTO_RETRY_MARKER,
                        at="2026-10-10T02:00:00Z")
        ]))

    def test_another_commit_does_not_count(self):
        self.assertEqual(0, self.count([
            self.review("x\n" + auto_approve.AUTO_RETRY_MARKER, sha="other")
        ]))

    def test_a_human_cannot_post_it(self):
        self.assertEqual(0, self.count([
            self.review("x\n" + auto_approve.AUTO_RETRY_MARKER, user_type="User")
        ]))

    def test_an_unmarked_bot_review_does_not_count(self):
        self.assertEqual(0, self.count([self.review("an ordinary block")]))

    def test_a_null_body_is_not_an_error(self):
        self.assertEqual(0, self.count([self.review(None)]))

    def test_an_empty_list_is_not_an_error(self):
        self.assertEqual(0, self.count([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
