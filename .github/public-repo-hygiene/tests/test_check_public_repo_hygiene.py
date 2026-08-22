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

import codecs
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

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
        return self.run_checks(**kwargs).findings

    def warnings(self, **kwargs):
        return self.run_checks(**kwargs).warnings


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

    def test_public_identifier_namespaces_clear_by_prefix(self):
        # `\b[A-Z]{2,6}-\d{2,6}\b` matches `CVE-2021` INSIDE `CVE-2021-44228`
        # (the `\b` holds against the following hyphen), so a SECURITY.md or a
        # dependency changelog reddened what adopters wire in as a REQUIRED
        # check. An exact-token carve-out would cost one entry per year prefix
        # and break again each January, so the namespace is allowlisted instead.
        self.repo.write(
            "SECURITY.md",
            "Patched CVE-2021-44228 (see also CVE-2024-3094).\n"
            "Classified as CWE-89; the API follows PEP-484 and RFC-9110,\n"
            "with ISO-19115 metadata written as UTF-16 or UTF-32.\n",
        )
        self.assertEqual(self.findings(), [])

    def test_an_allowlisted_prefix_does_not_clear_a_longer_acronym(self):
        # The carve-out is the namespace token itself, not any acronym starting
        # with those letters -- `CVEX-1234` is still ticket-shaped.
        self.repo.write("README.md", "See CVEX-1234 and ISOP-99 for context.\n")
        findings = self.findings()
        self.assertEqual(len(findings), 2, findings)

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
                    for f in checker.run_checks(repo.root).findings
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

    def test_sentence_final_period_is_stripped(self):
        # BE-8697, the change this test was flipped by: the repo-name class
        # includes `.`, so prose ending "...built on Comfy-Org/ComfyUI." used
        # to be flagged as `ComfyUI.`. Both per-repo scripts did that and this
        # checker carried it over on purpose, because the migration's proof was
        # findings-parity against them -- a proof that ended when that
        # migration merged. A GitHub slug can never end in `.`, so a trailing
        # one is always sentence punctuation.
        self.repo.write("README.md", "Built on Comfy-Org/ComfyUI.\n")
        self.assertEqual(self.findings(), [])

    def test_lowercase_org_segment_is_still_flagged(self):
        # The bypass BE-8697 closes: GitHub resolves owner names
        # case-insensitively, so `comfy-org/x` reaches the same repo as
        # `Comfy-Org/x` and default-deny has to see both spellings.
        self.repo.write("README.md", "See comfy-org/some-internal-thing.\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("not in the known-public allowlist", findings[0])
        self.assertIn("some-internal-thing", findings[0])

    def test_org_segment_casings_of_public_repos_pass(self):
        # Widening the match must not turn public references into findings.
        self.repo.write(
            "README.md",
            "comfy-org/comfyui and COMFY-ORG/ComfyUI and CoMfY-oRg/comfy-cli\n",
        )
        self.assertEqual(self.findings(), [])

    def test_repo_allowlist_membership_is_case_insensitive(self):
        # `ComfyUI` and `Comfy-Desktop` are stored in their canonical GitHub
        # spelling; a differently-cased reference resolves to the same public
        # repo, so it is not a leak.
        self.repo.write(
            "README.md",
            "Comfy-Org/comfyui Comfy-Org/comfy-desktop Comfy-Org/COMFYUI_FRONTEND\n",
        )
        self.assertEqual(self.findings(), [])

    def test_near_miss_repo_name_is_still_flagged(self):
        # Casefolding admits no name that is not in the allowlist -- only
        # other CASINGS of names that are.
        self.repo.write("README.md", "Comfy-Org/ComfyUI2\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Comfy-Org/ComfyUI2", findings[0])

    def test_team_handle_casing_and_sentence_final_period(self):
        # The empirically-confirmed false positive: CODEOWNERS spells the
        # handle `@Comfy-Org/Comfy-Cloud-Team` while the allowlist stores the
        # slug, and prose puts a period after it. The period strip runs BEFORE
        # the team/repo fork, so the team branch gets it too -- the `.git`
        # strip further down is repo-branch-only and never reaches here.
        self.repo.write(
            "docs/owners.md",
            "Owned by @Comfy-Org/Comfy-Cloud-Team.\n"
            "Also @Comfy-Org/Core-Engine-Team and @comfy-org/comfy-cloud-team.\n",
        )
        self.assertEqual(self.findings(), [])

    def test_unknown_team_with_lowercase_org_is_flagged(self):
        self.repo.write(".github/CODEOWNERS", "docs/ @comfy-org/secret-squad\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/secret-squad", findings[0])
        self.assertIn("a team not in the known-public allowlist", findings[0])

    def test_bare_org_slash_period_is_not_a_finding(self):
        # `Comfy-Org/.` names no repo at all once the sentence punctuation is
        # stripped, so there is nothing to report and nothing to crash on.
        self.repo.write("README.md", "Everything lives under Comfy-Org/.\n")
        self.assertEqual(self.findings(), [])

    def test_trailing_git_suffix_survives_the_period_strip(self):
        # The `.git` strip runs on the already-period-stripped name, so a URL
        # at the end of a sentence still resolves to the public repo.
        self.repo.write(
            "docs/install.md",
            "Clone https://github.com/Comfy-Org/comfy-typescript-sdk.git.\n",
        )
        self.assertEqual(self.findings(), [])

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

    def test_npm_scope_of_a_public_repo_is_not_a_team_finding(self):
        # `@comfy-org/<pkg>` is an npm / GitHub Packages scope, not a CODEOWNERS
        # handle, and those coordinates are REQUIRED to be lowercase. Before the
        # org segment matched case-insensitively this spelling did not match at
        # all; once it did, it landed in the `@` branch and a dependency on a
        # known-PUBLIC repo was reported as "a team not in the known-public
        # allowlist" -- with no caller-side escape short of `exclude_paths:`,
        # since neither allowlist is a workflow input. The `@` branch therefore
        # admits the repo allowlist too, for a spelling that could BE an npm
        # coordinate.
        self.repo.write(
            "package.json",
            '{"dependencies": {"@comfy-org/comfy-typescript-sdk": "^1.0.0"}}\n',
        )
        self.assertEqual(self.findings(), [])

    def test_the_npm_crossing_does_not_clear_a_github_team_spelling(self):
        # Naming a team after the repo it owns is the commonest CODEOWNERS
        # convention there is, so an unconditional crossing cleared exactly the
        # likely collision: a team handle waved through because a public repo
        # shares its name. npm scopes cannot be spelled with a capital, so the
        # canonical GitHub team casing does not reach the crossing and
        # default-deny holds for it.
        self.repo.write("CODEOWNERS", "* @Comfy-Org/ComfyUI_frontend\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/ComfyUI_frontend", findings[0])
        self.assertIn("team not in the known-public allowlist", findings[0])

    def test_at_prefixed_name_on_neither_allowlist_is_still_flagged(self):
        # Admitting the repo allowlist widens the `@` branch by exactly the
        # public repo names and nothing else -- default-deny still holds.
        self.repo.write("package.json", '{"x": "@comfy-org/secret-pkg"}\n')
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/secret-pkg", findings[0])

    def test_trailing_git_suffix_is_stripped_case_insensitively(self):
        # The `.git` strip has to be as case-insensitive as the match above it
        # and the membership test below it, or `ComfyUI.GIT` keeps its suffix,
        # casefolds to `comfyui.git` and is reported as a leak for a repo the
        # allowlist already covers.
        self.repo.write(
            "docs/install.md",
            "Clone Comfy-Org/ComfyUI.GIT or Comfy-Org/comfy-cli.Git\n",
        )
        self.assertEqual(self.findings(), [])

    def test_case_insensitive_git_strip_does_not_admit_a_private_name(self):
        self.repo.write("README.md", "Comfy-Org/some-internal-thing.GIT\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Comfy-Org/some-internal-thing", findings[0])

    def test_org_slash_dotgit_names_no_repo(self):
        # `Comfy-Org/.git` clears the period strip untouched (it has no TRAILING
        # dot) and is then consumed WHOLE by the `.git` strip, so the empty
        # check has to run after that strip too -- otherwise this yields the
        # repo-less "reference to Comfy-Org/" that names nothing actionable.
        self.repo.write("README.md", "The bare remote is Comfy-Org/.git\n")
        self.assertEqual(self.findings(), [])

    def test_name_class_stays_ascii_under_the_scoped_ignorecase(self):
        # `re.IGNORECASE` applied to the WHOLE pattern also widens `[A-Za-z]`:
        # under Unicode case-folding it matches U+017F and U+212A, so
        # `.casefold()` at the membership test would fold `comfy-typeſcript-sdk`
        # back onto an allowlisted name -- a default-deny bypass. Scoping the
        # flag to the org segment keeps the CAPTURE ASCII; the characters it
        # cannot read are then handled as a partly-read name (below) rather than
        # as a name boundary.
        self.repo.write("README.md", "Comfy-Org/comfy-typeſcript-sdk\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("homoglyph", findings[0])

    def test_a_name_the_ascii_class_only_partly_read_is_never_cleared(self):
        # The capture class has no right boundary, so the match stops at the
        # first non-ASCII character -- and the allowlist would then be tested
        # against a PREFIX of what the file actually says. U+2010 HYPHEN renders
        # identically to `-` on github.com, so `Comfy-Org/comfyui‐internal`
        # captured `comfyui`, casefolded into the known-public set and passed
        # clean while the full private name sat in the tree.
        self.repo.write("README.md", "See Comfy-Org/comfyui‐internal\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        # The finding quotes the WHOLE name, not the prefix that was read.
        self.assertIn("comfyui‐internal", findings[0])
        self.assertIn("homoglyph", findings[0])
        # "Add it to the allowlist" is not the remedy for a homoglyph, so the
        # ordinary message would send the reader the wrong way.
        self.assertNotIn("confirm it's public and add it", findings[0])

    def test_a_kelvin_sign_after_a_public_name_is_a_finding_not_a_pass(self):
        # U+212A casefolds to `k`, so absorbing it silently would clear a name
        # that is not the allowlisted one. It is a letter, so it continues the
        # name, so the reference is only partly read and is never cleared.
        self.repo.write("other.md", "Built on Comfy-Org/comfyuiK\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("homoglyph", findings[0])

    def test_a_cyrillic_homoglyph_is_caught_like_the_dash_one(self):
        # The rule is every non-ASCII name character, not just the ones that
        # casefold onto ASCII: Cyrillic `а` folds to nothing ASCII, so narrowing
        # the rule to fold-collisions would leave the capture stopping at it and
        # `comfyui` clearing on its own -- the same bypass, one alphabet over.
        self.repo.write("README.md", "Comfy-Org/comfyui\u0430internal\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("homoglyph", findings[0])

    def test_prose_butted_against_an_allowlisted_name_is_a_known_cost(self):
        # The flip side of the rule above, asserted so it is a DECISION rather
        # than a surprise: non-Latin prose immediately after an allowlisted name
        # (no separator) reads as part of the name and is reported. The message
        # names the remedy, and a separator clears it.
        self.repo.write("ja.md", "Comfy-Org/ComfyUIを使う\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("adjacent PROSE", findings[0])

        self.repo.write("ja.md", "Comfy-Org/ComfyUI を使う\n")
        self.assertEqual(self.findings(), [])

    def test_the_partly_read_rule_covers_the_team_branch_too(self):
        self.repo.write("CODEOWNERS", "* @Comfy-Org/core-engine‐team\n")
        findings = self.findings()
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("@Comfy-Org/core-engine‐team", findings[0])

    def test_ordinary_prose_after_a_public_name_is_not_a_partial_read(self):
        # Only characters that CONTINUE a name (letters, digits, marks, dash and
        # connector punctuation) mean the class stopped mid-token. A curly
        # apostrophe, an em dash and an ellipsis end it -- otherwise every
        # changelog written with smart quotes would produce homoglyph findings
        # for repos that are on the allowlist.
        self.repo.write(
            "README.md",
            "Comfy-Org/ComfyUI’s frontend — see Comfy-Org/comfy-cli…\n",
        )
        self.assertEqual(self.findings(), [])

    def test_the_org_segment_must_start_a_token(self):
        # Without a left boundary the org segment matched inside a longer word,
        # so `NotComfy-Org/private` was reported as a reference to THIS org.
        self.repo.write("README.md", "NotComfy-Org/whatever is a different org\n")
        self.assertEqual(self.findings(), [])

        # Every real spelling begins at a separator, and all of them still match.
        self.repo.write(
            "urls.md",
            "https://github.com/Comfy-Org/secret-one\n"
            "see Comfy-Org/secret-two\n"
            '"Comfy-Org/secret-three"\n',
        )
        self.assertEqual(len(self.findings()), 3, self.findings())

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
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.skipped, [("binary", 1)])

    def test_undecodable_text_is_skipped(self):
        self.repo.write("latin.txt", b"BE-1234 caf\xe9\n", track=True)
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.skipped, [("non-UTF-8", 1)])

    def test_directory_exclusion_prunes_subtree_and_is_counted(self):
        self.repo.write("src/generated/a.py", "BE-1234\n")
        self.repo.write("src/generated/deep/b.py", "BE-5678\n")
        self.repo.write("src/hand.py", "BE-9999\n")
        result = self.run_checks(excludes=["src/generated/"])
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("src/hand.py", result.findings[0])
        self.assertEqual(result.exclusions, [("src/generated/", 2)])

    def test_file_exclusion_is_exact(self):
        self.repo.write("scripts/check.py", "BE-1234\n")
        self.repo.write("scripts/check.py.bak", "BE-5678\n")
        result = self.run_checks(excludes=["scripts/check.py"])
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("scripts/check.py.bak", result.findings[0])
        self.assertEqual(result.exclusions, [("scripts/check.py", 1)])

    def test_exclusion_that_matches_nothing_is_still_reported(self):
        # A typo'd exclusion has to be visible in the log. Reporting only the
        # ones that fired is how a repo ends up believing it excluded a tree
        # it never named correctly.
        self.repo.write("a.md", "hello\n")
        self.assertEqual(
            self.run_checks(excludes=["typo/"]).exclusions, [("typo/", 0)]
        )

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

    def test_a_root_that_does_not_exist_says_so_rather_than_blaming_git(self):
        # subprocess.run(cwd=...) raises FileNotFoundError both when `git` is
        # missing and when the cwd does not exist, so an unqualified handler
        # sends the operator hunting for a git that is right there (BE-8654
        # review).
        missing = os.path.join(self.repo.root, "no-such-dir")
        with self.assertRaises(checker.ConfigError) as caught:
            checker.run_checks(missing)
        self.assertIn("not an existing directory", str(caught.exception))
        self.assertNotIn("git is not available", str(caught.exception))

    def test_a_root_naming_a_file_is_a_config_error_not_a_traceback(self):
        # NotADirectoryError escaping uncaught exits 1 -- the code the workflow
        # reads as "internal-only references found" rather than the exit 2 every
        # other unusable-configuration path returns.
        not_a_dir = self.repo.write("plain.md", "clean\n")
        with self.assertRaises(checker.ConfigError) as caught:
            checker.run_checks(not_a_dir)
        self.assertIn("cannot run git in", str(caught.exception))


class CoverageReportingTest(CheckerTestCase):
    """Nothing the checker skips may be invisible in the log (BE-8654).

    A guard that silently declines to scan something is worse than no guard,
    because the green run reads as coverage. Each skip route therefore has to
    leave a trace: an exclusion leaves a count, an unreadable file leaves a
    warning, and a run that scanned nothing at all says so.
    """

    def test_unreadable_file_is_named_rather_than_disappearing(self):
        # A tracked path the checker cannot read at all -- here a directory
        # that git tracks a file under, made unreadable. Unlike binary/
        # non-UTF-8 this is not an expected skip, so it names the file.
        self.repo.write("locked/secret.md", "clean\n")
        self.repo.write("ok.md", "clean\n")
        locked = os.path.join(self.repo.root, "locked")
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o755)
        if os.access(os.path.join(locked, "secret.md"), os.R_OK):
            # root ignores the mode bits, and CI containers often run as root.
            raise unittest.SkipTest("permissions are not enforced for this user")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(len(result.warnings), 1, result.warnings)
        self.assertIn("locked/secret.md", result.warnings[0])
        self.assertIn("NOT scanned", result.warnings[0])
        self.assertEqual(result.skipped, [("unreadable", 1)])

    def test_symlink_target_string_is_scanned_but_never_followed(self):
        # `open()` follows a symlink, so scanning one would read whatever it
        # points AT rather than the target string git stores in the blob: a
        # link out of the repo would pull arbitrary runner content into a
        # PUBLIC run log, and one to /dev/zero or a FIFO would hang or OOM the
        # job. But the TARGET STRING is what this repo publishes, so skipping
        # the entry outright discarded the one thing worth checking (BE-8654
        # review): `os.readlink` reads it without opening anything.
        outside = os.path.join(tempfile.mkdtemp(), "outside.md")
        self.addCleanup(os.remove, outside)
        with open(outside, "w") as fh:
            fh.write("Comfy-Org/super-secret-repo\n")
        os.symlink(outside, os.path.join(self.repo.root, "link.md"))
        # ...and the dangling case, which cannot be opened at all -- yet its
        # target string is still tracked, still published, and here it leaks.
        os.symlink(
            "../Comfy-Org/private-thing/BE-4242.md",
            os.path.join(self.repo.root, "dangling"),
        )
        self.repo._git("add", "--", "link.md", "dangling")
        self.repo.write("ok.md", "clean\n")

        result = self.run_checks()
        # The content on the far side of the link is NOT reported -- it is not
        # this repo's, and echoing it would be the leak, not the guard.
        self.assertFalse(
            any("super-secret-repo" in f for f in result.findings),
            result.findings,
        )
        # The target STRING is, on both the resolvable and the dangling link.
        self.assertEqual(len(result.findings), 2, result.findings)
        self.assertTrue(
            any("Comfy-Org/private-thing" in f for f in result.findings),
            result.findings,
        )
        self.assertTrue(
            any("BE-4242" in f for f in result.findings), result.findings
        )
        # Reading the target string IS coverage, so these count as scanned --
        # but each still names itself, because the file body was not read.
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.scanned, 3)
        link_warnings = [w for w in result.warnings if "symlink" in w]
        self.assertEqual(len(link_warnings), 2, result.warnings)
        for w in link_warnings:
            self.assertIn("TARGET STRING", w)

    def test_a_fifo_is_still_skipped_outright(self):
        # A symlink has a target string worth scanning; a device node or FIFO
        # has nothing this repo stores, and reading one blocks. git cannot
        # track a FIFO directly, so reach the case the way reality would: a
        # tracked regular file that IS one by the time the scan lstats it.
        fifo = self.repo.write("pipe", "clean\n")
        os.remove(fifo)
        os.mkfifo(fifo)
        self.addCleanup(os.remove, fifo)
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.skipped, [("not a regular file", 1)])
        self.assertEqual(result.scanned, 1)
        self.assertEqual(len(result.warnings), 1, result.warnings)
        self.assertIn("not a regular file", result.warnings[0])

    def test_oversized_file_is_truncated_loudly_not_dropped(self):
        # An unbounded read is a runner-memory DoS a PR author controls. Most
        # of a large file is still worth scanning, so the cap truncates and
        # names the unread tail rather than skipping the file.
        with unittest.mock.patch.object(checker, "MAX_FILE_BYTES", 64):
            self.repo.write("big.md", "Comfy-Org/nope\n" + "x" * 200 + "\nBE-1234\n")
            result = self.run_checks()
        # The part within the cap is still checked...
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("Comfy-Org/nope", result.findings[0])
        # ...and the part beyond it is a named warning, not a silent drop.
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.skipped, [])
        self.assertEqual(len(result.warnings), 1, result.warnings)
        self.assertIn("big.md", result.warnings[0])
        self.assertIn("only the first 64", result.warnings[0])

    def test_truncation_landing_mid_codepoint_still_scans_the_head(self):
        # The cap can split a multi-byte character. Reporting the whole file as
        # non-UTF-8 would turn a size limit into a detection hole.
        with unittest.mock.patch.object(checker, "MAX_FILE_BYTES", 20):
            self.repo.write(
                "wide.md", ("Comfy-Org/nope\n" + "é" * 40).encode()
            )
            result = self.run_checks()
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertEqual(result.skipped, [])

    def test_a_bad_byte_far_from_the_cap_is_non_utf8_not_a_short_scan(self):
        # The mid-codepoint fallback must be GATED on the error landing at the
        # truncation boundary. `data[:exc.start]` is valid UTF-8 by
        # construction, so an ungated fallback silently "succeeds" for a
        # genuinely non-UTF-8 file: commit filler past the cap, one bad byte
        # near the TOP, then the internal references, and the file is scanned
        # only up to that byte, still counted as scanned, and reported under a
        # warning claiming the whole cap was read (BE-8654 review).
        with unittest.mock.patch.object(checker, "MAX_FILE_BYTES", 64):
            self.repo.write(
                "sneaky.md",
                b"ok\n\xff\nComfy-Org/nope\nBE-1234\n" + b"x" * 200,
                track=True,
            )
            self.repo.write("ok.md", "clean\n")
            result = self.run_checks()
        # Not scanned-to-the-bad-byte-and-called-covered: a real skip, counted.
        self.assertEqual(result.findings, [])
        self.assertEqual(result.skipped, [("non-UTF-8", 1)])
        self.assertEqual(result.scanned, 1)
        # And no warning that overstates what was read.
        self.assertFalse(
            any("only the first" in w for w in result.warnings), result.warnings
        )

    def test_a_committed_utf16_blob_is_decoded_rather_than_called_binary(self):
        # No gitattribute is involved here: the NUL bytes are in what git
        # STORES, so `_work_tree_encoded` never sees this file, and the `binary`
        # skip would drop it from the scan entirely -- a per-run count that
        # never touches the verdict -- while GitHub still renders the blob as
        # readable text. Every one of these encodings is self-describing via a
        # BOM, so the decode is exact rather than guessed.
        for name, encoding in (
            ("le.md", "utf-16-le"),
            ("be.md", "utf-16-be"),
            ("u32.md", "utf-32-le"),
        ):
            with self.subTest(encoding=encoding):
                repo = RepoFixture()
                self.addCleanup(repo.cleanup)
                bom = {
                    "utf-16-le": codecs.BOM_UTF16_LE,
                    "utf-16-be": codecs.BOM_UTF16_BE,
                    "utf-32-le": codecs.BOM_UTF32_LE,
                }[encoding]
                repo.write(
                    name,
                    bom + "See Comfy-Org/definitely-not-public\n".encode(encoding),
                )
                result = checker.run_checks(repo.root)
                self.assertEqual(result.skipped, [], result.skipped)
                self.assertEqual(result.scanned, 1)
                self.assertEqual(len(result.findings), 1, result.findings)

    def test_a_utf8_bom_does_not_hide_a_reference_on_the_first_line(self):
        # UTF-8 is not in the BOM table -- these bytes decode down the ordinary
        # path, leaving a U+FEFF at the head of line 1. That must not swallow a
        # reference sitting immediately behind it: U+FEFF is not a word
        # character, so neither the ticket pattern's `\b` nor the repo pattern's
        # left boundary is affected.
        self.repo.write(
            "bom.md", codecs.BOM_UTF8 + b"Comfy-Org/definitely-not-public BE-1234\n"
        )
        result = self.run_checks()
        self.assertEqual(result.scanned, 1)
        self.assertEqual(len(result.findings), 2, result.findings)

    def test_a_bom_over_bytes_that_do_not_decode_is_still_not_scanned(self):
        # Failing OPEN here would be worse than the binary skip it replaces: a
        # mislabelled BOM must not become "scanned, found nothing".
        self.repo.write("lie.md", codecs.BOM_UTF32_LE + b"\xff\xff\xff")
        result = self.run_checks()
        self.assertEqual(result.skipped, [("non-UTF-8", 1)])
        self.assertEqual(result.scanned, 0)

    def test_a_git_lfs_pointer_stub_is_named_as_a_coverage_hole(self):
        # `actions/checkout` leaves LFS off, so an LFS-tracked file is present
        # only as its pointer stub. Reading the stub and counting the file as
        # covered is a silent hole -- and one a PR could use deliberately.
        self.repo.write(
            "asset.bin",
            checker.LFS_POINTER_PREFIX
            + "\noid sha256:" + "a" * 64 + "\nsize 12345\n",
        )
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        # The stub parses as text, but none of the file's actual bytes were
        # read, so it must NOT prop up the coverage claim: only `ok.md` counts.
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.skipped, [("git-LFS pointer", 1)])
        lfs = [w for w in result.warnings if "git-LFS" in w]
        self.assertEqual(len(lfs), 1, result.warnings)
        self.assertIn("asset.bin", lfs[0])

    def test_a_file_only_pretending_to_be_an_lfs_stub_is_still_scanned(self):
        # Classifying on the first line alone made the skip an OPT-OUT any
        # tracked file could take: type the magic line, and everything below it
        # went unread while the file still rendered as plain text on github.com
        # -- and with one other clean file present the zero-scan net did not
        # fire either. A genuine stub has to satisfy the whole pointer grammar.
        self.repo.write(
            "notes.md",
            checker.LFS_POINTER_PREFIX
            + "\nSee Comfy-Org/definitely-not-public and BE-1234\n",
        )
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(len(result.findings), 2, result.findings)
        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.skipped, [])

    def test_an_oversized_pointer_lookalike_is_not_a_stub_either(self):
        # A real stub is ~130 bytes. The size ceiling stops a file that carries
        # a valid-looking header ABOVE a payload from riding the classification.
        self.repo.write(
            "big.md",
            checker.LFS_POINTER_PREFIX
            + "\noid sha256:" + "c" * 64 + "\nsize 1\n"
            + "# padding Comfy-Org/definitely-not-public\n"
            + "x" * checker.LFS_POINTER_MAX_BYTES + "\n",
        )
        result = self.run_checks()
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.scanned, 1)
        self.assertEqual(len(result.findings), 1, result.findings)

    def test_a_genuine_stub_is_read_even_though_it_does_not_count_as_scanned(self):
        # Coverage and detection are separate questions, and this is the one
        # file kind where they diverge: the stub is not the file (so it cannot
        # prop up `scanned`), but it IS text this run has in hand, so it is
        # still checked. A real stub is hex and digits, so it yields nothing --
        # which is exactly why scanning it is free.
        self.repo.write(
            "asset.bin",
            checker.LFS_POINTER_PREFIX
            + "\noid sha256:" + "d" * 64 + "\nsize 7\n",
        )
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.skipped, [("git-LFS pointer", 1)])

    def test_an_all_lfs_repo_cannot_hold_the_zero_scan_net_open(self):
        # The reason the stub had to move out of `scanned`: `git lfs track
        # '*.md'` plus a commit carrying internal references would otherwise
        # exit 0 on a required status check, having read no content at all.
        self.repo.write(
            "doc.md",
            checker.LFS_POINTER_PREFIX
            + "\noid sha256:" + "b" * 64 + "\nsize 99\n",
        )
        result = self.run_checks()
        self.assertEqual(result.scanned, 0)
        self.assertEqual(result.skipped, [("git-LFS pointer", 1)])
        self.assertTrue(
            any("no files were scanned" in w for w in result.warnings),
            result.warnings,
        )
        self.assertEqual(checker._emit(result), 2)

    def test_a_submodule_gitlink_is_named_as_one_not_as_a_device_node(self):
        # `git ls-files` lists gitlinks, and the reusable workflow checks the
        # caller out without `submodules:` -- so this is the commonest way a
        # tracked path is not a regular file, and reporting it as a FIFO or
        # device node is the wrong type AND the wrong reason.
        self.repo.write("ok.md", "clean\n")
        sub = os.path.join(self.repo.root, "vendor", "lib")
        os.makedirs(sub)
        subprocess.run(["git", "init", "-q"], cwd=sub, check=True,
                       capture_output=True)
        for key, value in (("user.email", "t@example.invalid"),
                           ("user.name", "t")):
            subprocess.run(["git", "config", key, value], cwd=sub, check=True,
                           capture_output=True)
        with open(os.path.join(sub, "f.txt"), "w") as fh:
            fh.write("x\n")
        subprocess.run(["git", "add", "-A"], cwd=sub, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "s"], cwd=sub, check=True,
                       capture_output=True)
        env = dict(os.environ, GIT_ALLOW_PROTOCOL="file")
        subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "add",
             "-q", "./vendor/lib", "vendor/lib"],
            cwd=self.repo.root, check=True, capture_output=True, env=env,
        )
        result = self.run_checks()
        gitlink = [w for w in result.warnings if "vendor/lib:" in w]
        self.assertEqual(len(gitlink), 1, result.warnings)
        self.assertIn("submodule gitlink", gitlink[0])
        self.assertNotIn("device node", gitlink[0])
        self.assertIn(("submodule gitlink", 1), result.skipped)

    def test_a_work_tree_encoding_attribute_is_a_hard_failure(self):
        # The blob git stores stays UTF-8 and plainly readable on GitHub, while
        # checkout writes NUL-laden UTF-16 to disk -- which this scan, which
        # reads the WORK TREE, skips as binary. A green run over content the
        # guard never looked at, from a two-line commit.
        self.repo.write(".gitattributes", "*.md working-tree-encoding=UTF-16\n")
        # Written as the work tree would actually hold it -- git validates the
        # round-trip on `add`, and the blob it stores from these bytes is UTF-8
        # with the reference in plain view.
        self.repo.write(
            "leak.md", "See Comfy-Org/definitely-not-public\n".encode("utf-16")
        )
        with self.assertRaises(checker.ConfigError) as ctx:
            self.run_checks()
        self.assertIn("working-tree-encoding", str(ctx.exception))
        self.assertIn("leak.md", str(ctx.exception))

    def test_an_excluded_path_may_carry_a_work_tree_encoding_attribute(self):
        # The conversion only hides something this run was going to read. On an
        # excluded path the hole is already named in the log by its exclusion
        # count, so failing there would be a false alarm with no way to clear it.
        # `UTF-16` fits TICKET_RE, so the fixture's own `.gitattributes` would
        # once have been a category-1 finding in its own right; the `UTF`
        # namespace now clears by prefix (TICKET_ALLOWED_PREFIXES), so the
        # canonical spelling is safe to write here.
        self.repo.write(
            ".gitattributes", "vendor/* working-tree-encoding=UTF-16\n"
        )
        self.repo.write("vendor/blob.md", "clean\n".encode("utf-16"))
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks(excludes=["vendor/"])
        self.assertEqual(result.findings, [])
        self.assertEqual(result.scanned, 2)

    def test_a_mangled_work_tree_gitattributes_cannot_hide_the_conversion(self):
        # Resolved from the WORK TREE, the guard reads the same `.gitattributes`
        # a conversion may itself have mangled: a commit that applies
        # `working-tree-encoding=UTF-16` to the attributes file ALONGSIDE the
        # leaking file leaves an unparseable attributes file on disk, every path
        # comes back `unspecified`, and the guard fails open over exactly the
        # commit it exists to catch. The property being asserted is about the
        # bytes git STORES, so the question goes to the index (`--cached`).
        self.repo.write(".gitattributes", "*.md working-tree-encoding=UTF-16\n")
        self.repo.write(
            "leak.md", "See Comfy-Org/definitely-not-public\n".encode("utf-16")
        )
        # Mangle only the work-tree copy; the INDEX still holds the readable
        # rule, which is what checkout acted on.
        self.repo.write(
            ".gitattributes",
            "*.md working-tree-encoding=UTF-16\n".encode("utf-16"),
            track=False,
        )
        with self.assertRaises(checker.ConfigError) as ctx:
            self.run_checks()
        self.assertIn("leak.md", str(ctx.exception))

    def test_a_utf8_working_tree_encoding_is_not_a_conversion(self):
        # UTF-8 is the identity mapping git skips re-encoding for, so it hides
        # nothing -- and an exit 2 the caller can only clear by excluding paths
        # costs real coverage.
        self.repo.write(".gitattributes", "*.md working-tree-encoding=UTF-8\n")
        self.repo.write("doc.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.scanned, 2)

    def test_the_utf8_exemption_does_not_cover_the_bom_variants(self):
        # `UTF-8-BOM` and `UTF-8BOM` DO rewrite the bytes, so they stay fatal.
        # Asserted against the resolver rather than a fixture because git
        # refuses to stage a file whose declared BOM variant it cannot
        # round-trip, so the case cannot be built as a repo.
        for value in ("UTF-8-BOM", "utf-8bom", "UTF-16"):
            with self.subTest(value=value):
                out = "\0".join(["a.md", "working-tree-encoding", value]) + "\0"
                with unittest.mock.patch.object(
                    checker.subprocess,
                    "run",
                    return_value=unittest.mock.Mock(stdout=out.encode()),
                ):
                    self.repo.write("a.md", "clean\n")
                    self.assertEqual(
                        checker._work_tree_encoded(self.repo.root, ["a.md"]),
                        ["a.md"],
                    )

    def test_an_encoding_rule_matching_a_symlink_is_not_a_conversion(self):
        # `git check-attr` answers purely by PATH PATTERN, so a rule like
        # `*.bin working-tree-encoding=UTF-16` also "converts" a tracked symlink
        # named `notes.bin` -- an entry checkout writes with no encoding step at
        # all. Only a regular file can actually be re-encoded.
        self.repo.write(".gitattributes", "*.bin working-tree-encoding=UTF-16\n")
        os.symlink("target.txt", os.path.join(self.repo.root, "notes.bin"))
        self.repo._git("add", "--", "notes.bin")
        result = self.run_checks()
        self.assertEqual(result.findings, [])
        self.assertEqual(result.scanned, 2)

    def test_per_file_warnings_are_capped_without_losing_the_accounting(self):
        # `warnings` embeds a path per entry and has six producers; uncapped, a
        # tree of tens of thousands of tracked symlinks floods a PUBLIC run log
        # -- the same derived-output problem the finding caps close, through a
        # door they do not cover.
        self.repo.write("ok.md", "clean\n")
        count = checker.MAX_WARNINGS_TOTAL + 5
        for i in range(count):
            link = os.path.join(self.repo.root, f"link{i}")
            os.symlink("target.txt", link)
            self.repo._git("add", "--", f"link{i}")
        result = self.run_checks()
        self.assertEqual(len(result.warnings), checker.MAX_WARNINGS_TOTAL + 1)
        self.assertIn("+5 more per-file warning(s)", result.warnings[-1])
        # Coverage accounting is NOT capped: every symlink is still counted.
        self.assertEqual(result.scanned, count + 1)

    def test_a_buried_truncation_warning_still_leaves_a_coverage_count(self):
        # `scanned`/`skipped` cannot express "read, but not all of it": a file
        # truncated at the read cap counts as `scanned` with no skipped kind, so
        # before `partial` the per-file `::warning::` was the ONLY record that
        # coverage was partial -- and that warning is subject to the cap above.
        # Enough cheap tracked symlinks sorting ahead of a large file therefore
        # buried the one line saying its tail was never read, and the report
        # then claimed full coverage over a file it had only partly read.
        for i in range(checker.MAX_WARNINGS_TOTAL):
            os.symlink("target.txt", os.path.join(self.repo.root, f"aaa{i:04d}"))
            self.repo._git("add", "--", f"aaa{i:04d}")
        with unittest.mock.patch.object(checker, "MAX_FILE_BYTES", 64):
            self.repo.write("zz-big.md", "clean\n" + "x" * 200 + "\n")
            result = self.run_checks()
        # The warning naming the file lost its budget to the symlinks...
        self.assertFalse(
            [w for w in result.warnings if "zz-big.md" in w], result.warnings
        )
        # ...but the coverage arithmetic survives it.
        self.assertEqual(result.partial, [(checker.PARTIAL_READ, 1)])

    def test_a_capped_findings_file_is_counted_as_partial_too(self):
        # The per-FILE findings cap is the other producer with no count of its
        # own: the file is fully read but only partly REPORTED.
        with unittest.mock.patch.object(checker, "MAX_FINDINGS_PER_FILE", 3):
            self.repo.write("many.md", "Comfy-Org/nope\n" * 10)
            result = self.run_checks()
        self.assertEqual(len(result.findings), 3, result.findings)
        self.assertEqual(result.partial, [(checker.PARTIAL_FINDINGS, 1)])

    def test_a_clean_run_reports_no_partial_coverage(self):
        self.repo.write("ok.md", "clean\n")
        self.assertEqual(self.run_checks().partial, [])

    def test_findings_are_capped_per_file_without_softening_the_verdict(self):
        # The read cap bounds bytes READ; nothing bounded what was DERIVED from
        # them. One matching line copied once per matching pattern is how a
        # 5 MiB file becomes tens of MiB of retained strings and a flooded
        # PUBLIC run log (BE-8654 review).
        with unittest.mock.patch.object(checker, "MAX_FINDINGS_PER_FILE", 5):
            self.repo.write("many.md", "Comfy-Org/nope\n" * 50)
            result = self.run_checks()
        self.assertEqual(len(result.findings), 5, result.findings)
        capped = [w for w in result.warnings if "more than 5 findings" in w]
        self.assertEqual(len(capped), 1, result.warnings)
        self.assertIn("many.md", capped[0])
        # Capping is an enumeration limit, never an amnesty.
        self.assertIn("still FAILS", capped[0])
        self.assertEqual(checker._emit(result), 1)

    def test_findings_are_capped_per_run_too(self):
        with unittest.mock.patch.object(checker, "MAX_FINDINGS_TOTAL", 3):
            self.repo.write("a.md", "Comfy-Org/nope\n" * 5)
            self.repo.write("b.md", "Comfy-Org/nope\n" * 5)
            result = self.run_checks()
        self.assertEqual(len(result.findings), 3, result.findings)
        self.assertTrue(
            any("across the repo" in w for w in result.warnings), result.warnings
        )
        # Coverage accounting stays COMPLETE past the cap -- only the
        # enumeration stops.
        self.assertEqual(result.scanned, 2)
        self.assertEqual(checker._emit(result), 1)

    def test_a_matched_line_is_echoed_only_as_a_bounded_excerpt(self):
        # Category-2 findings echo the whole matched line, which is
        # attacker-controlled and can be the entire file.
        self.repo.write("huge.md", "https://notion.so/" + "z" * 5000 + "\n")
        result = self.run_checks()
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("line truncated", result.findings[0])
        self.assertLess(len(result.findings[0]), 400, result.findings[0])

    def test_an_unbounded_repo_name_match_is_bounded_in_the_finding(self):
        # REPO_REF_RE's name class has no length bound, so `Comfy-Org/` plus
        # 5 MiB of word characters is ONE match whose text is the whole file --
        # the excerpt bound on whole LINES does not cover it.
        self.repo.write("wide.md", "Comfy-Org/" + "a" * 5000 + "\n")
        result = self.run_checks()
        self.assertEqual(len(result.findings), 1, result.findings)
        self.assertIn("truncated", result.findings[0])
        self.assertLess(len(result.findings[0]), 600, len(result.findings[0]))

    def test_binary_and_non_utf8_skips_are_counted_not_warned(self):
        # These are ordinary, expected skips, so warning on each would bury the
        # unreadable case in noise -- but silent is not an option either: one
        # stray byte appended to a document hides the whole file while it still
        # renders as text on GitHub. They get a per-run count.
        self.repo.write("blob.bin", b"\x00\xff", track=True)
        self.repo.write("latin.txt", b"caf\xe9\n", track=True)
        self.repo.write("ok.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.skipped, [("binary", 1), ("non-UTF-8", 1)])
        self.assertEqual(result.scanned, 1)

    def test_scanning_nothing_is_never_reported_as_clean(self):
        self.repo.write("only.md", "clean\n")
        result = self.run_checks(excludes=["only.md"])
        self.assertEqual(result.findings, [])
        self.assertEqual(result.scanned, 0)
        self.assertTrue(
            any("no files were scanned" in w for w in result.warnings),
            result.warnings,
        )

    def test_a_repo_whose_files_are_all_unscannable_is_not_clean(self):
        # The zero-scan net has to count files the READER declined too, not
        # just excluded ones -- otherwise a tree of binaries reports "scanned"
        # coverage it never had.
        self.repo.write("blob.bin", b"\x00\xff", track=True)
        result = self.run_checks()
        self.assertEqual(result.scanned, 0)
        self.assertTrue(
            any("no files were scanned" in w for w in result.warnings),
            result.warnings,
        )

    def test_empty_repo_warns_too(self):
        self.assertTrue(
            any("no files were scanned" in w for w in self.warnings())
        )

    def test_tracked_content_at_the_reserved_path_is_a_hard_failure(self):
        # The reusable workflow checks THIS repo out at `_public_repo_hygiene/`
        # inside the caller's workspace, so tracked content there is shadowed
        # by that checkout and can never be examined. Skipping it quietly would
        # have made the reserved path a parking spot for internal references
        # that still ships green; it is exit 2 instead.
        self.repo.write("_public_repo_hygiene/x.py", "BE-1234 Comfy-Org/private\n")
        self.repo.write("real.md", "clean\n")
        with self.assertRaises(checker.ConfigError) as ctx:
            self.run_checks()
        self.assertIn("_public_repo_hygiene/", str(ctx.exception))
        self.assertIn("RESERVED", str(ctx.exception))
        self.assertEqual(
            checker.main(["--root", self.repo.root]), 2
        )

    def test_the_reserved_path_costs_nothing_when_it_is_absent(self):
        # The ordinary case: the workflow's checkout lands UNTRACKED there, so
        # `git ls-files` never lists it and the run says nothing about it.
        self.repo.write("real.md", "clean\n")
        result = self.run_checks()
        self.assertEqual(result.exclusions, [])
        self.assertEqual(result.skipped, [])

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
            # The casefolded views are what membership actually consults
            # (BE-8697), so they carry the same invariant as the lists they
            # derive from -- a mutable one would be the same hole one hop down.
            checker._PUBLIC_REPOS_CF,
            checker._PUBLIC_TEAMS_CF,
        ):
            self.assertIsInstance(const, frozenset)

    def test_casefolded_views_cover_their_source_lists(self):
        # The views are derived at import so they cannot drift, but pin it:
        # an entry that vanished under casefold (or a hand-written second copy
        # of the list) would silently un-allowlist a public name.
        self.assertEqual(
            checker._PUBLIC_REPOS_CF,
            {n.casefold() for n in checker.PUBLIC_COMFY_ORG_REPOS},
        )
        self.assertEqual(
            checker._PUBLIC_TEAMS_CF,
            {n.casefold() for n in checker.PUBLIC_COMFY_ORG_TEAMS},
        )
        # And no two canonical entries collide under casefold, which would mean
        # the human-edited list carries a duplicate spelling.
        self.assertEqual(
            len(checker._PUBLIC_REPOS_CF), len(checker.PUBLIC_COMFY_ORG_REPOS)
        )
        self.assertEqual(
            len(checker._PUBLIC_TEAMS_CF), len(checker.PUBLIC_COMFY_ORG_TEAMS)
        )

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

    def test_a_scan_that_read_nothing_exits_2_not_0(self):
        # Enumerating every top-level directory in `exclude_paths` disables the
        # whole scan without ever naming the repo root, so the root-exclusion
        # rejection alone would be one spelling away from pointless.
        self.repo.write("docs/only.md", "clean\n")
        self.assertEqual(
            checker.main(["--root", self.repo.root, "--exclude", "docs/"]), 2
        )

    def test_multi_value_inputs_are_split(self):
        self.assertEqual(
            checker._split_values(["a/, b/\nc/", "", "  ", "d/"]),
            ["a/", "b/", "c/", "d/"],
        )


if __name__ == "__main__":
    unittest.main()
