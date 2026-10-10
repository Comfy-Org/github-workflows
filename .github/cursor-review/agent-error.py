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

WHAT IT PRINTS, AND WHY SO LITTLE. These files also hold the agent's reasoning,
its tool calls and its findings — model output steered by PR-authored text,
which `Report cell outcome` already refuses to echo for that reason. So this
does not dump them. It prints an ALLOWLIST of short metadata fields
(``ERROR_FIELDS``) from objects that look like errors, plus a census of event
types.

ONE field is read out of model-written territory — Claude Code's API-failure
cause, which sits in `result` and nowhere else — under the gates in
``_api_error_cause``. Everything below describes the rule that field is the
stated exception to.

TWO allowlists, and the second one is the load-bearing one. Restricting the
FIELDS is not enough on its own, because a reviewer agent writes part of this
file: it chooses the `arguments` of its own MCP tool calls. A prompt-injected
agent could therefore plant `{"error": {"message": "<anything>"}}` in a tool
call, and a walk that descended into every value would dutifully print it —
putting attacker-chosen text in the log and contradicting the whole point of
withholding the file. So DESCENT is an allowlist too (``ENVELOPE_KEYS``): from
an event object this visits only the keys a CLI writes around the model's
output, never into `arguments`, `result`, `content` or `text`. The model's
writable surfaces are not denied one by one — they are simply never reached.
That is deliberately narrower than "find any error anywhere": a provider shape
nesting its error somewhere unanticipated reports only the event census, which
is the safe direction to be wrong in. Widen ``ENVELOPE_KEYS`` to a key a CLI
owns, never to one the model fills.

Every emitted value is sanitized for a run-log line: disallowed characters are
REPLACED, not dropped, because deleting would let a crafted "e r r o r"
collapse into a value that reads as something else. Both the modern `::` and
the legacy `##[command]` forms are broken, each with a single-pass regex that
provably leaves no `::` or `##[` behind (`"a:::b"` defeats a lone
`str.replace`, which does not rescan what it just wrote).

DIAGNOSTIC ONLY: it exits 0 on anything, including a missing, truncated,
invalid-UTF-8 or non-JSON file, and including a payload that makes the JSON
decoder raise — `json.loads` raises `RecursionError` (a RuntimeError, NOT a
ValueError) on deeply nested input, and 400 KB of `[` is enough. The step that
runs it has already captured the agent's real exit code; a traceback here would
replace the diagnosis with a crash, and under the step's `bash -e` it would
abort `Report cell outcome` before the status line, turning an advisory leg red.

Usage:
    agent-error.py --events /tmp/codex-events.jsonl --label adversarial/gpt-5.6-sol
