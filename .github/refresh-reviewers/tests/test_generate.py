#!/usr/bin/env python3
"""Tests for the refresh-reviewers generator (BE-4116).

Pure-python coverage of the scoring/rewrite core: glob-semantics parity with
assign-reviewers.yml's globToRegExp, decay math, threshold/floor/backfill
selection (including the under-floor leave-unchanged case), bot/generated-path/
rename-syntax filtering, noreply-email decoding, and the surgical rewrite
preserving every byte outside the edited lists — including a CRLF document's
line endings, and the multi-line flow sequences the rewrite must refuse.

Everything is stdlib-only and offline except two legs, both of which degrade
rather than fail: `yaml.safe_load` is skipped when PyYAML is absent (this
repo's CI installs no requirements, and the stdlib re-parse leg beside it
catches the same regressions), and `TestReadCommittedConfig` shells out to the
`git` already on every runner, against a throwaway repo under `tempfile`.

TestSharedParserCorpus drives ../../assign-reviewers/parser-corpus.json — the
one fixture file .github/assign-reviewers/tests/assignment.test.cjs runs
through the JS originals — so parser parity between the two hand-ported
implementations is asserted by an executable corpus rather than by a comment.

Run: python3 -m unittest discover -s .github/refresh-reviewers/tests -p 'test_*.py' -v
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

try:
    import yaml
except ImportError:            # stdlib-only CI: the safe_load leg skips
    yaml = None

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "generate.py")
_spec = importlib.util.spec_from_file_location("refresh_reviewers_generate", _MODULE_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


class TestGlobSemantics(unittest.TestCase):
    """Parity with assign-reviewers.yml's globToRegExp: `*` within a segment,
    `**` across segments, `?` one non-slash char, full-string anchored."""

    def match(self, glob, path):
        return bool(gen.glob_to_regexp(glob).match(path))

    def test_double_star_prefix_matches_any_depth(self):
        # `**/` compiles to an OPTIONAL leading-dirs group, so it also
        # matches at the repo root — the property the inference bucket needs.
        self.assertTrue(self.match("**/inference/**", "inference/model.go"))
        self.assertTrue(self.match("**/inference/**", "services/api/inference/model.go"))
        self.assertFalse(self.match("**/inference/**", "inference"))  # needs a file under it
        self.assertFalse(self.match("**/inference/**", "services/inference.go"))

    def test_single_dir_glob(self):
        self.assertTrue(self.match("services/ingest/**", "services/ingest/api/handler.go"))
        self.assertTrue(self.match("services/ingest/**", "services/ingest/main.go"))
        self.assertFalse(self.match("services/ingest/**", "services/ingest"))
        self.assertFalse(self.match("services/ingest/**", "services/ingestx/main.go"))
        self.assertFalse(self.match("services/ingest/**", "xservices/ingest/main.go"))

    def test_single_star_stays_within_segment(self):
        self.assertTrue(self.match("*.css", "site.css"))
        self.assertFalse(self.match("*.css", "styles/site.css"))
        self.assertTrue(self.match("docs/*.md", "docs/readme.md"))
        self.assertFalse(self.match("docs/*.md", "docs/sub/readme.md"))

    def test_question_mark_single_non_slash_char(self):
        self.assertTrue(self.match("v?/api.go", "v1/api.go"))
        self.assertFalse(self.match("v?/api.go", "v12/api.go"))
        self.assertFalse(self.match("v?/api.go", "v//api.go"))

    def test_bare_double_star_matches_everything(self):
        self.assertTrue(self.match("**", "a/b/c.go"))
        self.assertTrue(self.match("**", "top.go"))

    def test_regex_chars_escaped(self):
        self.assertTrue(self.match("a+b/c.go", "a+b/c.go"))
        self.assertFalse(self.match("a+b/c.go", "aab/c.go"))


class TestDecayMath(unittest.TestCase):
    def test_half_life(self):
        self.assertAlmostEqual(gen.decay_weight(0, 90), 1.0)
        self.assertAlmostEqual(gen.decay_weight(90, 90), 0.5)
        self.assertAlmostEqual(gen.decay_weight(180, 90), 0.25)
        self.assertAlmostEqual(gen.decay_weight(45, 90), 0.5 ** 0.5)

    def test_future_commit_clamped_to_full_weight(self):
        self.assertAlmostEqual(gen.decay_weight(-5, 90), 1.0)


class TestEmailResolution(unittest.TestCase):
    def test_plain_noreply_decodes(self):
        self.assertEqual(gen.email_to_login("octocat@users.noreply.github.com"), "octocat")

    def test_digits_plus_login_form_decodes(self):
        self.assertEqual(gen.email_to_login("583231+octocat@users.noreply.github.com"), "octocat")

    def test_decode_preserves_login_case(self):
        # collaborator matching canonicalizes case-insensitively, so the
        # decode must not lowercase mixed-case logins like DrJKL away.
        self.assertEqual(gen.email_to_login("66172478+DrJKL@users.noreply.github.com"), "DrJKL")
        self.assertEqual(gen.email_to_login("DrJKL@Users.Noreply.GitHub.com"), "DrJKL")

    def test_other_emails_do_not_decode(self):
        self.assertIsNone(gen.email_to_login("dev@example.com"))
        self.assertIsNone(gen.email_to_login("someone@users.noreply.github.com.evil.com"))

    def test_bot_emails_match_exclusion(self):
        self.assertTrue(gen.BOT_EMAIL_RX.search("49699333+dependabot[bot]@users.noreply.github.com"))
        self.assertTrue(gen.BOT_EMAIL_RX.search("noreply@argoproj.io"))
        self.assertFalse(gen.BOT_EMAIL_RX.search("dev@example.com"))


class TestPathFiltering(unittest.TestCase):
    def setUp(self):
        self.rxs = [re.compile(rx) for rx in gen.BUILTIN_EXCLUDE_PATHS]

    def excluded(self, path):
        return any(rx.search(path) for rx in self.rxs)

    def test_generated_paths_excluded(self):
        for p in [
            "ent/user.go",
            "services/api/ent/user_query.go",
            "api/types.gen.go",
            "proto/svc.pb.go",
            "vendor/golang.org/x/net/http2.go",
            "go.sum",
            "services/api/go.sum",
            "go.work.sum",
            "go.work.prod.sum",
            "package-lock.json",
            "web/pnpm-lock.yaml",
            "web/yarn.lock",
            "Cargo.lock",
            "infrastructure/dynamicconfig/staging/config.json",
            "frontend-version.json",
        ]:
            self.assertTrue(self.excluded(p), p)

    def test_hand_written_paths_survive(self):
        for p in [
            "ent/schema/user.go",          # hand-written ent schema
            "services/api/ent/schema/x.go",
            "services/api/handler.go",
            "docs/lockfiles.md",
            "infrastructure/terraform/main.tf",
        ]:
            self.assertFalse(self.excluded(p), p)

    def test_rename_brace_syntax_uses_new_path(self):
        self.assertEqual(
            gen.normalize_numstat_path("services/{ingest => intake}/api.go"),
            "services/intake/api.go")
        self.assertEqual(
            gen.normalize_numstat_path("services/{ => new}/api.go"),
            "services/new/api.go")
        self.assertEqual(
            gen.normalize_numstat_path("services/{old => }/api.go"),
            "services/api.go")

    def test_whole_path_rename_uses_new_path(self):
        self.assertEqual(
            gen.normalize_numstat_path("old.go => pkg/new.go"), "pkg/new.go")

    def test_plain_path_untouched(self):
        self.assertEqual(gen.normalize_numstat_path("a/b/c.go"), "a/b/c.go")

    def test_quoted_path_escapes_are_decoded(self):
        # git C-style quoting (core.quotePath): \t, \", \\, octal non-ASCII
        self.assertEqual(gen.normalize_numstat_path('"a\\ttab.go"'), "a\ttab.go")
        self.assertEqual(gen.normalize_numstat_path('"quo\\"te.go"'), 'quo"te.go')
        self.assertEqual(gen.normalize_numstat_path('"back\\\\slash.go"'), "back\\slash.go")
        self.assertEqual(gen.normalize_numstat_path('"sp\\303\\244th.go"'), "späth.go")


class TestParseLog(unittest.TestCase):
    LOG = "\n".join([
        "@aaa1|1700000000|dev@example.com",
        "10\t2\tservices/ingest/api.go",
        "5\t0\tgo.sum",
        "",
        "@bbb2|1700086400|49699333+dependabot[bot]@users.noreply.github.com",
        "1\t1\tpackage-lock.json",
        "",
        "@ccc3|1700172800|583231+octocat@users.noreply.github.com",
        "-\t-\tassets/logo.png",
        "3\t3\tservices/{ingest => intake}/handler.go",
    ])

    def test_parse_shape(self):
        commits = gen.parse_log(self.LOG.splitlines())
        self.assertEqual(len(commits), 3)
        sha, ts, email, paths = commits[0]
        self.assertEqual((sha, ts, email), ("aaa1", 1700000000, "dev@example.com"))
        self.assertEqual(paths, ["services/ingest/api.go", "go.sum"])
        # binary numstat (- -) and rename lines both parse
        self.assertEqual(commits[2][3], ["assets/logo.png", "services/intake/handler.go"])


class TestSelection(unittest.TestCase):
    KNOBS = dict(top_k=4, floor=2, min_touches=5, min_score=1.5, floor_min_touches=2)

    def test_threshold_and_cap(self):
        score = {"a": 9.0, "b": 7.0, "c": 6.0, "d": 5.0, "e": 4.0}
        touches = {l: 10 for l in score}
        picks, under, starred = gen.select_for_rule(score, touches, **self.KNOBS)
        self.assertEqual(picks, ["a", "b", "c", "d"])  # top_k caps at 4
        self.assertFalse(under)
        self.assertEqual(starred, set())

    def test_min_touches_disqualifies_high_score(self):
        # one huge recent commit != sustained expertise
        score = {"drive-by": 9.0, "a": 5.0, "b": 4.0}
        touches = {"drive-by": 1, "a": 10, "b": 10}
        picks, under, _ = gen.select_for_rule(score, touches, **self.KNOBS)
        self.assertEqual(picks, ["a", "b"])
        self.assertFalse(under)

    def test_floor_backfill_from_ranked_remainder(self):
        # only one qualifier -> backfill the best remainder with touches >= 2
        score = {"a": 5.0, "b": 1.0, "c": 0.8}
        touches = {"a": 10, "b": 3, "c": 4}
        picks, under, starred = gen.select_for_rule(score, touches, **self.KNOBS)
        self.assertEqual(picks, ["a", "b"])
        self.assertFalse(under)
        self.assertEqual(starred, {"b"})

    def test_backfill_skips_below_floor_min_touches(self):
        score = {"a": 5.0, "b": 1.0, "c": 0.8}
        touches = {"a": 10, "b": 1, "c": 4}  # b under floor_min_touches
        picks, under, starred = gen.select_for_rule(score, touches, **self.KNOBS)
        self.assertEqual(picks, ["a", "c"])
        self.assertEqual(starred, {"c"})

    def test_under_floor_reports_unchanged_case(self):
        # nobody backfillable -> under_floor True (caller leaves committed list)
        score = {"a": 5.0, "b": 0.5}
        touches = {"a": 10, "b": 1}
        picks, under, _ = gen.select_for_rule(score, touches, **self.KNOBS)
        self.assertEqual(picks, ["a"])
        self.assertTrue(under)

    def test_deterministic_tiebreak_by_login(self):
        score = {"zed": 3.0, "amy": 3.0}
        touches = {"zed": 9, "amy": 9}
        picks, _, _ = gen.select_for_rule(score, touches, **self.KNOBS)
        self.assertEqual(picks, ["amy", "zed"])

    def test_default_pool_skips_heavy_anchors_and_excludes(self):
        overall = {"a": 50.0, "b": 40.0, "c": 30.0, "d": 20.0, "e": 10.0,
                   "f": 5.0, "g": 1.0}
        final_lists = [["a", "b"], ["a", "c"], ["a"]]  # a anchors 3, b/c 1 each
        pool = gen.select_default_pool(overall, final_lists, {"d"})
        self.assertEqual(pool, ["b", "c", "e", "f", "g"])  # no a (>=2 rules), no d


class TestComputeScores(unittest.TestCase):
    def test_bucket_touch_and_overall(self):
        rules = [[gen.glob_to_regexp("services/ingest/**")],
                 [gen.glob_to_regexp("**/inference/**")]]
        exclude = [re.compile(rx) for rx in gen.BUILTIN_EXCLUDE_PATHS]
        now = 1_800_000_000
        day = 86400
        commits = [
            # fresh commit touching ingest twice (one file excluded)
            ("alice", now, ["services/ingest/a.go", "go.sum"]),
            # 90-day-old commit touching both buckets
            ("alice", now - 90 * day, ["services/ingest/b.go", "api/inference/m.go"]),
            # commit whose files are ALL excluded — contributes nothing
            ("bob", now, ["vendor/x.go", "package-lock.json"]),
            # unmatched path — lands in the gap report, not a bucket
            ("carol", now, ["docs/guide.md"]),
        ]
        score, touches, overall, gap = gen.compute_scores(commits, rules, exclude, now, 90)
        self.assertAlmostEqual(score[0]["alice"], 1.5)   # 1.0 + 0.5
        self.assertEqual(touches[0]["alice"], 2)
        self.assertAlmostEqual(score[1]["alice"], 0.5)
        self.assertEqual(touches[1]["alice"], 1)
        self.assertNotIn("bob", overall)                 # all-excluded commit
        self.assertAlmostEqual(overall["alice"], 1.5)
        self.assertAlmostEqual(gap["docs"]["carol"], 1.0)
        self.assertNotIn("services/ingest", gap)         # covered by a rule

    def test_gap_dedupes_per_commit_and_keys_by_directory(self):
        # a single commit touching many unmatched files in one directory must
        # add its weight ONCE per gap key (same commit-touch semantics as the
        # rule scores — else the gap column is incomparable), and the key is
        # the top-two-level DIRECTORY, never the filename.
        rules = [[gen.glob_to_regexp("services/ingest/**")]]
        now = 1_800_000_000
        commits = [
            ("carol", now, ["docs/a.md", "docs/b.md", "docs/sub/c.md"]),
            ("dave", now, ["README.md"]),                    # root-level file
            ("erin", now, ["web/src/components/App.tsx"]),   # deep path
        ]
        _s, _t, _o, gap = gen.compute_scores(commits, rules, [], now, 90)
        self.assertAlmostEqual(gap["docs"]["carol"], 1.0)    # not 2.0
        self.assertAlmostEqual(gap["docs/sub"]["carol"], 1.0)
        self.assertAlmostEqual(gap["(root)"]["dave"], 1.0)
        self.assertAlmostEqual(gap["web/src"]["erin"], 1.0)
        self.assertNotIn("docs/a.md", gap)


CONFIG = """\
# Reviewer expertise map — hand-tuned, comments are documentation.
# default_pool is the fallback when no rule matches.
default_pool: [old-a, old-b]  # keep small

