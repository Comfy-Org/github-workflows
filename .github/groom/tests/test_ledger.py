#!/usr/bin/env python3
"""Tests for the groom dedup/rejection ledger (BE-3874).

The core property the ledger must hold: a finding filed OR human-rejected in
run N is never re-filed in run N+1 (same signature), and a rejection is durable.
These tests drive the pure logic (marker round-trip, classification, ledger
build, partition) with no network, plus a stubbed `gh` fetch.

Run: python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
"""

import importlib.util
import json
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "ledger.py")
_spec = importlib.util.spec_from_file_location("groom_ledger", _MODULE_PATH)
ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger)


def issue(signature=None, *, state="open", state_reason=None, labels=("groom",), body=None, pr=False, merged_at=None):
    """Build a minimal GitHub-issue dict, embedding a marker unless body given.

    Set `pr=True` to model a builder pull request (the `/issues` listing returns
    PRs too, tagged with a `pull_request` object); `merged_at` is the merge
    timestamp GitHub stamps on that object when a PR merges (None = unmerged).
    """
    if body is None:
        body = "Some finding text.\n\n" + ledger.signature_marker(signature) if signature else "no marker"
    d = {
        "state": state,
        "state_reason": state_reason,
        "labels": [{"name": n} for n in labels],
        "body": body,
    }
    if pr:
        d["pull_request"] = {"url": "http://x", "merged_at": merged_at}
    return d


class MarkerRoundTripTest(unittest.TestCase):
    def test_round_trip(self):
        sig = "sha256:abcdef123"
        self.assertEqual(ledger.extract_signature(ledger.signature_marker(sig)), sig)

    def test_marker_embedded_in_prose(self):
        sig = "repo:rule-x:path/to/file.go:func"
        body = f"# A groom finding\n\nBlah blah.\n\n{ledger.signature_marker(sig)}\n\nmore text"
        self.assertEqual(ledger.extract_signature(body), sig)

    def test_no_marker_returns_none(self):
        self.assertIsNone(ledger.extract_signature("a plain human-written issue"))
        self.assertIsNone(ledger.extract_signature(""))
        self.assertIsNone(ledger.extract_signature(None))

    def test_normalize_trims_whitespace(self):
        self.assertEqual(ledger.normalize_signature("  sig  \n"), "sig")

    def test_signature_is_case_sensitive(self):
        # Opaque token — must NOT be lowercased (would collide distinct hashes).
        self.assertEqual(ledger.extract_signature(ledger.signature_marker("AbC")), "AbC")

    def test_round_trip_signature_with_comment_terminator(self):
        # A signature containing `-->` must not close the HTML comment early and
        # truncate the recovered key (which would re-file the finding forever).
        sig = "rule:x-->y:path/file.go:func"
        self.assertEqual(ledger.extract_signature(ledger.signature_marker(sig)), sig)

    def test_round_trip_signature_with_newlines_and_markup(self):
        sig = "line1\nline2 <b>markup</b> & <!-- nested -->"
        self.assertEqual(ledger.extract_signature(ledger.signature_marker(sig)), sig)

    def test_last_marker_wins_over_planted_shadow(self):
        # An attacker-controlled finding snippet can embed a marker-shaped
        # comment; the authoritative marker the filing step appends comes LAST
        # and must win, so the genuine signature is the one recovered.
        planted = ledger.signature_marker("forged-suppression-target")
        genuine = ledger.signature_marker("genuine-sig")
        body = f"Quoted code:\n\n{planted}\n\nfinding text\n\n{genuine}"
        self.assertEqual(ledger.extract_signature(body), "genuine-sig")

    def test_invalid_base64_payload_ignored(self):
        # A marker whose payload is not valid base64 must not poison a key —
        # both when out-of-alphabet chars stop the regex and when the payload is
        # in-alphabet but undecodable (bad length).
        self.assertIsNone(ledger.extract_signature("<!-- groom-signature: not*base64!! -->"))
        self.assertIsNone(ledger.extract_signature("<!-- groom-signature: A -->"))


