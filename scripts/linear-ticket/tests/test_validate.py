"""Hermetic orchestration tests for validate.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import validate  # noqa: E402


class FakeGitHub:
    repo = "Comfy-Org/example"

    def __init__(self, protected):
        self.protected = protected
        self.current_base = "release/next"
        self.statuses = []
        self.deleted_comments = []

    def get(self, path, *, paginate=False):
        if path.endswith("/pulls/17"):
            return {
                "number": 17,
                "state": "open",
                "html_url": "https://github.com/Comfy-Org/example/pull/17",
                "head": {"sha": "abc123", "ref": "feature/be-123"},
                "base": {"ref": "release/next"},
                "title": "Change something",
                "body": "",
                "labels": [],
                "user": {"login": "octocat"},
            }
        raise AssertionError(f"unexpected GitHub request: {path}")

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

    def test_unprotected_base_skips_linear_and_publishes_success(self):
        github = FakeGitHub(protected=False)
        validator = self.validator(github)
        validator._query_attachments = lambda _url: self.fail("Linear must not be queried")

        self.assertEqual(validator.run(event()), 0)

        self.assertEqual(github.requested_branch, "release/next")
        self.assertEqual([status[1] for status in github.statuses], ["pending", "success"])
        self.assertIn("unprotected branch", github.statuses[-1][2])
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
        self.assertEqual([status[1] for status in github.statuses], ["pending"])

    def test_retargeted_pr_does_not_publish_stale_terminal_status(self):
        github = FakeGitHub(protected=False)
        github.current_base = "main"
        validator = self.validator(github)
        validator._query_attachments = lambda _url: self.fail("Linear must not be queried")

        self.assertEqual(validator.run(event()), 0)
        self.assertEqual([status[1] for status in github.statuses], ["pending"])


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
