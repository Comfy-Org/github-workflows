// fake-upstream.mjs — a tiny local HTTPS server standing in for api.anthropic.com
// in the broker tests (BE-4302). It never talks to the real API. The broker points
// at it via BROKER_UPSTREAM_HOST/PORT; the broker process runs with
// NODE_TLS_REJECT_UNAUTHORIZED=0 so it accepts this server's self-signed cert
// (test-only — production talks real TLS to api.anthropic.com).
//
//   node fake-upstream.mjs <port> <key.pem> <cert.pem>
//
// It echoes back the x-api-key header it RECEIVED so the test can prove the broker
// injected the real key and stripped the caller's dummy one. On /v1/stream it emits
// a chunked SSE response to prove streaming survives the proxy.

import https from 'node:https';
import fs from 'node:fs';

const port = Number(process.argv[2]);
const key = fs.readFileSync(process.argv[3]);
const cert = fs.readFileSync(process.argv[4]);

const server = https.createServer({ key, cert }, (req, res) => {
  const received = req.headers['x-api-key'] || '';

  if (req.url === '/v1/stream') {
    // Chunked SSE: multiple writes with a gap so a buffering proxy would collapse
    // them; a streaming proxy delivers all three data frames intact.
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'x-echo-received-key': received,
    });
    res.write('data: one\n\n');
    setTimeout(() => {
      res.write('data: two\n\n');
      res.write('data: [DONE]\n\n');
      res.end();
    }, 50);
    return;
  }

  res.writeHead(200, {
    'content-type': 'text/plain',
    'x-echo-received-key': received,
  });
  res.end(`received_x_api_key=${received}\n`);
});

server.listen(port, '127.0.0.1', () => {
  console.log(`fake-upstream listening on 127.0.0.1:${port}`);
});
