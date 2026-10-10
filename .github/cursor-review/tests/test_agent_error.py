#!/usr/bin/env python3
"""Tests for agent-error.py, which makes a direct-API cell's failure legible.

Two things are being protected, and they pull against each other.

LEGIBILITY. The motivating outage was an OpenAI `insufficient_quota` /
`credit_balance_exhausted`, reported by `codex exec --json` as an event on
STDOUT — i.e. into the events file, which is never echoed. The log showed a
non-zero exit and nothing else, so the lane's failures had to be inferred from
job DURATION. `NestedEnvelopeTest` pins that exact shape, because the first
draft of this script DID parse it and still printed only `type=error`: the
provider's error object describes itself as `insufficient_quota`, and the
heuristic looking for the substring "error" in `type`/`subtype`/`code` walked
straight past the one message worth printing.

SILENCE ABOUT MODEL OUTPUT. These files also hold the agent's reasoning, tool
calls and findings — model output steered by PR-authored text, which the
workflow's own `Report cell outcome` step refuses to echo for that reason. The
allowlist is the safety property, so `AllowlistTest` asserts the absence of
prose rather than the presence of fields: a future key added to ERROR_FIELDS
that happens to carry content would fail there.

Everything else is about not becoming a second failure. The script runs in an
`always()` step after the agent already failed, so a truncated, empty,
non-JSON, invalid-UTF-8 or missing file must still exit 0 (`DegenerateTest`),
and an agent-written file must not be able to exhaust memory or the recursion
limit (`BoundsTest`).

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py' -v
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "agent-error.py")
)
SPEC = importlib.util.spec_from_file_location("agent_error", SCRIPT)
agent_error = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_error)

WORKFLOW = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml"
    )
)

# Marker strings. If one of these reaches the output, prose reached a public log.
PROSE = "PROSE_MUST_NOT_APPEAR"


def summary(obj_or_text):
    """The script's output lines for a payload, as the workflow would get them."""
    text = obj_or_text
    if not isinstance(text, str):
        text = json.dumps(obj_or_text)
    return agent_error.summarize(text)


def joined(obj_or_text):
    return "\n".join(summary(obj_or_text))


class NestedEnvelopeTest(unittest.TestCase):
    """The outage this script exists for, pinned shape by shape."""

    # The literal body OpenAI returned, with the surrounding codex events.
    QUOTA = (
        '{"type":"session.created","session_id":"abc"}\n'
        '{"type":"item.completed","item":{"type":"reasoning","text":"%s"}}\n'
        '{"type":"error","error":{"type":"insufficient_quota",'
        '"code":"credit_balance_exhausted","message":"You have no credits '
        'remaining. Add credits to continue using the API at '
        'https://platform.openai.com/settings/organization/billing/."}}\n'
    ) % PROSE

    def test_the_provider_error_is_named(self):
        out = joined(self.QUOTA)
        # Each of these is independently the thing a reader needs.
        self.assertIn("insufficient_quota", out)
        self.assertIn("credit_balance_exhausted", out)
        self.assertIn("no credits remaining", out)

    def test_the_billing_link_survives(self):
        # The provider's message names the fix; truncating it away would leave
        # the reader with a code and no next step.
        self.assertIn("platform.openai.com", joined(self.QUOTA))

    def test_the_reasoning_event_does_not(self):
        self.assertNotIn(PROSE, joined(self.QUOTA))

    def test_a_nested_error_needs_no_self_description(self):
        # The regression directly: nothing here contains the string "error"
        # except the envelope key, so a substring heuristic alone finds nothing.
        out = joined({"error": {"type": "overloaded", "code": "slow_down"}})
        self.assertIn("overloaded", out)
        self.assertIn("slow_down", out)

    def test_a_string_error_is_still_read(self):
        self.assertIn("boom", joined({"error": "boom"}))

    def test_a_blank_string_error_is_not_reported(self):
        out = joined({"type": "turn.completed", "error": "   "})
        self.assertNotIn("error:", out)


class AllowlistTest(unittest.TestCase):
    """What may NOT appear. Asserted as absence, so a widened allowlist fails."""

    PROSE_KEYS = (
        "result", "content", "text", "input", "output", "arguments",
        "reasoning", "delta", "thinking", "summary", "findings",
    )

    def test_no_prose_key_is_printed_from_an_error_object(self):
        for key in self.PROSE_KEYS:
            with self.subTest(key=key):
                payload = {"type": "error", "is_error": True, key: PROSE}
                self.assertNotIn(PROSE, joined(payload))

    def test_the_allowlist_holds_no_prose_key(self):
        for key in self.PROSE_KEYS:
            self.assertNotIn(key, agent_error.ERROR_FIELDS)

    def test_a_structured_message_is_not_flattened(self):
        # `message` IS allowlisted, so a provider that nests content blocks
        # under it must be skipped rather than stringified.
        payload = {"type": "error", "message": [{"type": "text", "text": PROSE}]}
        self.assertNotIn(PROSE, joined(payload))

    def test_a_dict_message_is_not_flattened(self):
        payload = {"type": "error", "message": {"body": PROSE}}
        self.assertNotIn(PROSE, joined(payload))

    def test_claude_result_prose_is_withheld_but_the_failure_is_named(self):
        payload = {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": PROSE,
            "usage": {"input_tokens": 10},
        }
        out = joined(payload)
        self.assertNotIn(PROSE, out)
        self.assertIn("error_during_execution", out)


