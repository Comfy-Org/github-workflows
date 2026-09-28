"""Hermetic orchestration tests for validate.py."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import validate  # noqa: E402


class FakeGitHub:
    repo = "Comfy-Org/example"

    def __init__(self, protected, changed_files=None, changed_files_error=None,
                 declared_changed_files=None, head_ref="feature/be-123"):
        self.protected = protected
        self.head_ref = head_ref
        self.current_base = "release/next"
        self.statuses = []
        self.deleted_comments = []
        self.comments = []
        self._changed_files = changed_files
        self._changed_files_error = changed_files_error
        self.declared_changed_files = declared_changed_files
        self.changed_files_calls = []

    def get(self, path, *, paginate=False):
        if path.endswith("/pulls/17"):
            pr = {
                "number": 17,
                "state": "open",
                "html_url": "https://github.com/Comfy-Org/example/pull/17",
                "head": {"sha": "abc123", "ref": self.head_ref},
                "base": {"ref": "release/next"},
                "title": "Change something",
                "body": "",
                "labels": [],
                "user": {"login": "octocat"},
            }
            if self.declared_changed_files is not None:
                pr["changed_files"] = self.declared_changed_files
            return pr
        raise AssertionError(f"unexpected GitHub request: {path}")

    def changed_files(self, pr, declared_count=None):
        self.changed_files_calls.append((pr, declared_count))
        if self._changed_files_error is not None:
            raise validate.ChangedFilesUnavailable(self._changed_files_error)
        if self._changed_files is None:
            raise AssertionError("changed_files must not be consulted")
        return list(self._changed_files)

    def upsert_marker_comment(self, pr, body):
        self.comments.append((pr, body))

    def branch_is_protected(self, branch):
        self.requested_branch = branch
        return self.protected

    def publish_status(self, sha, state, description, target_url, *, critical=False):
        self.statuses.append((sha, state, description, critical))
        return True

    def delete_marker_comment(self, pr):
        self.deleted_comments.append(pr)

    def current_pr_target(self, pr):
        return "abc123", self.current_base


def event():
    return {
        "workflow_run": {
            "event": "pull_request",
            "head_sha": "abc123",
            "pull_requests": [{"number": 17}],
        }
    }


class ProtectedBaseBranch(unittest.TestCase):
    def validator(self, github):
        return validate.Validator(github, "token", [], True, True, False, "run-url")

    def test_unprotected_base_skips_linear_without_publishing_status(self):
        github = FakeGitHub(protected=False)
        validator = self.validator(github)
        validator._query_attachments = lambda _url: self.fail("Linear must not be queried")

        self.assertEqual(validator.run(event()), 0)

        self.assertEqual(github.requested_branch, "release/next")
        self.assertEqual(github.statuses, [])
        self.assertEqual(github.deleted_comments, [17])

    def test_protected_base_runs_linear_validation(self):
        github = FakeGitHub(protected=True)
        validator = self.validator(github)
        queries = []
        validator._query_attachments = lambda url: (
            queries.append(url) or
            ([{"issue": {"identifier": "BE-123", "team": {"key": "BE"},
                          "state": {"type": "started"}}}], False)
        )

        self.assertEqual(validator.run(event()), 0)

        self.assertEqual(queries, ["https://github.com/Comfy-Org/example/pull/17"])
        self.assertEqual([status[1] for status in github.statuses], ["pending", "success"])
        self.assertIn("BE-123", github.statuses[-1][2])

    def test_unknown_protection_state_fails_closed_without_querying_linear(self):
        github = FakeGitHub(protected=None)
        validator = self.validator(github)
        validator._query_attachments = lambda _url: self.fail("Linear must not be queried")

        self.assertEqual(validator.run(event()), 1)
        self.assertEqual(github.statuses, [])

    def test_retargeted_pr_does_not_publish_stale_terminal_status(self):
        github = FakeGitHub(protected=False)
        github.current_base = "main"
        validator = self.validator(github)
        validator._query_attachments = lambda _url: self.fail("Linear must not be queried")

        self.assertEqual(validator.run(event()), 0)
        self.assertEqual(github.statuses, [])
        self.assertEqual(github.deleted_comments, [])


class PathExemption(unittest.TestCase):
    """The `exempt-paths` short-circuit, which runs after the label/actor hatches and BEFORE
    the Linear query — so an exempt PR spends no Linear budget and survives a Linear outage."""

    PATTERNS = ["infrastructure/dynamicconfig/**"]

    def setUp(self):
        # The suite is hermetic (test-linear-ticket.yml says so). The diagnostic lookup in
        # _diagnose_and_fail is the one path that reaches Linear without going through
        # _query_attachments, so pin it shut rather than relying on every case stubbing it.
        patcher = mock.patch.object(
            validate, "linear_post",
            side_effect=AssertionError("no test in this suite may reach the network"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def validator(self, github, exempt_paths):
        return validate.Validator(github, "token", [], True, True, False, "run-url",
                                  exempt_paths)

    def never_query_linear(self, validator):
        validator._query_attachments = lambda _url: self.fail("Linear must not be queried")

    def test_all_changed_files_matching_is_exempt_and_green(self):
        github = FakeGitHub(protected=True, changed_files=[
            "infrastructure/dynamicconfig/prod.yaml",
            "infrastructure/dynamicconfig/staging.yaml",
        ])
        validator = self.validator(github, self.PATTERNS)
        self.never_query_linear(validator)

        self.assertEqual(validator.run(event()), 0)

        # Green via the shared finish_exempt path: pending, then a SUCCESS status naming the
        # reason. It must publish, not skip — a required-but-unpublished context never merges.
        self.assertEqual([status[1] for status in github.statuses], ["pending", "success"])
        self.assertIn("exempt-paths", github.statuses[-1][2])
        self.assertTrue(github.statuses[-1][3], "the terminal write must be critical")
        self.assertEqual(github.deleted_comments, [17])
        self.assertEqual(github.comments, [])

    def test_one_unmatched_file_is_not_exempt_and_falls_through_to_linear(self):
        github = FakeGitHub(protected=True, changed_files=[
            "infrastructure/dynamicconfig/prod.yaml",
            "services/api/handler.go",
        ])
        validator = self.validator(github, self.PATTERNS)
        queried = []
        validator._query_attachments = lambda url: (
            queried.append(url) or
            ([{"issue": {"identifier": "BE-1", "team": {"key": "BE"},
                         "state": {"type": "started"}}}], False)
        )

        self.assertEqual(validator.run(event()), 0)

        self.assertEqual(len(queried), 1)
        self.assertIn("BE-1", github.statuses[-1][2])

    def test_zero_changed_files_is_not_exempt(self):
        # An empty or fully-reverted branch: "all files match" must not be vacuously true.
        # A branch with no identifier in it, so the red path needs no diagnostic lookup.
        github = FakeGitHub(protected=True, changed_files=[], head_ref="revert-everything")
        validator = self.validator(github, self.PATTERNS)
        validator._query_attachments = lambda _url: ([], False)

        self.assertEqual(validator.run(event()), 1)   # enforce=True, no linked issue

        self.assertEqual(github.statuses[-1][1], "failure")
        self.assertNotIn("exempt", github.statuses[-1][2].lower())

    def test_empty_input_never_looks_up_changed_files(self):
        # The default. Every existing caller must behave byte-identically — including making
        # no extra GitHub API call.
        github = FakeGitHub(protected=True)   # changed_files=None -> asserts if consulted
        validator = self.validator(github, [])
        validator._query_attachments = lambda _url: (
            [{"issue": {"identifier": "BE-1", "team": {"key": "BE"},
                        "state": {"type": "started"}}}], False)

        self.assertEqual(validator.run(event()), 0)
        self.assertEqual(github.changed_files_calls, [])

    def test_unreadable_changed_file_list_fails_closed_without_querying_linear(self):
        github = FakeGitHub(protected=True,
                            changed_files_error="the PR reports 4000 changed files, at or "
                                                "past GitHub's 3000-file ceiling")
        validator = self.validator(github, self.PATTERNS)
        self.never_query_linear(validator)

        self.assertEqual(validator.run(event()), 1)

        self.assertEqual(github.statuses[-1][1], "failure")
        self.assertIn("changed-file list", github.statuses[-1][2])
        self.assertNotIn("exempt", github.statuses[-1][2].lower())
        # The marker comment must name the category so the red check is triageable.
        self.assertEqual(len(github.comments), 1)
        self.assertIn("changed_files_unavailable", github.comments[0][1])
        self.assertIn("3000-file ceiling", github.comments[0][1])

    def test_declared_count_is_handed_to_the_lookup(self):
        github = FakeGitHub(protected=True, changed_files=["infrastructure/dynamicconfig/a"],
                            declared_changed_files=1)
        validator = self.validator(github, self.PATTERNS)
        self.never_query_linear(validator)

        self.assertEqual(validator.run(event()), 0)
        self.assertEqual(github.changed_files_calls, [(17, 1)])


class ChangedFilesLookup(unittest.TestCase):
    """validate.GitHub.changed_files — pagination and the 3000-file ceiling."""

    def github(self, payload):
        gh = validate.GitHub("Comfy-Org/example")
        gh.requests = []
        gh.get = lambda path, *, paginate=False: (
            gh.requests.append((path, paginate)) or payload)
        return gh

    def test_paginates_past_one_page(self):
        # gh api --paginate merges every page's top-level array into one list, so the unit
        # under test is what changed_files does with a merged list longer than a single page.
        payload = [{"filename": f"infrastructure/dynamicconfig/f{i}.yaml"} for i in range(250)]
        gh = self.github(payload)

        paths = gh.changed_files(17)

        self.assertEqual(len(paths), 250)
        self.assertEqual(paths[-1], "infrastructure/dynamicconfig/f249.yaml")
        self.assertEqual(gh.requests,
                         [("/repos/Comfy-Org/example/pulls/17/files?per_page=100", True)])

    def test_rename_contributes_both_paths(self):
        # Renaming a Go file INTO an exempt directory must not buy the exemption.
        gh = self.github([{"filename": "infrastructure/dynamicconfig/handler.go",
                           "previous_filename": "services/api/handler.go"}])

        self.assertEqual(gh.changed_files(17),
                         ["infrastructure/dynamicconfig/handler.go",
                          "services/api/handler.go"])

    def test_declared_count_past_the_cap_raises_before_any_request(self):
        gh = self.github([])
        with self.assertRaises(validate.ChangedFilesUnavailable) as caught:
            gh.changed_files(17, declared_count=validate.CHANGED_FILES_CAP + 1)
        self.assertIn("3000", str(caught.exception))
        self.assertEqual(gh.requests, [], "no request should be spent on a known-over-cap PR")

    def test_declared_count_exactly_at_the_cap_raises(self):
        # At the cap the API returns everything it will ever return, and cannot say whether
        # that is the whole truth — indistinguishable from a truncated read.
        gh = self.github([])
        with self.assertRaises(validate.ChangedFilesUnavailable):
            gh.changed_files(17, declared_count=validate.CHANGED_FILES_CAP)

    def test_returned_list_at_the_cap_raises_even_without_a_declared_count(self):
        # The belt to the declared count's braces: a PR payload missing `changed_files` must
        # not slip a silently truncated list past the guard.
        payload = [{"filename": f"c/f{i}.yaml"} for i in range(validate.CHANGED_FILES_CAP)]
        gh = self.github(payload)
        with self.assertRaises(validate.ChangedFilesUnavailable):
            gh.changed_files(17)

    def test_malformed_declared_count_degrades_to_the_list_guard(self):
        # A non-int `changed_files` must not raise TypeError out of a privileged job; the
        # returned-list guard still covers the truncation case.
        gh = self.github([{"filename": "c/a.yaml"}])
        self.assertEqual(gh.changed_files(17, declared_count="lots"), ["c/a.yaml"])

    def test_failed_request_raises_rather_than_returning_empty(self):
        # get() returns None on failure; an empty list would read as "zero changed files".
        gh = self.github(None)
        with self.assertRaises(validate.ChangedFilesUnavailable):
            gh.changed_files(17)

    def test_entry_without_a_filename_raises(self):
        gh = self.github([{"filename": "c/a.yaml"}, {"status": "modified"}])
        with self.assertRaises(validate.ChangedFilesUnavailable):
            gh.changed_files(17)

    def test_short_list_against_the_declared_count_raises(self):
        # THE dangerous direction: the dropped entry is exactly the non-exempt path, so every
        # survivor matches and a partial read would publish a waiver.
        gh = self.github([{"filename": "c/a.yaml"}, {"filename": "c/b.yaml"}])
        with self.assertRaises(validate.ChangedFilesUnavailable):
            gh.changed_files(17, declared_count=3)

    def test_surplus_list_against_the_declared_count_raises(self):
        gh = self.github([{"filename": "c/a.yaml"}, {"filename": "c/b.yaml"}])
        with self.assertRaises(validate.ChangedFilesUnavailable):
            gh.changed_files(17, declared_count=1)

    def test_matching_declared_count_is_accepted(self):
        # A rename yields two paths from one entry, so the count is compared against ENTRIES,
        # never against the expanded path list.
        gh = self.github([{"filename": "c/a.yaml", "previous_filename": "old/a.yaml"}])
        self.assertEqual(gh.changed_files(17, declared_count=1), ["c/a.yaml", "old/a.yaml"])

    def test_non_object_entry_raises_rather_than_attributeerror(self):
        gh = self.github([{"filename": "c/a.yaml"}, "c/b.yaml"])
        with self.assertRaises(validate.ChangedFilesUnavailable):
            gh.changed_files(17)

    def test_non_string_filename_raises_before_it_reaches_the_matcher(self):
        # lib.path_matches_any calls re.fullmatch(), which raises TypeError on a non-string.
        gh = self.github([{"filename": 17}])
        with self.assertRaises(validate.ChangedFilesUnavailable):
            gh.changed_files(17)

    def test_non_string_previous_filename_raises(self):
        gh = self.github([{"filename": "c/a.yaml", "previous_filename": ["old/a.yaml"]}])
        with self.assertRaises(validate.ChangedFilesUnavailable):
            gh.changed_files(17)


class MalformedExemptPathsInput(unittest.TestCase):
    """A malformed `exempt-paths` fails the RUN, the way a malformed `team-keys` does."""

    def test_main_rejects_a_malformed_list_before_touching_the_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_path = os.path.join(tmp, "event.json")
            # DELIBERATELY unparseable: this test's claim is that `exempt-paths` is validated
            # BEFORE the event is read, and a well-formed fixture cannot tell the two orders
            # apart. If main() ever parses the event first, json.load raises here and the test
            # fails loudly instead of passing on a coincidence.
            with open(event_path, "w", encoding="utf-8") as handle:
                handle.write("{ this is not json")
            env = {
                "GH_REPO": "Comfy-Org/example",
                "GITHUB_EVENT_PATH": event_path,
                "LINEAR_API_TOKEN": "token",
                "EXEMPT_PATHS": "infrastructure/dynamicconfig/**,,docs/**",
            }
            original = {key: os.environ.get(key) for key in env}
            os.environ.update(env)
            try:
                self.assertEqual(validate.main(), 1)
            finally:
                for key, value in original.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class BranchProtectionLookup(unittest.TestCase):
    def test_encodes_branch_names_and_returns_boolean(self):
        github = validate.GitHub("Comfy-Org/example")
        paths = []
        github.get = lambda path: paths.append(path) or {"protected": True}

        self.assertTrue(github.branch_is_protected("release/next"))
        self.assertEqual(paths, ["/repos/Comfy-Org/example/branches/release%2Fnext"])

    def test_missing_protected_field_is_unknown(self):
        github = validate.GitHub("Comfy-Org/example")
        github.get = lambda _path: {}

        self.assertIsNone(github.branch_is_protected("main"))


if __name__ == "__main__":
    unittest.main()
