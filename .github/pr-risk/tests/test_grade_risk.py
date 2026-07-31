"""Unit tests for the PR risk grader (grade_risk.py)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grade_risk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)


def numstat(*rows):
    """Build a `git diff --numstat -z` stream from (added, deleted, path)."""
    return "".join(f"{a}\t{d}\t{p}\0" for a, d, p in rows)


class ClassifyPathTest(unittest.TestCase):
    def test_docs_are_r0(self):
        for path in ("README.md", "docs/design/thing.md", "a/b/NOTES.txt"):
            self.assertEqual(grade_risk.classify_path(path)[0], grade_risk.R0, path)

    def test_lockfiles_are_r0(self):
        self.assertEqual(grade_risk.classify_path("go.sum")[0], grade_risk.R0)
        self.assertEqual(grade_risk.classify_path("web/pnpm-lock.yaml")[0], grade_risk.R0)

    def test_tests_are_r1(self):
        for path in (
            "services/api/handler_test.go",
            "app/tests/test_thing.py",
            "src/foo.spec.ts",
            "pkg/testdata/golden.json",
        ):
            self.assertEqual(grade_risk.classify_path(path)[0], grade_risk.R1, path)

    def test_ordinary_source_is_r2(self):
        tier, reason = grade_risk.classify_path("services/api/handler.go")
        self.assertEqual(tier, grade_risk.R2)
        self.assertEqual(reason, grade_risk.DEFAULT_REASON)

    def test_sensitive_surfaces_are_r3(self):
        for path in (
            "db/migrations/0007_add_col.sql",
            ".github/workflows/deploy.yml",
            "infra/main.tf",
            "services/auth/session.go",
            "internal/billing/invoice.py",
            "config/secrets.yaml",
        ):
            self.assertEqual(grade_risk.classify_path(path)[0], grade_risk.R3, path)

    def test_test_rules_win_over_sensitive_rules(self):
        """A test for a sensitive surface is still a test — low risk."""
        self.assertEqual(
            grade_risk.classify_path("services/auth/session_test.go")[0], grade_risk.R1
        )

    def test_docs_about_a_sensitive_surface_are_still_docs(self):
        self.assertEqual(grade_risk.classify_path("docs/auth.md")[0], grade_risk.R0)

    def test_top_level_tests_dir_matches(self):
        """`**/tests/**` must match a repo-root `tests/` too, not only nested."""
        self.assertEqual(grade_risk.classify_path("tests/test_x.py")[0], grade_risk.R1)


class ParseNumstatTest(unittest.TestCase):
    def test_basic(self):
        files = grade_risk.parse_numstat(numstat((3, 1, "a.go"), (10, 0, "b/c.md")))
        self.assertEqual([f["path"] for f in files], ["a.go", "b/c.md"])
        self.assertEqual(files[0]["changed"], 4)

    def test_binary_counts_zero_but_is_kept(self):
        files = grade_risk.parse_numstat(numstat(("-", "-", "img/logo.png")))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["binary"])
        self.assertEqual(files[0]["changed"], 0)

    def test_path_with_spaces(self):
        files = grade_risk.parse_numstat(numstat((1, 1, "some dir/my file.go")))
        self.assertEqual(files[0]["path"], "some dir/my file.go")

    def test_rename_keeps_new_path(self):
        # git -z renames emit an empty path field, then old NUL new NUL.
        stream = "2\t2\t\0old/name.go\0new/name.go\0"
        files = grade_risk.parse_numstat(stream)
        self.assertEqual([f["path"] for f in files], ["new/name.go"])

    def test_empty_stream(self):
        self.assertEqual(grade_risk.parse_numstat(""), [])

    def test_malformed_record_is_skipped_not_fatal(self):
        files = grade_risk.parse_numstat("garbage\0" + numstat((1, 0, "a.go")))
        self.assertEqual([f["path"] for f in files], ["a.go"])


class GradeTest(unittest.TestCase):
    def test_tier_is_the_max_of_file_tiers(self):
        report = grade_risk.grade(
            grade_risk.parse_numstat(
                numstat((500, 0, "docs/big.md"), (20, 20, "services/auth/login.go"))
            )
        )
        self.assertEqual(report["tier"], grade_risk.R3)
        self.assertEqual(report["label"], "risk:R3")

    def test_docs_only_pr_is_r0(self):
        report = grade_risk.grade(grade_risk.parse_numstat(numstat((5, 2, "README.md"))))
        self.assertEqual(report["label"], "risk:R0")
        self.assertEqual(report["status"], "graded")

    def test_large_single_file_escalates_that_file(self):
        report = grade_risk.grade(
            grade_risk.parse_numstat(
                numstat((grade_risk.FILE_ESCALATE_LINES + 1, 0, "README.md"))
            )
        )
        self.assertEqual(report["files"][0]["tier"], grade_risk.R1)
        self.assertTrue(report["files"][0]["escalated"])

    def test_whole_pr_size_escalates_one_tier(self):
        # Many small docs files: each stays R0, but the PR total escalates.
        rows = [(40, 0, f"docs/p{i}.md") for i in range(30)]
        report = grade_risk.grade(grade_risk.parse_numstat(numstat(*rows)))
        self.assertGreaterEqual(report["total_lines"], grade_risk.SIZE_ESCALATE_LINES)
        self.assertTrue(report["size_escalated"])
        self.assertEqual(report["tier"], grade_risk.R1)

    def test_escalation_never_exceeds_r3(self):
        rows = [(500, 500, f"services/auth/f{i}.go") for i in range(5)]
        report = grade_risk.grade(grade_risk.parse_numstat(numstat(*rows)))
        self.assertEqual(report["tier"], grade_risk.R3)

    def test_grade_rises_when_the_pr_grows(self):
        """The load-bearing property: re-grading a grown diff moves the tier."""
        before = grade_risk.grade(grade_risk.parse_numstat(numstat((4, 0, "README.md"))))
        after = grade_risk.grade(
            grade_risk.parse_numstat(
                numstat((4, 0, "README.md"), (30, 10, "db/migrations/001.sql"))
            )
        )
        self.assertEqual(before["label"], "risk:R0")
        self.assertEqual(after["label"], "risk:R3")

    def test_empty_diff_is_graded_r0_not_unknown(self):
        report = grade_risk.grade([])
        self.assertEqual(report["status"], "graded")
        self.assertEqual(report["label"], "risk:R0")

    def test_tier_lines_sum_to_total(self):
        report = grade_risk.grade(
            grade_risk.parse_numstat(
                numstat((10, 0, "a.md"), (5, 5, "b.go"), (1, 0, "c/auth/x.go"))
            )
        )
        self.assertEqual(
            sum(report["tier_lines"].values()), report["total_lines"]
        )


class ConcentrationTest(unittest.TestCase):
    def test_names_the_files_that_drive_the_tier(self):
        report = grade_risk.grade(
            grade_risk.parse_numstat(
                numstat(
                    (300, 0, "docs/a.md"),
                    (300, 0, "docs/b.md"),
                    (20, 20, "services/auth/x.go"),
                )
            )
        )
        sentence = grade_risk.concentration_sentence(report)
        self.assertIn("R3", sentence)
        self.assertIn("1 file", sentence)
        self.assertIn("40 lines", sentence)

    def test_uniform_diff_reports_all_at_one_tier(self):
        report = grade_risk.grade(grade_risk.parse_numstat(numstat((10, 0, "a.md"))))
        self.assertIn("All 10 changed lines are R0", grade_risk.concentration_sentence(report))

    def test_zero_line_diff_does_not_divide_by_zero(self):
        report = grade_risk.grade(grade_risk.parse_numstat(numstat(("-", "-", "a.png"))))
        self.assertIn("no counted lines", grade_risk.concentration_sentence(report))


class UnknownTest(unittest.TestCase):
    def test_unknown_report_has_no_tier_and_no_label(self):
        report = grade_risk.unknown_report("git exploded")
        self.assertEqual(report["status"], "unknown")
        self.assertIsNone(report["tier"])
        self.assertIsNone(report["label"])
        self.assertIn("git exploded", report["reason"])

    def test_unknown_comment_says_so_and_never_claims_a_tier(self):
        body = grade_risk.render_comment(grade_risk.unknown_report("boom"), "<!-- m -->")
        self.assertIn("unknown", body.lower())
        self.assertIn("boom", body)
        # The body may only mention risk:R0 to say it was NOT applied. What it
        # must never do is announce a tier the way a graded comment does.
        for tier in range(grade_risk.MAX_TIER + 1):
            self.assertNotIn(f"## Risk: **`risk:R{tier}`**", body)
        self.assertIn("No `risk:*` label was applied", body)

    def test_unknown_check_run_title(self):
        title, summary = grade_risk.render_check(grade_risk.unknown_report("boom"))
        self.assertEqual(title, "Risk: unknown")
        self.assertIn("never fails", summary)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.report = grade_risk.grade(
            grade_risk.parse_numstat(numstat((10, 0, "a.md"), (5, 5, "svc/auth/x.go")))
        )

    def test_comment_starts_with_the_sticky_marker(self):
        body = grade_risk.render_comment(self.report, "<!-- ci-pr-risk -->")
        self.assertTrue(body.startswith("<!-- ci-pr-risk -->"))

    def test_comment_carries_the_unchecked_dispute_box_by_default(self):
        body = grade_risk.render_comment(self.report, "<!-- m -->")
        self.assertIn("- [ ] **This grade is wrong**", body)

    def test_comment_can_render_the_box_checked(self):
        body = grade_risk.render_comment(self.report, "<!-- m -->", disputed=True)
        self.assertIn("- [x] **This grade is wrong**", body)

    def test_comment_has_the_per_file_breakdown(self):
        body = grade_risk.render_comment(self.report, "<!-- m -->")
        self.assertIn("`svc/auth/x.go`", body)
        self.assertIn("Per-file breakdown", body)

    def test_comment_states_nothing_is_gated(self):
        body = grade_risk.render_comment(self.report, "<!-- m -->")
        self.assertIn("never blocks merge", body)

    def test_check_summary_carries_tier_and_reason(self):
        title, summary = grade_risk.render_check(self.report)
        self.assertEqual(title, "Risk: R3")
        self.assertIn("Reason:", summary)

    def test_long_file_list_is_truncated_not_dropped(self):
        rows = [(1, 0, f"src/f{i}.go") for i in range(60)]
        report = grade_risk.grade(grade_risk.parse_numstat(numstat(*rows)))
        body = grade_risk.render_comment(report, "<!-- m -->")
        self.assertIn("and 10 more files", body)


class CliTest(unittest.TestCase):
    def _run(self, stream):
        out = tempfile.mkdtemp()
        proc = subprocess.run(
            [sys.executable, os.path.join(PKG, "grade_risk.py"), "--out-dir", out],
            input=stream,
            capture_output=True,
            text=True,
        )
        return proc, out

    def test_writes_all_three_artifacts_and_exits_zero(self):
        proc, out = self._run(numstat((3, 1, "svc/auth/a.go")))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("risk-report.json", "risk-comment.md", "risk-check.md"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        with open(os.path.join(out, "risk-report.json")) as fh:
            self.assertEqual(json.load(fh)["label"], "risk:R3")

    def test_exits_zero_on_an_empty_diff(self):
        proc, out = self._run("")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(out, "risk-report.json")) as fh:
            self.assertEqual(json.load(fh)["label"], "risk:R0")


class ShellEntrypointTest(unittest.TestCase):
    """End-to-end over a throwaway git repo, exercising grade-risk.sh."""

    def _git(self, cwd, *args):
        subprocess.run(
            ["git", "-C", cwd, *args],
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"},
        )

    def _repo(self):
        d = tempfile.mkdtemp()
        self._git(d, "init", "-q", "-b", "main")
        with open(os.path.join(d, "README.md"), "w") as fh:
            fh.write("hello\n")
        self._git(d, "add", "-A")
        self._git(d, "commit", "-qm", "base")
        return d

    def _sh(self, *args):
        return subprocess.run(
            ["bash", os.path.join(PKG, "grade-risk.sh"), *args],
            capture_output=True,
            text=True,
        )

    def test_grades_a_real_diff(self):
        d = self._repo()
        base = subprocess.run(
            ["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        os.makedirs(os.path.join(d, "svc", "auth"))
        with open(os.path.join(d, "svc", "auth", "login.go"), "w") as fh:
            fh.write("package auth\n")
        self._git(d, "add", "-A")
        self._git(d, "commit", "-qm", "add auth")
        out = tempfile.mkdtemp()
        proc = self._sh("--base", base, "--head", "HEAD", "--out-dir", out, "--repo-dir", d)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(out, "risk-report.json")) as fh:
            report = json.load(fh)
        self.assertEqual(report["label"], "risk:R3")

    def test_unreadable_diff_is_unknown_and_still_exits_zero(self):
        d = self._repo()
        out = tempfile.mkdtemp()
        proc = self._sh(
            "--base", "0" * 40, "--head", "HEAD", "--out-dir", out, "--repo-dir", d
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(out, "risk-report.json")) as fh:
            report = json.load(fh)
        self.assertEqual(report["status"], "unknown")
        self.assertIsNone(report["label"])

    def test_missing_refs_are_unknown_not_a_crash(self):
        out = tempfile.mkdtemp()
        proc = self._sh("--out-dir", out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(out, "risk-report.json")) as fh:
            self.assertEqual(json.load(fh)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
