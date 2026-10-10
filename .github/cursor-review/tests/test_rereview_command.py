#!/usr/bin/env python3
"""Tests for `rereview_command` — asking for another round on the SAME commit.

WHY IT EXISTS. `Check for existing review` skips the panel when a non-dismissed
Bot review carrying the consolidated marker exists for the current HEAD SHA.
That dedupe is otherwise ABSOLUTE, and both documented escape hatches fail for
the case that actually wants one: pushing a commit is not an option when the
code is fine, and "dismiss the existing review" cannot be done at all, because
that review is posted COMMENTED and GitHub does not permit dismissing a
commented review. So a round that reached no decision because reviewers
ERRORED could only be retried by pushing a no-op commit.

TWO HALVES, AND BOTH ARE NEEDED. `cursor-review-auto-label.yml` turns the
comment into a label event (a label is the only thing cursor-review.yml
triggers on, and 49 of its expressions read `github.event.pull_request`, so it
cannot be called from an `issue_comment` event directly). `cursor-review.yml`
then has to not dedupe the round that arrives. Setting one without the other
is a silent no-op, so `BothHalvesTest` pins that the input exists on both.

Parsed WITHOUT PyYAML, like its sibling suites: this repo is stdlib-only and CI
installs no requirements for these tests.

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py' -v
"""

import io
import json
import os
import re
import shutil
import subprocess
import unittest

WORKFLOWS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "workflows")
)
REVIEW = os.path.join(WORKFLOWS, "cursor-review.yml")
AUTO_LABEL = os.path.join(WORKFLOWS, "cursor-review-auto-label.yml")

AUTHORIZED = ("OWNER", "MEMBER", "COLLABORATOR")
OUTSIDERS = ("CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN")


def read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def step(path, name_fragment):
    """One step's raw YAML block, located by a fragment of its `name:`."""
    text, out, cur = read(path), [], None
    for line in text.splitlines():
        if re.match(r"^      - name: ", line):
            if cur is not None:
                out.append("\n".join(cur))
            cur = [line] if name_fragment in line else None
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        out.append("\n".join(cur))
    assert len(out) == 1, f"{name_fragment!r} matched {len(out)} steps in {path}"
    return out[0]


def run_block(block):
    """The `run:` script of a step block, dedented."""
    m = re.search(r"^        run: \|\n(.*)", block, re.S | re.M)
    assert m, "step has no run: block"
    body = []
    for line in m.group(1).splitlines():
        if line.strip() and not line.startswith("          "):
            break
        body.append(line[10:])
    return "\n".join(body)


def jq_filter(script, after):
    """The single-quoted jq program that follows `after` in a shell script."""
    seg = script[script.index(after):]
    start = seg.index("'") + 1
    return seg[start:seg.index("' \\", start)]


class BothHalvesTest(unittest.TestCase):
    """The input must exist on both workflows, defaulting to off."""

    def test_the_input_exists_on_both_and_defaults_to_empty(self):
        for path in (REVIEW, AUTO_LABEL):
            with self.subTest(path=os.path.basename(path)):
                text = read(path)
                m = re.search(
                    r"^      rereview_command:\n(.*?)^      \w+:",
                    text, re.S | re.M,
                )
                self.assertIsNotNone(m, "input not declared")
                decl = m.group(1)
                self.assertIn("type: string", decl)
                self.assertIn("required: false", decl)
                self.assertRegex(decl, r"default:\s*''")

    def test_each_half_tells_the_reader_the_other_is_required(self):
        # Setting one alone is a silent no-op, which is the failure mode most
        # likely to be read as "the command does not work".
        self.assertIn("rereview_command", read(AUTO_LABEL))
        m = re.search(r"^      rereview_command:\n(.*?)^      \w+:",
                      read(AUTO_LABEL), re.S | re.M)
        self.assertIn("SAME value on cursor-review.yml", m.group(1))

    def test_the_gate_asks_for_nothing_the_documented_caller_does_not_grant(self):
        # A nested job cannot exceed its caller's grant, and the documented
        # caller grants only `contents` + `pull-requests`. An `issues: read`
        # here (once added for the comment read, which Pull requests already
        # authorizes on a PR) failed EVERY caller at startup on the pin bump.
        text = read(REVIEW)
        gate = text[text.index("\n  gate:"):text.index("\n  ledger:")]
        perms = re.search(r"(?m)^    permissions:\n((?:      .*\n)+)", gate)
        self.assertIsNotNone(perms)
        granted = set(re.findall(r"(?m)^      ([a-z-]+):", perms.group(1)))
        self.assertEqual({"contents", "pull-requests"}, granted)


