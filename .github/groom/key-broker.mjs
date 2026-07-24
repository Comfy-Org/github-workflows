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

const DEFAULT_PORT = 8199;
const DEFAULT_UPSTREAM = "https://api.anthropic.com";

function die(message) {
  console.error(`groom-key-broker: ${message}`);
  process.exit(1);
}

// --- config (env only — NOT the key) -------------------------------------
const rawPort = process.env.GROOM_BROKER_PORT || String(DEFAULT_PORT);
const PORT = Number.parseInt(rawPort, 10);
if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) {
  die(`invalid GROOM_BROKER_PORT: ${JSON.stringify(rawPort)}`);
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
const client = upstream.protocol === "http:" ? http : https;

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
// readline strips the trailing newline; also drop a lone CR from CRLF input.
const realKey = firstLine == null ? "" : firstLine.replace(/\r$/, "");
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

  const upstreamReq = client.request(
    {
      protocol: upstream.protocol,
      hostname: upstream.hostname,
      port: upstream.port || undefined,
      method,
      path: req.url, // forward path+query verbatim (already validated /v1/*)
      headers,
    },
    (upstreamRes) => {
      // Copy upstream status + response headers back verbatim; stream the body
      // (SSE streaming works because we pipe rather than buffer).
      res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
      upstreamRes.pipe(res);
      logReq(method, path, upstreamRes.statusCode || 502);
    },
  );

  upstreamReq.on("error", () => {
    if (!res.headersSent) {
      res.writeHead(502, { "content-type": "text/plain" });
      res.end("upstream unavailable");
    } else {
      res.end();
    }
    logReq(method, path, 502);
  });

  // Stream the request body up to the upstream (so large / streaming bodies work).
  req.pipe(upstreamReq);
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
  // Exactly one readiness line (the wait-loop keys off the port being
  // connectable; this line is for debugging).
  console.log(`groom-key-broker listening on 127.0.0.1:${PORT}`);
});
