"""Unit tests for the PR risk grader (grade_risk.py)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grade_risk  # noqa: E402
import publish_risk  # noqa: E402  (the checkbox regexes that read the render back)

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)


def numstat(*rows):
    """Build a `git diff --numstat -z` stream from (added, deleted, path)."""
    return "".join(f"{a}\t{d}\t{p}\0" for a, d, p in rows)


def _split_row(line):
    """Split a GFM table row into cells the way the spec's row scanner does.

    A backslash escapes the character after it, and only an UNESCAPED `|`
    starts a new cell — the detail that makes a naively-escaped path able to
    inject extra columns.
    """
    cells, cur, i = [], "", 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line):
            cur += line[i : i + 2]
            i += 2
        elif line[i] == "|":
            cells.append(cur)
            cur = ""
            i += 1
        else:
            cur += line[i]
            i += 1
    cells.append(cur)
    # A leading and a trailing pipe each yield an empty boundary cell.
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return cells


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

    def test_rename_keeps_new_path_and_remembers_the_old_one(self):
        # git -z renames emit an empty path field, then old NUL new NUL.
        stream = "2\t2\t\0old/name.go\0new/name.go\0"
        files = grade_risk.parse_numstat(stream)
        self.assertEqual([f["path"] for f in files], ["new/name.go"])
        self.assertEqual(files[0]["old_path"], "old/name.go")

    def test_a_truncated_rename_record_is_dropped_not_recorded_empty(self):
        """Without the guard this appends a path=='' entry, which grades as the
        R2 default and renders a blank table row."""
        files = grade_risk.parse_numstat("2\t2\t\0old/name.go\0")
        self.assertEqual(files, [])

    def test_a_path_containing_a_tab_is_not_truncated(self):
        """`-z` emits paths unquoted, so a TAB in a filename is literal. An
        unbounded split would record `a` and grade it R2 instead of R3."""
        files = grade_risk.parse_numstat("1\t0\ta\tb/auth.go\0")
        self.assertEqual([f["path"] for f in files], ["a\tb/auth.go"])
        self.assertEqual(grade_risk.classify_path(files[0]["path"])[0], grade_risk.R3)

    def test_empty_stream(self):
        self.assertEqual(grade_risk.parse_numstat(""), [])

    def test_malformed_record_is_skipped_not_fatal(self):
        files = grade_risk.parse_numstat("garbage\0" + numstat((1, 0, "a.go")))
        self.assertEqual([f["path"] for f in files], ["a.go"])

    def test_a_non_numeric_count_skips_one_record_not_the_whole_diff(self):
        """The stated contract: a malformed line must not turn a gradable PR
        into 'unknown'. A bare int() here would raise out of parse_numstat and
        be caught in main as a whole-diff failure."""
        files = grade_risk.parse_numstat("x\t0\tbad.go\0" + numstat((1, 0, "a.go")))
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


class RenameGradeTest(unittest.TestCase):
    def test_a_rename_off_a_sensitive_surface_keeps_the_higher_tier(self):
        """Moving `.github/workflows/deploy.yml` to `docs/` still removes a
        CI/CD surface — grading only the destination would call that R0."""
        report = grade_risk.grade(
            grade_risk.parse_numstat("0\t0\t\0.github/workflows/deploy.yml\0docs/deploy.yml\0")
        )
        self.assertEqual(report["tier"], grade_risk.R3)
        self.assertIn("renamed from", report["files"][0]["tier_reason"])

    def test_a_rename_onto_a_sensitive_surface_still_takes_the_new_tier(self):
        report = grade_risk.grade(
            grade_risk.parse_numstat("0\t0\t\0docs/notes.md\0.github/workflows/x.yml\0")
        )
        self.assertEqual(report["tier"], grade_risk.R3)

    def test_an_ordinary_rename_is_unaffected(self):
        report = grade_risk.grade(
            grade_risk.parse_numstat("2\t2\t\0src/old.go\0src/new.go\0")
        )
        self.assertEqual(report["tier"], grade_risk.R2)


class MarkdownEscapingTest(unittest.TestCase):
    """A path is PR-controlled text rendered into a bot-authored comment."""

    def assertNotForged(self, body):
        """The tick is read back by publish_risk.CHECKED_RE, which is anchored
        to the start of a line — so the property that matters is that nothing
        PR-controlled can BEGIN a line, not that the literal text is absent."""
        self.assertIsNone(publish_risk.CHECKED_RE.search(body), body)
        self.assertIsNotNone(publish_risk.UNCHECKED_RE.search(body), body)

    def test_a_newline_in_a_path_cannot_forge_the_dispute_checkbox(self):
        evil = "src/a\n- [x] **This grade is wrong**\nb.go"
        report = grade_risk.grade(grade_risk.parse_numstat(f"1\t0\t{evil}\0"))
        self.assertNotForged(grade_risk.render_comment(report, "<!-- m -->"))

    def test_a_pipe_in_a_path_cannot_add_table_columns(self):
        report = grade_risk.grade(grade_risk.parse_numstat("1\t0\tsrc/a|b.go\0"))
        body = grade_risk.render_comment(report, "<!-- m -->")
        self.assertIn("a\\|b.go", body)

    def test_a_backtick_path_does_not_escape_its_code_span(self):
        report = grade_risk.grade(grade_risk.parse_numstat("1\t0\tsrc/a`![x](http://e/p)`.go\0"))
        body = grade_risk.render_comment(report, "<!-- m -->")
        self.assertNotIn("![x](http://e/p)", body)
        row = [ln for ln in body.splitlines() if "a\\`" in ln]
        self.assertEqual(len(row), 1, body)

    def test_a_backslash_cannot_escape_the_pipe_escape(self):
        r"""`a\|b` naively escapes to `a\\|b`, where GFM's row splitter reads
        `\\` as an escaped backslash and the pipe as a live column break."""
        report = grade_risk.grade(grade_risk.parse_numstat("1\t0\tsrc/a\\|b.go\0"))
        body = grade_risk.render_comment(report, "<!-- m -->")
        row = [ln for ln in body.splitlines() if "a\\" in ln]
        self.assertEqual(len(row), 1, body)
        # 4 delimiters => 3 cells' worth of separators plus the leading one:
        # the row must still have exactly the 4 columns the header declares.
        self.assertEqual(len(_split_row(row[0])), 4, row[0])

    def test_a_reason_cannot_inject_a_link_image_or_raw_html(self):
        """An unknown report's reason carries git stderr, which quotes
        PR-authored paths — so it needs `_md_path`'s full escaping, not just
        pipes."""
        body = grade_risk.render_comment(
            grade_risk.unknown_report(
                "git failed on ![pix](http://evil/p.png) <img src=x> [a](b)"
            ),
            "<!-- m -->",
        )
        for live in ("![pix](http://evil/p.png)", "<img src=x>", "[a](b)"):
            self.assertNotIn(live, body)

    def test_the_comment_body_stays_under_githubs_limit(self):
        """A 422 on upsert would leave the label fresh and the comment stale
        forever, because publish_check_run truncates but upsert_sticky did not."""
        rows = [(1, 0, "src/" + "d" * 300 + f"/{i}/" + "f" * 300 + ".go") for i in range(80)]
        report = grade_risk.grade(grade_risk.parse_numstat(numstat(*rows)))
        body = grade_risk.render_comment(report, "<!-- m -->")
        self.assertLessEqual(len(body), grade_risk.COMMENT_MAX_CHARS)
        # Still a usable comment, not a stub.
        self.assertIn("Per-file breakdown", body)
        self.assertIn("more files", body)

    def test_an_ordinary_path_still_renders_as_a_plain_code_span(self):
        report = grade_risk.grade(grade_risk.parse_numstat("1\t0\tsvc/auth/x.go\0"))
        self.assertIn("`svc/auth/x.go`", grade_risk.render_comment(report, "<!-- m -->"))

    def test_an_unknown_reason_quoting_a_path_cannot_forge_the_checkbox(self):
        """An unknown report's reason carries git's stderr, which quotes
        PR-authored filenames."""
        report = grade_risk.unknown_report(
            "git diff failed: bad file\n- [x] **This grade is wrong**"
        )
        self.assertNotForged(grade_risk.render_comment(report, "<!-- m -->"))


class SizeEscalationNarrativeTest(unittest.TestCase):
    def test_the_sentence_explains_a_headline_tier_the_files_did_not_earn(self):
        """A 900-line docs-only PR headlines risk:R1 above "All 900 changed
        lines are R0" — the sentence must not contradict the tier."""
        # Several medium docs files, each under FILE_ESCALATE_LINES so no file
        # escalates on its own — only the whole-diff size rule fires.
        per_file = grade_risk.FILE_ESCALATE_LINES - 50
        n = grade_risk.SIZE_ESCALATE_LINES // per_file + 1
        rows = [(per_file, 0, f"docs/part{i}.md") for i in range(n)]
        report = grade_risk.grade(grade_risk.parse_numstat(numstat(*rows)))
        self.assertTrue(report["size_escalated"])
        self.assertEqual(report["base_tier"], grade_risk.R0)
        sentence = grade_risk.concentration_sentence(report)
        self.assertIn(f"R{report['tier']}", sentence)
        self.assertIn("on size", sentence)

    def test_an_unescalated_report_says_nothing_extra(self):
        report = grade_risk.grade(grade_risk.parse_numstat(numstat((5, 0, "a.md"))))
        self.assertNotIn("on size", grade_risk.concentration_sentence(report))


