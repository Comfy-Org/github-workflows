#!/usr/bin/env python3
"""Parse raw cursor-agent output into a normalized findings record.

Used by per-cell matrix steps. Each cell calls this to convert the model's
raw stdout into a JSON file the consolidate step can ingest. The output is
always structured — even on parse failures or empty output — so the
consolidate step has a uniform input.

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


def parse_json_findings(raw_text: str):
    """Extract a JSON array from raw model output, tolerating surrounding prose.

    Returns the parsed value, or None if no JSON array could be located.
    """
    text = raw_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

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

    findings = parse_json_findings(raw)

    if findings is None:
        # Truncate raw so artifacts stay small even on chatty parse failures.
        record.update(
            status="parse_error",
            error=f"Could not parse JSON findings from output. First 500 chars:\n{raw[:500]}",
            findings=[],
        )
    elif not isinstance(findings, list):
        record.update(
            status="parse_error",
            error=f"Output parsed but is not an array (got {type(findings).__name__}).",
            findings=[],
        )
    else:
        record.update(status="ok", findings=findings)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(record, f)


if __name__ == "__main__":
    main()