"""

import argparse
import json
import os
import re
import sys

# Short, provider-authored metadata. Deliberately NOT `result`, `content`,
# `text`, `input`, `output`, `arguments`, `reasoning`, `delta` or `thinking`:
# those carry model output. Adding a key here widens what a public log can show,
# so it is a decision, not a convenience.
ERROR_FIELDS = (
    "type", "subtype", "is_error", "code", "param",
    "status", "status_code", "http_status", "message",
    # Claude Code's own scalars on a failed result object, beside the
    # model-written `result`: the HTTP status and `api_error` respectively.
    "api_error_status", "terminal_reason",
)

# The ONLY keys this descends through — the envelope a CLI writes around the
# model's output. `item` carries codex's per-event payload (its `type` is what
# the census counts); `error`, `last_error`, `response`, `data` and `detail` are
# where the known providers put a failure. Every key a model fills is absent by
# construction, which is the point: see the module docstring.
ENVELOPE_KEYS = ("error", "last_error", "item", "response", "data", "detail")

# Bounds on what one invocation can print. A failing agent can emit a very
# large file, and this output goes to a log a human reads.
MAX_VALUE = 300
MAX_LINES = 40
MAX_BYTES = 8000
# Read at most this much, from the END of the file (see `read_tail`).
MAX_READ = 4_000_000
# Bound the walk: these files are agent-written, so depth and breadth are not
# ours to trust. A findings payload nests ~4 deep; 12 is slack, not a limit
# anyone legitimately reaches.
MAX_DEPTH = 12
MAX_NODES = 20000

# Claude Code's API-failure cause sits in `result`, a field the model also
# writes. Printing it is THE one exception to "no model-written field reaches
# the log" — see `_api_error_cause`, which gates it.
CAUSE_FIELD = "result"
CAUSE_LIMIT = 120
# `[0-9]`, not `\d`: in a str pattern `\d` matches any Unicode digit.
_API_ERROR = re.compile(r"^API Error: [0-9]{3} ")
# An Anthropic key's prefix, and the window of the REAL key (from the step's
# env) that a cause may not contain. A window rather than the whole key: the
# cut to CAUSE_LIMIT leaves a truncated key, which Actions' exact-value mask
# no longer matches.
_KEY_PREFIX = "sk-ant-"
KEY_WINDOW = 16

_SAFE = re.compile(r"[^A-Za-z0-9 .,:;_/@+()\[\]{}='\"!?#$%^&*|~`<>-]")
# A colon followed by a colon, and a `#` that begins a `##[`. Lookahead, so one
# pass breaks every run however long: `":::"` -> `": : :"`, `"###["` -> `"## #["`.
_COLON_RUN = re.compile(r":(?=:)")
_LEGACY_CMD = re.compile(r"#(?=#\[)")


def sanitize(value, limit: int = MAX_VALUE) -> str:
    """One line, safe to drop into a run log. Replaces, never deletes."""
    text = str(value)
    # Newlines first: a single value must not become two log lines, and must
    # not be able to open a workflow command of its own.
    text = text.replace("\r", " ").replace("\n", " ")
    text = _SAFE.sub("?", text)
    # Break both command forms rather than drop a character, so the text still
    # reads. `str.replace` alone is not enough — it does not rescan its own
    # output, so `":::"` would come back as `": ::"`.
    text = _COLON_RUN.sub(": ", text)
    text = _LEGACY_CMD.sub("# ", text)
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


def _api_error_cause(obj: dict, top: bool = False) -> str:
    """Claude Code's API-failure cause, read out of a model-written field.

    THE one exception to the rule that no model-written field reaches the log,
    and it is deliberate rather than an oversight. Claude Code reports an
    API-level failure as `{"type":"result","subtype":"success","is_error":true,
    "result":"API Error: 400 ..."}` — note `subtype`, which reads as SUCCESS —
    and the cause appears nowhere else in the file. Since the file is not
    uploaded either, an Anthropic-lane outage was otherwise undiagnosable
    without re-running the cell.

    So it is gated:

    1. the object is the CLI's own top-level `{"type":"result"}` object with
       `is_error` true — never a nested or codex-lane object (``walk`` passes
       ``top``), where the shape is not the one this exception covers;
    2. the CLI marked it an API failure: a numeric `api_error_status` (a CLI
       scalar the model cannot write — `Invalid API key · Fix external API
       key` with status 401 is a real 2.1.286 failure, and carries no
       preamble), or a string opening with `API Error: <status> `;
    3. it holds no Anthropic key: not the `sk-ant-` prefix and no
       ``KEY_WINDOW``-char slice of the step's own ``ANTHROPIC_API_KEY`` —
       the screen `findings.json` gets before publishing, which this path
       would otherwise skip;
    4. it is cut to ``CAUSE_LIMIT`` and sanitized like every other value.

    A prompt-injected agent CAN forge the preamble, so this is a BOUNDED and
    documented channel, not a safe one — roughly 120 sanitized characters
    that cannot carry a workflow command or the key, but can carry other
    text. That trade was taken knowingly (#402); the alternative was a lane
    that reports `subtype=success` and names nothing.

    A failure with neither marker yields nothing rather than a guess, which is
    the safe direction: widen the gate only against a real message, never
    speculatively.
    """
    if not top or obj.get("type") != "result" or obj.get("is_error") is not True:
        return ""
    value = obj.get(CAUSE_FIELD)
    if not isinstance(value, str):
        return ""
    status = obj.get("api_error_status")
    cli_marked = isinstance(status, int) and not isinstance(status, bool)
    if not cli_marked and not _API_ERROR.match(value):
        return ""
    if _holds_key(value):
        return f"{CAUSE_FIELD}=(withheld: it contains an Anthropic API key or part of one)"
    return f"{CAUSE_FIELD}={sanitize(value, CAUSE_LIMIT)}"


def _holds_key(value: str) -> bool:
    """True when ``value`` carries the key prefix or a slice of the real key."""
    if _KEY_PREFIX in value:
        return True
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return False
    if len(key) <= KEY_WINDOW:
        return key in value
    return any(
        key[i : i + KEY_WINDOW] in value for i in range(len(key) - KEY_WINDOW + 1)
    )


def walk(node, out: list, types: dict, state: dict, depth: int = 0) -> None:
    """Collect error objects and a census of `type` values. Bounded.

    Descends only through ``ENVELOPE_KEYS``, so the model's own writable
    subtrees are never visited.
    """
    if state["nodes"] >= MAX_NODES or depth > MAX_DEPTH:
        state["truncated"] = True
        return
    state["nodes"] += 1
    if isinstance(node, list):
        for value in node:
            walk(value, out, types, state, depth + 1)
        return
    if not isinstance(node, dict):
        return

    kind = node.get("type")
    if isinstance(kind, str):
        key = sanitize(kind, 60)
        types[key] = types.get(key, 0) + 1
    if _looks_like_error(node):
        rendered = _fields(node)
        if rendered and rendered not in out:
            out.append(rendered)
        cause = _api_error_cause(node, top=depth == 0)
        if cause and cause not in out:
            out.append(cause)
    # An `error` KEY is the canonical envelope and must be read on its own
    # terms. The provider's error object does not have to describe ITSELF as an
    # error — OpenAI's is `{"error": {"type": "insufficient_quota", "code":
    # "credit_balance_exhausted", ...}}`, and neither field contains the string
    # "error", so `_looks_like_error` alone walked straight past the one
    # message worth printing and reported only the envelope's own `type=error`.
    err = node.get("error")
    if isinstance(err, str) and err.strip():
        rendered = f"error={sanitize(err)}"
        if rendered not in out:
            out.append(rendered)
    elif isinstance(err, dict):
        rendered = _fields(err)
        if rendered and rendered not in out:
            out.append(rendered)

    for key in ENVELOPE_KEYS:
        if key in node:
            walk(node[key], out, types, state, depth + 1)


def _loads(text):
    """`json.loads` that cannot raise. Deep nesting raises RecursionError."""
    try:
        return True, json.loads(text)
    except (ValueError, RecursionError):
        return False, None


def parse(text: str, drop_first_line: bool = False) -> list:
    """Whole-file JSON (claude) or newline-delimited events (codex), or [].

    Partial output is the normal case for a killed agent, so a file that does
    not parse as a whole is read line by line and the unparseable lines are
    skipped rather than failing the read. `drop_first_line` discards a line the
    tail read cut in half.
    """
    stripped = text.strip()
    if not stripped:
        return []
    ok, value = _loads(stripped)
    if ok:
        return [value]
    lines = stripped.splitlines()
    if drop_first_line and lines:
        lines = lines[1:]
    nodes = []
    for line in lines:
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        ok, value = _loads(line)
        if ok:
            nodes.append(value)
    return nodes


def read_tail(path: str):
    """The LAST ``MAX_READ`` bytes, as text, plus whether the head was cut.

    The tail, not the head: the event that explains a failure (a terminal
    `error`, a `turn.failed`) is the LAST thing an agent writes, so reading
    forward from byte 0 of a long run drops exactly the event worth having.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - MAX_READ)
        f.seek(start)
        raw = f.read(MAX_READ)
    # Agent-written bytes, so invalid UTF-8 is reachable and must not raise.
    return raw.decode("utf-8", "replace"), start > 0