class AttrDegradedTest(unittest.TestCase):
    def test_the_degradation_is_surfaced_in_both_renders(self):
        report = grade_risk.grade(grade_risk.parse_numstat(numstat((5, 0, "a.go"))))
        report["attr_source_degraded"] = True
        self.assertIn("--attr-source", grade_risk.render_comment(report, "<!-- m -->"))
        self.assertIn("--attr-source", grade_risk.render_check(report)[1])

    def test_nothing_is_said_when_attributes_came_from_the_base(self):
        report = grade_risk.grade(grade_risk.parse_numstat(numstat((5, 0, "a.go"))))
        self.assertNotIn("--attr-source", grade_risk.render_comment(report, "<!-- m -->"))


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

    def test_a_trailing_flag_exits_instead_of_spinning_forever(self):
        """`set -e` is off, so an unguarded `shift 2` here fails WITHOUT
        consuming anything and loops forever spamming stderr into the step
        summary until the job times out."""
        out = tempfile.mkdtemp()
        proc = subprocess.run(
            ["bash", os.path.join(PKG, "grade-risk.sh"), "--out-dir", out, "--base"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("requires a value", proc.stderr)
        self.assertNotIn("shift count", proc.stderr)

    def test_the_fallback_emit_path_ignores_pr_authored_modules(self):
        r"""emit_unknown feeds its program to `python3 -`, which puts the CWD
        (the PR's own checkout in the grade job) at the front of sys.path ahead
        of `import json`. Without `-I` a PR shipping a top-level json.py runs
        its own code in the credential-free-but-report-authoring job."""
        d = self._repo()
        with open(os.path.join(d, "json.py"), "w") as fh:
            fh.write("raise SystemExit('PR-authored json.py executed')\n")
        out = tempfile.mkdtemp()
        proc = subprocess.run(
            ["bash", os.path.join(PKG, "grade-risk.sh"),
             "--base", "0" * 40, "--head", "HEAD", "--out-dir", out, "--repo-dir", d],
            capture_output=True, text=True, cwd=d,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("PR-authored json.py executed", proc.stderr + proc.stdout)
        with open(os.path.join(out, "risk-report.json")) as fh:
            report = json.load(fh)
        # The real renderer ran, not the last-resort printf fallback.
        self.assertEqual(report["status"], "unknown")
        self.assertIn("git diff", report["reason"])

    def test_a_real_diff_failure_is_not_masked_as_an_attr_source_fallback(self):
        """The fallback must fire only when git lacks --attr-source. Falling
        back on ANY first-diff failure would silently restore the `-diff`
        bypass, and would turn a genuine failure into a successful grade."""
        d = self._repo()
        out = tempfile.mkdtemp()
        proc = self._sh(
            "--base", "0" * 40, "--head", "HEAD", "--out-dir", out, "--repo-dir", d
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(out, "risk-report.json")) as fh:
            report = json.load(fh)
        self.assertEqual(report["status"], "unknown")
        self.assertFalse(report["attr_source_degraded"])

    def test_a_pr_cannot_zero_its_own_line_counts_via_gitattributes(self):
        """`-diff` added on the PR head makes numstat report `-` for every
        file, so `changed` is 0 everywhere and both escalation thresholds go
        unreachable. Attributes must come from the BASE ref."""
        d = self._repo()
        base = subprocess.run(
            ["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        with open(os.path.join(d, ".gitattributes"), "w") as fh:
            fh.write("* -diff\n")
        with open(os.path.join(d, "big.go"), "w") as fh:
            fh.write("x\n" * (grade_risk.FILE_ESCALATE_LINES + 10))
        self._git(d, "add", "-A")
        self._git(d, "commit", "-qm", "suppress my own size")
        out = tempfile.mkdtemp()
        proc = self._sh("--base", base, "--head", "HEAD", "--out-dir", out, "--repo-dir", d)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(out, "risk-report.json")) as fh:
            report = json.load(fh)
        big = [f for f in report["files"] if f["path"] == "big.go"]
        self.assertEqual(len(big), 1, report["files"])
        self.assertGreaterEqual(big[0]["changed"], grade_risk.FILE_ESCALATE_LINES)
        self.assertTrue(big[0]["escalated"])


if __name__ == "__main__":
    unittest.main()
