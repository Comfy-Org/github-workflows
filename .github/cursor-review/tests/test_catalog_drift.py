#!/usr/bin/env python3
"""Regression tests for catalog-drift.py.

The weekly drift check (BE-4819) is only worth having if it is right about three
things, so these tests pin exactly those:

  * **extraction** — the pins are read OUT of cursor-review.yml (panel heredoc +
    `judge_model` default). A duplicated list would rot; a silently-failed
    extraction would report "no drift" forever, so extraction failure must be a
    loud non-zero exit, and the extractors are also run against the REAL
    workflow file in this repo so a refactor there can't quietly blind the check.
  * **comparison** — delisted pins use the preflight's whole-token match (so
    `kimi-k2.7` does not "match" inside `kimi-k2.75`), unpinned same-lab ids are
    grouped by the labs the pins actually use, and the audit date goes stale at
    the threshold, not before.
  * **reporting** — NO-ZDR markers survive into the body verbatim, the raw
    catalog is folded into a <details> block, and a clean run says so (that is
    what closes the sticky issue).

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import datetime
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

# catalog-drift.py has a hyphen, so import it by path rather than `import`.
_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "catalog-drift.py")
_spec = importlib.util.spec_from_file_location("catalog_drift", _MODULE_PATH)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_REAL_WORKFLOW = os.path.join(_REPO_ROOT, ".github", "workflows", "cursor-review.yml")

WORKFLOW = """\
on:
  workflow_call:
    inputs:
      judge_model:
        description: >-
          Single judge model.
        type: string
        required: false
        default: claude-opus-4-8-thinking-max
      diff_size_cap:
        type: number
        default: 5000

jobs:
  preflight:
    steps:
      - name: Define panel models
        id: models
        # Pinned to each lab's newest highest-reasoning tier in Cursor's
        # catalog (last checked 2026-07-14).
        run: |
          cat > /tmp/models.json <<'JSON'
          [
            "gpt-5.6-sol-max",
            "claude-opus-4-8-thinking-max",
            "gemini-3.1-pro",
            "kimi-k2.7-code"
          ]
          JSON
          echo "models=$(jq -c . /tmp/models.json)" >> "$GITHUB_OUTPUT"
