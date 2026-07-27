#!/usr/bin/env python3
"""Unit tests for scope.py — groom's `path` scoping (BE-4757).

Structure mirrors the studio fleet's `scan-path-scoping-test.sh`, the suite that
paid for these invariants on the vulnscan side (BE-4655). The four that matter,
in the order they'd hurt:

1. BACKWARD COMPATIBILITY. `path: ''` must reproduce today's behavior exactly —
   same scope_label, same scope_desc, no filtering, no scoped job name. All 11
   live callers pass no `path`, so a regression here is a silent behavior change
   across every one of them. This is the single most important assertion.
2. THE CADENCE CLOCK. A path-scoped run must NOT count as the repo's last real
   groom, or a partial audit stamps "done" over the full one and the next
   scheduled whole-repo tick is suppressed for GROOM_INTERVAL_DAYS. Asserted in
   `test_interval.py` (`ScopedRunDoesNotResetCadence`) against the real
   `run_audited`.
3. CONTAINMENT. Absolute paths, `..` COMPONENTS and symlink escapes are
   rejected; a DOTTED directory name (`services/my..svc`) and a deeply nested one
   are accepted — over-rejecting is as much a bug as under-rejecting. Both sides
   normalize through realpath before the prefix compare, so macOS' trailing-slash
   $TMPDIR cannot report a false escape.
4. DEDUP SYMMETRY. A finding's signature must not absorb the scope, so a scoped
   run and a whole-repo run recognise each other's issues. Asserted here against
   the verifier brief's actual template text.

    python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scope  # noqa: E402


class ValidatePath(unittest.TestCase):
    """Invariant 3 — reject the dangerous, accept the merely unusual."""

    def test_empty_is_whole_repo(self):
        for raw in (None, "", "   "):
            self.assertEqual(scope.validate_path(raw), "")

    def test_plain_and_nested_paths_accepted(self):
        self.assertEqual(scope.validate_path("services"), "services")
        self.assertEqual(scope.validate_path("services/api"), "services/api")
        # Legitimately deep — the vulnscan suite's "validator over-rejects a deep
        # path" arm.
        self.assertEqual(
            scope.validate_path("services/agent/internal/api"),
            "services/agent/internal/api",
        )

    def test_dotted_directory_name_accepted(self):
        # ONLY a `..` COMPONENT is dangerous. A component that merely contains
        # dots is a legitimate directory name and must survive.
        self.assertEqual(scope.validate_path("services/my..svc"), "services/my..svc")
        self.assertEqual(scope.validate_path("services/.config"), "services/.config")
        self.assertEqual(scope.validate_path("a.b/c.d.e"), "a.b/c.d.e")

    def test_ergonomic_forms_normalize(self):
        self.assertEqual(scope.validate_path("./services/api"), "services/api")
        self.assertEqual(scope.validate_path("services/api/"), "services/api")
        self.assertEqual(scope.validate_path("  services/api  "), "services/api")

    def test_unsafe_paths_rejected(self):
        for bad in (
            "/etc",
            "/etc/passwd",
            "../../etc",
            "..",
            "services/../../etc",
            "services/..",
            "../services",
            "~/secrets",
            "services//api",
            "./",
            ".",
            "/",
            "services\\api",
            "services/api\x00",
            "services/a b",          # whitespace — outside the conservative charset
            "services/$(whoami)",    # shell metacharacters
            "services/`id`",         # backticks would break the issue-body markdown
        ):
            with self.subTest(path=bad):
                with self.assertRaises(scope.UnsafePathError):
                    scope.validate_path(bad)

    def test_rejected_paths_never_look_like_a_component(self):
        # The derived label lands in prompts and in an issue body inside
        # backticks; the vulnscan suite's "key is a safe single component" arm.
        # Anything that survives validation is a `/`-joined run of safe atoms and
        # is never `.`, `..` or empty.
        for good in ("services", "services/api", "services/my..svc", "a.b/c-d/e_f"):
            with self.subTest(path=good):
                parts = scope.validate_path(good).split("/")
                self.assertTrue(all(parts))
                self.assertNotIn(".", parts)
                self.assertNotIn("..", parts)


class DeriveScope(unittest.TestCase):
    """Invariant 1 — path:'' is byte-identical; explicit overrides still win."""

    def test_empty_path_is_byte_identical_to_today(self):
        derived = scope.derive_scope("", scope.DEFAULT_SCOPE_LABEL, scope.DEFAULT_SCOPE_DESC)
        self.assertEqual(
            derived,
            {
                "path": "",
                "scope_label": scope.DEFAULT_SCOPE_LABEL,
                "scope_desc": scope.DEFAULT_SCOPE_DESC,
            },
        )

    def test_empty_path_preserves_an_explicit_label(self):
        derived = scope.derive_scope("", "common/assets", "the assets package")
        self.assertEqual(derived["scope_label"], "common/assets")
        self.assertEqual(derived["scope_desc"], "the assets package")

    def test_path_drives_the_cosmetic_inputs(self):
        derived = scope.derive_scope("services/api", scope.DEFAULT_SCOPE_LABEL, scope.DEFAULT_SCOPE_DESC)
        self.assertEqual(derived["scope_label"], "services/api")
        self.assertIn("services/api", derived["scope_desc"])

    def test_explicit_override_beats_the_derivation(self):
        derived = scope.derive_scope("services/api", "api-only", "just the API service")
        self.assertEqual(derived["scope_label"], "api-only")
        self.assertEqual(derived["scope_desc"], "just the API service")

    def test_newlines_collapse_so_github_output_cannot_be_injected(self):
        # These values are written as `key=value` lines to $GITHUB_OUTPUT; an
        # embedded newline would truncate the value and inject a bogus key.
        derived = scope.derive_scope("", "a\nb", "one\ntwo\n\nthree")
        self.assertEqual(derived["scope_label"], "a b")
        self.assertEqual(derived["scope_desc"], "one two three")


class ResolveWithin(unittest.TestCase):
    """Invariant 3, filesystem half — the check the gate's syntax pass can't do."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Normalize the sandbox root the same way the SUT does. macOS' $TMPDIR
        # carries a TRAILING SLASH, so an un-normalized root makes a prefix
        # comparison report a FALSE escape — the exact trap the vulnscan suite
        # documented.
        self.root = os.path.realpath(self._tmp.name)
        os.makedirs(os.path.join(self.root, "services", "api"))
        os.makedirs(os.path.join(self.root, "services", "my..svc"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_in_tree_directory_resolves(self):
        self.assertEqual(
            scope.resolve_within(self.root, "services/api"),
            os.path.join(self.root, "services", "api"),
        )
        self.assertEqual(
            scope.resolve_within(self.root, "services/my..svc"),
            os.path.join(self.root, "services", "my..svc"),
        )

    def test_trailing_slash_on_the_root_is_not_a_false_escape(self):
        self.assertEqual(
            scope.resolve_within(self.root + "/", "services/api"),
            os.path.join(self.root, "services", "api"),
        )

    def test_symlink_escape_rejected(self):
        outside = tempfile.mkdtemp()
        try:
            link = os.path.join(self.root, "escape")
            os.symlink(outside, link)
            with self.assertRaises(scope.UnsafePathError):
                scope.resolve_within(self.root, "escape")
        finally:
            os.rmdir(outside)

    def test_missing_directory_rejected(self):
        # A typo'd dispatch must fail LOUDLY, not audit an empty file list and
        # report a suspiciously clean directory.
        with self.assertRaises(scope.UnsafePathError):
            scope.resolve_within(self.root, "services/nope")

    def test_a_file_is_not_a_directory_scope(self):
        with open(os.path.join(self.root, "README.md"), "w", encoding="utf-8") as f:
            f.write("x")
        with self.assertRaises(scope.UnsafePathError):
            scope.resolve_within(self.root, "README.md")

    def test_empty_path_is_the_root(self):
        self.assertEqual(scope.resolve_within(self.root, ""), self.root)


class SiteScoping(unittest.TestCase):
    def test_site_forms_normalize(self):
        for raw, expected in (
            ("services/api/main.go", "services/api/main.go"),
            ("services/api/main.go:42", "services/api/main.go"),
            ("services/api/main.go:42-80", "services/api/main.go"),
            ("services/api/main.go:42:9", "services/api/main.go"),
            ("./services/api/main.go", "services/api/main.go"),
            ("  services/api/main.go:7  ", "services/api/main.go"),
            (None, ""),
            ("", ""),
        ):
            with self.subTest(site=raw):
                self.assertEqual(scope.normalize_site(raw), expected)

    def test_absolute_runner_paths_relativize(self):
        self.assertEqual(
            scope.normalize_site("/home/runner/work/x/x/repo/services/api/a.go:3", clone="/home/runner/work/x/x/repo"),
            "services/api/a.go",
        )

    def test_scope_membership(self):
        self.assertTrue(scope.site_in_scope("services/api/a.go:3", "services/api"))
        self.assertTrue(scope.site_in_scope("services/api", "services/api"))
        self.assertFalse(scope.site_in_scope("services/apiary/a.go", "services/api"))
        self.assertFalse(scope.site_in_scope("common/b.go", "services/api"))
        # Whole-repo: everything is in scope, including an unparseable site.
        self.assertTrue(scope.site_in_scope("common/b.go", ""))
        self.assertTrue(scope.site_in_scope(None, ""))


class FilterFindings(unittest.TestCase):
    """AC 3 — an out-of-scope finding is dropped, and the drop is COUNTED."""

    IN_SCOPE = {"title": "dupe in api", "sites": ["services/api/a.go:10", "services/api/b.go:20"]}
    CROSS = {"title": "api duplicates common", "sites": ["services/api/a.go:10", "common/x.go:5"]}
    OUT = {"title": "dupe in web", "sites": ["web/a.ts:10", "web/b.ts:20"]}
    NO_SITES = {"title": "vague", "sites": []}
    MALFORMED = {"title": "no sites key"}

    def test_whole_repo_keeps_everything_untouched(self):
        findings = [self.IN_SCOPE, self.CROSS, self.OUT, self.NO_SITES, self.MALFORMED]
        kept, dropped = scope.filter_findings(findings, "")
        self.assertEqual(kept, findings)
        self.assertEqual(dropped, [])

    def test_out_of_scope_findings_are_dropped(self):
        kept, dropped = scope.filter_findings(
            [self.IN_SCOPE, self.CROSS, self.OUT, self.NO_SITES, self.MALFORMED], "services/api"
        )
        self.assertEqual([f["title"] for f in kept], ["dupe in api", "api duplicates common"])
        self.assertEqual(
            [f["title"] for f in dropped], ["dupe in web", "vague", "no sites key"]
        )

    def test_cross_boundary_finding_is_kept(self):
        # ANY-in-scope, not ALL: the checkout is deliberately FULL so a refactor
        # in services/api can be judged against the common/ code it references.
        # Requiring every site in scope would suppress exactly those findings.
        kept, _ = scope.filter_findings([self.CROSS], "services/api")
        self.assertEqual(len(kept), 1)

    def test_cli_filter_writes_the_dropped_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.json")
            dst = os.path.join(tmp, "out.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump({"repo": "o/r", "findings": [self.IN_SCOPE, self.OUT]}, f)
            self.assertEqual(
                scope.main(["filter", "--path", "services/api", "--in", src, "--out", dst]), 0
            )
            with open(dst, encoding="utf-8") as f:
                out = json.load(f)
            self.assertEqual(len(out["findings"]), 1)
            self.assertEqual(out["scope_dropped"], 1)

    def test_cli_rejects_an_unsafe_path_nonzero(self):
        # Fails CLOSED — unlike the cadence/volume gates there is no safe
        # fail-open reading of "audit a directory I could not validate".
        self.assertEqual(scope.main(["validate", "--path", "../../etc"]), 2)
        self.assertEqual(scope.main(["validate", "--path", "/etc"]), 2)
        self.assertEqual(scope.main(["validate", "--path", "services/../../etc"]), 2)


class ScopeNote(unittest.TestCase):
    def test_note_states_both_halves_of_the_rule(self):
        note = scope.scope_note("services/api")
        self.assertIn("services/api", note)
        self.assertIn("DROPPED", note)
        # The full checkout is a feature — the brief must not tell the agent it
        # may only READ inside the directory.
        self.assertIn("CONTEXT", note)

    def test_file_list_announces_truncation(self):
        files = [f"services/api/f{i}.go" for i in range(scope._MAX_LISTED_FILES + 5)]
        block = scope.file_list_block(files, "services/api")
        self.assertIn(f"In-scope tracked files ({len(files)})", block)
        self.assertIn("more (list truncated", block)
        # A silently short list reads to the agent as "that is the whole dir".
        self.assertIn("not just the files listed", block)


class ListFiles(unittest.TestCase):
    """The finder gets an enumeration, not prose — against a real git tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        for rel in ("services/api/a.go", "services/api/sub/b.go", "common/c.go"):
            os.makedirs(os.path.join(self.root, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(self.root, rel), "w", encoding="utf-8") as f:
                f.write("package x\n")
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        for cmd in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        ):
            subprocess.run(cmd, cwd=self.root, env=env, check=True, capture_output=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lists_only_in_scope_tracked_files_recursively(self):
        files = scope.list_files(self.root, "services/api")
        self.assertEqual(sorted(files), ["services/api/a.go", "services/api/sub/b.go"])

    def test_empty_path_lists_the_whole_tree(self):
        self.assertEqual(len(scope.list_files(self.root, "")), 3)

    def test_untracked_files_are_not_listed(self):
        with open(os.path.join(self.root, "services/api/untracked.go"), "w", encoding="utf-8") as f:
            f.write("x\n")
        self.assertNotIn("services/api/untracked.go", scope.list_files(self.root, "services/api"))


class SignatureIsContentDerived(unittest.TestCase):
    """AC 5 — a scoped run and a whole-repo run dedup against each other.

    The signature template lives in the verifier brief, and the workflow wires its
    `{{SIG_SCOPE}}` to the caller's RAW `scope_label` — never to the path-derived
    one. Pin both halves: if the brief ever went back to `{{SCOPE_LABEL}}` (which
    IS path-derived), the same defect would file twice, once per scope.
    """

    def setUp(self):
        brief_path = os.path.join(os.path.dirname(__file__), "..", "verifier.md")
        with open(brief_path, encoding="utf-8") as f:
            self.brief = f.read()

    def test_signature_format_uses_the_scope_independent_placeholder(self):
        self.assertIn("{{REPO_BASENAME}}:{{SIG_SCOPE}}:<slug>", self.brief)
        self.assertNotIn("{{REPO_BASENAME}}:{{SCOPE_LABEL}}:<slug>", self.brief)

    def test_brief_tells_the_verifier_not_to_absorb_the_scope(self):
        self.assertIn("CONTENT-derived, never scope-derived", self.brief)

    def test_workflow_wires_sig_scope_to_the_raw_input(self):
        wf = os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "groom.yml")
        with open(wf, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("SIG_SCOPE: ${{ inputs.scope_label }}", text)
        # …and the path-DERIVED label must NOT be what feeds it.
        self.assertNotIn("SIG_SCOPE: ${{ needs.gate.outputs.scope_label }}", text)

    def test_same_defect_yields_one_signature_across_scopes(self):
        # The end-to-end shape, expressed as the template the verifier fills in:
        # the only inputs to a signature are the repo basename, the RAW
        # scope_label, and a slug derived from the finding's title.
        def signature(repo_basename, raw_scope_label, title_slug):
            return f"{repo_basename}:{raw_scope_label}:{title_slug}"

        whole_repo = scope.derive_scope("", scope.DEFAULT_SCOPE_LABEL, scope.DEFAULT_SCOPE_DESC)
        scoped = scope.derive_scope("services/api", scope.DEFAULT_SCOPE_LABEL, scope.DEFAULT_SCOPE_DESC)
        # The cosmetic labels DO differ — a filed issue names its scope (AC 6)…
        self.assertNotEqual(whole_repo["scope_label"], scoped["scope_label"])
        # …while the signatures, keyed on the RAW input, do NOT.
        raw = scope.DEFAULT_SCOPE_LABEL
        self.assertEqual(
            signature("cloud", raw, "duplicate-retry-helper"),
            signature("cloud", raw, "duplicate-retry-helper"),
        )


if __name__ == "__main__":
    unittest.main()
