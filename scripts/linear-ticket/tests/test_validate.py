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
        self.pr_state = "open"
        self.pr_merged = False
        self.pr_head = "abc123"
        self.association_paginated = None
        self.fail_pull_numbers = set()
        self.statuses = []
        self.deleted_comments = []

    def get(self, path, *, paginate=False):
        if path.endswith("/commits/abc123/pulls"):
            self.association_paginated = paginate
            return [{
                "number": 17,
                "state": self.pr_state,
                "head": {"sha": self.pr_head},
                "base": {"repo": {"full_name": self.repo}},
            }]
        if path.endswith("/pulls/17"):
            if 17 in self.fail_pull_numbers:
                return None
            return {
                "number": 17,
                "state": self.pr_state,
                "merged": self.pr_merged,
                "html_url": "https://github.com/Comfy-Org/example/pull/17",
                "head": {"sha": self.pr_head, "ref": "feature/be-123"},
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

    def test_signal_that_finishes_after_pr_merge_is_a_noop(self):
        github = FakeGitHub(protected=True)
        github.pr_state = "closed"
        github.pr_merged = True
        validator = self.validator(github)
        validator._query_attachments = lambda _url: self.fail("Linear must not be queried")
        stale_event = event()
        stale_event["workflow_run"]["pull_requests"] = []

        self.assertEqual(validator.run(stale_event), 0)
        self.assertEqual(github.statuses, [])
        self.assertEqual(github.deleted_comments, [])

    def test_closed_unmerged_pr_fails_closed(self):
        github = FakeGitHub(protected=True)
        github.pr_state = "closed"
        validator = self.validator(github)
        stale_event = event()
        stale_event["workflow_run"]["pull_requests"] = []

        self.assertEqual(validator.run(stale_event), 1)
        self.assertEqual(github.statuses, [])

    def test_merged_pr_with_a_newer_head_is_a_noop(self):
        github = FakeGitHub(protected=True)
        github.pr_state = "closed"
        github.pr_merged = True
        github.pr_head = "newer-head"
        validator = self.validator(github)
        validator._query_attachments = lambda _url: self.fail("Linear must not be queried")
        stale_event = event()
        stale_event["workflow_run"]["pull_requests"] = []

        self.assertEqual(validator.run(stale_event), 0)
        self.assertEqual(github.statuses, [])

    def test_pr_fetch_failure_does_not_become_a_completed_noop(self):
        github = FakeGitHub(protected=True)
        github.fail_pull_numbers.add(17)
        validator = self.validator(github)
        stale_event = event()
        stale_event["workflow_run"]["pull_requests"] = []

        self.assertEqual(validator.run(stale_event), 1)
        self.assertEqual(github.statuses, [])

    def test_commit_associations_are_paginated(self):
        github = FakeGitHub(protected=True)
        validator = self.validator(github)
        stale_event = event()
        stale_event["workflow_run"]["pull_requests"] = []

        self.assertEqual(validator._resolve_pr(stale_event, "abc123"), (17, False))
        self.assertTrue(github.association_paginated)

    def test_retargeted_pr_does_not_publish_stale_terminal_status(self):
        github = FakeGitHub(protected=False)
        github.current_base = "main"
        validator = self.validator(github)
        validator._query_attachments = lambda _url: self.fail("Linear must not be queried")

        self.assertEqual(validator.run(event()), 0)
        self.assertEqual(github.statuses, [])
        self.assertEqual(github.deleted_comments, [])


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
