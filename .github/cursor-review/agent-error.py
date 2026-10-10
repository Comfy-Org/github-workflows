#!/usr/bin/env python3
"""Why a direct-API reviewer cell failed, in a form a PUBLIC run log can carry.

Both direct lanes write their agent's output to a file and, until this script,
threw it away: the OpenAI lane redirects `codex exec --json` to
`codex-events.jsonl` and the Anthropic lane redirects `claude --output-format
json` to `claude-result.json`. Neither file was echoed or uploaded, and codex
reports API failures on STDOUT as events — so the log showed `Reading prompt
from stdin...`, `exit code 1`, and nothing else. A real outage
(`insufficient_quota` / `credit_balance_exhausted`, naming its own cause and
linking the billing page) was discarded, and the lane's failures had to be
inferred from job DURATION. That is what this fixes.

WHAT IT PRINTS, AND WHY SO LITTLE. These files hold the agent's reasoning, its
tool calls and its findings — model output steered by PR-authored text, which
`Report cell outcome` already refuses to echo for that reason. So this does not
dump them. It walks the JSON and emits, from objects that look like ERRORS, an
ALLOWLIST of short metadata fields (``ERROR_FIELDS``) plus a census of event
types. The allowlist is the safety property: `result`, `content`, `text`,
`arguments`, `reasoning` and friends are not in it, so assistant prose cannot
reach the log through here no matter how the provider nests it. The raw file is
deliberately NOT uploaded as an artifact either: the Anthropic lane screens
`findings.json` for a leaked API key before publishing it, and shipping the
unscreened transcript alongside would route the agent's own prose straight past
that guard. When the allowlist plus the event census is not enough to explain a
failure, widen the allowlist — do not publish the file.

Every emitted value is sanitized for a ``::workflow command::`` line the same
way the rest of this workflow does it — disallowed characters are REPLACED, not
dropped, because deleting would let a crafted "e r r o r" collapse into a value
that reads as something else.

DIAGNOSTIC ONLY: it exits 0 on anything, including a missing, truncated,
invalid-UTF-8 or non-JSON file. The step that runs it has already captured the
agent's real exit code; a traceback here would replace the diagnosis with a
crash, which is the failure this script exists to remove.

Usage:
    agent-error.py --events /tmp/codex-events.jsonl --label adversarial/gpt-5.6-sol
"""

import argparse
import json
import re
import sys

# Short, provider-authored metadata. Deliberately NOT `result`, `content`,
# `text`, `input`, `output`, `arguments`, `reasoning`, `delta` or `thinking`:
# those carry model output. Adding a key here widens what a public log can show,
# so it is a decision, not a convenience.
ERROR_FIELDS = ("type", "subtype", "code", "param", "status", "status_code", "http_status", "message")

# Bounds on what one invocation can print. A failing agent can emit a very
# large file, and this output goes to a log a human reads.
MAX_VALUE = 300
MAX_LINES = 40
MAX_BYTES = 8000
# Bound the walk: these files are agent-written, so depth and breadth are not
# ours to trust. A findings payload nests ~4 deep; 12 is slack, not a limit
# anyone legitimately reaches.
MAX_DEPTH = 12
MAX_NODES = 20000

_SAFE = re.compile(r"[^A-Za-z0-9 .,:;_/@+()\[\]{}='\"!?#$%^&*|~`<>-]")


def sanitize(value, limit: int = MAX_VALUE) -> str:
    """One line, safe to drop into a run log. Replaces, never deletes."""
    text = str(value)
    # Newlines first: a single value must not become two log lines, and must
    # not be able to open a `::workflow command::` of its own.
    text = text.replace("\r", " ").replace("\n", " ")
    text = _SAFE.sub("?", text)
    # `::` is what starts a workflow command; break the pair rather than drop a
    # character, so the text still reads.
    text = text.replace("::", ": :")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _looks_like_error(obj: dict) -> bool:
    """True when this object is reporting a failure rather than describing work."""
    if obj.get("is_error") is True:
        return True
    for key in ("type", "subtype", "code"):
        value = obj.get(key)
        if isinstance(value, str) and "error" in value.lower():
            return True
    for key in ("status", "status_code", "http_status"):
        value = obj.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 400:
            return True
    return False


