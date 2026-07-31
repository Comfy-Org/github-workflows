#!/usr/bin/env python3
"""Tests for the single-source-of-truth @anthropic-ai/claude-code pin (BE-5373).

The pin the groom agent jobs install used to be three hardcoded literals inside
`run:` steps, which meant (a) nothing watched it — Dependabot's github-actions
ecosystem only parses `uses:` refs, and an inline `npm install -g pkg@ver` is
invisible to every ecosystem without an npm manifest — and (b) a hand bump could
update two of the three and leave a split state.

The fix moves the version into `.github/groom/package.json`, resolved once by
groom.yml's `gate` job. Nothing in CI actually runs those workflow steps, so
these are the guards that keep the arrangement from quietly regressing:

- the manifest exists, parses, and pins an EXACT version (a range would defeat
  the pin, and groom.yml's gate rejects one at run time);
- groom.yml contains no hardcoded `@anthropic-ai/claude-code@<version>` literal
  anywhere — re-introducing one is the regression;
- every "Install Claude Code" step reads the gate's output instead;
- .github/dependabot.yml still carries the npm entry for `/.github/groom` —
  without it the manifest is just an unwatched second place to rot.

Run: python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
"""

import json
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_MANIFEST = os.path.join(_REPO_ROOT, ".github", "groom", "package.json")
_GROOM_YML = os.path.join(_REPO_ROOT, ".github", "workflows", "groom.yml")
_DEPENDABOT = os.path.join(_REPO_ROOT, ".github", "dependabot.yml")

_PACKAGE = "@anthropic-ai/claude-code"
# Mirrors the gate step's shell guard in groom.yml. Exact X.Y.Z, optionally with
# a prerelease/build suffix; no ranges, wildcards or dist-tags.
_EXACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)*$")
# Any `<pkg>@<something>` literal in the workflow. The point is that NO version
# literal survives, so this is deliberately loose about what follows the `@`.
_HARDCODED = re.compile(re.escape(_PACKAGE) + r"@(?!\$\{|\")[^\s\"']+")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class ManifestTest(unittest.TestCase):
    def test_manifest_pins_the_cli_at_an_exact_version(self):
        manifest = json.loads(_read(_MANIFEST))
        deps = manifest.get("dependencies", {})
        self.assertIn(
            _PACKAGE,
            deps,
            f"{_MANIFEST} is the single source of truth for the groom agent CLI pin",
        )
        version = deps[_PACKAGE]
        self.assertRegex(
            version,
            _EXACT_VERSION,
            f"{_PACKAGE} must be pinned to an exact version, got {version!r}. "
            "A range or dist-tag would un-pin executable supply chain, and "
            "groom.yml's gate rejects it at run time.",
        )

    def test_manifest_is_private_and_declares_no_other_dependencies(self):
        # It is a pin carrier, not a project. Keeping it to one dependency is
        # what makes a Dependabot PR against this directory unambiguous, and
        # `private` keeps it from ever being publishable by accident.
        manifest = json.loads(_read(_MANIFEST))
        self.assertIs(manifest.get("private"), True)
        self.assertEqual(list(manifest.get("dependencies", {})), [_PACKAGE])
        self.assertNotIn("devDependencies", manifest)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.groom = _read(_GROOM_YML)

    def test_no_hardcoded_version_literal_remains(self):
        found = _HARDCODED.findall(self.groom)
        self.assertEqual(
            [],
            found,
            "groom.yml must not hardcode the CLI version — it comes from "
            f".github/groom/package.json via the gate job. Found: {found}",
        )

    def test_every_install_step_uses_the_gate_output(self):
        installs = [
            line
            for line in self.groom.splitlines()
            if "npm install -g" in line and _PACKAGE in line
        ]
        self.assertEqual(
            3,
            len(installs),
            f"expected the three known install sites, found {len(installs)}: {installs}",
        )
        for line in installs:
            self.assertIn("CLAUDE_CODE_VERSION", line, line)

        # Each of those steps must actually be handed the gate's output; an
        # `env:` block that never got added would leave the variable unset and
        # the `:?` guard would fail the run rather than install a wrong version,
        # but catching it here is cheaper than catching it in a groom run.
        self.assertEqual(
            3,
            self.groom.count(
                "CLAUDE_CODE_VERSION: ${{ needs.gate.outputs.claude_code_version }}"
            ),
        )

    def test_gate_exports_the_resolved_version(self):
        self.assertIn(
            "claude_code_version: ${{ steps.claude_version.outputs.version }}",
            self.groom,
        )

    def test_every_install_job_depends_on_the_gate(self):
        # The gate output is only readable by jobs that `needs: gate`; a future
        # agent job that installs the CLI without that edge would silently
        # resolve to an empty string.
        job_re = re.compile(r"^  ([a-z_][a-z0-9_]*):$", re.MULTILINE)
        starts = [(m.group(1), m.start()) for m in job_re.finditer(self.groom)]
        for name, start in starts:
            end = next(
                (s for _, s in starts if s > start),
                len(self.groom),
            )
            block = self.groom[start:end]
            if "npm install -g" in block and _PACKAGE in block:
                self.assertRegex(
                    block,
                    r"needs: (gate\b|\[gate[,\]])",
                    f"job {name!r} installs the agent CLI but does not `needs: gate`",
                )


class DependabotTest(unittest.TestCase):
    def test_npm_entry_watches_the_groom_manifest(self):
        # Text assertions rather than a YAML parse: this repo's Python is
        # stdlib-only (no PyYAML in CI), and these three lines are exactly the
        # contract — ecosystem, directory, and the exact-pin strategy the
        # workflow guard depends on.
        text = _read(_DEPENDABOT)
        self.assertIn('- package-ecosystem: "npm"', text)
        self.assertIn('directory: "/.github/groom"', text)
        self.assertIn('versioning-strategy: "increase"', text)


if __name__ == "__main__":
    unittest.main()
