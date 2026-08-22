"""Unit tests for the centralized public-repo hygiene checker (BE-8654).

Two things are under test here. The first is the ordinary one: each of the
three detection categories still fires, and the two per-repo knobs behave.

The second is the reason this file moved into github-workflows at all -- that
a PR in the CALLER repo cannot alter the checker or its allowlist. The
`TamperResistanceTest` case below asserts that directly, by planting a
wide-open copy of the checker (and every other override shape someone might
reach for) inside the scanned tree and proving the real checker's verdict does
not move. That is a property of the checker itself, so it is worth an
assertion rather than a comment: the workflow half of the guarantee -- loading
this file from a pinned ref instead of the caller's checkout -- is asserted by
`.github/workflows/test-public-repo-hygiene.yml`.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import check_public_repo_hygiene as checker  # noqa: E402


class RepoFixture:
    """A throwaway git work tree, because the checker scans TRACKED files."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        self._git("init", "-q")
        # Local-only config so the fixture never depends on the machine's.
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "test")

    def _git(self, *args):
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True
        )

    def write(self, rel, content, track=True):
        path = os.path.join(self.root, rel)
        if os.path.dirname(rel):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as fh:
            fh.write(content)
        if track:
            self._git("add", "--", rel)
        return path

    def cleanup(self):
        self.dir.cleanup()


class CheckerTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture()
        self.addCleanup(self.repo.cleanup)

    def run_checks(self, **kwargs):
        return checker.run_checks(self.repo.root, **kwargs)

    def findings(self, **kwargs):
        return self.run_checks(**kwargs)[0]

    def warnings(self, **kwargs):
        return self.run_checks(**kwargs)[2]


