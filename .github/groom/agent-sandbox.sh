#!/usr/bin/env bash
#
# agent-sandbox.sh — run an arbitrary command inside a bubblewrap (bwrap) jail.
#
# This is the confinement harness for the groom auto-builder's agent step
# (BE-4302 phase 1; isolated-netns hardening BE-4421 phase 2). It gives an
# untrusted agent a shell in an ISOLATED network namespace — an empty netns with
# only loopback up (no host network, no host loopback services, no cloud metadata
# at 169.254.169.254 / 168.63.129.16) — that can ONLY see: a read-only /usr + /etc,
# ephemeral /tmp + $HOME, the target clone (read-only, or read-write worktree with
# a read-only .git), an explicit set of read-only files, and one writable out-dir.
# Everything else on the host — other repos, the runner's secrets, $HOME,
# $RUNNER_TEMP, $GITHUB_WORKSPACE, the host process table — is invisible. The real
# API key never enters the jail; the agent reaches Anthropic only through the
# broker (broker.mjs), bind-mounted into the jail as a unix-domain socket at
# /run/broker.sock (--uds). There is NO network egress — nothing else off-host is
# reachable, so in-jail `git fetch` / `npm install` cannot work.
#
# Usage:
#   agent-sandbox.sh --clone <path> --clone-mode ro|rw-git-ro --out-dir <path> \
#       [--ro-file <path> ...] [--env KEY=VALUE ...] [--uds <host-socket-path>] \
#       -- <command...>
#
#   agent-sandbox.sh --preflight-only
#
#   agent-sandbox.sh --validate-only --clone <path> --clone-mode ro|rw-git-ro \
#       --out-dir <path> [--ro-file <path> ...] [--env KEY=VALUE ...] \
#       [--uds <host-socket-path>]
#
#   --uds bind-mounts a host-side listening unix socket (the broker) to the fixed
#   in-jail path /run/broker.sock (read-only: connect(2) to a socket works under a
#   read-only bind, but the jail can't chmod/replace the shared inode).
#   Omit it for a fully offline jail.
#
#   --preflight-only runs ONLY preflight() — the (mutating) sandbox bring-up
#   (install bubblewrap, the AppArmor profile, the sysctl fallback) — then exits:
#   0 if a working bwrap sandbox is now usable, non-zero if it cannot be
#   established. It takes NO --clone/--out-dir/-- <command>. It exists so the groom
#   jobs can do the bring-up in a step SEPARATE from `Run <agent>` (BE-14756): a
#   bring-up failure then fails that preflight step and NEVER reaches the billed
#   agent step, so interval.py does not miscount a no-spend setup failure as a
#   spent audit. preflight() is idempotent (fast path returns instantly when the
#   sandbox is already usable), so the real `Run <agent>` step's own preflight is
#   then a no-op.
#
#   --validate-only is the second half of that split (BE-14771). It takes the SAME
#   arguments a real run does and walks the SAME code path — argument validation,
#   the absolute-path checks, the --uds `-S` + live-broker healthz probe, the
#   clone/out-dir existence + overlap check, preflight(), and the whole bwrap_args
#   assembly with its embedded `--env KEY=VALUE`, `rw-git-ro` `.git`-pointer and
#   `--ro-file` absolute-path guards — then stops at the single exec point instead
#   of exec'ing bwrap, printing `validate-only: all pre-exec guards passed` and
#   exiting 0. Every one of those guards is no-spend and fail-loud, but on a real
#   run they die INSIDE the billed `Run <agent>` step, which interval.py then reads
#   as a started (spent) audit and advances the cadence clock for a run that billed
#   nothing (BE-4814). Hoisting them into the same separate step as the bring-up
#   moves that failure off the billed step's name. It takes NO `-- <command>`:
#   nothing is ever executed, and rejecting one keeps a stray `--validate-only` on
#   a real agent step from becoming a green no-op that runs no agent. Walking the
#   REAL path (rather than a re-implementation of the checks) is the point — a
#   parallel copy would drift, and a guard it missed would still kill `Run <agent>`
#   no-spend.
#
# The preflight FAILS LOUD: if a working bwrap sandbox cannot be established on
# this runner image, the script exits non-zero and the command is NEVER run. It
# never falls back to running the command unsandboxed.

set -euo pipefail

die() {
	echo "agent-sandbox: $*" >&2
	exit 2
}