class ClassifyIssueTest(unittest.TestCase):
    def test_open_issue_is_filed(self):
        self.assertEqual(ledger.classify_issue(issue("s", state="open")), ledger.FILED)

    def test_closed_completed_is_filed(self):
        # Fixed & closed → already handled, still suppressed (don't re-file).
        self.assertEqual(
            ledger.classify_issue(issue("s", state="closed", state_reason="completed")),
            ledger.FILED,
        )

    def test_closed_not_planned_is_rejected(self):
        # GitHub "Close as not planned" == wontfix → durable rejection.
        self.assertEqual(
            ledger.classify_issue(issue("s", state="closed", state_reason="not_planned")),
            ledger.REJECTED,
        )

    def test_rejected_label_open_is_rejected(self):
        # Label rejection works even without closing the issue.
        self.assertEqual(
            ledger.classify_issue(issue("s", state="open", labels=("groom", "groom-rejected"))),
            ledger.REJECTED,
        )

    def test_superseded_label(self):
        self.assertEqual(
            ledger.classify_issue(issue("s", labels=("groom", "groom-superseded"))),
            ledger.SUPERSEDED,
        )

    def test_rejected_label_beats_superseded(self):
        self.assertEqual(
            ledger.classify_issue(issue("s", labels=("groom", "groom-superseded", "groom-rejected"))),
            ledger.REJECTED,
        )

    # --- Builder PR states (BE-4003) ---

    def test_open_builder_pr_is_pr_open(self):
        self.assertEqual(ledger.classify_issue(issue("s", pr=True, state="open")), ledger.PR_OPEN)

    def test_merged_builder_pr_is_merged(self):
        # A merge stamps `merged_at` — the finding shipped, don't re-propose.
        self.assertEqual(
            ledger.classify_issue(issue("s", pr=True, state="closed", merged_at="2026-07-21T00:00:00Z")),
            ledger.MERGED,
        )

    def test_closed_unmerged_builder_pr_is_pr_closed(self):
        # Closed without merging == a human declined the fix — durable, never re-propose.
        self.assertEqual(
            ledger.classify_issue(issue("s", pr=True, state="closed", merged_at=None)),
            ledger.PR_CLOSED,
        )

    def test_rejected_label_on_open_pr_wins(self):
        # groom-rejected on an open builder PR is still a durable human "no".
        self.assertEqual(
            ledger.classify_issue(issue("s", pr=True, state="open", labels=("groom", "groom-rejected"))),
            ledger.REJECTED,
        )


class BuildLedgerTest(unittest.TestCase):
    def test_skips_issues_without_marker(self):
        # A human-opened groom issue with no marker must not create a key.
        led = ledger.build_ledger([issue(body="human wrote this, no marker")])
        self.assertEqual(len(led), 0)

    def test_skips_markerless_pull_requests(self):
        # A human PR labeled `groom` by hand (no signature marker) is not ours —
        # the marker check is what makes including PRs safe.
        led = ledger.build_ledger([issue(body="human PR, no marker", pr=True)])
        self.assertEqual(len(led), 0)

    def test_includes_signed_builder_pr(self):
        # A groom builder PR carries the marker AND the bot's `groom-pr` label →
        # it IS a ledger record now.
        led = ledger.build_ledger(
            [issue("built", pr=True, state="open", labels=("groom", "groom-pr"))]
        )
        self.assertEqual(led, {"built": ledger.PR_OPEN})

    def test_skips_pr_without_builder_label(self):
        # A `groom`-labeled PR carrying a pasted signature marker but NOT the
        # bot-applied `groom-pr` label is a spoof — it must not enter the ledger
        # (else anyone with label access could suppress a live finding).
        led = ledger.build_ledger([issue("spoof", pr=True, state="open")])
        self.assertEqual(len(led), 0)

    def test_spoof_pr_cannot_suppress_live_issue(self):
        # A genuine open issue for a signature stays FILED even if a hand-opened
        # `groom` PR (no `groom-pr` label) pastes the same marker and is closed
        # unmerged to try to force a `pr-closed` suppression.
        led = ledger.build_ledger([
            issue("dup", state="open"),
            issue("dup", pr=True, state="closed", merged_at=None),  # no groom-pr
        ])
        self.assertEqual(led["dup"], ledger.FILED)

    def test_pr_closed_beats_open_issue_for_same_signature(self):
        # A finding filed as an issue AND later built into a declined builder PR
        # (carrying `groom-pr`): the human decline (pr-closed) is the most
        # decision-bearing status.
        led = ledger.build_ledger([
            issue("dup", state="open"),
            issue("dup", pr=True, state="closed", merged_at=None,
                  labels=("groom", "groom-pr")),
        ])
        self.assertEqual(led["dup"], ledger.PR_CLOSED)

    def test_rejection_wins_when_duplicate_signatures(self):
        # Same signature on a filed AND a rejected issue → rejected surfaces.
        led = ledger.build_ledger(
            [
                issue("dup", state="open"),
                issue("dup", state="closed", state_reason="not_planned"),
            ]
        )
        self.assertEqual(led["dup"], ledger.REJECTED)

    def test_mixed_repo(self):
        led = ledger.build_ledger(
            [
                issue("filed-sig", state="open"),
                issue("rejected-sig", state="closed", state_reason="not_planned"),
                issue("super-sig", labels=("groom", "groom-superseded")),
                issue(body="no marker human issue"),
            ]
        )
        self.assertEqual(led, {
            "filed-sig": ledger.FILED,
            "rejected-sig": ledger.REJECTED,
            "super-sig": ledger.SUPERSEDED,
        })


