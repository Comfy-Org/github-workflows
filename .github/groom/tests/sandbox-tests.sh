#!/usr/bin/env bash
#
# sandbox-tests.sh — deterministic proofs of the agent-sandbox.sh confinement
# contract and the broker.mjs credential proxy (BE-4302, phase 1).
#
# Runs the wrapper's preflight and then asserts every confinement property with
# `bash -c` as the sandboxed command — NO claude, NO API key, NO spend. Requires a
# Linux host with unprivileged user namespaces (a GitHub `ubuntu-latest` runner);
# the wrapper's preflight installs bubblewrap + the AppArmor profile as needed.
#
# Each assertion's inside-command is written to exit 0 on success, so a green
# `bwrap` exit means the property held; the driver fails loud on any non-zero.
#
# shellcheck disable=SC2016  # inside-command snippets intentionally keep $VAR literal for the jail

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SANDBOX="$ROOT/.github/groom/agent-sandbox.sh"
BROKER="$ROOT/.github/groom/broker.mjs"
FAKE_UPSTREAM="$ROOT/.github/groom/tests/fake-upstream.mjs"

# Runnable outside GitHub Actions too — synthesize the runner env vars if absent.
: "${RUNNER_TEMP:=$(mktemp -d)}"
: "${GITHUB_WORKSPACE:=$ROOT}"

work="$(mktemp -d "${RUNNER_TEMP%/}/sandbox-tests.XXXXXX")"
clone="$work/clone"
outdir="$work/out"
rofile="$work/allowed-ro.txt"

# Host canaries that must be INVISIBLE from inside the jail.
home_canary="$HOME/agent-sandbox-canary-home.$$"
temp_canary="${RUNNER_TEMP%/}/agent-sandbox-canary-temp.$$"
ws_canary="${GITHUB_WORKSPACE%/}/agent-sandbox-canary-ws.$$"

fake_pid=""
broker_pid=""
host_sleep_pid=""
host_http_pid=""

cleanup() {
	if [[ -n "$fake_pid" ]]; then kill "$fake_pid" 2>/dev/null || true; fi
	if [[ -n "$broker_pid" ]]; then kill "$broker_pid" 2>/dev/null || true; fi
	if [[ -n "$host_sleep_pid" ]]; then kill "$host_sleep_pid" 2>/dev/null || true; fi
	if [[ -n "$host_http_pid" ]]; then kill "$host_http_pid" 2>/dev/null || true; fi
	rm -f "$home_canary" "$temp_canary" "$ws_canary" /tmp/canary
	rm -rf "$work"
}
trap cleanup EXIT

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

pass() {
	echo "PASS: $*"
}

# --- fixtures ----------------------------------------------------------------

mkdir -p "$clone"
(
	cd "$clone"
	git init -q
	git config user.email t@t.local
	git config user.name tester
	echo tracked > tracked.txt
	git add -A
	git commit -qm init
)
echo "read-only-content" > "$rofile"
echo secret > "$home_canary"
echo secret > "$temp_canary"
echo secret > "$ws_canary"
echo secret > /tmp/canary

# --- 1. environment scrub ----------------------------------------------------
# Only FOO/HOME/PATH (+ TERM) inside; a host-exported canary is NOT injected.

export HOSTSECRET=leaked-host-value
if ! "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" \
	--env FOO=bar -- bash -c '
	# NB: read via `cat` not `< /proc/self/environ` — the `<` redirect resolves
	# /proc/self in the pre-exec forked shell and reads empty inside the jail.
	dump=$(cat /proc/self/environ | tr "\0" "\n")
	echo "$dump" | grep -qx "FOO=bar"                              || { echo "FOO missing"; exit 1; }
	echo "$dump" | grep -qx "HOME=/home/agent"                     || { echo "HOME wrong"; exit 1; }
	echo "$dump" | grep -qx "PATH=/usr/local/bin:/usr/bin:/bin"    || { echo "PATH wrong"; exit 1; }
	if echo "$dump" | grep -q "HOSTSECRET"; then echo "HOSTSECRET leaked into jail"; exit 1; fi
	exit 0
