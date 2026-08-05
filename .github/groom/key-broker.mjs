#!/usr/bin/env node
// groom key-broker (BE-4419) — a tiny localhost HTTP proxy that HOLDS the real
// API key and injects it into forwarded requests, so the groom agent steps
// (BE-4311) can run the Claude CLI with only a DUMMY key and the CLI's base-URL
// override pointed at this broker (see .github/groom/README.md for the env
// wiring the caller sets).
//
// Why a broker at all: the groom agent has an unscoped Read tool and
// `Bash(cat:*)`, and everything on the runner is the same user — so
// `/proc/<broker-pid>/environ` and `/proc/<broker-pid>/cmdline` are
// agent-readable. Therefore the real key MUST arrive on **stdin** (never env,
// never argv), this process must never log request/response headers or bodies
// (the log file is agent-readable too — we log at most `method path -> status`),
// and it listens on 127.0.0.1 only. This file deliberately never names or reads
// the provider key's env var (a grep for the provider name over this file finds
// nothing) — the only env it reads is GROOM_BROKER_PORT / GROOM_BROKER_UPSTREAM.

import http from "node:http";
import https from "node:https";
import readline from "node:readline";
import { pipeline } from "node:stream";

const DEFAULT_PORT = 8199;
const DEFAULT_UPSTREAM = "https://api.anthropic.com";
// Socket-inactivity timeout for a forwarded upstream request: a stalled or dead
// upstream is torn down instead of pinning the forwarded request (and the real
// key it carries) plus its downstream socket open forever, which would let
// repeated stalls exhaust the broker's sockets. This is an INACTIVITY timeout —
// it does not fire while data (e.g. an SSE token stream) keeps flowing — so it
// is set generously to never clip a legitimately slow/long streaming reply.
const UPSTREAM_TIMEOUT_MS = 600_000;

function die(message) {
  console.error(`groom-key-broker: ${message}`);
  process.exit(1);
}

// --- config (env only — NOT the key) -------------------------------------
const rawPort = process.env.GROOM_BROKER_PORT || String(DEFAULT_PORT);
// Reject the whole string, not just parseInt's leading digits — otherwise
// "8199x" (→ 8199) or "1e3" (→ 1) would be silently coerced to a valid port.
const PORT = /^\d+$/.test(rawPort) ? Number.parseInt(rawPort, 10) : NaN;
if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) {
  die(`invalid GROOM_BROKER_PORT: ${JSON.stringify(rawPort)}`);
}

function isLoopbackHost(host) {
  const h = host.replace(/^\[|\]$/g, ""); // strip IPv6 brackets, e.g. [::1]
  return h === "localhost" || h === "::1" || /^127\./.test(h);
}

let upstream;
try {
  upstream = new URL(process.env.GROOM_BROKER_UPSTREAM || DEFAULT_UPSTREAM);
} catch {
  die(`invalid GROOM_BROKER_UPSTREAM: ${JSON.stringify(process.env.GROOM_BROKER_UPSTREAM)}`);
}
if (upstream.protocol !== "http:" && upstream.protocol !== "https:") {
  die(`GROOM_BROKER_UPSTREAM must be http/https, got ${upstream.protocol}`);
}
// Cleartext http:// is allowed ONLY for loopback (the fake upstream used by the
// tests). Injecting the real key into a plaintext request to a remote host would
// transmit the secret in the clear — defeating the broker's whole purpose.
if (upstream.protocol === "http:" && !isLoopbackHost(upstream.hostname)) {
  die(`GROOM_BROKER_UPSTREAM http:// is allowed only for loopback hosts (got ${upstream.hostname}); use https:// for remote upstreams`);
}
const client = upstream.protocol === "http:" ? http : https;
// Prefix from the upstream URL (e.g. https://host/prefix → "/prefix"), stripped
// of a trailing slash; "" for a root upstream. Prepended to the forwarded path
// so a non-root upstream isn't silently dropped.
const upstreamPrefix = upstream.pathname.replace(/\/+$/, "");

// --- the real key: first line of stdin, nothing else ---------------------
async function readKeyFromStdin() {
  const rl = readline.createInterface({ input: process.stdin });
  for await (const line of rl) {
    rl.close();
    return line;
  }
  return null; // stdin closed without a single line
}

const firstLine = await readKeyFromStdin();
// readline strips the trailing newline; trim() drops a lone CR from CRLF input
// plus any leading/trailing whitespace common in copy-pasted secrets, which
// would otherwise be an illegal header character that throws on client.request.
const realKey = firstLine == null ? "" : firstLine.trim();
// We no longer need stdin — release the pipe so nothing lingers holding the key.
process.stdin.destroy();
if (!realKey) {
  die("no API key on stdin (first line was empty or stdin closed before a line); refusing to start");
}

