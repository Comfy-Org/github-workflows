#!/usr/bin/env python3
"""cursor-approve: render the per-axis prompts and turn the axes into one decision.

Five reviewers each judge one axis of a PR (business, design, correctness,
completeness, conformance) and answer with one JSON object:
``{"verdict": "red"|"yellow"|"green", "confidence": 0..1, "summary": "..."}``.
Two subcommands, both pure — nothing here writes to GitHub:

``render``
    Print the prompt for one axis: ``prompt-common.md`` followed by
    ``prompt-<axis>.md``, with every ``{{placeholder}}`` substituted. An unknown
    axis exits 2.

``decide``
    Read ``<axis>.json`` for each expected axis from a directory and print one
    JSON result: ``event`` (``APPROVE`` or ``NONE``), the per-axis ``verdicts``,
    the per-axis detail in ``axes``, and the ``reasons``. The rules, in order:

    1. any expected axis missing, unparsable, with a verdict outside
       red/yellow/green, or a confidence outside 0..1 → ``NONE`` (the round is
       untrusted: withhold the approval, never veto);
    2. any ``red`` → ``NONE``, naming the red axes;
    3. more ``yellow`` axes than ``--max-yellow-axes`` → ``NONE``;
    4. otherwise → ``APPROVE``.

    Bad arguments (an unknown axis, an empty axis list, ``--max-yellow-axes``
    outside 0..3 or not below the number of expected axes) exit 2. Every
    decision, including ``NONE``, exits 0.

The workflow's decide job submits the review by reusing
``.github/cursor-review/auto-approve.py``; this script only says what to submit.

Trust model: every verdict is model output over the PR's own content, so a diff
that prompt-injects all five reviewers can steer the round to an approval. The
approval is an automated review signal, not a substitute for a human reviewer.
"""

import argparse
import json
import math
import os
import re
import stat
import sys

AXES = ("business", "design", "correctness", "completeness", "conformance")
VERDICTS = ("red", "yellow", "green")
MAX_YELLOW_LIMIT = 3
SUMMARY_LIMIT = 1200
# An axis output is one small JSON object; the cap is what decide will read of
# a model-written file before calling it untrusted.
MAX_OUTPUT_BYTES = 64 * 1024

APPROVE = "APPROVE"
NONE = "NONE"

PROMPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
PLACEHOLDERS = ("pr_number", "repo", "head_sha", "merge_base_sha", "base_ref", "context_file")
SHA_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
# Every value lands in a prompt, two of them inside a `git diff` command line,
# so each one is checked to be the shape it claims to be.
# The character class alone would fullmatch `../../../home/runner/.ssh/id_rsa`,
# and `context_file` names a file the reviewer agent is told to read, so a `..`
# COMPONENT is rejected as well. Only the component is: `a..b` stays a legal
# name, and an absolute path stays legal because the workflow passes one
# ($RUNNER_TEMP/pr-context.md). `..` is not a legal ref component either, so
# `base_ref` carries the same guard.
NO_DOTDOT = r"(?!.*(?:^|/)\.\.(?:/|$))"
PATH_RE = re.compile(NO_DOTDOT + r"[A-Za-z0-9_./-]+")
# `repo` is itself an owner/name path fragment that lands in the prompt, so it
# carries the same `..` guard as the other two. Neither segment may START with
# `-` either: `-x/-y` is option-shaped, and no real GitHub owner or repo does.
_SEG = r"[A-Za-z0-9_.][A-Za-z0-9_.-]*"
VALUE_RES = {
    "pr_number": re.compile(r"[1-9][0-9]*"),
    "repo": re.compile(NO_DOTDOT + _SEG + "/" + _SEG),
    "head_sha": SHA_RE,
    "merge_base_sha": SHA_RE,
    "base_ref": PATH_RE,
    "context_file": PATH_RE,
}


def render(axis: str, values: dict) -> str:
    """Common rules + the axis file, every placeholder substituted. Pure but for the reads.

    Substitution is one pass over the template, so a value is never itself
    scanned for placeholders. Raises ValueError on an unknown axis, a missing or
    malformed value, or a placeholder the template uses that this script does
    not know.
    """
    if axis not in AXES:
        raise ValueError(f"unknown axis {axis!r} (expected one of {', '.join(AXES)})")
    for name in PLACEHOLDERS:
        value = values.get(name)
        if not isinstance(value, str) or not VALUE_RES[name].fullmatch(value):
            raise ValueError(f"--{name.replace('_', '-')} is missing or malformed: {value!r}")
    parts = []
    for filename in ("prompt-common.md", f"prompt-{axis}.md"):
        with open(os.path.join(PROMPT_DIR, filename), encoding="utf-8") as f:
            parts.append(f.read().strip("\n"))
    template = "\n\n".join(parts) + "\n"

    def substitute(match):
        name = match.group(1)
        if name not in PLACEHOLDERS:
            raise ValueError(f"the prompt uses an unknown placeholder {{{{{name}}}}}")
        return values[name]

    return PLACEHOLDER_RE.sub(substitute, template)


