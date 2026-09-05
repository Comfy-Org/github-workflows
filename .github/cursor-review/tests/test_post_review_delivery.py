#!/usr/bin/env python3
"""The blocking gate's delivery signal: what a zero exit does NOT prove.

THE FAILURE THIS PINS. `blocking: true`'s Blocking gate used to read
`needs.post-review.result == 'success'` as "a review carrying resolvable finding
threads landed on this PR". It does not. post-review.py exits 0 on at least four
paths that leave the PR with no cursor-review thread on it at all:

* a read-only token — the review is written to the job summary, never posted;
* the body-only "Review failed" error review, when the judge crashed;
* the no-findings review a run posts when EVERY panel cell errored;
* the 422 fallback, which drops the inline half to make the body postable.

Each satisfied the old guard, and each left the gate's `reviewThreads` query
empty — so the required check went GREEN over a round that reviewed nothing,
which is the fail-OPEN the gate exists to prevent. A fifth path needs no failure
at all: when every finding's anchor misses the reviewed diff, a perfectly
successful POST creates zero threads.

So the script states delivery POSITIVELY, on $GITHUB_OUTPUT, and the workflow
requires the statement instead of inferring it from an exit code:

    delivered         a real, adjudicated review reached the PR itself
    gated_findings    findings carrying an inline thread (the gate can hold on these)
    ungated_findings  findings that reached the review BODY only (no thread exists,
                      so no resolution can ever clear them)

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "post-review.py")
SPEC = importlib.util.spec_from_file_location("post_review_delivery", MODULE_PATH)
PR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PR)

# Two hunks on the NEW side: 11-13 anchor, anything else does not.
DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,3 +10,4 @@ def handler():
 ctx
+added one
+added two
 ctx tail
"""


def finding(path, line, severity="high", body="msg"):
    return {"file": path, "line": line, "severity": severity, "body": body}


class MainDriverMixin:
    """Drive main() with a stubbed `gh` and read what it wrote to $GITHUB_OUTPUT."""

    def setUp(self):
        # The emitter is once-per-process by design (the paths below fall through
        # each other, and duplicate keys in $GITHUB_OUTPUT are ambiguous), so the
        # module global has to be reset between cases.
        PR._DELIVERY_EMITTED = False
        # `None` = main() returned without raising. Set by run_main so a case can
        # pin the exit code alongside what was emitted — the read-only path's whole
        # point is that those two disagree.
        self.exit_code = None

    def run_main(
        self,
        findings,
        panel=None,
        post_returncode=0,
        stderr="",
        error_message=None,
        with_diff=True,
        fallback_ok=False,
    ):
        """Return (posted_payloads, delivery_dict). delivery is {} when nothing was written.

        `fallback_ok` models the real 422: the inline payload is what GitHub
        rejects, so the anchor-free retry that follows it succeeds.
        """
        posted = []

        def fake_post(repo, pr_number, payload):
            posted.append(json.loads(payload))
            rc, err = post_returncode, stderr
            if fallback_ok and len(posted) > 1:
                rc, err = 0, ""
            return subprocess.CompletedProcess(
                args=["gh"], returncode=rc, stdout="", stderr=err
            )

        if panel is None:
            panel = [{"model": "m", "review_type": "adversarial", "status": "ok"}]

        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, "consolidated.json")
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump({"findings": findings, "panel": panel}, f)
            outpath = os.path.join(d, "github_output")
            argv = [
                "post-review.py",
                "--findings", fpath,
                "--pr-number", "1",
                "--repo", "o/r",
                "--commit-sha", "deadbeef",
            ]
            if with_diff:
                dpath = os.path.join(d, "pr-diff.patch")
                with open(dpath, "w", encoding="utf-8") as f:
                    f.write(DIFF)
                argv += ["--diff", dpath]
            if error_message is not None:
                argv += ["--error-message", error_message]

            with mock.patch.object(PR, "gh_post_review", side_effect=fake_post), \
                 mock.patch.object(PR.sys, "argv", argv), \
                 mock.patch.object(PR, "write_step_summary", lambda *a, **k: None), \
                 mock.patch.dict(os.environ, {"GITHUB_OUTPUT": outpath}, clear=False):
                try:
                    PR.main()
                except SystemExit as exc:
                    self.exit_code = exc.code

            delivery = {}
            if os.path.exists(outpath):
                with open(outpath, encoding="utf-8") as f:
                    for raw in f.read().splitlines():
                        if "=" in raw:
                            k, _, v = raw.partition("=")
                            delivery[k] = v
        return posted, delivery


