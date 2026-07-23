#!/usr/bin/env python3
"""Tests for the refresh-reviewers generator (BE-4116).

Pure-python coverage of the scoring/rewrite core: glob-semantics parity with
assign-reviewers.yml's globToRegExp, decay math, threshold/floor/backfill
selection (including the under-floor leave-unchanged case), bot/generated-path/
rename-syntax filtering, noreply-email decoding, and the surgical rewrite
preserving every byte outside the edited lists. No network, no git.

Run: python3 -m unittest discover -s .github/refresh-reviewers/tests -p 'test_*.py' -v
"""

import importlib.util
import os
import re
import unittest

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
        report = {
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
        }
        body = gen.build_pr_body(report)
        self.assertIn("new-a (9.1/20)", body)          # score/touch table
        self.assertIn("new-b\\* (1.2/3)", body)        # starred backfill
        self.assertIn("unchanged — fewer than floor qualify", body)
        self.assertIn("**3**", body)                   # unresolved-email count
        self.assertIn("docs/site", body)               # gap report
        self.assertIn("window_months=12", body)        # knob values
        self.assertIn("map_exclude=op-login", body)


if __name__ == "__main__":
    unittest.main()
