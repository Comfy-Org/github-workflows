#!/usr/bin/env python3
"""Tests for aggregate-panel.py — which panel cells count toward the gate.

The direct-API OpenAI cells (`findings-direct-<review_type>-<model>`) gate only
when they REPLACE the Cursor OpenAI lane; side by side they stay advisory. A
regression in either direction is invisible in a diff: counting advisory cells
lets a comparison run withhold approval, and NOT counting replacing cells
takes the OpenAI lane out of the gate, so a failed one costs nothing against
`approve_max_failed_reviewers`. The workflow wiring that feeds this script its
flags is executed in test_workflow_panel_integrity.py.

Also pins the two downstream readers of the markers this script sets: the
`direct` tag in post-review.py's "did not contribute" line, and auto-approve's
`panel_gate` counting a failed direct cell like any other.

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "aggregate-panel.py")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AGG = _load("aggregate_panel", "aggregate-panel.py")
PR = _load("post_review", "post-review.py")
AA = _load("auto_approve", "auto-approve.py")

CURSOR_MODELS = ["claude-opus", "kimi"]
DIRECT = "gpt-5.6-sol"


def cell(model, review_type, status="ok", **extra):
    return {"model": model, "review_type": review_type, "status": status, "findings": [], **extra}


class AggregateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, artifact, record):
        os.makedirs(os.path.join(self.dir, artifact), exist_ok=True)
        with open(os.path.join(self.dir, artifact, "findings.json"), "w", encoding="utf-8") as f:
            json.dump(record, f)

    def full_cursor_panel(self):
        for model in CURSOR_MODELS:
            for review_type in AGG.REVIEW_TYPES:
                self.write(f"findings-{review_type}-{model}", cell(model, review_type))

    def direct(self, review_type, status="ok", **extra):
        self.write(f"findings-direct-{review_type}-{DIRECT}", cell(DIRECT, review_type, status, **extra))

    def run_agg(self, counts):
        return AGG.aggregate(self.dir, CURSOR_MODELS, DIRECT, counts)

    def test_replace_true_both_direct_ok_are_counted(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        self.direct("edge-case")
        cells, ok, total, _ = self.run_agg(True)
        self.assertEqual((ok, total), (6, 6))
        direct = [c for c in cells if c.get("direct_api")]
        self.assertEqual(sorted(c["review_type"] for c in direct), ["adversarial", "edge-case"])
        self.assertFalse(any(c.get("advisory") for c in cells))

    def test_replace_true_one_direct_error_counts_as_failed(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        self.direct("edge-case", status="error")
        _, ok, total, _ = self.run_agg(True)
        self.assertEqual((ok, total), (5, 6))

    def test_replace_true_missing_direct_artifact_is_synthesised_as_error(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        cells, ok, total, _ = self.run_agg(True)
        self.assertEqual((ok, total), (5, 6))
        missing = [c for c in cells if c.get("direct_api") and c["review_type"] == "edge-case"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["status"], "error")
        self.assertEqual(missing[0]["model"], DIRECT)

    def test_replace_false_direct_cells_are_advisory_and_uncounted(self):
        self.full_cursor_panel()
        self.direct("adversarial", status="error")
        self.direct("edge-case")
        cells, ok, total, _ = self.run_agg(False)
        self.assertEqual((ok, total), (4, 4))
        direct = [c for c in cells if c.get("direct_api")]
        self.assertEqual(len(direct), 2)
        self.assertTrue(all(c.get("advisory") is True for c in direct))

    def test_replace_false_missing_direct_cell_is_not_synthesised(self):
        self.full_cursor_panel()
        cells, ok, total, _ = self.run_agg(False)
        self.assertEqual((ok, total), (4, 4))
        self.assertFalse(any(c.get("direct_api") for c in cells))

    def test_advisory_direct_cell_cannot_mask_a_missing_cursor_cell(self):
        # Side by side the Cursor cell may share the direct id; the direct
        # record must not stand in for a Cursor cell that never uploaded.
        self.write("findings-adversarial-claude-opus", cell("claude-opus", "adversarial"))
        self.write(f"findings-direct-edge-case-{DIRECT}", cell("claude-opus", "edge-case"))
        cells, ok, total, _ = AGG.aggregate(self.dir, ["claude-opus"], DIRECT, False)
        self.assertEqual((ok, total), (1, 2))

    def test_a_cell_cannot_set_its_own_markers(self):
        self.write("findings-adversarial-kimi", cell("kimi", "adversarial", "error", advisory=True, direct_api=True))
        self.direct("adversarial", status="error", advisory=True)
        cells, ok, total, _ = AGG.aggregate(self.dir, ["kimi"], DIRECT, True)
        cursor = [c for c in cells if c["model"] == "kimi" and c["review_type"] == "adversarial"]
        self.assertNotIn("advisory", cursor[0])
        self.assertNotIn("direct_api", cursor[0])
        self.assertFalse(any(c.get("advisory") for c in cells))
        self.assertEqual((ok, total), (0, 4))

    def test_counts_without_a_model_falls_back_to_advisory(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        _, ok, total, _ = AGG.aggregate(self.dir, CURSOR_MODELS, "", True)
        self.assertEqual((ok, total), (4, 4))

    def test_unreadable_artifacts_are_skipped(self):
        os.makedirs(os.path.join(self.dir, "findings-adversarial-kimi"))
        with open(os.path.join(self.dir, "findings-adversarial-kimi", "findings.json"), "wb") as f:
            f.write(b"\xff\xfe not json")
        cells, ok, total, _ = AGG.aggregate(self.dir, ["kimi"], "", False)
        self.assertEqual((ok, total), (0, 2))

    def test_cli_writes_panel_and_outputs(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        self.direct("edge-case", status="error")
        out = os.path.join(self.dir, "panel.json")
        gh_out = os.path.join(self.dir, "gh-output")
        result = subprocess.run(
            [sys.executable, SCRIPT, "--panel-dir", self.dir, "--models", json.dumps(CURSOR_MODELS),
             "--direct-model", DIRECT, "--direct-counts", "true", "--review-result", "success",
             "--out", out, "--github-output", gh_out],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("Panel: 5/6 cells contributed findings.", result.stdout)
        self.assertIn("Direct-API cell edge-case (counted): status=error.", result.stdout)
        with open(gh_out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "ok_count=5\ntotal=6\npanel_inconsistent=false\n")
        with open(out, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 6)


class ConsistencyTest(AggregateTest):
    """Leg results vs artifacts, and artifact name vs record.

    `Fail the leg when the cell did not submit` reds every non-ok cell, so a
    red matrix whose every counted artifact reads ok means some leg failed
    after its agent ran — an upload 409 behind a forged artifact, or a
    post-review infra failure. Neither leaves the count trustworthy.
    """

    def consistent(self, counts=False, review_result="success", direct_result="skipped"):
        return AGG.aggregate(self.dir, CURSOR_MODELS, DIRECT, counts, review_result, direct_result)[3]

    def test_all_ok_under_a_failed_review_matrix_is_inconsistent(self):
        self.full_cursor_panel()
        self.assertTrue(self.consistent(review_result="failure"))

    def test_all_ok_under_a_successful_matrix_is_consistent(self):
        self.full_cursor_panel()
        self.assertFalse(self.consistent(review_result="success"))

    def test_defaults_are_consistent(self):
        self.full_cursor_panel()
        self.assertFalse(AGG.aggregate(self.dir, CURSOR_MODELS)[3])

    def test_an_error_cell_explains_the_red_matrix(self):
        self.full_cursor_panel()
        self.write("findings-edge-case-kimi", cell("kimi", "edge-case", "error"))
        self.assertFalse(self.consistent(review_result="failure"))

    def test_a_missing_artifact_explains_the_red_matrix(self):
        self.full_cursor_panel()
        os.remove(os.path.join(self.dir, "findings-edge-case-kimi", "findings.json"))
        self.assertFalse(self.consistent(review_result="failure"))

    def test_cancelled_matrix_with_all_ok_is_inconsistent(self):
        self.full_cursor_panel()
        self.assertTrue(self.consistent(review_result="cancelled"))

    def test_counted_direct_lane_failed_with_all_ok_is_inconsistent(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        self.direct("edge-case")
        self.assertTrue(self.consistent(counts=True, direct_result="failure"))
        self.assertFalse(self.consistent(counts=True, direct_result="success"))
        self.assertFalse(self.consistent(counts=True, direct_result="skipped"))

    def test_counted_direct_lane_failed_with_an_error_cell_is_consistent(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        self.direct("edge-case", status="error")
        self.assertFalse(self.consistent(counts=True, direct_result="failure"))

    def test_counted_direct_lane_failed_with_its_artifact_missing_is_consistent(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        self.assertFalse(self.consistent(counts=True, direct_result="failure"))

    def test_advisory_direct_lane_result_is_ignored(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        self.direct("edge-case")
        self.assertFalse(self.consistent(counts=False, direct_result="failure"))

    def test_a_direct_error_does_not_explain_a_red_review_matrix(self):
        # The two lanes are checked separately: an errored direct cell is no
        # reason the Cursor matrix went red.
        self.full_cursor_panel()
        self.direct("adversarial")
        self.direct("edge-case", status="error")
        self.assertTrue(self.consistent(counts=True, review_result="failure", direct_result="failure"))

    def test_forged_ok_under_another_cells_name_with_its_leg_red(self):
        # The forger's own cell is honest-looking; the cell it impersonated
        # lost the upload race (409) and went red, so only the result shows it.
        self.full_cursor_panel()
        self.write("findings-adversarial-kimi", cell("kimi", "adversarial", "ok", findings=[]))
        cells, ok, total, inconsistent = AGG.aggregate(self.dir, CURSOR_MODELS, DIRECT, False, "failure")
        self.assertTrue(inconsistent)
        # The records are left alone: the judge still reads every finding.
        self.assertEqual((ok, total), (4, 4))

    def test_record_naming_another_cell_is_a_mismatch(self):
        self.write("findings-adversarial-kimi", cell("claude-opus", "adversarial"))
        cells, ok, total, inconsistent = AGG.aggregate(self.dir, ["kimi"], "", False)
        mismatched = [c for c in cells if c.get("status") == AGG.MISMATCH_STATUS]
        self.assertEqual(len(mismatched), 1)
        # Keyed to the slot its artifact was uploaded for; the claim kept aside.
        self.assertEqual((mismatched[0]["model"], mismatched[0]["review_type"]), ("kimi", "adversarial"))
        self.assertEqual(mismatched[0]["claimed_model"], "claude-opus")
        self.assertEqual(ok, 0)
        # One slot per artifact: no phantom error is synthesised for the slot
        # the artifact exists for, and the claimed slot is not counted twice.
        self.assertEqual(total, 2)
        self.assertTrue(inconsistent)
        # Not the tolerable `error`: auto-approve never excuses it.
        self.assertNotEqual(AGG.MISMATCH_STATUS, AA.TOLERABLE_CELL_STATUS)
        reason, tolerated = AA.panel_gate(
            [{k: c[k] for k in ("model", "review_type", "status")} for c in cells], max_failed=5
        )
        self.assertIsNotNone(reason)
        self.assertEqual(tolerated, [])

    def test_record_naming_another_review_type_is_a_mismatch(self):
        self.write("findings-edge-case-kimi", cell("kimi", "adversarial"))
        cells, _, _, _ = AGG.aggregate(self.dir, ["kimi"], "", False)
        self.assertIn(AGG.MISMATCH_STATUS, [c["status"] for c in cells])

    def test_direct_record_is_checked_against_its_direct_name(self):
        self.write(f"findings-direct-edge-case-{DIRECT}", cell("kimi", "edge-case"))
        cells, _, _, _ = AGG.aggregate(self.dir, ["kimi"], DIRECT, True)
        direct = [c for c in cells if c.get("direct_api") and c.get("claimed_model") == "kimi"]
        self.assertEqual(direct[0]["status"], AGG.MISMATCH_STATUS)
        self.assertEqual((direct[0]["model"], direct[0]["review_type"]), (DIRECT, "edge-case"))

    def test_unparseable_artifact_name_is_a_mismatch(self):
        self.write("findings-other-kimi", cell("kimi", "adversarial"))
        cells, _, total, inconsistent = AGG.aggregate(self.dir, ["kimi"], "", False)
        self.assertIn(AGG.MISMATCH_STATUS, [c["status"] for c in cells])
        # It cannot occupy kimi/adversarial: that slot is synthesised as error.
        synthesised = [c for c in cells if (c["model"], c["review_type"]) == ("kimi", "adversarial")]
        self.assertEqual([c["status"] for c in synthesised], ["error"])
        self.assertEqual(total, 3)
        self.assertTrue(inconsistent)

    def test_phantom_error_under_a_made_up_name_does_not_explain_a_red_leg(self):
        # A forged ok under the victim's name plus a self-consistent `error`
        # under a cell the matrix never ran: the phantom must not be the
        # tolerable error that explains the victim's red leg away.
        self.full_cursor_panel()
        self.write("findings-adversarial-phantom", cell("phantom", "adversarial", "error"))
        cells, ok, total, inconsistent = AGG.aggregate(self.dir, CURSOR_MODELS, DIRECT, False, "failure")
        phantom = [c for c in cells if c["model"] == "phantom"]
        self.assertEqual(phantom[0]["status"], AGG.MISMATCH_STATUS)
        self.assertTrue(inconsistent)
        reason, tolerated = AA.panel_gate(
            [{k: c[k] for k in ("model", "review_type", "status")} for c in cells], max_failed=5
        )
        self.assertIsNotNone(reason)
        self.assertEqual(tolerated, [])

    def test_phantom_ok_cannot_complete_a_review_type(self):
        self.write("findings-edge-case-kimi", cell("kimi", "edge-case"))
        self.write("findings-adversarial-phantom", cell("phantom", "adversarial"))
        cells, ok, _, inconsistent = AGG.aggregate(self.dir, ["kimi"], "", False)
        self.assertEqual(ok, 1)
        self.assertTrue(inconsistent)

    def test_unexpected_direct_cell_is_a_mismatch_only_when_counted(self):
        self.full_cursor_panel()
        self.write("findings-direct-adversarial-other", cell("other", "adversarial"))
        cells, _, _, inconsistent = AGG.aggregate(self.dir, CURSOR_MODELS, DIRECT, True)
        self.assertIn(AGG.MISMATCH_STATUS, [c["status"] for c in cells if c["model"] == "other"])
        self.assertTrue(inconsistent)
        cells, _, _, inconsistent = AGG.aggregate(self.dir, CURSOR_MODELS, DIRECT, False)
        self.assertEqual([c["status"] for c in cells if c["model"] == "other"], ["ok"])
        self.assertFalse(inconsistent)

    def test_cli_requires_the_review_result(self):
        # Fail closed: a dropped flag must not read as a successful matrix.
        result = subprocess.run(
            [sys.executable, SCRIPT, "--panel-dir", self.dir, "--models", json.dumps(["kimi"]),
             "--out", os.path.join(self.dir, "panel.json")],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--review-result", result.stderr)

    def test_matching_record_with_hyphenated_model_keeps_its_status(self):
        self.write("findings-edge-case-gpt-5.6-sol-xhigh", cell("gpt-5.6-sol-xhigh", "edge-case"))
        self.write("findings-adversarial-gpt-5.6-sol-xhigh", cell("gpt-5.6-sol-xhigh", "adversarial"))
        _, ok, total, _ = AGG.aggregate(self.dir, ["gpt-5.6-sol-xhigh"], "", False)
        self.assertEqual((ok, total), (2, 2))

    def test_identity_from_name(self):
        self.assertEqual(AGG.identity_from_name("findings-edge-case-kimi"), ("edge-case", "kimi"))
        self.assertEqual(AGG.identity_from_name(f"findings-direct-adversarial-{DIRECT}"), ("adversarial", DIRECT))
        self.assertEqual(AGG.identity_from_name("findings-adversarial-"), (None, None))
        self.assertEqual(AGG.identity_from_name("pr-diff"), (None, None))

    def test_cli_writes_panel_inconsistent(self):
        self.full_cursor_panel()
        gh_out = os.path.join(self.dir, "gh-output")
        result = subprocess.run(
            [sys.executable, SCRIPT, "--panel-dir", self.dir, "--models", json.dumps(CURSOR_MODELS),
             "--review-result", "failure", "--direct-result", "skipped",
             "--out", os.path.join(self.dir, "panel.json"), "--github-output", gh_out],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("a reviewer matrix did not succeed (review=failure", result.stdout)
        with open(gh_out, encoding="utf-8") as f:
            self.assertIn("panel_inconsistent=true\n", f.read())

    def test_cli_log_line_cannot_forge_a_command(self):
        self.write("findings-adversarial-kimi", cell("x\n::error::forged", "adversarial"))
        result = subprocess.run(
            [sys.executable, SCRIPT, "--panel-dir", self.dir, "--models", json.dumps(["kimi"]),
             "--review-result", "success", "--out", os.path.join(self.dir, "panel.json")],
            capture_output=True, text=True, check=True,
        )
        self.assertNotIn("\n::error::forged", result.stdout)
        self.assertIn("counted as status=mismatch", result.stdout)


ANTHROPIC = "claude-opus-5-5"


class PerLabTest(unittest.TestCase):
    """The OpenAI and Anthropic direct cells each count by their OWN lab's
    flag: one lab's role must never leak into the other's."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        # The Cursor panel once both labs' Cursor cells are replaced.
        self.models = ["kimi"]
        for review_type in AGG.REVIEW_TYPES:
            self.write(f"findings-{review_type}-kimi", cell("kimi", review_type))

    def write(self, artifact, record):
        os.makedirs(os.path.join(self.dir, artifact), exist_ok=True)
        with open(os.path.join(self.dir, artifact, "findings.json"), "w", encoding="utf-8") as f:
            json.dump(record, f)

    def direct(self, model, review_type, status="ok", **extra):
        self.write(f"findings-direct-{review_type}-{model}", cell(model, review_type, status, **extra))

    def both_labs(self, openai_status="ok", anthropic_status="ok"):
        for review_type in AGG.REVIEW_TYPES:
            self.direct(DIRECT, review_type, openai_status)
            self.direct(ANTHROPIC, review_type, anthropic_status)

    def run_agg(self, openai_counts, anthropic_counts, **kw):
        return AGG.aggregate(self.dir, self.models, DIRECT, openai_counts,
                             kw.get("review_result", "success"), kw.get("direct_result", "success"),
                             ANTHROPIC, anthropic_counts, kw.get("anthropic_result", "success"))

    def roles(self, cells):
        return {(c["model"], c["review_type"]): c.get("advisory", False)
                for c in cells if c.get("direct_api")}

    def test_both_labs_counting(self):
        self.both_labs(anthropic_status="error")
        cells, ok, total, inconsistent = self.run_agg(True, True, anthropic_result="failure")
        self.assertEqual((ok, total), (4, 6))
        self.assertFalse(inconsistent)
        self.assertFalse(any(self.roles(cells).values()))

    def test_both_labs_advisory(self):
        self.both_labs(openai_status="error", anthropic_status="error")
        cells, ok, total, inconsistent = self.run_agg(False, False)
        self.assertEqual((ok, total), (2, 2))
        self.assertFalse(inconsistent)
        self.assertTrue(all(self.roles(cells).values()))

    def test_openai_counting_anthropic_advisory(self):
        self.both_labs(anthropic_status="error")
        cells, ok, total, _ = self.run_agg(True, False)
        self.assertEqual((ok, total), (4, 4), "an advisory Anthropic cell was counted")
        roles = self.roles(cells)
        self.assertFalse(roles[(DIRECT, "adversarial")])
        self.assertTrue(roles[(ANTHROPIC, "adversarial")])

    def test_anthropic_counting_openai_advisory(self):
        self.both_labs(openai_status="error")
        cells, ok, total, _ = self.run_agg(False, True)
        self.assertEqual((ok, total), (4, 4), "an advisory OpenAI cell was counted")
        roles = self.roles(cells)
        self.assertTrue(roles[(DIRECT, "edge-case")])
        self.assertFalse(roles[(ANTHROPIC, "edge-case")])

    def test_missing_anthropic_artifact_is_synthesised_as_error(self):
        self.direct(ANTHROPIC, "adversarial")
        cells, ok, total, _ = self.run_agg(False, True)
        self.assertEqual((ok, total), (3, 4))
        synthesised = [c for c in cells if c["model"] == ANTHROPIC and c["review_type"] == "edge-case"]
        self.assertEqual([c["status"] for c in synthesised], ["error"])
        self.assertTrue(synthesised[0]["direct_api"])

    def test_missing_advisory_anthropic_artifact_is_not_synthesised(self):
        cells, ok, total, _ = self.run_agg(True, False)
        self.assertFalse([c for c in cells if c["model"] == ANTHROPIC])

    def test_an_anthropic_cell_cannot_set_its_own_markers(self):
        self.both_labs()
        self.direct(ANTHROPIC, "edge-case", "error", advisory=True, direct_api=False)
        cells, ok, total, _ = self.run_agg(False, True)
        self.assertEqual((ok, total), (3, 4), "a forged `advisory` excused a failed counted cell")
        forged = [c for c in cells if c["model"] == ANTHROPIC and c["review_type"] == "edge-case"][0]
        self.assertIs(forged["direct_api"], True)
        self.assertNotIn("advisory", forged)

    def test_counted_anthropic_lane_failed_with_all_ok_is_inconsistent(self):
        self.both_labs()
        _, _, _, inconsistent = self.run_agg(False, True, anthropic_result="failure")
        self.assertTrue(inconsistent)
        _, _, _, inconsistent = self.run_agg(True, False, anthropic_result="failure")
        self.assertFalse(inconsistent, "an advisory lab's red matrix was read as a forgery")

    def test_direct_artifact_matching_neither_lab_is_a_mismatch_when_any_counts(self):
        self.both_labs()
        self.direct("other-model", "adversarial")
        cells, ok, total, inconsistent = self.run_agg(False, True)
        self.assertEqual((ok, total), (4, 5))
        self.assertTrue(inconsistent)
        cells, ok, total, inconsistent = self.run_agg(False, False)
        self.assertEqual((ok, total), (2, 2))
        self.assertFalse(inconsistent)

    def test_cli_takes_the_anthropic_flags(self):
        self.both_labs(anthropic_status="error")
        out = os.path.join(self.dir, "panel.json")
        gh = os.path.join(self.dir, "gh-output")
        result = subprocess.run(
            [sys.executable, SCRIPT, "--panel-dir", self.dir, "--models", json.dumps(self.models),
             "--direct-model", DIRECT, "--direct-counts", "false", "--review-result", "success",
             "--anthropic-direct-model", ANTHROPIC, "--anthropic-direct-counts", "true",
             "--anthropic-direct-result", "failure", "--out", out, "--github-output", gh],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("Panel: 2/4 cells contributed findings.", result.stdout)
        with open(gh, encoding="utf-8") as f:
            self.assertIn("panel_inconsistent=false", f.read())


class DownstreamTest(unittest.TestCase):
    def test_summary_names_a_failed_direct_cell_with_its_backend(self):
        panel = [
            cell("claude-opus", "adversarial"),
            {"model": DIRECT, "review_type": "edge-case", "status": "error", "direct": True},
            {"model": "kimi", "review_type": "adversarial", "status": "error"},
        ]
        summary = PR.build_panel_summary(panel)
        self.assertIn("_Panel: 1/3 reviewers contributed findings._", summary)
        self.assertIn(f"{DIRECT}:edge-case (direct, error)", summary)
        self.assertIn("kimi:adversarial (error)", summary)

    def test_auto_approve_counts_a_failed_direct_cell(self):
        panel = [
            cell(m, t) for m in CURSOR_MODELS for t in AGG.REVIEW_TYPES
        ] + [
            {"model": DIRECT, "review_type": "adversarial", "status": "ok", "direct": True},
            {"model": DIRECT, "review_type": "edge-case", "status": "error", "direct": True},
        ]
        reason, tolerated = AA.panel_gate(panel, max_failed=0)
        self.assertEqual(reason, "1/6 panel reviewers did not complete")
        reason, tolerated = AA.panel_gate(panel, max_failed=1)
        self.assertIsNone(reason)
        self.assertEqual(len(tolerated), 1)


class DirectJobShapeTest(unittest.TestCase):
    """The workflow half: none of these would fail a lint if dropped."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, HERE)
        try:
            import test_workflow_panel_integrity as wpi
        finally:
            sys.path.remove(HERE)
        jobs = wpi.split_jobs(wpi.read_workflow())
        cls.direct = "\n".join(wpi.code_lines(jobs["review-openai-direct"]))
        cls.consolidate = "\n".join(wpi.code_lines(jobs["consolidate"]))
        cls.preflight = "\n".join(wpi.code_lines(jobs["preflight"]))

    def test_runs_both_review_types_with_their_own_prompt(self):
        self.assertIn("review_type: [adversarial, edge-case]", self.direct)
        self.assertIn('--prompt "$CURSOR_REVIEW_ASSETS/prompt-${REVIEW_TYPE}.md"', self.direct)
        self.assertNotIn("prompt-adversarial.md", self.direct)
        self.assertNotIn("--review-type adversarial", self.direct)

    def test_artifact_is_named_per_review_type(self):
        self.assertIn(
            "name: findings-direct-${{ matrix.review_type }}-${{ inputs.openai_direct_model }}",
            self.direct,
        )

    def test_failures_are_swallowed_only_when_advisory(self):
        self.assertIn(
            "continue-on-error: ${{ needs.preflight.outputs.openai_direct_counts != 'true' }}",
            self.direct,
        )
        self.assertNotIn("continue-on-error: true\n    timeout-minutes", self.direct)
        self.assertIn(
            "if: always() && needs.preflight.outputs.openai_direct_counts == 'true'", self.direct
        )

    def test_preflight_and_consolidate_carry_the_role(self):
        self.assertIn("openai_direct_counts: ${{ steps.models.outputs.openai_direct_counts }}", self.preflight)
        self.assertIn("DIRECT_COUNTS: ${{ needs.preflight.outputs.openai_direct_counts }}", self.consolidate)
        self.assertIn('if c.get("advisory") is True:', self.consolidate)
        self.assertIn('meta["direct"] = True', self.consolidate)


if __name__ == "__main__":
    unittest.main()