class DeliverySignalTest(MainDriverMixin, unittest.TestCase):
    """`delivered` — what a zero exit does NOT prove, for the blocking gate."""

    # --- the happy path is the only one that may green the gate --------------

    def test_a_posted_review_reports_its_threads_and_its_demoted_half(self):
        findings = [finding("app.py", 11), finding("app.py", 12), finding("app.py", 900)]
        posted, delivery = self.run_main(findings)
        self.assertEqual(len(posted), 1)
        self.assertEqual(len(posted[0]["comments"]), 2)
        self.assertEqual(delivery["delivered"], "true")
        self.assertEqual(delivery["gated_findings"], "2")
        # The out-of-hunk finding reached the body, so no thread holds the gate on it.
        self.assertEqual(delivery["ungated_findings"], "1")

    def test_a_clean_round_is_delivered_with_nothing_to_gate(self):
        posted, delivery = self.run_main([])
        self.assertEqual(len(posted), 1)
        self.assertIn("No high-signal findings", posted[0]["body"])
        self.assertEqual(delivery["delivered"], "true")
        self.assertEqual(delivery["gated_findings"], "0")
        self.assertEqual(delivery["ungated_findings"], "0")

    # --- the four zero-exit paths that must NOT green the gate ---------------

    def test_a_read_only_token_is_not_a_delivery(self):
        # The review reaches the job SUMMARY, not the PR — there is no thread on
        # the PR at all, so the gate's empty query means nothing.
        posted, delivery = self.run_main(
            [finding("app.py", 11)],
            post_returncode=1,
            stderr="gh: Resource not accessible by integration (HTTP 403)",
        )
        self.assertEqual(delivery["delivered"], "false")

    def test_the_error_review_is_not_a_delivery(self):
        # A posted "Review failed" body adjudicates nothing and anchors nothing.
        posted, delivery = self.run_main([], error_message="Judge call failed (status=error)")
        self.assertEqual(len(posted), 1)
        self.assertIn("Review failed", posted[0]["body"])
        self.assertEqual(delivery["delivered"], "false")

    def test_an_all_cells_failed_round_is_not_a_delivery(self):
        # Zero findings because every reviewer errored is not zero findings
        # because the PR is clean.
        posted, delivery = self.run_main(
            [],
            panel=[
                {"model": "m1", "review_type": "adversarial", "status": "error"},
                {"model": "m2", "review_type": "edge-case", "status": "error"},
            ],
        )
        self.assertEqual(len(posted), 1)
        self.assertIn("Panel did not produce any findings", posted[0]["body"])
        self.assertEqual(delivery["delivered"], "false")

    def test_a_genuine_post_failure_is_not_a_delivery(self):
        posted, delivery = self.run_main(
            [], post_returncode=1, stderr="gh: Server Error (HTTP 500)"
        )
        self.assertEqual(delivery["delivered"], "false")

    # --- delivered, but with nothing a thread query can see ------------------

    def test_the_422_fallback_delivers_every_finding_and_gates_none(self):
        findings = [finding("app.py", 11), finding("app.py", 900)]
        posted, delivery = self.run_main(
            findings,
            post_returncode=1,
            stderr="gh: Unprocessable Entity (HTTP 422)",
            fallback_ok=True,
        )
        self.assertEqual(len(posted), 2, "inline attempt, then the body-only fallback")
        self.assertNotIn("comments", posted[1])
        # The review DID reach the PR — but the inline half is exactly what was
        # dropped to make it postable, so every finding is ungated.
        self.assertEqual(delivery["delivered"], "true")
        self.assertEqual(delivery["gated_findings"], "0")
        self.assertEqual(delivery["ungated_findings"], "2")

    def test_a_fallback_that_also_failed_is_not_a_delivery(self):
        # Nothing reached the PR at all — the inline POST 422'd and the
        # anchor-free retry failed too, so the review exists only in the summary.
        findings = [finding("app.py", 11), finding("app.py", 900)]
        posted, delivery = self.run_main(
            findings, post_returncode=1, stderr="gh: Unprocessable Entity (HTTP 422)"
        )
        self.assertEqual(len(posted), 2)
        self.assertEqual(delivery["delivered"], "false")

    def test_a_fully_demoted_round_posts_findings_and_gates_none(self):
        # No failure anywhere: the POST succeeds, and still no thread exists,
        # because every anchor missed the reviewed diff.
        findings = [finding("elsewhere.py", 7), finding("elsewhere.py", 8)]
        posted, delivery = self.run_main(findings)
        self.assertEqual(posted[0]["comments"], [])
        self.assertIn("Found **2** finding(s)", posted[0]["body"])
        self.assertEqual(delivery["delivered"], "true")
        self.assertEqual(delivery["gated_findings"], "0")
        self.assertEqual(delivery["ungated_findings"], "2")

    # --- the contract itself -------------------------------------------------

    def test_nothing_is_written_when_the_script_never_decides(self):
        # A crash before any post leaves the output file untouched, so the gate
        # reads an unset `delivered` — which is not 'true'. Fail-closed by
        # default, which is the property that makes every path this file does
        # NOT enumerate safe too.
        def boom(*a, **k):
            raise RuntimeError("gh vanished")

        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, "consolidated.json")
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump({"findings": [finding("app.py", 11)], "panel": []}, f)
            outpath = os.path.join(d, "github_output")
            argv = [
                "post-review.py",
                "--findings", fpath,
                "--pr-number", "1",
                "--repo", "o/r",
                "--commit-sha", "deadbeef",
            ]
            with mock.patch.object(PR, "gh_post_review", side_effect=boom), \
                 mock.patch.object(PR.sys, "argv", argv), \
                 mock.patch.object(PR, "write_step_summary", lambda *a, **k: None), \
                 mock.patch.dict(os.environ, {"GITHUB_OUTPUT": outpath}, clear=False):
                with self.assertRaises(RuntimeError):
                    PR.main()
            self.assertFalse(
                os.path.exists(outpath) and open(outpath, encoding="utf-8").read().strip(),
                "a run that never decided must claim nothing",
            )

    def test_the_signal_is_written_once(self):
        with tempfile.TemporaryDirectory() as d:
            outpath = os.path.join(d, "github_output")
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": outpath}, clear=False):
                PR.emit_delivery(True, gated=3, ungated=1)
                PR.emit_delivery(False)
            with open(outpath, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln]
        self.assertEqual(
            lines,
            [
                "delivered=true",
                "gated_findings=3",
                "ungated_findings=1",
                # A delivery is always a post; see PostedSignalTest for why the
                # converse does not hold.
                "posted=true",
            ],
        )

    def test_no_github_output_is_not_an_error(self):
        # Runs outside Actions (a local reproduction) must still post the review.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
        with mock.patch.dict(os.environ, env, clear=True):
            PR.emit_delivery(True, gated=1)