"""

CATALOG = """\
gpt-5.6-sol-max
gpt-5.6-sol
claude-opus-4-8-thinking-max
gemini-3.1-pro
kimi-k2.7-code
fable-5-max (NO ZDR)
"""

PANEL = ["gpt-5.6-sol-max", "claude-opus-4-8-thinking-max", "gemini-3.1-pro", "kimi-k2.7-code"]
JUDGE = "claude-opus-4-8-thinking-max"
TODAY = datetime.date(2026, 7, 27)


def analyze(catalog=CATALOG, panel=None, judge=JUDGE, last_checked=datetime.date(2026, 7, 14), today=TODAY):
    return cd.analyze(
        list(PANEL) if panel is None else panel, judge, catalog, last_checked, today, cd.DEFAULT_STALE_DAYS
    )


class ExtractionTest(unittest.TestCase):
    def test_extracts_panel_models_from_the_heredoc(self):
        self.assertEqual(cd.extract_panel_models(WORKFLOW), PANEL)

    def test_extracts_judge_model_default(self):
        self.assertEqual(cd.extract_judge_model(WORKFLOW), JUDGE)

    def test_judge_extraction_ignores_a_later_inputs_default(self):
        # `diff_size_cap`'s default must never be mistaken for judge_model's.
        self.assertNotEqual(cd.extract_judge_model(WORKFLOW), "5000")

    def test_extracts_last_checked_date(self):
        self.assertEqual(cd.extract_last_checked(WORKFLOW), datetime.date(2026, 7, 14))

    def test_missing_last_checked_is_none_not_an_error(self):
        self.assertIsNone(cd.extract_last_checked("no audit comment here"))

    def test_last_checked_is_read_near_the_pins_not_the_first_match_anywhere(self):
        # An unrelated `last checked` elsewhere in the file must not shadow the
        # real pin-adjacent audit date.
        preamble = "# changelog: last checked 2020-01-01\n" + "#\n" * (cd.LAST_CHECKED_WINDOW + 5)
        self.assertEqual(cd.extract_last_checked(preamble + WORKFLOW), datetime.date(2026, 7, 14))

    def test_last_checked_far_from_the_pins_is_not_adopted(self):
        text = WORKFLOW.replace(
            "        # catalog (last checked 2026-07-14).",
            "\n".join(["# stray: last checked 2020-01-01"] + [""] * cd.LAST_CHECKED_WINDOW),
        )
        self.assertIsNone(cd.extract_last_checked(text))

    def test_heredoc_with_a_trailing_comma_falls_back_to_quoted_tokens(self):
        text = WORKFLOW.replace('"kimi-k2.7-code"', '"kimi-k2.7-code",')
        self.assertEqual(cd.extract_panel_models(text), PANEL)

    def test_quoted_token_fallback_rejects_a_quoted_non_id(self):
        # The fallback must not adopt a quoted comment as a "pin": a guessed pin
        # set means false delisted alarms every Monday, or a real pin silently
        # dropped and then never monitored.
        text = WORKFLOW.replace(
            '            "kimi-k2.7-code"\n',
            '            "kimi-k2.7-code",  # see "the Kimi tier" in the catalog\n',
        )
        with self.assertRaises(cd.ExtractionError):
            cd.extract_panel_models(text)

    def test_pin_and_catalog_id_shape_rules_agree(self):
        # The pin validator and the catalog parser MUST accept the same shapes:
        # a pin the catalog parser would never emit reads as delisted every run.
        # Both currently require a `-`/`.`, so a separator-less id (`o3`) is
        # rejected at extraction — a loud checker-defect signal, not a false
        # claim about the catalog. If that rule is ever relaxed, relax it in
        # both places; this test is the tripwire.
        for token in ["o3", "sonnet", "gpt5"]:
            self.assertFalse(cd._is_model_id(token), token)
            self.assertEqual(cd.catalog_entries(token + "\n"), [])
        for token in ["gpt-5.6-sol-max", "kimi-k2.7-code", "gemini-3.1-pro"]:
            self.assertTrue(cd._is_model_id(token), token)
            self.assertEqual([m for m, _ in cd.catalog_entries(token + "\n")], [token])

    def test_valid_json_with_a_placeholder_entry_raises(self):
        # Strict JSON is not enough — a placeholder would be "checked" as a pin.
        text = WORKFLOW.replace(
            '            "kimi-k2.7-code"\n',
            '            "kimi-k2.7-code",\n            "TODO pick a fifth"\n',
        )
        with self.assertRaises(cd.ExtractionError):
            cd.extract_panel_models(text)

    def test_missing_heredoc_raises(self):
        with self.assertRaises(cd.ExtractionError):
            cd.extract_panel_models("jobs:\n  preflight:\n    steps: []\n")

    def test_unterminated_heredoc_raises(self):
        text = WORKFLOW.replace("          JSON\n", "")
        with self.assertRaises(cd.ExtractionError):
            cd.extract_panel_models(text)

    def test_judge_default_with_an_inline_comment_is_trimmed(self):
        text = WORKFLOW.replace(
            "default: claude-opus-4-8-thinking-max",
            "default: claude-opus-4-8-thinking-max  # top ZDR-eligible tier",
        )
        self.assertEqual(cd.extract_judge_model(text), JUDGE)

    def test_folded_judge_default_raises_instead_of_pinning_a_scalar_marker(self):
        # `>-` would sail through as a "pin" and report delisted every run.
        text = WORKFLOW.replace(
            "        default: claude-opus-4-8-thinking-max\n",
            "        default: >-\n          claude-opus-4-8-thinking-max\n",
        )
        with self.assertRaises(cd.ExtractionError):
            cd.extract_judge_model(text)

    def test_missing_judge_default_raises(self):
        text = WORKFLOW.replace("        default: claude-opus-4-8-thinking-max\n", "")
        with self.assertRaises(cd.ExtractionError):
            cd.extract_judge_model(text)

    def test_extraction_works_against_the_real_cursor_review_workflow(self):
        # The whole design rests on reading the pins out of the live workflow
        # file; if a refactor there breaks these anchors, fail HERE (in a cheap
        # unit run) rather than silently reporting "no drift" every Monday.
        with open(_REAL_WORKFLOW, encoding="utf-8") as handle:
            real = handle.read()
        panel = cd.extract_panel_models(real)
        self.assertTrue(panel, "no panel models extracted from the real workflow")
        self.assertTrue(all(m and " " not in m for m in panel), panel)
        judge = cd.extract_judge_model(real)
        self.assertTrue(judge and " " not in judge, judge)
        self.assertIsNotNone(
            cd.extract_last_checked(real),
            "the real workflow lost its `last checked YYYY-MM-DD` audit comment",
        )


class CatalogParsingTest(unittest.TestCase):
    def test_parses_ids_and_notes(self):
        entries = dict(cd.catalog_entries(CATALOG))
        self.assertIn("gpt-5.6-sol", entries)
        self.assertEqual(entries["fable-5-max"], "(NO ZDR)")

    def test_a_numbered_list_marker_is_not_mistaken_for_an_id(self):
        # `1.` satisfies "lowercase token with a separator", so without the
        # letter requirement a numbered catalog format would parse as the ids
        # ['1.', '2.'] — a catalog that looks valid while every real pin reads
        # as delisted. Parsing nothing is the better failure: `main` turns it
        # into the diagnostic "the format may have changed" hard exit.
        self.assertEqual(cd.catalog_entries("1. gpt-5.6-sol-max\n2. kimi-k2.7-code\n"), [])

    def test_skips_prose_and_bullets(self):
        entries = cd.catalog_entries("Available models:\n\n  - gpt-5.6-sol-max\n  * kimi-k2.7-code\n")
        self.assertEqual([m for m, _ in entries], ["gpt-5.6-sol-max", "kimi-k2.7-code"])

    def test_dedupes_repeated_ids(self):
        entries = cd.catalog_entries("gpt-5.6-sol\ngpt-5.6-sol (NO ZDR)\n")
        self.assertEqual(entries, [("gpt-5.6-sol", "(NO ZDR)")])

    def test_a_later_no_zdr_note_on_a_repeated_id_is_not_discarded(self):
        # Keep-first would drop the one marker a human must see.
        entries = dict(cd.catalog_entries("gpt-5.6-sol 200k ctx\ngpt-5.6-sol (NO ZDR)\n"))
        self.assertIn("(NO ZDR)", entries["gpt-5.6-sol"])
        self.assertIn("200k ctx", entries["gpt-5.6-sol"])

    def test_repeated_identical_notes_are_not_duplicated(self):
        entries = dict(cd.catalog_entries("gpt-5.6-sol (NO ZDR)\ngpt-5.6-sol (NO ZDR)\n"))
        self.assertEqual(entries["gpt-5.6-sol"], "(NO ZDR)")

    def test_present_matches_parsed_ids_exactly(self):
        ids = {m for m, _ in cd.catalog_entries(CATALOG)}
        self.assertTrue(cd.present("kimi-k2.7-code", ids))
        self.assertFalse(cd.present("kimi-k2.7", ids))
        self.assertFalse(cd.present("gpt-5.6-sol-max-plus", ids))

    def test_lab_of_splits_on_the_first_separator(self):
        self.assertEqual(cd.lab_of("gpt-5.6-sol-max"), "gpt")
        self.assertEqual(cd.lab_of("kimi-k2.7-code"), "kimi")


class AnalyzeTest(unittest.TestCase):
    def test_clean_catalog_with_no_extras_has_no_findings(self):
        catalog = "\n".join(PANEL) + "\n"
        report = analyze(catalog=catalog)
        self.assertFalse(report["has_findings"])
        self.assertFalse(report["urgent"])
        self.assertEqual(cd.summary_line(report), "no drift")

    def test_delisted_pin_is_urgent_and_names_same_lab_alternatives(self):
        catalog = CATALOG.replace("kimi-k2.7-code\n", "kimi-k3-code\n")
        report = analyze(catalog=catalog)
        self.assertTrue(report["urgent"])
        delisted = report["delisted"]
        self.assertEqual([d["id"] for d in delisted], ["kimi-k2.7-code"])
        self.assertEqual(delisted[0]["roles"], ["panel"])
        self.assertEqual(delisted[0]["same_lab_available"], ["kimi-k3-code"])

    def test_delisted_judge_pin_is_reported_with_its_role(self):
        catalog = "gpt-5.6-sol-max\ngemini-3.1-pro\nkimi-k2.7-code\n"
        report = analyze(catalog=catalog)
        self.assertEqual([d["id"] for d in report["delisted"]], [JUDGE])
        # The judge default is also a panel pin here, so both roles show.
        self.assertEqual(report["delisted"][0]["roles"], ["panel", "judge"])

    def test_delisted_pin_mentioned_only_in_a_successor_note_is_still_reported(self):
        # The regression the raw-text scan had: a delist normally ships WITH a
        # note naming the id it replaces, so scanning the text found the dead pin
        # and reported "no drift" while consumer PRs went red at preflight.
        catalog = CATALOG.replace(
            "kimi-k2.7-code\n", "kimi-k3-code (replaces kimi-k2.7-code)\n"
        )
        report = analyze(catalog=catalog)
        self.assertTrue(report["urgent"])
        self.assertEqual([d["id"] for d in report["delisted"]], ["kimi-k2.7-code"])

    def test_a_pinned_model_marked_no_zdr_is_urgent(self):
        catalog = CATALOG.replace("gemini-3.1-pro\n", "gemini-3.1-pro (NO ZDR)\n")
        report = analyze(catalog=catalog)
        self.assertEqual([z["id"] for z in report["zdr_risk"]], ["gemini-3.1-pro"])
        self.assertEqual(report["zdr_risk"][0]["roles"], ["panel"])
        self.assertFalse(report["delisted"])
        self.assertTrue(report["urgent"])
        self.assertIn("NO-ZDR", cd.summary_line(report))

    def test_an_unmarked_pin_is_not_a_zdr_risk(self):
        catalog = "\n".join(PANEL) + "\n"
        self.assertEqual(analyze(catalog=catalog)["zdr_risk"], [])

    def test_a_no_zdr_marker_on_an_unpinned_id_is_not_urgent(self):
        # `fable-5-max (NO ZDR)` is in CATALOG but not pinned — it belongs in the
        # raw fold, and must not redden the weekly run.
        self.assertEqual(analyze()["zdr_risk"], [])
        self.assertFalse(analyze()["urgent"])

    def test_judge_pin_outside_the_panel_is_still_checked(self):
        report = analyze(judge="claude-judge-only")
        self.assertEqual([d["id"] for d in report["delisted"]], ["claude-judge-only"])
        self.assertEqual(report["delisted"][0]["roles"], ["judge"])

    def test_unpinned_same_lab_ids_are_grouped_and_listed(self):
        report = analyze()
        groups = {g["lab"]: g for g in report["unpinned"]}
        self.assertEqual([c["id"] for c in groups["gpt"]["candidates"]], ["gpt-5.6-sol"])
        self.assertTrue(report["has_findings"])
        self.assertFalse(report["urgent"])

    def test_ids_from_unpinned_labs_are_not_listed_as_candidates(self):
        # `fable-*` is a lab the panel does not pin — it belongs in the raw
        # catalog fold, not in the review-me list.
        report = analyze()
        self.assertNotIn("fable", [g["lab"] for g in report["unpinned"]])

    def test_a_newly_shipped_same_lab_model_shows_up(self):
        report = analyze(catalog=CATALOG + "claude-opus-5-thinking-max\n")
        claude = [g for g in report["unpinned"] if g["lab"] == "claude"][0]
        self.assertEqual([c["id"] for c in claude["candidates"]], ["claude-opus-5-thinking-max"])

    def test_audit_date_is_stale_only_past_the_threshold(self):
        catalog = "\n".join(PANEL) + "\n"
        fresh = analyze(catalog=catalog, last_checked=TODAY - datetime.timedelta(days=30))
        self.assertFalse(fresh["audit"]["stale"])
        self.assertFalse(fresh["has_findings"])
        stale = analyze(catalog=catalog, last_checked=TODAY - datetime.timedelta(days=31))
        self.assertTrue(stale["audit"]["stale"])
        self.assertTrue(stale["has_findings"])
        self.assertFalse(stale["urgent"])

    def test_a_future_dated_audit_comment_counts_as_stale_not_fresh(self):
        # A typo'd future date yields a negative age, which read as "fresh" and
        # suppressed the alert until ~stale_days past that future date.
        report = analyze(
            catalog="\n".join(PANEL) + "\n", last_checked=TODAY + datetime.timedelta(days=400)
        )
        self.assertTrue(report["audit"]["stale"])
        self.assertTrue(report["audit"]["future_dated"])
        self.assertTrue(report["has_findings"])
        body = cd.render_body(report, "\n".join(PANEL) + "\n")
        self.assertIn("future", body.lower())

    def test_missing_audit_date_counts_as_stale(self):
        report = analyze(catalog="\n".join(PANEL) + "\n", last_checked=None)
        self.assertTrue(report["audit"]["stale"])
        self.assertIn("no audit date", cd.summary_line(report))


class RenderTest(unittest.TestCase):
    def test_title_carries_the_sticky_prefix(self):
        title = cd.issue_title(analyze())
        self.assertTrue(title.startswith(cd.STICKY_TITLE_PREFIX), title)

    def test_body_reports_delisted_pins_prominently(self):
        catalog = CATALOG.replace("kimi-k2.7-code\n", "kimi-k3-code\n")
        body = cd.render_body(analyze(catalog=catalog), catalog, "https://example/run")
        self.assertIn("Delisted pin", body)
        self.assertIn("`kimi-k2.7-code`", body)
        self.assertIn("`kimi-k3-code`", body)

    def test_body_surfaces_no_zdr_markers_verbatim(self):
        catalog = CATALOG + "gpt-5.7-preview (NO ZDR)\n"
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("gpt-5.7-preview", body)
        self.assertIn("(NO ZDR)", body)
        self.assertIn("review-me list, not a recommendation", body)

    def test_body_folds_the_raw_catalog_into_details(self):
        body = cd.render_body(analyze(), CATALOG)
        self.assertIn("<details>", body)
        self.assertIn("fable-5-max (NO ZDR)", body)

    def test_body_fence_survives_backticks_in_the_catalog(self):
        catalog = CATALOG + "```\n"
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("````text", body)

    def test_oversized_catalog_is_truncated(self):
        catalog = CATALOG + ("gpt-filler-x\n" * 6000)
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("truncated", body)
        self.assertLess(len(body), 65536)

    def test_a_backtick_in_a_catalog_note_cannot_break_out_of_its_code_span(self):
        # Notes are unconstrained third-party text reproduced in a bot-authored
        # issue in a PUBLIC repo; a bare single-backtick span would let one inject
        # markdown or an @mention that notifies real people.
        catalog = CATALOG + "gpt-5.7-preview `@everyone` see docs\n"
        body = cd.render_body(analyze(catalog=catalog), catalog)
        # Two-backtick delimiter, padded because the note itself starts with one.
        self.assertIn("`` `@everyone` see docs ``", body)
        self.assertNotIn("- `gpt-5.7-preview` — `` `@everyone`` see", body)

    def test_a_pinned_no_zdr_marker_is_called_out_in_the_body(self):
        catalog = CATALOG.replace("gemini-3.1-pro\n", "gemini-3.1-pro (NO ZDR)\n")
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("NO-ZDR", body)
        self.assertIn("`gemini-3.1-pro`", body)
        self.assertIn("confidentiality regression", body)

    def test_clean_body_says_no_drift(self):
        catalog = "\n".join(PANEL) + "\n"
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("No drift", body)


class MainTest(unittest.TestCase):
    def _run(self, workflow_text, catalog_text, extra=None):
        tmp = tempfile.mkdtemp()
        wf = os.path.join(tmp, "cursor-review.yml")
        cat = os.path.join(tmp, "catalog.txt")
        with open(wf, "w", encoding="utf-8") as handle:
            handle.write(workflow_text)
        with open(cat, "w", encoding="utf-8") as handle:
            handle.write(catalog_text)
        argv = [
            "--workflow", wf,
            "--catalog", cat,
            "--now", "2026-07-27T06:17:00",
            "--title-out", os.path.join(tmp, "title.txt"),
            "--body-out", os.path.join(tmp, "body.md"),
            "--json-out", os.path.join(tmp, "report.json"),
        ] + (extra or [])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cd.main(argv)
        return code, tmp, buf.getvalue()

    def test_end_to_end_reports_findings_and_writes_outputs(self):
        code, tmp, _ = self._run(WORKFLOW, CATALOG)
        self.assertEqual(code, 0)
        with open(os.path.join(tmp, "report.json"), encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertTrue(report["has_findings"])
        self.assertFalse(report["urgent"])
        with open(os.path.join(tmp, "title.txt"), encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith(cd.STICKY_TITLE_PREFIX))
        self.assertTrue(os.path.getsize(os.path.join(tmp, "body.md")) > 0)

    def test_unreadable_pins_exit_non_zero_instead_of_reporting_clean(self):
        code, _, out = self._run("nothing useful here\n", CATALOG)
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)

    def test_empty_catalog_exits_non_zero(self):
        code, _, out = self._run(WORKFLOW, "\n")
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)

    def test_unparseable_catalog_exits_non_zero_instead_of_crying_wolf(self):
        # `cursor-agent models` exits 1 (and prints nothing to stdout) when it
        # can't authenticate, so the workflow's own guard catches that case —
        # this is the belt to that suspenders: a catalog that IS non-empty but
        # yields no ids must not be reported as "every pin is delisted".
        code, _, out = self._run(WORKFLOW, "Error: Authentication required.\n")
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)
        self.assertNotIn("delisted", out.lower())

    def test_an_unparseable_catalog_cannot_smuggle_a_workflow_command_into_the_log(self):
        # `cursor-agent` output is semi-trusted and this repo is public: a line
        # beginning `::` echoed raw would be EXECUTED by the runner.
        code, _, out = self._run(WORKFLOW, "Error page\n::add-mask::hunter2\n")
        self.assertEqual(code, 1)
        self.assertNotIn("\n::add-mask::", out)
        self.assertIn("| ::add-mask::hunter2", out)

    def test_github_output_receives_the_flags(self):
        tmp = tempfile.mkdtemp()
        out_file = os.path.join(tmp, "gh_output")
        os.environ["GITHUB_OUTPUT"] = out_file
        try:
            catalog = CATALOG.replace("kimi-k2.7-code\n", "kimi-k3-code\n")
            code, _, _ = self._run(WORKFLOW, catalog)
        finally:
            del os.environ["GITHUB_OUTPUT"]
        self.assertEqual(code, 0)
        with open(out_file, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn("has_findings=true", written)
        self.assertIn("urgent=true", written)


if __name__ == "__main__":
    unittest.main()
