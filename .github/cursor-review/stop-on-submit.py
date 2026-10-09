#!/usr/bin/env python3
"""Run a panel cell's agent, and end it once its findings.json says status=ok.

A reviewer cell's work is done the moment `cursor_review_finish` writes
`status: ok` to its findings.json (review-output-mcp.py), but cursor-agent does
not always exit then: some cells sat on a finished review until the 15-minute
`Run cursor review` step cap killed them. This wrapper runs the agent command
and polls the findings file while it runs. Once the file exists, parses as a
JSON object and carries the exact string `"status": "ok"`, the agent gets
`--linger` seconds to exit on its own, then SIGTERM to its whole process group,
then SIGKILL after `--grace` seconds. With the defaults (5s poll, 15s linger,
10s grace) a submitted cell is gone within 30 seconds of its submission.

What it deliberately does NOT do:
  * touch the findings file — it only reads it, so the bytes the agent's MCP
    tool wrote are exactly the bytes the upload, the leg check and the judge
    see;
  * stop anything that has not submitted — a missing, partial, unparseable,
    non-object or non-`ok` file is "keep running", so a cell that never
    submits still runs to the step cap and is reported exactly as before;
  * outlive its agent's process group — every exit path, including the agent
    exiting by itself and an exception here, ends with SIGKILL to the group,
    so nothing the agent left running can rewrite the findings afterwards.

Retry on a transient Cursor capacity error (`--retry-delays`): when the agent
exits non-zero by itself and the tail of its stderr carries Cursor's retriable
marker (`[resource_exhausted]` or `RetriableError`), the agent is run again
after the next delay, up to one retry per delay. Only stderr is matched: stdout
is the model's own text, which the PR under review can steer. A retry happens
only when the findings file still reads as an untouched, unsubmitted record — a
parseable object, status not "ok", and NO recorded findings — because the MCP
tool appends to that file, so re-running a cell that already recorded findings
would duplicate them. It is also skipped once `--retry-window` seconds
(measured from the wrapper's start, including the delay) would be exceeded, so
a retry starts only while a full review still fits inside the step cap.
Anything else — exit 0, a different error, death by signal, a forwarded signal
(the step cap) — is reported exactly as without retries. Each retry prints one
`::warning::` line, on a line of its own, naming the `--cell`, the attempt and
the matched marker. The agent's stdout is inherited untouched; its stderr still
reaches this wrapper's stderr unchanged, only teed through here so its tail can
be matched.

The prompt is the agent's stdin (`< /tmp/prompt.txt`), one open file whose
offset every attempt shares, and the first attempt reads it to EOF. So the
offset stdin has at startup is recorded, and stdin is put back there before
every retry; without that, the retry would read an empty prompt and fail at
once. A stdin that cannot seek (a pipe, a tty) cannot be replayed, so it rules
the retry out, and the wrapper logs why.

A SIGINT/SIGTERM/SIGHUP to this wrapper (the step cap, a run cancel) is passed
to the agent's group; the agent then gets at most 5 seconds before SIGKILL —
inside the runner's own SIGINT -> SIGTERM -> kill escalation — and the wrapper
exits 128+signal whatever the agent's own status was.

Exit status: the (last attempt's) agent's own exit code when it exits by
itself (before or after submitting); 0 when this wrapper stopped it after a
verified submission; 128+signal when a forwarded signal ended it.
Status lines go to stderr with a `stop-on-submit:` prefix.

Usage:
  stop-on-submit.py --findings /tmp/findings-out/findings.json \
    [--retry-delays 30,90 --cell adversarial/model] -- cursor-agent ...
"""

import argparse
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time

FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# A real findings.json is at most a few hundred KB (review-output-mcp.py caps
# each finding); anything larger is not a submission, and never read whole.
MAX_FINDINGS_BYTES = 8 * 1024 * 1024
# Upper bound on the agent's grace once the wrapper itself is signalled: the
# runner escalates SIGINT -> SIGTERM 7.5s later -> kill, and the group must be
# SIGKILLed before the wrapper is, or the agent outlives the step.
SIGNAL_GRACE = 5.0
# How often a wait re-checks for a forwarded signal.
WAIT_SLICE = 0.1
# Cursor's own label for a transient capacity error, e.g.
# `RetriableError: [resource_exhausted] Error`. Matched on raw bytes; the
# warning names only the fixed marker, never agent-printed text.
# The specific code is preferred over the generic class when both appear.
RETRIABLE = (
    ("resource_exhausted", re.compile(rb"\[resource_exhausted\]")),
    ("RetriableError", re.compile(rb"\bRetriableError\b")),
)
# How much of the end of the agent's stderr is kept for the match. The marker
# is the last thing a failed run prints.
TAIL_BYTES = 64 * 1024
# How long to wait for stderr to drain after the agent's group is killed.
# Bounded so a stray process that escaped the group cannot hang the wrapper.
DRAIN_TIMEOUT = 5.0


def log(message):
    print(f"stop-on-submit: {message}", file=sys.stderr, flush=True)


def read_findings(path):
    """The findings record as a dict, or None for anything that is not one.

    The file is agent-writable (the agent runs `--trust` with a shell), so
    every failure mode reads as "not submitted" rather than raising or
    blocking: only a regular file is read (O_NONBLOCK|O_NOFOLLOW, so a FIFO or
    a symlink to a device cannot stall or flood the poll), at most
    MAX_FINDINGS_BYTES of it; `ValueError` covers both JSONDecodeError and the
    UnicodeDecodeError that invalid UTF-8 raises; RecursionError covers
    pathologically nested input.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with open(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return None
            data = source.read(MAX_FINDINGS_BYTES + 1)
        if len(data) > MAX_FINDINGS_BYTES:
            return None
        record = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError, MemoryError):
        return None
    return record if isinstance(record, dict) else None


def submitted(path):
    """True only for a complete, parseable JSON object with status == "ok"."""
    record = read_findings(path)
    return record is not None and record.get("status") == "ok"


def retry_safe(path):
    """True only for an unsubmitted record with no findings recorded yet.

    review-output-mcp.py appends each recorded finding to the file, so a re-run
    on top of a partial review would duplicate findings; and an unreadable file
    is not known to be untouched. Both are left to fail as they always did.
    """
    record = read_findings(path)
    return (record is not None and record.get("status") != "ok"
            and record.get("findings") == [])


class Tee:
    """Copy a child's pipe to `sink_fd` unchanged, keeping its last TAIL_BYTES."""

    def __init__(self, source, sink_fd):
        self.source = source
        self.sink_fd = sink_fd
        self.tail = b""
        self.thread = threading.Thread(target=self.pump, daemon=True)
        self.thread.start()

    def pump(self):
        fd = self.source.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.tail = (self.tail + chunk)[-TAIL_BYTES:]
            view = memoryview(chunk)
            while view:
                try:
                    view = view[os.write(self.sink_fd, view):]
                except OSError:
                    break

    def drain(self):
        self.thread.join(DRAIN_TIMEOUT)
        return self.tail


def retriable_marker(stderr_tail):
    """The fixed marker name Cursor's retriable error carries, or None.

    Only the agent's stderr is searched, never its stdout: that is the model's
    own output, which text in the PR under review can steer.
    """
    for name, pattern in RETRIABLE:
        if pattern.search(stderr_tail):
            return name
    return None


def fresh_line(stderr_tail):
    """A newline when the agent's stderr ended mid-line, else nothing.

    The agent's stderr is copied into the wrapper's, and its last write may
    lack a newline; a `::warning::` glued onto it is not a workflow command
    (the runner only parses one at the start of a line).
    """
    return "" if not stderr_tail or stderr_tail.endswith(b"\n") else "\n"