rules:
  # Ingest service — the API front door.
  - paths: ["services/ingest/**"]
    reviewers: [old-a, old-c]  # ingest folk
  # Inference — anywhere in the tree.
  - paths:
      - "**/inference/**"
    reviewers:
      - old-d
      - old-e
  # Cold-start rule — nobody active enough; must stay untouched.
  - paths: ["services/quiet/**"]
    reviewers: [old-f]  # keep: cold-start
"""


class TestSurgicalRewrite(unittest.TestCase):
    def test_parse_shapes(self):
        config, locs = gen.parse_reviewer_config(CONFIG)
        self.assertEqual(config["default_pool"], ["old-a", "old-b"])
        self.assertEqual([r["reviewers"] for r in config["rules"]],
                         [["old-a", "old-c"], ["old-d", "old-e"], ["old-f"]])
        self.assertEqual([r["paths"] for r in config["rules"]],
                         [["services/ingest/**"], ["**/inference/**"], ["services/quiet/**"]])
        self.assertEqual(locs["default_pool"][0], "flow")
        self.assertEqual(locs["rules"][0][0], "flow")
        self.assertEqual(locs["rules"][1][0], "block")

    def test_flow_rewrite_preserves_comments(self):
        config, locs = gen.parse_reviewer_config(CONFIG)
        out = gen.rewrite_config(CONFIG, locs, {0: ["new-x", "new-y"]}, None)
        self.assertIn("reviewers: [new-x, new-y]  # ingest folk\n", out)
        # rule 1 (block) and rule 2 byte-identical; header comments intact
        self.assertIn("      - old-d\n      - old-e\n", out)
        self.assertIn("reviewers: [old-f]  # keep: cold-start", out)
        self.assertIn("# Reviewer expertise map — hand-tuned", out)

    def test_flow_span_is_anchored_to_the_value_not_to_any_later_bracket(self):
        # Fallout of narrowing `_strip_comment` to s-white: after a character
        # that is NOT a space or a tab, `#` no longer opens a comment, so the
        # whole tail is the plain scalar VALUE. `_rewrite_flow_line` must not
        # treat a `[` inside that tail as the flow span — doing so rewrote the
        # comment-shaped text and left the real value `alice` routing.
        line = "    reviewers: alice\u00a0# see [bob]"   # NBSP, escaped on purpose
        self.assertEqual(
            gen._rewrite_flow_line(line, "reviewers:", ["new1", "new2"]),
            "    reviewers: [new1, new2]")
        # A real trailing comment (space before `#`) still keeps its bytes, and a
        # genuine flow value is still rewritten in place — the anchoring only
        # rejects brackets that are not the value itself.
        self.assertEqual(
            gen._rewrite_flow_line("    reviewers: alice # see [bob]", "reviewers:", ["new1"]),
            "    reviewers: [new1] # see [bob]")
        self.assertEqual(
            gen._rewrite_flow_line("    reviewers: [a, b]  # note [x]", "reviewers:", ["new1"]),
            "    reviewers: [new1]  # note [x]")

    def test_rewriter_and_parser_agree_on_the_value_span_for_non_s_white(self):
        # The rewriter located the value with a BARE `lstrip()`/`rstrip()`, which
        # still absorb U+00A0 and U+001C, while `_parse_flow` trims s-white only.
        # So `reviewers:<NBSP>[alice]` is a bare SCALAR to the parser but took the
        # BRACKET branch here, emitting a line that re-parses as ONE ineligible
        # scalar login: the refreshed rule routed NOBODY. Assert the round trip,
        # not just the bytes — that is the property that was broken.
        for value in ("\u00a0[alice]", "\u001c[alice]"):   # escaped on purpose
            with self.subTest(value=value):
                doc = "rules:\n  - paths: ['a/**']\n    reviewers:%s\n" % value
                _config, locs = gen.parse_reviewer_config(doc)
                out = gen.rewrite_config(doc, locs, {0: ["new1", "new2"]}, None)
                reparsed, _ = gen.parse_reviewer_config(out)
                self.assertEqual(reparsed["rules"][0]["reviewers"],
                                 ["new1", "new2"])
        # The mirror image at the other end: a trailing non-s-white byte is part
        # of the VALUE to the parser, so the replaced span must swallow it rather
        # than leave it stranded after the new list.
        self.assertEqual(
            gen._rewrite_flow_line("    reviewers: alice\u00a0", "reviewers:", ["new1"]),
            "    reviewers: [new1]")
        self.assertEqual(
            gen._rewrite_flow_line("    reviewers: alice\u00a0  # note", "reviewers:", ["new1"]),
            "    reviewers: [new1]  # note")

    def test_crlf_document_round_trips_without_mixing_line_endings(self):
        # `main()` reads the config as BYTES now (universal-newline translation
        # would hide a CR from the parser that the runtime port plainly sees), so
        # a CRLF document reaches `rewrite_config` with its CRs intact for the
        # first time. Emitting bare-LF item lines into it would leave the file
        # with MIXED endings — a diff on every untouched line for the reviewer of
        # the drift PR.
        doc = "default_pool:\r\n  - alice\r\n  - bob\r\n"
        _config, locs = gen.parse_reviewer_config(doc)
        out = gen.rewrite_config(doc, locs, {}, ["carol", "dave"])
        self.assertEqual(out, "default_pool:\r\n  - carol\r\n  - dave\r\n")
        self.assertNotIn("\n", out.replace("\r\n", ""))
        # An LF document must not acquire CRs by the same code path.
        lf = "default_pool:\n  - alice\n  - bob\n"
        _config, lf_locs = gen.parse_reviewer_config(lf)
        self.assertEqual(gen.rewrite_config(lf, lf_locs, {}, ["carol"]),
                         "default_pool:\n  - carol\n")
        # The flow arm keeps the tail (CR included) on its own.
        flow = "default_pool: [alice]\r\n"
        _config, flow_locs = gen.parse_reviewer_config(flow)
        self.assertEqual(gen.rewrite_config(flow, flow_locs, {}, ["carol"]),
                         "default_pool: [carol]\r\n")

    def test_block_rewrite_replaces_items_at_same_indent(self):
        config, locs = gen.parse_reviewer_config(CONFIG)
        out = gen.rewrite_config(CONFIG, locs, {1: ["new-p", "new-q", "new-r"]}, None)
        self.assertIn("    reviewers:\n      - new-p\n      - new-q\n      - new-r\n", out)
        self.assertNotIn("old-d", out)
        # untouched lists keep their bytes
        self.assertIn("reviewers: [old-a, old-c]  # ingest folk", out)

    def test_default_pool_rewrite(self):
        config, locs = gen.parse_reviewer_config(CONFIG)
        out = gen.rewrite_config(CONFIG, locs, {}, ["pool-1", "pool-2"])
        self.assertIn("default_pool: [pool-1, pool-2]  # keep small\n", out)

    def test_everything_outside_edited_lists_is_byte_identical(self):
        config, locs = gen.parse_reviewer_config(CONFIG)
        out = gen.rewrite_config(CONFIG, locs, {0: ["n1", "n2"]}, ["p1"])
        orig_lines = CONFIG.split("\n")
        new_lines = out.split("\n")
        self.assertEqual(len(orig_lines), len(new_lines))
        edited = {2, 7}  # default_pool line, rule-0 reviewers line
        for i, (a, b) in enumerate(zip(orig_lines, new_lines)):
            if i in edited:
                self.assertNotEqual(a, b, f"line {i} should have changed")
            else:
                self.assertEqual(a, b, f"line {i} changed unexpectedly")

    def test_noop_rewrite_is_byte_identical(self):
        config, locs = gen.parse_reviewer_config(CONFIG)
        self.assertEqual(gen.rewrite_config(CONFIG, locs, {}, None), CONFIG)

    def test_block_default_pool(self):
        cfg = ("default_pool:\n"
               "  - old-a  # anchor\n"
               "  - old-b\n"
               "rules:\n"
               "  - paths: [\"x/**\"]\n"
               "    reviewers: [r1]\n")
        config, locs = gen.parse_reviewer_config(cfg)
        self.assertEqual(config["default_pool"], ["old-a", "old-b"])
        out = gen.rewrite_config(cfg, locs, {}, ["new-a"])
        self.assertIn("default_pool:\n  - new-a\nrules:\n", out)
        self.assertNotIn("old-a", out)

    def test_scalar_reviewers_becomes_flow(self):
        cfg = ("rules:\n"
               "  - paths: [\"x/**\"]\n"
               "    reviewers: solo  # single owner\n")
        config, locs = gen.parse_reviewer_config(cfg)
        self.assertEqual(config["rules"][0]["reviewers"], ["solo"])
        out = gen.rewrite_config(cfg, locs, {0: ["a", "b"]}, None)
        self.assertIn("    reviewers: [a, b]  # single owner\n", out)

    def test_bracket_inside_comment_is_not_the_flow(self):
        # a `[` in the trailing comment must never be mistaken for the list
        cfg = ("rules:\n"
               "  - paths: [\"x/**\"]\n"
               "    reviewers: solo  # [see docs]\n")
        config, locs = gen.parse_reviewer_config(cfg)
        out = gen.rewrite_config(cfg, locs, {0: ["a"]}, None)
        self.assertIn("    reviewers: [a]  # [see docs]\n", out)

    def test_default_pool_exclude_is_case_insensitive(self):
        pool = gen.select_default_pool({"DrJKL": 9.0, "b": 5.0}, [], {"drjkl"})
        self.assertEqual(pool, ["b"])

    def test_bom_flow_default_pool_rewrite_keeps_the_bom(self):
        # `parse_reviewer_config` strips a leading U+FEFF so the first key is
        # recognised; `rewrite_config` deliberately does NOT, because its
        # contract is byte-faithfulness. Dropping one character off the head of
        # line 0 changes no line INDEX, and `_rewrite_flow_line` locates the
        # bracket span WITHIN the line, so the BOM survives the rewrite in
        # place — this pins that the two halves stay compatible.
        # U+FEFF stays ESCAPED here, as in parser-corpus.json: a literal would be
        # invisible in review and would go silently VACUOUS if an editor or lint
        # normalised it away — `startswith("")` is always true, so the BOM
        # assertions below would keep passing while testing nothing.
        cfg = "\ufeffdefault_pool: [old-a]  # keep small\nrules:\n  - paths: [\"x/**\"]\n    reviewers: [r1]\n"
        config, locs = gen.parse_reviewer_config(cfg)
        self.assertEqual(config["default_pool"], ["old-a"])
        out = gen.rewrite_config(cfg, locs, {}, ["new-a", "new-b"])
        self.assertTrue(out.startswith("\ufeff"), "the rewrite dropped the BOM")
        self.assertIn("\ufeffdefault_pool: [new-a, new-b]  # keep small\n", out)
        self.assertEqual(out.split("\n")[1:], cfg.split("\n")[1:])


class TestShowInvisible(unittest.TestCase):
    """`_show_invisible` has to render a token the way the JS `showInvisible` does.

    Both warning channels quote the offending token, and a reader comparing a
    `::warning::` here against a `core.warning` there must see one string.
    """

    def test_bmp_characters_escape_to_four_lower_case_hex_digits(self):
        self.assertEqual(gen._show_invisible("a\u00a0b\ufeff"), "a\\u00a0b\\ufeff")

    def test_printable_ascii_is_left_alone(self):
        self.assertEqual(gen._show_invisible(" !~%"), " !~%")

    def test_astral_characters_escape_as_a_surrogate_pair(self):
        # JS's regex has no `u` flag, so it walks UTF-16 code units and renders U+1F600
        # as `\ud83d\ude00`. A plain `"\\u%04x" % ord(c)` renders `\u1f600` — five
        # digits, which is not a valid `\u` escape at all and reads ambiguously as
        # `\u1f60` followed by `0`. Emoji reach this via a stray line or a configured
        # login, both of which the helper escapes whole.
        self.assertEqual(gen._show_invisible("\U0001f600"), "\\ud83d\\ude00")
        self.assertEqual(gen._show_invisible("\U0010ffff"), "\\udbff\\udfff")


class TestParserWarnings(unittest.TestCase):
    """Both parser diagnostics: a duplicate `default_pool:` (last-wins) and a
    column-0 line that is neither key (it ends the block above it). Warnings,
    never rejections — a drift generator must not hard-fail on a malformed map.

    Every parsed result here is pinned language-neutrally by the shared corpus;
    the warnings cannot be, because each port emits on its own channel
    (`::warning::` here, `core.warning` in the JS step), so each side asserts
    its own. assignment.test.cjs mirrors each case below.
    """

    def parse(self, text):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            config, locs = gen.parse_reviewer_config(text)
        return config, locs, buf.getvalue()

    def test_duplicate_warns_once_and_last_wins(self):
        config, _locs, out = self.parse("default_pool: [alice]\ndefault_pool:\n  - bob\n")
        self.assertEqual(config["default_pool"], ["bob"])
        self.assertEqual(out.count("::warning::"), 1)
        self.assertIn("duplicate top-level default_pool: key", out)

    def test_nbsp_line_ending_default_pool_warns_once(self):
        # U+00A0 stays ESCAPED here, as everywhere in this repo: a literal would be
        # invisible in review, and an editor or lint that normalised it away would
        # make this test silently vacuous rather than red.
        config, _locs, out = self.parse("default_pool:\n  - alice\n\u00a0\n  - bob\n")
        self.assertEqual(config["default_pool"], ["alice"])
        self.assertEqual(out.count("::warning::"), 1)
        self.assertIn("line 3 is not a recognised top-level key (\\u00a0)", out)
        # The literal `U+00A0` in the message's own prose is what makes the character
        # searchable for a reader who does not think in `\u` escapes.
        self.assertIn("U+00A0", out)

    def test_nbsp_line_ending_rules_warns_once(self):
        config, _locs, out = self.parse(
            "rules:\n  - paths: ['a/**']\n    reviewers: [alice]\n"
            "\u00a0\n  - paths: ['b/**']\n    reviewers: [bob]\n")
        self.assertEqual(len(config["rules"]), 1)
        self.assertEqual(out.count("::warning::"), 1)
        self.assertIn("line 4 is not a recognised top-level key (\\u00a0)", out)

    def test_indented_nbsp_line_is_silent(self):
        # The negative control, and the one that keeps the warning useful: the orphaned
        # `- bob` tail below a REAL truncation falls through the same way, and warning
        # once per orphan would bury the single terminator that explains them all.
        config, _locs, out = self.parse("default_pool:\n  - alice\n  \u00a0\n  - bob\n")
        self.assertEqual(config["default_pool"], ["alice", "bob"])
        self.assertNotIn("::warning::", out)

    def test_unknown_top_level_key_warns_once(self):
        # The key-SHAPED variant: nothing invisible, same silent truncation before this.
        config, _locs, out = self.parse("default_pool:\n  - alice\nowners:\n  - bob\n")
        self.assertEqual(config["default_pool"], ["alice"])
        self.assertEqual(out.count("::warning::"), 1)
        self.assertIn("line 3 is not a recognised top-level key (owners:)", out)

    def test_warnings_name_the_configured_path_when_given(self):
        # `reviewer_config_path` is a caller input, so neither warning may hardcode
        # `reviewers.yml` — a caller that configured another name would be sent to look
        # at a file its repo does not have. Without a path the prefix is simply absent,
        # never a literal `None:`; the JS port asserts the same two shapes.
        # The second `default_pool:` is the BLOCK form on purpose: `owners:` only warns
        # because it terminates an open block, so the flow form would make this test
        # assert one warning while claiming to assert two.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gen.parse_reviewer_config(
                "default_pool: [alice]\ndefault_pool:\n  - bob\nowners:\n",
                ".github/owners.yml")
        lines = [l for l in buf.getvalue().splitlines() if l]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line.startswith("::warning::.github/owners.yml: "), line)
        self.assertNotIn("None", self.parse("default_pool:\n  - alice\nowners:\n  - bob\n")[2])

    # --- what the warning deliberately stays SILENT about -------------------------
    # Warning on EVERY column-0 fallthrough both overclaimed and flooded, so these six
    # are the negative half of the diagnostic and each one is a shape a real config has.

    def test_document_markers_are_silent(self):
        # yamllint's default `document-start` rule REQUIRES the leading `---`, so a
        # conformant reviewers.yml has one; `...` even TERMINATES the `rules:` block, so
        # without the carve-out a well-formed config warns on every scheduled run.
        config, _locs, out = self.parse(
            "---\ndefault_pool:\n  - alice\nrules:\n  - paths: ['a/**']\n"
            "    reviewers: [bob]\n...\n")
        self.assertEqual(config["default_pool"], ["alice"])
        self.assertEqual(len(config["rules"]), 1)
        self.assertNotIn("::warning::", out)

    def test_metadata_key_before_any_block_is_silent(self):
        # No block is open yet, so nothing is ended and nothing is dropped — the old
        # message asserted both. The nested `default_pool:` decoy is still ignored.
        config, _locs, out = self.parse(
            "version: 1\nnotes:\n  default_pool: [mallory]\ndefault_pool: [alice]\n")
        self.assertEqual(config["default_pool"], ["alice"])
        self.assertNotIn("::warning::", out)

    def test_zero_indented_block_sequence_warns_once_not_per_item(self):
        # `default_pool:` followed by a COLUMN-0 sequence is valid YAML and common, and
        # every item used to draw its own annotation — five here, ten in a real pool,
        # against GitHub's ~10-per-step budget. Only the first ends the block; the rest
        # end nothing, so they are silent and the one warning that explains the empty
        # pool is not buried.
        config, _locs, out = self.parse(
            "default_pool:\n- alice\n- bob\n- carol\n- dave\n- eve\n")
        self.assertEqual(config["default_pool"], [])
        self.assertEqual(out.count("::warning::"), 1)
        self.assertIn("line 2 is not a recognised top-level key (- alice)", out)

    def test_non_yaml_file_is_silent(self):
        # A `reviewer_config_path` aimed at prose opens no block, so it warns not at all
        # rather than once per line. The empty parse is the diagnostic there.
        config, _locs, out = self.parse(
            "Some prose here.\nAnother line.\nAnd another.\nYet more.\n")
        self.assertEqual(config, {"default_pool": [], "rules": []})
        self.assertNotIn("::warning::", out)

    # --- near misses: a line that NAMES a supported key without opening it ---------

    def test_invisible_prefix_before_a_key_warns_as_a_near_miss(self):
        # The stray character is not indentation, so the key reads as column 0, falls
        # through, and its whole block is never read — while the line looks perfect in an
        # editor. No block was open, so the terminator arm would have stayed silent here;
        # this arm is what keeps the two BOM/NEL corpus cases annotated.
        # U+0085, not U+FEFF: a SINGLE leading BOM is legal YAML and the parser strips
        # it, so a one-BOM document parses fine and must stay silent. It takes a second
        # BOM (the corpus case) or a character nothing strips to reach this arm.
        config, _locs, out = self.parse("\u0085default_pool: [alice]\nrules:\n")
        self.assertEqual(config["default_pool"], [])
        self.assertEqual(out.count("::warning::"), 1)
        self.assertIn(
            "line 1 is not a recognised top-level key (\\u0085default_pool: [alice]) "
            "— it is not the supported `default_pool:` key", out)
        # The negative control the line above depends on.
        self.assertNotIn("::warning::", self.parse("\ufeffdefault_pool: [alice]\n")[2])

    def test_prefix_sharing_key_is_not_honoured_and_warns(self):
        # `yaml.safe_load("rules:v2:\\n  - paths: [a]")` is `{"rules:v2": [...]}` — a key
        # NAMED `rules:v2`, not `rules`. The old `startswith` claimed it and parsed its
        # children as live routing rules, so an unsupported key was silently HONOURED.
        config, _locs, out = self.parse("rules:v2:\n  - paths: ['a/**']\n    reviewers: [alice]\n")
        self.assertEqual(config["rules"], [])
        self.assertEqual(out.count("::warning::"), 1)
        self.assertIn("it is not the supported `rules:` key", out)

    def test_no_space_after_the_colon_is_not_a_key(self):
        # `yaml.safe_load("default_pool:[alice]")` is the STRING `default_pool:[alice]`:
        # YAML reads `key:` as a mapping only with s-white or a line end after the colon.
        config, _locs, out = self.parse("default_pool:[alice]\n")
        self.assertEqual(config["default_pool"], [])
        self.assertEqual(out.count("::warning::"), 1)
        self.assertIn("it is not the supported `default_pool:` key", out)

    # --- the annotation has to survive the runner ---------------------------------

    def test_workflow_command_characters_are_escaped(self):
        # `::warning::` is built by hand here, and the runner DECODES `%25`/`%0D`/`%0A`
        # out of the message: an unescaped `%` in a config token (printable ASCII, so
        # `_show_invisible` leaves it) is decoded into a mangled multi-line annotation,
        # and a newline in the caller-configured path can start a SECOND runner command.
        # `core.warning` does exactly this for the JS port, so this is also the parity.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gen.parse_reviewer_config(
                "default_pool:\n  - alice\n100%0Adone\n  - bob\n", "a\nb.yml")
        out = buf.getvalue()
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("a%0Ab.yml: ", out)
        self.assertIn("(100%250Adone)", out)

    def test_live_consumer_config_shape_is_silent(self):
        # The acceptance shape every live consumer config has: only `default_pool:`,
        # `rules:`, comments and blank lines at column 0. A warning here would fire on
        # every scheduled drift run and train people to ignore the annotation.
        _config, _locs, out = self.parse(
            "# a leading comment\n\n"
            "default_pool:\n  - alice\n  - bob\n\n"
            "# a comment between blocks\n\n"
            "rules:\n  - paths: ['a/**']\n    reviewers: [alice]\n\n"
            "  # an indented comment\n  - paths: ['b/**']\n    reviewers: [bob]\n")
        self.assertNotIn("::warning::", out)

    def test_empty_first_list_still_warns(self):
        # Keyed on "the key was seen", not on "the list is non-empty" — an
        # emptiness test would make this port silent where the JS one warns.
        _config, _locs, out = self.parse("default_pool: []\ndefault_pool: [bob]\n")
        self.assertEqual(out.count("::warning::"), 1)

    def test_single_or_indented_default_pool_is_silent(self):
        for text in ("default_pool: [alice]\n",
                     "default_pool:\n  - alice\n",
                     "rules:\n  - reviewers: [a]\n",
                     "  default_pool: [indented]\ndefault_pool: [alice]\n"):
            with self.subTest(text=text):
                self.assertNotIn("::warning::", self.parse(text)[2])

    def test_locs_still_point_at_a_rewritable_list(self):
        # last-wins is a PARSE rule; the rewrite must stay coherent with it.
        cfg = "default_pool: [alice]\ndefault_pool:\n  - bob\n"
        _config, locs, _out = self.parse(cfg)
        out = gen.rewrite_config(cfg, locs, {}, ["carol"])
        self.assertEqual(out, "default_pool: [alice]\ndefault_pool:\n  - carol\n")

    def test_locs_drop_a_shadowed_key_when_the_winner_has_no_items(self):
        # The other half of last-wins-for-`locs`, and the dangerous one: the
        # winning occurrence carries no item lines, so there is nothing to
        # rewrite. `locs` must NOT fall back to the shadowed first occurrence —
        # rewriting there emits a file whose refreshed pool sits on a dead key,
        # re-reads as the empty winner, and regenerates the same diff forever.
        cfg = "default_pool: [alice]\ndefault_pool:\nrules:\n  - paths: [\"x/**\"]\n    reviewers: [r1]\n"
        config, locs, _out = self.parse(cfg)
        self.assertEqual(config["default_pool"], [])
        self.assertIsNone(locs["default_pool"])
        # With no rewritable location the document is returned untouched, rather
        # than rewritten into the occurrence the parse discarded.
        self.assertEqual(gen.rewrite_config(cfg, locs, {}, ["carol"]), cfg)


def _as_parser_shape(doc):
    """Normalize a PyYAML-loaded reviewers.yml to parse_reviewer_config's
    shape so the two can be compared: absent keys become empty lists and a
    bare-scalar value becomes a one-element list."""
    def as_list(v):
        if v is None:
            return []
        return [v] if isinstance(v, str) else list(v)
    doc = doc or {}
    return {"default_pool": as_list(doc.get("default_pool")),
            "rules": [{"paths": as_list(r.get("paths")),
                       "reviewers": as_list(r.get("reviewers"))}
                      for r in (doc.get("rules") or [])]}


def _pr_body_report():
    """A complete report dict for build_pr_body — the contract fixture."""
    return {
        "repo": "o/r", "default_branch": "main",
        "config_path": ".github/reviewers.yml",
        "knobs": {"window_months": 12, "half_life_days": 90, "top_k": 4,
                  "floor": 2, "min_touches": 5, "min_score": 1.5,
                  "floor_min_touches": 2, "map_exclude": ["op-login"]},
        "changed": True,
        "bot_commits_excluded": 7,
        "unresolved_email_commits": 3,
        "rules": [
            {"index": 0, "paths": ["services/ingest/**"],
             "before": ["old-a"], "after": ["new-a", "new-b"],
             "changed": True, "under_floor": False, "starred": ["new-b"],
             "scores": {"new-a": 9.1, "new-b": 1.2},
             "touches": {"new-a": 20, "new-b": 3}},
            {"index": 1, "paths": ["services/quiet/**"],
             "before": ["old-f"], "after": ["old-f"],
             "changed": False, "under_floor": True, "starred": [],
             "scores": {}, "touches": {}},
        ],
        "default_pool": {"before": ["old-a"], "after": ["new-a"],
                         "changed": True, "scores": {"new-a": 30.0}},
        "gaps": [{"dir": "docs/site", "score": 12.5,
                  "top": [{"login": "carol", "score": 8.0}]}],
        "skipped_unterminated": [],
    }


CONFIG_CRLF = CONFIG.replace("\n", "\r\n")

# `default_pool` closes on line 0; `rules[0].reviewers` opens a flow sequence
# that only closes on the NEXT line — the shape the rewrite must refuse.
UNTERMINATED_RULE = """\
default_pool: [alice, bob]
rules:
  - paths: ["x/**"]
    reviewers: [carol,
      dave]
