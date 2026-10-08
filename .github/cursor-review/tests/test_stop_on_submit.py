#!/usr/bin/env python3
"""Tests for stop-on-submit.py, the wrapper that ends a submitted panel cell.

Two layers. `submitted()` is the trigger, so it is pinned case by case: only a
complete JSON object whose status is the exact string "ok" may stop a cell.
The end-to-end tests run the real wrapper against a stub agent (a Python child
that writes findings.json and then hangs, the way a real stalled cell does)
with sub-second timings, standing in for the 15-minute step cap with a short
subprocess timeout.

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py' -v
"""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

SCRIPT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "stop-on-submit.py")
)
SPEC = importlib.util.spec_from_file_location("stop_on_submit", SCRIPT)
stop_on_submit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stop_on_submit)

OK = {"status": "ok", "model": "m", "review_type": "adversarial", "findings": []}
TIMINGS = ["--poll", "0.1", "--linger", "0.3", "--grace", "0.5"]


class SubmittedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "findings.json")

    def tearDown(self):
        self.dir.cleanup()

    def write(self, data):
        with open(self.path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def test_status_ok_is_submitted(self):
        self.write(json.dumps(OK))
        self.assertTrue(stop_on_submit.submitted(self.path))

    def test_never_written_is_not_submitted(self):
        self.assertFalse(stop_on_submit.submitted(self.path))

    def test_error_status_is_not_submitted(self):
        # The pre-seeded artifact, and the shape record_finding leaves mid-review.
        self.write(json.dumps({"status": "error", "error": "x", "findings": []}))
        self.assertFalse(stop_on_submit.submitted(self.path))

    def test_partial_write_is_not_submitted(self):
        whole = json.dumps(OK)
        for cut in range(len(whole)):
            self.write(whole[:cut])
            self.assertFalse(stop_on_submit.submitted(self.path), whole[:cut])

    def test_non_object_and_non_string_status_are_not_submitted(self):
        for body in ('["ok"]', '"ok"', '{"status": true}', '{"status": "OK"}',
                     '{"status": "ok "}', '{"findings": []}', "null"):
            self.write(body)
            self.assertFalse(stop_on_submit.submitted(self.path), body)

    def test_invalid_utf8_and_deep_nesting_are_not_submitted(self):
        self.write(b'{"status": "ok", "x": "\xff"}')
        self.assertFalse(stop_on_submit.submitted(self.path))
        self.write("[" * 100000 + "]" * 100000)
        self.assertFalse(stop_on_submit.submitted(self.path))

    def test_a_directory_is_not_submitted(self):
        os.mkdir(self.path)
        self.assertFalse(stop_on_submit.submitted(self.path))


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "findings.json")
        self.pidfile = os.path.join(self.dir.name, "pids")

    def tearDown(self):
        # Never leave a stub behind, whatever a failing assertion skipped.
        self.kill_stubs()
        self.dir.cleanup()

    def kill_stubs(self):
        if os.path.exists(self.pidfile):
            with open(self.pidfile) as f:
                for line in f:
                    try:
                        os.kill(int(line), signal.SIGKILL)
                    except (ProcessLookupError, ValueError):
                        pass

    def agent(self, body):
        """A stub agent: records its pid, then runs `body` (Python)."""
        prelude = textwrap.dedent(f"""\
            import json, os, signal, subprocess, sys, time
            PATH = {self.path!r}
            def record_pid(pid):
                with open({self.pidfile!r}, "a") as f:
                    f.write(f"{{pid}}\\n")
            record_pid(os.getpid())
            def write(text):
                tmp = PATH + ".tmp"
                with open(tmp, "w") as f:
                    f.write(text)
                os.replace(tmp, PATH)
            """)
        return [sys.executable, "-c", prelude + textwrap.dedent(body)]

    def wrap(self, body, *, cap=None, extra=TIMINGS):
        return subprocess.Popen(
            [sys.executable, SCRIPT, "--findings", self.path, *extra, "--",
             *self.agent(body)],
            stderr=subprocess.PIPE,
            text=True,
        )

    def finish(self, proc, cap):
        """(exit code, stderr, seconds), or (None, …) if still up at `cap`."""
        started = time.monotonic()
        try:
            _, err = proc.communicate(timeout=cap)
            return proc.returncode, err, time.monotonic() - started
        except subprocess.TimeoutExpired:
            # Still up at the stand-in cap. Kill the stubs first: they inherit
            # the stderr pipe, so communicate() would otherwise wait them out.
            proc.kill()
            self.kill_stubs()
            _, err = proc.communicate()
            return None, err, time.monotonic() - started

    def pids(self):
        with open(self.pidfile) as f:
            return [int(line) for line in f]

    def assert_gone(self, pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"pid {pid} is still running")

    def test_ok_cell_is_stopped_early_and_findings_are_untouched(self):
        proc = self.wrap(f"""\
            write({json.dumps(json.dumps(OK))})
            time.sleep(600)
            """)
        code, err, took = self.finish(proc, cap=10)
        self.assertEqual(code, 0, err)
        self.assertLess(took, 5)
        self.assertIn("sending SIGTERM", err)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), json.dumps(OK).encode("utf-8"))
        self.assert_gone(self.pids()[0])

    def test_findings_bytes_are_exactly_what_the_agent_wrote(self):
        # Odd formatting and key order survive: the wrapper never rewrites.
        raw = '{ "findings":[{"file":"a.py","line":1}] ,\n "status" : "ok"}\n'
        proc = self.wrap(f"""\
            write({raw!r})
            time.sleep(600)
            """)
        code, err, _ = self.finish(proc, cap=10)
        self.assertEqual(code, 0, err)
        with open(self.path) as f:
            self.assertEqual(f.read(), raw)

    def test_not_ok_cell_runs_to_the_cap(self):
        proc = self.wrap("""\
            write(json.dumps({"status": "error", "findings": []}))
            time.sleep(600)
            """)
        code, err, _ = self.finish(proc, cap=3)
        self.assertIsNone(code, f"wrapper exited ({code}) on a non-ok cell: {err}")
        self.assertNotIn("SIGTERM", err)

    def test_partial_write_does_not_stop_the_cell(self):
        # Written in place (no rename), and never completed.
        proc = self.wrap(f"""\
            with open(PATH, "w") as f:
                f.write({json.dumps(OK)[:-5]!r})
            time.sleep(600)
            """)
        code, err, _ = self.finish(proc, cap=3)
        self.assertIsNone(code, err)

    def test_never_written_runs_to_the_cap(self):
        proc = self.wrap("time.sleep(600)\n")
        code, err, _ = self.finish(proc, cap=3)
        self.assertIsNone(code, err)

    def test_agent_exit_code_passes_through(self):
        for status in ("error", "ok"):
            with self.subTest(status=status):
                proc = self.wrap(f"""\
                    write(json.dumps({{"status": {status!r}}}))
                    sys.exit(3)
                    """)
                code, err, _ = self.finish(proc, cap=10)
                self.assertEqual(code, 3, err)
                self.assertNotIn("SIGTERM", err)

    def test_agent_that_exits_during_the_linger_is_not_signalled(self):
        proc = self.wrap(f"""\
            write({json.dumps(json.dumps(OK))})
            time.sleep(0.2)
            sys.exit(0)
            """, extra=["--poll", "0.05", "--linger", "5", "--grace", "1"])
        code, err, _ = self.finish(proc, cap=10)
        self.assertEqual(code, 0, err)
        self.assertNotIn("SIGTERM", err)

    def test_status_that_reverts_during_the_linger_is_not_stopped(self):
        proc = self.wrap(f"""\
            write({json.dumps(json.dumps(OK))})
            time.sleep(0.1)
            write(json.dumps({{"status": "error"}}))
            time.sleep(600)
            """, extra=["--poll", "0.05", "--linger", "0.5", "--grace", "0.5"])
        code, err, _ = self.finish(proc, cap=3)
        self.assertIsNone(code, err)

    def test_sigterm_ignoring_agent_and_its_children_are_killed(self):
        proc = self.wrap(f"""\
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            kid = subprocess.Popen([sys.executable, "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"])
            record_pid(kid.pid)
            write({json.dumps(json.dumps(OK))})
            time.sleep(600)
            """)
        code, err, took = self.finish(proc, cap=10)
        self.assertEqual(code, 0, err)
        self.assertIn("sending SIGKILL", err)
        self.assertLess(took, 5)
        for pid in self.pids():
            self.assert_gone(pid)

    def test_signals_to_the_wrapper_reach_the_agent(self):
        # The step cap and a run cancel signal the wrapper, not the agent's
        # separate process group; the agent must still die with it.
        proc = self.wrap("time.sleep(600)\n")
        deadline = time.monotonic() + 5
        while not os.path.exists(self.pidfile) and time.monotonic() < deadline:
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        code, err, _ = self.finish(proc, cap=5)
        self.assertEqual(code, 128 + signal.SIGTERM, err)
        self.assert_gone(self.pids()[0])

    def test_missing_command_is_a_usage_error(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--findings", self.path, "--"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)


class WorkflowWiringTest(unittest.TestCase):
    def test_run_step_wraps_the_reviewer_agent(self):
        workflow = os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "..", "workflows", "cursor-review.yml"))
        with open(workflow, encoding="utf-8") as f:
            text = f.read()
        step = text.split("      - name: Run cursor review\n", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn('python3 "$CURSOR_REVIEW_ASSETS/stop-on-submit.py"', step)
        self.assertIn("--findings /tmp/findings-out/findings.json --", step)
        self.assertLess(step.index("stop-on-submit.py"), step.index("cursor-agent \\"))
        self.assertIn("timeout-minutes: 15", step)


if __name__ == "__main__":
    unittest.main()