class DetectionTest(unittest.TestCase):
    def test_is_error_true(self):
        self.assertTrue(agent_error._looks_like_error({"is_error": True}))

    def test_is_error_false_is_not_an_error(self):
        self.assertFalse(agent_error._looks_like_error({"is_error": False}))

    def test_an_http_status_at_or_over_400(self):
        self.assertTrue(agent_error._looks_like_error({"status": 429}))
        self.assertTrue(agent_error._looks_like_error({"status_code": 500}))
        self.assertTrue(agent_error._looks_like_error({"http_status": 400}))

    def test_a_successful_status_is_not_an_error(self):
        self.assertFalse(agent_error._looks_like_error({"status": 200}))

    def test_a_boolean_status_is_not_a_status_code(self):
        # Pins the BEHAVIOUR, which today holds by arithmetic rather than by
        # the `isinstance(value, bool)` guard above it: `bool` is an `int`
        # subclass, but `True >= 400` is already false, so deleting that guard
        # changes nothing and this test cannot catch it. The guard stays as a
        # statement of intent for anyone who lowers the threshold.
        self.assertFalse(agent_error._looks_like_error({"status": True}))
        self.assertFalse(agent_error._looks_like_error({"status": False}))

    def test_a_clean_run_reports_no_error(self):
        text = (
            '{"type":"session.created"}\n'
            '{"type":"item.completed","item":{"type":"tool_call"}}\n'
            '{"type":"turn.completed"}\n'
        )
        out = joined(text)
        self.assertIn("no error object", out)
        self.assertNotIn("error: ", out)

    def test_the_event_census_is_printed(self):
        # The census is the fallback diagnosis when there is no error object:
        # it says how far the agent got.
        out = joined('{"type":"session.created"}\n{"type":"turn.completed"}\n')
        self.assertIn("events: 2", out)
        self.assertIn("session.created", out)


class SanitizationTest(unittest.TestCase):
    def test_a_workflow_command_cannot_be_forged(self):
        payload = {"error": {"message": "x\n::error::forged\n::set-output name=a::b"}}
        out = joined(payload)
        self.assertNotIn("::", out)
        self.assertIn("forged", out)   # broken, not deleted

    def test_a_value_cannot_become_two_log_lines(self):
        out = summary({"error": {"message": "first\nsecond\rthird"}})
        self.assertEqual(
            1, sum(1 for line in out if "first" in line and "third" in line)
        )

    def test_disallowed_characters_are_replaced_not_dropped(self):
        # Deleting would let a crafted value collapse into different text.
        self.assertEqual("a?b", agent_error.sanitize("a\u0000b"))

    def test_a_long_value_is_truncated(self):
        out = agent_error.sanitize("x" * 5000)
        self.assertLessEqual(len(out), agent_error.MAX_VALUE)
        self.assertTrue(out.endswith("…"))

    def test_the_label_is_sanitized(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "events.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}\n")
            out = run_script(path, label="::error::forged")
            self.assertNotIn("::error::", out)


class BoundsTest(unittest.TestCase):
    def test_deep_nesting_does_not_recurse_without_limit(self):
        node = {"type": "error", "code": "deep"}
        for _ in range(5000):
            node = {"wrap": node}
        out = joined(node)   # must not raise RecursionError
        self.assertIn("walk bounded", out)

    def test_wide_breadth_is_bounded(self):
        payload = {"items": [{"type": f"t{i}"} for i in range(50000)]}
        out = joined(payload)
        self.assertIn("walk bounded", out)

    def test_many_error_objects_are_capped(self):
        text = "\n".join(
            json.dumps({"type": "error", "code": f"c{i}"}) for i in range(200)
        )
        out = summary(text)
        self.assertLessEqual(
            sum(1 for line in out if line.startswith("error: ")),
            agent_error.MAX_LINES,
        )
        self.assertTrue(any("more error object(s)" in line for line in out))

    def test_duplicate_error_objects_collapse(self):
        one = json.dumps({"type": "error", "code": "same"})
        out = summary("\n".join([one] * 20))
        self.assertEqual(1, sum(1 for line in out if line.startswith("error: ")))

    def test_the_census_is_capped(self):
        text = "\n".join(json.dumps({"type": f"t{i}"}) for i in range(100))
        census = [line for line in summary(text) if line.startswith("events: ")]
        self.assertEqual(1, len(census))
        self.assertLessEqual(census[0].count("×"), 10)


