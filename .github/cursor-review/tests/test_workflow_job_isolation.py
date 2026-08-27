#!/usr/bin/env python3
"""Structural regression tests for cursor-review.yml's credential isolation.

The review panel and the judge run `cursor-agent --print --trust` over a PR
checkout with no `--sandbox`, and print mode can use write and shell tools. So a
job that checks out PR-authored code and runs an agent over it must hold NOTHING
write-scoped: a prompt-injected model in such a job could rewrite the assets
checkout or a downloaded action before a later step in the SAME job mints the
bot token and posts. The review is posted from a separate `post-review` job that
checks out no PR code and receives its payload as an artifact — the same
two-job split `pr-size.yml` uses for its `comment` job.

That property is invisible in a diff (nothing fails when a `pull-requests:
write` creeps back onto `consolidate`, or when a `Checkout PR repo` step is
added to `post-review`), so it is pinned here instead.

Deliberately parsed WITHOUT PyYAML: this repo is stdlib-only and CI installs no
requirements, so a yaml import would simply not run. The workflow is uniformly
2-space indented, which is all the block splitter below needs.

Run: python3 .github/cursor-review/tests/test_workflow_job_isolation.py
"""

import os
import re
import unittest

WORKFLOW = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml"
    )
)

JOB_HEADER = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
STEP_HEADER = re.compile(r"^      - ")
CHECKOUT = re.compile(r"^\s+uses:\s+actions/checkout@")
REPOSITORY = re.compile(r"^\s+repository:\s*\S")
COMMENT = re.compile(r"^\s*#")


def read_workflow():
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read().split("\n")


def split_jobs(lines):
    """{job name: [lines]} for the top-level jobs: mapping."""
    try:
        start = lines.index("jobs:") + 1
    except ValueError:  # pragma: no cover - the file always has one
        raise AssertionError("cursor-review.yml has no top-level `jobs:` key")

    jobs, name, body = {}, None, []
    for line in lines[start:]:
        match = JOB_HEADER.match(line)
        if match:
            if name:
                jobs[name] = body
            name, body = match.group(1), []
            continue
        if name is not None:
            body.append(line)
    if name:
        jobs[name] = body
    return jobs


def split_steps(job_lines):
    """[[lines]] for each `- name:` step in a job body."""
    steps, current = [], None
    for line in job_lines:
        if STEP_HEADER.match(line):
            if current is not None:
                steps.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        steps.append(current)
    return steps


def code_lines(block):
    """Lines with comments dropped, so a comment mentioning a key never counts."""
    return [line for line in block if not COMMENT.match(line)]


def checks_out_pr_repo(job_lines):
    """True when any step runs actions/checkout with no explicit `repository:`.

    An actions/checkout with no `repository:` defaults to the CALLER's repo —
    i.e. the PR under review. Steps that name a repository (the pinned
    `Comfy-Org/github-workflows` assets checkout) are not PR code.
    """
    for step in split_steps(job_lines):
        body = code_lines(step)
        if any(CHECKOUT.match(line) for line in body) and not any(
            REPOSITORY.match(line) for line in body
        ):
            return True
    return False


def references_bot_key(job_lines):
    return any("BOT_APP_PRIVATE_KEY" in line for line in code_lines(job_lines))


def grants_pull_request_write(job_lines):
    return any(
        line.strip() == "pull-requests: write" for line in code_lines(job_lines)
    )


class WorkflowJobIsolationTest(unittest.TestCase):
    def setUp(self):
        self.jobs = split_jobs(read_workflow())
        # Guard the parser itself: if the splitter silently stopped matching,
        # every assertion below would pass vacuously.
        names = sorted(self.jobs)
        for expected in ("gate", "review", "consolidate", "post-review"):
            # Compare against the NAME list, never the job bodies — an
            # assertIn against the dict renders every line of the workflow.
            self.assertIn(expected, names, f"job splitter lost `{expected}`")

    def test_no_job_both_checks_out_pr_code_and_holds_the_bot_key(self):
        for name, body in self.jobs.items():
            if checks_out_pr_repo(body):
                self.assertFalse(
                    references_bot_key(body),
                    f"job `{name}` checks out the PR repo AND references "
                    "secrets.BOT_APP_PRIVATE_KEY",
                )

    def test_no_job_both_checks_out_pr_code_and_can_write_to_the_pr(self):
        for name, body in self.jobs.items():
            if checks_out_pr_repo(body):
                self.assertFalse(
                    grants_pull_request_write(body),
                    f"job `{name}` checks out the PR repo AND grants "
                    "pull-requests: write",
                )

    def test_the_bot_key_is_confined_to_the_checkout_free_posting_jobs(self):
        holders = {
            name for name, body in self.jobs.items() if references_bot_key(body)
        }
        self.assertEqual(holders, {"over-cap-comment", "post-review"})

    def test_consolidate_runs_the_judge_over_a_pr_checkout(self):
        # The premise of the whole split. If the judge ever stops running here
        # the tests above go quiet for the wrong reason.
        body = self.jobs["consolidate"]
        self.assertTrue(checks_out_pr_repo(body))
        self.assertTrue(any("cursor-agent" in line for line in body))

    def test_post_review_checks_out_no_pr_code(self):
        body = self.jobs["post-review"]
        self.assertFalse(checks_out_pr_repo(body))
        self.assertTrue(grants_pull_request_write(body))
        self.assertTrue(references_bot_key(body))
        # It is the job that actually posts.
        self.assertTrue(any("post-review.py" in line for line in body))

    def test_only_post_review_runs_post_review_py(self):
        posters = {
            name
            for name, body in self.jobs.items()
            if any("post-review.py" in line for line in code_lines(body))
        }
        self.assertEqual(posters, {"post-review"})

    def test_payload_is_handed_over_as_an_artifact(self):
        consolidate = "\n".join(self.jobs["consolidate"])
        post = "\n".join(self.jobs["post-review"])
        self.assertIn("name: cursor-review-consolidated", consolidate)
        self.assertIn("actions/upload-artifact@", consolidate)
        self.assertIn("name: cursor-review-consolidated", post)
        self.assertIn("actions/download-artifact@", post)


if __name__ == "__main__":
    unittest.main(verbosity=2)
