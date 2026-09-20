#!/usr/bin/env python3
"""Regression tests for the incremental "hunks new since last round" block (BE-15558).

The prompt tells the panel this block "is the subset of the diff above". Built
as `git diff LAST_REVIEWED...HEAD` it was not: with a merge commit at HEAD, LAST
is an ancestor of HEAD, the merge base of the two IS LAST, and the range carries
every commit the merge pulled in from the base branch. The property pinned here
is the one that was missing — **the block only ever contains hunks the PR itself
carries** — plus the behaviours that must survive the rewrite: a non-merge round
still shows only what changed since the last round, a pure rebase shows nothing,
and a file dropped from the PR contributes nothing.

`TestMergeCommitHead` is the real repro, run against an actual git repository
with a real merge commit, so it fails against the old commit-range formulation
rather than only against a hand-written fixture.

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSETS = os.path.join(_HERE, "..")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ASSETS, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inc = _load("incremental_diff", "incremental-diff.py")


def _patch(path, hunk_header, body, index="1111111..2222222 100644"):
    """One file section of a unified diff, in git's own shape."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index {index}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"{hunk_header}\n"
        f"{body}"
    )


# --------------------------------------------------------------------------- #
# 1. The headline case: a merge commit at HEAD                                 #
# --------------------------------------------------------------------------- #


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _rev(repo, ref):
    return _git(repo, "rev-parse", ref).strip()


def _write(repo, name, text):
    with open(os.path.join(repo, name), "w", encoding="utf-8") as fh:
        fh.write(text)


class TestMergeCommitHead(unittest.TestCase):
    """A real repo, a real merge of the base branch into the PR branch."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:  # pragma: no cover - CI always has git
            raise unittest.SkipTest("git not available")
        cls.repo = tempfile.mkdtemp(prefix="inc-diff-repo-")
        repo = cls.repo
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "Test")
        _write(repo, "app.py", "one\ntwo\nthree\n")
        _write(repo, "unrelated.py", "base\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        cls.fork_point = _rev(repo, "HEAD")

        # The PR branch: round 1 touches app.py only.
        _git(repo, "checkout", "-q", "-b", "pr")
        _write(repo, "app.py", "one\nTWO\nthree\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "pr round 1")
        cls.last_reviewed = _rev(repo, "HEAD")

        # Meanwhile main moves — a file the PR never touches.
        _git(repo, "checkout", "-q", "main")
        _write(repo, "unrelated.py", "base\nmain moved on\nand on\nand on\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "main advances")
        cls.base_sha = _rev(repo, "HEAD")

        # Round 2: the author merges main into the branch and edits app.py.
        _git(repo, "checkout", "-q", "pr")
        _git(repo, "merge", "-q", "--no-ff", "-m", "merge main", "main")
        _write(repo, "app.py", "one\nTWO\nTHREE\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "pr round 2")
        cls.head_sha = _rev(repo, "HEAD")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def _diff(self, a, b):
        return _git(self.repo, "-c", "core.quotePath=false", "diff", f"{a}...{b}", "--", ".")

    def test_old_formulation_pulled_in_base_branch_files(self):
        """The bug, pinned: the commit range carries main's own commits."""
        stale = self._diff(self.last_reviewed, self.head_sha)
        self.assertIn("unrelated.py", stale)

    def test_block_contains_only_pr_files(self):
        old = self._diff(self.base_sha, self.last_reviewed)
        new = self._diff(self.base_sha, self.head_sha)
        block = inc.build(old, new)
        self.assertIn("app.py", block)
        self.assertNotIn("unrelated.py", block)
        self.assertIn("+THREE", block)

    def test_block_passes_the_subset_fail_safe(self):
        old = self._diff(self.base_sha, self.last_reviewed)
        new = self._diff(self.base_sha, self.head_sha)
        block = inc.build(old, new)
        foreign, new_lines, full_lines = inc.check(block, new)
        self.assertEqual(foreign, 0)
        self.assertLessEqual(new_lines, full_lines)

    def test_old_formulation_fails_the_subset_fail_safe(self):
        """The fail-safe would have caught the shipped bug on its own."""
        stale = self._diff(self.last_reviewed, self.head_sha)
        reviewed = self._diff(self.base_sha, self.head_sha)
        foreign, _new_lines, _full_lines = inc.check(stale, reviewed)
        self.assertGreater(foreign, 0)

    def test_unchanged_file_is_not_re_emitted_after_a_merge(self):
        """A merge that brings in no PR-file change yields an empty block."""
        merge_only = _rev(self.repo, f"{self.head_sha}^")
        old = self._diff(self.base_sha, self.last_reviewed)
        new = self._diff(self.base_sha, merge_only)
        self.assertEqual(inc.build(old, new), "")