class LedgerDecisionTest(unittest.TestCase):
    def setUp(self):
        self.led = ledger.Ledger({
            "filed": ledger.FILED,
            "rejected": ledger.REJECTED,
            "super": ledger.SUPERSEDED,
        })

    def test_unknown_should_file(self):
        self.assertTrue(self.led.should_file("brand-new"))
        self.assertFalse(self.led.is_known("brand-new"))
        self.assertEqual(self.led.status("brand-new"), ledger.UNKNOWN)

    def test_blank_signature_is_not_filable(self):
        # Mirrors partition's `invalid` routing: an empty/missing/non-string
        # signature has no recoverable marker, so it must NOT be filed (else it
        # re-files every run). Guards the single-signature `should_file`/`--check`
        # path against disagreeing with `partition`.
        self.assertFalse(self.led.should_file(""))
        self.assertFalse(self.led.should_file("   "))
        self.assertFalse(self.led.should_file(None))
        self.assertFalse(self.led.should_file(123))

    def test_filed_suppressed(self):
        self.assertFalse(self.led.should_file("filed"))

    def test_rejected_suppressed(self):
        # The load-bearing acceptance case: a human rejection stays suppressed.
        self.assertFalse(self.led.should_file("rejected"))
        self.assertTrue(self.led.is_known("rejected"))

    def test_superseded_suppressed(self):
        self.assertFalse(self.led.should_file("super"))

    def test_status_lookup_normalizes(self):
        self.assertEqual(self.led.status("  filed \n"), ledger.FILED)

    def test_partition(self):
        findings = [
            {"signature": "brand-new", "title": "A"},
            {"signature": "filed", "title": "B"},
            {"signature": "rejected", "title": "C"},
            {"signature": "", "title": "D no sig"},
            {"title": "E missing sig key"},
            "not even a dict",
        ]
        to_file, suppressed, invalid = self.led.partition(findings)
        self.assertEqual([f["title"] for f in to_file], ["A"])
        self.assertEqual({f["title"]: f["ledger_status"] for f in suppressed},
                         {"B": ledger.FILED, "C": ledger.REJECTED})
        self.assertEqual(len(invalid), 3)

    def test_partition_dedups_within_batch(self):
        # Two findings sharing ONE new signature must not both be filed in a
        # single run — the ledger only refreshes from GitHub between runs, so a
        # second issue would be the exact duplicate spam this exists to prevent.
        findings = [
            {"signature": "new-dup", "title": "first"},
            {"signature": "new-dup", "title": "second"},
            {"signature": "  new-dup \n", "title": "third-whitespace-variant"},
        ]
        to_file, suppressed, invalid = self.led.partition(findings)
        self.assertEqual([f["title"] for f in to_file], ["first"])
        self.assertEqual(
            [(f["title"], f["ledger_status"]) for f in suppressed],
            [("second", ledger.PENDING), ("third-whitespace-variant", ledger.PENDING)],
        )
        self.assertEqual(invalid, [])


