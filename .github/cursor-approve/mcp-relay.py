#!/usr/bin/env python3
"""Byte relay between cursor-agent's stdio MCP client and a pre-started context-proxy.py.

    mcp-relay.py <proxy-stdin-fifo> <proxy-stdout-fifo>

Why a relay: cursor-agent starts a stdio MCP server itself, AFTER the agent is
running, so a proxy it launched would still find its token file on disk while
the agent can act. cursor-axis-base.yml instead starts context-proxy.py first,
on two FIFOs, waits until it has read and deleted the token file, and only then
starts the agent, whose mcp.json names this relay as the server's command.

This file holds no token and parses nothing: it copies stdin to the first FIFO
and the second FIFO to stdout, and exits when stdin closes. Stdlib only.
"""

import os
import sys
import threading

CHUNK = 65536


def pump(src: int, dst: int) -> None:
    while True:
        data = os.read(src, CHUNK)
        if not data:
            return
        view = memoryview(data)
        while view:
            view = view[os.write(dst, view):]


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: mcp-relay.py <proxy-stdin-fifo> <proxy-stdout-fifo>", file=sys.stderr)
        return 2
    to_proxy = os.open(args[0], os.O_WRONLY)
    from_proxy = os.open(args[1], os.O_RDONLY)
    threading.Thread(target=pump, args=(from_proxy, sys.stdout.fileno()), daemon=True).start()
    try:
        pump(sys.stdin.fileno(), to_proxy)
    except (BrokenPipeError, OSError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
