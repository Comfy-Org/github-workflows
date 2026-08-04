#!/usr/bin/env python3
"""Contract tests for the Cursor Review stdio MCP output tools."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "review-output-mcp.py")
SPEC = importlib.util.spec_from_file_location("review_output_mcp", MODULE_PATH)
MCP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MCP)

FINDING = {
    "file": "internal/api/handler.go",
    "line": 42,
    "side": "RIGHT",
    "severity": "high",
    "body": "User-supplied filename reaches os.Open without traversal checks.",
}


class ValidationTest(unittest.TestCase):
    def test_finding_is_normalized(self):
        finding = {**FINDING, "file": "internal/api/../api/handler.go"}
        self.assertEqual(MCP.validate_finding(finding)["file"], FINDING["file"])

    def test_rejects_path_traversal(self):
        with self.assertRaisesRegex(ValueError, "inside the repository"):
            MCP.validate_finding({**FINDING, "file": "../secret"})

    def test_rejects_unknown_fields(self):
        with self.assertRaisesRegex(ValueError, "keys must be exactly"):
            MCP.validate_finding({**FINDING, "confidence": 0.9})

    def test_non_object_record_falls_back_to_initial_state(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "findings.json")
            with open(output_path, "w", encoding="utf-8") as output:
                json.dump([], output)
            args = types.SimpleNamespace(
                out=output_path,
                model="test-model",
                review_type="reviewer",
            )
            self.assertEqual(MCP.read_record(args), MCP.initial_record(args))

    def test_tool_io_error_returns_json_rpc_error(self):
        args = types.SimpleNamespace(mode="reviewer")
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "cursor_review_finish", "arguments": {}},
        }
        with mock.patch.object(MCP, "call_tool", side_effect=OSError("disk full")):
            response = MCP.handle(args, message)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "disk full")


class StdioServerTest(unittest.TestCase):
    def run_server(self, mode, messages, include_initial=False):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "findings.json")
            subprocess.run(
                [sys.executable, MODULE_PATH, "--mode", mode, "--out", output_path,
                 "--model", "test-model", "--review-type", mode, "--init"],
                check=True,
            )
            with open(output_path, encoding="utf-8") as source:
                initial_record = json.load(source)
            process = subprocess.run(
                [sys.executable, MODULE_PATH, "--mode", mode, "--out", output_path,
                 "--model", "test-model", "--review-type", mode],
                input="".join(json.dumps(message) + "\n" for message in messages),
                text=True,
                capture_output=True,
                check=True,
            )
            with open(output_path, encoding="utf-8") as source:
                record = json.load(source)
            responses = [json.loads(line) for line in process.stdout.splitlines()]
            if include_initial:
                return record, responses, initial_record
            return record, responses

    def test_reviewer_records_findings_and_finishes(self):
        record, responses = self.run_server("reviewer", [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "cursor_review_record_finding", "arguments": FINDING,
            }},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "cursor_review_finish", "arguments": {},
            }},
        ])
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["findings"], [FINDING])
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])

    def test_reviewer_must_finish_empty_review(self):
        record, _ = self.run_server("reviewer", [])
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["findings"], [])

    def test_recorded_finding_is_incomplete_until_finish(self):
        record, _ = self.run_server("reviewer", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "cursor_review_record_finding", "arguments": FINDING,
            }},
        ])
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["findings"], [FINDING])

    def test_cannot_overwrite_finished_review(self):
        record, responses = self.run_server("reviewer", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "cursor_review_finish", "arguments": {},
            }},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "cursor_review_record_finding", "arguments": FINDING,
            }},
        ])
        self.assertEqual(record["findings"], [])
        self.assertTrue(responses[1]["result"]["isError"])

    def test_judge_submits_empty_final_review(self):
        record, _ = self.run_server("judge", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "cursor_review_submit_final", "arguments": {"findings": []},
            }},
        ])
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["findings"], [])

    def test_invalid_tool_input_does_not_replace_output(self):
        record, responses, initial_record = self.run_server("judge", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "cursor_review_submit_final",
                "arguments": {"findings": [FINDING, {**FINDING, "line": 0}]},
            }},
        ], include_initial=True)
        self.assertEqual(record, initial_record)
        self.assertTrue(responses[0]["result"]["isError"])

    def test_rejects_falsy_non_object_tool_arguments(self):
        record, responses = self.run_server("reviewer", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "cursor_review_finish", "arguments": [],
            }},
        ])
        self.assertEqual(record["status"], "error")
        self.assertTrue(responses[0]["result"]["isError"])

    def test_negotiates_only_supported_protocol(self):
        _, responses = self.run_server("reviewer", [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2099-01-01",
            }},
        ])
        self.assertEqual(
            responses[0]["result"]["protocolVersion"], MCP.PROTOCOL_VERSION
        )

    def test_negotiates_current_protocol(self):
        _, responses = self.run_server("reviewer", [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25",
            }},
        ])
        self.assertEqual(
            responses[0]["result"]["protocolVersion"], "2025-11-25"
        )


if __name__ == "__main__":
    unittest.main()