"""

# The mirror image: the multi-line list is `default_pool`, the rule is fine.
UNTERMINATED_POOL = """\
default_pool: [alice,
  bob]
rules:
  - paths: ["x/**"]
    reviewers: [carol]
"""

BLOCK_POOL = """\
default_pool:
  - old-a  # anchor
  - old-b
rules:
  - paths: ["x/**"]
    reviewers: [r1]
"""

SCALAR_REVIEWERS = """\
rules:
  - paths: ["x/**"]
    reviewers: solo  # single owner
"""

BRACKET_IN_COMMENT = """\
rules:
  - paths: ["x/**"]
    reviewers: solo  # [see docs]
"""

# The full rewrite corpus, one dict per fixture, each carrying what the OUTPUT
# must satisfy on all four legs of the round-trip regression:
#   parses_to — parse_reviewer_config(out)[0], the generator's own reader
#   loads_to  — yaml.safe_load(out) normalized, a real YAML parser's reading
#               (they differ only where the parser's documented `s[1:]` flow
#               fallback does, i.e. on a multi-line flow sequence)
#   orphans   — how many ORPHAN_RX lines the output may contain; 0 everywhere
#               except the fixtures whose own committed text legitimately
#               continues a flow sequence onto the next line
def _case(label, text, rules, pool, parses_to, loads_to=None, orphans=0):
    return dict(label=label, text=text, rules=rules, pool=pool,
                parses_to=parses_to, loads_to=loads_to or parses_to,
                orphans=orphans)


_CONFIG_RULES_UNCHANGED = [
    {"paths": ["services/ingest/**"], "reviewers": ["old-a", "old-c"]},
    {"paths": ["**/inference/**"], "reviewers": ["old-d", "old-e"]},
    {"paths": ["services/quiet/**"], "reviewers": ["old-f"]},
]

REWRITE_CASES = [
    _case("flow+pool", CONFIG, {0: ["n1", "n2"]}, ["p1"],
          {"default_pool": ["p1"],
           "rules": [{"paths": ["services/ingest/**"], "reviewers": ["n1", "n2"]}]
                    + _CONFIG_RULES_UNCHANGED[1:]}),
    _case("block", CONFIG, {1: ["new-p", "new-q", "new-r"]}, None,
          {"default_pool": ["old-a", "old-b"],
           "rules": [_CONFIG_RULES_UNCHANGED[0],
                     {"paths": ["**/inference/**"],
                      "reviewers": ["new-p", "new-q", "new-r"]},
                     _CONFIG_RULES_UNCHANGED[2]]}),
    _case("noop", CONFIG, {}, None,
          {"default_pool": ["old-a", "old-b"], "rules": _CONFIG_RULES_UNCHANGED}),
    _case("block-pool", BLOCK_POOL, {}, ["new-a"],
          {"default_pool": ["new-a"],
           "rules": [{"paths": ["x/**"], "reviewers": ["r1"]}]}),
    _case("scalar", SCALAR_REVIEWERS, {0: ["a", "b"]}, None,
          {"default_pool": [],
           "rules": [{"paths": ["x/**"], "reviewers": ["a", "b"]}]}),
    _case("bracket-in-comment", BRACKET_IN_COMMENT, {0: ["a"]}, None,
          {"default_pool": [],
           "rules": [{"paths": ["x/**"], "reviewers": ["a"]}]}),
    # The two shapes the rewrite must REFUSE: the multi-line list keeps its
    # committed bytes while the well-formed list beside it is still rewritten.
    _case("unterminated-pool", UNTERMINATED_POOL, {0: ["z1", "z2"]}, ["p1", "p2"],
          {"default_pool": ["alice"],
           "rules": [{"paths": ["x/**"], "reviewers": ["z1", "z2"]}]},
          loads_to={"default_pool": ["alice", "bob"],
                    "rules": [{"paths": ["x/**"], "reviewers": ["z1", "z2"]}]},
          orphans=1),
    _case("unterminated-rule", UNTERMINATED_RULE, {0: ["z1"]}, ["p1", "p2"],
          {"default_pool": ["p1", "p2"],
           "rules": [{"paths": ["x/**"], "reviewers": ["carol"]}]},
          loads_to={"default_pool": ["p1", "p2"],
                    "rules": [{"paths": ["x/**"], "reviewers": ["carol", "dave"]}]},
          orphans=1),
]
# The same matrix again with CRLF line endings: a rewrite that is byte-faithful
# on LF and not on CRLF is exactly the defect this pairing exists to catch.
REWRITE_CASES += [dict(c, label=c["label"] + "/crlf",
                       text=c["text"].replace("\n", "\r\n"))
                  for c in REWRITE_CASES]

# An output line that ends in `]` but is neither a `key: ...` line nor a block
# item is the orphaned tail of a flow sequence the rewrite tore in half.
ORPHAN_RX = re.compile(r"^\s*[^\s\-#][^:]*\]\s*$", re.M)


class TestCrlfByteFaithfulRewrite(unittest.TestCase):
    r"""A CRLF-committed config must come back CRLF. `text.split("\n")` leaves
    the `\r` on every line, so the flow/scalar edits carry it for free — but an
    INSERTED block item has no line to inherit from and must copy the ending of
    the item it replaces, or the rewrite emits a mixed-ending file nobody
    wrote."""

    def assert_all_crlf(self, out):
        self.assertTrue(out.endswith("\r\n"), repr(out[-6:]))
        for i, line in enumerate(out.split("\r\n")[:-1]):
            self.assertNotIn("\n", line, f"line {i} is not CRLF-terminated")
            self.assertNotIn("\r", line, f"line {i} carries a stray CR")

    def test_flow_rewrite_keeps_crlf(self):
        config, locs = gen.parse_reviewer_config(CONFIG_CRLF)
        out = gen.rewrite_config(CONFIG_CRLF, locs, {0: ["new-x", "new-y"]}, None)
        self.assert_all_crlf(out)
        self.assertIn("reviewers: [new-x, new-y]  # ingest folk\r\n", out)

    def test_block_rewrite_keeps_crlf(self):
        config, locs = gen.parse_reviewer_config(CONFIG_CRLF)
        out = gen.rewrite_config(CONFIG_CRLF, locs, {1: ["new-p", "new-q"]}, None)
        self.assert_all_crlf(out)
        self.assertIn("    reviewers:\r\n      - new-p\r\n      - new-q\r\n", out)
        self.assertNotIn("old-d", out)

    def test_default_pool_and_scalar_rewrites_keep_crlf(self):
        config, locs = gen.parse_reviewer_config(CONFIG_CRLF)
        out = gen.rewrite_config(CONFIG_CRLF, locs, {}, ["pool-1", "pool-2"])
        self.assert_all_crlf(out)
        self.assertIn("default_pool: [pool-1, pool-2]  # keep small\r\n", out)

        scalar = SCALAR_REVIEWERS.replace("\n", "\r\n")
        config, locs = gen.parse_reviewer_config(scalar)
        out = gen.rewrite_config(scalar, locs, {0: ["a", "b"]}, None)
        self.assert_all_crlf(out)
        self.assertIn("    reviewers: [a, b]  # single owner\r\n", out)

    def test_block_default_pool_keeps_crlf(self):
        block_pool = BLOCK_POOL.replace("\n", "\r\n")
        config, locs = gen.parse_reviewer_config(block_pool)
        out = gen.rewrite_config(block_pool, locs, {}, ["new-a"])
        self.assert_all_crlf(out)
        self.assertIn("default_pool:\r\n  - new-a\r\nrules:\r\n", out)

    def test_everything_outside_edited_lists_is_byte_identical_crlf(self):
        config, locs = gen.parse_reviewer_config(CONFIG_CRLF)
        out = gen.rewrite_config(CONFIG_CRLF, locs, {0: ["n1", "n2"]}, ["p1"])
        orig_lines = CONFIG_CRLF.split("\r\n")
        new_lines = out.split("\r\n")
        self.assertEqual(len(orig_lines), len(new_lines))
        edited = {2, 7}  # default_pool line, rule-0 reviewers line
        for i, (a, b) in enumerate(zip(orig_lines, new_lines)):
            if i in edited:
                self.assertNotEqual(a, b, f"line {i} should have changed")
            else:
                self.assertEqual(a, b, f"line {i} changed unexpectedly")

    def test_noop_rewrite_is_byte_identical_crlf(self):
        config, locs = gen.parse_reviewer_config(CONFIG_CRLF)
        self.assertEqual(gen.rewrite_config(CONFIG_CRLF, locs, {}, None),
                         CONFIG_CRLF)

    def test_mixed_endings_stay_mixed_per_line(self):
        # Endings are taken per POSITION, so a file someone left half
        # converted comes back exactly as half converted as it went in.
        mixed = ("rules:\r\n"
                 "  - paths: [\"x/**\"]\r\n"
                 "    reviewers:\r\n"
                 "      - old-d\n"
                 "      - old-e\r\n")
        config, locs = gen.parse_reviewer_config(mixed)
        out = gen.rewrite_config(mixed, locs, {0: ["n1", "n2"]}, None)
        # item 0 displaces the LF line, item 1 the CRLF line
        self.assertIn("      - n1\n      - n2\r\n", out)
        self.assertTrue(out.startswith("rules:\r\n"))

    def test_longer_list_past_the_old_items_takes_the_block_ending(self):
        # Sampling one ending for the whole insert cannot answer this: the
        # new list outruns the old one, and the extra items must not arrive
        # bare-LF in a CRLF block.
        mixed = ("rules:\r\n"
                 "  - paths: [\"x/**\"]\r\n"
                 "    reviewers:\r\n"
                 "      - old-d\n"
                 "      - old-e\r\n"
                 "      - old-f\r\n")
        config, locs = gen.parse_reviewer_config(mixed)
        out = gen.rewrite_config(mixed, locs, {0: ["n1", "n2", "n3", "n4"]}, None)
        # positional for the first three, then the block's dominant ending
        self.assertIn("      - n1\n      - n2\r\n      - n3\r\n      - n4\r\n",
                      out)

    def test_block_at_an_unterminated_final_line_stays_crlf(self):
        # The final element of a `split("\n")` is the tail AFTER the last
        # newline: it has no ending to lend. Sampling it handed every
        # inserted item a bare LF inside a CRLF document.
        crlf_eof = ("rules:\r\n"
                    "  - paths: [\"x/**\"]\r\n"
                    "    reviewers:\r\n"
                    "      - old-d")          # no trailing newline
        config, locs = gen.parse_reviewer_config(crlf_eof)
        out = gen.rewrite_config(crlf_eof, locs, {0: ["n1", "n2"]}, None)
        self.assertEqual(out, "rules:\r\n"
                              "  - paths: [\"x/**\"]\r\n"
                              "    reviewers:\r\n"
                              "      - n1\r\n"
                              "      - n2")
        self.assertFalse(out.endswith("\r"), "stray CR left at EOF")

    def test_shorter_list_at_an_unterminated_final_line_leaves_no_stray_cr(self):
        crlf_eof = ("rules:\r\n"
                    "  - paths: [\"x/**\"]\r\n"
                    "    reviewers:\r\n"
                    "      - old-d\r\n"
                    "      - old-e")          # no trailing newline
        config, locs = gen.parse_reviewer_config(crlf_eof)
        out = gen.rewrite_config(crlf_eof, locs, {0: ["n1"]}, None)
        self.assertEqual(out, "rules:\r\n"
                              "  - paths: [\"x/**\"]\r\n"
                              "    reviewers:\r\n"
                              "      - n1")


class TestUnterminatedFlowSequence(unittest.TestCase):
    """A flow sequence continued onto later lines cannot be rewritten by a
    single-line span replacement — doing so leaves `  bob]` behind as orphaned
    YAML. The parse half is unchanged (corpus parity); only the location tag
    and the rewrite's willingness to touch it change."""

    def test_pool_is_tagged_unterminated_and_left_alone(self):
        config, locs = gen.parse_reviewer_config(UNTERMINATED_POOL)
        self.assertEqual(locs["default_pool"][0], "unterminated")
        self.assertEqual(locs["default_pool"][1], 0)
        # corpus parity: `_parse_flow` still returns the `s[1:]`-based items
        self.assertEqual(config["default_pool"], ["alice"])
        self.assertEqual(config["rules"][0]["reviewers"], ["carol"])
        out = gen.rewrite_config(UNTERMINATED_POOL, locs, {0: ["z1", "z2"]},
                                 ["p1", "p2"])
        # the pool is untouched, the well-formed rule is still rewritten
        self.assertIn("default_pool: [alice,\n  bob]\n", out)
        self.assertIn("reviewers: [z1, z2]", out)
        # the fixture's own continuation line is the only `]`-tail, still
        # attached to the list it belongs to — the rewrite added no new one
        self.assertEqual(len(ORPHAN_RX.findall(out)),
                         len(ORPHAN_RX.findall(UNTERMINATED_POOL)))

    def test_rule_is_tagged_unterminated_and_left_alone(self):
        config, locs = gen.parse_reviewer_config(UNTERMINATED_RULE)
        self.assertEqual(locs["rules"][0][0], "unterminated")
        self.assertEqual(locs["rules"][0][1], 3)
        self.assertEqual(config["rules"][0]["reviewers"], ["carol"])
        self.assertEqual(config["default_pool"], ["alice", "bob"])
        out = gen.rewrite_config(UNTERMINATED_RULE, locs, {0: ["z1"]},
                                 ["p1", "p2"])
        self.assertIn("reviewers: [carol,\n      dave]\n", out)
        self.assertIn("default_pool: [p1, p2]\n", out)
        self.assertEqual(len(ORPHAN_RX.findall(out)),
                         len(ORPHAN_RX.findall(UNTERMINATED_RULE)))

    def test_unterminated_locations_names_key_and_line(self):
        _, locs = gen.parse_reviewer_config(UNTERMINATED_POOL)
        self.assertEqual(gen.unterminated_locations(locs),
                         [("default_pool", 0)])
        _, locs = gen.parse_reviewer_config(UNTERMINATED_RULE)
        self.assertEqual(gen.unterminated_locations(locs),
                         [("rules[0].reviewers", 3)])
        _, locs = gen.parse_reviewer_config(CONFIG)
        self.assertEqual(gen.unterminated_locations(locs), [])

    def test_closing_bracket_in_a_trailing_comment_does_not_terminate(self):
        # `_flow_is_unterminated` sees the comment-STRIPPED value, so a `]`
        # inside the comment must not make a torn list look closed.
        cfg = "default_pool: [alice,  # see [docs]\n  bob]\n"
        _, locs = gen.parse_reviewer_config(cfg)
        self.assertEqual(locs["default_pool"][0], "unterminated")

    def test_single_line_flow_is_not_unterminated(self):
        self.assertFalse(gen._flow_is_unterminated("[a, b]"))
        self.assertFalse(gen._flow_is_unterminated("  [a]  "))
        self.assertTrue(gen._flow_is_unterminated("[a,"))
        self.assertFalse(gen._flow_is_unterminated("solo"))
        self.assertFalse(gen._flow_is_unterminated(""))

    def test_pr_body_reports_the_skipped_lists(self):
        report = _pr_body_report()
        report["skipped_unterminated"] = [{"key": "default_pool", "line": 1}]
        body = gen.build_pr_body(report)
        self.assertIn("1 list(s) left unchanged", body)
        self.assertIn("multi-line flow sequences", body)
        self.assertIn("(line 1)", body)

    def test_pr_body_says_nothing_when_there_is_nothing_to_say(self):
        body = gen.build_pr_body(_pr_body_report())
        self.assertNotIn("left unchanged because", body)


