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
            lib.extract_candidates("feat/be-1-x\nTitle ENG-2\nCloses OPS-3"),
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


class ParseActorList(unittest.TestCase):
    def test_empty_is_no_exemption(self):
        self.assertEqual(lib.parse_actor_list(""), [])
        self.assertEqual(lib.parse_actor_list("   "), [])

    def test_lowercases_and_trims(self):
        self.assertEqual(
            lib.parse_actor_list(" Dependabot[bot] , renovate[bot] "),
            ["dependabot[bot]", "renovate[bot]"],
        )

    def test_tolerates_stray_empty_field(self):
        self.assertEqual(lib.parse_actor_list("a,,b"), ["a", "b"])


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


ALL_CATEGORIES = ("no_candidate", "exists_not_linked", "policy_mismatch", "infra_error")


class FailureOutcomeModes(unittest.TestCase):
    """The reporting-mode table in lib.py. The invariant under all of it: soft-fail changes
    only how LOUD a red verdict is, never the diagnosis and never the exit code contract."""

    def test_every_category_has_copy_for_every_surface(self):
        # ALL_CATEGORIES must be exhaustive, or the loops below silently stop covering a
        # category; and _STATUS_HEADLINE must cover exactly what _GUIDANCE does, or
        # failure_outcome raises KeyError on a category that passed its own guard.
        self.assertEqual(set(ALL_CATEGORIES), set(lib._GUIDANCE))
        self.assertEqual(set(lib._STATUS_HEADLINE), set(lib._GUIDANCE))

    def test_enforce_is_red_and_exits_nonzero(self):
        outcome = lib.failure_outcome("no_candidate", enforce=True, soft_fail=False)
        self.assertEqual(outcome.state, "failure")
        self.assertEqual(outcome.exit_code, 1)
        self.assertFalse(outcome.advisory)

    def test_enforce_ignores_soft_fail(self):
        # soft-fail is a warn-only knob; enforcing already publishes the red status it exists
        # to produce, and must still exit nonzero.
        self.assertEqual(lib.failure_outcome("no_candidate", enforce=True, soft_fail=True),
                         lib.failure_outcome("no_candidate", enforce=True, soft_fail=False))

    def test_soft_fail_is_red_but_exits_zero(self):
        outcome = lib.failure_outcome("exists_not_linked", enforce=False, soft_fail=True)
        self.assertEqual(outcome.state, "failure")   # loud: the PR check list shows a red X
        self.assertEqual(outcome.exit_code, 0)       # but the run itself stays green
        self.assertTrue(outcome.advisory)            # and the comment says it does not block

    def test_silent_warn_only_is_green(self):
        outcome = lib.failure_outcome("exists_not_linked", enforce=False, soft_fail=False)
        self.assertEqual(outcome.state, "success")
        self.assertEqual(outcome.exit_code, 0)
        self.assertFalse(outcome.advisory)

    def test_only_enforce_ever_exits_nonzero(self):
        # Scope: the VERDICT's exit code. The job can still exit nonzero in warn-only for
        # reasons that are not a verdict — a failed terminal status write (finish_fail), or a
        # broken run (missing GH_REPO/LINEAR_API_TOKEN, malformed team-keys, a non-
        # pull_request event). The docs say "a failing verdict", not "the job", for that
        # reason.
        for category in ALL_CATEGORIES:
            for soft_fail in (True, False):
                self.assertEqual(
                    lib.failure_outcome(category, enforce=False, soft_fail=soft_fail).exit_code,
                    0, f"warn-only must not exit nonzero ({category}, soft_fail={soft_fail})")

    def test_advisory_only_when_red_and_non_gating(self):
        # The "does not block" note must never ride along with an enforcing (gating) verdict —
        # that would tell an author to ignore a check that is actually blocking them.
        for category in ALL_CATEGORIES:
            for enforce in (True, False):
                for soft_fail in (True, False):
                    outcome = lib.failure_outcome(category, enforce, soft_fail)
                    self.assertEqual(outcome.advisory,
                                     not enforce and soft_fail and outcome.state == "failure")

    def test_category_is_named_in_every_verdict_and_description(self):
        for category in ALL_CATEGORIES:
            for enforce in (True, False):
                for soft_fail in (True, False):
                    outcome = lib.failure_outcome(category, enforce, soft_fail)
                    self.assertIn(category, outcome.verdict)
                    self.assertIn(category, outcome.description)

    def test_description_fits_the_github_status_limit(self):
        # publish_status truncates at 140; a description that only ever survives truncation
        # would silently lose the category, so keep every mode's copy inside the limit.
        for category in ALL_CATEGORIES:
            for enforce in (True, False):
                for soft_fail in (True, False):
                    outcome = lib.failure_outcome(category, enforce, soft_fail)
                    self.assertLessEqual(len(outcome.description), 140)

    def test_unknown_category_raises(self):
        with self.assertRaises(KeyError):
            lib.failure_outcome("bogus", enforce=False, soft_fail=True)

    def test_description_names_the_diagnosis_this_run_actually_reached(self):
        # infra_error means Linear could not be queried, so the run determined NOTHING about
        # the ticket; a status reading "no linked Linear issue" would be a diagnosis it never
        # made. Soft-fail promotes this copy to the loudest PR-visible surface.
        for enforce in (True, False):
            for soft_fail in (True, False):
                infra = lib.failure_outcome("infra_error", enforce, soft_fail)
                self.assertNotIn("linked Linear issue", infra.description)
        self.assertIn("linked Linear issue",
                      lib.failure_outcome("no_candidate", True, False).description)