class AcceptanceScenarioTest(unittest.TestCase):
    """End-to-end run N -> run N+1 using the ledger built from prior issues."""

    def test_filed_then_not_refiled(self):
        # Run N filed signature "x" (an open issue now exists). Run N+1:
        led = ledger.Ledger(ledger.build_ledger([issue("x", state="open")]))
        _, suppressed, _ = led.partition([{"signature": "x"}])
        self.assertEqual(len(suppressed), 1)

    def test_human_rejection_durably_suppresses(self):
        # Run N filed "y"; a human closed it as not planned. Run N+1 must NOT re-file.
        led = ledger.Ledger(
            ledger.build_ledger([issue("y", state="closed", state_reason="not_planned")])
        )
        to_file, suppressed, _ = led.partition([{"signature": "y"}])
        self.assertEqual(to_file, [])
        self.assertEqual(suppressed[0]["ledger_status"], ledger.REJECTED)

    def test_new_finding_still_files(self):
        led = ledger.Ledger(ledger.build_ledger([issue("y", state="open")]))
        to_file, _, _ = led.partition([{"signature": "z-new"}])
        self.assertEqual(len(to_file), 1)

    # --- Builder auto-PR dedup (BE-4003, acceptance criterion 3) ---

    # Builder PRs carry the bot-applied `groom-pr` label — that's what admits
    # them to the ledger (a marker alone on a hand-opened PR does not).
    _PR_LABELS = ("groom", "groom-pr")

    def test_open_builder_pr_suppresses_reproposal(self):
        # Run N built "b" into an OPEN PR. Run N+1 must NOT re-propose it.
        led = ledger.Ledger(ledger.build_ledger(
            [issue("b", pr=True, state="open", labels=self._PR_LABELS)]
        ))
        to_file, suppressed, _ = led.partition([{"signature": "b"}])
        self.assertEqual(to_file, [])
        self.assertEqual(suppressed[0]["ledger_status"], ledger.PR_OPEN)

    def test_merged_builder_pr_never_reproposed(self):
        # A merged builder PR means the fix shipped — never re-propose.
        led = ledger.Ledger(ledger.build_ledger(
            [issue("b", pr=True, state="closed", merged_at="2026-07-21T00:00:00Z",
                   labels=self._PR_LABELS)]
        ))
        to_file, suppressed, _ = led.partition([{"signature": "b"}])
        self.assertEqual(to_file, [])
        self.assertEqual(suppressed[0]["ledger_status"], ledger.MERGED)

    def test_closed_builder_pr_never_reproposed(self):
        # A human closed the builder PR unmerged — durable decline, never re-propose.
        led = ledger.Ledger(ledger.build_ledger(
            [issue("b", pr=True, state="closed", merged_at=None, labels=self._PR_LABELS)]
        ))
        to_file, suppressed, _ = led.partition([{"signature": "b"}])
        self.assertEqual(to_file, [])
        self.assertEqual(suppressed[0]["ledger_status"], ledger.PR_CLOSED)


class FetchTest(unittest.TestCase):
    """Stub `gh api` to exercise the I/O shell without network."""

    class _Result:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def test_fetch_parses_single_array(self):
        payload = json.dumps([issue("a"), issue("b")])
        run = lambda *a, **k: self._Result(0, stdout=payload)
        issues = ledger.fetch_groom_issues("o/r", run=run)
        self.assertEqual(len(issues), 2)

    def test_fetch_parses_concatenated_pages(self):
        # --paginate can emit concatenated top-level arrays; must not truncate.
        payload = json.dumps([issue("a")]) + "\n" + json.dumps([issue("b"), issue("c")])
        run = lambda *a, **k: self._Result(0, stdout=payload)
        self.assertEqual(len(ledger.fetch_groom_issues("o/r", run=run)), 3)

    def test_fetch_raises_on_error(self):
        run = lambda *a, **k: self._Result(1, stderr="boom")
        with self.assertRaises(RuntimeError):
            ledger.fetch_groom_issues("o/r", run=run)

    def test_fetch_rejects_malformed_repo(self):
        # A repo with URL metacharacters / extra path segments could override
        # the labels/state query or redirect the endpoint — reject before the
        # gh call so it can never corrupt the issue set (never calls `run`).
        never = lambda *a, **k: self.fail("run must not be called for a bad repo")
        for bad in ("o/r?labels=other", "o/r/extra", "o r", "", "justname"):
            with self.assertRaises(ValueError):
                ledger.fetch_groom_issues(bad, run=never)

    def test_fetch_raises_on_timeout(self):
        import subprocess as _sp

        def run(*a, **k):
            raise _sp.TimeoutExpired(cmd="gh", timeout=k.get("timeout", 0))

        with self.assertRaises(RuntimeError):
            ledger.fetch_groom_issues("o/r", run=run)

    def test_empty_output(self):
        run = lambda *a, **k: self._Result(0, stdout="")
        self.assertEqual(ledger.fetch_groom_issues("o/r", run=run), [])

    def test_load_ledger_end_to_end(self):
        payload = json.dumps([
            issue("open-one", state="open"),
            issue("rejected-one", state="closed", state_reason="not_planned"),
        ])
        run = lambda *a, **k: self._Result(0, stdout=payload)
        led = ledger.load_ledger("o/r", run=run)
        self.assertTrue(led.should_file("something-new"))
        self.assertFalse(led.should_file("open-one"))
        self.assertFalse(led.should_file("rejected-one"))


