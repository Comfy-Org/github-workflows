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


class PathTokenTest(unittest.TestCase):
    """The `<path-slug>` segment extraction the backstop keys on (BE-4460)."""

    def test_extracts_segment_after_second_colon(self):
        self.assertEqual(ledger.path_token("cloud:whole-repo:src-tools-ts"), "src-tools-ts")
        self.assertEqual(ledger.path_token("repo:services-ingest:services-ingest-main-go"),
                         "services-ingest-main-go")

    def test_a_colon_in_the_scope_label_does_not_shear_the_token(self):
        # `<scope-label>` is a free-form workflow input and may contain a colon;
        # `<repo-basename>` and `<path-slug>` cannot (the slug collapses every
        # non-alphanumeric run to a hyphen). So the token is what follows the
        # LAST colon — counting from the left would yield `api:src-tools-ts`
        # here and break cross-scope matching for the same path.
        self.assertEqual(ledger.path_token("cloud:pkg:api:src-tools-ts"), "src-tools-ts")
        self.assertEqual(
            ledger.path_token("cloud:pkg:api:src-tools-ts"),
            ledger.path_token("cloud:whole-repo:src-tools-ts"),
        )

    def test_leading_and_trailing_hyphens_are_trimmed(self):
        # The verifier's slug rule read literally turns `services/ingest/` into
        # `services-ingest-`, while its worked example shows `services-ingest`.
        # Trimming reconciles the two so one directory has one token.
        self.assertEqual(ledger.path_token("repo:scope:services-ingest-"), "services-ingest")
        self.assertEqual(
            ledger.path_token("repo:scope:services-ingest-"),
            ledger.path_token("repo:scope:services-ingest"),
        )
        self.assertEqual(ledger.path_token("repo:scope:---"), "")

    def test_no_third_segment_has_no_token(self):
        # A malformed / pre-format signature has no path anchor and must never
        # collide with anything (else it would suppress unrelated findings).
        self.assertEqual(ledger.path_token("cloud:whole-repo"), "")
        self.assertEqual(ledger.path_token("just-a-slug"), "")
        self.assertEqual(ledger.path_token(""), "")
        self.assertEqual(ledger.path_token(None), "")
        self.assertEqual(ledger.path_token(123), "")

    def test_token_is_trimmed_and_lowercased_for_comparison(self):
        self.assertEqual(ledger.path_token("  cloud:whole-repo: SRC-Tools-TS \n"), "src-tools-ts")

    def test_empty_third_segment_has_no_token(self):
        self.assertEqual(ledger.path_token("cloud:whole-repo:"), "")
        self.assertEqual(ledger.path_token("cloud:whole-repo:   "), "")