# A rule whose `paths:` is the torn list; its `reviewers:` is a perfectly
# editable single line, which is precisely why it is dangerous.
UNTERMINATED_PATHS = """\
default_pool: [alice, bob]
rules:
  - paths: ["x/**",
      "y/**"]
    reviewers: [carol, dave]
"""

# A `]` inside a quoted scalar on a list that really is torn open.
QUOTED_BRACKET_TORN = """\
rules:
  - paths: ["x/**"]
    reviewers: ["a]b", carol,
      dave]
"""


class TestUnterminatedPathsHoldsBackItsRule(unittest.TestCase):
    """The reviewers half of the guard only covers a list with no line to
    edit. A torn `paths:` is the other half: the reviewers line IS editable,
    but the rule's globs were truncated at the break, so rewriting it would
    write a list scored against the wrong bucket — silently wrong output
    rather than the skipped output the guard exists to produce."""

    def test_paths_is_tagged_and_named(self):
        _, locs = gen.parse_reviewer_config(UNTERMINATED_PATHS)
        self.assertEqual(locs["rule_paths"][0][0], "unterminated")
        self.assertEqual(locs["rule_paths"][0][1], 2)
        # the reviewers list itself is a normal, single-line flow
        self.assertEqual(locs["rules"][0][0], "flow")
        self.assertEqual(gen.unterminated_locations(locs),
                         [("rules[0].paths", 2)])

    def test_the_rule_is_not_rewritable_and_is_left_alone(self):
        _, locs = gen.parse_reviewer_config(UNTERMINATED_PATHS)
        self.assertFalse(gen.rule_is_rewritable(locs, 0))
        out = gen.rewrite_config(UNTERMINATED_PATHS, locs, {0: ["z1"]}, None)
        self.assertEqual(out, UNTERMINATED_PATHS)

    def test_a_whole_rule_is_still_rewritable(self):
        _, locs = gen.parse_reviewer_config(CONFIG)
        self.assertTrue(gen.rule_is_rewritable(locs, 0))
        self.assertIsNone(locs["rule_paths"][0])

    def test_pr_body_explains_the_paths_entry(self):
        report = _pr_body_report()
        report["skipped_unterminated"] = [{"key": "rules[0].paths", "line": 3}]
        body = gen.build_pr_body(report)
        self.assertIn("scored against the wrong bucket", body)


