#!/usr/bin/env python3
"""Tests for the groom CI-privileged patch-path policy (BE-4404).

The property the policy must hold: a patch that touches a path the caller's
pre-review CI would EXECUTE is denied (downgraded from an auto-PR to a filed
issue), and the deny must survive git's raw `-z` output — including paths that
carry an embedded quote or newline. Over-blocking is safe; under-blocking is the
security hole, so every "must NOT match" case anchors a basename exactly.

Run: python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
"""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "patch_policy.py")
_spec = importlib.util.spec_from_file_location("groom_patch_policy", _MODULE_PATH)
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)


def denied(path):
    """True if a single path is denied by the policy."""
    return policy.denied_paths([path]) == [path]


class PortedPatternsTest(unittest.TestCase):
    """The pre-BE-4404 patterns must still match, at root and nested — no regression."""

    def test_github_workflows_and_actions(self):
        for p in (
            ".github/workflows/ci.yml",
            ".github/workflows/nested/deploy.yaml",
            ".github/actions/build/action.yml",
        ):
            self.assertTrue(denied(p), p)

    def test_manifests_and_build_config_root_and_nested(self):
        for name in (
            "package.json",
            "Makefile",
            "GNUmakefile",
            "conftest.py",
            "noxfile.py",
            "tox.ini",
            "pytest.ini",
            "setup.py",
            "setup.cfg",
            "pyproject.toml",
            "Dockerfile",
            ".pre-commit-config.yaml",
        ):
            self.assertTrue(denied(name), name)
            self.assertTrue(denied("sub/pkg/" + name), "sub/pkg/" + name)


class LockfilesTest(unittest.TestCase):
    """The BE-4404 headline gap: dependency lockfiles re-resolved by CI installs."""

    LOCKFILES = (
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "Gemfile.lock",
    )

    def test_lockfiles_root_and_nested(self):
        for name in self.LOCKFILES:
            self.assertTrue(denied(name), name)
            self.assertTrue(denied("packages/app/" + name), "nested " + name)


class AddedPatternsTest(unittest.TestCase):
    """The rest of the BE-4404 additions, each root and nested where meaningful."""

    def test_husky_any_segment(self):
        for p in (".husky/pre-commit", "frontend/.husky/pre-push", "a/b/.husky/commit-msg"):
            self.assertTrue(denied(p), p)

    def test_action_manifests(self):
        for p in ("action.yml", "action.yaml", "tools/action.yml", "deep/dir/action.yaml"):
            self.assertTrue(denied(p), p)

    def test_gitmodules(self):
        self.assertTrue(denied(".gitmodules"))
        self.assertTrue(denied("vendor/.gitmodules"))

    def test_requirements_glob(self):
        for p in (
            "requirements.txt",
            "requirements-dev.txt",
            "requirements_test.txt",
            "svc/requirements.txt",
        ):
            self.assertTrue(denied(p), p)

    def test_python_and_rust_and_swift(self):
        for p in ("Pipfile", "Cargo.toml", "build.rs", "Package.swift", "crates/core/Cargo.toml"):
            self.assertTrue(denied(p), p)

    def test_xcode_and_gradle(self):
        for p in (
            "MyApp.xcodeproj/project.pbxproj",
            "build.gradle",
            "app/build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "gradle/wrapper/gradle-wrapper.properties",
            "android/gradle/wrapper/gradle-wrapper.jar",
            # Wrapper scripts CI runs via `./gradlew` — root, no extension.
            "gradlew",
            "gradlew.bat",
            "android/gradlew",
        ):
            self.assertTrue(denied(p), p)

    def test_ruby(self):
        for p in ("Gemfile", "Rakefile", "rakefile", "myproj.gemspec", "gems/foo.gemspec"):
            self.assertTrue(denied(p), p)

    def test_cmake_and_bazel(self):
        for p in (
            "CMakeLists.txt",
            "src/CMakeLists.txt",
            "cmake/toolchain.cmake",
            "WORKSPACE",
            "WORKSPACE.bazel",
            "BUILD",
            "pkg/BUILD",
            "BUILD.bazel",
            "rules/defs.bzl",
        ):
            self.assertTrue(denied(p), p)

    def test_task_runners(self):
        for p in (
            "Jenkinsfile",
            "Taskfile.yml",
            "Taskfile.yaml",
            "taskfile.yml",
            "taskfile.yaml",
            "justfile",
            "Justfile",
            "ci/Jenkinsfile",
        ):
            self.assertTrue(denied(p), p)


