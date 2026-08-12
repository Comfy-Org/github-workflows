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
    catalog is folded into a <details> block, a clean run says so (that is what
    closes the sticky issue), and the same-lab review-me list collapses
    reasoning/speed tiers into one row per family, newest family first, so the
    row cap drops the oldest rather than an arbitrary tail of Cursor's print
    order (BE-6911 — see `TierCollapseTest`).

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

# The real `cursor-agent models` output captured verbatim in the 2026-08-10
# sticky issue (Comfy-Org/github-workflows#144), with the pins that run reported
# against. It is the whole reason BE-6911 exists — 178 unpinned same-lab ids, in
# which the two genuinely new families (`gpt-5.6-terra-*`, `gpt-5.6-luna-*`) sat
# below a 25-row head truncation — so the rendering fix is asserted against it
# rather than against a hand-built approximation of it.
REAL_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "catalog-2026-08-10.txt")
REAL_PANEL = ["gpt-5.6-sol-max", "claude-opus-5-thinking-max", "gemini-3.1-pro", "kimi-k3-max"]
REAL_JUDGE = "claude-opus-5-thinking-max"


def real_catalog():
    with open(REAL_CATALOG_PATH, encoding="utf-8") as handle:
        return handle.read()


def lab_rows(body, lab):
    """The rendered rows of one lab group in the same-lab review-me list."""
    section = body[body.index("## Unpinned same-lab") :]
    section = section[section.index(f"**`{lab}`** (pinned:") :]
    rows = []
    for line in section.splitlines()[1:]:
        if line.startswith("**`") or line.startswith("<details>") or line.startswith("## "):
            break
        if line.startswith("- "):
            rows.append(line)
    return rows


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
        # All three paths go through `_is_model_id`, which requires a letter
        # plus a `-`/`.` or a digit — so a bare prose word (`sonnet`) is
        # rejected at extraction: a loud checker-defect signal, not a false
        # claim about the catalog. If that rule is ever relaxed, relaxing the
        # one function relaxes every side; this test is the tripwire.
        for token in ["sonnet", "models", "available"]:
            self.assertFalse(cd._is_model_id(token), token)
            self.assertEqual(cd.catalog_entries(token + "\n"), [])
        for token in ["gpt-5.6-sol-max", "kimi-k2.7-code", "gemini-3.1-pro", "o5-pro", "o3", "gpt5"]:
            self.assertTrue(cd._is_model_id(token), token)
            self.assertEqual([m for m, _ in cd.catalog_entries(token + "\n")], [token])

    def test_a_digit_less_id_is_accepted_on_both_sides(self):
        # Cursor ships digit-less ids for real (`code-supernova`). Requiring a
        # digit dropped them in `catalog_entries` BEFORE parsing, so a newly
        # shipped digit-less family never reached the BE-4852 catch-all and the
        # run reported clean — the silence this check exists to end — and a
        # digit-less PIN hard-failed extraction every run.
        self.assertTrue(cd._is_model_id("code-supernova"))
        self.assertEqual(
            [m for m, _ in cd.catalog_entries("code-supernova\n")], ["code-supernova"]
        )
        text = WORKFLOW.replace('"kimi-k2.7-code"', '"code-supernova"')
        self.assertIn("code-supernova", cd.extract_panel_models(text))

    def test_a_digit_less_family_reaches_the_unpinned_families_catch_all(self):
        # The end-to-end of the above: a digit-less family the panel pins nothing
        # from must surface as a finding, not vanish.
        report = analyze(catalog=CATALOG + "code-supernova\n")
        families = {g["lab"]: [c["id"] for c in g["candidates"]] for g in report["unpinned_labs"]}
        self.assertEqual(families["code"], ["code-supernova"])
        self.assertTrue(report["has_findings"])

    def test_a_bare_separator_less_id_is_accepted_on_both_sides(self):
        # OpenAI ships bare o-series ids for real (`o3`). Requiring a `-`/`.`
        # dropped them in `catalog_entries` BEFORE parsing — the same pre-parse
        # silence that got the digit rule reverted, and it hit exactly the bare
        # o-series rebrand the BE-4852 catch-all was built for: the id never
        # reached it and the run reported clean.
        report = analyze(catalog=CATALOG + "o3\n")
        families = {g["lab"]: [c["id"] for c in g["candidates"]] for g in report["unpinned_labs"]}
        self.assertEqual(families["o3"], ["o3"])
        self.assertTrue(report["has_findings"])
        # …and the pin side of the shared rule accepts it too.
        text = WORKFLOW.replace("default: claude-opus-4-8-thinking-max", "default: o3")
        self.assertEqual(cd.extract_judge_model(text), "o3")

    def test_a_judge_default_the_catalog_parser_would_reject_raises(self):
        # The judge pin was the one hole in the "both sides agree" contract:
        # `extract_judge_model` checked only for whitespace/scalar markers, so a
        # default the catalog parser drops (`sonnet` — a bare digit-less word)
        # extracted cleanly and then failed `present()` — a phantom URGENT
        # "delisted pin" every Monday against a model sitting right there in
        # the catalog.
        text = WORKFLOW.replace("default: claude-opus-4-8-thinking-max", "default: sonnet")
        with self.assertRaises(cd.ExtractionError):
            cd.extract_judge_model(text)

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

    def test_a_hyphenated_prose_word_is_admitted_and_that_is_the_accepted_trade(self):
        # `gpt-based` is lowercase, hyphenated and has letters, so it parses as
        # an id: one bogus row in a never-urgent list. Screening it out by
        # requiring a digit was tried and reverted — it also dropped real
        # digit-less ids (`code-supernova`) before parsing, which is the
        # under-report direction this checker refuses (see `_is_model_id` and
        # `present`). Over-reporting a prose word is the cheaper failure, and
        # this test pins that choice so it is not silently re-tightened.
        entries = cd.catalog_entries(
            "gpt-based models are listed below\ngpt-5.6-sol-max\nself-hosted options: none\n"
        )
        self.assertEqual(
            [m for m, _ in entries], ["gpt-based", "gpt-5.6-sol-max", "self-hosted"]
        )
        # What usually matters is that the noise is inert. `gpt-based` reads as
        # lab `gpt`, so it shows up as one extra row in the same-lab review-me
        # list — a list that is explicitly "review me, not a recommendation" and
        # never `urgent` — and a token that matches no pin cannot redden a run
        # or mask one. The exception — a prose token that exactly EQUALS a pin —
        # is pinned separately below.
        report = analyze(catalog="\n".join(PANEL) + f"\n{JUDGE}\ngpt-based models are listed\n")
        self.assertFalse(report["urgent"])
        self.assertEqual(report["delisted"], [])
        self.assertEqual(
            [(g["lab"], [c["id"] for c in g["candidates"]]) for g in report["unpinned"]],
            [("gpt", ["gpt-based"])],
        )

    def test_a_prose_line_leading_with_a_pinned_id_reads_as_that_pin(self):
        # The known residual of admitting prose tokens: a catalog line whose
        # FIRST token exactly equals a pinned id ("kimi-k2.7-code was removed…")
        # is shape-indistinguishable from a listing of that id with a note, so
        # `present()` counts the pin as listed and the urgent delisted finding
        # is suppressed. Telling the two apart means interpreting the note text,
        # which this checker refuses by design for everything except NO-ZDR (see
        # `catalog_entries`) — a "removed"/"deprecated" word denylist would be
        # guessing at phrasing Cursor has never committed to, with a false match
        # crying delisted-wolf about a live pin. Pinned here so the trade stays
        # explicit rather than accidental: if Cursor ever ships prose like this,
        # the fix is note interpretation, not tighter token screening.
        catalog = CATALOG.replace(
            "kimi-k2.7-code\n", "kimi-k2.7-code was removed from the catalog\n"
        )
        report = analyze(catalog=catalog)
        self.assertEqual(report["delisted"], [])
        self.assertFalse(report["urgent"])

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

    def test_ids_from_unpinned_labs_are_not_listed_as_same_lab_candidates(self):
        # `fable-*` is a lab the panel does not pin — it belongs in the quieter
        # unpinned-families catch-all, never in the same-lab review-me list.
        report = analyze()
        self.assertNotIn("fable", [g["lab"] for g in report["unpinned"]])
        self.assertEqual(
            [g["lab"] for g in report["unpinned_labs"]], ["fable"]
        )
        self.assertEqual(
            report["unpinned_labs"][0]["candidates"], [{"id": "fable-5-max", "note": "(NO ZDR)"}]
        )

    def test_a_rebranded_family_from_a_pinned_lab_is_caught_by_the_catch_all(self):
        # The BE-4852 case: OpenAI ships `o5-pro` alongside `gpt-*`. `lab_of`
        # reads its family as `o5` (the first `-`/`.`-separated token), which no
        # pin uses, so the same-lab review-me list cannot see it — the catch-all
        # is the only thing standing between "a newer model shipped" and silence.
        report = analyze(catalog=CATALOG + "o5-pro\no5-pro-thinking\n")
        self.assertEqual(cd.lab_of("o5-pro"), "o5")
        self.assertNotIn("o5", [g["lab"] for g in report["unpinned"]])
        families = {g["lab"]: [c["id"] for c in g["candidates"]] for g in report["unpinned_labs"]}
        self.assertEqual(families["o5"], ["o5-pro", "o5-pro-thinking"])
        self.assertTrue(report["has_findings"])

    def test_an_unpinned_family_is_a_finding_but_never_urgent(self):
        # It is a standing watchlist, not breakage: it must gate the sticky issue
        # (or a rebranded family renders into a body nobody sees) but must not
        # redden the weekly run.
        catalog = "\n".join(PANEL) + "\nfable-5-max\n"
        report = analyze(catalog=catalog)
        self.assertTrue(report["has_findings"])
        self.assertFalse(report["urgent"])
        self.assertIn("unpinned famil", cd.summary_line(report))

    def test_a_catalog_of_exactly_the_pins_still_has_no_unpinned_families(self):
        # The auto-close path: the catch-all must not make `has_findings` true
        # unconditionally.
        report = analyze(catalog="\n".join(PANEL) + "\n")
        self.assertEqual(report["unpinned_labs"], [])
        self.assertFalse(report["has_findings"])

    def test_a_delisted_pins_lab_still_counts_as_pinned_for_the_catch_all(self):
        # A lab whose only pin was just delisted is emphatically still "a lab the
        # panel pins" — its successors belong in the same-lab review-me list with
        # the delisted-pin context, not demoted into the quiet fold.
        catalog = CATALOG.replace("kimi-k2.7-code\n", "kimi-k3-code\n")
        report = analyze(catalog=catalog)
        self.assertNotIn("kimi", [g["lab"] for g in report["unpinned_labs"]])
        kimi = [g for g in report["unpinned"] if g["lab"] == "kimi"][0]
        self.assertEqual([c["id"] for c in kimi["candidates"]], ["kimi-k3-code"])

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
        # `gpt-filler-1x` is a parseable same-lab id on purpose (a digit-less
        # `gpt-filler-x` is no longer admitted, which would make this exercise
        # only the raw-fold clamp and not the report above it).
        catalog = CATALOG + ("gpt-filler-1x\n" * 6000)
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("truncated", body)
        self.assertLess(len(body), 65536)

    def test_a_catalog_of_many_distinct_unpinned_families_still_fits_the_body_cap(self):
        # GitHub rejects an oversized body outright (422), so the section added
        # for BE-4852 must not be able to push the report past the clamp.
        catalog = CATALOG + "".join(f"lab{n}-9-max\n" for n in range(4000))
        report = analyze(catalog=catalog)
        self.assertGreater(len(report["unpinned_labs"]), 100)
        body = cd.render_body(report, catalog)
        self.assertLess(len(body), 65536)
        # Length alone is not the property that matters — the section must be
        # budgeted rather than sliced by the blunt `MAX_BODY_CHARS` clamp, which
        # would leave the markup unterminated (swallowing the rest of the issue
        # in GitHub's renderer) and drop the sections below it.
        self.assertEqual(body.count("<details>"), body.count("</details>"))
        self.assertIn("more", body)
        # The sections that follow the fold survive.
        self.assertIn("Raw <code>cursor-agent models</code> output", body)
        self.assertIn("Filed by the weekly", body)

    def test_the_unpinned_families_fold_names_what_it_truncated(self):
        # Silent truncation would read as "these are all the families" — the one
        # reading that could hide the rebranded family this section exists for.
        catalog = "\n".join(PANEL) + "".join(f"\nlab{n}-9-max" for n in range(cd.MAX_FAMILY_LABS + 5))
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("+5 more", body)
        self.assertEqual(body.count("<details>"), body.count("</details>"))

    def test_a_single_family_with_many_ids_is_capped_and_says_so(self):
        ids = "".join(f"\nsolo-{n}-max" for n in range(cd.MAX_FAMILY_IDS + 3))
        catalog = "\n".join(PANEL) + ids
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("… and 3 more", body)
        self.assertEqual(body.count("<details>"), body.count("</details>"))

    def test_worst_case_lists_and_notes_never_reach_the_blunt_clamp(self):
        # The row caps alone bound COUNTS, not chars: max-rows-everywhere with
        # long notes used to blow through MAX_BODY_CHARS and land in the blunt
        # `body[:N]` clamp, which slices mid-markup — exactly the corruption the
        # per-section budgets exist to prevent. The one-id-per-family test above
        # never reaches that worst case, so this one does: long notes, hundreds
        # of same-lab ids, and dozens of families × dozens of ids at once. The
        # body must come in UNDER the cap via budgeting (notes capped, lists
        # capped, families fold char-budgeted, raw fold given only the leftover
        # budget) — not via the clamp.
        note = "context " * 60  # ~480 chars, > MAX_NOTE_CHARS
        catalog = (
            CATALOG
            + "".join(f"gpt-tier-{n} {note}\n" for n in range(300))
            + "".join(f"newlab{n}-tier-{m} {note}\n" for n in range(60) for m in range(30))
        )
        report = analyze(catalog=catalog)
        body = cd.render_body(report, catalog)
        self.assertLessEqual(len(body), cd.MAX_BODY_CHARS)
        self.assertNotIn("report truncated", body)
        self.assertEqual(body.count("<details>"), body.count("</details>"))
        # The sections after the big lists survive, well-formed.
        self.assertIn("Filed by the weekly", body)
        self.assertIn("cursor-agent models", body)

    def test_the_raw_fold_shrinks_to_the_budget_the_report_left_over(self):
        # A 40K report + a 40K raw fold is 80K — over GitHub's 65536 limit even
        # though each half respects its own constant. The fold must take only
        # what MAX_BODY_CHARS has left, and the whole body must stay well-formed.
        catalog = CATALOG + "".join(f"lab{n}-9-max\n" for n in range(4000))
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertLessEqual(len(body), cd.MAX_BODY_CHARS)
        self.assertNotIn("report truncated", body)
        self.assertIn("truncated — see the workflow run log", body)

    def test_a_delisted_pins_alternatives_list_is_capped(self):
        extra = cd.MAX_FAMILY_IDS + 8
        catalog = CATALOG.replace("kimi-k2.7-code\n", "") + "".join(
            f"kimi-k{n}-code\n" for n in range(extra)
        )
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("Delisted pin", body)
        self.assertIn("+8 more", body)

    def test_an_oversized_note_is_capped_in_its_row(self):
        catalog = CATALOG + "gpt-5.7-preview " + ("x" * 1000) + "\n"
        body = cd.render_body(analyze(catalog=catalog), catalog)
        row = [ln for ln in body.splitlines() if ln.startswith("- `gpt-5.7-preview`")][0]
        self.assertLess(len(row), cd.MAX_NOTE_CHARS + 100)
        self.assertIn("…", row)

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

    def test_body_lists_unpinned_families_in_a_collapsed_section(self):
        catalog = CATALOG + "o5-pro\n"
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("unpinned model families", body)
        self.assertIn("`o5-pro`", body)
        self.assertIn("`o5`", body)
        # Collapsed, and below the same-lab review-me list it must not crowd out.
        self.assertLess(body.index("review-me list"), body.index("unpinned model families"))
        self.assertIn("<summary>Catalog ids from <b>unpinned model families</b>", body)

    def test_a_backtick_in_an_unpinned_family_note_cannot_break_out(self):
        # Same public-repo injection surface as the same-lab list — these notes
        # go through `_inline_code` too, not a bare single-backtick span.
        catalog = CATALOG + "fable-6-max `@everyone` see docs\n"
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("`` `@everyone` see docs ``", body)

    def test_clean_body_says_no_drift(self):
        catalog = "\n".join(PANEL) + "\n"
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("No drift", body)


