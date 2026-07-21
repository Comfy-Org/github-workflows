#!/usr/bin/env python3
"""Parse raw cursor-agent output into a normalized findings record.

Used by per-cell matrix steps AND by the judge/consolidate step. Each caller
converts the model's raw stdout into a JSON file the next step can ingest. The
output is always structured — even on parse failures or empty output — so the
downstream step has a uniform input.

Output shape:
    {
        "model": str,
        "review_type": str,
        "status": "ok" | "empty" | "error" | "parse_error",
        "findings": [{"file": str, "line": int, "side": "RIGHT", "body": str}, ...],
        "error": str  # only when status != "ok"
    }
"""

import argparse
import json
import re


def _try_load(snippet: str):
    """json.loads `snippet`, returning the value only if it's a list or dict.

    A bare number/string/bool is never a findings payload, so reject it — this
    keeps the brace-scan below from "succeeding" on a stray scalar.
    """
    try:
        value = json.loads(snippet)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, (list, dict)) else None


def _iter_json_candidates(text: str):
    """Yield each top-level balanced {...} / [...] region embedded in `text`.

    String- and escape-aware: braces or brackets inside JSON string literals
    don't throw off the nesting count, so prose like `... the findings […] are`
    surrounding a real array doesn't corrupt the match the way a naive
    first-`[`/last-`]` slice does. Regions are yielded in document order; the
    caller parses each and keeps the last that is findings-shaped.
    """
    openers = {"{", "["}
    closers = {"}", "]"}
    i, n = 0, len(text)
    while i < n:
        if text[i] not in openers:
            i += 1
            continue
        depth = 0
        in_str = False
        escape = False
        j = i
        while j < n:
            c = text[j]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c in openers:
                depth += 1
            elif c in closers:
                depth -= 1
                if depth == 0:
                    yield text[i : j + 1]
                    break
            j += 1
        # Resume scanning after this region (or after an unbalanced opener).
        i = j + 1


def parse_json_findings(raw_text: str):
    """Extract the findings JSON value from raw model output.

    Tolerates surrounding prose and markdown fences. Returns the parsed value
    (a findings array, or a `{"findings": [...]}` wrapper), or None if no
    findings-shaped JSON could be located.

    Crucially this scans for a *findings-shaped* region, not merely the first
    thing that parses as JSON, and prefers the LAST such region. The judge
    (esp. on verification-heavy diffs, BE-3160) opens with prose that quotes
    individual finding OBJECTS or scalar lists inline while reasoning, then
    emits the real array LAST. Taking the first parseable region there yields
    an un-coercible object (→ spurious parse_error) or a bogus scalar list,
    while the genuine findings array sits further down. Layered so a clean
    response still takes the fast path:

    1. The whole output is the findings JSON.
    2. A fenced ```json (or bare ```) block holds it — last valid block wins.
    3. A balanced {...}/[...] region embedded in prose — last valid wins.
    """
    text = raw_text.strip()

    # Fast path: the whole response is the findings payload.
    whole = _try_load(text)
    if coerce_findings_list(whole) is not None:
        return whole

    # Fenced blocks: prose/verification precedes the answer, so the last
    # findings-shaped fence is the real one.
    best = None
    for match in re.finditer(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL):
        parsed = _try_load(match.group(1).strip())
        if coerce_findings_list(parsed) is not None:
            best = parsed
    if best is not None:
        return best

    # Bare balanced regions embedded in prose: keep the LAST findings-shaped
    # one so an inline finding object / scalar list quoted mid-reasoning never
    # shadows the real array that follows it.
    for candidate in _iter_json_candidates(text):
        parsed = _try_load(candidate)
        if coerce_findings_list(parsed) is not None:
            best = parsed
    return best


