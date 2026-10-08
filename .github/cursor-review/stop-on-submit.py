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

A SIGINT/SIGTERM/SIGHUP to this wrapper (the step cap, a run cancel) is passed
to the agent's group; the agent then gets at most 5 seconds before SIGKILL —
inside the runner's own SIGINT -> SIGTERM -> kill escalation — and the wrapper
exits 128+signal whatever the agent's own status was.

Exit status: the agent's own exit code when it exits by itself (before or
after submitting); 0 when this wrapper stopped it after a verified submission;
128+signal when a forwarded signal ended it.
Status lines go to stderr with a `stop-on-submit:` prefix.

Usage:
  stop-on-submit.py --findings /tmp/findings-out/findings.json -- cursor-agent ...
"""

import argparse
import json
import math
import os
import signal
import stat
import subprocess
import sys
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


def log(message):
    print(f"stop-on-submit: {message}", file=sys.stderr, flush=True)


def submitted(path):
    """True only for a complete, parseable JSON object with status == "ok".

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
        return False
    try:
        with open(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return False
            data = source.read(MAX_FINDINGS_BYTES + 1)
        if len(data) > MAX_FINDINGS_BYTES:
            return False
        record = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError, MemoryError):
        return False
    return isinstance(record, dict) and record.get("status") == "ok"


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


def run(command, findings, poll, linger, grace):
    received = []
    child = None

    # The step cap and a run cancel signal THIS process; pass them on, so a cell
    # that never submits still dies at the cap exactly as it did unwrapped.
    # Installed BEFORE the spawn, so no signal can kill the wrapper in between
    # and orphan the agent; one landing before `child` is set is forwarded by
    # stop_on_signal instead.
    def forward(signum, _frame):
        received.append(signum)
        if child is not None:
            signal_group(child.pid, signum)

    for signum in FORWARDED_SIGNALS:
        signal.signal(signum, forward)

    # Own process group, so a stop reaches cursor-agent's children too (the
    # MCP server, any shell it spawned) — not just the top-level process.
    child = subprocess.Popen(command, start_new_session=True)
    try:
        return supervise(child, findings, poll, linger, grace, received)
    finally:
        # Every path out — the agent exiting by itself, a stop, an exception
        # here: nothing left in the agent's group may outlive the wrapper and
        # rewrite the findings after the upload and leg check read them.
        signal_group(child.pid, signal.SIGKILL)


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
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given after --")
    for name in ("poll", "linger", "grace"):
        value = getattr(args, name)
        if not (math.isfinite(value) and value > 0):
            parser.error(f"--{name} must be a positive finite number")
    code = run(command, args.findings, args.poll, args.linger, args.grace)
    # A signal death reads as the shell would report it (143 for SIGTERM), not
    # as Python's negative returncode wrapped mod 256.
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    sys.exit(main())