class FooterTest(unittest.TestCase):
    """BE-6912 — the footer describes THIS issue's state, and only promises a close it can keep.

    The defect this pins: the footer told every reader "closed automatically once
    a run finds no drift" on an issue that cannot reach that state. `unpinned`
    counts toward `has_findings`, and any real catalog lists more reasoning tiers
    per pinned lab than the panel pins one of — issue #144 is the proof (178
    unpinned same-lab ids, zero delisted pins, run green, issue open forever). A
    reader taking the footer at face value concludes the drift has gone
    unaddressed for weeks and skims — on the one issue that is also where a
    delisted pin (which fails the review preflight on the very next consumer PR)
    and a NO-ZDR pin (private diffs to a model that may retain them) get
    reported.
    """

    # The exact promise that could never be kept. Nothing may reintroduce it on a
    # report that has findings, in any wording that still reads as "this closes
    # itself once the drift is dealt with".
    BROKEN_PROMISE = "closed automatically once a run finds no drift"

    def urgent_delisted(self):
        catalog = CATALOG.replace("kimi-k2.7-code\n", "kimi-k3-code\n")
        return analyze(catalog=catalog), catalog

    def urgent_zdr(self):
        catalog = CATALOG.replace("gemini-3.1-pro\n", "gemini-3.1-pro (NO ZDR)\n")
        return analyze(catalog=catalog), catalog

    def advisory(self):
        # Stock fixtures: `gpt-5.6-sol` is an unpinned same-lab id and
        # `fable-5-max` an unpinned family, so there are findings — but no pin is
        # delisted or marked NO-ZDR, and the audit date is 13 days old. This is
        # the steady state of the real check.
        return analyze(), CATALOG

    def clean(self):
        catalog = "\n".join(PANEL) + "\n"
        return analyze(catalog=catalog), catalog

    def stale_only(self):
        # The one findings state with NO standing list holding the issue open:
        # a catalog trimmed to exactly the pins, with an overdue audit date. It
        # really does close once the date is refreshed, so the footer must not
        # tell this reader the issue stays open indefinitely.
        catalog = "\n".join(PANEL) + "\n"
        return analyze(catalog=catalog, last_checked=TODAY - datetime.timedelta(days=60)), catalog

    def test_the_fixtures_map_onto_the_states_the_footer_describes(self):
        # The footer's three arms are `urgent` / findings-but-not-urgent / clean.
        # If these flags ever move, the assertions below stop testing the arm
        # they name, so pin the mapping rather than assume it.
        for name, (report, _) in [
            ("delisted", self.urgent_delisted()),
            ("zdr", self.urgent_zdr()),
        ]:
            self.assertTrue(report["urgent"], name)
            self.assertTrue(report["has_findings"], name)
        advisory, _ = self.advisory()
        self.assertFalse(advisory["urgent"])
        self.assertTrue(advisory["has_findings"])
        clean, _ = self.clean()
        self.assertFalse(clean["urgent"])
        self.assertFalse(clean["has_findings"])

    def test_an_urgent_report_tells_the_reader_to_act_now(self):
        for name, (report, catalog) in [
            ("delisted", self.urgent_delisted()),
            ("zdr", self.urgent_zdr()),
        ]:
            body = cd.render_body(report, catalog)
            footer = cd._footer(report)
            self.assertTrue(body.rstrip("\n").endswith(footer), name)
            self.assertIn("Act on this now", footer)
            self.assertIn("delisted or marked NO-ZDR", footer)
            # It must not tell the reader to wait for an auto-close that the
            # advisory list below keeps out of reach — the honest promise is
            # that repinning drops the 🚨 section on the next run.
            self.assertNotIn(self.BROKEN_PROMISE, footer)
            self.assertIn("will **not** close itself once you repin", footer)

    def test_an_advisory_report_says_nothing_is_urgent_and_that_staying_open_is_normal(self):
        report, catalog = self.advisory()
        body = cd.render_body(report, catalog)
        footer = cd._footer(report)
        self.assertTrue(body.rstrip("\n").endswith(footer))
        self.assertIn("Nothing here is urgent", footer)
        self.assertIn("stays open indefinitely", footer)
        self.assertIn("not a sign anyone is ignoring it", footer)
        # The whole point: this arm promises no close, because it never gets one.
        self.assertNotIn(self.BROKEN_PROMISE, footer)
        self.assertNotIn("closes the issue", footer)
        # And it points at how an urgent report will look instead, so a reader
        # can tell the two apart without ever seeing the other one.
        self.assertIn("act on this now", footer.lower().split("nothing here is urgent")[1])

    def test_a_findings_report_with_no_standing_list_is_not_told_it_stays_open_forever(self):
        # The mirror image of the bug being fixed: an issue that WILL close on
        # its own must not be described as permanent. Only the stale audit date
        # is open here, and refreshing it clears `has_findings`.
        report, catalog = self.stale_only()
        self.assertFalse(report["urgent"])
        self.assertTrue(report["has_findings"])
        self.assertEqual(report["unpinned"], [])
        self.assertEqual(report["unpinned_labs"], [])
        footer = cd._footer(report)
        self.assertIn("Nothing here is urgent", footer)
        self.assertNotIn("stays open indefinitely", footer)
        self.assertIn("closes itself on the first run that finds nothing at all", footer)
        self.assertIn(footer, cd.render_body(report, catalog))

    def test_only_a_clean_report_promises_a_close(self):
        report, catalog = self.clean()
        footer = cd._footer(report)
        self.assertIn("closes the issue", footer)
        self.assertIn("No drift at all", footer)
        self.assertIn(footer, cd.render_body(report, catalog))

    def test_every_arm_still_names_the_check_that_filed_it(self):
        # The one invariant across arms — a reader must always be able to find
        # which workflow to blame.
        for name, (report, _) in [
            ("delisted", self.urgent_delisted()),
            ("zdr", self.urgent_zdr()),
            ("advisory", self.advisory()),
            ("clean", self.clean()),
        ]:
            footer = cd._footer(report)
            self.assertIn("Filed by the weekly `cursor-review-catalog-drift` check", footer, name)
            # Rendered as one italic run, so it reads as a footnote rather than
            # body copy — and hard-wrapping it would break that.
            self.assertTrue(footer.startswith("_") and footer.endswith("_"), name)
            self.assertNotIn("\n", footer, name)

    def test_the_real_catalog_run_that_prompted_this_gets_the_advisory_footer(self):
        # Issue #144 itself: 178 unpinned same-lab ids, no delisted pin, no
        # NO-ZDR pin. The report that shipped the broken promise must now render
        # the advisory arm.
        catalog = real_catalog()
        report = analyze(catalog=catalog, panel=list(REAL_PANEL), judge=REAL_JUDGE)
        self.assertFalse(report["urgent"])
        self.assertTrue(report["has_findings"])
        footer = cd._footer(report)
        self.assertIn("Nothing here is urgent", footer)
        self.assertNotIn(self.BROKEN_PROMISE, footer)


