#!/usr/bin/env python3
"""Tests for check_agents_md.py.

Each case builds a throwaway repo tree in a tempdir and asserts which hard
checks fire (failures) vs which only warn. Covers at least one fully-passing
repo and one repo that trips every hard check.

Run: python3 .github/agents-md-integrity/tests/test_check_agents_md.py
"""

import contextlib
import importlib.util
import io
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
    "require_shim": True,
    "require_codeowners": False,
    "exclude": [],
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
        """(failures, warnings) — the pair almost every case cares about."""
        failures, warnings, _ = self._run_full(**overrides)
        return failures, warnings

    def _run_full(self, **overrides):
        """(failures, warnings, exclusions) — for the exclusion cases."""
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

    def test_missing_claude_md_fails(self):
        # The shim is REQUIRED: Claude Code reads only CLAUDE.md and does not
        # fall back to AGENTS.md, so its absence hides the instructions.
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        failures, _ = self._run()
        self.assertTrue(any("no root 'CLAUDE.md' shim" in f for f in failures))

    def test_missing_claude_md_passes_when_require_shim_off(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        failures, _ = self._run(require_shim=False)
        self.assertEqual(failures, [])

    def test_missing_agents_md_does_not_also_report_missing_shim(self):
        # Empty repo: check 1 (AGENTS.md missing) fires; the shim check stays
        # quiet rather than piling a second failure on the same root cause.
        failures, _ = self._run()
        self.assertEqual(len([f for f in failures if "CLAUDE.md" in f]), 0)

    def test_cursorrules_gate_off(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n")
        _write(self.root, ".cursorrules", "rules\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        failures, _ = self._run(forbid_cursorrules=False)
        self.assertEqual(failures, [])

    def test_nested_gate_off(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        _write(self.root, "packages/x/AGENTS.md", "nested, no shim\n")
        failures, _ = self._run(check_nested=False)
        self.assertEqual(failures, [])

    def test_nested_scan_skips_vendored_dirs(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n")
        _write(self.root, "CODEOWNERS", "* @o\n")
        # A vendored AGENTS.md must not trip the nested check.
        _write(self.root, "node_modules/pkg/AGENTS.md", "vendored\n")
        failures, _ = self._run()
        self.assertEqual(failures, [])

    def test_codeowners_missing_warns_by_default(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n")
        failures, warnings = self._run()
        self.assertEqual(failures, [])
        self.assertTrue(any("no CODEOWNERS file" in w for w in warnings))

    def test_codeowners_unmatched_warns(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n")
        _write(self.root, ".github/CODEOWNERS", "/src/ @team\n")
        failures, warnings = self._run()
        self.assertEqual(failures, [])
        self.assertTrue(any("not matched by any CODEOWNERS" in w for w in warnings))

    def test_codeowners_wildcard_matches(self):
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n")
        _write(self.root, "CODEOWNERS", "* @default-team\n")
        failures, warnings = self._run(require_codeowners=True)
        self.assertEqual(failures, [])
        self.assertEqual(warnings, [])

    def test_codeowners_last_match_wins_unassign(self):
        # A later, more specific rule with NO owner unassigns AGENTS.md.
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n")
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
        failures, _ = self._run(agents_file="docs/AGENTS.md", require_shim=False)
        self.assertEqual(failures, [])


class ExcludePathsTest(unittest.TestCase):
    """`--exclude` / `exclude_paths`: carve payload subtrees out of the walk.

    The motivating shape is a repo whose PRODUCT is agent instructions — a
    plugin marketplace ships `plugins/<name>/AGENTS.md` next to a real
    multi-line Claude payload, not an `@AGENTS.md` shim — where `check_nested`
    is correct everywhere except that subtree.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        # A compliant root, so only the nested/exclusion behavior is under test.
        _write(self.root, "AGENTS.md", "thin\n")
        _write(self.root, "CLAUDE.md", "@AGENTS.md\n")
        _write(self.root, "CODEOWNERS", "* @o\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **overrides):
        return cam.run_checks(self.root, _config(**overrides))

    def _write_plugin_payload(self):
        """The comfy-conventions shape: payload AGENTS.md + a real CLAUDE.md."""
        _write(self.root, "plugins/comfy-conventions/AGENTS.md", "payload\n")
        _write(
            self.root,
            "plugins/comfy-conventions/CLAUDE.md",
            "\n".join(f"claude payload line {i}" for i in range(44)),
        )

    # --- the four acceptance cases ---------------------------------------

    def test_excluded_nested_pair_passes(self):
        self._write_plugin_payload()
        failures, warnings, exclusions = self._run(exclude=["plugins/**"])
        self.assertEqual(failures, [])
        self.assertEqual(warnings, [])
        self.assertIn(("plugins", "plugins/**"), exclusions)

    def test_non_excluded_nested_pair_still_fails(self):
        # Same repo, a SECOND nested file outside the excluded subtree: the
        # exclusion must not disable the rule everywhere else.
        self._write_plugin_payload()
        _write(self.root, "packages/api/AGENTS.md", "nested\n")
        _write(self.root, "packages/api/CLAUDE.md", "divergent, no import\n")

        failures, _, exclusions = self._run(exclude=["plugins/**"])
        self.assertEqual(len(failures), 1)
        self.assertIn("packages/api/AGENTS.md", failures[0])
        self.assertIn("no sibling 'CLAUDE.md'", failures[0])
        self.assertNotIn("plugins", "\n".join(failures))
        self.assertEqual(exclusions, [("plugins", "plugins/**")])

    def test_exclusion_targeting_root_errors_out(self):
        for glob in ("**", "AGENTS.md", "CLAUDE.md", "*", "/AGENTS.md", "./CLAUDE.md"):
            with self.subTest(glob=glob):
                with self.assertRaises(cam.ExcludeConfigError) as ctx:
                    self._run(exclude=[glob])
                self.assertIn("not excludable", str(ctx.exception))

    def test_root_exclusion_rejected_even_when_nested_check_is_off(self):
        # The glob is never consulted with check_nested off, but asking for
        # something the checker will not do is still a loud config error.
        with self.assertRaises(cam.ExcludeConfigError):
            self._run(exclude=["**"], check_nested=False)

    def test_pathful_agents_file_is_protected_too(self):
        _write(self.root, "docs/AGENTS.md", "thin\n")
        with self.assertRaises(cam.ExcludeConfigError):
            self._run(agents_file="docs/AGENTS.md", exclude=["docs/**"])
        # ...but excluding an unrelated subtree is still fine.
        self._run(agents_file="docs/AGENTS.md", exclude=["plugins/**"])

    def test_no_exclude_reproduces_todays_behavior(self):
        self._write_plugin_payload()
        failures, warnings, exclusions = self._run()
        self.assertEqual(exclusions, [])
        self.assertEqual(warnings, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("plugins/comfy-conventions/AGENTS.md", failures[0])
        self.assertIn("no sibling 'CLAUDE.md'", failures[0])

    # --- walk-time (not post-filter) semantics ----------------------------

    def test_excluded_subtree_is_never_line_counted(self):
        # A nested payload file way over the ceiling: a post-filter on findings
        # would have opened and counted it first. Excluded means never read.
        _write(
            self.root,
            "plugins/big/AGENTS.md",
            "\n".join(f"l{i}" for i in range(500)),
        )
        _write(self.root, "plugins/big/CLAUDE.md", "44 lines of payload\n")
        failures, _, _ = self._run(exclude=["plugins/**"])
        self.assertEqual(failures, [])

    def test_exclusion_prunes_the_directory_before_descending(self):
        _write(self.root, "plugins/a/b/c/AGENTS.md", "deep payload\n")
        failures, _, exclusions = self._run(exclude=["plugins/**"])
        self.assertEqual(failures, [])
        # Reported once, at the pruned directory — not once per buried file.
        self.assertEqual(exclusions, [("plugins", "plugins/**")])

    def test_directly_matched_nested_file_is_reported(self):
        glob = "packages/api/AGENTS.md"
        _write(self.root, glob, "nested, no shim\n")
        failures, _, exclusions = self._run(exclude=[glob])
        self.assertEqual(failures, [])
        self.assertEqual(exclusions, [(glob, glob)])

    def test_exclusions_with_check_nested_off_warn_that_they_do_nothing(self):
        # Both knobs set is the invisible-coverage-loss shape exclusions exist
        # to replace, so say so rather than letting it read as scoped.
        _write(self.root, "plugins/x/AGENTS.md", "payload\n")
        failures, warnings, exclusions = self._run(
            exclude=["plugins/**"], check_nested=False
        )
        self.assertEqual(failures, [])
        self.assertEqual(exclusions, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("`check_nested` is false", warnings[0])

    def test_blank_glob_in_a_handmade_config_is_dropped(self):
        _write(self.root, "packages/api/AGENTS.md", "nested, no shim\n")
        failures, _, exclusions = self._run(exclude=["", "   "])
        self.assertEqual(exclusions, [])
        self.assertEqual(len(failures), 1)  # a blank glob excludes NOTHING

    def test_skip_dirs_remain_the_always_on_baseline(self):
        # SKIP_DIRS is not replaced by --exclude; it still applies alongside it.
        _write(self.root, "node_modules/pkg/AGENTS.md", "vendored\n")
        _write(self.root, "plugins/x/AGENTS.md", "payload\n")
        failures, _, exclusions = self._run(exclude=["plugins/**"])
        self.assertEqual(failures, [])
        # The vendored tree is skipped silently (baseline), not reported as an
        # exclusion — only the caller's own globs get an EXCLUDED line.
        self.assertEqual(exclusions, [("plugins", "plugins/**")])

    # --- glob semantics ---------------------------------------------------

    def test_bare_directory_glob_excludes_the_whole_subtree(self):
        _write(self.root, "plugins/x/AGENTS.md", "payload\n")
        failures, _, exclusions = self._run(exclude=["plugins"])
        self.assertEqual(failures, [])
        self.assertEqual(exclusions, [("plugins", "plugins")])

    def test_single_star_does_not_cross_a_path_separator(self):
        _write(self.root, "packages/api/AGENTS.md", "nested, no shim\n")
        # `packages/*` matches `packages/api` (one segment) — excluded.
        failures, _, _ = self._run(exclude=["packages/*"])
        self.assertEqual(failures, [])
        # `pack*/AGENTS.md` must NOT match `packages/api/AGENTS.md`.
        failures, _, exclusions = self._run(exclude=["pack*/AGENTS.md"])
        self.assertEqual(exclusions, [])
        self.assertEqual(len(failures), 1)

    def test_leading_double_star_matches_at_any_depth(self):
        _write(self.root, "a/payload/AGENTS.md", "payload\n")
        _write(self.root, "payload/AGENTS.md", "payload\n")
        failures, _, exclusions = self._run(exclude=["**/payload"])
        self.assertEqual(failures, [])
        self.assertEqual(
            exclusions, [("a/payload", "**/payload"), ("payload", "**/payload")]
        )

    def test_interior_double_star_spans_zero_segments(self):
        # The documented contract is "`**` crosses segments", and the leading
        # `**/` case already matches zero of them; an interior one that needed
        # at least one segment would silently not apply the exclusion a caller
        # wrote in the standard globstar form.
        _write(self.root, "plugins/AGENTS.md", "payload\n")
        _write(self.root, "plugins/deep/nest/AGENTS.md", "payload\n")
        failures, _, exclusions = self._run(exclude=["plugins/**/AGENTS.md"])
        self.assertEqual(failures, [])
        self.assertIn(("plugins/AGENTS.md", "plugins/**/AGENTS.md"), exclusions)
        self.assertIn(
            ("plugins/deep/nest/AGENTS.md", "plugins/**/AGENTS.md"), exclusions
        )

    def test_trailing_double_star_prunes_at_the_directory_itself(self):
        # `plugins` and `plugins/**` are documented as identical. If `/**`
        # matched only the CHILDREN, a marketplace with hundreds of plugins
        # would emit hundreds of EXCLUDED lines instead of one.
        for name in ("a", "b", "c"):
            _write(self.root, f"plugins/{name}/AGENTS.md", "payload\n")
        bare = self._run(exclude=["plugins"])
        globbed = self._run(exclude=["plugins/**"])
        self.assertEqual(bare[0], [])
        self.assertEqual([p for p, _ in bare[2]], ["plugins"])
        self.assertEqual([p for p, _ in globbed[2]], ["plugins"])

    def test_glob_is_a_strict_full_match_not_match_plus_dollar(self):
        # Python's `$` also matches before a trailing newline, and a path
        # component may contain one, so `re.match(...\n)` would let a crafted
        # directory name be pruned by an exclusion that does not name it.
        regex = cam._exclude_pattern_to_regex("plugins/demo")
        self.assertTrue(regex.fullmatch("plugins/demo"))
        self.assertTrue(regex.fullmatch("plugins/demo/nested"))
        self.assertIsNone(regex.fullmatch("plugins/demo\n"))
        self.assertIsNone(cam._match_exclude("plugins/demo\n", [("g", regex)]))

    def test_wildcard_only_glob_is_rejected(self):
        # `*/**` matches every path containing a slash but neither protected
        # root file, so the root guard alone would let it disable the whole
        # nested scan while `check_nested` still read `true`.
        for glob in ("*/**", "*/*", "**/*", "**/**"):
            with self.subTest(glob=glob):
                with self.assertRaises(cam.ExcludeConfigError) as ctx:
                    self._run(exclude=[glob])
                self.assertIn("not excludable", str(ctx.exception))

    def test_glob_normalizing_to_the_root_is_rejected(self):
        # `/` most plausibly reads as "exclude the repo root"; it must be the
        # loud exit-2 rejection, not a regex that silently matches nothing.
        for glob in ("/", "./", "//", "."):
            with self.subTest(glob=glob):
                with self.assertRaises(cam.ExcludeConfigError) as ctx:
                    self._run(exclude=[glob])
                self.assertIn("not excludable", str(ctx.exception))

    def test_unmatched_glob_is_a_silent_no_op_not_an_error(self):
        _write(self.root, "packages/api/AGENTS.md", "nested\n")
        _write(self.root, "packages/api/CLAUDE.md", "@AGENTS.md\n")
        failures, warnings, exclusions = self._run(exclude=["does/not/exist/**"])
        self.assertEqual(failures, [])
        self.assertEqual(warnings, [])
        self.assertEqual(exclusions, [])

    # --- CLI / value parsing ----------------------------------------------

    def test_split_patterns_handles_repeatable_csv_and_newlines(self):
        self.assertEqual(
            cam._split_patterns(["a/**,b/**", "  c/** \n\n d/** \n", ""]),
            ["a/**", "b/**", "c/**", "d/**"],
        )

    def test_split_patterns_of_blank_input_is_empty(self):
        # The workflow passes the raw input through; a blank one must be a
        # true no-op rather than an empty glob that matches everything.
        for value in ([], [""], ["   "], ["\n"], [",,"]):
            with self.subTest(value=value):
                self.assertEqual(cam._split_patterns(value), [])

    def _main(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cam.main(["--root", self.root, *argv])
        return code, buf.getvalue()

    def test_cli_excluded_payload_passes_and_logs_the_exclusion(self):
        self._write_plugin_payload()
        code, out = self._main("--exclude", "plugins/**")
        self.assertEqual(code, 0)
        self.assertIn("Exclusion globs: plugins/**", out)
        self.assertIn("EXCLUDED: plugins (matched plugins/**)", out)
        self.assertIn("::notice::AGENTS.md integrity: EXCLUDED", out)
        self.assertIn("Result: AGENTS.md integrity OK.", out)

    def test_cli_without_exclude_fails_the_same_payload(self):
        self._write_plugin_payload()
        code, out = self._main()
        self.assertEqual(code, 1)
        self.assertNotIn("EXCLUDED", out)
        self.assertNotIn("Exclusion globs", out)

    def test_cli_root_exclusion_exits_two(self):
        code, out = self._main("--exclude", "**")
        self.assertEqual(code, 2)
        self.assertIn("not excludable", out)
        self.assertIn("::error::", out)

    def test_annotations_escape_newlines_out_of_repo_controlled_paths(self):
        # A path component may contain a newline, and the scanned tree is
        # PR-controlled: unescaped, the name below would close the `::notice::`
        # and emit a second workflow command that suppresses the annotations
        # printed after it.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cam._emit(
                ["nested 'a\n::error::forged' is bad"],
                [],
                [("x\n::stop-commands::tok", "x*")],
            )
        lines = buf.getvalue().splitlines()
        # The injected commands survive only as inert %0A-escaped text, so no
        # LINE begins with a workflow command other than the ones we emitted.
        self.assertIn("EXCLUDED: x%0A::stop-commands::tok (matched x*)", lines)
        for line in lines:
            if line.startswith("::"):
                self.assertRegex(line, r"^::(notice|warning|error)::AGENTS\.md ")

    def test_cli_accepts_repeated_and_csv_flags(self):
        _write(self.root, "plugins/x/AGENTS.md", "payload\n")
        _write(self.root, "vendored-skills/y/AGENTS.md", "payload\n")
        code, out = self._main("--exclude", "plugins/**,vendored-skills/**")
        self.assertEqual(code, 0)
        self.assertIn("EXCLUDED: plugins (matched plugins/**)", out)
        self.assertIn("EXCLUDED: vendored-skills (matched vendored-skills/**)", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
