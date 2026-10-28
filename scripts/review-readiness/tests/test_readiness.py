import copy
import json
import subprocess
import sys
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("readiness", Path(__file__).resolve().parents[1] / "readiness.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

BASE = "a" * 40
HEAD = "b" * 40
TESTED = "c" * 40
SCOPE = {"repo": "example/repo", "pr": 42, "base_sha": BASE, "head_sha": HEAD, "policy_version": "v1"}
PROOF = "https://example.com/evidence"


def sample():
    return {
        "snapshot": {**SCOPE, "tested_sha": TESTED, "author": "author"},
        "policy": {"version": "v1", "required_checks": [{"name": "build", "revision": "tested"}], "required_human_approvals": 1},
        "checks_complete": True,
        "checks": [{"scope": SCOPE, "name": "build", "result": "success", "tested_sha": TESTED, "proof": PROOF}],
        "reviews": {"scope": SCOPE, "complete": True, "proof": PROOF, "approvals": [{"actor": "reviewer", "human": True, "state": "approved"}]},
        "findings": {"scope": SCOPE, "complete": True, "proof": PROOF, "items": []},
    }


class ReadinessTests(unittest.TestCase):
    def test_complete_current_evidence_is_ready(self):
        self.assertEqual(module.evaluate(sample())["state"], "READY")

    def test_current_failure_blocks_even_with_incomplete_evidence(self):
        data = sample()
        data["checks"][0]["result"] = "failure"
        data["reviews"]["complete"] = False
        self.assertEqual(module.evaluate(data)["state"], "BLOCKED")

    def test_open_finding_blocks_even_when_inventory_incomplete(self):
        data = sample()
        data["findings"].update(complete=False, items=[{"state": "open"}])
        self.assertEqual(module.evaluate(data)["state"], "BLOCKED")

    def test_stale_failure_does_not_block_or_satisfy_current_check(self):
        data = sample()
        data["checks"][0]["scope"] = {**SCOPE, "head_sha": "d" * 40}
        data["checks"][0]["result"] = "failure"
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_skipped_or_unavailable_check_waits(self):
        for result in ("skipped", "neutral", "unavailable", "cancelled", "pending"):
            data = sample()
            data["checks"][0]["result"] = result
            self.assertEqual(module.evaluate(data)["state"], "WAITING", result)

    def test_policy_can_require_both_head_and_merge_candidate_checks(self):
        data = sample()
        data["policy"]["required_checks"].append({"name": "preview", "revision": "head"})
        data["checks"].append({"scope": SCOPE, "name": "preview", "result": "success", "tested_sha": HEAD, "proof": PROOF})
        self.assertEqual(module.evaluate(data)["state"], "READY")
        data["checks"][1]["tested_sha"] = TESTED
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_stale_payload_is_ignored_after_its_scope_is_known(self):
        data = sample()
        data["checks"][0].update(scope={**SCOPE, "head_sha": "d" * 40}, result="unrecognized")
        data["reviews"].update(scope={**SCOPE, "head_sha": "d" * 40}, approvals=None)
        data["findings"].update(scope={**SCOPE, "head_sha": "d" * 40}, items=None)
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_wrong_tested_revision_does_not_satisfy_check(self):
        data = sample()
        data["checks"][0]["tested_sha"] = "d" * 40
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_stale_approval_and_unavailable_findings_wait(self):
        for section in ("reviews", "findings"):
            data = sample()
            data[section]["scope"] = {**SCOPE, "policy_version": "old"}
            self.assertEqual(module.evaluate(data)["state"], "WAITING")
        data = sample()
        data["findings"]["complete"] = False
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_self_and_bot_approvals_do_not_count(self):
        data = sample()
        data["reviews"]["approvals"] = [{"actor": "author", "human": True, "state": "approved"}, {"actor": "robot", "human": False, "state": "approved"}]
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_missing_or_inconsistent_policy_is_rejected(self):
        for change in ({"required_checks": []}, {"required_human_approvals": 0}, {"version": "other"}):
            data = sample()
            data["policy"].update(change)
            with self.assertRaises(ValueError):
                module.evaluate(data)

    def test_conflicting_review_states_do_not_count_as_approval(self):
        data = sample()
        data["reviews"]["approvals"].append({"actor": "reviewer", "human": True, "state": "dismissed"})
        self.assertNotEqual(module.evaluate(data)["state"], "READY")

    def test_missing_proof_or_incomplete_checks_cannot_be_ready(self):
        for target in ("check", "reviews", "findings"):
            data = sample()
            item = data["checks"][0] if target == "check" else data[target]
            item.pop("proof")
            self.assertEqual(module.evaluate(data)["state"], "WAITING")
        data = sample()
        data["checks_complete"] = False
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_other_pr_cannot_satisfy_same_revision(self):
        data = sample()
        data["checks"][0]["scope"] = {**SCOPE, "pr": 43}
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_cli_runs_real_evaluator_and_rejects_invalid_input(self):
        script = str(Path(__file__).resolve().parents[1] / "readiness.py")
        run = subprocess.run([sys.executable, script], input=json.dumps(sample()), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(run.stdout)["state"], "READY")
        invalid = subprocess.run([sys.executable, script], input="{}", text=True, capture_output=True)
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(invalid.stdout, "")

    def test_case_insensitive_self_and_duplicate_reviewers(self):
        data = sample()
        data["reviews"]["approvals"] = [{"actor": "AUTHOR", "human": True, "state": "approved"}]
        self.assertEqual(module.evaluate(data)["state"], "WAITING")
        data["policy"]["required_human_approvals"] = 2
        data["reviews"]["approvals"] = [{"actor": "Reviewer", "human": True, "state": "approved"}, {"actor": "reviewer", "human": True, "state": "approved"}]
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_output_keeps_current_evidence_links(self):
        self.assertEqual(module.evaluate(sample())["proofs"], [PROOF])

    def test_stale_finding_and_change_request_do_not_block_current_snapshot(self):
        data = sample()
        stale = {**SCOPE, "base_sha": "d" * 40}
        data["findings"].update(scope=stale, items=[{"state": "open"}])
        data["reviews"].update(scope=stale, approvals=[{"actor": "reviewer", "human": True, "state": "changes_requested"}])
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_current_change_request_blocks_incomplete_review_inventory(self):
        data = sample()
        data["reviews"].update(complete=False, approvals=[{"actor": "reviewer", "human": True, "state": "changes_requested"}])
        self.assertEqual(module.evaluate(data)["state"], "BLOCKED")

    def test_new_snapshot_with_no_evidence_waits(self):
        data = sample()
        data.update(checks=[], checks_complete=False, reviews=None, findings=None)
        self.assertEqual(module.evaluate(data)["state"], "WAITING")

    def test_input_is_not_mutated(self):
        data = sample()
        before = copy.deepcopy(data)
        module.evaluate(data)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