class TestQuotedBracketDoesNotCloseTheSequence(unittest.TestCase):
    """`"]" not in s` is not quote-aware: a `]` inside a quoted scalar made a
    torn list look closed, and the span rewrite then cut it at that quoted
    bracket — leaving the continuation line behind as orphaned YAML, the
    exact corruption the guard exists to prevent."""

    def test_flow_is_unterminated_skips_quoted_brackets(self):
        self.assertTrue(gen._flow_is_unterminated('["a]b", carol,'))
        self.assertTrue(gen._flow_is_unterminated("['a]b', carol,"))
        self.assertFalse(gen._flow_is_unterminated('["a]b", carol]'))

    def test_find_unquoted_tracks_state_from_the_start(self):
        self.assertEqual(gen._find_unquoted('["a]b"]', "]"), 6)
        # `start` narrows the answer without losing the quote state before it
        self.assertEqual(gen._find_unquoted('reviewers: ["a]b"]', "[", 9), 11)

    def test_the_torn_list_is_left_exactly_as_committed(self):
        _, locs = gen.parse_reviewer_config(QUOTED_BRACKET_TORN)
        self.assertEqual(locs["rules"][0][0], "unterminated")
        out = gen.rewrite_config(QUOTED_BRACKET_TORN, locs, {0: ["z1"]}, None)
        self.assertEqual(out, QUOTED_BRACKET_TORN)
        self.assertEqual(len(ORPHAN_RX.findall(out)),
                         len(ORPHAN_RX.findall(QUOTED_BRACKET_TORN)))

    def test_a_closed_list_carrying_a_quoted_bracket_is_rewritten_whole(self):
        cfg = 'rules:\n  - paths: ["x/**"]\n    reviewers: ["a]b", carol]\n'
        _, locs = gen.parse_reviewer_config(cfg)
        out = gen.rewrite_config(cfg, locs, {0: ["z1", "z2"]}, None)
        self.assertIn("reviewers: [z1, z2]\n", out)
        self.assertNotIn("carol", out)
        self.assertNotIn("a]b", out)


