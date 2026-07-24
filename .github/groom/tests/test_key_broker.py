#!/usr/bin/env python3
"""Tests for the groom key-broker (BE-4419).

`key-broker.mjs` is a localhost HTTP proxy that HOLDS the real API key and
injects it into forwarded requests, so the groom agent steps (BE-4311) can run
the Claude CLI with only a dummy key + a base-URL override pointed at the broker.
The properties under test are the ones the design leans on:

  * inbound credentials (a dummy `x-api-key`, an `authorization: Bearer`) are
    stripped and the REAL key is injected before forwarding to the upstream;
  * request bodies reach the upstream byte-for-byte (streaming, so SSE works);
  * the connectivity probe (`HEAD /`) and non-`/v1/` paths are answered locally
    and never forwarded;
  * the real key is NOT visible in the broker process's env or argv
    (`/proc/<pid>/{environ,cmdline}` are agent-readable on the shared runner);
  * an upstream connection error yields a local 502 and the broker stays alive.

These are integration tests: they boot the real `key-broker.mjs` under `node`
against a local fake upstream. The class is skipped when `node` is absent.

Run: python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
"""

import http.client
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

# A distinctive sentinel so the /proc assertions can't false-negative against a
# real key that happens to live in this machine's environment.
REAL_KEY = "sk-broker-test-REALKEY-9f2c1a7b4e"

NODE = shutil.which("node")
SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "key-broker.mjs"))