# --------------------------------------------------------------------------- #
# 2. The behaviours the rewrite must preserve                                  #
# --------------------------------------------------------------------------- #


class TestBuild(unittest.TestCase):
    def test_non_merge_head_shows_only_what_changed_since_last_round(self):
        touched = _patch("a.py", "@@ -1,2 +1,3 @@", " one\n+two\n+three\n")
        untouched = _patch("b.py", "@@ -1 +1,2 @@", " x\n+y\n")
        old = _patch("a.py", "@@ -1,2 +1,2 @@", " one\n+two\n") + untouched
        new = touched + untouched
        block = inc.build(old, new)
        self.assertIn("a/a.py", block)
        self.assertNotIn("a/b.py", block)

    def test_file_dropped_since_last_round_contributes_nothing(self):
        gone = _patch("gone.py", "@@ -1 +1 @@", "-x\n+y\n")
        kept = _patch("kept.py", "@@ -1 +1 @@", "-p\n+q\n")
        self.assertEqual(inc.build(gone + kept, kept), "")

    def test_pure_rebase_yields_an_empty_block(self):
        """Identical hunks at shifted line numbers, over a moved base blob."""
        old = _patch("a.py", "@@ -10,3 +10,4 @@ def f():", " x\n+new\n y\n", index="aaaaaaa..bbbbbbb 100644")
        new = _patch("a.py", "@@ -84,3 +85,4 @@ def f():", " x\n+new\n y\n", index="ccccccc..ddddddd 100644")
        self.assertEqual(inc.build(old, new), "")

    def test_a_real_edit_at_a_shifted_line_still_shows(self):
        old = _patch("a.py", "@@ -10,3 +10,4 @@", " x\n+new\n y\n")
        new = _patch("a.py", "@@ -84,3 +85,4 @@", " x\n+newer\n y\n")
        self.assertIn("+newer", inc.build(old, new))

    def test_a_file_new_this_round_is_emitted_whole(self):
        old = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        added = (
            "diff --git a/n.py b/n.py\n"
            "new file mode 100644\n"
            "index 0000000..3333333\n"
            "--- /dev/null\n"
            "+++ b/n.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+hello\n"
            "+world\n"
        )
        block = inc.build(old, old + added)
        self.assertEqual(block, added)

    def test_emitted_sections_are_byte_for_byte_slices_of_new(self):
        new = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        self.assertIn(inc.build("", new), new)

    def test_empty_new_yields_an_empty_block(self):
        self.assertEqual(inc.build(_patch("a.py", "@@ -1 +1 @@", "-x\n+y\n"), ""), "")

    def test_a_binary_file_changed_since_last_round_is_emitted(self):
        """One `git diff` invocation emits ONE binary format, not two.

        Without `--binary` — which this workflow does not pass — a changed
        binary is a header, an `index`, and the CONSTANT line
        `Binary files a/X and b/X differ`. So `index` is the section's only
        content-dependent line, and excluding it (right for a text file, whose
        blob id moves on every rebase) silently dropped every binary change.
        The fixture this replaced pitted a `Binary files ... differ` stanza
        against a `GIT binary patch` one — two formats a single `git diff` run
        never mixes — so it passed while the gap was open.
        """
        old = (
            "diff --git a/i.png b/i.png\n"
            "index 1111111..2222222 100644\n"
            "Binary files a/i.png and b/i.png differ\n"
        )
        new = old.replace("..2222222", "..4444444")
        self.assertEqual(inc.build(old, new), new)

    def test_an_unchanged_binary_file_is_not_re_emitted(self):
        same = (
            "diff --git a/i.png b/i.png\n"
            "index 1111111..2222222 100644\n"
            "Binary files a/i.png and b/i.png differ\n"
        )
        self.assertEqual(inc.build(same, same), "")

    def test_a_mode_change_on_an_already_edited_file_is_emitted(self):
        """`+x` on a script the PR already edits.

        The mode lines sit BEFORE the first `@@`, so a signature that began at
        the first `@@` compared the two rounds equal and dropped the section —
        losing exactly the small, high-signal privilege change the block exists
        to surface. Only the mode-ONLY case (no hunks at all) used to survive.
        """
        old = _patch("s.sh", "@@ -1,2 +1,2 @@", "-a\n+b\n")
        new = old.replace(
            "diff --git a/s.sh b/s.sh\n",
            "diff --git a/s.sh b/s.sh\nold mode 100644\nnew mode 100755\n",
        )
        self.assertEqual(inc.build(old, new), new)

    def test_a_moved_base_blob_still_yields_no_block(self):
        """Folding pre-hunk metadata in must not undo the rebase-quiet property.

        `index` and the similarity percentage track the BASE blob, so they move
        on a rebase the author had no part in; they stay out of the signature.
        """
        old = _patch("a.py", "@@ -1,3 +1,3 @@", "-x\n+y\n")
        new = _patch(
            "a.py", "@@ -41,3 +41,3 @@", "-x\n+y\n", index="9999999..8888888 100644"
        )
        self.assertEqual(inc.build(old, new), "")

    def test_a_mode_only_change_new_this_round_is_emitted(self):
        new = (
            "diff --git a/s.sh b/s.sh\n"
            "old mode 100644\n"
            "new mode 100755\n"
        )
        self.assertEqual(inc.build("", new), new)


