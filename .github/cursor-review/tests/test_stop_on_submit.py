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

    def test_a_fifo_is_not_submitted_and_does_not_block(self):
        # A blocking open() on a FIFO with no writer would hang the poll forever.
        os.mkfifo(self.path)
        self.assertFalse(stop_on_submit.submitted(self.path))

    def test_a_symlink_is_not_submitted(self):
        target = os.path.join(self.dir.name, "real.json")
        with open(target, "w") as f:
            json.dump(OK, f)
        os.symlink(target, self.path)
        self.assertFalse(stop_on_submit.submitted(self.path))

    def test_an_oversized_file_is_not_submitted(self):
        padding = "x" * (stop_on_submit.MAX_FINDINGS_BYTES)
        self.write(json.dumps({"status": "ok", "pad": padding}))
        self.assertFalse(stop_on_submit.submitted(self.path))


class AgentHarness(unittest.TestCase):
    """Runs the real wrapper over a stub agent; holds no tests of its own."""

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

    def wrap(self, body, *, cap=None, extra=TIMINGS, stdin=None, stdout=None):
        return subprocess.Popen(
            [sys.executable, SCRIPT, "--findings", self.path, *extra, "--",
             *self.agent(body)],
            stdin=stdin,
            stdout=stdout,
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

class WrapperTest(AgentHarness):
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

    def wait_for_pids(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.path.exists(self.pidfile) and len(self.pids()) >= count:
                return
            time.sleep(0.05)
        self.fail(f"stub never recorded {count} pid(s)")

    def test_signal_trapping_agent_is_killed_and_reported_as_signalled(self):
        # An agent that traps the forwarded signal and stays up must not hold
        # the wrapper (and the step) until the runner's unforwardable kill; one
        # that traps it and exits 0 must not read as success either.
        proc = self.wrap("""\
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            kid = subprocess.Popen([sys.executable, "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"])
            record_pid(kid.pid)
            time.sleep(600)
            """, extra=["--poll", "5", "--linger", "5", "--grace", "0.5"])
        self.wait_for_pids(2)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        code, err, _ = self.finish(proc, cap=10)
        self.assertEqual(code, 128 + signal.SIGTERM, err)
        self.assertLess(time.monotonic() - started, 3, "signal not noticed mid-poll")
        self.assertIn("sending SIGKILL", err)
        for pid in self.pids():
            self.assert_gone(pid)

    def test_agent_that_exits_on_its_own_leaves_nothing_behind(self):
        # A grandchild still in the agent's group could rewrite findings.json
        # after the upload; the wrapper reaps the group on this path too.
        proc = self.wrap("""\
            kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
            record_pid(kid.pid)
            sys.exit(4)
            """)
        code, err, _ = self.finish(proc, cap=10)
        self.assertEqual(code, 4, err)
        for pid in self.pids():
            self.assert_gone(pid)

    def test_non_finite_timings_are_a_usage_error(self):
        for flag in ("--poll", "--linger", "--grace"):
            for value in ("nan", "inf", "0", "-1"):
                with self.subTest(flag=flag, value=value):
                    result = subprocess.run(
                        [sys.executable, SCRIPT, "--findings", self.path, flag, value,
                         "--", sys.executable, "-c", "pass"],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)

    def test_missing_command_is_a_usage_error(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--findings", self.path, "--"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)


SEEDED = {"status": "error", "error": "x", "findings": [], "model": "m",
          "review_type": "adversarial"}
EXHAUSTED = "RetriableError: [resource_exhausted] Error"
RETRY = ["--retry-delays", "0.2,0.4", "--retry-window", "30",
         "--cell", "adversarial/kimi-k3-high"]
# A multi-line prompt about the size of a real diff review (~120 KB).
PROMPT = b"Review this diff.\n" + b"+an added line\n" * 8192
# What cursor-agent prints, then exits 1, when its stdin is empty.
NO_PROMPT = "Error: No prompt provided for print mode"


class RetryTest(AgentHarness):
    """A non-zero exit carrying Cursor's retriable marker re-runs the agent.

    The wrapper's stdin is the prompt, a regular file, as the workflow feeds
    it (`< /tmp/prompt.txt`): a retry rewinds it, and a stdin that cannot seek
    rules the retry out. Feeding it explicitly also keeps these tests off the
    test runner's own stdin, which is a pipe on a GitHub runner.
    """

    def setUp(self):
        super().setUp()
        with open(self.path, "w") as f:
            json.dump(SEEDED, f)
        self.prompt = os.path.join(self.dir.name, "prompt.txt")
        with open(self.prompt, "wb") as f:
            f.write(PROMPT)

    def prompt_file(self, offset=0):
        """The prompt opened for the wrapper's stdin, positioned at `offset`."""
        source = open(self.prompt, "rb", buffering=0)
        self.addCleanup(source.close)
        source.seek(offset)
        return source

    def stub(self, attempt_body):
        """An agent that counts its runs; `attempt_body` sees ATTEMPT (1-based)."""
        return (f"ATTEMPT = len(open({self.pidfile!r}).read().split())\n"
                + textwrap.dedent(attempt_body))

    def reading(self, attempt_body):
        """A stub that first reads its whole stdin, as cursor-agent does, keeps
        a copy per attempt, and fails like cursor-agent when it is empty."""
        return textwrap.dedent(f"""\
            PROMPT_READ = sys.stdin.buffer.read()
            with open(os.path.join({self.dir.name!r}, f"prompt-{{ATTEMPT}}"), "wb") as f:
                f.write(PROMPT_READ)
            if not PROMPT_READ:
                print({NO_PROMPT!r}, file=sys.stderr)
                sys.exit(1)
            """) + textwrap.dedent(attempt_body)

    def prompts_read(self):
        """What each attempt of a `reading` stub read from its stdin, in order."""
        reads = []
        for attempt in range(1, len(self.pids()) + 1):
            with open(os.path.join(self.dir.name, f"prompt-{attempt}"), "rb") as f:
                reads.append(f.read())
        return reads

    def retry(self, attempt_body, extra=None, stdin=None):
        proc = self.wrap(self.stub(attempt_body), extra=TIMINGS + (extra or RETRY),
                         stdin=self.prompt_file() if stdin is None else stdin)
        return self.finish(proc, cap=20)

    def test_resource_exhausted_then_success_is_retried(self):
        code, err, _ = self.retry(f"""\
            if ATTEMPT == 1:
                print({EXHAUSTED!r}, file=sys.stderr)
                sys.exit(1)
            write({json.dumps(json.dumps(OK))})
            sys.exit(0)
            """)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.pids()), 2)
        warnings = [l for l in err.splitlines() if l.startswith("::warning::")]
        self.assertEqual(warnings, [
            "::warning::Reviewer cell adversarial/kimi-k3-high: attempt 1/3 failed "
            "with Cursor's retriable error [resource_exhausted] (exit 1); retrying in 0.2s"])
        # The agent's own stderr still reaches the wrapper's, unchanged, and
        # one that already ended its line gets no blank line before the warning.
        self.assertIn(EXHAUSTED + "\n::warning::", err)
        with open(self.path) as f:
            self.assertEqual(json.load(f)["status"], "ok")

    def test_every_retry_reads_the_whole_prompt_again(self):
        # Each attempt inherits the same stdin, offset included, and the first
        # reads it to EOF: without a rewind every retry would get an empty
        # prompt and fail at once, so a retry could never recover a cell.
        code, err, _ = self.retry(self.reading(f"""\
            if ATTEMPT < 3:
                print({EXHAUSTED!r}, file=sys.stderr)
                sys.exit(1)
            write({json.dumps(json.dumps(OK))})
            """))
        self.assertEqual(code, 0, err)
        self.assertNotIn(NO_PROMPT, err)
        self.assertEqual(self.prompts_read(), [PROMPT] * 3)

    def test_the_prompt_is_replayed_from_where_stdin_started(self):
        # The rewind goes back to the offset stdin had when the wrapper
        # started, not to the start of the file.
        start = len(b"Review this diff.\n")
        code, err, _ = self.retry(self.reading(f"""\
            if ATTEMPT == 1:
                print({EXHAUSTED!r}, file=sys.stderr)
                sys.exit(1)
            write({json.dumps(json.dumps(OK))})
            """), stdin=self.prompt_file(start))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.prompts_read(), [PROMPT[start:]] * 2)

    def test_a_stdin_that_cannot_seek_rules_the_retry_out(self):
        # A pipe cannot be replayed, so a retry could only run on an empty
        # prompt: the first failure stands, and the wrapper logs why, once.
        prompt = PROMPT[:1024]  # fits the pipe buffer, so the write cannot block
        read_end, write_end = os.pipe()
        os.write(write_end, prompt)
        os.close(write_end)
        try:
            code, err, _ = self.retry(self.reading(f"""\
                print({EXHAUSTED!r}, file=sys.stderr)
                sys.exit(1)
                """), stdin=read_end)
        finally:
            os.close(read_end)
        self.assertEqual(code, 1, err)
        self.assertEqual(self.prompts_read(), [prompt])
        self.assertNotIn("::warning::", err)
        self.assertEqual(err.count("cannot be rewound to replay the prompt"), 1, err)
        self.assertIn("not retrying", err)

    def test_the_warning_starts_a_line_after_an_unterminated_stderr(self):
        # The runner only parses `::warning::` at the start of a line; an agent
        # whose last stderr write has no newline must not swallow it.
        code, err, _ = self.retry(f"""\
            if ATTEMPT == 1:
                sys.stderr.write({EXHAUSTED!r})
                sys.exit(1)
            write({json.dumps(json.dumps(OK))})
            """)
        self.assertEqual(code, 0, err)
        self.assertIn(EXHAUSTED + "\n::warning::Reviewer cell adversarial/kimi-k3-high: "
                      "attempt 1/3 failed", err)

    def test_marker_on_stdout_is_not_retried_and_stdout_passes_through(self):
        # stdout is the model's own text, which the PR under review can steer;
        # only the agent's stderr may call for a retry.
        proc = self.wrap(self.stub(f"""\
            print("out-" + str(ATTEMPT))
            print({EXHAUSTED!r})
            sys.exit(1)
            """), extra=TIMINGS + RETRY, stdin=self.prompt_file(),
            stdout=subprocess.PIPE)
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 1, err)
        self.assertEqual(out, "out-1\n" + EXHAUSTED + "\n")
        self.assertEqual(len(self.pids()), 1)
        self.assertNotIn("::warning::", err)
        self.assertNotIn("not retrying", err)

    def test_persistent_resource_exhausted_exhausts_retries(self):
        code, err, took = self.retry(f"""\
            print({EXHAUSTED!r}, file=sys.stderr)
            sys.exit(1)
            """)
        self.assertEqual(code, 1, err)
        self.assertEqual(len(self.pids()), 3)
        warnings = [l for l in err.splitlines() if l.startswith("::warning::")]
        self.assertEqual(len(warnings), 2, err)
        self.assertIn("attempt 2/3", warnings[1])
        self.assertIn("retrying in 0.4s", warnings[1])
        self.assertGreaterEqual(took, 0.6)
        with open(self.path) as f:
            self.assertEqual(json.load(f), SEEDED)

    def test_other_errors_are_not_retried(self):
        code, err, _ = self.retry("""\
            print("Error: invalid model", file=sys.stderr)
            sys.exit(1)
            """)
        self.assertEqual(code, 1, err)
        self.assertEqual(len(self.pids()), 1)
        self.assertNotIn("::warning::", err)

    def test_marker_with_exit_zero_or_signal_death_is_not_retried(self):
        for ending, expected in (("sys.exit(0)", 0),
                                 ("os.kill(os.getpid(), signal.SIGKILL)", 128 + 9)):
            with self.subTest(ending=ending):
                open(self.pidfile, "w").close()
                code, err, _ = self.retry(f"""\
                    print({EXHAUSTED!r}, file=sys.stderr, flush=True)
                    {ending}
                    """)
                self.assertEqual(code, expected, err)
                self.assertEqual(len(self.pids()), 1)
                self.assertNotIn("::warning::", err)

    def test_no_retry_without_retry_delays(self):
        code, err, _ = self.retry(f"""\
            print({EXHAUSTED!r}, file=sys.stderr)
            sys.exit(1)
            """, extra=["--cell", "a/b"])
        self.assertEqual(code, 1, err)
        self.assertEqual(len(self.pids()), 1)

    def test_submitted_or_partially_recorded_cells_are_never_rerun(self):
        records = (
            OK,
            {"status": "error", "findings": [{"file": "a.py", "line": 1}]},
            "not json",
        )
        for record in records:
            with self.subTest(record=record):
                open(self.pidfile, "w").close()
                text = record if isinstance(record, str) else json.dumps(record)
                code, err, _ = self.retry(f"""\
                    write({text!r})
                    print({EXHAUSTED!r}, file=sys.stderr)
                    sys.exit(1)
                    """)
                self.assertEqual(code, 1, err)
                self.assertEqual(len(self.pids()), 1)
                self.assertNotIn("::warning::", err)

    def test_retry_that_would_start_past_the_window_is_skipped(self):
        code, err, _ = self.retry(f"""\
            print({EXHAUSTED!r}, file=sys.stderr)
            sys.exit(1)
            """, extra=["--retry-delays", "5", "--retry-window", "1", "--cell", "a/b"])
        self.assertEqual(code, 1, err)
        self.assertEqual(len(self.pids()), 1)
        self.assertIn("retry window", err)

    def test_signal_during_backoff_ends_the_wrapper_without_retrying(self):
        # The step cap landing mid-backoff: no further attempt may start.
        proc = self.wrap(self.stub(f"""\
            print({EXHAUSTED!r}, file=sys.stderr)
            sys.exit(1)
            """), extra=TIMINGS + ["--retry-delays", "30", "--retry-window", "60"],
            stdin=self.prompt_file())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
                os.path.exists(self.pidfile) and self.pids()):
            time.sleep(0.05)
        time.sleep(1)  # the stub exits at once; the wrapper is now backing off
        proc.send_signal(signal.SIGTERM)
        code, err, _ = self.finish(proc, cap=5)
        self.assertEqual(code, 128 + signal.SIGTERM, err)
        self.assertIn("::warning::", err)
        self.assertIn("during the retry backoff", err)
        self.assertEqual(len(self.pids()), 1)

    def test_cell_label_is_sanitized_in_the_warning(self):
        code, err, _ = self.retry(f"""\
            if ATTEMPT == 1:
                print({EXHAUSTED!r}, file=sys.stderr)
                sys.exit(1)
            write({json.dumps(json.dumps(OK))})
            """, extra=["--retry-delays", "0", "--retry-window", "30",
                        "--cell", "a/b\n::error::x"])
        self.assertEqual(code, 0, err)
        self.assertIn("::warning::Reviewer cell a/b___error__x:", err)
        self.assertNotIn("\n::error::", err)

    def test_bad_retry_delays_are_a_usage_error(self):
        for value in ("x", "-1", "nan"):
            with self.subTest(value=value):
                result = subprocess.run(
                    [sys.executable, SCRIPT, "--findings", self.path,
                     "--retry-delays", value, "--", "true"],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)


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
        # The runner signals the step's shell, not the wrapper: the shell must
        # run the wrapper in the background and forward the signals to it.
        self.assertIn("> /tmp/review-raw.txt 2>/tmp/review-stderr.txt &\n", step)
        for sig in ("INT", "TERM", "HUP"):
            self.assertIn(f"trap 'forward_signal {sig}' {sig}", step)
        self.assertLess(step.index("trap 'forward_signal TERM'"), step.index("stop-on-submit.py\""))
        self.assertIn("timeout-minutes: 15", step)
        # Transient Cursor capacity errors are retried, named per cell.
        self.assertIn("--retry-delays 30,90", step)
        self.assertIn('--cell "$REVIEW_TYPE/$MODEL"', step)
        self.assertIn("REVIEW_TYPE: ${{ matrix.review_type }}", step)
        # A retry replays the prompt by rewinding the wrapper's stdin, so the
        # prompt must arrive as a file redirect: a pipe cannot be rewound, and
        # would turn every retry off.
        self.assertIn("< /tmp/prompt.txt \\\n", step)


if __name__ == "__main__":
    unittest.main()