class AdvisoryNote(unittest.TestCase):
    """The note appended to a warn-only red verdict."""

    def test_never_asserts_flatly_that_nothing_is_blocked(self):
        # The validator never reads the caller's ruleset, so it cannot know whether the
        # `linear-ticket` context is required. In the documented footgun configuration
        # (warn-only + context already required) the red check IS blocking the author, and a
        # flat "this does not block the merge" would send them to debug the wrong thing.
        for category in ALL_CATEGORIES:
            note = lib.advisory_note(category)
            self.assertNotIn("This does not block", note)
            self.assertIn("warn-only", note)
            # It must still name what actually blocks, and the misconfiguration to report.
            self.assertIn("ruleset", note)
            self.assertIn("misconfiguration", note)

    def test_infra_error_does_not_tell_the_author_to_link_a_ticket(self):
        # A missing/invalid LINEAR_API_TOKEN is not the PR author's to fix.
        note = lib.advisory_note("infra_error")
        self.assertNotIn("Linking the ticket", note)
        self.assertIn("could not reach Linear", note)

    def test_ticket_categories_do_tell_the_author_to_link(self):
        for category in ("no_candidate", "exists_not_linked", "policy_mismatch"):
            self.assertIn("Linking the ticket", lib.advisory_note(category))

    def test_unknown_category_raises(self):
        with self.assertRaises(KeyError):
            lib.advisory_note("bogus")


class SoftFailEnv(unittest.TestCase):
    """Parsing of the SOFT_FAIL env var — the pin-skew guard."""

    def test_absent_is_the_silent_variant(self):
        # linear-ticket.yml ALWAYS passes SOFT_FAIL, so absent means the workflow YAML predates
        # the input: `workflows_ref` skewed ahead of the caller's `uses:` pin. A caller that
        # cannot express the input must not be silently upgraded from green to red statuses.
        self.assertFalse(lib.soft_fail_enabled(None))
        self.assertFalse(lib.soft_fail_enabled(""))

    def test_explicit_true_opts_in(self):
        self.assertTrue(lib.soft_fail_enabled("true"))
        self.assertTrue(lib.soft_fail_enabled("TRUE"))
        self.assertTrue(lib.soft_fail_enabled(" true "))

    def test_explicit_false_and_anything_unrecognised_stay_silent(self):
        for raw in ("false", "False", "0", "yes", "no", "1", "maybe"):
            self.assertFalse(lib.soft_fail_enabled(raw), raw)


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

    def test_hostile_candidate_skipped(self):
        # A candidate carrying a quote / GraphQL syntax must be dropped, never emitted as an
        # unescaped literal — this pins the ^[A-Z0-9-]+$ injection guard so a refactor can't
        # silently remove it and stay green. (extract_candidates never produces such a value,
        # but build_diagnostic_query defends its input independently.)
        query = lib.build_diagnostic_query(['BE-1", x: y(q: "', "ENG-2"])
        self.assertEqual(query.count("issueSearch"), 1)
        self.assertIn('query: "ENG-2"', query)
        self.assertNotIn("x: y(q:", query)

    def test_all_candidates_malformed_raises(self):
        # Lowercase never survives extract_candidates, but if every input is unusable the
        # builder must raise rather than emit an empty (invalid) query.
        with self.assertRaises(ValueError):
            lib.build_diagnostic_query(["be-1"])


if __name__ == "__main__":
    unittest.main()