class PathCollisionTest(unittest.TestCase):
    """The path-token backstop: one issue per anchoring path, not per wording.

    The incident this guards: the SAME `src/tools.ts` finding was filed twice
    because its signature was a slug of the LLM-generated title, which was
    re-worded between runs. Signatures are now path-anchored, and this backstop
    suppresses a re-keyed candidate whose path is already covered.
    """

    def setUp(self):
        # A known issue for `src/tools.ts`, filed under one scope label.
        self.led = ledger.Ledger({"repo:whole-repo:src-tools-ts": ledger.FILED})

    def test_same_path_under_a_different_signature_is_suppressed(self):
        # Same path, different leading segments (a legacy signature, a re-scoped
        # run) => exact-string dedup misses it; the path backstop catches it.
        to_file, suppressed, invalid = self.led.partition(
            [{"signature": "repo:src:src-tools-ts", "security": False, "title": "tools.ts is a monolith"}]
        )
        self.assertEqual(to_file, [])
        self.assertEqual(suppressed[0]["ledger_status"], ledger.PATH_COLLISION)
        self.assertEqual(invalid, [])

    def test_exact_signature_match_still_reports_its_ledger_status(self):
        # The backstop must not shadow the real status: an identical signature
        # is `filed`, not `path-collision`.
        _, suppressed, _ = self.led.partition([{"signature": "repo:whole-repo:src-tools-ts"}])
        self.assertEqual(suppressed[0]["ledger_status"], ledger.FILED)

    def test_different_path_is_not_suppressed(self):
        # No false suppression: a finding about another file still files, even
        # when the paths share tokens (`tools-ts`) — matching is exact, never
        # substring/fuzzy.
        to_file, suppressed, _ = self.led.partition([
            {"signature": "repo:whole-repo:src-server-ts", "title": "other file"},
            {"signature": "repo:whole-repo:test-tools-ts", "title": "same basename, other dir"},
        ])
        self.assertEqual([f["title"] for f in to_file], ["other file", "same basename, other dir"])
        self.assertEqual(suppressed, [])

    def test_legacy_title_slug_embedding_the_path_is_not_matched(self):
        # Documented limit (BE-4460): matching is EXACT on the path segment, so
        # a legacy TITLE-derived slug that merely *embeds* the path
        # (`split-tools-ts-into-modules`) does not block the new path-anchored
        # candidate — substring matching would silently drop real findings whose
        # files share a basename. Cost is bounded: at most one duplicate per
        # finding during the format transition, then it is stable forever.
        led = ledger.Ledger({"repo:whole-repo:split-tools-ts-into-focused-modules": ledger.FILED})
        to_file, _, _ = led.partition([{"signature": "repo:whole-repo:src-tools-ts"}])
        self.assertEqual(len(to_file), 1)

    def test_signature_without_a_path_segment_never_collides(self):
        led = ledger.Ledger({"legacy-flat-signature": ledger.FILED})
        to_file, _, _ = led.partition([{"signature": "another-flat-signature"}])
        self.assertEqual(len(to_file), 1)

    def test_intra_batch_path_collision_suppresses_the_second(self):
        # Within ONE run GitHub state is not refreshed, so two candidates
        # anchored to the same path must not open two issues either.
        led = ledger.Ledger({})
        to_file, suppressed, _ = led.partition([
            {"signature": "repo:whole-repo:src-tools-ts", "security": False, "title": "first"},
            {"signature": "repo:src:src-tools-ts", "security": False, "title": "second, re-keyed"},
        ])
        self.assertEqual([f["title"] for f in to_file], ["first"])
        self.assertEqual([(f["title"], f["ledger_status"]) for f in suppressed],
                         [("second, re-keyed", ledger.PATH_COLLISION)])

    def test_rejected_path_stays_suppressed_across_a_rewording(self):
        # A human rejection must survive the verifier re-keying the finding.
        led = ledger.Ledger({"repo:whole-repo:src-tools-ts": ledger.REJECTED})
        to_file, suppressed, _ = led.partition(
            [{"signature": "repo:whole-repo-v2:src-tools-ts", "security": False}]
        )
        self.assertEqual(to_file, [])
        self.assertEqual(suppressed[0]["ledger_status"], ledger.PATH_COLLISION)

    def test_should_file_agrees_with_partition(self):
        # The single-signature probe (`--check`) must not say "file it" for a
        # candidate `partition` suppresses.
        self.assertFalse(self.led.should_file("repo:src:src-tools-ts"))
        self.assertTrue(self.led.path_collides("repo:src:src-tools-ts"))
        self.assertTrue(self.led.should_file("repo:whole-repo:src-server-ts"))
        self.assertFalse(self.led.path_collides("repo:whole-repo:src-server-ts"))
        self.assertFalse(self.led.path_collides("no-path-segment"))

    def test_superseded_record_does_not_seed_the_path_index(self):
        # `groom-superseded` is the documented "retire this issue so the finding
        # can be re-filed under the current signature format" signal. If the
        # retired issue stayed in the path index it would keep suppressing the
        # replacement by path and defeat the label the human applied.
        led = ledger.Ledger({"repo:whole-repo:src-tools-ts": ledger.SUPERSEDED})
        to_file, suppressed, _ = led.partition([{"signature": "repo:src:src-tools-ts"}])
        self.assertEqual(len(to_file), 1)
        self.assertEqual(suppressed, [])

    def test_superseded_exact_signature_is_still_suppressed(self):
        # Only the PATH index drops superseded records; exact-signature dedup is
        # untouched, so the retired issue is not re-filed under its own key.
        led = ledger.Ledger({"repo:whole-repo:src-tools-ts": ledger.SUPERSEDED})
        to_file, suppressed, _ = led.partition([{"signature": "repo:whole-repo:src-tools-ts"}])
        self.assertEqual(to_file, [])
        self.assertEqual(suppressed[0]["ledger_status"], ledger.SUPERSEDED)