class TestSkippedListsDoNotReachTheReport(unittest.TestCase):
    """`rewrite_config` refusing a list is only half the job: the report, the
    PR body and the default pool's anti-pile-on count all have to describe
    the bytes that were actually written, or the drift PR advertises a change
    `reviewers.new.yml` does not contain."""

    KNOBS = {"top_k": 4, "floor": 2, "min_touches": 1, "min_score": 0.0,
             "floor_min_touches": 1}

    def _reports(self, text, score, touches):
        config, locs = gen.parse_reviewer_config(text)
        return config, locs, gen.build_rule_reports(
            config, locs, score, touches, self.KNOBS)

    def test_unterminated_rule_is_reported_unchanged_with_committed_before(self):
        score = [{"zed": 9.0, "yan": 8.0}]
        touches = [{"zed": 9, "yan": 8}]
        _, _, (reports, replacements, final) = self._reports(
            UNTERMINATED_RULE, score, touches)
        self.assertEqual(replacements, {}, "a skipped list must not be written")
        self.assertFalse(reports[0]["changed"])
        self.assertTrue(reports[0]["skipped"])
        # the parity parse truncates to ["carol"]; the report must show the
        # list the file actually holds
        self.assertEqual(reports[0]["before"], ["carol", "dave"])
        self.assertEqual(reports[0]["after"], ["carol", "dave"])
        # ... and the pool's anti-pile-on count must see dave as anchored
        self.assertEqual(final, [["carol", "dave"]])

    def test_unterminated_paths_is_reported_unchanged_too(self):
        score = [{"zed": 9.0, "yan": 8.0}]
        touches = [{"zed": 9, "yan": 8}]
        _, _, (reports, replacements, final) = self._reports(
            UNTERMINATED_PATHS, score, touches)
        self.assertEqual(replacements, {})
        self.assertTrue(reports[0]["skipped"])
        self.assertEqual(reports[0]["after"], ["carol", "dave"])
        self.assertEqual(final, [["carol", "dave"]])

    def test_a_rewritable_rule_still_reports_its_change(self):
        score = [{"zed": 9.0, "yan": 8.0}]
        touches = [{"zed": 9, "yan": 8}]
        _, _, (reports, replacements, final) = self._reports(
            UNTERMINATED_POOL, score, touches)      # rule 0 here is fine
        self.assertEqual(replacements, {0: ["zed", "yan"]})
        self.assertTrue(reports[0]["changed"])
        self.assertFalse(reports[0]["skipped"])
        self.assertEqual(final, [["zed", "yan"]])

    def test_committed_reviewers_reads_the_full_multi_line_list(self):
        _, locs = gen.parse_reviewer_config(UNTERMINATED_POOL)
        self.assertEqual(
            gen.committed_reviewers(locs["default_pool"], ["alice"]),
            ["alice", "bob"])
        _, locs = gen.parse_reviewer_config(CONFIG)
        self.assertEqual(
            gen.committed_reviewers(locs["default_pool"], ["alice"]), ["alice"])

    def test_a_never_closed_sequence_does_not_swallow_the_next_rule(self):
        # No `]` anywhere: the continuation scan must give up at the next
        # node rather than reading the following rule into `before`.
        cfg = ("rules:\n"
               "  - paths: [\"x/**\"]\n"
               "    reviewers: [carol,\n"
               "  - paths: [\"y/**\"]\n"
               "    reviewers: [dave]\n")
        _, locs = gen.parse_reviewer_config(cfg)
        self.assertEqual(locs["rules"][0][0], "unterminated")
        self.assertIsNone(locs["rules"][0][2])
        self.assertEqual(gen.committed_reviewers(locs["rules"][0], ["carol"]),
                         ["carol"])
        self.assertEqual(gen.rewrite_config(cfg, locs, {0: ["z"]}, None), cfg)

    def test_a_never_closed_sequence_reports_the_parity_parse(self):
        # nothing closes it, so there is no fuller truth to report
        cfg = "default_pool: [alice,\n  bob\nrules: []\n"
        _, locs = gen.parse_reviewer_config(cfg)
        self.assertEqual(locs["default_pool"][0], "unterminated")
        self.assertIsNone(locs["default_pool"][2])
        self.assertEqual(
            gen.committed_reviewers(locs["default_pool"], ["alice"]), ["alice"])

    def test_pr_body_renders_the_skipped_row_instead_of_a_proposal(self):
        report = _pr_body_report()
        report["rules"][0]["skipped"] = True
        body = gen.build_pr_body(report)
        self.assertIn("no single-line list here for the rewrite", body)