class PanelHardeningTest(unittest.TestCase):
    """Under-blocks closed after the BE-4404 cursor-review panel — each an
    executed-in-pre-review-CI surface the conservative default first missed."""

    def test_lowercase_makefile(self):
        # GNU Make prefers lowercase `makefile` over `Makefile`.
        for p in ("makefile", "sub/makefile"):
            self.assertTrue(denied(p), p)

    def test_go_modules(self):
        for p in ("go.mod", "go.sum", "go.work", "go.work.sum", "svc/api/go.mod"):
            self.assertTrue(denied(p), p)

    def test_dockerfile_variants(self):
        for p in (
            "Dockerfile",           # plain (regression guard)
            "Dockerfile.prod",      # suffix form
            "Dockerfile.dev",
            "prod.Dockerfile",      # extension form
            "docker/api.Dockerfile",
        ):
            self.assertTrue(denied(p), p)

    def test_cargo_config(self):
        for p in (".cargo/config.toml", ".cargo/config", "sub/.cargo/config.toml"):
            self.assertTrue(denied(p), p)

    def test_swift_and_pnpm_extras(self):
        for p in ("Package.resolved", "ios/Package.resolved", ".pnpmfile.cjs", "app/.pnpmfile.cjs"):
            self.assertTrue(denied(p), p)

    def test_case_insensitive_on_case_insensitive_runners(self):
        # macOS/Windows CI checks out `PACKAGE.JSON` as the real `package.json`.
        for p in ("PACKAGE.JSON", "MAKEFILE", "DOCKERFILE", "sub/Package-Lock.JSON"):
            self.assertTrue(denied(p), p)


class DatasetOfRecordTest(unittest.TestCase):
    """Owner-gated dataset-of-record paths (BE-9609): graded eval case files under
    a `suites/**/cases/` tree, whose merge publishes immutable versions — denied so
    a human authors the change, not the builder. Fixtures are deliberately generic
    (`s1`, `s2`): this is a PUBLIC repo, so no caller's real suite names appear."""

    def test_dataset_of_record_cases(self):
        for p in (
            "suites/s1/cases/foo.yaml",
            "suites/s2/cases/x.yml",
            "suites/s1/cases/deep/y.yaml",         # any depth under cases/
            "sub/suites/s1/cases/b.yaml",          # segment-anchored, nested tree
            "suites/cases/foo.yaml",               # flat layout — `**` spans ZERO
            #                                        segments, so the advertised
            #                                        suites/**/cases/ surface holds
            "SUITES/S1/CASES/z.YAML",              # case-insensitive
        ):
            self.assertTrue(denied(p), p)
        for p in (
            "suites/s1/driver.yaml",               # suite config, not a case
            "suites/s1/README.md",
            "suites/s1/cases/README.md",           # not YAML
            "cases/foo.yaml",                      # missing suites/ segment
            "packages/x/suites/s1/cases.yaml",     # cases.yaml file, not cases/ dir
        ):
            self.assertFalse(denied(p), p)

    def test_dataset_of_record_no_shape_bypass(self):
        """The tail and the mid-segments must not be a bypass: erring WIDE is the
        contract (invariant 1), so grouped/versioned layouts, an empty stem and a
        newline-bearing name are all denied."""
        for p in (
            "suites/group/s1/cases/x.yaml",        # extra segment ABOVE cases/
            "suites/s1/v2/deep/cases/x.yml",       # several segments above cases/
            "suites/s1/cases/.yaml",               # empty stem — `.+` missed this
            "suites/s1/cases/a\nb.yaml",           # match STRADDLES a raw newline:
            #                                        the line-split alone misses it,
            #                                        a `*.yaml` importer glob does not
        ):
            self.assertTrue(denied(p), p)

    def test_dataset_of_record_cases_symlink_shape(self):
        """git tracks no directories, so a change AT `suites/<x>/cases` is a file or
        a symlink — the indirection that would point the importer's glob at an
        undenied tree. Denied; a real `cases/`-as-directory never has this shape."""
        for p in (
            "suites/s1/cases",
            "sub/suites/s1/group/cases",
            "suites/cases",                        # flat layout — zero mid segments
            "SUITES/S1/CASES",
        ):
            self.assertTrue(denied(p), p)
        for p in (
            "cases",                               # no suites/ segment
            "suites/s1/testcases",                 # segment-anchored, not a suffix
        ):
            self.assertFalse(denied(p), p)