# The base mounts used by BOTH the preflight self-test and the real invocation,
# minus the caller-supplied clone/ro-file/out-dir/env. `true` runs as the probe.
selftest() {
	bwrap \
		--unshare-all \
		--ro-bind /usr /usr \
		--symlink usr/bin /bin \
		--symlink usr/lib /lib \
		--symlink usr/lib64 /lib64 \
		--symlink usr/sbin /sbin \
		--proc /proc \
		--dev /dev \
		--tmpfs /tmp \
		true 2>/dev/null
}

# Establish a working unprivileged-userns bwrap sandbox or exit non-zero. Mirrors
# the runner image's own podman AppArmor workaround
# (actions/runner-images: images/ubuntu/scripts/build/install-container-tools.sh):
# Ubuntu 23.10+ ships kernel.apparmor_restrict_unprivileged_userns=1, which blocks
# the unprivileged user namespaces bwrap needs unless an unconfined AppArmor
# profile is installed for /usr/bin/bwrap.
preflight() {
	# Everything here goes to STDERR, never stdout: the caller captures this
	# script's stdout as the agent's exec JSON (see the exec comment below), and
	# `apt-get`/`apparmor_parser`/`sysctl`/`::error::` chatter on stdout would be
	# prepended to that JSON, breaking the downstream `jq -e .` guard so the
	# diagnostics artifact is silently never written. Workflow `::` commands are
	# honoured on stderr too, so the fail-loud annotation still surfaces.
	# Fast path: already usable, do nothing (keeps repeated invocations quiet).
	if command -v bwrap >/dev/null 2>&1 && selftest; then
		return 0
	fi

	if ! command -v bwrap >/dev/null 2>&1; then
		sudo apt-get install -y bubblewrap >&2
	fi

	local restrict=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
	if [[ -r "$restrict" && "$(cat "$restrict")" == "1" ]]; then
		sudo tee /etc/apparmor.d/bwrap >/dev/null <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
PROFILE
		sudo apparmor_parser -r -W /etc/apparmor.d/bwrap >&2 || true
	fi

	if selftest; then
		return 0
	fi

	# Last resort: drop the unprivileged-userns restriction outright and retest.
	sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 >&2 || true
	if selftest; then
		return 0
	fi

	echo "::error::bwrap sandbox unavailable on this runner image — refusing to run the agent unsandboxed" >&2
	exit 1
}