class Prompt:
    """The agent's stdin, put back where it started before each retry.

    Every attempt inherits the same open stdin, offset included, and the first
    one reads it to EOF, so a retry left alone reads an empty prompt (and
    cursor-agent fails at once with "No prompt provided for print mode"). The
    offset is recorded before the first attempt; a stdin that cannot seek — a
    pipe, a tty, a closed descriptor — cannot be replayed, and `problem` says
    why.
    """

    def __init__(self, fd=0):
        self.fd = fd
        self.problem = None
        try:
            self.offset = os.lseek(fd, 0, os.SEEK_CUR)
        except OSError as exc:
            self.offset, self.problem = None, exc.strerror or repr(exc)

    def rewind(self):
        """True once stdin is back at the offset it had at startup."""
        if self.offset is None:
            return False
        try:
            os.lseek(self.fd, self.offset, os.SEEK_SET)
        except OSError as exc:
            self.problem = exc.strerror or repr(exc)
            return False
        return True


def safe_label(text):
    """A ::warning:: -safe rendering of a caller-supplied cell label."""
    return re.sub(r"[^A-Za-z0-9._/@+-]", "_", text)[:200]


def signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def wait(child, timeout, interrupted=lambda: False):
    """The child's exit code, or None if it is still running after `timeout`
    (or as soon as `interrupted()` turns true)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return child.wait(timeout=max(0.0, min(WAIT_SLICE, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            if interrupted() or time.monotonic() >= deadline:
                return None


def run(command, findings, poll, linger, grace,
        retry_delays=(), retry_window=0.0, cell="cell"):
    received = []
    current = [None]
    started = time.monotonic()
    # Recorded before the first attempt can move the shared offset.
    prompt = Prompt()

    # The step cap and a run cancel signal THIS process; pass them on, so a cell
    # that never submits still dies at the cap exactly as it did unwrapped.
    # Installed BEFORE the spawn, so no signal can kill the wrapper in between
    # and orphan the agent; one landing before a child is set is forwarded by
    # stop_on_signal instead, and one landing during a retry backoff ends the
    # wrapper before the next attempt starts.
    def forward(signum, _frame):
        received.append(signum)
        if current[0] is not None:
            signal_group(current[0].pid, signum)

    for signum in FORWARDED_SIGNALS:
        signal.signal(signum, forward)

    attempts = len(retry_delays) + 1
    for attempt in range(1, attempts + 1):
        code, stderr_tail = attempt_once(
            command, findings, poll, linger, grace, received, current)
        marker = retriable_marker(stderr_tail)
        if attempt == attempts or received or code <= 0 or marker is None:
            return code
        # Everything printed below follows the agent's own stderr, whose last
        # write may lack a newline; the `::warning::` must start a line.
        print(fresh_line(stderr_tail), end="", file=sys.stderr, flush=True)
        if not retry_safe(findings):
            log(f"agent exited {code} with [{marker}], but findings were already "
                "recorded or the file is unreadable; not retrying")
            return code
        delay = retry_delays[attempt - 1]
        if time.monotonic() - started + delay > retry_window:
            log(f"agent exited {code} with [{marker}], but a retry after {delay:g}s "
                f"would start past the {retry_window:g}s retry window; not retrying")
            return code
        # Nothing reads stdin between here and the next attempt: the agent's
        # whole group is dead, and this wrapper never reads it.
        if not prompt.rewind():
            log(f"agent exited {code} with [{marker}], but its stdin cannot be "
                f"rewound to replay the prompt ({prompt.problem}); not retrying")
            return code
        print(f"::warning::Reviewer cell {safe_label(cell)}: attempt {attempt}/"
              f"{attempts} failed with Cursor's retriable error [{marker}] "
              f"(exit {code}); retrying in {delay:g}s",
              file=sys.stderr, flush=True)
        deadline = time.monotonic() + delay
        while not received and time.monotonic() < deadline:
            time.sleep(min(WAIT_SLICE, max(0.0, deadline - time.monotonic())))
        if received:
            log(f"received signal {received[0]} during the retry backoff; not retrying")
            return -received[0]
    raise AssertionError("unreachable")


def attempt_once(command, findings, poll, linger, grace, received, current):
    """Run the agent once: (its status, the last TAIL_BYTES of its stderr)."""
    # Own process group, so a stop reaches cursor-agent's children too (the
    # MCP server, any shell it spawned) — not just the top-level process.
    # stdin and stdout are inherited as they are. stderr is piped through here
    # only so a retriable error can be recognised; every byte is copied on to
    # this wrapper's own stderr.
    child = subprocess.Popen(command, start_new_session=True,
                             stderr=subprocess.PIPE)
    current[0] = child
    tee = Tee(child.stderr, sys.stderr.fileno())
    try:
        code = supervise(child, findings, poll, linger, grace, received)
    finally:
        # Every path out — the agent exiting by itself, a stop, an exception
        # here: nothing left in the agent's group may outlive the wrapper and
        # rewrite the findings after the upload and leg check read them.
        signal_group(child.pid, signal.SIGKILL)
        current[0] = None
    stderr_tail = tee.drain()
    child.stderr.close()
    return code, stderr_tail


def stop_on_signal(child, signum, grace):
    """End the agent after a signal to the wrapper; report 128+signum."""
    pgid = child.pid
    log(f"received signal {signum}; stopping the agent")
    signal_group(pgid, signum)
    if wait(child, min(grace, SIGNAL_GRACE)) is None:
        log("agent did not exit after the forwarded signal; sending SIGKILL")
    signal_group(pgid, signal.SIGKILL)
    child.wait()
    return -signum


def supervise(child, findings, poll, linger, grace, received):
    pgid = child.pid

    def interrupted():
        return bool(received)

    while True:
        if received:
            return stop_on_signal(child, received[0], grace)
        code = wait(child, poll, interrupted)
        if received:
            continue
        if code is not None:
            return code
        if not submitted(findings):
            continue
        log(f"findings report status=ok; giving the agent {linger:g}s to exit on its own")
        code = wait(child, linger, interrupted)
        if received:
            continue
        if code is not None:
            return code
        # Re-read before signalling: a status that has stopped being ok since
        # (the file is agent-writable) is not a submission to stop on.
        if submitted(findings):
            break
        log("findings no longer report status=ok; continuing to wait")

    started = time.monotonic()
    log("agent still running after submitting; sending SIGTERM")
    signal_group(pgid, signal.SIGTERM)
    if wait(child, grace) is None:
        log(f"agent did not exit within {grace:g}s of SIGTERM; sending SIGKILL")
    # SIGKILL the group even when the top-level process exited: a grandchild
    # that ignored SIGTERM must not outlive the step and rewrite the findings.
    signal_group(pgid, signal.SIGKILL)
    child.wait()
    log(f"agent stopped {time.monotonic() - started:.1f}s after SIGTERM; findings left as written")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--findings", required=True)
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--linger", type=float, default=15.0)
    parser.add_argument("--grace", type=float, default=10.0)
    parser.add_argument("--retry-delays", default="",
                        help="comma-separated backoff seconds, one retry each "
                             "(default: none, never retry)")
    parser.add_argument("--retry-window", type=float, default=300.0,
                        help="no retry may start later than this many seconds "
                             "after the wrapper started")
    parser.add_argument("--cell", default="cell",
                        help="<review_type>/<model>, named in the retry warning")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given after --")
    for name in ("poll", "linger", "grace"):
        value = getattr(args, name)
        if not (math.isfinite(value) and value > 0):
            parser.error(f"--{name} must be a positive finite number")
    try:
        delays = tuple(float(d) for d in args.retry_delays.split(",") if d.strip())
    except ValueError:
        parser.error("--retry-delays must be comma-separated numbers")
    if not all(math.isfinite(d) and d >= 0 for d in delays):
        parser.error("--retry-delays must be non-negative finite numbers")
    if not (math.isfinite(args.retry_window) and args.retry_window >= 0):
        parser.error("--retry-window must be a non-negative finite number")
    code = run(command, args.findings, args.poll, args.linger, args.grace,
               delays, args.retry_window, args.cell)
    # A signal death reads as the shell would report it (143 for SIGTERM), not
    # as Python's negative returncode wrapped mod 256.
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    sys.exit(main())
