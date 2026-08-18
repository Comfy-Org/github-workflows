"""Hermetic tests for lib.py — no network.

Covers the design's §11 acceptance cases that live in pure functions: candidate
extraction/dedup/cap, team-key input validation, the attachment policy gate (incl. multi-
link and state/team handling), Linear error classification, failure-category selection, the
failure copy, and the batched diagnostic-query builder. The real-Linear behaviours (URL
canonicalization, attachment timing) are proven in the pilot, not here.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib  # noqa: E402


class ExtractCandidates(unittest.TestCase):
    def test_uppercases(self):
        self.assertEqual(lib.extract_candidates("fixes be-1234 please"), ["BE-1234"])

    def test_dedupes_case_insensitively_first_seen(self):
        self.assertEqual(
            lib.extract_candidates("be-1234 ENG-7 BE-1234 eng-7"), ["BE-1234", "ENG-7"]
        )

    def test_reads_multiline_concatenation(self):
        self.assertEqual(
            lib.extract_candidates("luke/be-1-x\nTitle ENG-2\nCloses OPS-3"),
            ["BE-1", "ENG-2", "OPS-3"],
        )

    def test_no_identifier_is_empty(self):
        self.assertEqual(lib.extract_candidates("just a normal title"), [])

    def test_caps_at_20(self):
        text = " ".join(f"AB-{i}" for i in range(1, 26))
        self.assertEqual(len(lib.extract_candidates(text)), 20)

    def test_none_text_is_empty(self):
        self.assertEqual(lib.extract_candidates(None), [])


class NormalizeTeamKeys(unittest.TestCase):
    def test_empty_accepts_any(self):
        self.assertEqual(lib.normalize_team_keys(""), [])

    def test_whitespace_only_accepts_any(self):
        self.assertEqual(lib.normalize_team_keys("   "), [])

    def test_trims_and_uppercases(self):
        self.assertEqual(lib.normalize_team_keys(" be , eng "), ["BE", "ENG"])

    def test_tolerates_stray_empty_field(self):
        self.assertEqual(lib.normalize_team_keys("BE,,ENG"), ["BE", "ENG"])

    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            lib.normalize_team_keys("BE,be")

    def test_malformed_rejected(self):
        with self.assertRaises(ValueError):
            lib.normalize_team_keys("BE,EN!G")

    def test_leading_digit_rejected(self):
        with self.assertRaises(ValueError):
            lib.normalize_team_keys("1BE")


def _issue(identifier, key, state):
    return {"issue": {"identifier": identifier, "team": {"key": key}, "state": {"type": state}}}


class FilterIssues(unittest.TestCase):
    LINKED_OPEN_BE = [_issue("BE-1", "BE", "started")]
    LINKED_DONE_BE = [_issue("BE-1", "BE", "completed")]
    LINKED_OPEN_OPS = [_issue("OPS-9", "OPS", "backlog")]
    MIXED = [{"issue": None}, _issue("BE-2", "BE", "triage")]
    MULTI = [_issue("BE-1", "BE", "canceled"), _issue("ENG-5", "ENG", "started")]

    def test_canonical_open_passes_any_team(self):
        self.assertEqual(lib.filter_issues(self.LINKED_OPEN_BE, [], True), ["BE-1"])

    def test_empty_attachments_no_pass(self):
        self.assertEqual(lib.filter_issues([], [], True), [])

    def test_completed_rejected_when_require_open(self):
        self.assertEqual(lib.filter_issues(self.LINKED_DONE_BE, [], True), [])

    def test_completed_accepted_when_not_require_open(self):
        self.assertEqual(lib.filter_issues(self.LINKED_DONE_BE, [], False), ["BE-1"])

    def test_restricted_accepts_matching_team(self):
        self.assertEqual(lib.filter_issues(self.LINKED_OPEN_BE, ["BE", "ENG"], True), ["BE-1"])

    def test_restricted_rejects_other_team(self):
        self.assertEqual(lib.filter_issues(self.LINKED_OPEN_OPS, ["BE", "ENG"], True), [])

    def test_null_issue_ignored_real_one_passes(self):
        self.assertEqual(lib.filter_issues(self.MIXED, [], True), ["BE-2"])

    def test_multi_passes_when_one_satisfies(self):
        self.assertEqual(lib.filter_issues(self.MULTI, [], True), ["ENG-5"])

    def test_multi_empty_when_restricted_excludes_open_one(self):
        self.assertEqual(lib.filter_issues(self.MULTI, ["BE"], True), [])

    def test_count_linked(self):
        self.assertEqual(lib.count_linked(self.MIXED), 1)
        self.assertEqual(lib.count_linked([]), 0)
        self.assertEqual(lib.count_linked(self.MULTI), 2)


class ClassifyLinearError(unittest.TestCase):
    def test_ratelimited_is_retryable_even_on_400(self):
        self.assertEqual(lib.classify_linear_error(400, ["RATELIMITED"]), "retryable")

    def test_503_retryable(self):
        self.assertEqual(lib.classify_linear_error(503, []), "retryable")

    def test_429_retryable(self):
        self.assertEqual(lib.classify_linear_error(429, []), "retryable")

    def test_auth_terminal(self):
        self.assertEqual(lib.classify_linear_error(401, ["AUTHENTICATION_ERROR"]), "terminal")

    def test_schema_400_terminal(self):
        self.assertEqual(lib.classify_linear_error(400, ["GRAPHQL_VALIDATION_FAILED"]), "terminal")


class SelectFailureCategory(unittest.TestCase):
    def test_infra_dominates(self):
        self.assertEqual(lib.select_failure_category(True, 0, 0), "infra_error")

    def test_infra_dominates_even_with_links(self):
        self.assertEqual(lib.select_failure_category(True, 2, 3), "infra_error")

    def test_linked_but_policy(self):
        self.assertEqual(lib.select_failure_category(False, 1, 0), "policy_mismatch")

    def test_referenced_is_exists_not_linked(self):
        self.assertEqual(lib.select_failure_category(False, 0, 2), "exists_not_linked")

    def test_referenced_count_decides_not_resolution(self):
        self.assertEqual(lib.select_failure_category(False, 0, 1), "exists_not_linked")

    def test_nothing_is_no_candidate(self):
        self.assertEqual(lib.select_failure_category(False, 0, 0), "no_candidate")


class FailureGuidance(unittest.TestCase):
    def test_every_category_non_empty(self):
        for category in ("no_candidate", "exists_not_linked", "policy_mismatch", "infra_error"):
            self.assertTrue(lib.failure_guidance(category).strip())

    def test_unknown_category_raises(self):
        with self.assertRaises(KeyError):
            lib.failure_guidance("bogus")


class DiagnosticQuery(unittest.TestCase):
    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            lib.build_diagnostic_query([])

    def test_one_alias_per_candidate(self):
        query = lib.build_diagnostic_query(["BE-1", "ENG-2"])
        self.assertEqual(query.count("issueSearch"), 2)
        self.assertIn('query: "BE-1"', query)
        self.assertIn("c0: issueSearch", query)
        self.assertIn("c1: issueSearch", query)

    def test_count_resolved(self):
        resp = {"data": {"c0": {"nodes": [{"identifier": "BE-1"}]}, "c1": {"nodes": []}}}
        self.assertEqual(lib.count_resolved_candidates(resp), 1)

    def test_count_resolved_malformed_is_zero(self):
        self.assertEqual(lib.count_resolved_candidates({}), 0)
        self.assertEqual(lib.count_resolved_candidates({"errors": []}), 0)
        self.assertEqual(lib.count_resolved_candidates("not a dict"), 0)


if __name__ == "__main__":
    unittest.main()