class GateBypassTest(unittest.TestCase):
    """The dedupe step's shape: off by default, and one request buys one round."""

    @classmethod
    def setUpClass(cls):
        cls.block = step(REVIEW, "Check for prior review on this commit")
        cls.script = run_block(cls.block)

    def test_the_command_arrives_through_the_environment(self):
        self.assertIn("REREVIEW_COMMAND: ${{ inputs.rereview_command }}", self.block)

    def test_an_unset_command_cannot_force_anything(self):
        # `-n "${REREVIEW_COMMAND:-}"` is what keeps the default behaviour
        # byte-identical for every existing caller.
        self.assertIn('[ -n "${REREVIEW_COMMAND:-}" ]', self.script)

    def test_the_bypass_requires_a_comment_newer_than_the_review(self):
        # Otherwise one stale `/review` would force a round every time the
        # label was touched, forever.
        self.assertIn(".created_at > $since", self.script)
        self.assertIn("submitted_at", self.script)

    def test_reviews_are_read_from_every_page(self):
        # 30 per page, oldest first: page 1 alone drops the NEWEST round.
        self.assertIn('gh api --paginate "repos/$REPO/pulls/$PR_NUMBER/reviews"',
                      self.script)
        self.assertNotRegex(self.script, r'gh api "repos/\$REPO/pulls/\$PR_NUMBER/reviews"')

    def test_a_command_with_whitespace_is_warned_about(self):
        self.assertIn("::warning::rereview_command contains whitespace", self.script)

    def test_the_skip_branch_still_exists(self):
        self.assertIn("already_reviewed=true", self.script)
        self.assertIn("already_reviewed=false", self.script)

    def test_the_comment_body_is_never_echoed(self):
        # Public logs; a comment body is attacker-authored.
        for line in self.script.splitlines():
            if line.strip().startswith("echo"):
                self.assertNotIn("$COMMENT", line)
                self.assertNotIn(".body", line)


