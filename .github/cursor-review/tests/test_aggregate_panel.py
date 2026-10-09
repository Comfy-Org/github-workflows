#!/usr/bin/env python3
"""Tests for aggregate-panel.py — which panel cells count toward the gate.

The direct-API OpenAI cells (`findings-direct-<review_type>-<model>`) gate only
when they REPLACE the Cursor OpenAI lane; side by side they stay advisory. A
regression in either direction is invisible in a diff: counting advisory cells
lets a comparison run withhold approval, and NOT counting replacing cells
shrinks the gating panel to two labs, so one lab outage withholds it.

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
        os.makedirs(os.path.join(self.dir, artifact))
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
        cells, ok, total = self.run_agg(True)
        self.assertEqual((ok, total), (6, 6))
        direct = [c for c in cells if c.get("direct_api")]
        self.assertEqual(sorted(c["review_type"] for c in direct), ["adversarial", "edge-case"])
        self.assertFalse(any(c.get("advisory") for c in cells))

    def test_replace_true_one_direct_error_counts_as_failed(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        self.direct("edge-case", status="error")
        _, ok, total = self.run_agg(True)
        self.assertEqual((ok, total), (5, 6))

    def test_replace_true_missing_direct_artifact_is_synthesised_as_error(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        cells, ok, total = self.run_agg(True)
        self.assertEqual((ok, total), (5, 6))
        missing = [c for c in cells if c.get("direct_api") and c["review_type"] == "edge-case"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["status"], "error")
        self.assertEqual(missing[0]["model"], DIRECT)

    def test_replace_false_direct_cells_are_advisory_and_uncounted(self):
        self.full_cursor_panel()
        self.direct("adversarial", status="error")
        self.direct("edge-case")
        cells, ok, total = self.run_agg(False)
        self.assertEqual((ok, total), (4, 4))
        direct = [c for c in cells if c.get("direct_api")]
        self.assertEqual(len(direct), 2)
        self.assertTrue(all(c.get("advisory") is True for c in direct))

    def test_replace_false_missing_direct_cell_is_not_synthesised(self):
        self.full_cursor_panel()
        cells, ok, total = self.run_agg(False)
        self.assertEqual((ok, total), (4, 4))
        self.assertFalse(any(c.get("direct_api") for c in cells))

    def test_advisory_direct_cell_cannot_mask_a_missing_cursor_cell(self):
        # Side by side the Cursor cell may share the direct id; the direct
        # record must not stand in for a Cursor cell that never uploaded.
        self.write("findings-adversarial-claude-opus", cell("claude-opus", "adversarial"))
        self.write(f"findings-direct-edge-case-{DIRECT}", cell("claude-opus", "edge-case"))
        cells, ok, total = AGG.aggregate(self.dir, ["claude-opus"], DIRECT, False)
        self.assertEqual((ok, total), (1, 2))

    def test_a_cell_cannot_set_its_own_markers(self):
        self.write("findings-adversarial-kimi", cell("kimi", "adversarial", "error", advisory=True, direct_api=True))
        self.direct("adversarial", status="error", advisory=True)
        cells, ok, total = AGG.aggregate(self.dir, ["kimi"], DIRECT, True)
        cursor = [c for c in cells if c["model"] == "kimi" and c["review_type"] == "adversarial"]
        self.assertNotIn("advisory", cursor[0])
        self.assertNotIn("direct_api", cursor[0])
        self.assertFalse(any(c.get("advisory") for c in cells))
        self.assertEqual((ok, total), (0, 4))

    def test_counts_without_a_model_falls_back_to_advisory(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        _, ok, total = AGG.aggregate(self.dir, CURSOR_MODELS, "", True)
        self.assertEqual((ok, total), (4, 4))

    def test_unreadable_artifacts_are_skipped(self):
        os.makedirs(os.path.join(self.dir, "findings-adversarial-kimi"))
        with open(os.path.join(self.dir, "findings-adversarial-kimi", "findings.json"), "wb") as f:
            f.write(b"\xff\xfe not json")
        cells, ok, total = AGG.aggregate(self.dir, ["kimi"], "", False)
        self.assertEqual((ok, total), (0, 2))

    def test_cli_writes_panel_and_outputs(self):
        self.full_cursor_panel()
        self.direct("adversarial")
        self.direct("edge-case", status="error")
        out = os.path.join(self.dir, "panel.json")
        gh_out = os.path.join(self.dir, "gh-output")
        result = subprocess.run(
            [sys.executable, SCRIPT, "--panel-dir", self.dir, "--models", json.dumps(CURSOR_MODELS),
             "--direct-model", DIRECT, "--direct-counts", "true", "--out", out, "--github-output", gh_out],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("Panel: 5/6 cells contributed findings.", result.stdout)
        self.assertIn("Direct-API cell edge-case (counted): status=error.", result.stdout)
        with open(gh_out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "ok_count=5\ntotal=6\n")
        with open(out, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 6)


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