class TierCollapseTest(unittest.TestCase):
    """BE-6911 — the same-lab review-me list is one row per FAMILY, newest first.

    The defect this pins: the panel pins ONE reasoning/speed tier per family, so
    every other tier of every already-pinned family is an "unpinned candidate"
    forever. The 2026-08-10 run listed 178 of them and then head-truncated each
    lab at 25 rows in Cursor's print order, which put the only two rows worth
    reading — the brand-new `gpt-5.6-terra-*` and `gpt-5.6-luna-*` families —
    inside the hidden "… and 57 more".
    """

    def test_family_of_splits_at_the_reasoning_tier(self):
        self.assertEqual(cd.family_of("gpt-5.6-terra-max-fast"), ("gpt-5.6-terra", "max-fast"))
        self.assertEqual(cd.family_of("gpt-5.6-terra-max"), ("gpt-5.6-terra", "max"))
        self.assertEqual(cd.family_of("gemini-3.6-flash-minimal"), ("gemini-3.6-flash", "minimal"))

    def test_an_id_with_no_tier_suffix_is_its_familys_default_tier(self):
        # `gpt-5.3-codex` must land in the SAME family as `gpt-5.3-codex-high`,
        # not strand itself as a family of one.
        self.assertEqual(cd.family_of("gpt-5.3-codex"), ("gpt-5.3-codex", ""))
        self.assertEqual(cd.family_of("kimi-k2.7-code"), ("kimi-k2.7-code", ""))

    def test_a_bare_fast_suffix_is_a_tier_of_its_family(self):
        # `gpt-5.3-codex-fast` is the default tier run fast — the same family.
        self.assertEqual(cd.family_of("gpt-5.3-codex-fast"), ("gpt-5.3-codex", "fast"))

    def test_the_longest_matching_tier_wins(self):
        # `extra-high` before `high`, or the family reads as `gpt-5.5-extra`.
        self.assertEqual(cd.family_of("gpt-5.5-extra-high"), ("gpt-5.5", "extra-high"))
        self.assertEqual(cd.family_of("gpt-5.5-extra-high-fast"), ("gpt-5.5", "extra-high-fast"))

    def test_thinking_is_not_collapsed_away(self):
        # The panel pins ON `-thinking` (`claude-opus-5-thinking-max` vs
        # `claude-opus-5-max`), so merging the two would hide the distinction a
        # promotion decision turns on.
        self.assertEqual(
            cd.family_of("claude-opus-5-thinking-max"), ("claude-opus-5-thinking", "max")
        )
        self.assertEqual(cd.family_of("claude-opus-5-max"), ("claude-opus-5", "max"))

    def test_collapse_groups_every_tier_into_one_row(self):
        candidates = [
            {"id": f"gpt-9.1-{tier}", "note": tier} for tier in ("low", "low-fast", "high", "max")
        ]
        groups = cd.collapse_tiers(candidates)
        self.assertEqual([g["family"] for g in groups], ["gpt-9.1"])
        self.assertEqual(len(groups[0]["members"]), 4)

    def test_collapse_orders_families_by_version_descending(self):
        candidates = [
            {"id": "gpt-5.3-codex-high", "note": ""},
            {"id": "gpt-5.6-luna-max", "note": ""},
            {"id": "gpt-5.6-terra-max", "note": ""},
            {"id": "gpt-5.4-high", "note": ""},
        ]
        # 5.6 families first (tie broken by catalog order: luna was printed
        # first), then 5.4, then 5.3 — NOT Cursor's print order, which leads
        # with 5.3.
        self.assertEqual(
            [g["family"] for g in cd.collapse_tiers(candidates)],
            ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.4", "gpt-5.3-codex"],
        )

    def test_a_versionless_family_sorts_last_rather_than_crashing(self):
        candidates = [{"id": "gpt-supernova-max", "note": ""}, {"id": "gpt-5.1-max", "note": ""}]
        self.assertEqual(
            [g["family"] for g in cd.collapse_tiers(candidates)], ["gpt-5.1", "gpt-supernova"]
        )

    def test_body_renders_one_row_per_family_naming_its_tiers(self):
        catalog = "\n".join(PANEL) + "".join(
            f"\ngpt-5.9-nova-{tier} - Nova {tier}" for tier in ("low", "high", "max", "max-fast")
        )
        body = cd.render_body(analyze(catalog=catalog), catalog)
        rows = lab_rows(body, "gpt")
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("`gpt-5.9-nova-*`", rows[0])
        self.assertIn("4 tiers", rows[0])
        for tier in ("`max`", "`max-fast`", "`high`", "`low`"):
            self.assertIn(tier, rows[0])
        # The row still reproduces ONE member's catalog note verbatim.
        self.assertIn("`gpt-5.9-nova-max`", rows[0])
        self.assertIn("Nova max", rows[0])

    def test_a_family_of_one_still_renders_as_a_plain_id_row(self):
        body = cd.render_body(analyze(), CATALOG)
        self.assertIn("- `gpt-5.6-sol`", body)

    def test_a_family_the_panel_already_pins_is_marked_as_such(self):
        # The rows that are noise by construction: the panel pins this family,
        # just at another tier.
        body = cd.render_body(analyze(), CATALOG)
        self.assertIn("panel already pins `gpt-5.6-sol-max`", body)

    def test_a_collapsed_row_reproduces_a_no_zdr_marker_verbatim(self):
        catalog = "\n".join(PANEL) + (
            "\ngpt-5.9-nova-low - Nova Low (NO ZDR)\ngpt-5.9-nova-max - Nova Max (NO ZDR)"
        )
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertIn("(NO ZDR)", lab_rows(body, "gpt")[0])

    def test_collapsed_rows_are_capped_and_name_what_they_dropped(self):
        # Silent truncation reads as "that is all of them" — the exact misreading
        # this ticket exists to end.
        ids = "".join(
            f"\ngpt-{n}.0-fam-max - fam {n}\ngpt-{n}.0-fam-low - fam {n} low"
            for n in range(cd.MAX_FAMILY_IDS + 3)
        )
        catalog = "\n".join(PANEL) + ids
        body = cd.render_body(analyze(catalog=catalog), catalog)
        rows = lab_rows(body, "gpt")
        self.assertEqual(len(rows), cd.MAX_FAMILY_IDS + 1)
        self.assertIn("… and 3 older families (6 ids)", rows[-1])
        # And what it dropped is the OLDEST, not an arbitrary tail: `gpt-0.0-*`
        # through `gpt-2.0-*` are the three lowest versions.
        self.assertIn("`gpt-27.0-fam-*`", rows[0])
        self.assertNotIn("gpt-0.0-fam", body[: body.index("<details>")])

    def test_a_brand_new_family_printed_last_is_visible_without_the_raw_fold(self):
        # The BE-6911 shape in miniature: 40 older tiers printed first, the new
        # family printed last. Pre-change it fell inside the hidden remainder.
        older = "".join(
            f"\ngpt-5.0-old{n}-{tier} - old {n} {tier}"
            for n in range(20)
            for tier in ("low", "max")
        )
        catalog = "\n".join(PANEL) + older + "\ngpt-9.9-brandnew-max - Brand New Max"
        body = cd.render_body(analyze(catalog=catalog), catalog)
        rows = lab_rows(body, "gpt")
        # 41 candidate ids — pre-change the 25-row cap hid the last 16, this one
        # among them. Collapsed and version-ordered it leads the group.
        self.assertEqual(sum(len(g["candidates"]) for g in analyze(catalog=catalog)["unpinned"]), 41)
        self.assertIn("`gpt-9.9-brandnew-max`", rows[0])
        self.assertNotIn("… and", "\n".join(rows))

    def test_the_2026_08_10_catalog_surfaces_both_new_families_in_full(self):
        # The acceptance case, against the catalog captured verbatim in #144.
        catalog = real_catalog()
        report = cd.analyze(
            REAL_PANEL, REAL_JUDGE, catalog, datetime.date(2026, 7, 28), datetime.date(2026, 8, 10), 30
        )
        # The finding itself is unchanged — 178 unpinned same-lab ids, still not
        # urgent (no pin delisted, none marked NO-ZDR). Only the rendering moved.
        self.assertEqual(sum(len(g["candidates"]) for g in report["unpinned"]), 178)
        self.assertFalse(report["urgent"])
        self.assertIn("178 unpinned same-lab ids", cd.summary_line(report))

        body = cd.render_body(report, catalog)
        rendered = body[: body.index("<details>")]
        rows = lab_rows(body, "gpt")
        # 82 gpt ids collapse to 11 family rows — inside the cap, so no
        # "… and N more" line, so nothing is hidden behind the raw fold.
        self.assertEqual(len(rows), 11, rows)
        self.assertLessEqual(len(rows), cd.MAX_FAMILY_IDS)
        self.assertNotIn("… and", "\n".join(rows))
        # Both new families are visible, and above the older ones the pre-change
        # report spent its 25 visible rows on.
        self.assertIn("`gpt-5.6-terra-*`", rendered)
        self.assertIn("`gpt-5.6-luna-*`", rendered)
        self.assertLess(rendered.index("gpt-5.6-terra"), rendered.index("gpt-5.3-codex"))
        self.assertLess(rendered.index("gpt-5.6-luna"), rendered.index("gpt-5.2-"))
        # Every lab group fits, so the whole 178 collapse to well under the cap.
        for lab in ("claude", "gemini", "kimi"):
            self.assertNotIn("… and", "\n".join(lab_rows(body, lab)), lab)

    def test_worst_case_collapsed_rows_still_leave_room_for_the_raw_fold(self):
        # The collapse adds a tier list to every row, so it has to be held to
        # roughly the char cost of the id row it replaces — otherwise the report
        # sections crowd out the raw catalog fold and reach the blunt clamp.
        note = "context " * 60  # ~480 chars, > MAX_NOTE_CHARS
        tiers = [t for suffix in cd.TIER_SUFFIXES for t in (suffix, suffix + "-fast")] + ["fast"]
        lines = list(PANEL)
        for lab in ("gpt", "claude", "gemini", "kimi"):
            for n in range(cd.MAX_FAMILY_IDS + 5):
                for tier in tiers:
                    lines.append(f"{lab}-9.{n}-fam-{tier} {note}")
        catalog = "\n".join(lines) + "\n"
        body = cd.render_body(analyze(catalog=catalog), catalog)
        self.assertLessEqual(len(body), cd.MAX_BODY_CHARS)
        self.assertNotIn("report truncated", body)
        self.assertEqual(body.count("<details>"), body.count("</details>"))
        self.assertIn("Raw <code>cursor-agent models</code> output", body)
        self.assertIn("Filed by the weekly", body)


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