@unittest.skipIf(shutil.which("jq") is None, "jq not installed")
class GateFilterTest(unittest.TestCase):
    """The real jq program, run against the payload shapes it will meet.

    Extracted from the workflow rather than copied, so the test cannot drift
    from what ships. The input framing matches `gh api --paginate | jq -s`:
    one JSON array per page, slurped into an array of pages.
    """

    SINCE = "2026-10-10T03:00:00Z"

    @classmethod
    def setUpClass(cls):
        cls.filter = jq_filter(run_block(step(REVIEW, "Check for prior review on this commit")),
                               "FORCED=$(gh api")

    def count(self, pages, command="/review"):
        proc = subprocess.run(
            ["jq", "-s", "--arg", "cmd", command, "--arg", "since", self.SINCE,
             self.filter],
            input="\n".join(json.dumps(p) for p in pages),
            capture_output=True, text=True,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        return int(proc.stdout.strip())

    @staticmethod
    def comment(body, association="OWNER", at="2026-10-10T04:00:00Z"):
        return {"body": body, "author_association": association, "created_at": at}

    def test_a_plain_command_counts(self):
        self.assertEqual(1, self.count([[self.comment("/review")]]))

    def test_an_argument_is_allowed(self):
        self.assertEqual(1, self.count([[self.comment("/review please look again")]]))
        self.assertEqual(1, self.count([[self.comment("/review\targ")]]))

    def test_leading_whitespace_is_tolerated(self):
        self.assertEqual(1, self.count([[self.comment("   /review")]]))

    def test_a_crlf_body_counts(self):
        self.assertEqual(1, self.count([[self.comment("/review\r\nthanks")]]))

    def test_a_prefix_collision_does_not_count(self):
        # `startswith` would have matched both of these.
        for body in ("/reviewer", "/reviewed it", "/reviewing"):
            with self.subTest(body=body):
                self.assertEqual(0, self.count([[self.comment(body)]]))

    def test_the_command_must_open_the_comment(self):
        # Otherwise quoting someone else's `/review` would re-fire a round.
        self.assertEqual(0, self.count([[self.comment("hello\n/review")]]))
        self.assertEqual(0, self.count([[self.comment("> /review")]]))

    def test_a_blank_first_line_is_not_skipped(self):
        # Trimming the WHOLE body first would strip the newline and promote
        # line two; the auto-label half (`head -n 1`) never sees it either.
        for body in ("\n/review", "  \n/review", "\r\n/review"):
            with self.subTest(body=body):
                self.assertEqual(0, self.count([[self.comment(body)]]))

    def test_only_ascii_blanks_delimit_the_token(self):
        # Matches awk's default field split in the auto-label half: a body the
        # label step would not act on must not count here either.
        for body in ("/review\u00a0x", "\u00a0/review"):
            with self.subTest(body=body):
                self.assertEqual(0, self.count([[self.comment(body)]]))

    def test_a_comment_older_than_the_review_does_not_count(self):
        self.assertEqual(
            0, self.count([[self.comment("/review", at="2026-10-10T02:00:00Z")]])
        )

    def test_only_an_authorized_association_counts(self):
        for assoc in AUTHORIZED:
            with self.subTest(assoc=assoc):
                self.assertEqual(1, self.count([[self.comment("/review", assoc)]]))
        for assoc in OUTSIDERS:
            with self.subTest(assoc=assoc):
                self.assertEqual(0, self.count([[self.comment("/review", assoc)]]))

    def test_a_missing_or_empty_body_is_not_an_error(self):
        # The filter runs over every comment on the PR, so it meets these.
        for body in (None, "", "   ", "\n"):
            with self.subTest(body=body):
                self.assertEqual(0, self.count([[self.comment(body)]]))
        self.assertEqual(0, self.count([[{"author_association": "OWNER",
                                          "created_at": "2026-10-10T04:00:00Z"}]]))

    def test_it_reads_every_page(self):
        self.assertEqual(
            1, self.count([[self.comment("lgtm")], [self.comment("/review")]])
        )

    def test_an_empty_page_is_not_an_error(self):
        self.assertEqual(0, self.count([[]]))

    def test_the_command_string_is_configurable(self):
        pages = [[self.comment("/look-again")]]
        self.assertEqual(1, self.count(pages, command="/look-again"))
        self.assertEqual(0, self.count(pages, command="/review"))


class AutoLabelCommandTest(unittest.TestCase):
    """The step that turns the comment into a label event."""

    @classmethod
    def setUpClass(cls):
        cls.block = step(AUTO_LABEL, "asks for another round")
        cls.script = run_block(cls.block)

    def test_it_only_runs_on_a_comment_and_only_when_enabled(self):
        self.assertIn("github.event_name == 'issue_comment'", self.block)
        self.assertIn("inputs.rereview_command != ''", self.block)

    def test_only_a_new_comment_counts(self):
        # An edited or deleted old `/review` must not toggle the label.
        self.assertIn("github.event.action == 'created'", self.block)

    def test_the_job_prefilters_comments_before_a_runner_or_token(self):
        text = read(AUTO_LABEL)
        job = text[text.index("\n  auto-label:"):text.index("\n    steps:")]
        m = re.search(r"(?m)^    if: >-\n((?:      .*\n)+)", job)
        self.assertIsNotNone(m, "auto-label job has no if:")
        cond = m.group(1)
        self.assertIn("github.event_name != 'issue_comment'", cond)
        for term in ("inputs.rereview_command != ''",
                     "github.event.action == 'created'",
                     "github.event.issue.pull_request",
                     "github.event.issue.state == 'open'",
                     "github.event.comment.author_association"):
            self.assertIn(term, cond)
        for assoc in AUTHORIZED:
            self.assertIn(assoc, cond)
        for assoc in OUTSIDERS:
            self.assertNotIn(assoc, cond)

    def test_the_add_waits_for_the_remove_to_land(self):
        # Fired back to back, the `unlabeled` run can be created second and
        # cancel the `labeled` run through cursor-review's trigger slot.
        between = self.script[self.script.index("--remove-label"):
                              self.script.rindex("--add-label")]
        self.assertIn("--json labels", between)
        self.assertRegex(between, r"(?m)^\s+sleep \d+$")

    def test_a_command_with_whitespace_is_warned_about(self):
        self.assertIn("::warning::rereview_command contains whitespace", self.script)

    def test_the_comment_body_is_not_interpolated_into_the_script(self):
        # The injection guard: `${{ github.event.comment.body }}` inside a
        # `run:` would let a comment run shell. It must arrive via `env:`.
        self.assertIn("COMMENT_BODY: ${{ github.event.comment.body }}", self.block)
        self.assertNotIn("${{ github.event.comment.body }}", self.script)
        self.assertNotIn("${{", self.script)

    def test_it_ignores_comments_on_plain_issues(self):
        self.assertIn("IS_PR: ${{ github.event.issue.pull_request != null }}", self.block)
        self.assertIn('[ "$IS_PR" != "true" ]', self.script)

    def test_it_matches_the_first_token_not_a_prefix(self):
        self.assertIn("awk '{print $1}'", self.script)
        self.assertIn('"$first_token" != "$REREVIEW_COMMAND"', self.script)

    def test_authorization_is_the_comment_association(self):
        self.assertIn("ASSOCIATION: ${{ github.event.comment.author_association }}",
                      self.block)
        for assoc in AUTHORIZED:
            self.assertIn(assoc, self.script)
        for assoc in OUTSIDERS:
            self.assertNotIn(assoc, self.script)

    def test_it_removes_before_adding(self):
        # Adding a label that is already present raises NO event, and after a
        # round the label is always already present — so a re-add alone would
        # look exactly like the command being ignored.
        self.assertIn("--remove-label", self.script)
        self.assertLess(self.script.index("--remove-label"),
                        self.script.rindex("--add-label"))

    def test_a_closed_pr_is_left_alone(self):
        self.assertIn("--json labels,state", self.script)
        self.assertLess(self.script.index('!= "OPEN"'),
                        self.script.index("--remove-label"))

    def test_a_failed_re_add_is_retried_and_then_fails_loudly(self):
        # After the remove lands, one failed add would leave the PR unlabeled.
        self.assertRegex(self.script, r"for attempt in 1 2 3; do\n\s+if gh pr edit .*--add-label")
        self.assertIn("exit 1", self.script[self.script.rindex("--add-label"):])

    def test_the_skip_label_still_wins(self):
        self.assertIn("SKIP_LABEL", self.script)

    def test_the_body_is_never_echoed(self):
        for line in self.script.splitlines():
            if line.strip().startswith("echo"):
                self.assertNotIn("COMMENT_BODY", line)
                self.assertNotIn("first_token", line)


@unittest.skipIf(shutil.which("awk") is None, "awk not installed")
class FirstTokenTest(unittest.TestCase):
    """The shell half of the command match, run as the step runs it."""

    @classmethod
    def setUpClass(cls):
        cls.script = run_block(step(AUTO_LABEL, "asks for another round"))
        m = re.search(r"first_token=\$\((.*?)\)\n", cls.script, re.S)
        assert m, "first-token pipeline not found"
        cls.pipeline = m.group(1)

    def token(self, body):
        proc = subprocess.run(
            ["bash", "-c", f'COMMENT_BODY="$1"; printf "%s" "$({self.pipeline})"',
             "bash", body],
            capture_output=True, text=True,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc.stdout

    def test_it_extracts_the_command(self):
        self.assertEqual("/review", self.token("/review"))
        self.assertEqual("/review", self.token("/review please"))
        self.assertEqual("/review", self.token("   /review   "))
        self.assertEqual("/review", self.token("/review\r\nmore"))
        self.assertEqual("/review", self.token("/review\nsecond line"))

    def test_a_collision_yields_a_different_token(self):
        for body in ("/reviewer", "/reviewed", "/reviewing now"):
            with self.subTest(body=body):
                self.assertNotEqual("/review", self.token(body))

    def test_a_later_line_is_not_reached(self):
        self.assertNotEqual("/review", self.token("hello\n/review"))
        self.assertNotEqual("/review", self.token("\n/review"))

    def test_degenerate_bodies_yield_nothing(self):
        for body in ("", "   ", "\n", "\r\n"):
            with self.subTest(body=body):
                self.assertEqual("", self.token(body))


if __name__ == "__main__":
    unittest.main(verbosity=2)
