// broker.mjs — a zero-dependency reverse proxy that holds the real Anthropic key
// so a sandboxed agent never sees it (BE-4302 phase 1; UDS transport BE-4421 phase 2).
//
//   node broker.mjs <port|socket-path>
//
// The agent inside the bwrap jail (agent-sandbox.sh) talks to this broker with NO
// key of its own; the broker strips any inbound credential, injects the real
// ANTHROPIC_API_KEY (read from its own env, never the jail's), and forwards to
// api.anthropic.com. The key lives only in the host process; the jail can spend
// against it but can never read it.
//
// Transport: a numeric argv[2] keeps the legacy TCP mode (127.0.0.1:<port>,
// retained for the fake-upstream test plumbing and back-compat); any other value
// is treated as an absolute unix-domain-socket path (the phase-2 default — the
// socket is bind-mounted into the isolated-netns jail at /run/broker.sock, since
// the jail has no network egress). A relative path is refused.
//
// Contract: listens on 127.0.0.1 (TCP mode) or a unix socket only; forwards only
// /v1/* paths; deletes inbound x-api-key / authorization before adding the real
// one; streams responses through unbuffered so SSE works; logs method + path +
// status ONLY (never headers/body). The request-handling contract is
// transport-independent — TCP and UDS behave identically.
//
// BROKER_UPSTREAM_HOST / BROKER_UPSTREAM_PORT override the upstream target for
// tests only; production leaves them unset and pins api.anthropic.com:443.

import http from 'node:http';
import https from 'node:https';
import fs from 'node:fs';

const arg = process.argv[2];
let listenArgs; // spread into server.listen(): [port, host] for TCP or [path] for UDS
let addr; // human label for the listen log line
let uds; // the socket path in UDS mode (undefined in TCP mode); chmod'd after bind
if (arg !== undefined && /^\d+$/.test(arg)) {
  const port = Number(arg);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    console.error('broker: usage: node broker.mjs <port|socket-path>');
    process.exit(1);
  }
  listenArgs = [port, '127.0.0.1'];
  addr = `127.0.0.1:${port}`;
} else {
  const sockPath = arg;
  if (!sockPath || sockPath[0] !== '/') {
    console.error('broker: usage: node broker.mjs <port|socket-path> (socket path must be absolute)');
    process.exit(1);
  }
  // Clear a stale socket left by a crashed prior run so listen() doesn't EADDRINUSE.
  // Only ever unlink an actual SOCKET: an unconditional rmSync(force) would crash on
  // a directory (EISDIR is not suppressed by force), silently delete a regular file
  // at a mistyped path, and unlink a live socket. Stat first and fail loud on
  // anything that isn't a socket. lstat so a symlink is treated as a non-socket.
  try {
    const st = fs.lstatSync(sockPath);
    if (!st.isSocket()) {
      console.error(`broker: refusing to start — ${sockPath} exists and is not a socket`);
      process.exit(1);
    }
    fs.unlinkSync(sockPath);
  } catch (e) {
    if (e.code !== 'ENOENT') throw e; // absent path is the normal case
  }
  uds = sockPath;
  listenArgs = [sockPath];
  addr = sockPath;
}

const KEY = process.env.ANTHROPIC_API_KEY;
if (!KEY) {
  console.error('broker: ANTHROPIC_API_KEY is empty — refusing to start');
  process.exit(1);
}

const UPSTREAM_HOST = process.env.BROKER_UPSTREAM_HOST || 'api.anthropic.com';
const UPSTREAM_PORT = Number(process.env.BROKER_UPSTREAM_PORT || 443);

const server = http.createServer((req, res) => {
  // Local health check — never touches upstream, never spends.
  if (req.method === 'GET' && req.url === '/healthz') {
    console.log(`${req.method} ${req.url} 200`);
    res.writeHead(200, { 'content-type': 'text/plain' });
    res.end('ok\n');
    return;
  }

  // Only the Messages/Anthropic API surface is proxied; everything else is denied.
  // Reject dot-segments too: a raw target like `/v1/../v2/foo` passes a bare
  // prefix check yet normalizes to a non-/v1 route upstream, so fail it closed.
  const reqPath = req.url.split('?')[0];
  if (!req.url.startsWith('/v1/') || reqPath.includes('..')) {
    console.log(`${req.method} ${req.url} 404`);
    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found\n');
    return;
  }

  const headers = { ...req.headers };
  delete headers['x-api-key'];
  delete headers['authorization'];
  headers.host = UPSTREAM_HOST;
  headers['x-api-key'] = KEY;

  const upstream = https.request(
    { host: UPSTREAM_HOST, port: UPSTREAM_PORT, method: req.method, path: req.url, headers },
    (upRes) => {
      console.log(`${req.method} ${req.url} ${upRes.statusCode}`);
      res.writeHead(upRes.statusCode, upRes.headers);
      upRes.pipe(res); // stream through — no buffering, so SSE passes intact
      // A mid-stream upstream failure (after headers) can't become a 502 anymore;
      // handle its 'error' so it tears down the client response instead of
      // bubbling to an uncaughtException that takes the whole broker down.
      upRes.on('error', () => {
        console.log(`${req.method} ${req.url} upstream-stream-error`);
        res.destroy();
      });
    },
  );

  upstream.on('error', () => {
    console.log(`${req.method} ${req.url} 502`);
    if (!res.headersSent) {
      res.writeHead(502, { 'content-type': 'text/plain' });
      res.end('upstream error\n');
    } else {
      // Response already began streaming (routine for SSE): a 502 body can't be
      // sent anymore, and appending 'upstream error' here would splice stray
      // bytes into the middle of the real body. Tear it down instead so the
      // client sees a truncated stream rather than a corrupted one.
      res.destroy();
    }
  });

  // Don't let a hung upstream pin a request open forever; tear it down on idle.
  upstream.setTimeout(120_000, () => upstream.destroy(new Error('upstream timeout')));

  // If the client goes away (aborted/dropped), stop talking to upstream so we
  // don't leave a half-open request spending against the key.
  res.on('close', () => {
    if (!res.writableFinished) upstream.destroy();
  });
  // .pipe() forwards neither errors nor a listener onto the client response, so a
  // client socket error mid-stream (ECONNRESET, write-after-close) would emit an
  // unhandled 'error' on res and take the whole broker down. Absorb it and tear
  // down the upstream leg — same crash-proofing as the upRes/upstream handlers.
  res.on('error', () => upstream.destroy());
  req.on('error', () => upstream.destroy());

  req.pipe(upstream); // forward the request body streaming too
});

// A listen failure (EADDRINUSE, EACCES, a bad UDS dir) would otherwise surface as
// an uncaught 'error' event and a raw stack trace; log it and exit non-zero.
server.on('error', (e) => {
  console.error(`broker: listen failed on ${addr}: ${e.message}`);
  process.exit(1);
});

server.listen(...listenArgs, () => {
  // Lock the UDS to owner-only. server.listen() creates it under the process umask,
  // so a permissive umask could let another local user connect(2) to this
  // credential-injecting proxy and spend the real key (they still can't read it).
  if (uds !== undefined) fs.chmodSync(uds, 0o600);
  console.log(`broker listening on ${addr} -> ${UPSTREAM_HOST}:${UPSTREAM_PORT}`);
});
