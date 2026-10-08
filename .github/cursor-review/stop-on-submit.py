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
    submits still runs to the step cap and is reported exactly as before.

Exit status: the agent's own exit code when it exits by itself (before or
after submitting); 0 when this wrapper stopped it after a verified submission.
Status lines go to stderr with a `stop-on-submit:` prefix.

Usage:
  stop-on-submit.py --findings /tmp/findings-out/findings.json -- cursor-agent ...
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def log(message):
    print(f"stop-on-submit: {message}", file=sys.stderr, flush=True)


def submitted(path):
    """True only for a complete, parseable JSON object with status == "ok".

    The file is agent-writable (the agent runs `--trust` with a shell), so
    every failure mode reads as "not submitted" rather than raising:
    `ValueError` covers both JSONDecodeError and the UnicodeDecodeError that
    invalid UTF-8 raises; RecursionError covers pathologically nested input.
    """
    try:
        with open(path, encoding="utf-8") as source:
            record = json.load(source)
    except (OSError, ValueError, RecursionError):
        return False
    return isinstance(record, dict) and record.get("status") == "ok"


def signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def wait(child, timeout):
    """The child's exit code, or None if it is still running after `timeout`."""
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def run(command, findings, poll, linger, grace):
    # Own process group, so a stop reaches cursor-agent's children too (the
    # MCP server, any shell it spawned) — not just the top-level process.
    child = subprocess.Popen(command, start_new_session=True)
    pgid = child.pid

    # The step cap and a run cancel signal THIS process; pass them on, so a cell
    # that never submits still dies at the cap exactly as it did unwrapped.
    def forward(signum, _frame):
        signal_group(pgid, signum)

    for signum in FORWARDED_SIGNALS:
        signal.signal(signum, forward)

    while True:
        code = wait(child, poll)
        if code is not None:
            return code
        if not submitted(findings):
            continue
        log(f"findings report status=ok; giving the agent {linger:g}s to exit on its own")
        code = wait(child, linger)
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
        if getattr(args, name) <= 0:
            parser.error(f"--{name} must be positive")
    code = run(command, args.findings, args.poll, args.linger, args.grace)
    # A signal death reads as the shell would report it (143 for SIGTERM), not
    # as Python's negative returncode wrapped mod 256.
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    sys.exit(main())
