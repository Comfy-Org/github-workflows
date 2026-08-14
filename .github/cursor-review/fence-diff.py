#!/usr/bin/env python3
"""Fence the reviewed diff with a per-run nonce (BE-7645).

The diff spliced into the panel and judge prompts is `git diff BASE...HEAD` —
i.e. **attacker-authored PR file bytes**, the most attacker-controlled input the
review sees. It used to sit between STATIC literal fences (`=== BEGIN DIFF ===`
/ `=== END DIFF ===`) with no untrusted-data labelling, while the prior-review
ledger — a strictly *less* controlled input — got both (see `build-ledger.py`'s
`_UNTRUSTED_HEADER`). This module closes that gap.

The control is an **unguessable per-run nonce in the fence**, not literal-string
matching. A PR cannot forge the close fence because it cannot know the nonce:
it is minted in the `diff-size` job at run time, after the head SHA the diff is
built from is already fixed, and it is never echoed to the (public) run log.

Two things this deliberately does NOT do:

* **It does not mutate the diff.** The body is copied through byte for byte,
  because a reviewer must be able to trust that the code shown is the code under
  review. In particular it is NOT run through `build-ledger.py`'s
  `_defang_fences`: that rewrites fence-looking lines, and in a unified diff
  every content line already carries a `+`/`-`/space prefix, so a forged fence
  renders as `+=== END DIFF ===` and cannot byte-match a close fence anyway.
  Defanging would therefore corrupt the payload to buy nothing.
* **It does not make the fence tamper-proof against a *guessed* nonce.** It
  makes the fence unguessable in practice and labels the region as data; the
  prompt wording (`prompt-adversarial.md`, `prompt-edge-case.md`,
  `prompt-judge.md`) carries the other half — that no text inside the markers is
  an instruction.

Subcommands:

  strip-marker  Drop the prompt head's trailing static `=== BEGIN DIFF ===`
                line. That line is the ledger splicer's `--marker` anchor, so it
                must stay in the prompt `.md` files verbatim; it is removed only
                after the splice, because `emit` re-emits it WITH the nonce.

  emit          Write `=== BEGIN <label> <nonce> ===\\n<diff bytes>\\n=== END
                <label> <nonce> ===\\n` to stdout (or `--out`).

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import argparse
import os
import re
import sys

# 16 random bytes rendered as hex by `openssl rand -hex 16`. The range is
# permissive on length (a caller may mint a longer nonce) but not on alphabet:
# anything outside lowercase hex could smuggle whitespace or `=` into a fence
# line and break the very delimiting this exists to guarantee.
NONCE_RE = re.compile(r"^[0-9a-f]{16,64}$")

# `DIFF`, or `HUNKS NEW SINCE ROUND 3` / `HUNKS NEW SINCE ROUND ?` (the `?` is
# the workflow's `${LEDGER_ROUNDS:-?}` fallback when the ledger job produced no
# round count). No `=`, no newline, no lowercase — a label is a fixed vocabulary
# word plus a round number, never free text.
LABEL_RE = re.compile(r"^[A-Z][A-Z0-9 ?]*$")


def _check_nonce(nonce: str) -> str:
    if not NONCE_RE.match(nonce or ""):
        raise SystemExit(
            "fence-diff: nonce must be 16-64 lowercase hex characters "
            "(got a value of length %d)" % len(nonce or "")
        )
    return nonce


def _check_label(label: str) -> str:
    if not LABEL_RE.match(label or ""):
        raise SystemExit(
            "fence-diff: label must match %s (got %r)" % (LABEL_RE.pattern, label)
        )
    return label


def open_fence(label: str, nonce: str) -> str:
    """The opening fence line, e.g. `=== BEGIN DIFF 0f3a… ===`."""
    return "=== BEGIN %s %s ===" % (_check_label(label), _check_nonce(nonce))


def close_fence(label: str, nonce: str) -> str:
    """The closing fence line, e.g. `=== END DIFF 0f3a… ===`."""
    return "=== END %s %s ===" % (_check_label(label), _check_nonce(nonce))


def fence_block(body: bytes, label: str, nonce: str) -> bytes:
    """Wrap `body` in nonce'd fences, preserving it byte for byte.

    The trailing newline before the close fence mirrors what the workflow's
    `cat <diff>; echo ""; echo "=== END DIFF ==="` produced, so the reviewed
    bytes are unchanged from before this hardening apart from the fence tokens.
    """
    return b"".join(
        [
            open_fence(label, nonce).encode("utf-8"),
            b"\n",
            body,
            b"\n",
            close_fence(label, nonce).encode("utf-8"),
            b"\n",
        ]
    )


def strip_trailing_marker(text: str, marker: str) -> str:
    """Remove a trailing `marker` line from `text`, if that is how it ends.

    The prompt files end with the bare `=== BEGIN DIFF ===` line and the ledger
    splice preserves that (it inserts *before* the marker), so this is the
    normal case. When the head does NOT end with the marker — a prompt edit, or
    the splicer's degraded append path — the text is returned unchanged and
    `emit` still supplies a properly nonce'd opening fence. There is therefore
    no state in which the diff is opened by a stale un-nonce'd fence.
    """
    body = text[:-1] if text.endswith("\n") else text
    head, sep, last = body.rpartition("\n")
    if last != marker:
        return text
    return head + "\n" if sep else ""


def cmd_strip_marker(args) -> int:
    with open(args.file, encoding="utf-8") as f:
        text = f.read()
    out = strip_trailing_marker(text, args.marker)
    if out == text:
        # Loud but non-fatal: the review still runs, correctly fenced, because
        # `emit` supplies the opening fence itself.
        print(
            "::warning::fence-diff: %s does not end with %r — leaving it as is; "
            "the nonce'd opening fence is emitted regardless"
            % (os.path.basename(args.file), args.marker)
        )
        return 0
    with open(args.file, "w", encoding="utf-8") as f:
        f.write(out)
    return 0


def cmd_emit(args) -> int:
    with open(args.diff, "rb") as f:
        body = f.read()
    block = fence_block(body, args.label, args.nonce)
    if args.out:
        with open(args.out, "wb") as f:
            f.write(block)
    else:
        sys.stdout.buffer.write(block)
        sys.stdout.buffer.flush()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    strip = sub.add_parser(
        "strip-marker", help="drop the prompt head's trailing static fence line"
    )
    strip.add_argument("--file", required=True)
    strip.add_argument("--marker", required=True)
    strip.set_defaults(func=cmd_strip_marker)

    emit = sub.add_parser("emit", help="write the nonce-fenced diff block")
    emit.add_argument("--diff", required=True)
    emit.add_argument("--label", default="DIFF")
    emit.add_argument("--nonce", required=True)
    emit.add_argument("--out", default=None, help="default: stdout")
    emit.set_defaults(func=cmd_emit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