class PostedSignalTest(MainDriverMixin, unittest.TestCase):
    """`posted` — the DM's question, which is NOT the gate's question (BE-10017).

    `notify-complete` DMs the triggerer "One consolidated review is on the PR."
    It used to key that purely on the post-review job's `result`, and this script
    exits 0 — green job — after a read-only-token 403, having written the review
    to the run's job summary instead. The DM then pointed a human at a PR
    carrying no review at all, which is the one failure mode a notification
    cannot afford: it is silently green.

    `delivered` cannot stand in for it, in BOTH directions. The error review and
    the all-cells-failed review reach the PR and are deliberately NOT deliveries
    (they adjudicate nothing, so the blocking gate must not go green on them) —
    keying the DM on `delivered` would report those as degraded while a review
    sits on the PR. So the script emits both, and each consumer reads its own.
    """

    def test_a_posted_review_says_so(self):
        _, delivery = self.run_main([finding("app.py", 11)])
        self.assertEqual(delivery["posted"], "true")
        self.assertEqual(delivery["delivered"], "true")

    def test_a_read_only_token_exits_zero_and_reports_not_posted(self):
        # The exact silent-green this signal exists for: zero exit, nothing on
        # the PR. The zero exit is deliberate and stays — a caller that forgot
        # `pull-requests: write` still gets its review in the job summary — so
        # the DM has to be told, not left to infer from the job result.
        posted, delivery = self.run_main(
            [finding("app.py", 11)],
            post_returncode=1,
            stderr="gh: Resource not accessible by integration (HTTP 403)",
        )
        self.assertEqual(len(posted), 1, "one attempt, rejected — no retry on a 403")
        self.assertIsNone(self.exit_code, "the read-only degradation still exits 0")
        self.assertEqual(delivery["posted"], "false")

    def test_a_bare_403_is_read_as_read_only_too(self):
        # is_read_only_token_error matches any HTTP 403, not just the integration
        # phrasing — pin that the weaker match reaches the same verdict.
        _, delivery = self.run_main(
            [finding("app.py", 11)],
            post_returncode=1,
            stderr="gh: HTTP 403: Forbidden",
        )
        self.assertIsNone(self.exit_code)
        self.assertEqual(delivery["posted"], "false")

    def test_a_genuine_post_failure_exits_one_and_never_claims_posted(self):
        # Both attempts fail for a non-403 reason. The job goes red, so the DM's
        # existing failure branch already covers it — but nothing may claim the
        # review posted, or a future consumer reading `posted` alone would lie.
        _, delivery = self.run_main(
            [finding("app.py", 11), finding("app.py", 900)],
            post_returncode=1,
            stderr="gh: Unprocessable Entity (HTTP 422)",
        )
        self.assertEqual(self.exit_code, 1)
        self.assertNotEqual(delivery.get("posted"), "true")

    def test_a_review_that_could_not_even_be_attempted_exits_one(self):
        # The no-inline-comments branch: the fallback would repost the same body,
        # so it is skipped entirely and the step goes red with nothing on the PR.
        _, delivery = self.run_main(
            [], post_returncode=1, stderr="gh: Server Error (HTTP 500)"
        )
        self.assertEqual(self.exit_code, 1)
        self.assertEqual(delivery["posted"], "false")

    def test_the_error_review_is_posted_but_not_delivered(self):
        # The direction that rules `delivered` out as the DM's signal: a "Review
        # failed" body DID reach the PR, so telling the author the review could
        # not be posted would be false.
        posted, delivery = self.run_main([], error_message="Judge call failed (status=error)")
        self.assertIn("Review failed", posted[0]["body"])
        self.assertEqual(delivery["delivered"], "false")
        self.assertEqual(delivery["posted"], "true")

    def test_an_all_cells_failed_round_is_posted_but_not_delivered(self):
        posted, delivery = self.run_main(
            [],
            panel=[
                {"model": "m1", "review_type": "adversarial", "status": "error"},
                {"model": "m2", "review_type": "edge-case", "status": "error"},
            ],
        )
        self.assertIn("Panel did not produce any findings", posted[0]["body"])
        self.assertEqual(delivery["delivered"], "false")
        self.assertEqual(delivery["posted"], "true")

    def test_the_422_fallback_counts_as_posted(self):
        # The inline payload 422'd and the anchor-free retry landed: a review IS
        # on the PR, so the DM's success text is honest here.
        _, delivery = self.run_main(
            [finding("app.py", 11), finding("app.py", 900)],
            post_returncode=1,
            stderr="gh: Unprocessable Entity (HTTP 422)",
            fallback_ok=True,
        )
        self.assertEqual(delivery["posted"], "true")

    def test_a_clamped_but_posted_review_is_still_posted(self):
        # `truncated` handling runs AFTER the emit, so a body GitHub made us cut
        # does not lose its claim — the cut review is on the PR either way.
        # Out of the reviewed hunk, so the finding is demoted into the BODY —
        # which is the half the clamp applies to.
        big = "x" * (PR.MAX_REVIEW_BODY_CHARS + 100)
        posted, delivery = self.run_main([finding("app.py", 900, body=big)])
        self.assertLessEqual(len(posted[0]["body"]), PR.MAX_REVIEW_BODY_CHARS)
        self.assertEqual(delivery["posted"], "true")

    def test_delivered_always_implies_posted(self):
        # An adjudicated review that never reached the PR is incoherent: it would
        # green the blocking gate while the DM reported a degradation. The
        # emitter repairs the pair rather than trusting call sites, so a future
        # delivery site that forgets `posted=True` cannot desync the two.
        with tempfile.TemporaryDirectory() as d:
            outpath = os.path.join(d, "github_output")
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": outpath}, clear=False):
                PR.emit_delivery(True, gated=1)
            with open(outpath, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln]
        self.assertIn("delivered=true", lines)
        self.assertIn("posted=true", lines)

    def test_the_signal_is_still_written_once(self):
        # Same once-guard as `delivered`: `posted` rides in the same emission, so
        # a later call cannot append a second, contradictory line.
        with tempfile.TemporaryDirectory() as d:
            outpath = os.path.join(d, "github_output")
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": outpath}, clear=False):
                PR.emit_delivery(False, posted=True)
                PR.emit_delivery(True, gated=3, ungated=1, posted=True)
            with open(outpath, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln]
        self.assertEqual(
            lines,
            ["delivered=false", "gated_findings=0", "ungated_findings=0", "posted=true"],
        )

    def test_no_github_output_is_still_not_an_error(self):
        # A local reproduction has no $GITHUB_OUTPUT; nothing consumes the signal
        # there and the helper must no-op rather than raise.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
        with mock.patch.dict(os.environ, env, clear=True):
            PR.emit_delivery(False, posted=True)

    def test_a_run_that_never_decides_claims_no_post_either(self):
        # Already pinned for `delivered` by the parent class's inherited case;
        # named here because the workflow's `posted` gate depends on the same
        # fail-closed default — an unset output is not 'true'.
        def boom(*a, **k):
            raise RuntimeError("gh vanished")

        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, "consolidated.json")
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump({"findings": [finding("app.py", 11)], "panel": []}, f)
            outpath = os.path.join(d, "github_output")
            argv = [
                "post-review.py",
                "--findings", fpath,
                "--pr-number", "1",
                "--repo", "o/r",
                "--commit-sha", "deadbeef",
            ]
            with mock.patch.object(PR, "gh_post_review", side_effect=boom), \
                 mock.patch.object(PR.sys, "argv", argv), \
                 mock.patch.object(PR, "write_step_summary", lambda *a, **k: None), \
                 mock.patch.dict(os.environ, {"GITHUB_OUTPUT": outpath}, clear=False):
                with self.assertRaises(RuntimeError):
                    PR.main()
            written = ""
            if os.path.exists(outpath):
                with open(outpath, encoding="utf-8") as f:
                    written = f.read()
        self.assertNotIn("posted=true", written)