class TestCrOnlyConfigIsANoOp(unittest.TestCase):
    """Dropping `text=True` is what keeps a CRLF config byte-faithful, but it
    also drops universal-newline handling of a lone `\r`. Parser and rewrite
    both split on `"\n"` alone, so a CR-only file arrives as ONE line: no key
    past the first is seen at indent 0, leaving zero rule buckets and a
    `default_pool` the run would still happily propose a replacement for."""

    CR_ONLY = "default_pool: [alice, bob]\rrules:\r  - paths: [\"x/**\"]\r"

    def test_the_parse_really_does_collapse(self):
        config, _ = gen.parse_reviewer_config(self.CR_ONLY)
        self.assertEqual(config["rules"], [])
        self.assertEqual(config["default_pool"], ["alice", "bob"])

    def test_main_declines_instead_of_scoring_it(self):
        env = {"GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "t",
               "DEFAULT_BRANCH": "main"}
        with tempfile.TemporaryDirectory() as td:
            env["RESULTS_DIR"] = td
            env["GITHUB_OUTPUT"] = os.path.join(td, "out")
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(gen, "read_committed_config",
                                   return_value=self.CR_ONLY), \
                 mock.patch.object(gen, "subprocess") as sp:
                self.assertEqual(gen.main(), 0)
            # refused before any git log / API call was attempted
            sp.run.assert_not_called()
            with open(env["GITHUB_OUTPUT"], encoding="utf-8") as f:
                self.assertIn("changed=false", f.read())
            self.assertFalse(os.path.exists(os.path.join(td, "report.json")))


class TestRewriteRoundTrip(unittest.TestCase):
    """The regression net both original defects trip: whatever the rewrite
    emits must still PARSE back to the lists it was asked to write, must add no
    orphaned flow tail, must keep the document's own line endings, and (with
    PyYAML available) must still be loadable YAML with the intended structure.

    The parse leg is stdlib-only and reds on the unterminated-flow defect by
    itself, so the regression does not depend on PyYAML being installed."""

    def _out(self, case):
        _, locs = gen.parse_reviewer_config(case["text"])
        return gen.rewrite_config(case["text"], locs, case["rules"], case["pool"])

    def test_output_reparses_to_the_intended_lists(self):
        for case in REWRITE_CASES:
            with self.subTest(case=case["label"]):
                got, _ = gen.parse_reviewer_config(self._out(case))
                want = case["parses_to"]
                self.assertEqual(got["default_pool"], want["default_pool"])
                self.assertEqual([r["reviewers"] for r in got["rules"]],
                                 [r["reviewers"] for r in want["rules"]])
                self.assertEqual([r["paths"] for r in got["rules"]],
                                 [r["paths"] for r in want["rules"]])

    def test_no_output_line_is_a_NEW_orphaned_flow_tail(self):
        for case in REWRITE_CASES:
            with self.subTest(case=case["label"]):
                out = self._out(case)
                self.assertEqual(len(ORPHAN_RX.findall(out)), case["orphans"],
                                 out)

    def test_line_endings_are_never_mixed_in(self):
        # A block rewrite legitimately changes the line COUNT (n reviewers in,
        # m out), so the invariant is not how many endings there are but that
        # every one of them still matches the document it came from.
        for case in REWRITE_CASES:
            with self.subTest(case=case["label"]):
                out, text = self._out(case), case["text"]
                if text.count("\n") == text.count("\r\n"):      # pure CRLF
                    self.assertEqual(out.count("\n"), out.count("\r\n"),
                                     "a bare LF leaked in")
                else:                                           # pure LF
                    self.assertEqual(out.count("\r"), 0, "a stray CR leaked in")

    @unittest.skipUnless(yaml is not None, "PyYAML not installed")
    def test_output_is_still_loadable_yaml(self):
        for case in REWRITE_CASES:
            with self.subTest(case=case["label"]):
                out = self._out(case)
                self.assertEqual(_as_parser_shape(yaml.safe_load(out)),
                                 case["loads_to"])


