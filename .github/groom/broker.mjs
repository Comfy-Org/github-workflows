// broker.mjs — a zero-dependency reverse proxy that holds the real Anthropic key
// so a sandboxed agent never sees it (BE-4302, phase 1).
//
//   node broker.mjs <port>
//
// The agent inside the bwrap jail (agent-sandbox.sh) talks to this broker on host
// loopback with NO key of its own; the broker strips any inbound credential,
// injects the real ANTHROPIC_API_KEY (read from its own env, never the jail's),
// and forwards to api.anthropic.com. The key lives only in the host process; the
// jail can spend against it but can never read it.
//
// Contract: listens on 127.0.0.1 only; forwards only /v1/* paths; deletes inbound
// x-api-key / authorization before adding the real one; streams responses through
// unbuffered so SSE works; logs method + path + status ONLY (never headers/body).
//
// BROKER_UPSTREAM_HOST / BROKER_UPSTREAM_PORT override the upstream target for
// tests only; production leaves them unset and pins api.anthropic.com:443.

import http from 'node:http';
import https from 'node:https';

const port = Number(process.argv[2]);
if (!Number.isInteger(port) || port <= 0 || port > 65535) {
  console.error('broker: usage: node broker.mjs <port>');
  process.exit(1);
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
  if (!req.url.startsWith('/v1/')) {
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
    },
  );

  upstream.on('error', () => {
    console.log(`${req.method} ${req.url} 502`);
    if (!res.headersSent) res.writeHead(502, { 'content-type': 'text/plain' });
    res.end('upstream error\n');
  });

  req.pipe(upstream); // forward the request body streaming too
});

server.listen(port, '127.0.0.1', () => {
  console.log(`broker listening on 127.0.0.1:${port} -> ${UPSTREAM_HOST}:${UPSTREAM_PORT}`);
});
