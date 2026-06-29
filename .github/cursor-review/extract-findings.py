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
    caller parses each and takes the first that loads.
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
    """Extract a JSON value (array or object) from raw model output.

    Tolerates surrounding prose and markdown fences. Returns the parsed value
    (list or dict), or None if no JSON could be located. Layered most- to
    least-strict so a clean response takes the fast path:

    1. The whole output is JSON.
    2. A fenced ```json (or bare ```) block holds the JSON.
    3. A balanced {...}/[...] region is embedded in prose.
    """
    text = raw_text.strip()

    parsed = _try_load(text)
    if parsed is not None:
        return parsed

    for match in re.finditer(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL):
        parsed = _try_load(match.group(1).strip())
        if parsed is not None:
            return parsed

    for candidate in _iter_json_candidates(text):
        parsed = _try_load(candidate)
        if parsed is not None:
            return parsed

    return None


def coerce_findings_list(parsed):
    """Reduce a parsed JSON value to the findings list, or None if it isn't one.

    The panel cells and judge are all asked for a bare JSON array, but a model
    intermittently wraps it as `{"findings": [...]}` (or a near-synonym key).
    Unwrap those so a well-formed-but-wrapped response parses instead of being
    discarded as a parse_error.
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("findings", "results", "items", "reviews"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", required=True, help="Path to raw cursor-agent output")
    parser.add_argument("--out", required=True, help="Path to write the findings JSON file")
    parser.add_argument("--model", required=True)
    parser.add_argument("--review-type", required=True)
    args = parser.parse_args()

    record = {"model": args.model, "review_type": args.review_type}

    try:
        with open(args.raw, encoding="utf-8") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError) as e:
        record.update(status="error", error=f"Could not read raw output: {e}", findings=[])
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