// --- request logging: method + path + status ONLY, never headers/body ----
function logReq(method, path, status) {
  process.stderr.write(`${method} ${path} -> ${status}\n`);
}

// --- forward a /v1/* request to the upstream, injecting the real key -----
function forward(req, res, method, path) {
  const headers = { ...req.headers };
  // Strip any inbound credentials the CLI sent (the dummy x-api-key, and an
  // authorization: Bearer if an auth-token env was set) and the inbound host.
  delete headers["x-api-key"];
  delete headers["authorization"];
  delete headers["host"];
  headers["x-api-key"] = realKey;
  headers["host"] = upstream.host;

  // `settled` guards the 502 path: once the upstream's headers are on their way
  // back (or we've already emitted a 502), a later error must NOT try to write a
  // second response.
  let settled = false;
  const fail502 = () => {
    if (settled) return;
    settled = true;
    if (!res.headersSent) {
      res.writeHead(502, { "content-type": "text/plain" });
      res.end("upstream unavailable");
    } else {
      // Headers already flushed — destroy so the caller sees a broken
      // connection, not a silently truncated "success".
      res.destroy();
    }
    logReq(method, path, 502);
  };

  let upstreamReq;
  try {
    upstreamReq = client.request({
      protocol: upstream.protocol,
      hostname: upstream.hostname,
      port: upstream.port || undefined,
      method,
      // Prepend any upstream path prefix; req.url is already validated /v1/*.
      path: upstreamPrefix + req.url,
      headers,
      timeout: UPSTREAM_TIMEOUT_MS,
    });
  } catch {
    // client.request can throw synchronously (an illegal char in the request
    // target or an injected key). Fail this one request; never crash the broker.
    fail502();
    req.resume(); // drain the inbound body so its socket can close cleanly
    return;
  }

  upstreamReq.on("response", (upstreamRes) => {
    // Past the 502 window: copy upstream status + headers back verbatim, then
    // stream the body. pipeline tears down BOTH sides on any error/disconnect,
    // so an upstream reset mid-stream destroys `res` (the caller sees a broken
    // connection, not a truncated 200) and a client disconnect destroys the
    // upstream response.
    settled = true;
    res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
    logReq(method, path, upstreamRes.statusCode || 502);
    pipeline(upstreamRes, res, () => {});
  });

  // A stalled upstream trips the inactivity timeout; destroy() surfaces as an
  // 'error' handled below (local 502 if nothing sent yet, else a torn stream).
  upstreamReq.on("timeout", () => upstreamReq.destroy(new Error("upstream timeout")));
  // Connection-level errors (refused/reset/DNS) before any response → local 502.
  upstreamReq.on("error", fail502);

  // Client disconnect (or normal completion) → tear down the outbound
  // real-key-bearing request so it can't linger and leak its socket.
  res.on("close", () => {
    if (!upstreamReq.destroyed) upstreamReq.destroy();
  });

  // Stream the inbound body up to the upstream (so large / streaming bodies
  // work). pipeline aborts the upstream on a client abort rather than letting an
  // unhandled 'error' on `req` crash the broker.
  pipeline(req, upstreamReq, (err) => {
    if (err && !upstreamReq.destroyed) upstreamReq.destroy();
  });
}

const server = http.createServer((req, res) => {
  const method = req.method || "GET";
  const rawUrl = req.url || "/";
  const path = rawUrl.split("?", 1)[0];

  // The CLI's connectivity probe — answer locally, never forward.
  if ((method === "HEAD" || method === "GET") && path === "/") {
    res.writeHead(200);
    res.end();
    logReq(method, path, 200);
    return;
  }

  if (path.startsWith("/v1/")) {
    forward(req, res, method, path);
    return;
  }

  // Anything else is not part of the CLI's surface — refuse locally.
  res.writeHead(404);
  res.end();
  logReq(method, path, 404);
});

server.on("error", (err) => {
  die(`failed to listen on 127.0.0.1:${PORT}: ${err.message}`);
});

server.listen(PORT, "127.0.0.1", () => {
  // Exactly one readiness line. A consumer wait-loop should match THIS line
  // (proof the broker itself bound the port) rather than a bare port-connect
  // check, which a foreign process already holding the port would pass while the
  // broker exits with EADDRINUSE.
  console.log(`groom-key-broker listening on 127.0.0.1:${PORT}`);
});
