#!/usr/bin/env python3
"""Read-only stdio MCP proxy: Linear, Notion and Slack context for cursor-approve.

The business, design and completeness axes need company context, but the agent
reading it must never hold a credential. So the agent gets this process as its
only MCP server; the process holds the tokens and exposes six read tools.

    context-proxy.py --token-file tokens.json --enable linear,notion,slack --log calls.jsonl

Token handling:

* ``--token-file`` is JSON with optional keys ``linear``, ``notion``, ``slack``.
  It is read once at startup and then deleted (even when it does not parse). A
  missing or empty key disables that source, whatever ``--enable`` says.
* A token never enters ``os.environ`` and never reaches a child process: an
  environment block stays readable through ``/proc/<pid>/environ``. This file
  starts no child processes at all.

Network boundary: every outbound HTTP call goes through ``_guarded_request``,
which runs ``check_request`` FIRST and raises ``GuardError`` before anything is
sent unless the call is on this allow-list:

* Notion (``api.notion.com``): ``POST /v1/search``, ``GET /v1/pages/<id>``,
  ``GET /v1/blocks/<id>/children``. The Notion token can write; this guard is
  what keeps it read-only.
* Linear (``api.linear.app``): ``POST /graphql`` whose ``query`` is one of the
  constant read queries below, a single ``query`` operation with no mutation or
  subscription. The agent's input goes only into GraphQL ``variables``.
* Slack (``slack.com``): ``GET /api/conversations.list`` and
  ``GET /api/conversations.history``.

The guard also attaches the token itself, picked by the request's source, so a
token can only ever travel to its own host; and it refuses redirects, which
urllib would otherwise follow with the Authorization header attached.
``tests/test_context_proxy.py`` parses this file and fails if any
``urllib.request`` / ``http.client`` use sits outside ``_guarded_request``.

Slack: the bot token has only ``channels:read`` + ``channels:history``, and bots
cannot call ``search.messages``. So a background thread started at startup
loads the last 30 days of top-level messages in every public channel the bot is
a member of, and both Slack tools search that cache (case-insensitive substring,
newest first). Thread replies are not loaded.

Limits: at most 40 tool calls per process; each response is truncated to 20 KB
and all responses together to 1 MB. Past a limit the tool returns an error
instead of data. Every tool call is logged to ``--log`` as one JSON line:
tool, arguments, response bytes, error. Tokens are never logged.

Stdlib only; Python 3.11+.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SOURCES = ("linear", "notion", "slack")
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}

MAX_TOOL_CALLS = 40
MAX_RESPONSE_BYTES = 20 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_HTTP_BYTES = 8 * 1024 * 1024
HTTP_TIMEOUT = 30
TRUNCATION_MARK = "\n[truncated]"

NOTION_VERSION = "2022-06-28"
NOTION_CHILD_FETCHES = 25
SLACK_DAYS = 30
SLACK_LIST_PAGES = 20
SLACK_HISTORY_PAGES = 10
SLACK_RESULTS = 200

HOSTS = {
    "linear": "api.linear.app",
    "notion": "api.notion.com",
    "slack": "slack.com",
}
_HEX32 = r"[0-9a-f]{32}"
NOTION_ROUTES = (
    ("POST", re.compile(r"/v1/search"), ()),
    ("GET", re.compile(rf"/v1/pages/{_HEX32}"), ()),
    ("GET", re.compile(rf"/v1/blocks/{_HEX32}/children"), ("page_size", "start_cursor")),
)
SLACK_ROUTES = {
    "/api/conversations.list": ("types", "exclude_archived", "limit", "cursor"),
    "/api/conversations.history": ("channel", "oldest", "limit", "cursor"),
}

# The ONLY GraphQL documents ever sent to Linear. Agent input reaches them
# through `variables`, never by formatting into this text.
LINEAR_QUERIES = {
    "search": """query ContextSearch($term: String!) {
  searchIssues(term: $term, first: 10) {
    nodes { identifier title url state { name } project { name } }
  }
}""",
    "issue": """query ContextIssue($id: String!) {
  issue(id: $id) {
    identifier title description url
    state { name }
    project { name description }
    parent { identifier title }
    comments(first: 50) { nodes { body createdAt user { name } } }
  }
}""",
}


class GuardError(Exception):
    """An outbound request is not on the read-only allow-list."""


class ToolError(Exception):
    """A tool call failed; the message is returned to the agent."""


def _graphql_tokens(document):
    """Names in a GraphQL document, with strings and comments removed."""
    without_blocks = re.sub(r'"""(?:\\.|[^\\])*?"""', " ", document, flags=re.S)
    without_strings = re.sub(r'"(?:\\.|[^"\\\n])*"', " ", without_blocks)
    without_comments = re.sub(r"#[^\n]*", " ", without_strings)
    return re.findall(r"[_A-Za-z][_0-9A-Za-z]*", without_comments)


def check_linear_body(body):
    if not isinstance(body, dict) or not set(body) <= {"query", "variables", "operationName"}:
        raise GuardError("linear body must be an object of query/variables/operationName")
    document = body.get("query")
    if not isinstance(document, str):
        raise GuardError("linear body has no query string")
    names = _graphql_tokens(document)
    if not names or names[0] != "query":
        raise GuardError("linear request must be a query operation")
    if {"mutation", "subscription"} & set(names):
        raise GuardError("linear request contains a mutation or subscription")
    if document not in LINEAR_QUERIES.values():
        raise GuardError("linear query is not one of the proxy's constant queries")
    if not isinstance(body.get("variables", {}), dict):
        raise GuardError("linear variables must be an object")


def check_request(source, method, url, body):
    """Raise GuardError unless (source, method, url, body) is an allowed read."""
    if source not in HOSTS:
        raise GuardError(f"unknown source: {source!r}")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.netloc != HOSTS[source] or parts.fragment:
        raise GuardError(f"{source}: host not allowed: {url}")
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)

    if source == "notion":
        for allowed_method, path, query_keys in NOTION_ROUTES:
            if method == allowed_method and path.fullmatch(parts.path):
                break
        else:
            raise GuardError(f"notion: {method} {parts.path} not allowed")
        if not set(query) <= set(query_keys):
            raise GuardError(f"notion: query parameters not allowed: {sorted(query)}")
        if method == "GET" and body is not None:
            raise GuardError("notion: GET carries no body")
        if method == "POST" and not isinstance(body, dict):
            raise GuardError("notion: search body must be an object")
        return

    if source == "linear":
        if method != "POST" or parts.path != "/graphql" or parts.query:
            raise GuardError(f"linear: {method} {parts.path} not allowed")
        check_linear_body(body)
        return

    allowed_keys = SLACK_ROUTES.get(parts.path)
    if method != "GET" or allowed_keys is None:
        raise GuardError(f"slack: {method} {parts.path} not allowed")
    if not set(query) <= set(allowed_keys):
        raise GuardError(f"slack: query parameters not allowed: {sorted(query)}")
    if body is not None:
        raise GuardError("slack: GET carries no body")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_tokens(path):
    """Read the token file, then delete it — whether or not it parsed."""
    try:
        with open(path, encoding="utf-8") as source:
            raw = source.read()
    finally:
        os.unlink(path)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("token file must be a JSON object")
    unknown = set(data) - set(SOURCES)
    if unknown:
        raise ValueError(f"token file has unknown keys: {sorted(unknown)}")
    tokens = {}
    for name in SOURCES:
        value = data.get(name)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(f"token {name} must be a string")
        tokens[name] = value
    return tokens


def _string_arg(arguments, name, limit=500):
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ToolError(f"{name} must be a non-empty string of at most {limit} characters")
    return value.strip()


def _days_arg(arguments):
    value = arguments.get("days", SLACK_DAYS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= SLACK_DAYS:
        raise ToolError(f"days must be an integer from 1 to {SLACK_DAYS}")
    return value


def notion_id(value):
    """Normalise a Notion id, dashed id or page URL to 32 lowercase hex."""
    match = re.search(r"([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
                      r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})(?:[?#].*)?$", value)
    if not match:
        raise ToolError("id must be a Notion page id or page URL")
    return match.group(1).replace("-", "").lower()


def _rich_text(items):
    return "".join(item.get("plain_text", "") for item in items or [] if isinstance(item, dict))


def _block_text(block):
    kind = block.get("type")
    payload = block.get(kind) if isinstance(kind, str) else None
    if not isinstance(payload, dict):
        return ""
    text = _rich_text(payload.get("rich_text"))
    if not text and kind in ("child_page", "child_database"):
        text = payload.get("title", "")
    return text


def _page_title(page):
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _rich_text(prop.get("title"))
    return ""


def truncate(text, limit):
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    mark = TRUNCATION_MARK.encode("utf-8")
    return data[: max(limit - len(mark), 0)].decode("utf-8", "ignore") + TRUNCATION_MARK


TOOL_SCHEMAS = {
    "linear_search": ("linear", "Search Linear issues by text. Returns up to 10 matches.", {
        "query": {"type": "string", "minLength": 1, "maxLength": 500},
    }, ["query"]),
    "linear_get_issue": ("linear", (
        "Get one Linear issue by identifier (e.g. ABC-123): title, description, state, "
        "project name and description, parent and comments."), {
        "id": {"type": "string", "minLength": 1, "maxLength": 100},
    }, ["id"]),
    "notion_search": ("notion", "Search Notion pages by title text. Returns up to 10 pages.", {
        "query": {"type": "string", "minLength": 1, "maxLength": 500},
    }, ["query"]),
    "notion_get_page": ("notion", (
        "Get a Notion page's title and text (its blocks plus one level of children), "
        "by page id or URL."), {
        "id": {"type": "string", "minLength": 1, "maxLength": 500},
    }, ["id"]),
    "slack_search": ("slack", (
        "Search recent public Slack messages (channels the bot is in, last 30 days) for a "
        "case-insensitive substring. Newest first."), {
        "query": {"type": "string", "minLength": 1, "maxLength": 500},
        "days": {"type": "integer", "minimum": 1, "maximum": SLACK_DAYS},
    }, ["query"]),
    "slack_history": ("slack", (
        "Recent messages of one public Slack channel the bot is in, by name or id. "
        "Newest first."), {
        "channel": {"type": "string", "minLength": 1, "maxLength": 200},
        "days": {"type": "integer", "minimum": 1, "maximum": SLACK_DAYS},
    }, ["channel"]),
}


class Proxy:
    def __init__(self, tokens, enabled, log_path=None, clock=time.time, sleep=time.sleep):
        self._tokens = dict(tokens)
        self.enabled = tuple(s for s in SOURCES if s in enabled and s in self._tokens)
        self.log_path = log_path
        self.clock = clock
        self.sleep = sleep
        self.calls = 0
        self.total_bytes = 0
        self.slack_messages = []
        self.slack_error = None
        self.slack_ready = threading.Event()

    # -- the network boundary -------------------------------------------------

    def _guarded_request(self, source, method, url, body=None):
        """The ONLY function that touches the network. Checks, then sends."""
        check_request(source, method, url, body)
        headers = {"Authorization": f"Bearer {self._tokens[source]}", "Accept": "application/json"}
        if source == "notion":
            headers["Notion-Version"] = NOTION_VERSION
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            payload = response.read(MAX_HTTP_BYTES + 1)
        if len(payload) > MAX_HTTP_BYTES:
            raise ToolError(f"{source} response exceeded {MAX_HTTP_BYTES} bytes")
        return json.loads(payload)

    def _request(self, source, method, url, body=None):
        try:
            return self._guarded_request(source, method, url, body)
        except urllib.error.HTTPError as exc:
            raise ToolError(f"{source} request failed: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"{source} request failed: {type(exc).__name__}") from None
        except ValueError as exc:
            raise ToolError(f"{source} returned an unreadable response") from exc

    # -- Linear ---------------------------------------------------------------

    def _linear(self, name, variables):
        body = {"query": LINEAR_QUERIES[name], "variables": variables}
        result = self._request("linear", "POST", "https://api.linear.app/graphql", body)
        if result.get("errors"):
            messages = [e.get("message", "") for e in result["errors"] if isinstance(e, dict)]
            raise ToolError("linear error: " + "; ".join(messages)[:500])
        return result.get("data") or {}

    def linear_search(self, arguments):
        data = self._linear("search", {"term": _string_arg(arguments, "query")})
        return (data.get("searchIssues") or {}).get("nodes") or []

    def linear_get_issue(self, arguments):
        issue = self._linear("issue", {"id": _string_arg(arguments, "id", 100)}).get("issue")
        if not issue:
            raise ToolError("linear issue not found")
        issue["comments"] = (issue.get("comments") or {}).get("nodes") or []
        return issue

    # -- Notion ---------------------------------------------------------------

    def notion_search(self, arguments):
        body = {
            "query": _string_arg(arguments, "query"),
            "page_size": 10,
            "filter": {"property": "object", "value": "page"},
        }
        result = self._request("notion", "POST", "https://api.notion.com/v1/search", body)
        return [
            {
                "id": page.get("id"),
                "title": _page_title(page),
                "url": page.get("url"),
                "last_edited_time": page.get("last_edited_time"),
            }
            for page in result.get("results") or []
            if isinstance(page, dict)
        ]

    def _children(self, block_id):
        url = f"https://api.notion.com/v1/blocks/{notion_id(block_id)}/children?page_size=100"
        result = self._request("notion", "GET", url)
        return [b for b in result.get("results") or [] if isinstance(b, dict)]

    def notion_get_page(self, arguments):
        page_id = notion_id(_string_arg(arguments, "id"))
        page = self._request("notion", "GET", f"https://api.notion.com/v1/pages/{page_id}")
        lines = []
        fetches = 0
        for block in self._children(page_id):
            lines.append(_block_text(block))
            if block.get("has_children") and isinstance(block.get("id"), str):
                if fetches >= NOTION_CHILD_FETCHES:
                    lines.append("  [nested blocks omitted]")
                    continue
                fetches += 1
                lines.extend("  " + _block_text(child) for child in self._children(block["id"]))
        return {
            "id": page.get("id"),
            "title": _page_title(page),
            "url": page.get("url"),
            "text": "\n".join(line for line in lines if line.strip()),
        }

    # -- Slack ----------------------------------------------------------------

    def _slack(self, method, params):
        url = f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}"
        for attempt in range(5):
            try:
                result = self._guarded_request("slack", "GET", url)
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 4:
                    raise
                retry_after = exc.headers.get("Retry-After", "1") if exc.headers else "1"
                self.sleep(min(int(retry_after) if retry_after.isdigit() else 1, 30))
                continue
            if not result.get("ok"):
                raise ToolError(f"slack {method} failed: {result.get('error', 'unknown')}")
            return result
        raise ToolError(f"slack {method} failed")

    @staticmethod
    def _cursor(result):
        return ((result.get("response_metadata") or {}).get("next_cursor")) or ""

    def preload_slack(self):
        """Load the last 30 days of every public channel the bot is a member of."""
        try:
            oldest = f"{self.clock() - SLACK_DAYS * 86400:.6f}"
            channels, cursor = [], ""
            for _ in range(SLACK_LIST_PAGES):
                params = {"types": "public_channel", "exclude_archived": "true", "limit": "200"}
                if cursor:
                    params["cursor"] = cursor
                result = self._slack("conversations.list", params)
                channels += [
                    c for c in result.get("channels") or []
                    if isinstance(c, dict) and c.get("is_member") and isinstance(c.get("id"), str)
                ]
                cursor = self._cursor(result)
                if not cursor:
                    break
            messages = []
            for channel in channels:
                cursor = ""
                for _ in range(SLACK_HISTORY_PAGES):
                    params = {"channel": channel["id"], "oldest": oldest, "limit": "200"}
                    if cursor:
                        params["cursor"] = cursor
                    result = self._slack("conversations.history", params)
                    for message in result.get("messages") or []:
                        if not isinstance(message, dict):
                            continue
                        try:
                            ts = float(message.get("ts", ""))
                        except (TypeError, ValueError):
                            continue
                        messages.append({
                            "channel": channel.get("name", ""),
                            "channel_id": channel["id"],
                            "ts": ts,
                            "user": message.get("user") or message.get("username") or "",
                            "text": message.get("text") or "",
                        })
                    cursor = self._cursor(result)
                    if not cursor:
                        break
            messages.sort(key=lambda m: m["ts"], reverse=True)
            self.slack_messages = messages
        except (ToolError, GuardError, urllib.error.URLError, OSError, ValueError) as exc:
            self.slack_error = str(exc) if isinstance(exc, (ToolError, GuardError)) else (
                f"slack preload failed: {type(exc).__name__}")
            self._log("slack_preload", {}, 0, self.slack_error)
        finally:
            self.slack_ready.set()

    def _slack_messages(self, days):
        if not self.slack_ready.wait(timeout=300):
            raise ToolError("slack history is still loading; try again shortly")
        if self.slack_error:
            raise ToolError(self.slack_error)
        cutoff = self.clock() - days * 86400
        return [m for m in self.slack_messages if m["ts"] >= cutoff]

    @staticmethod
    def _format_slack(messages):
        return [
            {
                "channel": m["channel"],
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(m["ts"])),
                "user": m["user"],
                "text": m["text"],
            }
            for m in messages[:SLACK_RESULTS]
        ]

    def slack_search(self, arguments):
        needle = _string_arg(arguments, "query").casefold()
        days = _days_arg(arguments)
        return self._format_slack(
            [m for m in self._slack_messages(days) if needle in m["text"].casefold()])

    def slack_history(self, arguments):
        channel = _string_arg(arguments, "channel", 200).lstrip("#")
        days = _days_arg(arguments)
        return self._format_slack(
            [m for m in self._slack_messages(days) if channel in (m["channel"], m["channel_id"])])

    # -- MCP ------------------------------------------------------------------

    def tools(self):
        return [
            {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": required,
                    "properties": properties,
                },
            }
            for name, (source, description, properties, required) in TOOL_SCHEMAS.items()
            if source in self.enabled
        ]

    def _log(self, tool, arguments, response_bytes, error):
        if not self.log_path:
            return
        entry = {
            "ts": round(self.clock(), 3),
            "tool": tool,
            "arguments": arguments,
            "response_bytes": response_bytes,
            "error": error,
        }
        with open(self.log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def call_tool(self, name, arguments):
        """Return (text, is_error). Every call is counted and logged."""
        self.calls += 1
        text, error = None, None
        try:
            if self.calls > MAX_TOOL_CALLS:
                raise ToolError(f"tool call limit reached ({MAX_TOOL_CALLS} per run)")
            if self.total_bytes >= MAX_TOTAL_BYTES:
                raise ToolError(f"response budget exhausted ({MAX_TOTAL_BYTES} bytes per run)")
            spec = TOOL_SCHEMAS.get(name) if isinstance(name, str) else None
            if spec is None or spec[0] not in self.enabled:
                raise ToolError(f"unknown tool: {name}")
            if not isinstance(arguments, dict):
                raise ToolError("arguments must be an object")
            unknown = set(arguments) - set(spec[2])
            if unknown:
                raise ToolError(f"unknown arguments: {sorted(unknown)}")
            data = getattr(self, name)(arguments)
            limit = min(MAX_RESPONSE_BYTES, MAX_TOTAL_BYTES - self.total_bytes)
            text = truncate(json.dumps(data, ensure_ascii=False), limit)
            self.total_bytes += len(text.encode("utf-8"))
        except (ToolError, GuardError) as exc:
            error = str(exc)
        self._log(name, arguments, len(text.encode("utf-8")) if text else 0, error)
        return (error, True) if error is not None else (text, False)


def result(request_id, value):
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(proxy, message):
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
            "serverInfo": {"name": "cursor-approve-context", "version": "1.0.0"},
        })
    if method == "ping":
        return result(request_id, {})
    if method == "tools/list":
        return result(request_id, {"tools": proxy.tools()})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        text, is_error = proxy.call_tool(params.get("name"), params.get("arguments", {}))
        return result(request_id, {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        })
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    return error(request_id, -32601, f"unknown method: {method}")


def serve(proxy, stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("request must be an object")
            response = handle(proxy, message)
        except ValueError as exc:
            response = error(None, -32700, str(exc))
        if response is not None:
            stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            stdout.flush()


def parse_enable(value):
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = set(names) - set(SOURCES)
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown source(s): {', '.join(sorted(unknown))}")
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--enable", type=parse_enable, required=True,
                        help="comma-separated: linear,notion,slack")
    parser.add_argument("--log", required=True)
    args = parser.parse_args(argv)
    try:
        tokens = load_tokens(args.token_file)
    except (OSError, ValueError) as exc:
        # The message names the problem, never a token value.
        parser.exit(2, f"context-proxy: cannot load token file: {type(exc).__name__}\n")
    proxy = Proxy(tokens, args.enable, args.log)
    if "slack" in proxy.enabled:
        threading.Thread(target=proxy.preload_slack, daemon=True).start()
    serve(proxy)


if __name__ == "__main__":
    main()