class RawDiffModeTest(unittest.TestCase):
    """`--raw -z` parsing plus the MODE-visible deny (BE-9612): a symlink-typed
    change in a `suites` tree is the indirection path shape cannot express — a
    link at `suites/<x>` has no `cases` segment and no YAML tail, yet a
    `suites/**/cases/*.yaml` importer resolves straight through it."""

    @staticmethod
    def raw(*entries):
        """Encode (old_mode, new_mode, path) byte triples as `--raw -z` output."""
        return b"".join(
            b":" + old + b" " + new + b" 0000000 1111111 M\x00" + path + b"\x00"
            for old, new, path in entries
        )

    def test_parse_raw_z_extracts_modes_and_paths(self):
        data = self.raw(
            (b"100644", b"100644", b"src/foo.py"),
            (b"000000", b"120000", b"suites/link"),
        )
        self.assertEqual(
            policy.parse_raw_z(data),
            [("100644", "100644", "src/foo.py"), ("000000", "120000", "suites/link")],
        )

    def test_parse_raw_z_empty_and_malformed(self):
        self.assertEqual(policy.parse_raw_z(b""), [])
        # A field where a meta record must sit but doesn't parse fails LOUD (the
        # gate runs under `set -euo pipefail`, so a raise fails closed, not open):
        # name-only-shaped input (the old producer) and an unpaired field both die.
        with self.assertRaises(ValueError):
            policy.parse_raw_z(b"package.json\x00")
        with self.assertRaises(ValueError):
            policy.parse_raw_z(b":100644 100644 0000000 1111111 M\x00")

    def test_parse_raw_z_rejects_rename_records(self):
        # Pins the module invariant-2 / `parse_raw_z` docstring claim that a
        # two-path R/C record fails CLOSED. Reachable only if a future editor
        # drops `--no-renames` from groom.yml's producer — the one edit that
        # would hide a rename's SOURCE (the denied side) from the policy and
        # rename this gate out of existence. Both arities must raise:
        rename = (
            b":100644 100644 0000000 1111111 R100\x00"
            b"suites/s1/cases/c1.yaml\x00suites/s1/retired.yaml\x00"
        )
        # one record -> 3 fields, caught by the odd-parity check;
        with self.assertRaises(ValueError):
            policy.parse_raw_z(rename)
        # two -> 6 fields, EVEN (parity check passes), caught only because the
        # misaligned meta slot holds a path that `_RAW_META` refuses.
        with self.assertRaises(ValueError):
            policy.parse_raw_z(rename * 2)

    def test_symlink_in_suites_tree_denied_by_mode(self):
        for path in (
            b"suites/newthing",        # a suite-dir-shaped link: no cases, no YAML
            b"suites",                 # the glob's root component itself
            b"sub/suites",             # nested tree's root component
            b"suites/s1/cases/link",   # inside cases/ with no YAML tail
            b"SUITES/lnk",             # case-insensitive runners (see _PATTERN)
        ):
            entries = policy.parse_raw_z(self.raw((b"000000", b"120000", path)))
            self.assertEqual(policy.denied_entries(entries), [path.decode()], path)

    def test_symlink_replaced_by_file_still_denied(self):
        # old mode 120000 → new 100644: retiring the link changes resolution too;
        # either side being a symlink denies (over-block is the safe direction).
        entries = policy.parse_raw_z(self.raw((b"120000", b"100644", b"suites/s1")))
        self.assertEqual(policy.denied_entries(entries), ["suites/s1"])

    def test_regular_files_fall_through_to_path_policy(self):
        entries = policy.parse_raw_z(
            self.raw(
                (b"100644", b"100644", b"suites/s1/harness.py"),  # suites, not a link
                (b"100644", b"100644", b"package.json"),          # path-denied as ever
                (b"000000", b"120000", b"docs/latest"),           # link OUTSIDE suites
            )
        )
        self.assertEqual(policy.denied_entries(entries), ["package.json"])


class ApiGuardTest(unittest.TestCase):
    """`denied_paths` must reject a bare str/bytes (a silent character-iteration footgun)."""

    def test_bare_str_raises(self):
        with self.assertRaises(TypeError):
            policy.denied_paths("package.json")

    def test_bare_bytes_raises(self):
        with self.assertRaises(TypeError):
            policy.denied_paths(b"package.json")

    def test_list_is_accepted(self):
        self.assertEqual(policy.denied_paths(["package.json"]), ["package.json"])


