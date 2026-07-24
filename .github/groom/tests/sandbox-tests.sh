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

cleanup() {
	[[ -n "$fake_pid" ]] && kill "$fake_pid" 2>/dev/null || true
	[[ -n "$broker_pid" ]] && kill "$broker_pid" 2>/dev/null || true
	[[ -n "$host_sleep_pid" ]] && kill "$host_sleep_pid" 2>/dev/null || true
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
	dump=$(tr "\0" "\n" < /proc/self/environ)
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

# --- 5. broker credential proxy ----------------------------------------------
# Fake local HTTPS upstream stands in for api.anthropic.com; the broker runs with
# the real (fake) key and a test-only TLS bypass for the self-signed upstream.

certdir="$work/certs"
mkdir -p "$certdir"
openssl req -x509 -newkey rsa:2048 -nodes \
	-keyout "$certdir/key.pem" -out "$certdir/cert.pem" \
	-days 1 -subj "/CN=localhost" >/dev/null 2>&1 || fail "could not generate test cert"

real_key="sk-ant-TESTFAKE-broker-forwarding-proof"
up_port=8791
broker_port=8790

node "$FAKE_UPSTREAM" "$up_port" "$certdir/key.pem" "$certdir/cert.pem" &
fake_pid=$!
ANTHROPIC_API_KEY="$real_key" \
	BROKER_UPSTREAM_HOST=127.0.0.1 \
	BROKER_UPSTREAM_PORT="$up_port" \
	NODE_TLS_REJECT_UNAUTHORIZED=0 \
	node "$BROKER" "$broker_port" &
broker_pid=$!

ready=""
for _ in $(seq 1 50); do
	if curl -fsS "http://127.0.0.1:$broker_port/healthz" >/dev/null 2>&1; then ready=1; break; fi
	sleep 0.2
done
[[ -n "$ready" ]] || fail "broker did not come up on 127.0.0.1:$broker_port"

if ! "$SANDBOX" --clone "$clone" --clone-mode ro --out-dir "$outdir" \
	--env BROKERPORT="$broker_port" --env REALKEY="$real_key" -- bash -c '
	base="http://127.0.0.1:$BROKERPORT"
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
	# chunked/SSE response streams through intact
	stream=$(curl -sN "$base/v1/stream")
	echo "$stream" | grep -q "data: one" || { echo "sse frame one missing"; exit 1; }
	echo "$stream" | grep -q "data: two" || { echo "sse frame two missing"; exit 1; }
	echo "$stream" | grep -q "\[DONE\]"  || { echo "sse [DONE] missing"; exit 1; }
	exit 0
'; then fail "broker credential proxy"; fi
pass "broker credential proxy (key injected+stripped, healthz local, non-/v1 404, SSE streams)"

echo "ALL SANDBOX TESTS PASSED"