class SecurityExemptionTest(unittest.TestCase):
    """A path anchors a LOCATION, not a finding, so the backstop must never bury
    a security finding behind an already-filed routine finding on the same file.

    The rest of the pipeline guarantees security findings always surface as
    investigations (groom.yml applies `groom-security` and refuses to
    auto-implement them); a heuristic that suppresses a candidate whose own
    signature is NEW must not be able to break that.
    """

    def setUp(self):
        # A routine finding already filed for `src/tools.ts`.
        self.led = ledger.Ledger({"repo:whole-repo:src-tools-ts": ledger.FILED})

    def test_security_candidate_is_not_suppressed_by_path_collision(self):
        to_file, suppressed, _ = self.led.partition(
            [{"signature": "repo:src:src-tools-ts", "security": True, "title": "authz bypass"}]
        )
        self.assertEqual([f["title"] for f in to_file], ["authz bypass"])
        self.assertEqual(suppressed, [])

    def test_string_true_counts_as_security(self):
        # The verifier is an LLM writing JSON; it sometimes emits the STRING
        # "true" rather than the literal.
        to_file, _, _ = self.led.partition(
            [{"signature": "repo:src:src-tools-ts", "security": "TRUE"}]
        )
        self.assertEqual(len(to_file), 1)

    def test_an_unusable_flag_fails_closed(self):
        # The backstop decides a SECURITY guarantee from an LLM-authored field,
        # so ambiguity resolves toward surfacing the finding — same conservative
        # reading the build gate uses. A flag that is absent, null or mangled
        # exempts the candidate rather than risking a silently buried finding.
        # Cost is one extra issue; the next run suppresses it as `filed`.
        absent = object()
        for security in (None, "no", 0, "", "yes", absent):
            with self.subTest(security=security):
                finding = {"signature": "repo:src:src-tools-ts"}
                if security is not absent:
                    finding["security"] = security
                to_file, suppressed, _ = self.led.partition([finding])
                self.assertEqual(len(to_file), 1)
                self.assertEqual(suppressed, [])

    def test_only_a_provably_false_flag_is_subject_to_the_backstop(self):
        # The well-formed case: the verifier's schema requires `security` on
        # every finding, so real batches always land here and the backstop is
        # fully active for them.
        for security in (False, "false", "FALSE", " false "):
            with self.subTest(security=security):
                to_file, suppressed, _ = self.led.partition(
                    [{"signature": "repo:src:src-tools-ts", "security": security}]
                )
                self.assertEqual(to_file, [])
                self.assertEqual(suppressed[0]["ledger_status"], ledger.PATH_COLLISION)

    def test_exemption_does_not_bypass_exact_signature_dedup(self):
        # The exemption is bounded: it only skips the path HEURISTIC. Once the
        # security issue exists, its own signature is `filed` and the next run
        # suppresses it — so the exemption costs at most one issue, never a
        # per-run duplicate.
        led = ledger.Ledger({"repo:whole-repo:sec-src-tools-ts": ledger.FILED})
        to_file, suppressed, _ = led.partition(
            [{"signature": "repo:whole-repo:sec-src-tools-ts", "security": True}]
        )
        self.assertEqual(to_file, [])
        self.assertEqual(suppressed[0]["ledger_status"], ledger.FILED)

    def test_security_candidate_is_exempt_from_the_intra_batch_path_check(self):
        # Same guarantee within ONE run: a routine finding routed to `to_file`
        # first must not swallow a security finding on the same file.
        led = ledger.Ledger({})
        to_file, suppressed, _ = led.partition([
            {"signature": "repo:whole-repo:src-tools-ts", "title": "routine"},
            {"signature": "repo:src:src-tools-ts", "security": True, "title": "authz bypass"},
        ])
        self.assertEqual([f["title"] for f in to_file], ["routine", "authz bypass"])
        self.assertEqual(suppressed, [])

    def test_the_security_lane_prefix_is_domain_separated(self):
        # `verifier.md` prefixes a security finding's slug `sec_` — UNDERSCORE,
        # because slugifying a path can never produce one (every `_` in a path
        # collapses to a hyphen). A hyphen prefix would NOT be domain-separated:
        # a routine finding about `sec/auth.ts` slugifies to `sec-auth-ts` and
        # would share a signature with a security finding about `auth.ts`,
        # silently suppressing one of them. These two must stay distinct.
        routine_in_sec_dir = "repo:whole-repo:sec-auth-ts"     # sec/auth.ts, routine
        security_on_auth = "repo:whole-repo:sec_auth-ts"       # auth.ts, security
        self.assertNotEqual(
            ledger.path_token(routine_in_sec_dir), ledger.path_token(security_on_auth)
        )
        led = ledger.Ledger({routine_in_sec_dir: ledger.FILED})
        to_file, suppressed, _ = led.partition(
            [{"signature": security_on_auth, "security": False}]
        )
        # Suppression here would be the collision, not the exemption — so assert
        # it with the flag OFF, where the backstop is fully armed.
        self.assertEqual(len(to_file), 1)
        self.assertEqual(suppressed, [])

    def test_should_file_mirrors_the_exemption(self):
        sig = "repo:src:src-tools-ts"
        self.assertFalse(self.led.should_file(sig))
        self.assertTrue(self.led.should_file(sig, security=True))
        # A path-anchored key that is already `filed` is not filable either way.
        self.assertFalse(self.led.should_file("repo:whole-repo:src-tools-ts", security=True))

    def test_is_security_finding_helper(self):
        self.assertTrue(ledger.is_security_finding({"security": True}))
        self.assertTrue(ledger.is_security_finding({"security": " True "}))
        self.assertTrue(ledger.is_security_finding({}))          # fails closed
        self.assertTrue(ledger.is_security_finding({"security": None}))
        self.assertFalse(ledger.is_security_finding({"security": False}))
        self.assertFalse(ledger.is_security_finding({"security": " FALSE "}))
        # A non-dict is not a finding at all; `partition` routes it to `invalid`
        # before ever asking, so it must not be reported as security.
        self.assertFalse(ledger.is_security_finding("not a dict"))
        self.assertFalse(ledger.is_security_finding(None))


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

    # --- Path-anchored signatures (BE-4460) ---

    def test_same_file_reworded_next_run_is_not_refiled(self):
        # THE acceptance case. Run N filed the `src/tools.ts` monolith finding;
        # run N+1 describes the same file with different wording. Because the
        # signature is anchored to the PATH, not the title, run N+1 produces the
        # identical signature and is suppressed as already `filed` — one issue,
        # not two.
        sig = "repo:whole-repo:src-tools-ts"
        led = ledger.Ledger(ledger.build_ledger([issue(sig, state="open")]))
        to_file, suppressed, _ = led.partition(
            [{"signature": sig, "title": "src/tools.ts has grown into a monolith"}]
        )
        self.assertEqual(to_file, [])
        self.assertEqual(suppressed[0]["ledger_status"], ledger.FILED)

    def test_path_format_signature_marker_round_trip(self):
        # Marker round-trip is unchanged by the format: the signature is still
        # embedded and recovered verbatim (it stays an opaque string).
        sig = "repo:whole-repo:src-tools-ts"
        recovered = ledger.extract_signature(issue(sig)["body"])
        self.assertEqual(recovered, sig)
        self.assertEqual(ledger.path_token(recovered), "src-tools-ts")

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

    def test_decoy_eli5_heading_in_code_fence_is_rejected(self):
        # A `## ELI-5` that only appears inside a leading fenced code block never
        # renders as the opening heading — it must NOT be accepted as ELI-5-first.
        body = "```md\n## ELI-5\n```\n\n## What changed\n\nreal content."
        out = ledger.builder_pr_body(banner=self.BANNER, eli5_body=body,
                                     verifier_rationale="R.", signature="s")
        self.assertIn("## Verifier rationale", out)  # template fallback
        self.assertNotIn("<details>", out)

    def test_real_eli5_heading_after_code_fence_is_accepted(self):
        # A genuine ELI-5 heading is still detected even when an earlier fenced
        # block contains heading-shaped lines.
        body = "```\n# not a heading\n```\n\n## ELI-5\n\nplain words."
        out = ledger.builder_pr_body(banner=self.BANNER, eli5_body=body,
                                     verifier_rationale="R.", signature="s")
        self.assertIn("<details>", out)

    def test_comment_injection_in_body_is_neutralized(self):
        # An unclosed HTML comment in the builder body must not hide the rationale
        # or marker: the delimiters are escaped to visible text, and the ledger
        # marker still round-trips.
        body = "## ELI-5\n\nlooks fine <!-- everything after here is hidden"
        out = ledger.builder_pr_body(banner=self.BANNER, eli5_body=body,
                                     verifier_rationale="rationale stays visible", signature="sig-x")
        self.assertNotIn("<!--", out.replace(ledger.signature_marker("sig-x"), ""))
        self.assertIn("rationale stays visible", out)
        self.assertEqual(ledger.extract_signature(out), "sig-x")

    def test_details_injection_in_rationale_is_neutralized(self):
        # A `</details>` in the rationale must not close the wrapping section early.
        out = ledger.builder_pr_body(banner=self.BANNER, eli5_body=self.ELI5,
                                     verifier_rationale="oops </details> broke out", signature="s")
        self.assertNotIn("</details> broke out", out)
        self.assertIn("&lt;/details&gt;", out)

    def test_oversized_rationale_is_truncated_under_limit(self):
        huge = "X" * 200_000
        out = ledger.builder_pr_body(banner=self.BANNER, eli5_body=self.ELI5,
                                     verifier_rationale=huge, signature="sig-big")
        self.assertLessEqual(len(out), 65536)          # under GitHub's hard limit
        self.assertIn("truncated", out)
        self.assertTrue(out.rstrip().endswith("-->"))  # marker preserved LAST
        self.assertEqual(ledger.extract_signature(out), "sig-big")