'; then fail "environment scrub"; fi
unset HOSTSECRET
pass "environment scrub (FOO/HOME/PATH present, HOSTSECRET absent)"

# --- 2. filesystem confinement + tmpfs shadowing -----------------------------
# Host $HOME / $RUNNER_TEMP / $GITHUB_WORKSPACE canaries unreadable; host
# /tmp/canary shadowed by tmpfs; writes fail except to --out-dir.

if ! "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" \
	--ro-file "$rofile" \
	--env HOME_CANARY="$home_canary" --env TEMP_CANARY="$temp_canary" \
	--env WS_CANARY="$ws_canary" --env OUTDIR="$outdir" \
	--env CLONE="$clone" --env ROFILE="$rofile" -- bash -c '
	for f in "$HOME_CANARY" "$TEMP_CANARY" "$WS_CANARY" /tmp/canary; do
		if cat "$f" >/dev/null 2>&1; then echo "leaked readable: $f"; exit 1; fi
	done
	grep -q "read-only-content" "$ROFILE" || { echo "explicit --ro-file not readable"; exit 1; }
	if echo x > /usr/should-fail 2>/dev/null;         then echo "wrote read-only /usr"; exit 1; fi
	if echo x > "$CLONE/should-fail" 2>/dev/null;     then echo "wrote read-only clone"; exit 1; fi
	if echo x > "$ROFILE" 2>/dev/null;                then echo "wrote read-only --ro-file"; exit 1; fi
	echo captured > "$OUTDIR/proof.txt" || { echo "out-dir not writable"; exit 1; }
	exit 0
'; then fail "filesystem confinement"; fi
test -f "$outdir/proof.txt" || fail "out-dir write not visible on host"
pass "filesystem confinement (host canaries hidden, /tmp shadowed, writes gated to out-dir)"

# --- 3. clone rw-git-ro: worktree write lands on host; .git stays read-only ---

if ! "$SANDBOX" --clone "$clone" --clone-mode rw-git-ro --out-dir "$outdir" \
	--env CLONE="$clone" -- bash -c '
	echo "builder patch" > "$CLONE/patch-from-agent.txt" || { echo "worktree write failed"; exit 1; }
	if echo x >> "$CLONE/.git/config" 2>/dev/null; then echo "wrote read-only .git/config"; exit 1; fi
	exit 0
'; then fail "clone rw-git-ro"; fi
grep -q "builder patch" "$clone/patch-from-agent.txt" 2>/dev/null \
	|| fail "rw-git-ro worktree write not visible on host"
pass "clone rw-git-ro (worktree write captured on host, .git read-only)"

# --- 3b. out-dir/clone overlap rejection -------------------------------------
# The out-dir is bound rw LAST, so bwrap's last-wins ordering would let it shadow
# the read-only clone/.git mounts if it overlapped them. The wrapper must reject
# an out-dir equal to, inside, or an ancestor of the clone — failing loud before
# it ever builds the jail. (This validation runs before preflight, so it holds
# even on a host without bwrap.)

if "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$clone" -- true 2>/dev/null; then
	fail "out-dir == clone was accepted (rw bind would un-protect the read-only clone)"
fi
if "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$clone/nested/out" -- true 2>/dev/null; then
	fail "out-dir inside clone was accepted (rw bind would shadow the read-only clone)"
fi
if "$SANDBOX" --clone "$clone/.git" --clone-mode ro --out-dir "$clone" -- true 2>/dev/null; then
	fail "out-dir as an ancestor of clone was accepted (rw bind would shadow the read-only clone)"
fi
pass "out-dir/clone overlap rejected (equal, nested, and ancestor cases)"

# --- 4. pid isolation --------------------------------------------------------

sleep 300 &
host_sleep_pid=$!
if ! "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" \
	--env HOSTPID="$host_sleep_pid" -- bash -c '
	if ls /proc | grep -qx "$HOSTPID"; then echo "host pid $HOSTPID visible in jail"; exit 1; fi
	exit 0
'; then fail "pid isolation"; fi
kill "$host_sleep_pid" 2>/dev/null || true
host_sleep_pid=""
pass "pid isolation (host pids invisible in jail /proc)"

