"""The optional `environment` input, and the boundary it must never cross.

`groom.yml` lets a caller bind a GitHub environment so the bot App key can be an
ENVIRONMENT secret behind a deployment-branch policy instead of a repository
secret every branch can read. Two properties make that safe, and both are
invisible in review once the file is 3000 lines long:

  * the default is `''` — an empty environment name binds nothing, so the eight
    existing callers, none of which pass the input, keep today's behavior; and
  * only the jobs that MINT the bot token bind it. The finder / verifier /
    builder jobs run a model over untrusted repository content, and putting one
    of those inside a credentialed environment is exactly the boundary the
    split-job topology exists to hold.

Both regress silently: a hardcoded environment name breaks every caller at
startup with no local signal, and an `environment:` added to an agent job is one
green line in a diff. Asserted as text rather than parsed — PyYAML is not stdlib
and this repo is stdlib-only (same reasoning as test_interval.py's literal pins).
"""

import os
import re
import unittest

BINDING = "environment: ${{ inputs.environment }}"
SECRET = "${{ secrets.BOT_APP_PRIVATE_KEY }}"
# The jobs that run an agent over untrusted repo content. Named explicitly, not
# derived, so deleting the binding from a credentialed job cannot silently
# shrink this set too.
AGENT_JOBS = ("audit_find", "audit_verify", "build")


def _workflow_text():
    wf = os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "groom.yml")
    with open(wf, encoding="utf-8") as f:
        return f.read()


def _job_blocks(text):
    """Map job name -> that job's block, from `jobs:` to EOF."""
    body = text.split("\njobs:\n", 1)[1]
    blocks = {}
    for block in re.split(r"(?m)^  (?=[A-Za-z_][A-Za-z0-9_-]*:\s*$)", body):
        name = block.split(":", 1)[0].strip()
        if name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            blocks[name] = block
    return blocks


class EnvironmentInputTest(unittest.TestCase):
    def setUp(self):
        self.text = _workflow_text()
        self.jobs = _job_blocks(self.text)

    def test_the_input_is_optional_and_defaults_to_empty(self):
        # The whole no-op-for-existing-callers claim rests on this default.
        decl = re.search(
            r"(?ms)^      environment:\n(.*?)(?=^      [A-Za-z_])", self.text
        )
        self.assertIsNotNone(decl, "no `environment:` input declared in groom.yml")
        body = decl.group(1)
        self.assertIn("type: string", body)
        self.assertIn("required: false", body)
        self.assertRegex(body, r"(?m)^        default: ''$")

    def test_every_job_that_mints_the_bot_token_binds_the_environment(self):
        minting = sorted(n for n, b in self.jobs.items() if SECRET in b)
        self.assertEqual(
            minting, ["build_pr", "build_select", "file"],
            "the set of jobs reading the bot App key changed — re-check the binding",
        )
        for name in minting:
            with self.subTest(job=name):
                self.assertIn(BINDING, self.jobs[name])

    def test_no_agent_job_sits_inside_a_credentialed_environment(self):
        # The security boundary: a job that reads untrusted repo content with a
        # model must never be able to reach an environment's secrets.
        for name in AGENT_JOBS:
            with self.subTest(job=name):
                self.assertIn(name, self.jobs, "agent job missing from groom.yml")
                self.assertNotRegex(
                    self.jobs[name], r"(?m)^    environment:",
                    "an agent job must not bind a GitHub environment",
                )

    def test_only_the_minting_jobs_bind_anything_at_all(self):
        # The converse of the two above, so a binding added to a NEW uncredentialed
        # job (a future gate, a summary job) is caught rather than assumed benign.
        bound = sorted(
            n for n, b in self.jobs.items() if re.search(r"(?m)^    environment:", b)
        )
        self.assertEqual(bound, ["build_pr", "build_select", "file"])

    def test_the_binding_is_the_input_and_never_a_hardcoded_name(self):
        # A literal name here would bind an environment that does not exist in a
        # caller's repo, failing every existing caller at startup.
        for line in re.findall(r"(?m)^    environment:.*$", self.text):
            with self.subTest(line=line):
                self.assertEqual(line.strip(), BINDING)


if __name__ == "__main__":
    unittest.main()