def run_script(path, label="adversarial/gpt-5.6-sol"):
    """The script as the workflow runs it: a subprocess, whose code must be 0."""
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--events", path, "--label", label],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}"
    return proc.stdout


class DegenerateTest(unittest.TestCase):
    """It runs after the agent already failed; it must never fail in turn."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, data, name="events.jsonl"):
        path = os.path.join(self.dir.name, name)
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(path, mode) as f:
            f.write(data)
        return path

    def test_a_missing_file_exits_zero(self):
        out = run_script(os.path.join(self.dir.name, "nope.jsonl"))
        self.assertIn("could not read", out)

    def test_an_empty_file_exits_zero(self):
        self.assertIn("no JSON events", run_script(self.write("")))

    def test_plain_text_exits_zero(self):
        out = run_script(self.write("Reading prompt from stdin...\nnot json\n"))
        self.assertIn("no JSON events", out)

    def test_invalid_utf8_exits_zero(self):
        path = self.write(b'{"error":{"message":"caf\xc3\x28 bytes"}}\n')
        self.assertIn("bytes", run_script(path))

    def test_a_truncated_last_event_still_reads_the_rest(self):
        # The normal shape for a killed agent: complete events, then half of one.
        path = self.write(
            '{"type":"error","error":{"code":"first_error"}}\n'
            '{"type":"item.completed","item":{"te'
        )
        self.assertIn("first_error", run_script(path))

    def test_a_directory_exits_zero(self):
        self.assertIn("could not read", run_script(self.dir.name))

    def test_the_header_names_the_cell(self):
        out = run_script(self.write("{}\n"), label="edge-case/claude-opus-5-5")
        self.assertIn("edge-case/claude-opus-5-5", out)

    def test_output_is_bounded_overall(self):
        text = "\n".join(
            json.dumps({"type": "error", "message": "m" * 400, "code": f"c{i}"})
            for i in range(500)
        )
        out = run_script(self.write(text))
        self.assertLessEqual(len(out.encode("utf-8")), agent_error.MAX_BYTES + 512)


class WorkflowWiringTest(unittest.TestCase):
    """Both direct lanes must actually call it, on their own output file.

    Parsed WITHOUT PyYAML, like its sibling suites: this repo is stdlib-only
    and CI installs no requirements for these tests.
    """

    @classmethod
    def setUpClass(cls):
        with io.open(WORKFLOW, encoding="utf-8") as f:
            cls.text = f.read()
        cls.blocks = cls._steps(cls.text)

    @staticmethod
    def _steps(text):
        """The `Report cell outcome` step bodies, keyed in file order."""
        out, lines, current = [], text.splitlines(), None
        for line in lines:
            if re.match(r"^      - name: ", line):
                if current is not None:
                    out.append("\n".join(current))
                current = [line] if "Report cell outcome" in line else None
            elif current is not None:
                current.append(line)
        if current is not None:
            out.append("\n".join(current))
        return out

    def test_both_direct_lanes_report_the_failure(self):
        calling = [b for b in self.blocks if "agent-error.py" in b]
        self.assertEqual(2, len(calling), "expected exactly the two direct lanes")

    def test_each_lane_reads_its_own_output_file(self):
        files = sorted(
            re.search(r"--events (\S+)", b).group(1)
            for b in self.blocks
            if "agent-error.py" in b
        )
        self.assertEqual(["/tmp/claude-result.json", "/tmp/codex-events.jsonl"], files)

    def test_the_file_each_lane_reads_is_the_file_its_agent_writes(self):
        # A rename on one side only would leave the diagnosis reading a file
        # nobody writes, and `could not read` is a plausible-looking output.
        for path in ("/tmp/codex-events.jsonl", "/tmp/claude-result.json"):
            with self.subTest(path=path):
                self.assertIn(f"> {path}", self.text)

    def test_the_diagnosis_runs_only_when_the_cell_failed(self):
        # In the `ok` branch it would print "why the cell failed" about a cell
        # that succeeded.
        for block in self.blocks:
            if "agent-error.py" not in block:
                continue
            ok_at = block.index("submitted its findings.")
            self.assertLess(ok_at, block.index("agent-error.py"))
            self.assertIn("else", block[ok_at:block.index("agent-error.py")])

    def test_the_script_is_loaded_from_the_pinned_assets(self):
        # Never from the caller's checkout: a PR must not be able to rewrite
        # the code that reports on it.
        for block in self.blocks:
            if "agent-error.py" in block:
                self.assertIn('"$CURSOR_REVIEW_ASSETS/agent-error.py"', block)

    def test_the_cell_outcome_step_still_withholds_the_findings(self):
        for block in self.blocks:
            self.assertNotIn("cat /tmp/findings-out/findings.json", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
