#!/usr/bin/env python3
"""Idempotently wire the cloud-code-bot identity into a cursor-review caller.

Story 1.3 (BE-1814): the reusable cursor-review.yml (Story 1.1, PR #13) takes an
optional GitHub App identity so its consolidated review + line comments post
under a dedicated bot login instead of github-actions[bot]. Each consumer that
already holds the cloud-code-bot creds maps them through its thin caller:

    with:
      bot_app_id: ${{ vars.APP_ID }}
    secrets:
      BOT_APP_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}

Rather than hand-edit each caller, bump-cursor-review-callers.yml pipes the file
through this helper (gated per-caller — only repos with the creds provisioned).

Why line-based and not a PyYAML round-trip: the callers are heavily commented and
carry a specific hand-tuned layout (folded `diff_excludes`, inline SHA-pin
comments). A yaml.load/dump would strip every comment and reflow the file, so the
fan-out PR would be an unreviewable rewrite. This edits only the two anchor points
and leaves the rest byte-for-byte, so the PR diff is exactly the wiring.

Idempotent: if `bot_app_id:` / `BOT_APP_PRIVATE_KEY:` are already present, that
half is left untouched — a repo already wired (or re-run) converges to a no-op.

Usage: reads the caller YAML on stdin, writes the wired YAML to stdout.
    python3 wire-bot-identity.py < caller.yml > wired.yml
"""

import re
import sys

APP_ID_VALUE = "${{ vars.APP_ID }}"
PRIVATE_KEY_VALUE = "${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}"

_WITH_RE = re.compile(r"^(\s*)with:\s*$")
_SECRETS_RE = re.compile(r"^(\s*)secrets:\s*$")
_BOT_APP_ID_RE = re.compile(r"^\s*bot_app_id\s*:")
_PRIVATE_KEY_RE = re.compile(r"^\s*BOT_APP_PRIVATE_KEY\s*:")
_CURSOR_API_KEY_RE = re.compile(r"^(\s*)CURSOR_API_KEY\s*:")


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" "))]


def _child_indent(lines, block_idx: int, block_indent: str) -> str:
    """Indent of the first child of the block header at ``block_idx``.

    Falls back to the header's indent + 2 spaces when the block has no existing
    child to copy the indentation from.
    """
    for line in lines[block_idx + 1:]:
        if not line.strip():
            continue
        indent = _leading_ws(line)
        if len(indent) > len(block_indent):
            return indent
        # Dedented to the block's level or shallower — block has no children.
        break
    return block_indent + "  "


def wire(text: str) -> str:
    """Return ``text`` with the cloud-code-bot identity wired in (idempotent)."""
    lines = text.split("\n")

    already_app_id = any(_BOT_APP_ID_RE.match(ln) for ln in lines)
    already_key = any(_PRIVATE_KEY_RE.match(ln) for ln in lines)
    if already_app_id and already_key:
        return text

    # --- inject bot_app_id as the first child of the job's `with:` block ---
    if not already_app_id:
        for i, line in enumerate(lines):
            m = _WITH_RE.match(line)
            if not m:
                continue
            indent = _child_indent(lines, i, m.group(1))
            block = [
                f"{indent}# Post the consolidated review + line comments under the cloud-code-bot",
                f"{indent}# app identity (vars.APP_ID) instead of github-actions[bot]; the paired",
                f"{indent}# key rides the BOT_APP_PRIVATE_KEY secret below. Both optional — absent",
                f"{indent}# creds fall back to github-actions[bot] (non-breaking).",
                f"{indent}bot_app_id: {APP_ID_VALUE}",
            ]
            lines[i + 1:i + 1] = block
            break
        else:
            sys.stderr.write(
                "wire-bot-identity: no `with:` block found — bot_app_id not wired\n"
            )

    # --- inject BOT_APP_PRIVATE_KEY under the job's `secrets:` block ---
    if not already_key:
        insert_at = None
        secret_indent = None
        # Prefer to anchor right after CURSOR_API_KEY so it sits beside its siblings.
        for i, line in enumerate(lines):
            m = _CURSOR_API_KEY_RE.match(line)
            if m:
                insert_at = i + 1
                secret_indent = m.group(1)
                break
        if insert_at is None:
            for i, line in enumerate(lines):
                m = _SECRETS_RE.match(line)
                if m:
                    secret_indent = _child_indent(lines, i, m.group(1))
                    insert_at = i + 1
                    break
        if insert_at is not None:
            block = [
                f"{secret_indent}# PEM key paired with bot_app_id (vars.APP_ID). Optional — see above.",
                f"{secret_indent}BOT_APP_PRIVATE_KEY: {PRIVATE_KEY_VALUE}",
            ]
            lines[insert_at:insert_at] = block
        else:
            sys.stderr.write(
                "wire-bot-identity: no `secrets:` block found — "
                "BOT_APP_PRIVATE_KEY not wired\n"
            )

    return "\n".join(lines)


def main() -> int:
    sys.stdout.write(wire(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
