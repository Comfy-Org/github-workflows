import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
SPEC = importlib.util.spec_from_file_location("from_risk", MODULE_DIR / "from_risk.py")
consumer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(consumer)

BASE = "a" * 40
HEAD = "b" * 40
TESTED = "c" * 40


def context():
    return {
        "snapshot": {"repo": "test/repo", "pr": 42, "author": "dev", "base_sha": BASE,
                     "head_sha": HEAD, "tested_sha": TESTED, "policy_version": "v1"},
        "policy": {"version": "v1", "required_checks": [{"name": "unit", "revision": "tested"}],
                   "required_human_approvals": 1},
    }


def risk_record():
    raw = {"repo": "test/repo", "pr": 42, "title": "docs", "author": "dev",
           "author_association": "MEMBER", "is_fork": False, "labels": [],
           "head_ref": "feature", "head_sha": HEAD, "additions": 1, "deletions": 0,
           "changed_files": 1, "checks_state": "SUCCESS", "checks_status": "ok",
           "provenance_status": "ok", "changed_paths_status": "ok",
           "changed_paths": [{"path": "README.md", "additions": 1, "deletions": 0,
                              "change_type": "MODIFIED"}]}
    run = subprocess.run(["bash", str(ROOT / "scripts/pr-risk/grade-pr-risk.sh"), "--stdin"],
                         input=json.dumps(raw), text=True, capture_output=True, check=True)
    return json.loads(run.stdout)


def envelope():
    record = risk_record()
    return {**context(), "report": {"summary": {"pr": 42, "status": "graded", "tier": record["risk"]["tier"],
                                               "base_sha": BASE}, "record": record}}


class RiskConsumerTests(unittest.TestCase):
    def test_real_grader_output_never_substitutes_for_review_evidence(self):
        result = consumer.evaluate_report(envelope())
        self.assertEqual(result["state"], "WAITING")
        self.assertEqual(result["risk"]["status"], "observed")
        self.assertIn("review evidence missing", result["reasons"])
        self.assertIn("finding evidence missing", result["reasons"])

    def test_stale_report_is_not_presented_as_current(self):
        data = envelope()
        data["report"]["summary"]["base_sha"] = "d" * 40
        result = consumer.evaluate_report(data)
        self.assertEqual(result["state"], "WAITING")
        self.assertEqual(result["risk"]["status"], "stale")
        self.assertIsNone(result["risk"]["tier"])

    def test_unknown_or_inconsistent_grade_stays_unknown(self):
        for change in ("ungraded", "wrong_tier"):
            data = envelope()
            if change == "ungraded":
                data["report"]["summary"]["status"] = "ungraded"
            else:
                data["report"]["summary"]["tier"] = "R3"
            result = consumer.evaluate_report(data)
            self.assertEqual(result["state"], "WAITING")
            self.assertEqual(result["risk"]["status"], "unknown")
            self.assertIsNone(result["risk"]["tier"])

    def test_missing_or_partial_report_is_unknown(self):
        for report in (None, {}, {"summary": {}, "record": {}}, {"summary": None}):
            result = consumer.evaluate_report({**context(), "report": report})
            self.assertEqual(result["state"], "WAITING")
            self.assertEqual(result["risk"]["status"], "unknown")

    def test_cli_executes_consumer(self):
        run = subprocess.run([sys.executable, str(MODULE_DIR / "from_risk.py")],
                             input=json.dumps(envelope()), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(run.stdout)["state"], "WAITING")


if __name__ == "__main__":
    unittest.main()