def summarize(text: str, head_cut: bool = False) -> list:
    nodes = parse(text, drop_first_line=head_cut)
    out, types, state = [], {}, {"nodes": 0, "truncated": False}
    # Newest first: if the node budget runs out, spend it on the events nearest
    # the failure rather than on the session's opening handshake.
    for node in reversed(nodes):
        walk(node, out, types, state)
    lines = []
    if head_cut:
        lines.append(
            f"(read the last {MAX_READ // 1_000_000} MB of a larger file)"
        )
    if types:
        census = ", ".join(
            f"{k}×{v}"
            for k, v in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        )
        lines.append(f"events: {len(nodes)} ({census})")
    elif nodes:
        lines.append(f"events: {len(nodes)}")
    for item in out[:MAX_LINES]:
        lines.append(f"error: {item}")
    if len(out) > MAX_LINES:
        lines.append(f"… and {len(out) - MAX_LINES} more error object(s) (output bounded)")
    if state["truncated"]:
        lines.append("(walk bounded: the agent's output is larger than this summary reads)")
    if not lines or (head_cut and len(lines) == 1):
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
        text, head_cut = read_tail(args.events)
    except OSError as e:
        print(f"could not read {sanitize(args.events, 120)}: {sanitize(type(e).__name__, 40)}")
        return 0
    try:
        body = "\n".join(summarize(text, head_cut=head_cut))
    except Exception as e:  # noqa: BLE001
        # Belt and braces over the bounds above. This runs after the cell has
        # already failed; whatever goes wrong in here, the step must still
        # reach its status line.
        print(f"could not summarize the agent's output: {sanitize(type(e).__name__, 40)}")
        return 0
    if len(body.encode("utf-8")) > MAX_BYTES:
        body = body.encode("utf-8")[:MAX_BYTES].decode("utf-8", "ignore") + "\n… (bounded)"
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