class NotifyCompleteGateTest(unittest.TestCase):
    """The workflow half: the DM's success text must be unreachable without `posted`.

    A unit test cannot run the shell in `notify-complete`, but it can pin the two
    things that made this bug possible in the first place — the job never
    surfaced the script's statement as an output, and the DM never read one.
    """

    WORKFLOW = os.path.join(
        os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml"
    )

    def setUp(self):
        with open(self.WORKFLOW, encoding="utf-8") as f:
            self.text = f.read()

    def test_the_post_job_surfaces_posted_as_an_output(self):
        self.assertIn("posted: ${{ steps.post.outputs.posted }}", self.text)

    def test_the_dm_reads_the_post_jobs_posted_output(self):
        self.assertIn("REVIEW_POSTED: ${{ needs.post-review.outputs.posted }}", self.text)

    def test_the_success_dm_requires_both_the_job_result_and_the_signal(self):
        # The regression this file exists to stop coming back: a success branch
        # keyed on the job result ALONE goes green over the read-only 403.
        success_guard = (
            'if [ "$POST_REVIEW_RESULT" = "success" ] && [ "$REVIEW_POSTED" = "true" ]; then'
        )
        self.assertIn(success_guard, self.text)
        claim = "One consolidated review is on the PR."
        self.assertEqual(self.text.count(claim), 1, "one success claim, one guard")
        # The claim must sit AFTER the compound guard and BEFORE the next branch,
        # i.e. inside it.
        guard_at = self.text.index(success_guard)
        claim_at = self.text.index(claim)
        next_branch = self.text.index('elif [ "$POST_REVIEW_RESULT" = "success" ]; then')
        self.assertLess(guard_at, claim_at)
        self.assertLess(claim_at, next_branch)

    def test_the_degraded_branch_names_the_cause_and_warns(self):
        self.assertIn('TITLE="Cursor review degraded"', self.text)
        self.assertIn("could not be posted on the PR", self.text)
        self.assertIn("pull-requests: write", self.text)


if __name__ == "__main__":
    unittest.main()
