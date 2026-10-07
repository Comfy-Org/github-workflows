#!/usr/bin/env python3
"""Tests for context-proxy.py, the read-only Linear/Notion/Slack MCP proxy.

Every network call is mocked: `urllib.request.build_opener` is patched for the
whole module (see setUpModule), so a test that forgets to mock fails loudly
instead of reaching the internet.
"""

import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, "..", "context-proxy.py")
SPEC = importlib.util.spec_from_file_location("context_proxy", MODULE_PATH)
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)

TOKENS = {"linear": "lin_api_SECRETLINEAR", "notion": "ntn_SECRETNOTION", "slack": "xoxb-SECRETSLACK"}
PAGE_ID = "0123456789abcdef0123456789abcdef"
DASHED_PAGE_ID = "01234567-89ab-cdef-0123-456789abcdef"
NOW = 1_800_000_000.0

_NETWORK = mock.patch.object(
    PROXY.urllib.request, "build_opener",
    side_effect=AssertionError("unmocked network call"),
)


def setUpModule():
    _NETWORK.start()


def tearDownModule():
    _NETWORK.stop()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeNetwork:
    """Stands in for build_opener(); routes each request to `responder`."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []

    def __call__(self, *handlers):
        assert any(isinstance(h, PROXY._NoRedirect) for h in handlers), "redirects must be refused"
        return self

    def open(self, request, timeout=None):
        body = json.loads(request.data) if request.data else None
        self.requests.append({
            "method": request.get_method(),
            "url": request.full_url,
            "headers": dict(request.header_items()),
            "body": body,
        })
        reply = self.responder(request.get_method(), request.full_url, body)
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(json.dumps(reply).encode("utf-8"))


def make_proxy(enabled=("linear", "notion", "slack"), tokens=None, log_path=None):
    return PROXY.Proxy(tokens or TOKENS, list(enabled), log_path,
                       clock=lambda: NOW, sleep=lambda seconds: None)


class GuardTest(unittest.TestCase):
    ALLOWED = [
        ("notion", "POST", "https://api.notion.com/v1/search", {"query": "x"}),
        ("notion", "GET", f"https://api.notion.com/v1/pages/{PAGE_ID}", None),
        ("notion", "GET", f"https://api.notion.com/v1/blocks/{PAGE_ID}/children?page_size=100", None),
        ("linear", "POST", "https://api.linear.app/graphql",
         {"query": PROXY.LINEAR_QUERIES["search"], "variables": {"term": "x"}}),
        ("linear", "POST", "https://api.linear.app/graphql",
         {"query": PROXY.LINEAR_QUERIES["issue"], "variables": {"id": "ABC-1"}}),
        ("slack", "GET", "https://slack.com/api/conversations.list?types=public_channel", None),
        ("slack", "GET", "https://slack.com/api/conversations.history?channel=C1&oldest=1", None),
    ]

    REJECTED = [
        # Notion writes, and every other Notion route.
        ("notion", "PATCH", f"https://api.notion.com/v1/pages/{PAGE_ID}", {"archived": True}),
        ("notion", "POST", "https://api.notion.com/v1/comments", {"rich_text": []}),
        ("notion", "DELETE", f"https://api.notion.com/v1/blocks/{PAGE_ID}", None),
        ("notion", "POST", "https://api.notion.com/v1/pages", {"parent": {}}),
        ("notion", "POST", "https://api.notion.com/v1/blocks", {}),
        ("notion", "PATCH", f"https://api.notion.com/v1/blocks/{PAGE_ID}/children", {"children": []}),
        ("notion", "POST", f"https://api.notion.com/v1/databases/{PAGE_ID}/query", {}),
        ("notion", "POST", "https://api.notion.com/v1/databases", {}),
        ("notion", "GET", f"https://api.notion.com/v1/blocks/{PAGE_ID}", None),
        ("notion", "GET", "https://api.notion.com/v1/users", None),
        ("notion", "GET", "https://api.notion.com/v1/pages/../comments", None),
        ("notion", "GET", f"https://api.notion.com/v1/pages/{PAGE_ID}/../../comments", None),
        ("notion", "GET", f"https://api.notion.com/v1/pages/{PAGE_ID}?x=1", None),
        ("notion", "GET", f"https://api.notion.com/v1/pages/{PAGE_ID}", {"x": 1}),
        ("notion", "POST", "https://api.notion.com/v1/search", None),
        # Wrong scheme, host, port, userinfo, or a token sent to another source's host.
        ("notion", "POST", "http://api.notion.com/v1/search", {}),
        ("notion", "POST", "https://api.notion.com:8443/v1/search", {}),
        ("notion", "POST", "https://user@api.notion.com/v1/search", {}),
        ("notion", "POST", "https://evil.example/v1/search", {}),
        ("notion", "POST", "https://api.notion.com.evil.example/v1/search", {}),
        ("notion", "POST", "https://api.linear.app/graphql",
         {"query": PROXY.LINEAR_QUERIES["search"], "variables": {}}),
        ("slack", "GET", f"https://api.notion.com/v1/pages/{PAGE_ID}", None),
        ("github", "GET", "https://api.github.com/user", None),
        # Slack writes and every other Slack method.
        ("slack", "POST", "https://slack.com/api/chat.postMessage", {"channel": "C1", "text": "x"}),
        ("slack", "GET", "https://slack.com/api/chat.postMessage?channel=C1&text=x", None),
        ("slack", "GET", "https://slack.com/api/search.messages?query=x", None),
        ("slack", "GET", "https://slack.com/api/conversations.join?channel=C1", None),
        ("slack", "POST", "https://slack.com/api/conversations.history", {"channel": "C1"}),
        ("slack", "GET", "https://slack.com/api/conversations.history?channel=C1&text=x", None),
        ("slack", "GET", "https://hooks.slack.com/api/conversations.list", None),
        # Linear: wrong method/path, mutation, subscription, non-constant query.
        ("linear", "GET", "https://api.linear.app/graphql?query=%7Bviewer%7Bid%7D%7D", None),
        ("linear", "POST", "https://api.linear.app/rest", {"query": PROXY.LINEAR_QUERIES["search"]}),
        ("linear", "POST", "https://api.linear.app/graphql",
         {"query": 'mutation { issueDelete(id: "ABC-1") { success } }'}),
        ("linear", "POST", "https://api.linear.app/graphql",
         {"query": "subscription { issueUpdated { id } }"}),
        ("linear", "POST", "https://api.linear.app/graphql",
         {"query": 'query Q { viewer { id } } mutation M { issueDelete(id: "x") { success } }'}),
        ("linear", "POST", "https://api.linear.app/graphql",
         {"query": PROXY.LINEAR_QUERIES["search"] + " mutation M { x }"}),
        ("linear", "POST", "https://api.linear.app/graphql",
         {"query": '{ issueDelete(id: "x") { success } }'}),
        ("linear", "POST", "https://api.linear.app/graphql", {"query": "query { viewer { id } }"}),
        ("linear", "POST", "https://api.linear.app/graphql",
         {"query": PROXY.LINEAR_QUERIES["search"], "variables": "mutation"}),
        ("linear", "POST", "https://api.linear.app/graphql",
         {"query": PROXY.LINEAR_QUERIES["search"], "extensions": {}}),
        ("linear", "POST", "https://api.linear.app/graphql", None),
    ]

    def test_allow_list_accepts_the_read_calls(self):
        for case in self.ALLOWED:
            with self.subTest(case=case[:3]):
                PROXY.check_request(*case)

    def test_guard_rejects_everything_else_before_any_network_call(self):
        proxy = make_proxy()
        for case in self.REJECTED:
            with self.subTest(case=case[:3]):
                with mock.patch.object(PROXY.urllib.request, "build_opener") as opener, \
                        mock.patch.object(PROXY.urllib.request, "Request") as request:
                    with self.assertRaises(PROXY.GuardError):
                        proxy._guarded_request(*case)
                    opener.assert_not_called()
                    request.assert_not_called()

    def test_graphql_keywords_inside_strings_and_comments_are_ignored(self):
        names = PROXY._graphql_tokens('query Q { a(x: "mutation") } # subscription\n')
        self.assertNotIn("mutation", names)
        self.assertNotIn("subscription", names)

    def test_constant_queries_are_pure_queries(self):
        for name, document in PROXY.LINEAR_QUERIES.items():
            with self.subTest(name=name):
                tokens = PROXY._graphql_tokens(document)
                self.assertEqual(tokens[0], "query")
                self.assertFalse({"mutation", "subscription"} & set(tokens))

    def test_token_travels_only_to_its_own_host(self):
        network = FakeNetwork(lambda method, url, body: {"results": []})
        with mock.patch.object(PROXY.urllib.request, "build_opener", network):
            make_proxy().notion_search({"query": "roadmap"})
        (sent,) = network.requests
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {TOKENS['notion']}")
        self.assertEqual(sent["headers"]["Notion-version"], "2022-06-28")

    def test_redirect_handler_refuses_to_follow(self):
        self.assertIsNone(PROXY._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil"))


class StaticNetworkBoundaryTest(unittest.TestCase):
    """Parse context-proxy.py: no network primitive outside _guarded_request."""

    GUARD = "_guarded_request"
    NETWORK_PREFIXES = ("urllib.request", "http.client", "http.", "socket", "ssl")
    ALLOWED_IMPORTS = {
        "argparse", "json", "os", "re", "sys", "threading", "time",
        "urllib.error", "urllib.parse", "urllib.request",
    }

    @classmethod
    def setUpClass(cls):
        with open(MODULE_PATH, encoding="utf-8") as source:
            cls.tree = ast.parse(source.read())

    @staticmethod
    def dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return None

    def guard_span(self):
        spans = [
            (node.lineno, node.end_lineno)
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == self.GUARD
        ]
        self.assertEqual(len(spans), 1, "exactly one guard function")
        return spans[0]

    def test_no_network_call_outside_the_guard(self):
        start, end = self.guard_span()
        # A class may subclass a urllib handler (`_NoRedirect`); naming a base
        # class sends nothing.
        base_ids = {
            id(part)
            for node in ast.walk(self.tree) if isinstance(node, ast.ClassDef)
            for base in node.bases
            for part in ast.walk(base)
        }
        offenders = []
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.Attribute, ast.Name)) or id(node) in base_ids:
                continue
            name = self.dotted(node)
            if not name or not name.startswith(self.NETWORK_PREFIXES):
                continue
            if not start <= node.lineno <= end:
                offenders.append(f"line {node.lineno}: {name}")
        self.assertEqual(offenders, [])

    def test_guard_checks_before_it_builds_anything(self):
        guard = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == self.GUARD
        )
        statements = [s for s in guard.body if not (
            isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        first = statements[0]
        self.assertIsInstance(first, ast.Expr)
        self.assertEqual(self.dotted(first.value.func), "check_request")

    def test_imports_are_on_the_allow_list(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIsNone(alias.asname, f"aliased import of {alias.name}")
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.fail(f"from-import of {node.module}: names would escape the static check")
        self.assertLessEqual(imported, self.ALLOWED_IMPORTS)

    def test_no_environment_writes_or_child_processes(self):
        forbidden = ("os.environ", "os.putenv", "os.system", "os.popen", "os.exec",
                     "os.spawn", "os.fork", "os.posix_spawn", "subprocess")
        names = [self.dotted(node) for node in ast.walk(self.tree)
                 if isinstance(node, (ast.Attribute, ast.Name))]
        self.assertEqual([n for n in names if n and n.startswith(forbidden)], [])

    def test_harness_catches_a_stray_call(self):
        tree = ast.parse("import urllib.request\ndef f():\n    urllib.request.urlopen('x')\n")
        names = [self.dotted(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
        self.assertIn("urllib.request.urlopen", names)


class ToolsListTest(unittest.TestCase):
    ALL = {"linear_search", "linear_get_issue", "notion_search", "notion_get_page",
           "slack_search", "slack_history"}

    def listed(self, proxy):
        response = PROXY.handle(proxy, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {tool["name"] for tool in response["result"]["tools"]}

    def test_exactly_six_read_tools_when_all_enabled(self):
        self.assertEqual(self.listed(make_proxy()), self.ALL)

    def test_filtered_by_enable(self):
        self.assertEqual(self.listed(make_proxy(enabled=["notion"])),
                         {"notion_search", "notion_get_page"})
        self.assertEqual(self.listed(make_proxy(enabled=["linear", "slack"])),
                         {"linear_search", "linear_get_issue", "slack_search", "slack_history"})

    def test_missing_token_disables_the_source(self):
        proxy = make_proxy(tokens={"linear": TOKENS["linear"]})
        self.assertEqual(self.listed(proxy), {"linear_search", "linear_get_issue"})

    def test_disabled_tool_cannot_be_called(self):
        text, is_error = make_proxy(enabled=["linear"]).call_tool("notion_search", {"query": "x"})
        self.assertTrue(is_error)
        self.assertIn("unknown tool", text)

    def test_unknown_enable_name_is_rejected(self):
        with self.assertRaises(Exception):
            PROXY.parse_enable("linear,github")

    def test_initialize(self):
        response = PROXY.handle(make_proxy(), {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")


class LinearTest(unittest.TestCase):
    def test_agent_input_reaches_linear_only_through_variables(self):
        hostile = 'x") { id } } mutation Evil { issueDelete(id: "ABC-1") { success } } #'
        network = FakeNetwork(lambda method, url, body: {"data": {"searchIssues": {"nodes": []}}})
        with mock.patch.object(PROXY.urllib.request, "build_opener", network):
            text, is_error = make_proxy().call_tool("linear_search", {"query": hostile})
        self.assertFalse(is_error, text)
        (sent,) = network.requests
        self.assertEqual(sent["body"]["query"], PROXY.LINEAR_QUERIES["search"])
        self.assertNotIn("mutation", sent["body"]["query"])
        self.assertEqual(sent["body"]["variables"], {"term": hostile.strip()})

    def test_get_issue_flattens_comments(self):
        issue = {
            "identifier": "ABC-1", "title": "T", "description": "D", "url": "u",
            "state": {"name": "Todo"}, "project": {"name": "P", "description": "PD"},
            "parent": {"identifier": "ABC-0", "title": "Parent"},
            "comments": {"nodes": [{"body": "hi", "createdAt": "now", "user": {"name": "a"}}]},
        }
        network = FakeNetwork(lambda method, url, body: {"data": {"issue": issue}})
        with mock.patch.object(PROXY.urllib.request, "build_opener", network):
            text, is_error = make_proxy().call_tool("linear_get_issue", {"id": "ABC-1"})
        self.assertFalse(is_error, text)
        data = json.loads(text)
        self.assertEqual(data["comments"][0]["body"], "hi")
        self.assertEqual(data["project"]["description"], "PD")
        self.assertEqual(network.requests[0]["body"]["variables"], {"id": "ABC-1"})

    def test_graphql_errors_become_tool_errors(self):
        network = FakeNetwork(lambda method, url, body: {"errors": [{"message": "nope"}]})
        with mock.patch.object(PROXY.urllib.request, "build_opener", network):
            text, is_error = make_proxy().call_tool("linear_search", {"query": "x"})
        self.assertTrue(is_error)
        self.assertIn("nope", text)


class NotionTest(unittest.TestCase):
    def responder(self, method, url, body):
        if url.endswith("/v1/search"):
            return {"results": [{"id": DASHED_PAGE_ID, "url": "https://notion.so/p",
                                 "properties": {"Name": {"type": "title",
                                                         "title": [{"plain_text": "Roadmap"}]}}}]}
        if f"/v1/pages/{PAGE_ID}" in url:
            return {"id": DASHED_PAGE_ID, "url": "https://notion.so/p",
                    "properties": {"title": {"type": "title", "title": [{"plain_text": "Roadmap"}]}}}
        if f"/v1/blocks/{PAGE_ID}/children" in url:
            return {"results": [
                {"id": "11111111-1111-1111-1111-111111111111", "type": "paragraph",
                 "has_children": True, "paragraph": {"rich_text": [{"plain_text": "Top"}]}},
                {"id": "22222222-2222-2222-2222-222222222222", "type": "heading_1",
                 "has_children": False, "heading_1": {"rich_text": [{"plain_text": "Head"}]}},
            ]}
        if "/v1/blocks/11111111111111111111111111111111/children" in url:
            return {"results": [{"id": "33333333-3333-3333-3333-333333333333", "type": "bulleted_list_item",
                                 "has_children": True,
                                 "bulleted_list_item": {"rich_text": [{"plain_text": "Child"}]}}]}
        raise AssertionError(f"unexpected request {method} {url}")

    def test_search(self):
        network = FakeNetwork(self.responder)
        with mock.patch.object(PROXY.urllib.request, "build_opener", network):
            text, is_error = make_proxy().call_tool("notion_search", {"query": "roadmap"})
        self.assertFalse(is_error, text)
        self.assertEqual(json.loads(text)[0]["title"], "Roadmap")
        self.assertEqual(network.requests[0]["body"]["query"], "roadmap")

    def test_get_page_reads_one_level_of_children(self):
        network = FakeNetwork(self.responder)
        with mock.patch.object(PROXY.urllib.request, "build_opener", network):
            text, is_error = make_proxy().call_tool(
                "notion_get_page", {"id": f"https://www.notion.so/Roadmap-{PAGE_ID}"})
        self.assertFalse(is_error, text)
        page = json.loads(text)
        self.assertEqual(page["title"], "Roadmap")
        self.assertEqual(page["text"], "Top\n  Child\nHead")
        self.assertTrue(all(r["method"] == "GET" for r in network.requests))
        self.assertEqual(len(network.requests), 3)  # page, its blocks, one child level

    def test_bad_id_is_refused_without_a_request(self):
        text, is_error = make_proxy().call_tool("notion_get_page", {"id": "../comments"})
        self.assertTrue(is_error)

    def test_http_error_is_reported_without_body(self):
        def fail(method, url, body):
            return urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        with mock.patch.object(PROXY.urllib.request, "build_opener", FakeNetwork(fail)):
            text, is_error = make_proxy().call_tool("notion_get_page", {"id": PAGE_ID})
        self.assertTrue(is_error)
        self.assertEqual(text, "notion request failed: HTTP 404")

    def test_long_block_list_is_paginated_and_a_cap_is_marked(self):
        def paged(method, url, body):
            if f"/v1/pages/{PAGE_ID}" in url:
                return {"id": DASHED_PAGE_ID, "properties": {}}
            page = int(url.split("start_cursor=c")[1]) if "start_cursor=" in url else 0
            return {"results": [{"type": "paragraph", "has_children": False,
                                 "paragraph": {"rich_text": [{"plain_text": f"p{page}"}]}}],
                    "has_more": True, "next_cursor": f"c{page + 1}"}
        network = FakeNetwork(paged)
        with mock.patch.object(PROXY.urllib.request, "build_opener", network), \
                mock.patch.object(PROXY, "NOTION_CHILD_PAGES", 3):
            text, is_error = make_proxy().call_tool("notion_get_page", {"id": PAGE_ID})
        self.assertFalse(is_error, text)
        self.assertEqual(json.loads(text)["text"], "p0\np1\np2\n[more blocks omitted]")
        self.assertIn("start_cursor=c2", network.requests[-1]["url"])
        self.assertEqual(len(network.requests), 4)

    def test_table_rows_keep_their_cells(self):
        row = {"type": "table_row", "has_children": False, "table_row": {"cells": [
            [{"plain_text": "Option"}], [{"plain_text": "Chosen"}]]}}
        def reply(method, url, body):
            if f"/v1/pages/{PAGE_ID}" in url:
                return {"id": DASHED_PAGE_ID, "properties": {}}
            return {"results": [row]}
        with mock.patch.object(PROXY.urllib.request, "build_opener", FakeNetwork(reply)):
            text, _ = make_proxy().call_tool("notion_get_page", {"id": PAGE_ID})
        self.assertEqual(json.loads(text)["text"], "Option | Chosen")


class SlackTest(unittest.TestCase):
    def responder(self, method, url, body):
        if "conversations.list" in url:
            return {"ok": True, "channels": [
                {"id": "C1", "name": "eng", "is_member": True},
                {"id": "C2", "name": "random", "is_member": False},
                {"id": "C3", "name": "product", "is_member": True},
            ]}
        if "channel=C1" in url:
            return {"ok": True, "messages": [
                {"ts": f"{NOW - 3600:.6f}", "user": "U1", "text": "Ship the Approve gate"},
                {"ts": f"{NOW - 10 * 86400:.6f}", "user": "U2", "text": "approve old"},
            ]}
        if "channel=C3" in url:
            return {"ok": True, "messages": [
                {"ts": f"{NOW - 60:.6f}", "user": "U3", "text": "APPROVE the roadmap"},
            ]}
        raise AssertionError(f"unexpected request {url}")

    def preloaded(self):
        proxy = make_proxy()
        network = FakeNetwork(self.responder)
        with mock.patch.object(PROXY.urllib.request, "build_opener", network):
            proxy.preload_slack()
        return proxy, network

    def test_preload_reads_member_channels_for_30_days(self):
        proxy, network = self.preloaded()
        urls = [r["url"] for r in network.requests]
        self.assertTrue(urls[0].startswith("https://slack.com/api/conversations.list?"))
        self.assertFalse(any("channel=C2" in url for url in urls))
        self.assertIn(f"oldest={NOW - 30 * 86400:.6f}", urls[1])
        self.assertTrue(all(r["method"] == "GET" for r in network.requests))
        self.assertEqual(len(proxy.slack_messages), 3)

    def test_search_is_case_insensitive_newest_first(self):
        proxy, _ = self.preloaded()
        text, is_error = proxy.call_tool("slack_search", {"query": "approve", "days": 30})
        self.assertFalse(is_error, text)
        self.assertEqual([m["text"] for m in json.loads(text)["messages"]],
                         ["APPROVE the roadmap", "Ship the Approve gate", "approve old"])

    def test_days_narrows_the_window(self):
        proxy, _ = self.preloaded()
        text, _ = proxy.call_tool("slack_search", {"query": "approve", "days": 7})
        self.assertEqual(len(json.loads(text)["messages"]), 2)

    def test_history_by_name_or_id(self):
        proxy, _ = self.preloaded()
        by_name, _ = proxy.call_tool("slack_history", {"channel": "#eng", "days": 30})
        by_id, _ = proxy.call_tool("slack_history", {"channel": "C1"})
        self.assertEqual(by_name, by_id)
        self.assertEqual([m["user"] for m in json.loads(by_name)["messages"]], ["U1", "U2"])

    def test_preload_failure_surfaces_as_tool_error(self):
        proxy = make_proxy()
        network = FakeNetwork(lambda method, url, body: {"ok": False, "error": "invalid_auth"})
        with mock.patch.object(PROXY.urllib.request, "build_opener", network):
            proxy.preload_slack()
        text, is_error = proxy.call_tool("slack_search", {"query": "x"})
        self.assertTrue(is_error)
        self.assertIn("invalid_auth", text)

    def test_rate_limit_is_retried(self):
        calls = []

        def limited(method, url, body):
            calls.append(url)
            if len(calls) == 1:
                return urllib.error.HTTPError(url, 429, "Too Many", {"Retry-After": "2"}, None)
            return {"ok": True, "channels": []}

        proxy = make_proxy()
        with mock.patch.object(PROXY.urllib.request, "build_opener", FakeNetwork(limited)):
            proxy.preload_slack()
        self.assertIsNone(proxy.slack_error)
        self.assertEqual(len(calls), 2)

    def test_one_failing_channel_keeps_the_rest_and_is_reported(self):
        def reply(method, url, body):
            if "channel=C3" in url:
                return {"ok": False, "error": "not_in_channel"}
            return self.responder(method, url, body)
        proxy = make_proxy()
        with mock.patch.object(PROXY.urllib.request, "build_opener", FakeNetwork(reply)):
            proxy.preload_slack()
        self.assertIsNone(proxy.slack_error)
        text, is_error = proxy.call_tool("slack_search", {"query": "approve"})
        self.assertFalse(is_error, text)
        result = json.loads(text)
        self.assertEqual(len(result["messages"]), 2)
        self.assertIn("1 channel(s) could not be read", result["incomplete"])

    def test_message_cap_stops_the_preload_and_is_reported(self):
        proxy = make_proxy()
        network = FakeNetwork(self.responder)
        with mock.patch.object(PROXY.urllib.request, "build_opener", network), \
                mock.patch.object(PROXY, "SLACK_MAX_MESSAGES", 2):
            proxy.preload_slack()
        self.assertFalse(any("channel=C3" in r["url"] for r in network.requests))
        self.assertEqual(len(proxy.slack_messages), 2)
        self.assertIn("2 messages", proxy.slack_incomplete)

    def test_deadline_stops_the_preload_and_is_reported(self):
        ticks = iter([0, 0, PROXY.SLACK_PRELOAD_SECONDS])
        proxy = PROXY.Proxy(TOKENS, ["slack"], clock=lambda: NOW, sleep=lambda s: None,
                            monotonic=lambda: next(ticks))
        with mock.patch.object(PROXY.urllib.request, "build_opener", FakeNetwork(self.responder)):
            proxy.preload_slack()
        self.assertEqual(len(proxy.slack_messages), 2)  # C1 only
        self.assertIn("stopped after", proxy.slack_incomplete)

    def test_non_object_reply_disables_slack_with_an_error(self):
        proxy = make_proxy()
        with mock.patch.object(PROXY.urllib.request, "build_opener",
                               FakeNetwork(lambda method, url, body: [1, 2])):
            proxy.preload_slack()
        self.assertTrue(proxy.slack_ready.is_set())
        text, is_error = proxy.call_tool("slack_search", {"query": "x"})
        self.assertTrue(is_error)
        self.assertIn("unexpected response", text)

    def test_bad_days_is_refused(self):
        proxy, _ = self.preloaded()
        for days in (0, 31, "7", True):
            with self.subTest(days=days):
                _, is_error = proxy.call_tool("slack_search", {"query": "x", "days": days})
                self.assertTrue(is_error)


class LimitsAndLoggingTest(unittest.TestCase):
    def test_call_limit(self):
        proxy = make_proxy()
        proxy.slack_ready.set()
        for _ in range(PROXY.MAX_TOOL_CALLS):
            _, is_error = proxy.call_tool("slack_search", {"query": "x"})
            self.assertFalse(is_error)
        text, is_error = proxy.call_tool("slack_search", {"query": "x"})
        self.assertTrue(is_error)
        self.assertIn("call limit", text)

    def test_response_truncated_to_20kb(self):
        proxy = make_proxy()
        proxy.slack_messages = [{"channel": "eng", "channel_id": "C1", "ts": NOW, "user": "U",
                                 "text": "é" * 30000}]
        proxy.slack_ready.set()
        text, is_error = proxy.call_tool("slack_search", {"query": "é"})
        self.assertFalse(is_error)
        self.assertLessEqual(len(text.encode("utf-8")), PROXY.MAX_RESPONSE_BYTES)
        self.assertTrue(text.endswith(PROXY.TRUNCATION_MARK))

    @mock.patch.object(PROXY, "MAX_TOOL_CALLS", 1000)
    def test_total_budget(self):
        # 40 calls x 20 KB < 1 MB, so lift the call cap to reach the byte budget.
        proxy = make_proxy()
        proxy.slack_messages = [{"channel": "eng", "channel_id": "C1", "ts": NOW, "user": "U",
                                 "text": "x" * 30000}]
        proxy.slack_ready.set()
        sizes = []
        for _ in range(100):
            text, is_error = proxy.call_tool("slack_search", {"query": "x"})
            if is_error:
                self.assertIn("budget", text)
                break
            sizes.append(len(text.encode("utf-8")))
        else:
            self.fail("budget never ran out")
        self.assertEqual(sum(sizes), PROXY.MAX_TOTAL_BYTES)
        self.assertEqual(proxy.total_bytes, PROXY.MAX_TOTAL_BYTES)

    def test_every_call_is_logged_without_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = os.path.join(directory, "calls.jsonl")
            proxy = make_proxy(log_path=log_path)
            network = FakeNetwork(lambda method, url, body: {"results": []})
            with mock.patch.object(PROXY.urllib.request, "build_opener", network):
                proxy.call_tool("notion_search", {"query": "roadmap"})
            proxy.call_tool("notion_get_page", {"id": "nope"})
            with open(log_path, encoding="utf-8") as log:
                raw = log.read()
        entries = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([e["tool"] for e in entries], ["notion_search", "notion_get_page"])
        self.assertEqual(entries[0]["arguments"], {"query": "roadmap"})
        self.assertEqual(entries[0]["response_bytes"], 2)
        self.assertIsNone(entries[0]["error"])
        self.assertIn("Notion page id", entries[1]["error"])
        for token in TOKENS.values():
            self.assertNotIn(token, raw)

    def test_unknown_arguments_are_refused(self):
        _, is_error = make_proxy().call_tool("linear_search", {"query": "x", "extra": 1})
        self.assertTrue(is_error)


class RobustnessTest(unittest.TestCase):
    """A malformed upstream reply or request is one error, never a dead server."""

    def test_non_object_upstream_json_is_a_tool_error(self):
        for tool, arguments in (("linear_search", {"query": "x"}),
                                ("linear_get_issue", {"id": "ABC-1"}),
                                ("notion_search", {"query": "x"}),
                                ("notion_get_page", {"id": PAGE_ID})):
            for reply in ([1], "text", {"data": {"issue": [1]}, "results": "x"}):
                with self.subTest(tool=tool, reply=reply), mock.patch.object(
                        PROXY.urllib.request, "build_opener",
                        FakeNetwork(lambda method, url, body, reply=reply: reply)):
                    text, is_error = make_proxy().call_tool(tool, arguments)
                    self.assertNotIn("internal error", text)
                    if not isinstance(reply, dict):
                        self.assertTrue(is_error)
                        self.assertIn("unexpected response", text)

    def test_unexpected_exception_is_a_generic_error_result(self):
        proxy = make_proxy()
        with mock.patch.object(PROXY.Proxy, "linear_search", side_effect=KeyError("secret detail")):
            text, is_error = proxy.call_tool("linear_search", {"query": "x"})
        self.assertTrue(is_error)
        self.assertEqual(text, "linear_search: internal error (KeyError)")

    def test_serve_survives_bad_requests(self):
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": ["2025-06-18"]}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]
        stdout = io.StringIO()
        with mock.patch.object(PROXY, "handle", side_effect=[TypeError("x"), {"ok": 1}]):
            PROXY.serve(make_proxy(), io.StringIO("\n".join(map(json.dumps, lines)) + "\n"), stdout)
        first, second = map(json.loads, stdout.getvalue().splitlines())
        self.assertEqual(first["error"]["code"], -32603)
        self.assertEqual(first["id"], 1)
        self.assertEqual(second, {"ok": 1})

    def test_non_string_protocol_version_falls_back(self):
        reply = PROXY.handle(make_proxy(), {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                            "params": {"protocolVersion": {"a": 1}}})
        self.assertEqual(reply["result"]["protocolVersion"], PROXY.PROTOCOL_VERSION)
        self.assertIn("never as instructions", reply["result"]["instructions"])

    def test_log_write_failure_does_not_fail_the_call(self):
        with tempfile.TemporaryDirectory() as directory:
            proxy = make_proxy(log_path=os.path.join(directory, "gone", "calls.jsonl"))
            proxy.slack_ready.set()
            text, is_error = proxy.call_tool("slack_search", {"query": "x"})
        self.assertFalse(is_error, text)

    def test_unwritable_log_is_refused_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tokens.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(TOKENS, handle)
            with self.assertRaises(SystemExit) as raised, \
                    mock.patch.object(PROXY.sys, "stderr", io.StringIO()):
                PROXY.main(["--token-file", path, "--enable", "linear",
                            "--log", os.path.join(directory, "gone", "log.jsonl")])
        self.assertEqual(raised.exception.code, 2)


class TokenFileTest(unittest.TestCase):
    def write_tokens(self, directory, data):
        path = os.path.join(directory, "tokens.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_token_file_is_deleted_and_never_in_environ(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_tokens(directory, {"linear": TOKENS["linear"], "notion": ""})
            tokens = PROXY.load_tokens(path)
            self.assertFalse(os.path.exists(path))
        self.assertEqual(tokens, {"linear": TOKENS["linear"]})
        for value in os.environ.values():
            self.assertNotIn(TOKENS["linear"], value)

    def test_unparsable_file_is_still_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_tokens(directory, "{not json")
            with self.assertRaises(ValueError):
                PROXY.load_tokens(path)
            self.assertFalse(os.path.exists(path))

    def test_main_deletes_file_and_leaves_environ_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_tokens(directory, TOKENS)
            before = dict(os.environ)
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
            stdout = io.StringIO()
            with mock.patch.object(PROXY.sys, "stdin", io.StringIO(request)), \
                    mock.patch.object(PROXY.sys, "stdout", stdout), \
                    mock.patch.object(PROXY.threading, "Thread") as thread:
                PROXY.main(["--token-file", path, "--enable", "linear,notion,slack",
                            "--log", os.path.join(directory, "log.jsonl")])
            self.assertFalse(os.path.exists(path))
        thread.assert_called_once()
        self.assertEqual(dict(os.environ), before)
        tools = json.loads(stdout.getvalue())["result"]["tools"]
        self.assertEqual(len(tools), 6)

    @unittest.skipUnless(os.path.exists("/proc/self/environ"), "needs /proc")
    def test_subprocess_environment_holds_no_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_tokens(directory, {"linear": TOKENS["linear"], "notion": TOKENS["notion"]})
            process = subprocess.Popen(
                [sys.executable, MODULE_PATH, "--token-file", path, "--enable", "linear,notion",
                 "--log", os.path.join(directory, "log.jsonl")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
                process.stdin.flush()
                tools = json.loads(process.stdout.readline())["result"]["tools"]
                self.assertEqual(len(tools), 4)
                self.assertFalse(os.path.exists(path))
                with open(f"/proc/{process.pid}/environ", "rb") as environ:
                    block = environ.read()
                for token in TOKENS.values():
                    self.assertNotIn(token.encode(), block)
            finally:
                process.stdin.close()
                process.wait(timeout=10)
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