def _free_port():
    """Bind :0, read the assigned port, release it — good enough for a test."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_port(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


class _RecordingHandler(BaseHTTPRequestHandler):
    """Fake upstream: records each request and returns a canned JSON response."""

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.records.append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        payload = self.server.resp_body
        self.send_response(self.server.resp_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle

    def log_message(self, *args):  # silence the default stderr access log
        pass


class _Upstream(ThreadingHTTPServer):
    def server_bind(self):
        # Skip HTTPServer.server_bind's socket.getfqdn() reverse-DNS lookup, which
        # can hang for tens of seconds on hosts with slow PTR resolution (seen on
        # macOS). TCPServer.server_bind just binds; we set the name/port ourselves.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def _start_upstream():
    server = _Upstream(("127.0.0.1", 0), _RecordingHandler)
    server.records = []
    server.resp_status = 200
    server.resp_body = b'{"upstream":"pong","n":42}'
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _start_broker(upstream_url, port, stdin_payload=None):
    env = {**os.environ, "GROOM_BROKER_UPSTREAM": upstream_url, "GROOM_BROKER_PORT": str(port)}
    proc = subprocess.Popen(
        [NODE, SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    # The real key arrives ONLY on stdin's first line — never env, never argv.
    # stdin_payload lets a test feed a raw (e.g. padded/CRLF) line; default is
    # the clean key followed by the newline the broker's readline consumes.
    payload = (REAL_KEY + "\n") if stdin_payload is None else stdin_payload
    proc.stdin.write(payload.encode())
    proc.stdin.flush()
    return proc


def _stop_broker(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if proc.stdin:
        proc.stdin.close()


@unittest.skipUnless(NODE, "node is required to run the key-broker")
class KeyBrokerTest(unittest.TestCase):
    def setUp(self):
        self.upstream = _start_upstream()
        # Cleanups run LIFO: shutdown() the serve loop first, then close the socket.
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        upstream_url = "http://127.0.0.1:%d" % self.upstream.server_address[1]

        self.port = _free_port()
        self.broker = _start_broker(upstream_url, self.port)
        self.addCleanup(_stop_broker, self.broker)
        if not _wait_port(self.port):
            raise self.failureException("broker did not become connectable")

    def _conn(self, port=None):
        return http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=5)

    @property
    def records(self):
        return self.upstream.records

    # --- header injection + status/body round-trip -------------------------

    def test_injects_real_key_and_strips_inbound_credentials(self):
        self.upstream.resp_status = 202  # prove a non-200 status round-trips
        conn = self._conn()
        conn.request(
            "POST",
            "/v1/messages?beta=true",
            body=b'{"model":"x"}',
            headers={
                "x-api-key": "dummy",
                "authorization": "Bearer nope",
                "content-type": "application/json",
            },
        )
        resp = conn.getresponse()
        got_body = resp.read()
        conn.close()

        self.assertEqual(resp.status, 202)
        self.assertEqual(got_body, self.upstream.resp_body)

        self.assertEqual(len(self.records), 1)
        rec = self.records[0]
        self.assertEqual(rec["method"], "POST")
        self.assertEqual(rec["path"], "/v1/messages?beta=true")
        # The upstream saw the REAL key, not the dummy the CLI sent...
        self.assertEqual(rec["headers"].get("x-api-key"), REAL_KEY)
        self.assertNotEqual(rec["headers"].get("x-api-key"), "dummy")
        # ...and the inbound authorization header was stripped entirely.
        self.assertNotIn("authorization", rec["headers"])

    def test_stdin_key_is_trimmed(self):
        # A padded/copy-pasted secret (leading+trailing spaces + CRLF) must reach
        # the upstream TRIMMED: the raw value carries illegal header chars that
        # would otherwise throw on client.request. Guards key-broker.mjs `.trim()`.
        padded = "  " + REAL_KEY + " \r\n"
        port = _free_port()
        upstream_url = "http://127.0.0.1:%d" % self.upstream.server_address[1]
        proc = _start_broker(upstream_url, port, stdin_payload=padded)
        self.addCleanup(_stop_broker, proc)
        if not _wait_port(port):
            raise self.failureException("padded-key broker did not become connectable")

        conn = self._conn(port)
        conn.request("POST", "/v1/messages", body=b"{}", headers={"content-type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()

        # The trimmed key — not the padded input — reaches x-api-key upstream.
        self.assertEqual(self.records[-1]["headers"].get("x-api-key"), REAL_KEY)

    def test_request_body_reaches_upstream_intact(self):
        body = b'{"prompt":"unicode \xe2\x9c\x93 check","pad":"' + b"x" * 2048 + b'"}'
        conn = self._conn()
        conn.request(
            "POST",
            "/v1/messages",
            body=body,
            headers={"x-api-key": "dummy", "content-type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()

        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0]["body"], body)

    # --- local-only responses (never forwarded) ---------------------------

    def test_head_root_is_answered_locally(self):
        conn = self._conn()
        conn.request("HEAD", "/")
        resp = conn.getresponse()
        resp.read()
        conn.close()

        self.assertEqual(resp.status, 200)
        self.assertEqual(self.records, [])  # the probe was NOT forwarded

    def test_non_v1_path_is_404_and_not_forwarded(self):
        conn = self._conn()
        conn.request("GET", "/anything-else")
        resp = conn.getresponse()
        resp.read()
        conn.close()

        self.assertEqual(resp.status, 404)
        self.assertEqual(self.records, [])

    # --- the key must not leak via the process table ----------------------

    @unittest.skipUnless(sys.platform.startswith("linux"), "/proc is Linux-only")
    def test_key_absent_from_process_env_and_argv(self):
        pid = self.broker.pid
        with open("/proc/%d/environ" % pid, "rb") as f:
            environ = f.read()
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmdline = f.read()
        self.assertNotIn(REAL_KEY.encode(), environ)
        self.assertNotIn(REAL_KEY.encode(), cmdline)

    # --- upstream failure is a local 502 and non-fatal --------------------

    def test_upstream_down_returns_502_and_broker_survives(self):
        dead_port = _free_port()  # nothing is listening here
        broker_port = _free_port()
        proc = _start_broker("http://127.0.0.1:%d" % dead_port, broker_port)
        self.addCleanup(_stop_broker, proc)
        if not _wait_port(broker_port):
            raise self.failureException("broker did not become connectable")

        for _ in range(2):  # a second request proves the broker stayed alive
            conn = self._conn(broker_port)
            conn.request("POST", "/v1/messages", body=b"{}",
                         headers={"content-type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 502)

        self.assertIsNone(proc.poll(), "broker should stay alive after an upstream error")

    # --- a client abort mid-request must not crash the broker -------------

    def test_client_abort_does_not_kill_broker(self):
        # Send a Content-Length that promises more body than we deliver, then
        # slam the socket shut — the classic "unhandled 'error' on req.pipe"
        # crash. The broker must survive and still serve the next request.
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        raw.sendall(
            b"POST /v1/messages HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 4096\r\n\r\n"
            b"{}"  # far short of the promised 4096 bytes
        )
        raw.close()  # abort mid-body
        time.sleep(0.2)

        self.assertIsNone(self.broker.poll(), "broker must survive a client abort")
        conn = self._conn()  # a fresh request proves it's still serving
        conn.request("HEAD", "/")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)

    # --- a non-root upstream keeps its path prefix ------------------------

    def test_upstream_path_prefix_is_preserved(self):
        prefixed = "http://127.0.0.1:%d/prefix" % self.upstream.server_address[1]
        port = _free_port()
        proc = _start_broker(prefixed, port)
        self.addCleanup(_stop_broker, proc)
        if not _wait_port(port):
            raise self.failureException("broker did not become connectable")

        conn = self._conn(port)
        conn.request("POST", "/v1/messages", body=b"{}",
                     headers={"content-type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()

        self.assertEqual(resp.status, 200)
        self.assertEqual(self.records[-1]["path"], "/prefix/v1/messages")


class BrokerStartupTest(unittest.TestCase):
    """Startup guards that don't need a running upstream."""

    @unittest.skipUnless(NODE, "node is required to run the key-broker")
    def test_exits_nonzero_when_stdin_has_no_key(self):
        # No key on stdin (immediate EOF) -> refuse to start, non-zero exit.
        proc = subprocess.Popen(
            [NODE, SCRIPT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GROOM_BROKER_PORT": str(_free_port())},
        )
        self.assertNotEqual(proc.wait(timeout=10), 0)

    def _startup_exit_code(self, env_overrides):
        env = {**os.environ, "GROOM_BROKER_PORT": str(_free_port())}
        env.update(env_overrides)
        proc = subprocess.Popen(
            [NODE, SCRIPT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        return proc.wait(timeout=10)

    @unittest.skipUnless(NODE, "node is required to run the key-broker")
    def test_rejects_port_with_trailing_garbage(self):
        # "8199x" must be rejected, not silently coerced to 8199.
        self.assertNotEqual(self._startup_exit_code({"GROOM_BROKER_PORT": "8199x"}), 0)

    @unittest.skipUnless(NODE, "node is required to run the key-broker")
    def test_rejects_plaintext_non_loopback_upstream(self):
        # http:// to a non-loopback host would leak the key in cleartext.
        code = self._startup_exit_code({"GROOM_BROKER_UPSTREAM": "http://example.com"})
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