# --- 5. broker credential proxy (over the bind-mounted unix socket) ----------
# Fake local HTTPS upstream stands in for api.anthropic.com; the broker runs with
# the real (fake) key and a test-only TLS bypass for the self-signed upstream, and
# now listens on a UNIX SOCKET (no TCP port) bind-mounted into the isolated-netns
# jail via --uds. Inside the jail the in-jail forwarder (jail-shim.mjs) on
# 127.0.0.1:8790 bridges curl to that socket — exactly the composition the
# production groom caller will use — and ALL the existing assertions run against it.

SHIM="$ROOT/.github/groom/jail-shim.mjs"
certdir="$work/certs"
mkdir -p "$certdir"
openssl req -x509 -newkey rsa:2048 -nodes \
	-keyout "$certdir/key.pem" -out "$certdir/cert.pem" \
	-days 1 -subj "/CN=localhost" >/dev/null 2>&1 || fail "could not generate test cert"

real_key="sk-ant-TESTFAKE-broker-forwarding-proof"
up_port=8791
shim_port=8790

node "$FAKE_UPSTREAM" "$up_port" "$certdir/key.pem" "$certdir/cert.pem" &
fake_pid=$!
ANTHROPIC_API_KEY="$real_key" \
	BROKER_UPSTREAM_HOST=127.0.0.1 \
	BROKER_UPSTREAM_PORT="$up_port" \
	NODE_TLS_REJECT_UNAUTHORIZED=0 \
	node "$BROKER" "$work/broker.sock" &
broker_pid=$!

ready=""
for _ in $(seq 1 50); do
	if curl -fsS --unix-socket "$work/broker.sock" http://broker/healthz >/dev/null 2>&1; then ready=1; break; fi
	sleep 0.2
done
[[ -n "$ready" ]] || fail "broker did not come up on $work/broker.sock"

if ! "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" \
	--uds "$work/broker.sock" --ro-file "$SHIM" \
	--env SHIM="$SHIM" --env SHIMPORT="$shim_port" --env REALKEY="$real_key" -- bash -c '
	# Bring up the in-jail TCP->UDS forwarder and wait for it before asserting.
	node "$SHIM" "$SHIMPORT" /run/broker.sock &
	base="http://127.0.0.1:$SHIMPORT"
	shim_ready=""
	for _ in $(seq 1 50); do
		if curl -fsS "$base/healthz" >/dev/null 2>&1; then shim_ready=1; break; fi
		sleep 0.2
	done
	[ -n "$shim_ready" ] || { echo "jail-shim did not come up on $base"; exit 1; }
	# real key injected, caller-supplied dummy stripped
	body=$(curl -s "$base/v1/messages" -H "x-api-key: dummy")
	echo "$body" | grep -q "$REALKEY" || { echo "real key not forwarded upstream: $body"; exit 1; }
	if echo "$body" | grep -q "dummy"; then echo "caller dummy key leaked upstream"; exit 1; fi
	# healthz served locally with 200
	code=$(curl -s -o /dev/null -w "%{http_code}" "$base/healthz")
	[ "$code" = "200" ] || { echo "healthz not 200: $code"; exit 1; }
	# non-/v1 path denied
	code=$(curl -s -o /dev/null -w "%{http_code}" "$base/not-v1")
	[ "$code" = "404" ] || { echo "non-/v1 not 404: $code"; exit 1; }
	# dot-segment path denied (raw target, sent un-normalized via --path-as-is):
	# a bare prefix check would pass "/v1/.." yet it normalizes off /v1 upstream.
	code=$(curl -s --path-as-is -o /dev/null -w "%{http_code}" "$base/v1/../not-v1")
	[ "$code" = "404" ] || { echo "dot-segment /v1/../ not 404: $code"; exit 1; }
	# chunked/SSE response streams through intact
	stream=$(curl -sN "$base/v1/stream")
	echo "$stream" | grep -q "data: one" || { echo "sse frame one missing"; exit 1; }
	echo "$stream" | grep -q "data: two" || { echo "sse frame two missing"; exit 1; }
	echo "$stream" | grep -q "\[DONE\]"  || { echo "sse [DONE] missing"; exit 1; }
	exit 0
