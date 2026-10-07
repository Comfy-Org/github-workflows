#!/usr/bin/env python3
"""Byte relay between cursor-agent's stdio MCP client and a pre-started context-proxy.py.

    mcp-relay.py <proxy-stdin-fifo> <proxy-stdout-fifo>

Why a relay: cursor-agent starts a stdio MCP server itself, AFTER the agent is
running, so a proxy it launched would still find its token file on disk while
the agent can act. cursor-axis-base.yml instead starts context-proxy.py first,
on two FIFOs, waits until it has read and deleted the token file, and only then
starts the agent, whose mcp.json names this relay as the server's command.

This file holds no token and parses nothing: it copies stdin to the first FIFO
and the second FIFO to stdout. It exits as soon as either direction closes or
fails, so a dead proxy surfaces as a dead MCP server, never a hang. Stdlib only.
"""

import os
import stat
import sys
import threading
import time

CHUNK = 65536
OPEN_TIMEOUT = 10.0


def pump(src: int, dst: int) -> None:
    while True:
        data = os.read(src, CHUNK)
        if not data:
            return
        view = memoryview(data)
        while view:
            view = view[os.write(dst, view):]


def open_fifo(path: str, flags: int) -> int:
    """Open a FIFO without blocking on a missing peer: a write end with no reader
    (the proxy died) fails after OPEN_TIMEOUT instead of hanging."""
    if not stat.S_ISFIFO(os.stat(path).st_mode):
        raise OSError(f"{path} is not a FIFO")
    deadline = time.monotonic() + OPEN_TIMEOUT
    while True:
        try:
            fd = os.open(path, flags | os.O_NONBLOCK)
            break
        except OSError as exc:
            if exc.errno != getattr(os, "ENXIO", 6) or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
    os.set_blocking(fd, True)
    return fd


def relay_out(src: int, dst: int) -> None:
    # The proxy's output ended or failed: nothing will answer the agent again.
    try:
        pump(src, dst)
    except OSError:
        pass
    os._exit(1)


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: mcp-relay.py <proxy-stdin-fifo> <proxy-stdout-fifo>", file=sys.stderr)
        return 2
    try:
        to_proxy = open_fifo(args[0], os.O_WRONLY)
        from_proxy = open_fifo(args[1], os.O_RDONLY)
    except OSError as exc:
        print(f"mcp-relay: cannot reach the context proxy: {exc}", file=sys.stderr)
        return 1
    threading.Thread(target=relay_out, args=(from_proxy, sys.stdout.fileno()), daemon=True).start()
    try:
        pump(sys.stdin.fileno(), to_proxy)
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