class BuilderPrBodyTest(unittest.TestCase):
    """The auto-builder PR body assembler (BE-4346).

    Properties: the builder-authored ELI-5 body leads; the verifier rationale is
    kept as a secondary `<details>` section; the banner is FIRST and the ledger
    marker is LAST (so the next run still dedups the finding and the marker can't
    be spoofed from the model body); and an empty / non-ELI-5 body falls back to
    the original template rather than opening an empty-body PR.
    """

    BANNER = "> 🤖 **Auto-built by the groom sweep** — review required. · [run](http://x)"
    ELI5 = ("## ELI-5\n\nWe renamed a helper so the two call sites read the same.\n\n"
            "## What changed\n\nExtracted `fmt()` in `a.go` and `b.go`.\n\n"
            "## Why\n\nLess duplication; behavior is identical.")

    def test_builder_body_leads_and_wraps_rationale(self):
        out = ledger.builder_pr_body(banner=self.BANNER, eli5_body=self.ELI5,
                                     verifier_rationale="The verifier said X.", signature="sig-1")
        self.assertTrue(out.startswith(self.BANNER))            # banner first
        self.assertIn("## ELI-5", out)
        self.assertLess(out.index("## ELI-5"), out.index("The verifier said X."))  # ELI-5 before rationale
        self.assertIn("<details>", out)
        self.assertIn("The verifier said X.", out)
        self.assertEqual(ledger.extract_signature(out), "sig-1")  # marker recoverable
        # Marker is LAST: nothing but whitespace after it.
        self.assertRegex(out, r"-->\s*\Z")

    def test_fallback_when_body_empty(self):
        out = ledger.builder_pr_body(banner=self.BANNER, eli5_body="",
                                     verifier_rationale="Rationale here.", signature="sig-2")
        self.assertTrue(out.startswith(self.BANNER))
        self.assertIn("## Verifier rationale", out)             # original template
        self.assertNotIn("<details>", out)
        self.assertIn("Rationale here.", out)
        self.assertEqual(ledger.extract_signature(out), "sig-2")

    def test_fallback_when_body_lacks_eli5_heading(self):
        # A body whose FIRST heading isn't ELI-5 is unusable → template fallback,
        # guaranteeing every builder-body PR opens with ELI-5.
        body = "## Summary\n\nDid a thing.\n\n## ELI-5\n\ntoo late, not first."
        out = ledger.builder_pr_body(banner=self.BANNER, eli5_body=body,
                                     verifier_rationale="R.", signature="sig-3")
        self.assertIn("## Verifier rationale", out)
        self.assertNotIn("<details>", out)

    def test_eli5_heading_variants_are_accepted(self):
        for heading in ("## ELI-5", "## ELI5", "### ELI-5: overview", "#  eli 5"):
            body = f"{heading}\n\nplain words."
            out = ledger.builder_pr_body(banner=self.BANNER, eli5_body=body,
                                         verifier_rationale="R.", signature="s")
            self.assertIn("<details>", out, f"{heading!r} should be accepted as ELI-5")

    def test_spoofed_marker_in_body_cannot_shadow_real_signature(self):
        # A prompt-injected body embedding a marker for a DIFFERENT signature must
        # NOT poison the ledger: extract_signature reads the LAST marker, and the
        # real one is appended after the body.
        evil = ledger.signature_marker("attacker-sig")
        body = f"## ELI-5\n\nlooks fine {evil}\n\nmore."
        out = ledger.builder_pr_body(banner=self.BANNER, eli5_body=body,
                                     verifier_rationale="R.", signature="real-sig")
        self.assertEqual(ledger.extract_signature(out), "real-sig")

    def test_whitespace_only_body_falls_back(self):
        out = ledger.builder_pr_body(banner=self.BANNER, eli5_body="   \n  ",
                                     verifier_rationale="R.", signature="s")
        self.assertIn("## Verifier rationale", out)
        self.assertNotIn("<details>", out)


if __name__ == "__main__":
    unittest.main()