def parse_exit_code(value):
    """Coerce the --exit-code argument to an int, or None if unknown.

    The workflow passes the captured cursor-agent exit status through as a
    string that may be blank (the run step didn't record one) or absent, so
    treat anything non-integer as "unknown" rather than erroring.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


# A delisted / unavailable model makes cursor-agent print this to stderr and
# exit non-zero with zero bytes on stdout. Matching it lets us tag the cell as
# a loud `error` instead of an `empty` that reads as "ran and found nothing".
_MODEL_UNAVAILABLE_RE = re.compile(r"Cannot use this model:.*", re.IGNORECASE)


def classify_run_error(exit_code, stderr_text, raw):
    """Return an error message if the cursor-agent call clearly failed, else None.

    Two signals:

    * stderr names an unusable model (`Cannot use this model: <id>`) — this is
      definitive (the model never ran), so it wins even when stdout has content.
    * a non-zero exit code AND empty stdout — the call failed and produced
      nothing, which the caller would otherwise misread as an `empty`
      (found-nothing) cell. A non-zero exit that still yielded parseable
      findings is left to the normal parse path so real findings are never
      discarded.
    """
    stderr_text = stderr_text or ""
    match = _MODEL_UNAVAILABLE_RE.search(stderr_text)
    if match:
        return match.group(0).strip()

    if exit_code not in (None, 0) and not (raw or "").strip():
        msg = f"cursor-agent exited with status {exit_code} and produced no output."
        tail = [line.strip() for line in stderr_text.splitlines() if line.strip()]
        if tail:
            msg += f" Last stderr: {tail[-1]}"
        return msg

    return None


def coerce_findings_list(parsed):
    """Reduce a parsed JSON value to the findings list, or None if it isn't one.

    A findings list is a JSON array of finding OBJECTS (an empty array is
    allowed — "no findings"), or an object wrapping such an array under a
    findings-like key. The panel cells and judge are asked for a bare JSON
    array, but a model intermittently wraps it as `{"findings": [...]}` (or a
    near-synonym key); unwrap those so a well-formed-but-wrapped response
    parses instead of being discarded as a parse_error.

    Requiring the elements to be objects is what lets the extractor above skip
    a scalar list the judge quotes in prose (e.g. `["contains", "startswith"]`
    while narrating jq builtins) and keep scanning for the real findings array.
    """
    if isinstance(parsed, list):
        return parsed if all(isinstance(item, dict) for item in parsed) else None
    if isinstance(parsed, dict):
        for key in ("findings", "results", "items", "reviews"):
            value = parsed.get(key)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", required=True, help="Path to raw cursor-agent output")
    parser.add_argument("--out", required=True, help="Path to write the findings JSON file")
    parser.add_argument("--model", required=True)
    parser.add_argument("--review-type", required=True)
    parser.add_argument(
        "--exit-code",
        default=None,
        help="cursor-agent process exit status (blank/absent = unknown).",
    )
    parser.add_argument(
        "--stderr",
        default=None,
        help="Path to the cursor-agent stderr capture, used to classify run errors.",
    )
    args = parser.parse_args()

    record = {"model": args.model, "review_type": args.review_type}

    stderr_text = ""
    if args.stderr:
        try:
            with open(args.stderr, encoding="utf-8", errors="replace") as f:
                stderr_text = f.read()
        except OSError:
            stderr_text = ""

    try:
        with open(args.raw, encoding="utf-8") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError) as e:
        record.update(status="error", error=f"Could not read raw output: {e}", findings=[])
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(record, f)
        return

    # Defense-in-depth against silent catalog drift: a delisted/unavailable
    # model exits non-zero with a "Cannot use this model: <id>" stderr and no
    # stdout. Tag that as a loud `error` (which post-review.py reports as
    # `(error)` and counts as a failed cell) rather than an `empty` that is
    # indistinguishable from "the model ran and found nothing".
    run_error = classify_run_error(parse_exit_code(args.exit_code), stderr_text, raw)
    if run_error is not None:
        record.update(status="error", error=run_error, findings=[])
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(record, f)
        return

    if not raw.strip():
        record.update(status="empty", error="Cursor agent produced empty output.", findings=[])
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(record, f)
        return

    findings = coerce_findings_list(parse_json_findings(raw))

    if findings is None:
        # Truncate raw so artifacts stay small even on chatty parse failures.
        record.update(
            status="parse_error",
            error=f"Could not parse JSON findings from output. First 500 chars:\n{raw[:500]}",
            findings=[],
        )
    else:
        record.update(status="ok", findings=findings)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(record, f)


if __name__ == "__main__":
    main()
