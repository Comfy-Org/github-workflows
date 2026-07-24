#!/usr/bin/env bash
#
# agent-sandbox.sh — run an arbitrary command inside a bubblewrap (bwrap) jail.
#
# This is the confinement harness for the groom auto-builder's agent step
# (BE-4302, phase 1). It gives an untrusted agent a network-connected shell that
# can ONLY see: a read-only /usr + /etc, ephemeral /tmp + $HOME, the target clone
# (read-only, or read-write worktree with a read-only .git), an explicit set of
# read-only files, and one writable out-dir. Everything else on the host — other
# repos, the runner's secrets, $HOME, $RUNNER_TEMP, $GITHUB_WORKSPACE, the host
# process table — is invisible. The real API key never enters the jail; the agent
# reaches Anthropic only through the broker (broker.mjs) on host loopback.
#
# Usage:
#   agent-sandbox.sh --clone <path> --clone-mode ro|rw-git-ro --out-dir <path> \
#       [--ro-file <path> ...] [--env KEY=VALUE ...] -- <command...>
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
		--unshare-all --share-net \
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
	# Fast path: already usable, do nothing (keeps repeated invocations quiet).
	if command -v bwrap >/dev/null 2>&1 && selftest; then
		return 0
	fi

	if ! command -v bwrap >/dev/null 2>&1; then
		sudo apt-get install -y bubblewrap
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
		sudo apparmor_parser -r -W /etc/apparmor.d/bwrap || true
	fi

	if selftest; then
		return 0
	fi

	# Last resort: drop the unprivileged-userns restriction outright and retest.
	sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 || true
	if selftest; then
		return 0
	fi

	echo "::error::bwrap sandbox unavailable on this runner image — refusing to run the agent unsandboxed"
	exit 1
}

main() {
	local clone="" clone_mode="" out_dir=""
	local ro_files=() envs=() cmd=()

	while [[ $# -gt 0 ]]; do
		case "$1" in
			--clone) [[ $# -ge 2 ]] || die "--clone needs a value"; clone="$2"; shift 2 ;;
			--clone-mode) [[ $# -ge 2 ]] || die "--clone-mode needs a value"; clone_mode="$2"; shift 2 ;;
			--out-dir) [[ $# -ge 2 ]] || die "--out-dir needs a value"; out_dir="$2"; shift 2 ;;
			--ro-file) [[ $# -ge 2 ]] || die "--ro-file needs a value"; ro_files+=("$2"); shift 2 ;;
			--env) [[ $# -ge 2 ]] || die "--env needs a value"; envs+=("$2"); shift 2 ;;
			--) shift; cmd=("$@"); break ;;
			*) die "unknown argument: $1" ;;
		esac
	done

	[[ -n "$clone" ]] || die "--clone is required"
	[[ -n "$out_dir" ]] || die "--out-dir is required"
	[[ ${#cmd[@]} -gt 0 ]] || die "a -- <command...> is required"
	# bwrap binds each of these at its REAL path; a relative value would resolve
	# against an unexpected CWD instead of failing loud, so require absolute paths.
	[[ "$clone" = /* ]] || die "--clone must be an absolute path (got '$clone')"
	[[ "$out_dir" = /* ]] || die "--out-dir must be an absolute path (got '$out_dir')"
	case "$clone_mode" in
		ro | rw-git-ro) ;;
		*) die "--clone-mode must be 'ro' or 'rw-git-ro' (got '${clone_mode:-}')" ;;
	esac
	[[ -d "$clone" ]] || die "clone path is not a directory: $clone"

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
		--unshare-all --share-net --die-with-parent --new-session --clearenv
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

	bwrap_args+=(--bind "$out_dir" "$out_dir" --chdir "$clone")

	# stdout/stderr pass through to the host shell; the caller redirects stdout
	# on the HOST side to capture any exec JSON out of the agent's reach.
	exec bwrap "${bwrap_args[@]}" -- "${cmd[@]}"
}

main "$@"
