#!/usr/bin/env python3
"""cursor-approve: render the per-axis prompts and turn the axes into one decision.

Five reviewers each judge one axis of a PR (business, design, correctness,
completeness, conformance) and answer with one JSON object:
``{"verdict": "red"|"yellow"|"green"|"n/a", "confidence": 0..1,
   "headline": "<=100 chars", "summary": "..."}``.
Three subcommands, all pure — nothing here writes to GitHub:

``render``
    Print the prompt for one axis: ``prompt-common.md`` followed by
    ``prompt-<axis>.md``, with every ``{{placeholder}}`` substituted. An unknown
    axis exits 2.

``extract``
    Pull the one JSON object out of a model's raw reply (cursor-axis-base.yml
    runs this before uploading) and stamp it with ``--commit-sha``, the commit
    the axis judged; zero or several candidates exit 1.

``decide``
    Read ``<axis>.json`` for each expected axis from a directory and print one
    JSON result: ``event`` (``APPROVE`` or ``NONE``), the per-axis ``verdicts``,
    the per-axis detail in ``axes``, and the ``reasons``. The rules, in order:

    1. any expected axis missing, unparsable, with a verdict outside
       red/yellow/green/n/a, a confidence outside 0..1, a missing or
       empty ``headline`` (an over-long one is clamped to the cap, not
       rejected), or (with ``--commit-sha``)
       a stamped commit other than that one → ``NONE`` (the round is
       untrusted: withhold the approval, never veto);
    2. any ``red`` → ``NONE``, naming the red axes;
    3. more ``yellow`` axes than ``--max-yellow-axes`` → ``NONE`` (an ``n/a``
       axis is not a yellow and is not counted here);
    4. every axis ``n/a`` → ``NONE``: nothing was judged, so there is nothing
       to approve on;
    5. otherwise → ``APPROVE``.

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
# `n/a` is a verdict, not an abstention: the axis ran, reached the change, and
# reports that nothing it judges is in scope. It is counted as REPORTING (so it
# does not read as a missing axis, which withholds) but not as a yellow (so it
# does not block). See `decide` for the one case where it still withholds.
VERDICTS = ("red", "yellow", "green", "n/a")
NA = "n/a"
HEADLINE_MAX = 100
MAX_YELLOW_LIMIT = 3
SUMMARY_LIMIT = 1200
# An axis output is one small JSON object; the cap is what decide will read of
# a model-written file before calling it untrusted.
MAX_OUTPUT_BYTES = 64 * 1024
# The raw model reply extract reads; a longer one is rejected, not truncated.
MAX_RAW_BYTES = 4 * MAX_OUTPUT_BYTES

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
    # A `type: number` input can render as `0` or `0.0` (auto-approve.py's
    # parse_max_rounds handles the same), so an integral float is accepted;
    # `1.5`, `inf` and `nan` are not silently truncated.
    try:
        number = float(str(value).strip())
        n = int(number) if math.isfinite(number) and number.is_integer() else -1
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
    """(verdict, confidence, summary, headline) of one axis output, or raise ValueError."""
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
    # The headline is the ONLY part of this that reaches the PR, so it is
    # required rather than best-effort: an axis that does not write one leaves
    # the card with a verdict and no reason, which is the exact failure this
    # field was added to fix. An over-long one is clamped, not rejected: the
    # model's word count is not deterministic, and failing a sound verdict
    # because its reason ran a few words over turned the axis job red on
    # length alone. The clamp keeps the opening words, which carry the reason.
    headline = data.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        raise ValueError("headline is missing or empty")
    headline = clamp_headline(" ".join(headline.split()))
    return verdict, confidence, summary, headline


def clamp_headline(headline: str) -> str:
    """`headline` cut to HEADLINE_MAX characters, at a word boundary, with "…".

    Falls back to a hard cut when the last space that fits sits in the first
    half (one long token, or "Fix: <long path>"), so the clamp never throws
    away most of the reason. Raises ValueError when nothing but separators
    survives: a bare "…" is the reason-free row the headline exists to prevent.
    """
    if len(headline) <= HEADLINE_MAX:
        return headline
    hard = headline[:HEADLINE_MAX - 1]
    space = headline.rfind(" ", 0, HEADLINE_MAX)
    candidates = [headline[:space], hard] if space >= HEADLINE_MAX // 2 else [hard]
    for cut in candidates:
        cut = cut.rstrip(" ,;:-")
        if cut.strip(" ,;:-"):
            return cut + "…"
    raise ValueError("headline is over the limit and holds no reason to keep")


def load_output(path: str, commit_sha: str = ""):
    """Parse one axis output file; raise ValueError on anything unusable.

    With `commit_sha`, the output must also carry that exact `commit_sha` (which
    `extract` stamps from the axis job's own input), so a verdict about any other
    commit is untrusted rather than counted."""
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
        result = validate_output(data)
    except RecursionError:
        raise ValueError("the output is nested too deeply to validate") from None
    if commit_sha and data.get("commit_sha") != commit_sha:
        raise ValueError(f"the output judged commit {_short(data.get('commit_sha'))}, not {commit_sha[:7]}")
    return result


def decide(outputs: dict, axes: list, max_yellow: int):
    """Return the decision dict for already-loaded outputs. Pure; no I/O.

    `outputs` maps an axis to (verdict, confidence, summary, headline), or to a ValueError
    when its output could not be used. An axis absent from `outputs` is missing.
    """
    detail = {}
    untrusted = []
    for axis in axes:
        out = outputs.get(axis, ValueError("no output"))
        if isinstance(out, ValueError):
            untrusted.append(f"{axis}: {out}")
            detail[axis] = {"verdict": None, "confidence": None, "summary": "",
                            "headline": "", "error": str(out)}
        else:
            verdict, confidence, summary, headline = out
            detail[axis] = {"verdict": verdict, "confidence": confidence,
                            "summary": summary, "headline": headline}
    verdicts = {axis: detail[axis]["verdict"] for axis in axes}

    if untrusted:
        event, reasons = NONE, [f"untrusted axis output, approval withheld ({u})" for u in untrusted]
    else:
        red = [a for a in axes if verdicts[a] == "red"]
        yellow = [a for a in axes if verdicts[a] == "yellow"]
        na = [a for a in axes if verdicts[a] == NA]
        if red:
            event, reasons = NONE, [f"red on {', '.join(red)}"]
        elif len(yellow) > max_yellow:
            event, reasons = NONE, [f"{len(yellow)} yellow axes ({', '.join(yellow)}) exceed the limit of {max_yellow}"]
        elif len(na) == len(axes):
            # Every axis says nothing it judges is in scope. Each `n/a` is
            # individually fine, but APPROVE here would mean approving on zero
            # signal — the same "an undecided run is not a clean run" line the
            # panel-integrity check draws, and the one way `n/a` could become a
            # path to a free approval. Withhold and say so.
            event, reasons = NONE, [
                f"every axis ({', '.join(axes)}) reported n/a, so nothing was actually judged"]
        else:
            event = APPROVE
            judged = [a for a in axes if verdicts[a] != NA]
            reasons = [f"no red axis, {len(yellow)} yellow of at most {max_yellow}"
                       + (f"; n/a on {', '.join(na)} ({len(judged)} axes judged)" if na else "")]
    return {"event": event, "verdicts": verdicts, "axes": detail, "reasons": reasons}


VERDICT_KEY_RE = re.compile(r'"verdict"\s*:')


def extract_object(raw: str):
    """The ONE JSON object carrying a ``verdict`` in a model's raw reply.

    The reply may wrap it in prose or a code fence. Every top-level ``{...}``
    that parses is a candidate; zero or several with a ``verdict`` key raise
    ValueError — two answers in one reply is ambiguity, and this path fails
    closed on ambiguity rather than picking one.

    Two more ambiguities fail closed. A duplicate key ANYWHERE in the reply:
    the object holding it is rejected, so the scan would otherwise go on to
    find a ``verdict`` nested inside it. And a ``"verdict":`` key anywhere but
    in the one candidate: a wrapper that failed to parse (trailing comma, smart
    quote) is skipped one byte at a time, so the scan resumes INSIDE it and
    could take a nested verdict for the answer the wrapper plainly gives.
    """
    decoder = json.JSONDecoder(object_pairs_hook=_no_duplicate_keys)
    found, i = [], 0
    while True:
        i = raw.find("{", i)
        if i < 0:
            break
        try:
            obj, end = decoder.raw_decode(raw, i)
        except _DuplicateKeyError as e:
            raise ValueError(str(e)) from None
        except (ValueError, RecursionError):
            i += 1
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            found.append(obj)
        i = end
    if len(found) != 1:
        raise ValueError(f"expected exactly one JSON object with a verdict, found {len(found)}")
    keys = len(VERDICT_KEY_RE.findall(raw))
    if keys != 1:
        raise ValueError(f"expected exactly one \"verdict\" key in the reply, found {keys}")
    return found[0]


def cmd_extract(args) -> int:
    try:
        if not SHA_RE.fullmatch(args.commit_sha or ""):
            raise ValueError(f"--commit-sha must be a full commit SHA, got {args.commit_sha!r}")
        # Bytes, and one past the cap: a silent truncation could cut a second
        # verdict off and make an ambiguous reply look like a single answer.
        with open(args.raw, "rb") as f:
            data = f.read(MAX_RAW_BYTES + 1)
        if len(data) > MAX_RAW_BYTES:
            raise ValueError(f"the reply is larger than {MAX_RAW_BYTES} bytes")
        obj = extract_object(data.decode("utf-8", errors="replace"))
        # The artifact carries the clamped headline, so what is uploaded is
        # already within the cap rather than relying on decide to re-clamp it.
        obj["headline"] = validate_output(obj)[3]
    except (OSError, ValueError) as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    # The commit this axis job judged, from its own input — never the model's.
    obj["commit_sha"] = args.commit_sha
    text = json.dumps(obj)
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        print(f"::error::the output is larger than {MAX_OUTPUT_BYTES} bytes", file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return 0


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
    if args.commit_sha and not SHA_RE.fullmatch(args.commit_sha):
        print(f"::error::--commit-sha must be a full commit SHA, got {args.commit_sha!r}", file=sys.stderr)
        return 2
    outputs = {}
    for axis in axes:
        # --artifact-dirs: each axis is read ONLY from its own `axis-<axis>`
        # artifact directory (download-artifact without merge-multiple), so a
        # differently named artifact holding `<axis>.json` is never picked up.
        parts = (f"axis-{axis}", f"{axis}.json") if args.artifact_dirs else (f"{axis}.json",)
        try:
            outputs[axis] = load_output(os.path.join(args.outputs_dir, *parts), args.commit_sha)
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
    d.add_argument("--commit-sha", default="")
    d.add_argument("--artifact-dirs", action="store_true")
    x = sub.add_parser("extract")
    x.add_argument("--raw", required=True)
    x.add_argument("--out", required=True)
    x.add_argument("--commit-sha", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "extract":
        return cmd_extract(args)
    return cmd_render(args) if args.cmd == "render" else cmd_decide(args)


if __name__ == "__main__":
    sys.exit(main())
