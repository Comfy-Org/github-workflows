#!/usr/bin/env python3
"""Tests for check_agents_md.py.

Each case builds a throwaway repo tree in a tempdir and asserts which hard
checks fire (failures) vs which only warn. Covers at least one fully-passing
repo and one repo that trips every hard check.

Run: python3 .github/agents-md-integrity/tests/test_check_agents_md.py
"""

import importlib.util
import os
import tempfile
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "check_agents_md.py")
_spec = importlib.util.spec_from_file_location("check_agents_md", _MODULE_PATH)
cam = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cam)


DEFAULT_CONFIG = {
    "agents_file": "AGENTS.md",
    "max_lines": 200,
    "warn_lines": 150,
    "forbid_cursorrules": True,
    "check_nested": True,
    "require_codeowners": False,
}


def _config(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class CheckAgentsMdTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **overrides):
        return cam.run_checks(self.root, _config(**overrides))

    # --- passing case -----------------------------------------------------

    def test_fully_compliant_repo_passes(self):
        _write(self.root, "AGENTS.md", "\n".join(f"line {i}" for i in range(120)))
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n")
        _write(self.root, ".github/CODEOWNERS", "/AGENTS.md @comfy-org/backend\n")
        # A well-formed nested package.
        _write(self.root, "packages/api/AGENTS.md", "nested\n")
        _write(self.root, "packages/api/CLAUDE.md", "@AGENTS.md\n")

        failures, warnings = self._run()
        self.assertEqual(failures, [])
        self.assertEqual(warnings, [])

    def test_warn_line_target_is_not_a_failure(self):
        # 170 lines: over warn_lines (150), under max_lines (200).
        _write(self.root, "AGENTS.md", "\n".join(f"l{i}" for i in range(170)))
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n")
        _write(self.root, "CODEOWNERS", "* @owner\n")

        failures, warnings = self._run()
        self.assertEqual(failures, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("aspirational target", warnings[0])

    # --- failing cases ----------------------------------------------------

    def test_missing_agents_md_fails(self):
        failures, _ = self._run()
        self.assertTrue(any("not found at the repo root" in f for f in failures))

    def test_every_hard_check_can_fail_at_once(self):
        # Over the ceiling.
        _write(self.root, "AGENTS.md", "\n".join(f"l{i}" for i in range(250)))
        # Divergent CLAUDE.md (no import).
        _write(self.root, "CLAUDE.md", "totally different instructions\n")
        # Legacy cursorrules.
        _write(self.root, ".cursorrules", "old rules\n")
        # Nested AGENTS.md, no sibling shim, also over the ceiling.
        _write(
            self.root,
            "packages/web/AGENTS.md",
            "\n".join(f"l{i}" for i in range(300)),
        )
        # No CODEOWNERS -> require_codeowners escalates to a failure.
        failures, warnings = self._run(require_codeowners=True)

        joined = "\n".join(failures)
        self.assertIn("over the hard ceiling", joined)  # top-level line ceiling
        self.assertIn("divergent copy", joined)  # CLAUDE.md shim
        self.assertIn(".cursorrules", joined)  # legacy file
        self.assertIn("no sibling 'CLAUDE.md'", joined)  # nested shim
        self.assertIn("packages/web/AGENTS.md' is 300 lines", joined)  # nested ceiling
        self.assertTrue(any("DRI" in f for f in failures))  # CODEOWNERS as failure

    def test_divergent_claude_md_fails(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CLAUDE.md", "no import here\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        failures, _ = self._run()
        self.assertTrue(any("divergent copy" in f for f in failures))

    def test_claude_md_shim_with_extra_lines_passes(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n\nClaude-only note.\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        failures, _ = self._run()
        self.assertEqual(failures, [])

    def test_no_claude_md_is_fine(self):
        # CLAUDE.md is optional; its absence is not a failure.
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        failures, _ = self._run()
        self.assertEqual(failures, [])

    def test_cursorrules_gate_off(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, ".cursorrules", "rules\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        failures, _ = self._run(forbid_cursorrules=False)
        self.assertEqual(failures, [])

    def test_nested_gate_off(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        _write(self.root, "packages/x/AGENTS.md", "nested, no shim\n")
        failures, _ = self._run(check_nested=False)
        self.assertEqual(failures, [])

    def test_nested_scan_skips_vendored_dirs(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        # A vendored AGENTS.md must not trip the nested check.
        _write(self.root, "node_modules/pkg/AGENTS.md", "vendored\n")
        failures, _ = self._run()
        self.assertEqual(failures, [])

    def test_codeowners_missing_warns_by_default(self):
        _write(self.root, "AGENTS.md", "thin\n")
        failures, warnings = self._run()
        self.assertEqual(failures, [])
        self.assertTrue(any("no CODEOWNERS file" in w for w in warnings))

    def test_codeowners_unmatched_warns(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, ".github/CODEOWNERS", "/src/ @team\n")
        failures, warnings = self._run()
        self.assertEqual(failures, [])
        self.assertTrue(any("not matched by any CODEOWNERS" in w for w in warnings))

    def test_codeowners_wildcard_matches(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CODEOWNERS", "* @default-team\n")
        failures, warnings = self._run(require_codeowners=True)
        self.assertEqual(failures, [])
        self.assertEqual(warnings, [])

    def test_codeowners_last_match_wins_unassign(self):
        # A later, more specific rule with NO owner unassigns AGENTS.md.
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CODEOWNERS", "* @default\n/AGENTS.md\n")
        failures, warnings = self._run()
        self.assertTrue(any("not matched by any CODEOWNERS" in w for w in warnings))

    def test_custom_agents_file_name(self):
        _write(self.root, "GUIDELINES.md", "thin\n")
        _write(self.root, "CLAUDE.md", "@GUIDELINES.md\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        failures, _ = self._run(agents_file="GUIDELINES.md")
        self.assertEqual(failures, [])

    def test_pathful_agents_file_not_double_checked_as_nested(self):
        # A pathful agents_file must be checked as the top-level file only, not
        # also flagged as a shim-less nested file.
        _write(self.root, "docs/AGENTS.md", "thin\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        failures, _ = self._run(agents_file="docs/AGENTS.md")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