'; then fail "broker credential proxy"; fi
pass "broker credential proxy over UDS (key injected+stripped, healthz local, non-/v1 404, SSE streams)"

# --- 6. broker crash-resilience: client disconnect mid-stream ----------------
# .pipe() puts no error listener on the client response, so a client that drops
# mid-SSE would emit an unhandled 'error' on res and kill the whole broker. Force
# that: /v1/stream sends one frame, waits 50ms, then the rest — abort inside the
# gap, then prove the broker is still serving. Host-side over the same UDS (the
# crash-proofing is transport-independent).
curl -sN --max-time 0.02 --unix-socket "$work/broker.sock" http://broker/v1/stream >/dev/null 2>&1 || true
sleep 0.2
curl -fsS --unix-socket "$work/broker.sock" http://broker/healthz >/dev/null 2>&1 \
	|| fail "broker crashed after a client disconnected mid-stream"
pass "broker crash-resilience (survives a client mid-stream disconnect)"

# --- 7. egress isolation proofs (the BE-4369 acceptance tests) ---------------
# --unshare-all with no shared-network flag gives an empty netns with only lo up, so
# the broker's bind-mounted socket (proven in section 5) is the ONLY reachable service.
# Everything else off-host is unreachable BY CONSTRUCTION — prove it deterministically
# (connects fail immediately: ECONNREFUSED on the jail's own lo, ENETUNREACH off-netns;
# --max-time 3 is only a backstop, no real network or long timeouts are needed).

# The 7a/7b/7c checks read a `curl` FAILURE as "unreachable" — so a curl missing
# from the jail PATH would make every branch false and green-light the tests
# without exercising the network control at all. Assert the tool is actually
# present in the jail first, so a missing dependency can't silently pass them.
"$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" -- bash -c 'command -v curl >/dev/null' \
	|| fail "curl not available in jail — egress-isolation checks (7a/7b/7c) would false-pass"

# 7a. Host loopback NOT reachable: a listener on the HOST's 127.0.0.1 sits on a
# different loopback than the jail's, so a jail connect must fail. Run this jail
# WITHOUT a shim on that port so nothing in-jail shadows it.
node -e 'require("http").createServer((q,s)=>s.end("host")).listen(8799, "127.0.0.1")' &
host_http_pid=$!
ready=""
for _ in $(seq 1 50); do
	if curl -fsS "http://127.0.0.1:8799/" >/dev/null 2>&1; then ready=1; break; fi
	sleep 0.2
done
[[ -n "$ready" ]] || fail "host-loopback listener did not come up on 127.0.0.1:8799"
if ! "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" -- bash -c '
	if curl -s --max-time 3 http://127.0.0.1:8799/ >/dev/null 2>&1; then
		echo "host loopback 127.0.0.1:8799 reachable from jail — netns not isolated"; exit 1; fi
	exit 0
'; then fail "host loopback reachable from jail"; fi
kill "$host_http_pid" 2>/dev/null || true
host_http_pid=""
pass "host loopback unreachable from jail (jail lo != host lo)"

# 7b. Cloud metadata endpoint NOT reachable (immediate ENETUNREACH in empty netns).
if ! "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" -- bash -c '
	if curl -s --max-time 3 http://169.254.169.254/metadata/instance >/dev/null 2>&1; then
		echo "cloud metadata 169.254.169.254 reachable from jail"; exit 1; fi
	exit 0
'; then fail "cloud metadata endpoint reachable from jail"; fi
pass "cloud metadata endpoint unreachable from jail"

# 7c. Arbitrary external IP NOT reachable (IP literal, so no DNS dependence).
if ! "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" -- bash -c '
	if curl -s --max-time 3 http://1.1.1.1/ >/dev/null 2>&1; then
		echo "external IP 1.1.1.1 reachable from jail"; exit 1; fi
	exit 0
'; then fail "arbitrary external IP reachable from jail"; fi
pass "arbitrary external IP unreachable from jail"

