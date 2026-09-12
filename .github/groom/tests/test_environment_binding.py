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

The two matchers below are deliberately SHAPE-based rather than exact strings.
An earlier draft tested for the literal `${{ secrets.BOT_APP_PRIVATE_KEY }}` and
for `^    environment:` at exactly four spaces, which meant a benign rewrite —
`${{secrets.BOT_APP_PRIVATE_KEY}}` with the spaces closed up, the key aliased
through a job-level `env:`, or a job re-indented — silently dropped the job out
of BOTH the minting set and the bound set at once, leaving every assertEqual
green while the property they exist to protect was gone.
"""

import os
import re
import unittest

BINDING = "environment: ${{ inputs.bot_app_id != '' && inputs.environment || '' }}"
# Any reference to the bot App key, however the expression is spaced or wrapped,
# and wherever in the job it appears (a step's `with:`, or a job-level `env:`).
SECRET_RE = re.compile(r"secrets\s*\.\s*BOT_APP_PRIVATE_KEY")
# A job-level `environment:` key at ANY indentation — YAML does not require the
# two-space-per-level style this file happens to use.
ENV_KEY_RE = re.compile(r"(?m)^\s+environment\s*:")
# The jobs that run an agent over untrusted repo content. Named explicitly, not
# derived, so deleting the binding from a credentialed job cannot silently
# shrink this set too.
AGENT_JOBS = ("audit_find", "audit_verify", "build")
MINTING_JOBS = ["build_pr", "build_select", "file"]


def _workflow_text():
    wf = os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "groom.yml")
    with open(wf, encoding="utf-8") as f:
        return f.read()


def _jobs_section(text):
    """Everything from `jobs:` to EOF — excludes the `on:` input declaration,
    which is also spelled `environment:` and would otherwise match ENV_KEY_RE."""
    return text.split("\njobs:\n", 1)[1]


def _job_blocks(text):
    """Map job name -> that job's block, from `jobs:` to EOF."""
    body = _jobs_section(text)
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
        minting = sorted(n for n, b in self.jobs.items() if SECRET_RE.search(b))
        self.assertEqual(
            minting, MINTING_JOBS,
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
                self.assertIsNone(
                    ENV_KEY_RE.search(self.jobs[name]),
                    "an agent job must not bind a GitHub environment",
                )

    def test_only_the_minting_jobs_bind_anything_at_all(self):
        # The converse of the two above, so a binding added to a NEW uncredentialed
        # job (a future gate, a summary job) is caught rather than assumed benign.
        bound = sorted(n for n, b in self.jobs.items() if ENV_KEY_RE.search(b))
        self.assertEqual(bound, MINTING_JOBS)

    def test_the_binding_is_the_input_and_never_a_hardcoded_name(self):
        # A literal name here would bind an environment that does not exist in a
        # caller's repo, failing every existing caller at startup.
        lines = re.findall(r"(?m)^\s+environment\s*:.*$", _jobs_section(self.text))
        self.assertEqual(len(lines), len(MINTING_JOBS))
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(line.strip(), BINDING)

    def test_the_binding_is_dropped_when_no_bot_app_is_configured(self):
        # With no App configured there is no credential for an environment to
        # guard, and binding one anyway would park the job behind protection
        # rules for nothing — a denying rule would then drop the run's findings
        # after the audit had already been billed. The BINDING constant above
        # already pins the guarded expression on all three jobs; this test pins
        # the invariant that makes dropping it correct.
        #
        # `build_select` and `file` mint OPTIONALLY, so their mint step carries
        # the same `bot_app_id != ''` condition the binding does.
        for name in ("build_select", "file"):
            with self.subTest(job=name):
                mint = re.search(
                    r"(?ms)^      - name: Mint bot-identity token.*?(?=^      - name: )",
                    self.jobs[name],
                )
                self.assertIsNotNone(mint, "no mint step found")
                self.assertRegex(
                    mint.group(0), r"if: \$\{\{ inputs\.bot_app_id != '' \}\}"
                )
        # `build_pr` mints UNCONDITIONALLY, which is safe only because it is
        # reachable in builder mode alone and `build_select` hard-fails a
        # `builder: true` run that set no bot_app_id. If that validation ever
        # goes away, build_pr's unguarded mint becomes reachable with an empty
        # key and the dropped binding stops being a no-op.
        self.assertIn(
            'if [ "$BUILDER" = "true" ] && [ -z "$BOT_APP_ID" ]; then',
            self.jobs["build_select"],
            "build_select no longer rejects builder:true without bot_app_id — "
            "build_pr's unconditional mint depends on that check",
        )


if __name__ == "__main__":
    unittest.main()
