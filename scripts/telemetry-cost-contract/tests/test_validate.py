"""Hermetic orchestration tests for validate.py, scoped to the supersession-guard and
fail-closed-status-publishing cluster (cursor-review findings on validate.py:214/314)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import validate  # noqa: E402


class FakeGitHub:
    repo = "Comfy-Org/example"

    def __init__(self, *, protected=True, changed_files=None, current_override=None,
                 pending_publish_ok=True):
        self.protected = protected
        self._changed_files = changed_files if changed_files is not None else []
        self.current_override = current_override
        self.pending_publish_ok = pending_publish_ok
        self.statuses = []
        self.deleted_comments = []
        self.upserted_comments = []
        self.pr = {
            "number": 17,
            "state": "open",
            "head": {"sha": "abc123"},
            "base": {"ref": "main"},
            "body": "## Cost impact\nnone",
            "labels": [],
            "changed_files": 0,
        }

    def get(self, path, *, paginate=False):
        if path.endswith("/pulls/17"):
            return dict(self.pr)
        raise AssertionError(f"unexpected GitHub request: {path}")

    def branch_is_protected(self, branch):
        return self.protected

    def publish_status(self, sha, state, description, target_url, *, critical=False):
        self.statuses.append((sha, state, description, critical))
        if state == "pending":
            return self.pending_publish_ok
        return True

    def changed_files(self, pr, declared_count=None):
        return list(self._changed_files)

    def upsert_marker_comment(self, pr, body):
        self.upserted_comments.append((pr, body))

    def delete_marker_comment(self, pr):
        self.deleted_comments.append(pr)

    def current_pr_target(self, pr):
        if self.current_override is not None:
            return self.current_override
        return (self.pr["head"]["sha"], self.pr["base"]["ref"], self.pr["body"],
                frozenset(lbl["name"] for lbl in self.pr["labels"]))


def event():
    return {
        "workflow_run": {
            "event": "pull_request",
            "head_sha": "abc123",
            "pull_requests": [{"number": 17}],
        }
    }


def validator(github):
    return validate.Validator(github, [r"posthog\.capture"], True, True, "run-url", "cost-exempt")


class SupersessionGuard(unittest.TestCase):
    def test_passes_through_when_pr_is_unchanged(self):
        github = FakeGitHub()
        self.assertEqual(validator(github).run(event()), 0)
        self.assertEqual([s[1] for s in github.statuses], ["pending", "success"])
        self.assertEqual(github.deleted_comments, [17])

    def test_fails_closed_without_publishing_when_refetch_fails(self):
        github = FakeGitHub(current_override=None)

        # current_override=None is indistinguishable from "not set" on the fake, so force it
        # by monkeypatching the method directly for this one test.
        github.current_pr_target = lambda pr: None

        self.assertEqual(validator(github).run(event()), 0)
        self.assertEqual([s[1] for s in github.statuses], ["pending"])
        self.assertEqual(github.deleted_comments, [])

    def test_fails_closed_when_body_changed_since_validation(self):
        github = FakeGitHub()
        github.current_override = ("abc123", "main", "## Cost impact\nCHANGED", frozenset())

        self.assertEqual(validator(github).run(event()), 0)
        self.assertEqual([s[1] for s in github.statuses], ["pending"])
        self.assertEqual(github.deleted_comments, [])

    def test_fails_closed_when_labels_changed_since_validation(self):
        github = FakeGitHub()
        github.current_override = ("abc123", "main", github.pr["body"], frozenset({"cost-exempt"}))

        self.assertEqual(validator(github).run(event()), 0)
        self.assertEqual([s[1] for s in github.statuses], ["pending"])
        self.assertEqual(github.deleted_comments, [])

    def test_marker_comment_is_not_deleted_before_the_guard_runs(self):
        # Regression for the ordering bug: delete_marker_comment used to run before
        # _guard_supersession, so a superseded pass could erase a newer run's failure comment.
        github = FakeGitHub()
        github.current_override = ("def456", "main", github.pr["body"], frozenset())

        self.assertEqual(validator(github).run(event()), 0)
        self.assertEqual(github.deleted_comments, [])


class FailClosedStatusPublishing(unittest.TestCase):
    def test_unreadable_branch_protection_publishes_failure_on_head_sha(self):
        github = FakeGitHub(protected=None)

        self.assertEqual(validator(github).run(event()), 1)
        self.assertEqual(github.statuses, [("abc123", "failure",
                                            github.statuses[0][2], True)])

    def test_pr_fetch_failure_publishes_failure_on_head_sha(self):
        github = FakeGitHub()
        github.pr = None  # type: ignore[assignment]

        def get(path, *, paginate=False):
            return None

        github.get = get

        self.assertEqual(validator(github).run(event()), 1)
        self.assertEqual([s[1] for s in github.statuses], ["failure"])
        self.assertEqual(github.statuses[0][0], "abc123")

    def test_missing_base_branch_publishes_failure_on_head_sha(self):
        github = FakeGitHub()
        github.pr["base"] = {"ref": ""}

        self.assertEqual(validator(github).run(event()), 1)
        self.assertEqual([s[1] for s in github.statuses], ["failure"])

    def test_non_pull_request_workflow_run_publishes_failure_before_any_pr_lookup(self):
        github = FakeGitHub()

        def get(path, *, paginate=False):
            raise AssertionError("must not look up the PR before checking wr_event")

        github.get = get
        bad_event = event()
        bad_event["workflow_run"]["event"] = "push"

        self.assertEqual(validator(github).run(bad_event), 1)
        self.assertEqual(github.statuses, [("abc123", "failure", github.statuses[0][2], True)])


class PendingStatusWriteFailure(unittest.TestCase):
    def test_failed_pending_write_is_treated_as_critical_and_short_circuits(self):
        github = FakeGitHub(pending_publish_ok=False)

        def changed_files(pr, declared_count=None):
            raise AssertionError("must not scan the diff once the pending write fails")

        github.changed_files = changed_files

        self.assertEqual(validator(github).run(event()), 1)
        self.assertEqual([s[1] for s in github.statuses], ["pending"])
        self.assertTrue(github.statuses[0][3], "pending write must be published as critical")


if __name__ == "__main__":
    unittest.main()
