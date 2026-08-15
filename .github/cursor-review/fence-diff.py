#!/usr/bin/env python3
"""Fence the reviewed diff with a per-run nonce (BE-7645).

The diff spliced into the panel and judge prompts is `git diff BASE...HEAD` —
i.e. **attacker-authored PR file bytes**, the most attacker-controlled input the
review sees. It used to sit between STATIC literal fences (`=== BEGIN DIFF ===`
/ `=== END DIFF ===`) with no untrusted-data labelling, while the prior-review
ledger — a strictly *less* controlled input — got both (see `build-ledger.py`'s
`_UNTRUSTED_HEADER`). This module closes that gap.

The control is an **unguessable per-run nonce in the fence**, not literal-string
matching. A PR cannot forge the close fence because the nonce does not exist
when the bytes it would have to contain are already fixed: it is minted inside
the consuming job, at prompt-build time, long after the head SHA the diff is
built from was resolved.

The nonce is *disclosed within its own run*, not secret. It is deliberately
never put in a step `env:` block (Actions prints a step's env map into the run
log before the script runs, and consumer logs are public), but a panel model
that quotes a fence marker while describing the diff republishes it into model
output the workflow prints and posts. That is harmless: by then the diff those
fences wrap is immutable, and every prompt mints its own nonce, so a value seen
in one job's output cannot forge a fence in another's.

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

  mint          Print a fresh nonce. Called per prompt-build step so the panel
                and judge prompts never share one — see `cmd_mint`.

  strip-marker  Drop the prompt head's trailing static `=== BEGIN DIFF ===`
                line. That line is the ledger splicer's `--marker` anchor, so it
                must stay in the prompt `.md` files verbatim; it is removed only
                after the splice, because `emit` re-emits it WITH the nonce.

  emit          Write `=== BEGIN <label> <nonce> ===\\n<body bytes>\\n=== END
                <label> <nonce> ===\\n` to stdout (or `--out`).

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import argparse
import os
import re
import secrets
import sys

# 16 random bytes rendered as hex. The range is permissive on length (a caller
# may mint a longer nonce) but not on alphabet: anything outside lowercase hex
# could smuggle whitespace or `=` into a fence line and break the very
# delimiting this exists to guarantee. Matched with `fullmatch`, never `match`:
# in Python `$` also matches just before a trailing newline, so `re.match` would
# accept `"<32 hex>\n"` and split the fence across two lines — precisely the
# whitespace smuggling the alphabet restriction exists to prevent.
NONCE_RE = re.compile(r"[0-9a-f]{16,64}")

# `DIFF`, `PANEL FINDINGS`, or `HUNKS NEW SINCE ROUND 3` / `HUNKS NEW SINCE
# ROUND ?` (the `?` is the workflow's `${LEDGER_ROUNDS:-?}` fallback when the
# ledger job produced no round count). No `=`, no newline, no lowercase — a
# label is a fixed vocabulary word plus a round number, never free text. Also
# `fullmatch`-only, for the same trailing-newline reason as NONCE_RE.
LABEL_RE = re.compile(r"[A-Z][A-Z0-9 ?]*")

# How many random bytes `mint` draws. 16 bytes = 32 hex characters = 128 bits.
NONCE_BYTES = 16


def _check_nonce(nonce: str) -> str:
    if not NONCE_RE.fullmatch(nonce or ""):
        raise SystemExit(
            "fence-diff: nonce must be 16-64 lowercase hex characters "
            "(got a value of length %d)" % len(nonce or "")
        )
    return nonce


def _check_label(label: str) -> str:
    if not LABEL_RE.fullmatch(label or ""):
        raise SystemExit(
            "fence-diff: label must match %s (got %r)" % (LABEL_RE.pattern, label)
        )
    return label


def mint_nonce() -> str:
    """A fresh fence nonce, from the CSPRNG."""
    return _check_nonce(secrets.token_hex(NONCE_BYTES))


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

    The in-memory equivalent of what `emit` streams; kept because it makes the
    byte-for-byte property directly assertable in one expression.
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


def ends_with_marker(text: str, marker: str) -> bool:
    """Whether `text`'s last non-blank line is exactly `marker`.

    Trailing whitespace is ignored, so `=== BEGIN DIFF ===\\n\\n` and a CRLF or
    space-padded variant all count. Anything laxer would leave the stale
    un-nonce'd marker in the prompt ahead of the nonce'd one — an opener a
    forged `=== END DIFF ===` inside the diff body could pair with.
    """
    return text.rstrip().rpartition("\n")[2] == marker


def strip_trailing_marker(text: str, marker: str) -> str:
    """Remove a trailing `marker` line from `text`, if that is how it ends.

    The prompt files end with the bare `=== BEGIN DIFF ===` line and the ledger
    splice preserves that (it inserts *before* the marker), so this is the
    normal case. When the head does NOT end with the marker — a prompt edit, or
    the splicer's degraded append path — the text is returned as is apart from a
    guaranteed trailing newline, and `emit` still supplies a properly nonce'd
    opening fence. There is therefore no state in which the diff is opened by a
    stale un-nonce'd fence.

    That newline is not cosmetic: `build-ledger.py`'s degraded append path
    (`prompt_text + "\\n" + block`) can return a head with no trailing newline,
    and the workflow's `cat head` immediately precedes the opening fence — so
    without it the opener glues onto the head's last line
    (`…LEDGER ===== BEGIN DIFF <nonce> ===`) and the diff has no valid opening
    marker line at all.
    """
    stripped = text.rstrip()
    head, sep, last = stripped.rpartition("\n")
    if last != marker:
        return text if text.endswith("\n") or not text else text + "\n"
    return head + "\n" if sep else ""


def cmd_strip_marker(args) -> int:
    with open(args.file, encoding="utf-8") as f:
        text = f.read()
    out = strip_trailing_marker(text, args.marker)
    if not ends_with_marker(text, args.marker):
        # Loud but non-fatal: the review still runs, correctly fenced, because
        # `emit` supplies the opening fence itself. The file may still be
        # rewritten below, to add the trailing newline that fence depends on.
        print(
            "::warning::fence-diff: %s does not end with %r — leaving it as is; "
            "the nonce'd opening fence is emitted regardless"
            % (os.path.basename(args.file), args.marker)
        )
    if out != text:
        with open(args.file, "w", encoding="utf-8") as f:
            f.write(out)
    return 0


def cmd_mint(args) -> int:
    # stdout only, for `nonce="$(fence-diff.py mint)"`. Never write it to
    # $GITHUB_OUTPUT or a step `env:` block: Actions dumps a step's env map into
    # the (public) run log before the script runs.
    print(mint_nonce())
    return 0


# 1 MiB. The diff is capped by changed LINES, not bytes, so a PR with a few
# enormous lines can still be large; stream it rather than holding the body and
# a fenced copy of it in memory at once, in every panel and judge job.
_CHUNK = 1 << 20


def cmd_emit(args) -> int:
    opener = (open_fence(args.label, args.nonce) + "\n").encode("utf-8")
    closer = ("\n" + close_fence(args.label, args.nonce) + "\n").encode("utf-8")
    with open(args.body, "rb") as src:
        if args.out:
            with open(args.out, "wb") as dst:
                _stream(src, dst, opener, closer)
        else:
            _stream(src, sys.stdout.buffer, opener, closer)
            sys.stdout.buffer.flush()
    return 0


def _stream(src, dst, opener: bytes, closer: bytes) -> None:
    dst.write(opener)
    while True:
        chunk = src.read(_CHUNK)
        if not chunk:
            break
        dst.write(chunk)
    dst.write(closer)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    mint = sub.add_parser("mint", help="print a fresh per-prompt fence nonce")
    mint.set_defaults(func=cmd_mint)

    strip = sub.add_parser(
        "strip-marker", help="drop the prompt head's trailing static fence line"
    )
    strip.add_argument("--file", required=True)
    strip.add_argument("--marker", required=True)
    strip.set_defaults(func=cmd_strip_marker)

    emit = sub.add_parser("emit", help="write a nonce-fenced block")
    emit.add_argument("--body", required=True, help="file holding the bytes to fence")
    emit.add_argument("--label", default="DIFF")
    emit.add_argument("--nonce", required=True)
    emit.add_argument("--out", default=None, help="default: stdout")
    emit.set_defaults(func=cmd_emit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
