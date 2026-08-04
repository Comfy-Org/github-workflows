#!/usr/bin/env bash
# Install the Cursor agent CLI from a versioned, sha256-pinned release artifact
# instead of the mutable `curl https://cursor.com/install | bash` bootstrap
# (BE-5869). Old versions stay downloadable, so a Cursor release never breaks a
# run; the pin moves only via a reviewed PR to cursor-review.yml.
#
# Lives here, not inline in cursor-review.yml, because three jobs (preflight,
# each review matrix cell, consolidate) install the CLI: a forked copy per job
# is exactly the drift mode AGENTS.md warns about, and every hardening below
# would otherwise have to be applied identically in three places. Loaded at run
# time from a pinned ref of THIS repo, like the rest of .github/cursor-review/.
#
# Inputs (env — set once in cursor-review.yml's top-level `env:`):
#   CURSOR_CLI_VERSION  version string, e.g. 2026.07.23-e383d2b
#   CURSOR_CLI_SHA256   sha256 of that version's linux/x64 agent-cli-package.tar.gz
#
# Appends ~/.local/bin to $GITHUB_PATH so later steps can call `cursor-agent`.
set -euo pipefail

: "${CURSOR_CLI_VERSION:?CURSOR_CLI_VERSION must be set (see cursor-review.yml env)}"
: "${CURSOR_CLI_SHA256:?CURSOR_CLI_SHA256 must be set (see cursor-review.yml env)}"

url="https://downloads.cursor.com/lab/${CURSOR_CLI_VERSION}/linux/x64/agent-cli-package.tar.gz"

# mktemp so nothing can pre-place a symlink for curl to write through. The trap
# makes cleanup unconditional — under `set -e` a checksum or tar failure aborts
# before any explicit `rm` would run.
pkg="$(mktemp "${RUNNER_TEMP:-/tmp}/cursor-cli.XXXXXX")"
trap 'rm -f "$pkg"' EXIT

# Flag notes, since this is a supply-chain control and the payload is ~83 MB
# (the old bootstrap was ~6 KB, so none of this mattered before):
#   --proto/--proto-redir  -L would otherwise follow a redirect down to plaintext
#                          http; a checksum failure is a pipeline outage, so an
#                          on-path attacker shouldn't be able to force one.
#   --speed-limit/--speed-time  stall detection instead of a flat wall-clock cap,
#                          which would kill a slow-but-healthy transfer at 99%.
#                          --max-time is only a backstop: 300s is ~175x the
#                          observed download time and, with --retry-max-time 180,
#                          keeps the worst case (~8 min) inside the tightest job
#                          cap (preflight's timeout-minutes: 10).
#   --max-filesize         bounds what a hostile or malfunctioning CDN can write
#                          to the runner volume before the digest is ever checked.
#   plain --retry          NOT --retry-all-errors: a 404 from a typo'd or pruned
#                          CURSOR_CLI_VERSION is permanent, and retrying it turns
#                          a clear error into an opaque job timeout across all
#                          ~10 concurrent jobs.
curl -fsSL \
  --proto '=https' --proto-redir '=https' \
  --connect-timeout 10 \
  --speed-limit 10240 --speed-time 30 --max-time 300 \
  --max-filesize 268435456 \
  --retry 3 --retry-delay 2 --retry-max-time 180 \
  "$url" -o "$pkg"

echo "${CURSOR_CLI_SHA256}  ${pkg}" | sha256sum -c -

# `tar --strip-components=1` silently DROPS any member that isn't nested at
# least one level deep, and still exits 0 — a layout change at pin-bump time
# would produce a partial install that the smoke test below can't see. The
# pinned archive is a single `dist-package/` tree, so any slash-free member
# means the layout moved: fail loudly instead of installing half of it.
if tar -tzf "$pkg" | grep -qv '/'; then
  echo "::error::Unexpected ${CURSOR_CLI_VERSION} archive layout — top-level members would be dropped by --strip-components=1."
  tar -tzf "$pkg" | grep -v '/'
  exit 1
fi

# Mirror the vendor installer's layout: versions dir + ~/.local/bin symlink
# (~/.local/bin is already on the hosted runner's default PATH). Recreate $dest
# from scratch rather than `mkdir -p` onto whatever is there, so the tree that
# actually executes is exactly the archive we just verified.
dest="$HOME/.local/share/cursor-agent/versions/${CURSOR_CLI_VERSION}"
rm -rf "$dest"
mkdir -p "$dest" "$HOME/.local/bin"
tar --strip-components=1 -xzf "$pkg" -C "$dest"
ln -sf "$dest/cursor-agent" "$HOME/.local/bin/cursor-agent"

# Prove the pinned binary runs AND is the build we verified. Printing the
# version without comparing it would not catch the bits being swapped (a CLI
# self-update, a stale symlink) — which is the whole point of the pin.
installed="$("$HOME/.local/bin/cursor-agent" --version)"
if [ "$installed" != "$CURSOR_CLI_VERSION" ]; then
  echo "::error::cursor-agent reports '${installed}', expected the pinned '${CURSOR_CLI_VERSION}'."
  exit 1
fi
echo "cursor-agent ${installed} installed from a sha256-verified artifact."

if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$HOME/.local/bin" >> "$GITHUB_PATH"
fi