def _fields(obj: dict) -> str:
    """The allowlisted fields of one error object, as `k=v` pairs."""
    parts = []
    for key in ERROR_FIELDS:
        if key not in obj:
            continue
        value = obj[key]
        # Only scalars. A dict or list under an allowlisted name (a `message`
        # holding content blocks, say) is NOT flattened — that is how model
        # prose would get in.
        if isinstance(value, (dict, list)):
            continue
        if value is None or value == "":
            continue
        parts.append(f"{key}={sanitize(value)}")
    return " ".join(parts)


def walk(node, out: list, types: dict, state: dict, depth: int = 0) -> None:
    """Collect error objects and a census of `type` values. Bounded."""
    if state["nodes"] >= MAX_NODES or depth > MAX_DEPTH:
        state["truncated"] = True
        return
    state["nodes"] += 1
    if isinstance(node, dict):
        kind = node.get("type")
        if isinstance(kind, str):
            key = sanitize(kind, 60)
            types[key] = types.get(key, 0) + 1
        if _looks_like_error(node):
            rendered = _fields(node)
            if rendered and rendered not in out:
                out.append(rendered)
        # An `error` KEY is the canonical envelope and must be read on its own
        # terms. The provider's error object does not have to describe ITSELF as
        # an error — OpenAI's is `{"error": {"type": "insufficient_quota",
        # "code": "credit_balance_exhausted", ...}}`, and neither field contains
        # the string "error", so `_looks_like_error` alone walked straight past
        # the one message worth printing and reported only the envelope's own
        # `type=error`. That is the exact failure this script was written for,
        # so it is handled explicitly rather than by heuristic.
        err = node.get("error")
        if isinstance(err, str) and err.strip():
            rendered = f"error={sanitize(err)}"
            if rendered not in out:
                out.append(rendered)
        elif isinstance(err, dict):
            rendered = _fields(err)
            if rendered and rendered not in out:
                out.append(rendered)
        for value in node.values():
            walk(value, out, types, state, depth + 1)
    elif isinstance(node, list):
        for value in node:
            walk(value, out, types, state, depth + 1)


def parse(text: str) -> list:
    """Whole-file JSON (claude) or newline-delimited events (codex), or [].

    Partial output is the normal case for a killed agent, so a file that does
    not parse as a whole is read line by line and the unparseable lines are
    skipped rather than failing the read.
    """
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return [json.loads(stripped)]
    except ValueError:
        pass
    nodes = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            nodes.append(json.loads(line))
        except ValueError:
            continue
    return nodes


def summarize(text: str) -> list:
    nodes = parse(text)
    out, types, state = [], {}, {"nodes": 0, "truncated": False}
    for node in nodes:
        walk(node, out, types, state)
    lines = []
    if types:
        census = ", ".join(f"{k}×{v}" for k, v in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))[:10])
        lines.append(f"events: {len(nodes)} ({census})")
    elif nodes:
        lines.append(f"events: {len(nodes)}")
    for item in out[:MAX_LINES]:
        lines.append(f"error: {item}")
    if len(out) > MAX_LINES:
        lines.append(f"… and {len(out) - MAX_LINES} more error object(s) (output bounded)")
    if state["truncated"]:
        lines.append("(walk bounded: the agent's output is larger than this summary reads)")
    if not lines:
        lines.append("no JSON events and no error object in the agent's output")
    elif not out:
        lines.append("no error object in the agent's output")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", required=True)
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)

    label = sanitize(args.label, 80) if args.label else ""
    head = f"=== why the cell failed ({label}) ===" if label else "=== why the cell failed ==="
    print(head)
    try:
        # Bounded read, errors="replace": these bytes are agent-written, so
        # invalid UTF-8 is reachable and must not raise.
        with open(args.events, "rb") as f:
            raw = f.read(4_000_000)
        text = raw.decode("utf-8", "replace")
    except OSError as e:
        print(f"could not read {sanitize(args.events, 120)}: {sanitize(type(e).__name__, 40)}")
        return 0
    body = "\n".join(summarize(text))
    if len(body.encode("utf-8")) > MAX_BYTES:
        body = body.encode("utf-8")[:MAX_BYTES].decode("utf-8", "ignore") + "\n… (bounded)"
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