# --------------------------------------------------------------------------- #
# 3. Header parsing — the identity the whole comparison keys on                #
# --------------------------------------------------------------------------- #


class TestParsePaths(unittest.TestCase):
    def test_plain_path(self):
        self.assertEqual(inc.parse_paths("diff --git a/x.py b/x.py\n"), ("x.py", "x.py"))

    def test_path_with_spaces(self):
        self.assertEqual(
            inc.parse_paths("diff --git a/my file.txt b/my file.txt\n"),
            ("my file.txt", "my file.txt"),
        )

    def test_path_containing_the_separator(self):
        self.assertEqual(
            inc.parse_paths("diff --git a/x b/y.txt b/x b/y.txt\n"),
            ("x b/y.txt", "x b/y.txt"),
        )

    def test_rename(self):
        self.assertEqual(inc.parse_paths("diff --git a/o.py b/n.py\n"), ("o.py", "n.py"))

    def test_unparseable_header_yields_nothing(self):
        self.assertEqual(inc.parse_paths("diff --git nonsense\n"), ())

    def test_a_section_with_an_unparseable_header_is_still_compared(self):
        weird = "diff --git nonsense\n@@ -1 +1 @@\n-x\n+y\n"
        self.assertEqual(inc.build(weird, weird), "")


# --------------------------------------------------------------------------- #
# 4. The fail-safe                                                             #
# --------------------------------------------------------------------------- #


