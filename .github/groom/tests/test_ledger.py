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


def issue(signature=None, *, state="open", state_reason=None, labels=("groom",), body=None, pr=False):
    """Build a minimal GitHub-issue dict, embedding a marker unless body given."""
    if body is None:
        body = "Some finding text.\n\n" + ledger.signature_marker(signature) if signature else "no marker"
    d = {
        "state": state,
        "state_reason": state_reason,
        "labels": [{"name": n} for n in labels],
        "body": body,
    }
    if pr:
        d["pull_request"] = {"url": "http://x"}
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


class BuildLedgerTest(unittest.TestCase):
    def test_skips_issues_without_marker(self):
        # A human-opened groom issue with no marker must not create a key.
        led = ledger.build_ledger([issue(body="human wrote this, no marker")])
        self.assertEqual(len(led), 0)

    def test_skips_pull_requests(self):
        led = ledger.build_ledger([issue("s", pr=True)])
        self.assertEqual(len(led), 0)

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


if __name__ == "__main__":
    unittest.main()