class TestReadCommittedConfig(unittest.TestCase):
    """The committed config is read as BYTES and decoded explicitly: with
    `text=True` Python's universal-newline translation rewrote a CRLF config to
    LF before the byte-faithful rewrite ever saw it."""

    def _repo(self, blob, path=".github/reviewers.yml"):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)

        def run(*args):
            subprocess.run(["git"] + list(args), cwd=root, check=True,
                           capture_output=True)

        run("init", "-q")
        # Pin the knobs that would otherwise normalize CRLF at commit time —
        # this test is about OUR reader, not about git's autocrlf setting.
        run("config", "core.autocrlf", "false")
        run("config", "core.eol", "lf")
        run("config", "user.email", "t@example.invalid")
        run("config", "user.name", "T")
        run("config", "commit.gpgsign", "false")
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(blob)
        run("add", "-A")
        run("-c", "core.autocrlf=false", "commit", "-q", "-m", "c")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              check=True, capture_output=True,
                              text=True).stdout.strip()
        # the generator reads refs/remotes/origin/<branch>, never the checkout
        run("update-ref", "refs/remotes/origin/main", head)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(root)
        return root

    def test_crlf_blob_comes_back_with_crlf(self):
        self._repo(b"default_pool: [a, b]\r\nrules:\r\n")
        text = gen.read_committed_config("main", ".github/reviewers.yml")
        self.assertIsNotNone(text)
        self.assertIn("\r\n", text)
        self.assertEqual(text, "default_pool: [a, b]\r\nrules:\r\n")

    def test_lf_blob_is_unchanged(self):
        self._repo(b"default_pool: [a, b]\nrules:\n")
        self.assertEqual(gen.read_committed_config("main", ".github/reviewers.yml"),
                         "default_pool: [a, b]\nrules:\n")

    def test_non_utf8_blob_is_a_noop_not_a_lossy_decode(self):
        self._repo(b"default_pool: [\xff\xfe]\n")
        self.assertIsNone(gen.read_committed_config("main", ".github/reviewers.yml"))

    def test_missing_path_is_none(self):
        self._repo(b"default_pool: [a]\n")
        self.assertIsNone(gen.read_committed_config("main", ".github/nope.yml"))

    def test_missing_branch_is_none(self):
        self._repo(b"default_pool: [a]\n")
        self.assertIsNone(gen.read_committed_config("nosuch", ".github/reviewers.yml"))


class TestEnvKnobs(unittest.TestCase):
    def _with_env(self, value, default=90):
        os.environ["_RR_TEST_KNOB"] = value
        self.addCleanup(os.environ.pop, "_RR_TEST_KNOB", None)
        return gen._env_pos_float("_RR_TEST_KNOB", default)

    def test_zero_half_life_falls_back_to_default(self):
        # half_life_days: 0 would divide by zero in decay_weight
        self.assertEqual(self._with_env("0"), 90.0)

    def test_negative_half_life_falls_back_to_default(self):
        # a negative half-life inverts the decay (older commits gain weight)
        self.assertEqual(self._with_env("-5"), 90.0)

    def test_positive_value_passes_through(self):
        self.assertEqual(self._with_env("30"), 30.0)


class TestApiErrorSentinel(unittest.TestCase):
    """A transient API failure must stay distinguishable from 'this email has
    no linked account' — collapsing the two silently drops contributors."""

    def _patch_gh_get(self, fake):
        orig = gen.gh_get
        gen.gh_get = fake
        self.addCleanup(setattr, gen, "gh_get", orig)

    def test_resolve_email_propagates_api_error(self):
        self._patch_gh_get(lambda url, token: gen.API_ERROR)
        self.assertIs(gen.resolve_email_via_api("a", "o/r", "t", "sha"),
                      gen.API_ERROR)

    def test_resolve_email_none_for_unlinked_account(self):
        self._patch_gh_get(lambda url, token: {"author": None})
        self.assertIsNone(gen.resolve_email_via_api("a", "o/r", "t", "sha"))

    def test_resolve_email_returns_login(self):
        self._patch_gh_get(lambda url, token: {"author": {"login": "octocat"}})
        self.assertEqual(gen.resolve_email_via_api("a", "o/r", "t", "sha"),
                         "octocat")

    def test_fetch_collaborators_unavailable_on_api_error(self):
        self._patch_gh_get(lambda url, token: gen.API_ERROR)
        self.assertIsNone(gen.fetch_collaborators("a", "o/r", "t"))


class TestMarkdownEscaping(unittest.TestCase):
    def test_md_code_escapes_table_breakers(self):
        # `|` would split the table cell, a backtick would close the span
        self.assertEqual(gen.md_code("a|b"), "`a\\|b`")
        self.assertEqual(gen.md_code("a`b`c"), "`a'b'c`")
        self.assertEqual(gen.md_code("plain/path.go"), "`plain/path.go`")


class TestPrBody(unittest.TestCase):
    def test_body_carries_the_contract_pieces(self):
        body = gen.build_pr_body(_pr_body_report())
        self.assertIn("new-a (9.1/20)", body)          # score/touch table
        self.assertIn("new-b\\* (1.2/3)", body)        # starred backfill
        self.assertIn("unchanged — fewer than floor qualify", body)
        self.assertIn("**3**", body)                   # unresolved-email count
        self.assertIn("docs/site", body)               # gap report
        self.assertIn("window_months=12", body)        # knob values
        self.assertIn("map_exclude=op-login", body)


class TestSharedParserCorpus(unittest.TestCase):
    """Drive the SHARED corpus through the Python port.

    `.github/assign-reviewers/parser-corpus.json` is the single fixture file
    that .github/assign-reviewers/tests/assignment.test.cjs runs through the
    JS originals (parseReviewerConfig / globToRegExp, inline in
    assign-reviewers.yml). refresh-reviewers WRITES the reviewers.yml that
    assign-reviewers READS, so the two hand-ported parsers agreeing is a
    correctness requirement — this class is what makes "parity" executable
    instead of a comment. Only the config half of parse_reviewer_config's
    (config, locations) return is compared; `locations` has no JS counterpart
    and stays covered by TestSurgicalRewrite above.

    Add a case to the corpus file, never as an inline literal here.
    """

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "assign-reviewers", "parser-corpus.json")
        with open(path, encoding="utf-8") as fh:
            cls.corpus = json.load(fh)

    def test_corpus_is_non_empty(self):
        self.assertTrue(self.corpus["configs"], "corpus has no config cases")
        self.assertTrue(self.corpus["globs"], "corpus has no glob cases")
        for entry in self.corpus["globs"]:
            self.assertTrue(entry["cases"], entry["glob"])

    def test_config_cases(self):
        for case in self.corpus["configs"]:
            with self.subTest(case["name"]):
                # stdout is swallowed only so the duplicate-key cases do not
                # emit `::warning::` annotations from a passing test job; the
                # warning itself is asserted by TestParserWarnings.
                with contextlib.redirect_stdout(io.StringIO()):
                    config, _locations = gen.parse_reviewer_config(case["text"])
                self.assertEqual(config, case["expected"])

    # Every corpus case that emits a `::warning::`, with its count. The point of an
    # EXHAUSTIVE table rather than per-case assertions is the silence: a regression
    # that made the unrecognised-key warning fire on ordinary blank lines, comment
    # lines or indented list items would warn on nearly every case here and nothing
    # else in either suite would notice. The JS port carries the same table.
    #
    # Read it in both directions. The entries that are PRESENT and non-obvious are the
    # second-BOM and leading-NEL cases: each is a document whose FIRST line is an
    # unrecognised key precisely because the stray character is not indentation, which
    # is the failure those two cases exist to pin, now with an annotation on it (they
    # reach the near-miss arm, not the terminator arm — no block is open on line 1).
    # The entries that are ABSENT matter just as much: `indented decoys and unknown
    # top-level keys are ignored` is silent because a `version:`/`notes:` key before
    # the first block ends nothing and drops nothing, and `YAML document markers are
    # not unrecognised keys` is silent because `---`/`...` are syntax. Both used to
    # warn, on documents whose expected parse right here is complete.
    CORPUS_WARNINGS = {
        "duplicate default_pool, flow then block": 1,
        "duplicate default_pool, block then flow": 1,
        "duplicate default_pool, second one empty": 1,
        "two leading BOMs: only one is stripped": 1,
        "U+0085 before a top-level key is not indentation": 1,
        "a U+00A0-only line at column 0 ends a default_pool block on both ports": 1,
        "a U+00A0-only line at column 0 drops every later rule on both ports": 1,
        "an unknown top-level key at column 0 ends the block": 1,
        "a key sharing a prefix with `rules:` is not the `rules:` key": 1,
        "no space after the colon is a plain scalar, not a key": 1,
    }

    def test_config_case_warnings_are_exactly_the_expected_set(self):
        counts = {}
        for case in self.corpus["configs"]:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                gen.parse_reviewer_config(case["text"])
            n = buf.getvalue().count("::warning::")
            if n:
                counts[case["name"]] = n
        self.assertEqual(counts, self.CORPUS_WARNINGS)

    def test_glob_cases(self):
        for entry in self.corpus["globs"]:
            compiled = gen.glob_to_regexp(entry["glob"])
            for case in entry["cases"]:
                with self.subTest(glob=entry["glob"], path=case["path"]):
                    self.assertEqual(bool(compiled.match(case["path"])),
                                     case["matches"])
                    self.assertEqual(
                        gen.matches_any(case["path"], [compiled]),
                        case["matches"])


if __name__ == "__main__":
    unittest.main()
