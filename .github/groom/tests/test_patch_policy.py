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
    """Owner-gated dataset-of-record paths (BE-9609): graded eval cases under
    `suites/*/cases/` whose merge publishes immutable versions — denied so a
    human authors the change, not the builder."""

    def test_dataset_of_record_cases(self):
        for p in (
            "suites/agent/cases/foo.yaml",
            "suites/cloud-mcp/cases/x.yml",
            "suites/creative/cases/deep/y.yaml",   # any depth under cases/
            "sub/suites/a/cases/b.yaml",           # segment-anchored, nested tree
            "SUITES/Agent/CASES/z.YAML",           # case-insensitive
        ):
            self.assertTrue(denied(p), p)
        for p in (
            "suites/cloud-mcp/driver-claude-mcp.yaml",  # suite config, not a case
            "suites/agent/README.md",
            "suites/agent/cases/README.md",             # not YAML
            "cases/foo.yaml",                           # missing suites/ segment
            "suites/cases/foo.yaml",                    # missing suite segment
            "packages/x/suites/agent/cases.yaml",       # cases.yaml file, not cases/ dir
        ):
            self.assertFalse(denied(p), p)


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
        # verbatim; parse_nul_delimited holds the byte as a lone surrogate.
        raw = b"p\xffkg/package.json\x00package.json\x00"
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

    def test_embedded_quote_caught_from_raw_bytes(self):
        # `.github/workflows/ev"il.yml` — default git output would quote-wrap this,
        # slipping the leading quote past a `^\.github/` anchor. Raw -z bytes don't.
        raw = b'.github/workflows/ev"il.yml'
        paths = policy.parse_nul_delimited(raw)
        self.assertEqual(policy.denied_paths(paths), ['.github/workflows/ev"il.yml'])

    def test_embedded_newline_split_both_lines_tested(self):
        # A single path carrying a raw newline arrives (via -z) as one field; the
        # policy splits it and tests BOTH lines. Match on either => denied.
        # (a) match on the first line
        paths = policy.parse_nul_delimited(b".github/workflows/x.yml\nsecond-line")
        self.assertEqual(len(paths), 1)
        self.assertEqual(policy.denied_paths(paths), paths)
        # (b) match on the SECOND line — proves both are tested, not just the first
        paths = policy.parse_nul_delimited(b"innocent-first\npackage.json")
        self.assertEqual(len(paths), 1)
        self.assertEqual(policy.denied_paths(paths), paths)

    def test_nul_delimited_multiple_paths_partition(self):
        raw = b".github/workflows/ci.yml\x00README.md\x00package-lock.json\x00"
        paths = policy.parse_nul_delimited(raw)
        self.assertEqual(paths, [".github/workflows/ci.yml", "README.md", "package-lock.json"])
        self.assertEqual(
            policy.denied_paths(paths),
            [".github/workflows/ci.yml", "package-lock.json"],
        )

    def test_empty_input_denies_nothing(self):
        self.assertEqual(policy.parse_nul_delimited(b""), [])
        self.assertEqual(policy.denied_paths([]), [])


if __name__ == "__main__":
    unittest.main()