class CheckCliTest(unittest.TestCase):
    """The `--check` single-signature probe, including its security variant.

    Exit 0 = "file it", 1 = suppressed; stdout is the reported status. The probe
    must agree with `partition`, so a routine probe reports `path-collision`
    where partition would suppress and `--check-security` reports `unknown`
    where partition's exemption would file.
    """

    STATUSES = {"repo:whole-repo:src-tools-ts": "filed"}

    def setUp(self):
        self._real_load = ledger.load_ledger
        ledger.load_ledger = lambda repo: ledger.Ledger(dict(self.STATUSES))
        self.addCleanup(setattr, ledger, "load_ledger", self._real_load)

    def _check(self, signature, *extra):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ledger.main(["--repo", "owner/name", "--check", signature, *extra])
        return code, out.getvalue().strip()

    def test_known_signature_is_suppressed(self):
        self.assertEqual(self._check("repo:whole-repo:src-tools-ts"), (1, "filed"))

    def test_new_path_is_filable(self):
        self.assertEqual(self._check("repo:whole-repo:src-server-ts"), (0, "unknown"))

    def test_blank_signature_is_invalid(self):
        self.assertEqual(self._check("   "), (1, "invalid"))

    def test_path_collision_is_reported_distinctly(self):
        self.assertEqual(self._check("repo:src:src-tools-ts"), (1, ledger.PATH_COLLISION))

    def test_check_security_skips_the_path_backstop(self):
        self.assertEqual(self._check("repo:src:src-tools-ts", "--check-security"), (0, "unknown"))

    def test_check_security_still_honors_exact_signature_dedup(self):
        self.assertEqual(
            self._check("repo:whole-repo:src-tools-ts", "--check-security"), (1, "filed")
        )


if __name__ == "__main__":
    unittest.main()
