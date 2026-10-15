"""The direct-Anthropic key probe: is the key USABLE, not just present?

A key whose organization hit its usage limit is present but refused, so every
direct lane errors and the round ends with no decision. `gate`'s `Probe the
direct Anthropic key` (and its byte-identical twin in cursor-axis-base.yml's
`axis-probe` job) makes one 1-token Messages call per configured model and
classifies the answer. EXECUTED against a local HTTP stub replaying recorded
responses:

  * 200 → usable; the usage-limit 400 / a billing error / 401 → unusable, with
    the API's message kept; 429 then 200 → usable after a retry; 529 on every
    attempt → unusable; 404 → invalid (a typo never falls back), except a
    dash-free Claude Code alias, which is not probed; a 400 is never invalid;
    a redirect is refused, never followed with the key; a truncated body is
    an answer to retry, not a crash; retries stop at the budget;
  * the key never reaches the step's output or log, and the request is the
    1-token call the ticket allows;
  * preflight reads unusable exactly like an absent key (the Cursor `claude-*`
    cells and the Cursor judge run, one warning per lane) and fails on invalid,
    but only after the openai/anthropic id collision check; a panel with no
    Cursor claude-* id says no Anthropic cell ran;
  * the axis base picks its runner from the same result.

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_workflow_panel_integrity import (
    PANEL_MODELS_STEP,
    block_mapping,
    read_workflow,
    render,
    run_block,
    run_step,
    split_jobs,
    step_named,
)

KEY = "sk-ant-test-probe-0123456789"
MODEL = "claude-opus-5-5"
PROBE_STEP = "Probe the direct Anthropic key"
AXIS_BASE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "workflows", "cursor-axis-base.yml"))

USAGE_LIMIT = (400, {"type": "error", "error": {
    "type": "invalid_request_error",
    "message": "You have reached your specified API usage limits. "
               "You will regain access on 2026-11-01 at 00:00 UTC."}})
BILLING = (400, {"type": "error", "error": {
    "type": "billing_error", "message": "Your credit balance is too low to access the Anthropic API."}})
UNAUTHORIZED = (401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
RATE_LIMITED = (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "Number of requests exceeded"}})
OVERLOADED = (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
NOT_FOUND = (404, {"type": "error", "error": {"type": "not_found_error", "message": f"model: {MODEL}-typo"}})
INVALID_400 = (400, {"type": "error", "error": {"type": "invalid_request_error", "message": f"model: invalid model id {MODEL}-typo"}})
OTHER_400 = (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens: too small for this model"}})
MODEL_WORDED_400 = (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "invalid max_tokens for this model"}})
ALIAS_404 = (404, {"type": "error", "error": {"type": "not_found_error", "message": "model: opus"}})
# A response whose body ends early: e.read() raises IncompleteRead.
TRUNCATED = (500, {"type": "error", "error": {"type": "api_error", "message": "x" * 200}}, {"truncate": True})
OK = (200, {"id": "msg_1", "type": "message", "content": [{"type": "text", "text": "p"}]})
# An error message echoing the key must never reach the output or the log.
LEAKY = (401, {"type": "error", "error": {"type": "authentication_error", "message": f"bad key {KEY}"}})


class Stub:
    """Replays `responses` in order (the last one repeats), recording requests."""

    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                stub.requests.append({"headers": {k.lower(): v for k, v in self.headers.items()},
                                      "body": json.loads(self.rfile.read(length))})
                status, body, *extra = stub.responses[min(len(stub.requests), len(stub.responses)) - 1]
                extra = extra[0] if extra else {}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                if "location" in extra:
                    self.send_header("location", extra["location"])
                self.end_headers()
                self.wfile.write(data[:20] if extra.get("truncate") else data)
                if extra.get("truncate"):
                    self.close_connection = True

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/messages"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def gate_probe_step():
    body = step_named(split_jobs(read_workflow())["gate"], PROBE_STEP)
    if body is None:
        raise AssertionError(f"gate step `{PROBE_STEP}` is gone")
    return body


def axis_base_lines():
    with open(AXIS_BASE, encoding="utf-8") as f:
        return f.read().splitlines()


def axis_step(name):
    body = step_named(split_jobs(axis_base_lines())["axis-probe"], name)
    if body is None:
        raise AssertionError(f"axis-probe step `{name}` is gone")
    return body


def probe(step_lines, responses, context):
    """Run the probe step against a stub. (CompletedProcess, outputs, requests)."""
    stub = Stub(responses)
    try:
        with tempfile.TemporaryDirectory() as workdir:
            output = os.path.join(workdir, "github-output")
            open(output, "w", encoding="utf-8").close()
            env = {"PATH": os.environ.get("PATH", ""), "GITHUB_OUTPUT": output}
            for name, value in block_mapping(step_lines, "        env:", 10).items():
                env[name] = render(value, {"secrets.ANTHROPIC_API_KEY": KEY, **context})
            env.update(ANTHROPIC_PROBE_URL=stub.url, PROBE_RETRY_DELAYS="0 0")
            done = subprocess.run([shutil.which("bash"), "-e", "-c", run_block(step_lines)],
                                  env=env, cwd=workdir, capture_output=True, text=True, timeout=60)
            with open(output, encoding="utf-8") as f:
                text = f.read()
    finally:
        stub.close()
    outputs = dict(line.partition("=")[::2] for line in text.splitlines())
    return done, outputs, stub.requests, text


class ProbeClassifiesRecordedResponses(unittest.TestCase):
    CONTEXT = {"inputs.anthropic_direct_model": MODEL, "inputs.judge_direct_model": ""}

    def run_probe(self, *responses):
        done, outputs, requests, text = probe(gate_probe_step(), responses, self.CONTEXT)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for where in (done.stdout, done.stderr, text):
            self.assertNotIn(KEY, where)
        return outputs, requests

    def test_200_is_usable_with_one_one_token_call(self):
        outputs, requests = self.run_probe(OK)
        self.assertEqual(outputs["cells_probe"], "usable")
        self.assertNotIn("fallback_note", outputs)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["body"]["max_tokens"], 1)
        self.assertEqual(requests[0]["body"]["model"], MODEL)
        self.assertEqual(requests[0]["headers"].get("x-api-key"), KEY)

    def test_the_usage_limit_400_is_unusable_and_keeps_the_message(self):
        outputs, requests = self.run_probe(USAGE_LIMIT)
        self.assertEqual(outputs["cells_probe"], "unusable")
        self.assertTrue(outputs["cells_probe_reason"].startswith("usage-limit: "))
        self.assertIn("2026-11-01 at 00:00 UTC", outputs["cells_probe_reason"])
        self.assertIn("ran on Cursor: cells", outputs["fallback_note"])
        self.assertEqual(len(requests), 1)  # a refusal is not retried

    def test_a_billing_error_is_unusable(self):
        outputs, _ = self.run_probe(BILLING)
        self.assertEqual((outputs["cells_probe"], outputs["cells_probe_reason"].split(":")[0]),
                         ("unusable", "billing"))

    def test_401_is_unusable(self):
        outputs, requests = self.run_probe(UNAUTHORIZED)
        self.assertEqual((outputs["cells_probe"], outputs["cells_probe_reason"].split(":")[0]),
                         ("unusable", "unauthorized"))
        self.assertEqual(len(requests), 1)

    def test_429_then_200_keeps_the_direct_lane(self):
        outputs, requests = self.run_probe(RATE_LIMITED, OK)
        self.assertEqual(outputs["cells_probe"], "usable")
        self.assertEqual(len(requests), 2)

    def test_529_on_every_attempt_is_unusable(self):
        outputs, requests = self.run_probe(OVERLOADED)
        self.assertEqual((outputs["cells_probe"], outputs["cells_probe_reason"].split(":")[0]),
                         ("unusable", "unavailable"))
        self.assertEqual(len(requests), 3)

    def test_404_is_invalid_never_a_fallback(self):
        outputs, _ = self.run_probe(NOT_FOUND)
        self.assertEqual(outputs["cells_probe"], "invalid")
        self.assertNotIn("fallback_note", outputs)

    def test_a_400_is_never_invalid_whatever_it_says_about_the_model(self):
        # Only a 404 / not_found_error is an unknown id: a 400's wording is a
        # guess that would fail a valid config ("invalid max_tokens for this model").
        for response in (INVALID_400, MODEL_WORDED_400):
            with self.subTest(message=response[1]["error"]["message"]):
                outputs, _ = self.run_probe(response)
                self.assertEqual((outputs["cells_probe"], outputs["cells_probe_reason"].split(":")[0]),
                                 ("unusable", "http-400"))

    def test_a_404_on_a_claude_code_alias_is_not_probed(self):
        done, outputs, requests, _ = probe(gate_probe_step(), [ALIAS_404], {
            "inputs.anthropic_direct_model": "opus", "inputs.judge_direct_model": ""})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(requests), 1)
        self.assertEqual(outputs["cells_probe"], "")
        self.assertNotIn("fallback_note", outputs)

    def test_a_redirect_is_refused_never_followed_with_the_key(self):
        target = Stub([OK])
        try:
            outputs, requests = self.run_probe((302, {}, {"location": target.url}))
        finally:
            target.close()
        self.assertEqual(target.requests, [])
        self.assertEqual(len(requests), 1)
        self.assertEqual((outputs["cells_probe"], outputs["cells_probe_reason"]), ("unusable", "http-302"))

    def test_a_truncated_error_body_is_retried_not_a_crash(self):
        outputs, requests = self.run_probe(TRUNCATED, OK)
        self.assertEqual(outputs["cells_probe"], "usable")
        self.assertEqual(len(requests), 2)

    def test_any_other_400_is_unusable_not_invalid(self):
        outputs, _ = self.run_probe(OTHER_400)
        self.assertEqual((outputs["cells_probe"], outputs["cells_probe_reason"].split(":")[0]),
                         ("unusable", "http-400"))

    def test_a_message_echoing_the_key_is_redacted(self):
        outputs, _ = self.run_probe(LEAKY)
        self.assertIn("***", outputs["cells_probe_reason"])

    def test_one_call_serves_both_lanes_on_one_model(self):
        done, outputs, requests, _ = probe(gate_probe_step(), [USAGE_LIMIT], {
            "inputs.anthropic_direct_model": MODEL, "inputs.judge_direct_model": MODEL})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(requests), 1)
        self.assertEqual((outputs["cells_probe"], outputs["judge_probe"]), ("unusable", "unusable"))
        self.assertIn("ran on Cursor: cells, judge", outputs["fallback_note"])

    def test_two_models_failing_differently_each_keep_their_reason(self):
        done, outputs, requests, _ = probe(gate_probe_step(), [USAGE_LIMIT, BILLING], {
            "inputs.anthropic_direct_model": MODEL, "inputs.judge_direct_model": f"{MODEL}-judge"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(requests), 2)
        note = outputs["fallback_note"]
        self.assertIn("ran on Cursor: cells (usage-limit: ", note)
        self.assertIn("; judge (billing: ", note)
        self.assertNotIn("this run (usage-limit", note)

    def test_every_output_is_written_at_once_at_the_end(self):
        # A probe that dies on the second lane must not leave the first
        # decided and the note missing: one write, after both lanes.
        script = run_block(gate_probe_step())
        self.assertEqual(script.count('open(os.environ["GITHUB_OUTPUT"]'), 1)
        self.assertLess(script.index("for lane in"), script.index('open(os.environ["GITHUB_OUTPUT"]'))

    def test_the_retries_stop_at_the_budget(self):
        stub = Stub([OVERLOADED])
        try:
            with tempfile.TemporaryDirectory() as workdir:
                output = os.path.join(workdir, "github-output")
                open(output, "w").close()
                env = {"PATH": os.environ.get("PATH", ""), "GITHUB_OUTPUT": output, "ANTHROPIC_API_KEY": KEY,
                       "ANTHROPIC_PROBE_URL": stub.url, "PROBE_RETRY_DELAYS": "30 30",
                       "PROBE_BUDGET_SECONDS": "5", "PROBE_LANES": "cells", "PROBE_MODEL_CELLS": MODEL}
                done = subprocess.run([shutil.which("bash"), "-e", "-c", run_block(gate_probe_step())],
                                      env=env, capture_output=True, text=True, timeout=20)
                with open(output, encoding="utf-8") as f:
                    text = f.read()
        finally:
            stub.close()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(stub.requests), 1)  # the 30 s retry would overrun 5 s
        self.assertIn("cells_probe=unusable\n", text)

    def test_both_steps_bound_the_retries_inside_their_timeout(self):
        for step, minutes in ((gate_probe_step(), 5), (axis_step(PROBE_STEP), 3)):
            env = block_mapping(step, "        env:", 10)
            self.assertLess(int(env["PROBE_BUDGET_SECONDS"].strip("'")), minutes * 60 - 20)
            self.assertIn(f"timeout-minutes: {minutes}", "\n".join(step))

    def test_a_non_plain_id_is_not_probed(self):
        done, outputs, requests, _ = probe(gate_probe_step(), [OK], {
            "inputs.anthropic_direct_model": "claude opus", "inputs.judge_direct_model": ""})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((requests, outputs), ([], {}))


class ProbeWiring(unittest.TestCase):
    def test_the_axis_probe_is_the_gate_probe(self):
        self.assertEqual(run_block(axis_step(PROBE_STEP)), run_block(gate_probe_step()))

    def test_it_runs_only_for_a_round_with_the_key_and_never_fails_the_gate(self):
        text = "\n".join(gate_probe_step())
        self.assertIn("steps.check.outputs.should_run == 'true'", text)
        self.assertIn("steps.dup.outputs.already_reviewed != 'true'", text)
        self.assertIn("steps.anthropic_key.outputs.present == 'true'", text)
        self.assertIn("continue-on-error: true", text)
        self.assertIn("ANTHROPIC_PROBE_URL: https://api.anthropic.com/v1/messages", text)

    def test_the_post_step_names_the_fallback(self):
        jobs = split_jobs(read_workflow())
        post = "\n".join(step_named(jobs["post-review"], "Post review"))
        self.assertIn("RUNNER_NOTE: ${{ needs.gate.outputs.anthropic_fallback_note }}", post)
        self.assertEqual(post.count('--runner-note "$RUNNER_NOTE"'), 3)


class PreflightFallsBackOnAnUnusableKey(unittest.TestCase):
    def resolve(self, cells, judge, **extra):
        step = step_named(split_jobs(read_workflow())["preflight"], PANEL_MODELS_STEP)
        with tempfile.TemporaryDirectory() as workdir:
            summary = os.path.join(workdir, "summary")
            context = {
                "inputs.anthropic_direct_model": MODEL,
                "inputs.anthropic_direct_replaces_cursor_anthropic": "true",
                "inputs.judge_direct_model": MODEL,
                "needs.gate.outputs.anthropic_key_present": "true",
                "needs.gate.outputs.anthropic_cells_probe": cells,
                "needs.gate.outputs.anthropic_cells_probe_reason": "usage-limit: You have reached your specified API usage limits.",
                "needs.gate.outputs.anthropic_judge_probe": judge,
                "needs.gate.outputs.anthropic_judge_probe_reason": "usage-limit: You have reached your specified API usage limits.",
                **extra,
            }
            # run_step sets no GITHUB_STEP_SUMMARY; the fallback branch writes
            # one. Relative to the step's cwd, as run_step re-roots /tmp/.
            step = [line.replace('"$GITHUB_STEP_SUMMARY"', '"summary"') for line in step]
            done, outputs = run_step(step, context, workdir)
            written = ""
            if os.path.exists(summary):
                with open(summary, encoding="utf-8") as f:
                    written = f.read()
        return done, outputs, written

    def test_unusable_runs_the_cursor_cells_and_judge_with_one_warning_per_lane(self):
        done, outputs, summary = self.resolve("unusable", "unusable")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((outputs["anthropic_direct"], outputs["anthropic_direct_counts"],
                          outputs["judge_direct"]), ("false", "false", "false"))
        # The Cursor claude-* cells were NOT dropped.
        self.assertTrue(any(m.startswith("claude-") for m in json.loads(outputs["models"])))
        warnings = [l for l in done.stdout.splitlines() if l.startswith("::warning::") and "fell back" in l]
        self.assertEqual(len(warnings), 2, done.stdout)
        self.assertTrue(all("usage limits" in w for w in warnings))
        self.assertEqual(summary.count("fell back to Cursor"), 2)

    def test_usable_or_unprobed_changes_nothing(self):
        for status in ("usable", ""):
            with self.subTest(status=status):
                done, outputs, summary = self.resolve(status, status)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual((outputs["anthropic_direct"], outputs["judge_direct"]), ("true", "true"))
                self.assertFalse(any(m.startswith("claude-") for m in json.loads(outputs["models"])))
                self.assertEqual(summary, "")

    def test_invalid_fails_the_run(self):
        for cells, judge in (("invalid", "usable"), ("usable", "invalid")):
            with self.subTest(cells=cells, judge=judge):
                done, _, _ = self.resolve(cells, judge)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("::error::", done.stdout)


    def test_the_id_collision_warns_before_a_probe_verdict_can_fail_the_run(self):
        # An OpenAI id in both direct inputs: the gate probes it against the
        # Anthropic API (a 404), but this is the documented collision warning.
        done, outputs, _ = self.resolve("invalid", "usable", **{
            "inputs.anthropic_direct_model": "gpt-5.6", "inputs.openai_direct_model": "gpt-5.6",
            "needs.gate.outputs.openai_key_present": "true"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(outputs["anthropic_direct"], "false")
        self.assertIn("same id as openai_direct_model", done.stdout)
        self.assertNotIn("::error::", done.stdout)

    def test_a_panel_with_no_cursor_claude_cell_says_no_anthropic_cell_ran(self):
        done, outputs, summary = self.resolve("unusable", "usable", **{
            "inputs.panel_models": '["gpt-5.6-sol-xhigh", "kimi-k3-max"]'})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((outputs["anthropic_direct"], outputs["anthropic_cells_no_cursor"]), ("false", "true"))
        self.assertIn("no Anthropic review cell runs this round", done.stdout)
        self.assertNotIn("Cursor Anthropic cells run", done.stdout)
        self.assertIn("no Anthropic review cell ran", summary)
        done, outputs, _ = self.resolve("unusable", "usable")
        self.assertEqual(outputs["anthropic_cells_no_cursor"], "false")

    def test_the_post_step_corrects_the_note_for_a_panel_with_no_claude_cell(self):
        jobs = split_jobs(read_workflow())
        post = "\n".join(step_named(jobs["post-review"], "Post review"))
        self.assertIn("NO_CURSOR_ANTHROPIC: ${{ needs.preflight.outputs.anthropic_cells_no_cursor }}", post)
        self.assertIn("preflight", "\n".join(l for l in jobs["post-review"] if l.startswith("    needs:")))
        self.assertIn("anthropic_cells_no_cursor: ${{ steps.models.outputs.anthropic_cells_no_cursor }}",
                      "\n".join(jobs["preflight"]))


class AxisPicksItsRunner(unittest.TestCase):
    def pick(self, status):
        step = axis_step("Pick the runner")
        with tempfile.TemporaryDirectory() as workdir:
            output, summary = os.path.join(workdir, "out"), os.path.join(workdir, "summary")
            open(output, "w").close()
            open(summary, "w").close()
            env = {"PATH": os.environ.get("PATH", ""), "GITHUB_OUTPUT": output, "GITHUB_STEP_SUMMARY": summary,
                   "AXIS": "correctness", "AXIS_DIRECT_MODEL": MODEL, "PROBE": status,
                   "REASON": "usage-limit: You will regain access on 2026-11-01 at 00:00 UTC."}
            done = subprocess.run([shutil.which("bash"), "-e", "-c", run_block(step)],
                                  env=env, capture_output=True, text=True, timeout=30)
            with open(output, encoding="utf-8") as out, open(summary, encoding="utf-8") as summ:
                return done, out.read(), summ.read()

    def test_unusable_runs_cursor_with_a_warning_and_a_summary_line(self):
        done, output, summary = self.pick("unusable")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(output, "runner=cursor\n")
        self.assertIn("::warning::", done.stdout)
        self.assertIn("2026-11-01", done.stdout)
        self.assertIn("fell back to Cursor", summary)

    def test_usable_or_an_unprobed_key_runs_direct(self):
        for status in ("usable", ""):
            with self.subTest(status=status):
                done, output, summary = self.pick(status)
                self.assertEqual((done.returncode, output, summary), (0, "runner=direct\n", ""))

    def test_invalid_fails_the_axis(self):
        done, output, _ = self.pick("invalid")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(output, "")

    def test_the_no_fallback_checks_run_before_the_probe(self):
        script = run_block(axis_step("Check the direct inputs"))
        base = {"PATH": os.environ.get("PATH", ""), "AXIS": "correctness", "CHECKOUT": "true",
                "CONTEXT_SOURCES": "", "AXIS_DIRECT_MODEL": MODEL, "KEY_PRESENT": "true"}
        for extra, ok in (({}, True), ({"AXIS_DIRECT_MODEL": "claude opus"}, False),
                          ({"CONTEXT_SOURCES": "linear"}, False), ({"KEY_PRESENT": "false"}, False),
                          ({"AXIS": "business"}, False), ({"CHECKOUT": "false"}, False)):
            with self.subTest(extra=extra):
                done = subprocess.run([shutil.which("bash"), "-e", "-c", script], env={**base, **extra},
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode == 0, ok, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