# 7d. Name resolution is dead inside the jail, and no nameserver is routable.
#
# 7a/7b/7c all use IP literals ON PURPOSE, so not one of them ever touches the
# resolver — routing is proven while name resolution is left untested. This section
# covers it, in TWO parts, because the obvious one-liner is a trap.
#
# (i) Resolution fails. Asserted on resolver-SPECIFIC exit codes rather than a bare
#     non-zero, which is what stops the same false-pass the 7a/7b/7c tool-presence
#     check exists to block: `getent` 2 is "key not found" specifically (a missing
#     binary is 127) and `curl` 6 is CURLE_COULDNT_RESOLVE_HOST specifically (a
#     connect-stage failure is 7, a timeout 28, a missing binary 127). The name is a
#     real, permanently-resolvable one, so neither branch can pass merely because the
#     hostname was bogus — and the host-side control below says so out loud.
#
# (ii) THE NETNS PROOF — do NOT collapse this into (i). Part (i) on its own is
#     VACUOUS as evidence of network isolation: it passes under a SHARED netns too
#     (measured, not theorized). On a systemd-resolved host /etc/resolv.conf is a
#     symlink into /run, and the jail mounts /etc but deliberately NOT /run (mounting
#     it would be a confinement regression in its own right), so the resolver has no
#     nameserver configured whatever the netns looks like — meaning a future change
#     that put the jail back on a shared network would NOT turn part (i) red. What
#     only an isolated netns can produce is an immediate no-route to a hardcoded
#     nameserver address, so assert that as well, again on the exact code: `curl` 7
#     is CURLE_COULDNT_CONNECT, which a shared netns cannot return here — it reaches
#     the host stack and comes back 52/56 when 1.1.1.1:53 answers, or 28 when egress
#     to it is filtered, never 7.
if getent hosts api.anthropic.com >/dev/null 2>&1; then
	echo "note: this host resolves api.anthropic.com — the in-jail failure below is the sandbox, not a dead name"
else
	echo "note: this host cannot resolve api.anthropic.com either (offline?) — the in-jail assertions are still enforced"
fi
if ! "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" -- bash -c '
	# (i) name resolution fails
	rc=0; getent hosts api.anthropic.com >/dev/null 2>&1 || rc=$?
	if [ "$rc" = 0 ]; then echo "api.anthropic.com RESOLVED in jail — name resolution is not closed off (an /etc/hosts entry on the host would do this, served through the read-only /etc bind)"; exit 1; fi
	[ "$rc" = 2 ] || { echo "getent exit $rc, want 2 (key not found) — getent missing from the jail PATH?"; exit 1; }
	# --max-time is generous so a resolver that retried before giving up still reports
	# 6 (could-not-resolve) rather than being clipped into an ambiguous 28 (timeout).
	rc=0; curl -s --max-time 10 http://api.anthropic.com/ >/dev/null 2>&1 || rc=$?
	if [ "$rc" = 0 ]; then echo "curl reached api.anthropic.com from jail — egress is NOT closed"; exit 1; fi
	[ "$rc" = 6 ] || { echo "curl exit $rc, want 6 (CURLE_COULDNT_RESOLVE_HOST) — failed past the resolver stage instead of at it"; exit 1; }
	# (ii) and no nameserver is routable either — the half a shared netns would fail
	rc=0; curl -s --max-time 10 http://1.1.1.1:53/ >/dev/null 2>&1 || rc=$?
	if [ "$rc" = 0 ]; then echo "nameserver 1.1.1.1:53 answered from jail — netns is not isolated"; exit 1; fi
	[ "$rc" = 7 ] || { echo "curl exit $rc to 1.1.1.1:53, want 7 (CURLE_COULDNT_CONNECT/no route) — a reachable netns returns 52/56/28 here, so this jail still has one"; exit 1; }
	exit 0
'; then fail "jail name resolution / nameserver routing is not closed off"; fi
pass "name resolution dead in jail (getent 2, curl 6) and no nameserver routable (curl 7 to 1.1.1.1:53)"

# 7e. --uds fail-loud: a nonexistent socket path must exit non-zero BEFORE the cmd.
if "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" \
	--uds /nonexistent.sock -- true 2>/dev/null; then
	fail "--uds /nonexistent.sock was accepted (must fail loud before running the command)"
fi
pass "--uds fail-loud on a nonexistent socket path"

echo "ALL SANDBOX TESTS PASSED"
