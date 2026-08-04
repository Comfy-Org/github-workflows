// jail-shim.mjs — an in-jail TCP→UDS forwarder for the groom agent sandbox
// (BE-4421, phase 2).
//
//   node jail-shim.mjs <port> <socket-path>
//
// The sandbox (agent-sandbox.sh) runs the agent in an ISOLATED network namespace
// with only loopback up and no egress; the key-broker (broker.mjs) is reachable
// only as a unix-domain socket bind-mounted into the jail at /run/broker.sock.
// Agent tooling that speaks HTTP to a host:port (e.g. an Anthropic base URL) can't
// dial a unix socket, so this shim listens on jail-local loopback and forwards
// every connection to the socket. It runs INSIDE the jail as part of the
// sandboxed command (background it, then exec the agent); an agent that kills it
// only breaks its own API access.

import net from 'node:net';

const port = Number(process.argv[2]);
const sockPath = process.argv[3];
if (!Number.isInteger(port) || port <= 0 || port > 65535 || !sockPath) {
  console.error('jail-shim: usage: node jail-shim.mjs <port> <socket-path>');
  process.exit(1);
}

const server = net.createServer((c) => {
  const u = net.connect(sockPath);
  c.pipe(u);
  u.pipe(c);
  c.on('error', () => u.destroy());
  u.on('error', () => c.destroy());
});

// The shim is backgrounded inside the jail while the caller captures the command's
// stdout as the agent's JSON output; a listen failure surfacing as an uncaught
// 'error' would kill the shim with a raw stack trace, so handle it and exit clean.
server.on('error', (e) => {
  console.error(`jail-shim: listen failed on 127.0.0.1:${port}: ${e.message}`);
  process.exit(1);
});

server.listen(port, '127.0.0.1', () => {
  // stderr, NOT stdout: the shim runs backgrounded alongside the agent whose stdout
  // the caller captures as JSON — a banner on stdout would corrupt that capture.
  console.error(`shim listening on 127.0.0.1:${port} -> ${sockPath}`);
});
