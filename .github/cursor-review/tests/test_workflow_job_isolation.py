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
REPOSITORY = re.compile(r"^\s+repository:\s*(\S.*)$")
COMMENT = re.compile(r"^\s*#")
RUN_KEY = re.compile(r"^(\s+)(run|script):")

# The one repo a checkout may name and still not be PR code: this repo's own
# pinned assets. Anything else resolves back to the PR under review.
ASSETS_REPO = "Comfy-Org/github-workflows"

# Values of a `permissions:` scope that grant nothing writable. `{}` is the
# explicit empty block; `read-all` is the whole-workflow read shorthand.
READ_ONLY_VALUES = frozenset({"read", "none", "read-all", "{}"})


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


def scalar(value):
    """A YAML scalar with its trailing comment and quotes removed.

    `code_lines` drops only WHOLE-line comments, and a trailing `# why` on the
    same line is this repo's house style — so `pull-requests: write  # reason`
    must not read as a different value from `pull-requests: write`, and neither
    must `pull-requests: 'write'`.
    """
    value = re.sub(r"\s+#.*$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def outside_run_blocks(step_lines):
    """Step lines with any `run:` / `script:` literal block stripped out.

    A `repository:`-shaped line inside a step's own shell body is shell text,
    not a checkout input, and must never exempt the step from the PR-code
    classification below.
    """
    kept, block_indent = [], None
    for line in step_lines:
        if block_indent is not None:
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) > block_indent:
                continue  # still inside the literal block
            block_indent = None
        match = RUN_KEY.match(line)
        if match:
            block_indent = len(match.group(1))
            continue
        kept.append(line)
    return kept


def checks_out_pr_repo(job_lines):
    """True when any actions/checkout step in the job checks out PR code.

    A checkout counts as NOT-PR-code only when it explicitly names the pinned
    assets repo. The mere PRESENCE of a `repository:` key is not equivalent:
    `repository: ${{ github.repository }}`,
    `repository: ${{ github.event.pull_request.head.repo.full_name }}` and
    `repository: ''` all resolve back to the repo under review, and a
    `repository:`-shaped line inside the step's own `run:` block would satisfy a
    key-presence test as well — at which point the isolation assertions below
    pass VACUOUSLY for a job that checks out PR code and holds the bot key.

    Matching the value fails SAFE in both directions: an unrecognised value is
    treated as PR code, which makes the assertions stricter, never quieter.
    """
    for step in split_steps(job_lines):
        body = outside_run_blocks(code_lines(step))
        if not any(CHECKOUT.match(line) for line in body):
            continue
        names_assets_repo = False
        for line in body:
            match = REPOSITORY.match(line)
            if match and scalar(match.group(1)) == ASSETS_REPO:
                names_assets_repo = True
                break
        if not names_assets_repo:
            return True
    return False


def references_bot_key(job_lines):
    return any("BOT_APP_PRIVATE_KEY" in line for line in code_lines(job_lines))


def parse_permissions(lines, indent):
    """The `permissions:` block at `indent` spaces as {scope: value}, or None.

    None means the key is ABSENT, which is emphatically not "no permissions": a
    `workflow_call` reusable with no `permissions:` block INHERITS the calling
    job's, and the documented caller grants `pull-requests: write`. Deleting the
    block from `consolidate` would therefore restore write scope silently, so
    absence is reported as its own distinct condition rather than folded in.

    The inline forms (`permissions: write-all`, `read-all`, `{}`) come back
    under the `*` key, since they set every scope at once.
    """
    pad = " " * indent
    key = re.compile(r"^%s(permissions):(.*)$" % re.escape(pad))
    scope = re.compile(r"^%s  ([A-Za-z-]+):(.*)$" % re.escape(pad))
    body = code_lines(lines)
    for i, line in enumerate(body):
        match = key.match(line)
        if not match:
            continue
        inline = scalar(match.group(2))
        if inline:
            return {"*": inline}
        scopes = {}
        for nxt in body[i + 1:]:
            if not nxt.strip():
                continue
            if not nxt.startswith(pad + "  "):  # dedented out of the block
                break
            smatch = scope.match(nxt)
            if smatch:
                scopes[smatch.group(1)] = scalar(smatch.group(2))
        return scopes
    return None


def job_permissions(job_lines):
    """A job's own `permissions:` block ({scope: value}), or None when absent."""
    return parse_permissions(job_lines, 4)