class TicketIdCategoryTest(CheckerTestCase):
    def test_ticket_shaped_id_is_flagged(self):
        self.repo.write("README.md", "See BE-1234 for context.\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("possible internal ticket ID", findings[0])
        self.assertIn("BE-1234", findings[0])
        self.assertTrue(findings[0].startswith("README.md:1:"), findings[0])

    def test_builtin_acronyms_are_not_flagged(self):
        self.repo.write("docs.md", "Encoded UTF-8, hashed SHA-256, dated ISO-8601.\n")
        self.assertEqual(self.findings(), [])

    def test_shape_boundaries(self):
        # One letter and seven letters are both outside [A-Z]{2,6}; one digit
        # and seven digits are both outside \d{2,6}. Keeping these pinned is
        # what stops a "small" regex tidy-up from silently widening or
        # narrowing the category.
        self.repo.write(
            "edge.md",
            "A-1234 ABCDEFG-1234 BE-1 BE-1234567 be-1234 BE-12 ABCDEF-123456\n",
        )
        flagged = sorted(f.split(": ")[-1] for f in self.findings())
        self.assertEqual(flagged, ["'ABCDEF-123456'", "'BE-12'"])

    def test_extra_ticket_allow_is_additive(self):
        self.repo.write("README.md", "GPU-100 and SHA-256 and BE-1234.\n")
        findings = self.findings(extra_ticket_allow=["GPU-100"])
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("BE-1234", findings[0])
        # ...and the built-ins survive the extension.
        self.assertFalse(any("SHA-256" in f for f in findings))

    def test_extra_ticket_allow_is_case_insensitive(self):
        self.repo.write("README.md", "GPU-100\n")
        self.assertEqual(self.findings(extra_ticket_allow=["gpu-100"]), [])


class InternalMarkerCategoryTest(CheckerTestCase):
    MARKERS = (
        "https://www.notion.so/team/page",
        "https://comfy.notion.site/page",
        "https://comfy.slack.com/archives/C123",
        "https://comfy.slack.com/client/T1/C1",
        "https://app.slack.com/client/T1",
        "https://docs.google.com/document/d/abc",
        "https://drive.google.com/file/d/abc",
        "https://app.datadoghq.com/dashboard/abc",
        "https://us.posthog.com/project/1/insights",
        "https://linear.app/comfyorg/issue/AA-1",
        "see incident-42 for the postmortem",
    )

    def test_each_marker_is_flagged(self):
        # At least one -- not exactly one. A line can satisfy two patterns
        # (`app.slack.com/client/` hits both the host and the path rule) and
        # both copies this replaces emitted one finding per matching pattern.
        # Preserving that keeps the merged checker's output identical to the
        # scripts it retires, which is what the parity check compares.
        for marker in self.MARKERS:
            with self.subTest(marker=marker):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                repo.write("notes.md", marker + "\n")
                findings = [
                    f
                    for f in checker.run_checks(repo.root)[0]
                    if "collaboration-tool marker" in f
                ]
                self.assertGreaterEqual(len(findings), 1, f"{marker}: {findings}")

    def test_marker_match_is_case_insensitive(self):
        self.repo.write("notes.md", "HTTPS://APP.DATADOGHQ.COM/dash\n")
        self.assertTrue(
            any("collaboration-tool marker" in f for f in self.findings())
        )

    def test_public_lookalikes_are_not_flagged(self):
        # `posthog.com` and `slack.com` on their own are public marketing
        # pages; only the project/archive paths are internal.
        self.repo.write(
            "notes.md",
            "https://posthog.com/docs and https://slack.com/intl/en-gb/\n",
        )
        self.assertEqual(self.findings(), [])


class RepoReferenceCategoryTest(CheckerTestCase):
    def test_unknown_repo_is_flagged_default_deny(self):
        self.repo.write("README.md", "See Comfy-Org/some-internal-thing.\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("not in the known-public allowlist", findings[0])
        self.assertIn("Comfy-Org/some-internal-thing", findings[0])

    def test_known_public_repos_pass(self):
        self.repo.write(
            "README.md",
            "Comfy-Org/ComfyUI Comfy-Org/comfy-cli Comfy-Org/ComfyUI_frontend\n",
        )
        self.assertEqual(self.findings(), [])

    def test_github_workflows_is_allowlisted(self):
        # The regression that surfaced BE-8654: both per-repo copies were
        # missing this repo, so a caller pinned at
        # `Comfy-Org/github-workflows/...` failed the hygiene check it was
        # being added alongside.
        self.repo.write(
            ".github/workflows/ci.yml",
            "    uses: Comfy-Org/github-workflows/.github/workflows/x.yml@abc\n",
        )
        self.assertEqual(self.findings(), [])

    def test_sentence_final_period_is_a_known_false_positive(self):
        # Carried over DELIBERATELY from both scripts this replaces: the repo
        # name class includes `.`, so prose ending "...see Comfy-Org/ComfyUI."
        # is flagged as `ComfyUI.`. Pinning it here makes the behaviour a
        # reviewed decision rather than an accident -- the parity proof for
        # this migration is only worth something if the migration changed
        # nothing, so the fix belongs in its own change. See README.md
        # "Known limitations".
        self.repo.write("README.md", "Built on Comfy-Org/ComfyUI.\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Comfy-Org/ComfyUI.", findings[0])

    def test_trailing_git_suffix_is_stripped(self):
        self.repo.write(
            "package.json",
            '{"repository": "https://github.com/Comfy-Org/comfy-typescript-sdk.git"}\n',
        )
        self.assertEqual(self.findings(), [])

    def test_codeowners_team_handles(self):
        self.repo.write(
            ".github/CODEOWNERS",
            "* @Comfy-Org/comfy-cloud-team\ndocs/ @Comfy-Org/secret-squad\n",
        )
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/secret-squad", findings[0])
        self.assertIn("a team not in the known-public allowlist", findings[0])

    def test_team_allowlist_does_not_leak_into_repo_allowlist(self):
        # A team handle and a repo reference share a namespace in the source
        # text but not in the allowlists; crossing them would let a public team
        # name whitelist a private repo of the same name.
        self.repo.write("README.md", "Comfy-Org/comfy-cloud-team\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("which is not in the known-public allowlist", findings[0])


class ScanScopeTest(CheckerTestCase):
    def test_untracked_files_are_not_scanned(self):
        self.repo.write("leak.md", "BE-1234\n", track=False)
        self.assertEqual(self.findings(), [])

    def test_binary_files_are_skipped(self):
        self.repo.write("blob.bin", b"BE-1234\x00\xff\xfe", track=True)
        self.assertEqual(self.findings(), [])

    def test_undecodable_text_is_skipped(self):
        self.repo.write("latin.txt", b"BE-1234 caf\xe9\n", track=True)
        self.assertEqual(self.findings(), [])

    def test_directory_exclusion_prunes_subtree_and_is_counted(self):
        self.repo.write("src/generated/a.py", "BE-1234\n")
        self.repo.write("src/generated/deep/b.py", "BE-5678\n")
        self.repo.write("src/hand.py", "BE-9999\n")
        findings, exclusions, _ = self.run_checks(excludes=["src/generated/"])
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("src/hand.py", findings[0])
        self.assertEqual(exclusions, [("src/generated/", 2)])

    def test_file_exclusion_is_exact(self):
        self.repo.write("scripts/check.py", "BE-1234\n")
        self.repo.write("scripts/check.py.bak", "BE-5678\n")
        findings, exclusions, _ = self.run_checks(excludes=["scripts/check.py"])
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("scripts/check.py.bak", findings[0])
        self.assertEqual(exclusions, [("scripts/check.py", 1)])

    def test_exclusion_that_matches_nothing_is_still_reported(self):
        # A typo'd exclusion has to be visible in the log. Reporting only the
        # ones that fired is how a repo ends up believing it excluded a tree
        # it never named correctly.
        self.repo.write("a.md", "hello\n")
        _, exclusions, _ = self.run_checks(excludes=["typo/"])
        self.assertEqual(exclusions, [("typo/", 0)])

    def test_root_exclusion_is_rejected(self):
        for bad in ("/", ".", "./", "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(checker.ConfigError):
                    self.run_checks(excludes=[bad])

    def test_parent_traversal_is_rejected(self):
        with self.assertRaises(checker.ConfigError):
            self.run_checks(excludes=["../elsewhere/"])

    def test_non_git_root_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaises(checker.ConfigError):
                checker.run_checks(plain)


class CoverageReportingTest(CheckerTestCase):
    """Nothing the checker skips may be invisible in the log (BE-8654).

    A guard that silently declines to scan something is worse than no guard,
    because the green run reads as coverage. Each skip route therefore has to
    leave a trace: an exclusion leaves a count, an unreadable file leaves a
    warning, and a run that scanned nothing at all says so.
    """

    def test_unreadable_file_warns_rather_than_disappearing(self):
        # A dangling symlink is tracked, is not binary, and cannot be opened.
        os.symlink("nowhere-at-all", os.path.join(self.repo.root, "dangling"))
        self.repo._git("add", "--", "dangling")
        self.repo.write("ok.md", "clean\n")
        findings, _, warnings = self.run_checks()
        self.assertEqual(findings, [])
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("dangling", warnings[0])
        self.assertIn("NOT scanned", warnings[0])

    def test_binary_and_non_utf8_skips_are_not_warnings(self):
        # These are ordinary, expected skips; warning on them would bury the
        # unreadable case in noise.
        self.repo.write("blob.bin", b"\x00\xff", track=True)
        self.repo.write("latin.txt", b"caf\xe9\n", track=True)
        self.assertEqual(self.warnings(), [])

    def test_scanning_nothing_is_never_reported_as_clean(self):
        self.repo.write("only.md", "clean\n")
        findings, _, warnings = self.run_checks(excludes=["only.md"])
        self.assertEqual(findings, [])
        self.assertTrue(
            any("no files were scanned" in w for w in warnings), warnings
        )

    def test_empty_repo_warns_too(self):
        self.assertTrue(
            any("no files were scanned" in w for w in self.warnings())
        )

    def test_script_checkout_dir_is_skipped_and_reported(self):
        # The reusable workflow checks THIS repo out at `_public_repo_hygiene/`
        # in the caller's workspace. A caller that tracks a directory of that
        # name would otherwise have this repo's own ticket ids and Comfy-Org
        # references scanned as its own.
        self.repo.write("_public_repo_hygiene/x.py", "BE-1234 Comfy-Org/private\n")
        self.repo.write("real.md", "clean\n")
        findings, exclusions, _ = self.run_checks()
        self.assertEqual(findings, [])
        self.assertIn(("_public_repo_hygiene/", 1), exclusions)

    def test_script_checkout_skip_is_silent_when_it_skips_nothing(self):
        self.repo.write("real.md", "clean\n")
        _, exclusions, _ = self.run_checks()
        self.assertEqual(exclusions, [])

    def test_undecodable_path_bytes_do_not_crash_the_report(self):
        # `git ls-files` paths arrive via surrogateescape, so a filename holding
        # non-UTF-8 bytes carries lone surrogates. Printing one to a strict
        # stdout used to be a UnicodeEncodeError instead of a finding.
        name = b"le\xe9k.md"
        path = os.path.join(self.repo.root.encode(), name)
        try:
            with open(path, "wb") as fh:
                fh.write(b"Comfy-Org/super-secret-repo\n")
        except OSError as exc:
            # APFS enforces UTF-8 filenames, so this case cannot be built on
            # macOS at all. It is real on the ubuntu-latest runners, where ext4
            # takes any byte but `/` and NUL — skip rather than delete, so the
            # assertion still runs where the behaviour exists.
            raise unittest.SkipTest(f"filesystem rejects non-UTF-8 names: {exc}")
        self.repo._git("add", "-A")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        # The escaping is what makes this printable at all.
        checker._esc_cmd(findings[0]).encode("utf-8")


class TamperResistanceTest(CheckerTestCase):
    """A caller-side edit to the allowlist must have NO effect (BE-8654).

    This is the acceptance criterion the whole move exists for. The scanned
    tree below contains every shape a PR author might reach for to widen the
    allowlist from inside the repo being checked -- an in-tree copy of the
    checker with a wide-open list, a config file, and (separately) environment
    variables named after the module constants. The verdict must not move.
    """

    WIDE_OPEN_COPY = (
        "PUBLIC_COMFY_ORG_REPOS = {'super-secret-repo'}\n"
        "PUBLIC_COMFY_ORG_TEAMS = {'secret-squad'}\n"
        "TICKET_ALLOWLIST = {'BE-1234'}\n"
        "INTERNAL_MARKER_RES = []\n"
        "def main():\n"
        "    return 0\n"
    )

    LEAK = (
        "Tracking BE-1234 in Comfy-Org/super-secret-repo, "
        "owned by @Comfy-Org/secret-squad. "
        "Dashboard: https://app.datadoghq.com/dash/1\n"
    )

    def _plant_and_scan(self):
        self.repo.write("LEAK.md", self.LEAK)
        # (1) The historical shape: the checker run straight out of the PR's
        #     own checkout, which is exactly what a PR could rewrite.
        self.repo.write(
            "scripts/check_public_repo_hygiene.py", self.WIDE_OPEN_COPY
        )
        # (2) The same trick at the centralized path, in case a caller tries
        #     to shadow the pinned checkout by vendoring the directory.
        self.repo.write(
            ".github/public-repo-hygiene/check_public_repo_hygiene.py",
            self.WIDE_OPEN_COPY,
        )
        # (3) A config file the checker might have been tempted to read.
        self.repo.write(
            ".public-repo-hygiene.json",
            '{"public_repos": ["super-secret-repo"], '
            '"public_teams": ["secret-squad"]}\n',
        )
        return self.findings()

    def _assert_leak_still_caught(self, findings):
        blob = "\n".join(findings)
        self.assertIn("Comfy-Org/super-secret-repo", blob)
        self.assertIn("@Comfy-Org/secret-squad", blob)
        self.assertIn("BE-1234", blob)
        self.assertIn("collaboration-tool marker", blob)

    def test_in_tree_checker_copy_and_config_are_inert(self):
        self._assert_leak_still_caught(self._plant_and_scan())

    def test_environment_cannot_widen_the_allowlists(self):
        # Nothing about the allowlists is env-driven. Asserting it keeps a
        # future "just make it configurable" from quietly reopening the hole.
        for name in (
            "PUBLIC_COMFY_ORG_REPOS",
            "PUBLIC_COMFY_ORG_TEAMS",
            "TICKET_ALLOWLIST",
            "HYGIENE_PUBLIC_REPOS",
            "EXTRA_PUBLIC_REPOS",
        ):
            os.environ[name] = "super-secret-repo,secret-squad,BE-1234"
            self.addCleanup(os.environ.pop, name, None)
        self._assert_leak_still_caught(self._plant_and_scan())

    def test_caller_inputs_cannot_reach_the_repo_allowlist(self):
        # The two knobs that ARE caller-supplied are additive and scoped: the
        # ticket allowlist takes acronyms, not repo names, and an exclusion
        # drops files from the scan without ever widening a category.
        self.repo.write("LEAK.md", self.LEAK)
        findings = self.findings(
            extra_ticket_allow=["BE-1234"], excludes=["scripts/"]
        )
        blob = "\n".join(findings)
        self.assertIn("Comfy-Org/super-secret-repo", blob)
        self.assertIn("@Comfy-Org/secret-squad", blob)
        self.assertNotIn("possible internal ticket ID", blob)

    def test_allowlists_are_immutable_constants(self):
        # A mutable set would let anything that imports this module mutate the
        # allowlist in place; frozenset makes that a TypeError rather than a
        # silently-widened check.
        for const in (
            checker.PUBLIC_COMFY_ORG_REPOS,
            checker.PUBLIC_COMFY_ORG_TEAMS,
            checker.TICKET_ALLOWLIST,
        ):
            self.assertIsInstance(const, frozenset)

    def test_allowlist_contains_no_obviously_private_shape(self):
        # The allowlist is safe to host in a public repo only because it lists
        # public names exclusively. Nothing enforces that but review -- and
        # this, which at least pins the invariant that it is a flat set of
        # plain repo names, not a mapping carrying private metadata.
        for name in checker.PUBLIC_COMFY_ORG_REPOS | checker.PUBLIC_COMFY_ORG_TEAMS:
            self.assertIsInstance(name, str)
            self.assertRegex(name, r"^[A-Za-z0-9._-]+$")


class OutputTest(CheckerTestCase):
    def test_workflow_command_injection_is_escaped(self):
        raw = "a%b\nc"
        self.assertEqual(checker._esc_cmd(raw), "a%25b%0Ac")

    def test_lone_surrogate_is_made_printable(self):
        # The runnable half of the non-UTF-8-path case that only the Linux
        # runner can build end to end: a path decoded with `surrogateescape`
        # carries lone surrogates, and printing one to a strict-UTF-8 stdout
        # raises UnicodeEncodeError. It must survive escaping AND encoding.
        escaped = checker._esc_cmd("le\udce9k.md")
        self.assertEqual(escaped, "le\\udce9k.md")
        escaped.encode("utf-8")  # would raise before the fix

    def test_exit_codes(self):
        self.repo.write("ok.md", "nothing to see\n")
        self.assertEqual(checker.main(["--root", self.repo.root]), 0)
        self.repo.write("bad.md", "Comfy-Org/nope\n")
        self.assertEqual(checker.main(["--root", self.repo.root]), 1)
        self.assertEqual(
            checker.main(["--root", self.repo.root, "--exclude", "/"]), 2
        )

    def test_multi_value_inputs_are_split(self):
        self.assertEqual(
            checker._split_values(["a/, b/\nc/", "", "  ", "d/"]),
            ["a/", "b/", "c/", "d/"],
        )


if __name__ == "__main__":
    unittest.main()