class TestCheck(unittest.TestCase):
    def test_a_subset_passes(self):
        a = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        b = _patch("b.py", "@@ -1 +1 @@", "-p\n+q\n")
        self.assertEqual(inc.check(a, a + b), (0, a.count("\n"), (a + b).count("\n")))

    def test_a_foreign_file_is_counted(self):
        a = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        foreign = _patch("elsewhere.py", "@@ -1 +1 @@", "-p\n+q\n")
        count, _n, _f = inc.check(a + foreign, a)
        self.assertEqual(count, 1)

    def test_an_empty_block_is_a_subset(self):
        self.assertEqual(inc.check("", _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n"))[0], 0)

    def test_a_rename_section_copied_verbatim_is_not_foreign(self):
        """A rename names two paths; copied verbatim it is still a subset."""
        full = (
            "diff --git a/o.py b/n.py\n"
            "similarity index 90%\n"
            "rename from o.py\n"
            "rename to n.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/o.py\n"
            "+++ b/n.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        self.assertEqual(inc.check(full, full)[0], 0)

    def test_a_fabricated_hunk_under_a_carried_path_is_foreign(self):
        """The property the README asserts, checked directly.

        Counting only path names waved this through: `a.py` IS in the reviewed
        diff, so a section carrying a hunk the PR never wrote passed as a
        verified subset. The bytes are what the panel reads, so the bytes are
        what the fail-safe compares.
        """
        full = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        forged = _patch("a.py", "@@ -1 +1 @@", "-x\n+subprocess.run(EXFIL)\n")
        self.assertEqual(inc.check(forged, full)[0], 1)

    def test_a_duplicated_section_is_foreign_on_its_second_copy(self):
        """Sections are matched as a multiset: the diff carries this one once."""
        a = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        self.assertEqual(inc.check(a + a, a)[0], 1)

    def test_a_reordered_block_is_still_a_subset(self):
        """Order is not part of the property — verbatim content is."""
        a = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        b = _patch("b.py", "@@ -1 +1 @@", "-p\n+q\n")
        self.assertEqual(inc.check(b + a, a + b)[0], 0)

    def test_what_build_emits_always_passes(self):
        """build copies sections out of NEW, so check can only trip on a
        builder bug — which is the whole reason it runs."""
        old = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        new = (
            _patch("a.py", "@@ -1 +1 @@", "-x\n+z\n")
            + _patch("b.py", "@@ -1 +1 @@", "-p\n+q\n")
        )
        self.assertEqual(inc.check(inc.build(old, new), new)[0], 0)

    def test_line_counts_match_wc_l(self):
        text = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        self.assertEqual(inc.check(text, text)[1], 4)


class TestLineSplitting(unittest.TestCase):
    """A git patch is LF-delimited. `str.splitlines` is not.

    It also breaks on a lone `\r`, `\v`, `\f`, `\x1c`-`\x1e`, `\x85`, U+2028
    and U+2029 — none of which git treats as a line break, and all of which a
    PR can put in a content line. Splitting on them let the diff's own payload
    forge a `diff --git` section boundary.
    """

    _FORGED = "diff --git a/lib/auth.py b/lib/auth.py"

    def test_a_form_feed_in_content_cannot_forge_a_section(self):
        text = (
            "diff --git a/n.txt b/n.txt\n"
            "index 1111111..2222222 100644\n"
            "--- a/n.txt\n"
            "+++ b/n.txt\n"
            "@@ -0,0 +1 @@\n"
            f"+x\x0c{self._FORGED}\n"
        )
        self.assertEqual(
            [header for header, _lines in inc.split_sections(text)],
            ["diff --git a/n.txt b/n.txt\n"],
        )

    def test_the_forged_path_is_not_learned_by_the_fail_safe(self):
        """The same bad split taught `check` the forged path, so a block
        carrying it did not count as foreign — the guard disarming itself."""
        full = (
            "diff --git a/n.txt b/n.txt\n"
            "@@ -0,0 +1 @@\n"
            f"+x\x0c{self._FORGED}\n"
        )
        block = f"{self._FORGED}\n@@ -1 +1 @@\n-secret\n+leaked\n"
        self.assertEqual(inc.check(block, full)[0], 1)

    def test_no_other_unicode_break_splits_a_section(self):
        for sep in ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(sep=repr(sep)):
                text = (
                    "diff --git a/a.py b/a.py\n"
                    "@@ -1 +1 @@\n"
                    f"+z{sep}diff --git a/forged b/forged\n"
                )
                self.assertEqual(len(inc.split_sections(text)), 1)

    def test_a_patch_with_no_trailing_newline_keeps_its_last_line(self):
        text = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y"
        _header, lines = inc.split_sections(text)[0]
        self.assertEqual("".join(lines), text)


class TestSectionPaths(unittest.TestCase):
    """`rename from`/`rename to` carry ONE path per line, so they settle the
    header ambiguity `parse_paths` can only guess at."""

    _AMBIGUOUS = (
        "diff --git a/x b/c b/d\n"
        "similarity index 90%\n"
        "rename from x b/c\n"
        "rename to d\n"
        "index 1111111..2222222 100644\n"
        "--- a/x b/c\n"
        "+++ b/d\n"
        "@@ -1 +1 @@\n-p\n+q\n"
    )

    def test_the_header_alone_takes_the_wrong_reading(self):
        """`a/x b/c b/d` splits two ways and the header cannot say which."""
        self.assertEqual(inc.parse_paths("diff --git a/x b/c b/d\n"), ("x", "c b/d"))

    def test_the_rename_lines_settle_it(self):
        header, lines = inc.split_sections(self._AMBIGUOUS)[0]
        self.assertEqual(inc.section_paths(header, lines), ("x b/c", "d"))

    def test_a_non_rename_section_falls_back_to_the_header(self):
        header, lines = inc.split_sections(_patch("a.py", "@@ -1 +1 @@", "-x\n+y\n"))[0]
        self.assertEqual(inc.section_paths(header, lines), ("a.py", "a.py"))

    def test_a_rename_to_line_in_content_is_not_read_as_metadata(self):
        """Only the pre-hunk region is metadata; `+rename to x` in a hunk body
        is content, and must not redirect the section's key."""
        body = "-p\n+rename to /etc/shadow\n"
        header, lines = inc.split_sections(_patch("a.py", "@@ -1 +1 @@", body))[0]
        self.assertEqual(inc.section_paths(header, lines), ("a.py", "a.py"))

    def test_the_ambiguous_rename_keys_on_the_path_a_later_round_uses(self):
        """Round N renames the file; round N+1 edits it in place and emits the
        plain `diff --git a/d b/d`. Keyed off the header's wrong guess
        (`c b/d`) the two never matched, so the file was re-emitted whole every
        round after the rename."""
        header, lines = inc.split_sections(self._AMBIGUOUS)[0]
        later = _patch("d", "@@ -1 +1 @@", "-p\n+q\n")
        later_header, later_lines = inc.split_sections(later)[0]
        self.assertEqual(
            inc.section_paths(header, lines)[1],
            inc.section_paths(later_header, later_lines)[1],
        )


class TestEmptyOldPatch(unittest.TestCase):
    """Why the workflow step refuses to call the builder with an empty OLD.

    The builder cannot tell "nothing was reviewed last round" from "the OLD
    patch came out empty", and must not: an empty OLD legitimately means every
    file is new. The guard therefore belongs in the step, and these two tests
    are what it is guarding against.
    """

    def test_an_empty_old_reproduces_the_whole_reviewed_diff(self):
        full = (
            _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
            + _patch("b.py", "@@ -1 +1 @@", "-p\n+q\n")
        )
        self.assertEqual(inc.build("", full), full)

    def test_and_the_fail_safe_cannot_catch_that(self):
        """Nothing is foreign and the block EQUALS the diff, so neither arm
        trips: the panel just gets the same diff twice, prioritizing nothing."""
        full = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        foreign, new_lines, full_lines = inc.check(inc.build("", full), full)
        self.assertEqual(foreign, 0)
        self.assertEqual(new_lines, full_lines)


# --------------------------------------------------------------------------- #
# 5. The CLI the workflow step actually calls                                  #
# --------------------------------------------------------------------------- #


class TestCli(unittest.TestCase):
    def _main(self, argv):
        """Run the CLI with its key=value report captured, not dumped into the suite."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = inc.main(argv)
        return rc, buf.getvalue()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="inc-diff-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _file(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_build_writes_the_block(self):
        old = self._file("old.patch", _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n"))
        new_text = _patch("a.py", "@@ -1 +1 @@", "-x\n+z\n")
        new = self._file("new.patch", new_text)
        out = os.path.join(self.tmp, "out.patch")
        self.assertEqual(inc.main(["build", "--old", old, "--new", new, "--out", out]), 0)
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), new_text)

    def test_build_truncates_a_pre_existing_out_file(self):
        """A second round must never append onto the previous round's block."""
        same = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        old = self._file("old.patch", same)
        new = self._file("new.patch", same)
        out = self._file("out.patch", "STALE\n")
        inc.main(["build", "--old", old, "--new", new, "--out", out])
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")

    def test_check_exit_codes_and_output(self):
        a = _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n")
        full = self._file("full.patch", a)
        ok = self._file("ok.patch", a)
        rc, out = self._main(["check", "--new", ok, "--full", full])
        self.assertEqual(rc, 0)
        self.assertIn("foreign=0", out)
        bad = self._file("bad.patch", a + _patch("z.py", "@@ -1 +1 @@", "-p\n+q\n"))
        rc, out = self._main(["check", "--new", bad, "--full", full])
        self.assertEqual(rc, 2)
        # The three numbers the workflow's ::warning:: interpolates.
        self.assertIn("foreign=1", out)
        self.assertRegex(out, r"new_lines=\d+")
        self.assertRegex(out, r"full_lines=\d+")

    def test_check_rejects_a_block_longer_than_the_reviewed_diff(self):
        """Same path, more lines. Both arms of the fail-safe trip here now: the
        section is not carried verbatim AND the block outgrows the diff. The
        length arm is kept as a redundant second gate, and its two numbers are
        what the workflow's ::warning:: interpolates."""
        full = self._file("full.patch", _patch("a.py", "@@ -1 +1 @@", "-x\n+y\n"))
        long_block = _patch("a.py", "@@ -1,9 +1,9 @@", "".join(f"+l{i}\n" for i in range(20)))
        bad = self._file("bad.patch", long_block)
        rc, out = self._main(["check", "--new", bad, "--full", full])
        self.assertEqual(rc, 2)
        self.assertIn("foreign=1", out)

    def test_non_utf8_bytes_survive_a_round_trip(self):
        """PR bytes are attacker-authored; the helper must not die on them."""
        raw = b"diff --git a/b.bin b/b.bin\n@@ -1 +1 @@\n-\xff\xfe\n+\xfe\xff\n"
        old = os.path.join(self.tmp, "old.patch")
        new = os.path.join(self.tmp, "new.patch")
        out = os.path.join(self.tmp, "out.patch")
        with open(old, "wb") as fh:
            fh.write(b"")
        with open(new, "wb") as fh:
            fh.write(raw)
        self.assertEqual(inc.main(["build", "--old", old, "--new", new, "--out", out]), 0)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), raw)


# --------------------------------------------------------------------------- #
# 6. The workflow wiring — the helper is worthless if the step drifts off it   #
# --------------------------------------------------------------------------- #


_WORKFLOW = os.path.join(_HERE, "..", "..", "workflows", "cursor-review.yml")


class TestWorkflowWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_WORKFLOW, encoding="utf-8") as fh:
            cls.text = fh.read()

    def _assert_has(self, needle):
        # assertIn would dump the whole 2,500-line workflow into the failure.
        self.assertTrue(needle in self.text, f"cursor-review.yml no longer contains {needle!r}")

    def test_the_step_calls_the_helper(self):
        self._assert_has("incremental-diff.py")

    def test_the_commit_range_formulation_is_gone(self):
        """`git diff LAST...HEAD` is the bug — it must not come back."""
        needle = 'git diff "${LAST_REVIEWED_SHA}...${HEAD_SHA}"'
        self.assertFalse(needle in self.text, f"cursor-review.yml is back on {needle!r}")

    def test_the_sweepable_log_line_survives(self):
        self._assert_has("Incremental diff since round")

    def test_the_fail_safe_warning_is_present(self):
        self._assert_has("::warning::Incremental diff discarded")

    def test_incremental_subset_is_a_job_output(self):
        self._assert_has("incremental_subset:")

    def test_the_step_declines_an_empty_last_reviewed_patch(self):
        """A successful-but-EMPTY OLD must not reach the builder.

        `BASE...LAST` comes out empty whenever merge-base(BASE, LAST) is LAST
        itself, or whenever $DIFF_EXCLUDES filters every file out. git reports
        no error, so only an explicit `-s` test stops the fall-through that
        re-emits the entire reviewed diff as the "incremental" block.
        """
        self._assert_has('! build_old_patch || [ ! -s "$OLD_PATCH" ]')

    def test_the_old_patch_is_built_with_quotepath_off(self):
        """It must match the NEW side, which check-pr-size builds with
        `-c core.quotePath=false`; under the default a non-ASCII path arrives
        C-quoted on one side and plain on the other, so the two never key
        alike and the file is re-emitted in full on every round."""
        self._assert_has(
            'git -c core.quotePath=false diff "${BASE_SHA}...${LAST_REVIEWED_SHA}"'
        )

    def test_the_old_patch_is_size_bounded(self):
        """NEW is bounded by diff_size_cap; OLD is bounded by nothing — it
        keeps the generated-file sections the classifier strips out of the
        reviewed diff, which cost nothing against that cap."""
        self._assert_has("OLD_PATCH_MAX_BYTES")
        self._assert_has('[ "$(wc -c < "$OLD_PATCH")" -gt "$OLD_PATCH_MAX_BYTES" ]')

    def test_the_helper_is_loaded_from_the_pinned_checkout_not_the_pr(self):
        """The path the step calls must be one a pinned checkout actually writes.

        The helper decides what the panel is shown, so it has to come from THIS
        repo at `workflows_ref` — never the PR's own tree, which the PR under
        review can rewrite. That only holds while the hardcoded path in the step
        matches the `path:` some checkout of `Comfy-Org/github-workflows`
        declares, and nothing else in the workflow ties the two together.

        Parsed without PyYAML, for the reason the sibling workflow suites give:
        this repo is stdlib-only and this job's CI installs no requirements.
        """
        checkout_paths = set()
        repo_seen = False
        for raw in self.text.splitlines():
            line = raw.strip()
            if line.startswith("- name:") or line.startswith("- uses:"):
                repo_seen = False
            if line == "repository: Comfy-Org/github-workflows":
                repo_seen = True
            elif repo_seen and line.startswith("path:"):
                checkout_paths.add(line.split(":", 1)[1].strip())
        self.assertTrue(checkout_paths, "no pinned checkout of this repo found in the workflow")

        called = [
            line for line in self.text.splitlines()
            if "incremental-diff.py" in line and "=" in line and "#" not in line
        ]
        self.assertTrue(called, "the step no longer resolves incremental-diff.py by path")
        self.assertTrue(
            any(f"{prefix}/" in called[0] for prefix in checkout_paths),
            f"the step loads the helper from {called[0].strip()!r}, which is not "
            f"under any pinned checkout of this repo {sorted(checkout_paths)}",
        )


if __name__ == "__main__":
    unittest.main()