def grants_pull_request_write(job_lines):
    """True when the job's OWN block grants write on pull-requests.

    Covers `write-all` and quoted / trailing-commented values. It deliberately
    does NOT cover an ABSENT block — that inherits the caller's write scope and
    is asserted separately, so the two regressions report distinct causes.
    """
    perms = job_permissions(job_lines) or {}
    return perms.get("pull-requests") == "write" or perms.get("*") == "write-all"


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

    def test_pr_checkout_jobs_declare_an_explicit_read_only_permissions_block(self):
        # The assertion above catches a `pull-requests: write` line being ADDED
        # back. It cannot catch the likelier regression: DELETING a job's
        # `permissions:` block entirely. In a `workflow_call` reusable that means
        # the job inherits the CALLER job's permissions, and the documented
        # caller grants `pull-requests: write` — so dropping the block from
        # `consolidate` silently restores write scope on a job that checks out
        # PR code and runs the judge, while every other test here stays green.
        for name, body in self.jobs.items():
            if not checks_out_pr_repo(body):
                continue
            perms = job_permissions(body)
            self.assertIsNotNone(
                perms,
                f"job `{name}` checks out PR code and declares NO `permissions:` "
                "block — a workflow_call reusable then INHERITS the caller job's "
                "permissions, and the documented caller grants pull-requests: write",
            )
            self.assertTrue(
                perms,
                f"job `{name}` checks out PR code and its `permissions:` block "
                "parsed empty — the parser or the block shape changed",
            )
            for scope, value in sorted(perms.items()):
                self.assertIn(
                    value,
                    READ_ONLY_VALUES,
                    f"job `{name}` checks out PR code and grants "
                    f"`{scope}: {value}` — PR-checkout jobs are read-only",
                )

    def test_no_workflow_level_permissions_grant_write(self):
        # A `permissions:` block ABOVE `jobs:` applies to every job that does not
        # override it, so a write scope there would reach the PR-checkout jobs no
        # matter how careful their own blocks are.
        lines = read_workflow()
        head = lines[: lines.index("jobs:")]
        perms = parse_permissions(head, 0)
        if perms is None:  # no workflow-level block at all — nothing to inherit
            return
        for scope, value in sorted(perms.items()):
            self.assertIn(
                value,
                READ_ONLY_VALUES,
                f"workflow-level permissions grant `{scope}: {value}`, which "
                "every job inherits — including the ones that check out PR code",
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
        # code_lines, as the neighbouring helpers do: this job's own header
        # comment says "Calls cursor-agent one more time as the judge", which
        # would satisfy a raw-line scan even if the judge STEP were deleted —
        # going quiet for exactly the reason the comment above warns about. And
        # the line must START the invocation, not merely mention the binary:
        # the step's own `echo "cursor-agent (judge) exit code: ..."` would keep
        # a substring scan green with the call itself removed.
        self.assertTrue(
            any(line.strip().startswith("cursor-agent") for line in code_lines(body)),
            "consolidate no longer invokes cursor-agent — the premise of the "
            "split is gone and the isolation assertions above are vacuous",
        )

    def test_post_review_checks_out_no_pr_code(self):
        body = self.jobs["post-review"]
        self.assertFalse(checks_out_pr_repo(body))
        self.assertTrue(grants_pull_request_write(body))
        self.assertTrue(references_bot_key(body))
        # It is the job that actually posts.
        self.assertTrue(any("post-review.py" in line for line in code_lines(body)))

    def test_only_post_review_runs_post_review_py(self):
        posters = {
            name
            for name, body in self.jobs.items()
            if any("post-review.py" in line for line in code_lines(body))
        }
        self.assertEqual(posters, {"post-review"})

    def test_payload_is_handed_over_as_an_artifact(self):
        # code_lines on both sides: several comments in these two jobs name
        # the artifact, so a raw-text scan would still pass with the upload or
        # the download deleted.
        consolidate = "\n".join(code_lines(self.jobs["consolidate"]))
        post = "\n".join(code_lines(self.jobs["post-review"]))
        self.assertIn("name: cursor-review-consolidated", consolidate)
        self.assertIn("actions/upload-artifact@", consolidate)
        self.assertIn("name: cursor-review-consolidated", post)
        self.assertIn("actions/download-artifact@", post)


if __name__ == "__main__":
    unittest.main(verbosity=2)