def parse_axes(value: str) -> list:
    """The comma-separated expected-axis list, deduplicated in order."""
    axes = []
    for name in (value or "").split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name not in AXES:
            raise ValueError(f"unknown axis {name!r} in --axes (expected some of {', '.join(AXES)})")
        if name not in axes:
            axes.append(name)
    if not axes:
        raise ValueError("--axes names no axis")
    return axes


def parse_max_yellow(value) -> int:
    try:
        n = int(str(value).strip())
    except ValueError:
        n = -1
    if not 0 <= n <= MAX_YELLOW_LIMIT:
        raise ValueError(f"--max-yellow-axes must be an integer from 0 to {MAX_YELLOW_LIMIT}, got {value!r}")
    return n


class _DuplicateKeyError(ValueError):
    """A repeated member name in an axis output."""


def _no_duplicate_keys(pairs):
    """json.loads keeps the LAST of a duplicate key, so a repeated member name
    would let `{"verdict": "red", ..., "verdict": "green"}` read as green and
    reach APPROVE while anyone opening the raw file plainly sees red. On a path
    whose whole job is to fail closed on ambiguous model output, that ambiguity
    is rejected rather than silently resolved either way."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKeyError(f"duplicate key {key!r} in the output")
        out[key] = value
    return out


def _short(value) -> str:
    """repr() of a model-supplied value, bounded: it is echoed into the reasons."""
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def validate_output(data):
    """(verdict, confidence, summary) of one axis output, or raise ValueError."""
    if not isinstance(data, dict):
        raise ValueError("the output is not a JSON object")
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict {_short(verdict)} is not one of {', '.join(VERDICTS)}")
    confidence = data.get("confidence")
    # bool is an int subclass; NaN and the infinities fail the range check.
    # math.isfinite() coerces its argument to float and raises OverflowError --
    # an ArithmeticError, NOT a ValueError -- for a JSON integer past ~1e308,
    # which would escape load_output and cmd_decide's per-axis except and abort
    # the whole decide instead of marking this one axis untrusted.
    # .github/groom/config.py catches the same pair for the same reason.
    try:
        ok = (
            not isinstance(confidence, bool)
            and isinstance(confidence, (int, float))
            and math.isfinite(confidence)
            and 0 <= confidence <= 1
        )
    except OverflowError:
        ok = False
    if not ok:
        raise ValueError(f"confidence {_short(confidence)} is not a number from 0 to 1")
    summary = data.get("summary")
    summary = summary[:SUMMARY_LIMIT] if isinstance(summary, str) else ""
    return verdict, confidence, summary


def load_output(path: str):
    """Parse one axis output file; raise ValueError on anything unusable."""
    # This file is model-written and untrusted. O_NONBLOCK so a FIFO at this
    # path cannot block the open, and every check runs against the DESCRIPTOR via
    # fstat rather than the path via stat: a path-level stat followed by a
    # separate open is two lookups, and whatever can write <axis>.json can swap
    # the object between them and put the blocking open back.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ValueError("no output") from None
    except OSError as e:
        raise ValueError(f"unreadable output ({e.__class__.__name__})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("the output is not a regular file")
        if info.st_size > MAX_OUTPUT_BYTES:
            raise ValueError(f"the output is larger than {MAX_OUTPUT_BYTES} bytes")
        # Bytes, not characters: MAX_OUTPUT_BYTES is a byte budget, and a
        # text-mode read counts characters, so a multibyte file could pass a
        # character-counted bound at several times the budget. Read first,
        # bound the bytes, and only then decode.
        chunks, remaining = [], MAX_OUTPUT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as e:
        raise ValueError(f"unreadable output ({e.__class__.__name__})") from None
    finally:
        os.close(fd)
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError(f"the output is larger than {MAX_OUTPUT_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("unreadable output (UnicodeDecodeError)") from None
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKeyError as e:
        raise ValueError(str(e)) from None
    # RecursionError is a RuntimeError, NOT a ValueError: a few tens of KB of
    # `[[[[...` raises it out of json.loads, and uncaught it would abort the
    # whole decide on exactly the input this path exists to fail closed on.
    # .github/cursor-review/build-ledger.py catches it for the same reason.
    except (ValueError, RecursionError):
        raise ValueError("the output is not valid JSON") from None
    # Validation is guarded too, not just the parse: _short()'s repr() walks the
    # value, so a structure json.loads accepted can still overflow the stack
    # here. Whether that window is open at all depends on repr having less
    # headroom than the scanner, which is an interpreter and build detail -- so
    # the contract is made structural rather than left resting on it.
    try:
        return validate_output(data)
    except RecursionError:
        raise ValueError("the output is nested too deeply to validate") from None


def decide(outputs: dict, axes: list, max_yellow: int):
    """Return the decision dict for already-loaded outputs. Pure; no I/O.

    `outputs` maps an axis to (verdict, confidence, summary), or to a ValueError
    when its output could not be used. An axis absent from `outputs` is missing.
    """
    detail = {}
    untrusted = []
    for axis in axes:
        out = outputs.get(axis, ValueError("no output"))
        if isinstance(out, ValueError):
            untrusted.append(f"{axis}: {out}")
            detail[axis] = {"verdict": None, "confidence": None, "summary": "", "error": str(out)}
        else:
            verdict, confidence, summary = out
            detail[axis] = {"verdict": verdict, "confidence": confidence, "summary": summary}
    verdicts = {axis: detail[axis]["verdict"] for axis in axes}

    if untrusted:
        event, reasons = NONE, [f"untrusted axis output, approval withheld ({u})" for u in untrusted]
    else:
        red = [a for a in axes if verdicts[a] == "red"]
        yellow = [a for a in axes if verdicts[a] == "yellow"]
        if red:
            event, reasons = NONE, [f"red on {', '.join(red)}"]
        elif len(yellow) > max_yellow:
            event, reasons = NONE, [f"{len(yellow)} yellow axes ({', '.join(yellow)}) exceed the limit of {max_yellow}"]
        else:
            event = APPROVE
            reasons = [f"no red axis, {len(yellow)} yellow of at most {max_yellow}"]
    return {"event": event, "verdicts": verdicts, "axes": detail, "reasons": reasons}


def cmd_render(args) -> int:
    values = {name: getattr(args, name) for name in PLACEHOLDERS}
    try:
        sys.stdout.write(render(args.axis, values))
    # OSError as well as ValueError: render() reads the prompt files, and a
    # missing or unreadable prompt-common.md / prompt-<axis>.md must produce the
    # documented ::error:: plus exit 2 that the calling workflow keys on rather
    # than a traceback and exit 1.
    except (ValueError, OSError) as e:
        print(f"::error::{e}", file=sys.stderr)
        return 2
    return 0


def cmd_decide(args) -> int:
    try:
        axes = parse_axes(args.axes)
        max_yellow = parse_max_yellow(args.max_yellow_axes)
        # Checked against the axis list, not only MAX_YELLOW_LIMIT: a limit at or
        # above the number of expected axes means every axis could come back
        # yellow -- every reviewer raising a material concern, zero green -- and
        # the round would still APPROVE, so narrowing --axes would silently
        # weaken the gate rather than tighten it.
        if max_yellow >= len(axes):
            raise ValueError(
                f"--max-yellow-axes must be below the number of expected axes "
                f"({len(axes)}: {', '.join(axes)}), got {max_yellow}"
            )
    except ValueError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 2
    outputs = {}
    for axis in axes:
        try:
            outputs[axis] = load_output(os.path.join(args.outputs_dir, f"{axis}.json"))
        except ValueError as e:
            outputs[axis] = e
        # The per-axis boundary is the last place the "every decision exits 0"
        # contract can be kept, so it does not depend on load_output having
        # converted every RecursionError on the way out.
        except RecursionError:
            outputs[axis] = ValueError("the output is nested too deeply")
    print(json.dumps(decide(outputs, axes, max_yellow), indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--axis", required=True)
    for name in PLACEHOLDERS:
        r.add_argument(f"--{name.replace('_', '-')}", dest=name, required=True)
    d = sub.add_parser("decide")
    d.add_argument("--outputs-dir", required=True)
    d.add_argument("--axes", default=",".join(AXES))
    d.add_argument("--max-yellow-axes", default="0")
    args = parser.parse_args(argv)
    return cmd_render(args) if args.cmd == "render" else cmd_decide(args)


if __name__ == "__main__":
    sys.exit(main())
