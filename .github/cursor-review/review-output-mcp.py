#!/usr/bin/env python3
"""Minimal stdio MCP server for durable Cursor Review output."""

import argparse
import json
import os
import posixpath
import sys
import tempfile

SEVERITIES = ("critical", "high", "medium", "low", "nit")
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}

FINDING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["file", "line", "side", "severity", "body"],
    "properties": {
        "file": {"type": "string", "minLength": 1, "maxLength": 1024},
        "line": {"type": "integer", "minimum": 1},
        "side": {"type": "string", "enum": ["RIGHT"]},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "body": {"type": "string", "minLength": 1, "maxLength": 20000},
    },
}
JUDGE_FINDING_SCHEMA = {
    **FINDING_SCHEMA,
    "properties": {
        **FINDING_SCHEMA["properties"],
        "repeat_of": {"type": "string", "minLength": 1, "maxLength": 2048},
        "repeat_round": {"type": "integer", "minimum": 1},
    },
    "dependentRequired": {
        "repeat_of": ["repeat_round"],
        "repeat_round": ["repeat_of"],
    },
}


def write_record(path, record):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(dir=directory, prefix=".review-output-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(record, output)
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def initial_record(args):
    record = {
        "status": "error",
        "error": "Cursor agent did not submit structured review output.",
        "findings": [],
    }
    if args.model:
        record["model"] = args.model
    if args.review_type:
        record["review_type"] = args.review_type
    return record


def read_record(args):
    try:
        with open(args.out, encoding="utf-8") as source:
            record = json.load(source)
            return record if isinstance(record, dict) else initial_record(args)
    except (OSError, json.JSONDecodeError):
        return initial_record(args)


def validate_finding(value, allow_repeat=False):
    if not isinstance(value, dict):
        raise ValueError("finding must be an object")
    base_keys = {"file", "line", "side", "severity", "body"}
    repeat_keys = {"repeat_of", "repeat_round"}
    valid_keys = (base_keys, base_keys | repeat_keys) if allow_repeat else (base_keys,)
    if set(value) not in valid_keys:
        choices = " or ".join(str(sorted(keys)) for keys in valid_keys)
        raise ValueError(f"finding keys must be exactly {choices}")

    path = value["file"]
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise ValueError("file must be a non-empty string of at most 1024 characters")
    if path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError("file must be a repo-relative POSIX path")
    normalized = posixpath.normpath(path)
    if normalized in (".", "..") or normalized.startswith("../"):
        raise ValueError("file must stay inside the repository")

    line = value["line"]
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise ValueError("line must be a positive integer")
    if value["side"] != "RIGHT":
        raise ValueError("side must be RIGHT")
    if value["severity"] not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    body = value["body"]
    if not isinstance(body, str) or not body or len(body) > 20000:
        raise ValueError("body must be a non-empty string of at most 20000 characters")
    if "repeat_of" in value:
        repeat_of = value["repeat_of"]
        if not isinstance(repeat_of, str) or not repeat_of or len(repeat_of) > 2048:
            raise ValueError("repeat_of must be a non-empty string of at most 2048 characters")
        repeat_round = value["repeat_round"]
        if isinstance(repeat_round, bool) or not isinstance(repeat_round, int) or repeat_round < 1:
            raise ValueError("repeat_round must be a positive integer")

    return {**value, "file": normalized}


def tools_for(mode):
    if mode == "reviewer":
        return [
            {
                "name": "cursor_review_record_finding",
                "description": (
                    "Record one code-review finding. Call once per distinct issue. "
                    "Only findings recorded with this tool count."
                ),
                "inputSchema": FINDING_SCHEMA,
            },
            {
                "name": "cursor_review_finish",
                "description": (
                    "Finish this review after recording every finding. Call exactly once, "
                    "including when there are no findings."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
            },
        ]
    return [
        {
            "name": "cursor_review_submit_final",
            "description": (
                "Submit the final adjudicated review. Call exactly once with every finding "
                "that should be posted, or an empty findings array. This tool is the only "
                "channel used for the final result."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["findings"],
                "properties": {
                    "findings": {
                        "type": "array",
                        "maxItems": 10,
                        "items": JUDGE_FINDING_SCHEMA,
                    }
                },
            },
        }
    ]


def call_tool(args, name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")

    if args.mode == "reviewer" and name == "cursor_review_record_finding":
        finding = validate_finding(arguments)
        record = read_record(args)
        if record.get("status") == "ok":
            raise ValueError("review is already finished")
        findings = record.get("findings")
        if not isinstance(findings, list):
            findings = []
        record.update(
            status="error",
            error="Cursor agent recorded findings but did not finish the review.",
            findings=[*findings, finding],
        )
        write_record(args.out, record)
        return f"Recorded {finding['severity']} finding on {finding['file']}:{finding['line']}."

    if args.mode == "reviewer" and name == "cursor_review_finish":
        if arguments:
            raise ValueError("cursor_review_finish takes no arguments")
        record = read_record(args)
        if record.get("status") == "ok":
            raise ValueError("review is already finished")
        findings = record.get("findings")
        record.update(status="ok", findings=findings if isinstance(findings, list) else [])
        record.pop("error", None)
        write_record(args.out, record)
        return f"Finished review with {len(record['findings'])} finding(s)."

    if args.mode == "judge" and name == "cursor_review_submit_final":
        if read_record(args).get("status") == "ok":
            raise ValueError("final review is already submitted")
        if set(arguments) != {"findings"} or not isinstance(arguments["findings"], list):
            raise ValueError("findings must be an array and the only argument")
        if len(arguments["findings"]) > 10:
            raise ValueError("final review is capped at 10 findings")
        findings = [
            validate_finding(finding, allow_repeat=True)
            for finding in arguments["findings"]
        ]
        record = initial_record(args)
        record.update(status="ok", findings=findings)
        record.pop("error", None)
        write_record(args.out, record)
        return f"Submitted final review with {len(findings)} finding(s)."

    raise ValueError(f"unknown tool for {args.mode} mode: {name}")


def result(request_id, value):
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(args, message):
    request_id = message.get("id")
    method = message.get("method")
    if method == "initialize":
        params = message.get("params") or {}
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        return result(request_id, {
            "protocolVersion": (
                requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            ),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "cursor-review-output", "version": "1.0.0"},
        })
    if method == "ping":
        return result(request_id, {})
    if method == "tools/list":
        return result(request_id, {"tools": tools_for(args.mode)})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            text = call_tool(args, params.get("name"), params.get("arguments", {}))
            return result(request_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return result(request_id, {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            })
    if method and method.startswith("notifications/"):
        return None
    return error(request_id, -32601, f"unknown method: {method}")


def serve(args):
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("request must be an object")
            response = handle(args, message)
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            response = error(None, -32700, str(exc))
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("reviewer", "judge"), required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model")
    parser.add_argument("--review-type")
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    if args.init:
        write_record(args.out, initial_record(args))
        return
    serve(args)


if __name__ == "__main__":
    main()
