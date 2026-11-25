"""Hermetic orchestration tests for validate.py, scoped to the supersession-guard and
fail-closed-status-publishing cluster (cursor-review findings on validate.py:214/314)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib  # noqa: E402
import validate  # noqa: E402


class FakeGitHub:
    repo = "Comfy-Org/example"

    def __init__(self, *, protected=True, changed_files=None, current_override=None,
                 pending_publish_ok=True, diff=None):
        self.protected = protected
        self._changed_files = changed_files if changed_files is not None else []
        self.current_override = current_override
        self.pending_publish_ok = pending_publish_ok
        self._diff = diff
        self.diff_requests = []
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
        # Entries are mutated in place by the recovery path, so hand out the real dicts
        # inside a fresh list — exactly what the live method does.
        return list(self._changed_files)

    def pull_request_diff(self, pr):
        self.diff_requests.append(pr)
        return self._diff

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
    # Patterns arrive COMPILED in production (main() builds them with lib.parse_patterns),
    # so compile them here too: the pre-existing raw-string list never reached first_match
    # because every test's changed-file list was empty, and a test that does reach it with
    # raw strings dies on `'str' object has no attribute 'search'` rather than on its claim.
    return validate.Validator(github, lib.parse_patterns(r"posthog\.capture"),
                              True, True, "run-url", "cost-exempt")


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


class OmittedPatchRecovery(unittest.TestCase):
    """Regression cluster for the live defect measured on a consumer repo, 2026-11-25.

    `GET .../pulls/{n}/files` omits a file's `patch` past GitHub's per-file render ceiling
    and does not flag the omission, so one large generated file took the whole gate out:
    8 of the 10 red `Telemetry cost contract` statuses across 339 open PRs were
    "could not verify", every one of them a single unreadable generated client or large
    JSON fixture, and all 8 were substantively clean. The whole-PR unified diff carries
    that content — verified up to an 18.3 MB diff whose largest fixture (+289,490 lines)
    has no API patch — so these must now be scanned, not skipped.
    """

    # One readable file, one whose `patch` GitHub omitted. Shape of the live failures.
    DIFF = (
        "diff --git a/svc/handler.go b/svc/handler.go\n"
        "index f3c4..b228 100644\n"
        "--- a/svc/handler.go\n"
        "+++ b/svc/handler.go\n"
        "@@ -1,2 +1,3 @@\n"
        " package svc\n"
        "+// nothing cost-sensitive here\n"
        "diff --git a/svc/ingest.gen.go b/svc/ingest.gen.go\n"
        "index aaaa..bbbb 100644\n"
        "--- a/svc/ingest.gen.go\n"
        "+++ b/svc/ingest.gen.go\n"
        "@@ -4874,2 +4874,3 @@ func (e Body) Valid() bool {\n"
        " \tswitch e {\n"
        "+\tcase \"agent_free_use\":\n"
    )

    def files(self):
        return [
            {"filename": "svc/handler.go", "additions": 1, "status": "modified",
             "patch": "@@ -1,2 +1,3 @@\n package svc\n+// nothing cost-sensitive here\n"},
            # additions > 0 and NO `patch` key — the live omission.
            {"filename": "svc/ingest.gen.go", "additions": 1531, "status": "modified"},
        ]

    def test_omitted_patch_is_recovered_and_the_check_reaches_a_real_verdict(self):
        github = FakeGitHub(changed_files=self.files(), diff=self.DIFF)

        self.assertEqual(validator(github).run(event()), 0)
        self.assertEqual([s[1] for s in github.statuses], ["pending", "success"])
        self.assertEqual(github.diff_requests, [17])
        self.assertEqual(github.upserted_comments, [])

    def test_recovery_still_catches_a_violation_hiding_in_the_omitted_file(self):
        # The load-bearing one: recovery must be a real scan, not a way to go green.
        # The matching line exists ONLY in the file whose patch was omitted.
        diff = self.DIFF.replace('+\tcase "agent_free_use":',
                                 '+\tposthog.capture("agent_turn", nil)')
        github = FakeGitHub(changed_files=self.files(), diff=diff)
        github.pr["body"] = "No cost section here."
        github.current_override = ("abc123", "main", "No cost section here.", frozenset())

        self.assertEqual(validator(github).run(event()), 1)
        self.assertEqual([s[1] for s in github.statuses], ["pending", "failure"])
        self.assertIn("ingest.gen.go", github.upserted_comments[0][1])

    def test_unreadable_diff_reports_error_not_failure(self):
        # GitHub answers 406 on a diff too large even for this endpoint. That is still a
        # cannot-verify, and it must not wear the same state as a real violation.
        github = FakeGitHub(changed_files=self.files(), diff=None)

        self.assertEqual(validator(github).run(event()), 1)
        self.assertEqual([s[1] for s in github.statuses], ["pending", "error"])
        self.assertNotIn("failure", [s[1] for s in github.statuses])
        self.assertIn("could not verify", github.statuses[-1][2].lower())

    def test_binary_file_still_fails_closed_after_recovery(self):
        # A genuine binary has no hunk in the unified diff either, so it must remain
        # unreadable rather than be passed as recovered-and-clean.
        files = self.files() + [
            {"filename": "assets/logo.png", "additions": 4, "status": "added"}]
        diff = self.DIFF + ("diff --git a/assets/logo.png b/assets/logo.png\n"
                            "index 0000..aaaa 100644\n"
                            "Binary files /dev/null and b/assets/logo.png differ\n")
        github = FakeGitHub(changed_files=files, diff=diff)

        self.assertEqual(validator(github).run(event()), 1)
        self.assertEqual([s[1] for s in github.statuses], ["pending", "error"])
        self.assertIn("logo.png", github.upserted_comments[0][1])
        # ...and the recovered file is NOT named as unreadable any more.
        self.assertNotIn("ingest.gen.go", github.upserted_comments[0][1])

    def test_no_diff_request_when_every_patch_is_present(self):
        # The fallback costs an extra API call on a potentially huge diff; it must only
        # fire when something is actually missing.
        github = FakeGitHub(changed_files=self.files()[:1], diff=self.DIFF)

        self.assertEqual(validator(github).run(event()), 0)
        self.assertEqual(github.diff_requests, [])


if __name__ == "__main__":
    unittest.main()