main() {
	local clone="" clone_mode="" out_dir="" uds="" preflight_only="" validate_only=""
	local ro_files=() envs=() cmd=()

	while [[ $# -gt 0 ]]; do
		case "$1" in
			--clone) [[ $# -ge 2 ]] || die "--clone needs a value"; clone="$2"; shift 2 ;;
			--clone-mode) [[ $# -ge 2 ]] || die "--clone-mode needs a value"; clone_mode="$2"; shift 2 ;;
			--out-dir) [[ $# -ge 2 ]] || die "--out-dir needs a value"; out_dir="$2"; shift 2 ;;
			--ro-file) [[ $# -ge 2 ]] || die "--ro-file needs a value"; ro_files+=("$2"); shift 2 ;;
			--env) [[ $# -ge 2 ]] || die "--env needs a value"; envs+=("$2"); shift 2 ;;
			--uds) [[ $# -ge 2 ]] || die "--uds needs a value"; [[ -n "$2" ]] || die "--uds needs a non-empty value"; [[ -z "$uds" ]] || die "--uds may be given at most once"; uds="$2"; shift 2 ;;
			--preflight-only) preflight_only=1; shift ;;
			--validate-only) validate_only=1; shift ;;
			--) shift; cmd=("$@"); break ;;
			*) die "unknown argument: $1" ;;
		esac
	done

	# The two pre-agent-step modes are mutually exclusive: --preflight-only takes NO
	# execution-mode arguments and --validate-only requires the full set, so the
	# combination cannot mean anything. Reject it instead of letting the
	# --preflight-only branch below win and silently skip the validation the caller
	# asked for — a green no-op where a caller expected a check is exactly the
	# failure mode both of these modes exist to prevent.
	[[ -z "$preflight_only" || -z "$validate_only" ]] \
		|| die "--preflight-only and --validate-only are mutually exclusive"

	# --preflight-only: run ONLY the (mutating) sandbox bring-up and report whether
	# a working jail is now available (BE-14756). It takes NO clone/clone-mode/
	# out-dir/uds/ro-file/env and NO `-- <command>`; combining it with any of those
	# is a copy-paste mistake — a stray `--preflight-only` on a real agent step
	# would otherwise silently discard the clone/out-dir/command and exit 0 having
	# run no agent, the opposite of this mode's contract. Every other bad flag
	# combination here dies loudly, so die here too instead of short-circuiting
	# past every validation. preflight() fails loud itself when the sandbox cannot
	# be established; `|| exit $?` keeps that structural even if preflight() is ever
	# refactored to RETURN non-zero rather than terminate the process.
	if [[ -n "$preflight_only" ]]; then
		[[ -z "$clone" && -z "$clone_mode" && -z "$out_dir" && -z "$uds" \
			&& ${#ro_files[@]} -eq 0 && ${#envs[@]} -eq 0 && ${#cmd[@]} -eq 0 ]] \
			|| die "--preflight-only takes no --clone/--clone-mode/--out-dir/--uds/--ro-file/--env and no -- <command>"
		preflight || exit $?
		exit 0
	fi

	[[ -n "$clone" ]] || die "--clone is required"
	[[ -n "$out_dir" ]] || die "--out-dir is required"
	if [[ -n "$validate_only" ]]; then
		# Nothing is ever executed under --validate-only, so a `-- <command>` here
		# is meaningless. Rejecting it (rather than accepting and ignoring it) is
		# what keeps a stray --validate-only on a real agent step LOUD: it dies
		# instead of exiting 0 having silently discarded the agent invocation.
		# Same misuse-guard posture as --preflight-only above.
		[[ ${#cmd[@]} -eq 0 ]] \
			|| die "--validate-only takes no -- <command...>: nothing is executed, so drop the command"
	else
		[[ ${#cmd[@]} -gt 0 ]] || die "a -- <command...> is required"
	fi
	# bwrap binds each of these at its REAL path; a relative value would resolve
	# against an unexpected CWD instead of failing loud, so require absolute paths.
	[[ "$clone" = /* ]] || die "--clone must be an absolute path (got '$clone')"
	[[ "$out_dir" = /* ]] || die "--out-dir must be an absolute path (got '$out_dir')"
	case "$clone_mode" in
		ro | rw-git-ro) ;;
		*) die "--clone-mode must be 'ro' or 'rw-git-ro' (got '${clone_mode:-}')" ;;
	esac
	[[ -d "$clone" ]] || die "clone path is not a directory: $clone"
	# The broker socket is bind-mounted at its real path, so require absolute; and
	# require it to already be a listening unix socket — a rw --bind of a missing or
	# non-socket path would just give the jail a useless mountpoint. Fail loud.
	if [[ -n "$uds" ]]; then
		[[ "$uds" = /* ]] || die "--uds must be an absolute path (got '$uds')"
		[[ -S "$uds" ]] || die "--uds path is not a listening unix socket (start the broker first): $uds"
		# -S only proves the inode is a socket, not that a broker is actually
		# listening — a stale socket from a crashed broker would pass -S yet the
		# in-jail connect() then fails at runtime, breaking the fail-loud-before-
		# running guarantee. Probe /healthz over the socket to confirm a live
		# listener (best-effort: only when curl is present, matching the tests).
		if command -v curl >/dev/null 2>&1; then
			curl -fsS --max-time 5 --unix-socket "$uds" http://broker/healthz >/dev/null 2>&1 \
				|| die "--uds socket has no live broker listening (healthz probe failed): $uds"
		fi
	fi

	# out-dir must exist on the host before it can be bound rw into the jail; create
	# it here so we can canonicalize it for the overlap check below.
	mkdir -p "$out_dir"

	# The writable out-dir is bound LAST, and bwrap's last-bind-wins ordering means
	# an out-dir that overlaps the clone would shadow the read-only clone/.git
	# mounts and silently make protected content (including .git under rw-git-ro)
	# writable — defeating the read-only contract. Canonicalize both and require
	# them to be disjoint (neither equal nor an ancestor of the other). Fail here,
	# before the (slow) preflight, so a bad invocation is rejected fast.
	local clone_real out_real
	clone_real="$(realpath "$clone")" || die "cannot resolve --clone path: $clone"
	out_real="$(realpath "$out_dir")" || die "cannot resolve --out-dir path: $out_dir"
	if [[ "$out_real" == "$clone_real" || "$out_real" == "$clone_real"/* || "$clone_real" == "$out_real"/* ]]; then
		die "--out-dir must not overlap --clone (out-dir '$out_real' vs clone '$clone_real'): a writable bind over the clone would defeat its read-only mounts"
	fi

	preflight

	local bwrap_args=(
		--unshare-all --die-with-parent --new-session --clearenv
		--ro-bind /usr /usr
		--symlink usr/bin /bin
		--symlink usr/lib /lib
		--symlink usr/lib64 /lib64
		--symlink usr/sbin /sbin
		--ro-bind /etc /etc
		--proc /proc
		--dev /dev
		--tmpfs /tmp
		--tmpfs /home/agent
		--setenv HOME /home/agent
		--setenv PATH /usr/local/bin:/usr/bin:/bin
		--setenv TERM dumb
	)

	local e key val
	if [[ ${#envs[@]} -gt 0 ]]; then
		for e in "${envs[@]}"; do
			[[ "$e" == *=* ]] || die "--env expects KEY=VALUE (got '$e')"
			key="${e%%=*}"
			val="${e#*=}"
			bwrap_args+=(--setenv "$key" "$val")
		done
	fi

	# The clone is bound AT ITS REAL PATH so tool output paths match the host.
	case "$clone_mode" in
		ro)
			bwrap_args+=(--ro-bind "$clone" "$clone")
			;;
		rw-git-ro)
			# Read-write worktree, but .git stays read-only: the agent may edit
			# tracked files (the patch we capture) yet can never rewrite history
			# or git config. Ordering matters — the rw clone bind first, then the
			# ro .git overlay on top.
			#
			# This assumes .git is a real directory. In a git-worktree checkout it
			# is instead a file holding a `gitdir:` pointer to metadata elsewhere on
			# the host — which would NOT be mounted into the jail, silently breaking
			# git rather than protecting it. Fail loud instead (callers pass plain
			# clones; worktree checkouts are unsupported here).
			[[ -d "$clone/.git" ]] || die "--clone-mode rw-git-ro needs a plain .git directory (got a gitdir pointer file — git worktree checkouts are unsupported): $clone/.git"
			bwrap_args+=(--bind "$clone" "$clone" --ro-bind "$clone/.git" "$clone/.git")
			;;
	esac

	local f
	if [[ ${#ro_files[@]} -gt 0 ]]; then
		for f in "${ro_files[@]}"; do
			[[ "$f" = /* ]] || die "--ro-file must be an absolute path (got '$f')"
			bwrap_args+=(--ro-bind "$f" "$f")
		done
	fi

	# Bind the broker's unix socket into the isolated netns at a fixed path, READ-
	# ONLY. connect(2) to a socket still works under a read-only bind — the kernel's
	# read-only-fs EROFS check (sb_permission) fires only for regular files, dirs and
	# symlinks, never for a socket inode — so the jail can still reach the broker,
	# while --ro-bind additionally strips the agent's ability to chmod the shared
	# socket inode (e.g. 000 to DoS itself, 0777 to widen host-side access). As a
	# mountpoint the socket also can't be unlinked or replaced from inside the jail.
	# bwrap auto-creates the dest. (Section 5 of sandbox-tests.sh exercises this exact
	# in-jail connect over --ro-bind, so a regression here fails CI loudly.)
	if [[ -n "$uds" ]]; then
		bwrap_args+=(--ro-bind "$uds" /run/broker.sock)
	fi

	bwrap_args+=(--bind "$out_dir" "$out_dir" --chdir "$clone")

	# THE single exec point, and therefore the single place --validate-only can
	# branch (BE-14771) and still be sure every pre-exec guard above ran — including
	# the ones embedded in the bwrap_args assembly just above (`--env KEY=VALUE`,
	# the rw-git-ro `.git`-pointer check, `--ro-file` absolute paths), which a
	# validation re-implemented elsewhere would silently skip.
	if [[ -n "$validate_only" ]]; then
		echo "validate-only: all pre-exec guards passed"
		exit 0
	fi

	# stdout/stderr pass through to the host shell; the caller redirects stdout
	# on the HOST side to capture any exec JSON out of the agent's reach.
	exec bwrap "${bwrap_args[@]}" -- "${cmd[@]}"
}

main "$@"