class MainStdoutTest(unittest.TestCase):
    """`main()` must emit a non-UTF-8 denied path without crashing (surrogateescape)."""

    def test_non_utf8_denied_path_is_emitted_not_crashed(self):
        import io

        # A denied path (package.json) whose DIRECTORY segment carries a raw non-UTF-8
        # byte (0xff) — the basename still anchors, so it is denied. git -z emits it
        # verbatim; parse_raw_z holds the byte as a lone surrogate.
        raw = (
            b":100644 100644 0000000 1111111 M\x00p\xffkg/package.json\x00"
            b":100644 100644 0000000 1111111 M\x00package.json\x00"
        )
        stdin = io.BytesIO(raw)
        stdout_buf = io.BytesIO()

        class _Stdin:
            buffer = stdin

        class _Stdout:
            buffer = stdout_buf

        orig_in, orig_out = policy.sys.stdin, policy.sys.stdout
        try:
            policy.sys.stdin, policy.sys.stdout = _Stdin(), _Stdout()
            rc = policy.main()
        finally:
            policy.sys.stdin, policy.sys.stdout = orig_in, orig_out
        self.assertEqual(rc, 0)
        # Both paths denied; the non-UTF-8 one survives round-trip as raw bytes.
        self.assertEqual(stdout_buf.getvalue(), b"p\xffkg/package.json\npackage.json\n")


class NegativeCasesTest(unittest.TestCase):
    """Anchoring: near-misses must NOT match, or the deny-list would over-fire noise."""

    def test_plain_source_and_docs_are_allowed(self):
        for p in (
            "src/foo.py",
            "README.md",
            "docs/guide.md",
            "src/index.ts",
            "test/data.json",
        ):
            self.assertFalse(denied(p), p)

    def test_basename_prefixes_and_suffixes_do_not_match(self):
        # The ticket's named anchoring regressions, plus a few more.
        for p in (
            "packages.json",      # not package.json
            "mypackage.json",     # not package.json
            "BUILD.md",           # not BUILD / BUILD.bazel
            "myrequirements.txt",  # requirements* glob is basename-anchored
            "notpyproject.toml",  # not pyproject.toml
            "gocode/foo.go",       # a .go source file is not go.mod/go.sum
            "cargofile.toml",      # not the `.cargo/config.toml` path suffix
        ):
            self.assertFalse(denied(p), p)

    def test_husky_substring_is_not_a_segment(self):
        # `.husky` must be a full path SEGMENT, not a substring of one.
        self.assertFalse(denied("not.husky.file"), "not.husky.file")


class RawByteRegressionTest(unittest.TestCase):
    """git C-quotes exotic paths in DEFAULT output; the policy reads raw `-z` bytes."""

    @staticmethod
    def _paths(raw_paths):
        """Wrap raw path bytes in `--raw -z` records and parse them back out."""
        data = b"".join(
            b":100644 100644 0000000 1111111 M\x00" + p + b"\x00" for p in raw_paths
        )
        return [path for _old, _new, path in policy.parse_raw_z(data)]

    def test_embedded_quote_caught_from_raw_bytes(self):
        # `.github/workflows/ev"il.yml` — default git output would quote-wrap this,
        # slipping the leading quote past a `^\.github/` anchor. Raw -z bytes don't.
        paths = self._paths([b'.github/workflows/ev"il.yml'])
        self.assertEqual(policy.denied_paths(paths), ['.github/workflows/ev"il.yml'])

    def test_embedded_newline_split_both_lines_tested(self):
        # A single path carrying a raw newline arrives (via -z) as one field; the
        # policy splits it and tests BOTH lines. Match on either => denied.
        # (a) match on the first line
        paths = self._paths([b".github/workflows/x.yml\nsecond-line"])
        self.assertEqual(len(paths), 1)
        self.assertEqual(policy.denied_paths(paths), paths)
        # (b) match on the SECOND line — proves both are tested, not just the first
        paths = self._paths([b"innocent-first\npackage.json"])
        self.assertEqual(len(paths), 1)
        self.assertEqual(policy.denied_paths(paths), paths)

    def test_multiple_records_partition(self):
        paths = self._paths([b".github/workflows/ci.yml", b"README.md", b"package-lock.json"])
        self.assertEqual(paths, [".github/workflows/ci.yml", "README.md", "package-lock.json"])
        self.assertEqual(
            policy.denied_paths(paths),
            [".github/workflows/ci.yml", "package-lock.json"],
        )

    def test_empty_input_denies_nothing(self):
        self.assertEqual(policy.parse_raw_z(b""), [])
        self.assertEqual(policy.denied_paths([]), [])
        self.assertEqual(policy.denied_entries([]), [])


if __name__ == "__main__":
    unittest.main()
