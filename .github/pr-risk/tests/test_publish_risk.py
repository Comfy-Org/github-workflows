"""Unit tests for the PR risk publisher (publish_risk.py).

These cover the invariants the ticket names explicitly, none of which the
grader can guarantee on its own:

  * exactly one `risk:R*` label at any time; stale tiers removed
  * an ungradable PR carries NO tier and is never defaulted to `risk:R0`
  * the Check Run's conclusion is always `neutral` — nothing is ever gated
  * the comment is sticky: updated in place, never duplicated per push
  * a reviewer's "this grade is wrong" tick survives a re-grade, and toggling
    it drives the `risk-grade-disputed` label

The GitHub API is injected as a recording fake, so nothing here touches the
network.
"""

import argparse
import json
import os
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grade_risk  # noqa: E402
import publish_risk  # noqa: E402


class FakeApi:
    """Records calls and serves canned GETs.

    `labels` is the PR's live label set (mutated by the calls under test, so a
    sequence of publishes reads as a real reconciliation would).
    """

    def __init__(self, labels=None, comments=None, head_sha="sha1", base_ref="main"):
        self.labels = list(labels or [])
        # Comments default to the GITHUB_TOKEN bot identity: ours are posted by
        # the app token or by github-actions[bot], and find_sticky requires the
        # type AND the login to match.
        self.comments = [
            {"user": {"type": "Bot", "login": publish_risk.DEFAULT_AUTHOR_LOGIN}, **c}
            for c in list(comments or [])
        ]
        self.head_sha = head_sha
        self.base_ref = base_ref
        self.calls = []
        self.next_comment_id = 1000

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "GET" and "/labels" in path:
            # Paginated like the real endpoint, so a pager bug shows up here.
            return self._page([{"name": n} for n in self.labels], path)
        if method == "GET" and "/pulls/" in path:
            return {"head": {"sha": self.head_sha}, "base": {"ref": self.base_ref}}
        if method == "GET" and "/issues/comments/" in path:
            # Single-comment read by id — `upsert_sticky` re-reads the body
            # here immediately before it PATCHes.
            cid = int(path.rsplit("/", 1)[1])
            return next((c for c in self.comments if c.get("id") == cid), {})
        if method == "GET" and "/comments" in path:
            return self._page(list(self.comments), path)
        if method == "POST" and path.endswith("/labels"):
            for n in body["labels"]:
                if n not in self.labels:
                    self.labels.append(n)
            return [{"name": n} for n in self.labels]
        if method == "DELETE" and "/labels/" in path:
            name = path.rsplit("/labels/", 1)[1].replace("%3A", ":")
            self.labels = [n for n in self.labels if n != name]
            return {}
        if method == "POST" and path.endswith("/check-runs"):
            return {"id": 42}
        if method == "POST" and "/comments" in path:
            self.next_comment_id += 1
            self.comments.append(
                {
                    "id": self.next_comment_id,
                    "body": body["body"],
                    "user": {
                        "type": "Bot",
                        "login": publish_risk.DEFAULT_AUTHOR_LOGIN,
                    },
                }
            )
            return {"id": self.next_comment_id}
        if method == "PATCH" and "/comments/" in path:
            cid = int(path.rsplit("/", 1)[1])
            for c in self.comments:
                if c["id"] == cid:
                    c["body"] = body["body"]
            return {"id": cid}
        return {}

    @staticmethod
    def _page(items, path):
        """Serve `items` the way GitHub pages a list endpoint.

        Honours `direction=desc` so `find_sticky`'s newest-first scan is
        exercised for real: served ascending regardless, the 1000-comment
        pagination test below would pass whichever direction the code asked
        for, which is the bug it exists to catch.
        """
        query = parse_qs(urlparse(path).query)
        if query.get("direction", [""])[0] == "desc":
            items = list(reversed(items))
        per_page = min(int(query.get("per_page", ["30"])[0]), 100)
        page = int(query.get("page", ["1"])[0])
        start = (page - 1) * per_page
        return items[start : start + per_page]

    def of(self, method, needle):
        return [c for c in self.calls if c[0] == method and needle in c[1]]


class MarkerAgreementTest(unittest.TestCase):
    def test_grader_and_publisher_agree_on_the_sticky_marker(self):
        """If these drift, every push posts a NEW comment instead of updating
        the sticky one — the exact duplication the ticket forbids. pr-risk.yml
        deliberately passes no --marker so these defaults are the only source."""
        self.assertEqual(grade_risk.DEFAULT_MARKER, publish_risk.MARKER)

    def test_the_publishers_own_checkbox_line_is_the_one_it_reads_back(self):
        """publish_risk renders its own fallback bodies (a malformed report),
        so its copy of the checkbox must match the regexes it later greps with
        AND the form grade_risk renders — or a dispute registered on a normal
        comment is silently dropped the first time a fallback body overwrites
        it, leaving `risk-grade-disputed` stuck on and unclearable."""
        self.assertTrue(publish_risk.UNCHECKED_RE.search(publish_risk.DISPUTE_LINE))
        self.assertIsNone(publish_risk.CHECKED_RE.search(publish_risk.DISPUTE_LINE))
        rendered = grade_risk.render_comment(
            grade_risk.unknown_report("nope"), publish_risk.MARKER
        )
        self.assertIn(publish_risk.DISPUTE_LINE, rendered)

    def test_the_shell_last_resort_body_carries_a_readable_checkbox(self):
        """grade-risk.sh writes that body with printf when Python itself is
        unavailable, so it is the one copy no Python test can reach by import."""
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "grade-risk.sh",
        )
        with open(script, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn(publish_risk.DISPUTE_LINE, source)
        self.assertIn(publish_risk.MARKER, source)


class ApiErrorWrappingTest(unittest.TestCase):
    """Every failure must leave `api()` as ApiError.

    `cmd_publish` attempts its three publications independently, and every one
    of those guards is `except ApiError`. Anything else escapes all three and
    aborts the publish after the Check Run has already announced a tier —
    which is reachable with no HTTP error at all.
    """

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _api_with(self, payload):
        real = publish_risk.urllib.request.urlopen
        publish_risk.urllib.request.urlopen = lambda *a, **k: self._Resp(payload)
        try:
            return publish_risk.api("GET", "/repos/o/r/pulls/1", "tok")
        finally:
            publish_risk.urllib.request.urlopen = real

    def test_a_non_json_body_raises_api_error(self):
        """A proxy's HTML error page, or a truncated response."""
        with self.assertRaises(publish_risk.ApiError) as ctx:
            self._api_with(b"<html>502 Bad Gateway</html>")
        self.assertIn("not JSON", str(ctx.exception))

    def test_a_read_timeout_raises_api_error(self):
        """`resp.read()` raises TimeoutError, which URLError does not cover."""
        with self.assertRaises(publish_risk.ApiError):
            self._api_with(TimeoutError("timed out"))

    def test_a_well_formed_body_still_round_trips(self):
        self.assertEqual(self._api_with(b'{"ok": true}'), {"ok": True})

    def test_an_empty_body_is_an_empty_dict(self):
        self.assertEqual(self._api_with(b""), {})


class ReconcileLabelsTest(unittest.TestCase):
    def test_adds_the_desired_tier(self):
        api = FakeApi(labels=["bug"])
        res = publish_risk.reconcile_labels(api, "o/r", 1, "risk:R2")
        self.assertEqual(res["added"], ["risk:R2"])
        self.assertIn("risk:R2", api.labels)

    def test_replaces_a_stale_tier_leaving_exactly_one(self):
        """A PR that grows from R0 into R3 must carry risk:R3 and ONLY risk:R3."""
        api = FakeApi(labels=["risk:R0", "enhancement"])
        res = publish_risk.reconcile_labels(api, "o/r", 1, "risk:R3")
        self.assertEqual(res["removed"], ["risk:R0"])
        self.assertEqual([n for n in api.labels if n.startswith("risk:R")], ["risk:R3"])

    def test_collapses_multiple_stale_tiers(self):
        api = FakeApi(labels=["risk:R0", "risk:R1", "risk:R2"])
        publish_risk.reconcile_labels(api, "o/r", 1, "risk:R1")
        self.assertEqual([n for n in api.labels if n.startswith("risk:R")], ["risk:R1"])

    def test_already_correct_is_a_no_op(self):
        api = FakeApi(labels=["risk:R2"])
        res = publish_risk.reconcile_labels(api, "o/r", 1, "risk:R2")
        self.assertEqual(
            res,
            {
                "added": [],
                "removed": [],
                "failed_removals": [],
                # The label set as READ, so cmd_publish can see whether a
                # dispute is on record without a second page walk.
                "names": ["risk:R2"],
            },
        )
        self.assertEqual(api.of("POST", "/labels"), [])
        self.assertEqual(api.of("DELETE", "/labels/"), [])

    def test_a_non_risk_label_is_refused_not_applied(self):
        """`desired` arrives verbatim from the report artifact and this is the
        privileged job. Reconciliation only DELETES names matching the regex,
        so a bogus label would never be cleaned up by any later run.

        The unsupported tiers and the trailing newline are the sharp cases:
        `^risk:R[0-9]+$` accepted `risk:R99`, and `$` also matches before a
        trailing newline, so `"risk:R2\\n"` passed and spliced a raw newline
        into the label DELETE path. The non-strings are sharper still — they
        raised `TypeError` out of the regex, which is NOT what cmd_publish
        catches, so the whole publish died instead of degrading to unknown."""
        api = FakeApi()
        bogus_values = (
            "lgtm", "risk-assessment-done", "risk:Rx", "",
            "risk:R4", "risk:R99", "risk:R00", "risk:R2\n", " risk:R2",
            2, None.__class__, ["risk:R2"], {"name": "risk:R2"}, True,
        )
        for bogus in bogus_values:
            with self.assertRaises(ValueError, msg=repr(bogus)):
                publish_risk.reconcile_labels(api, "o/r", 1, bogus)
        self.assertEqual(api.of("POST", "/labels"), [])

    def test_every_supported_tier_is_applicable(self):
        for tier in range(grade_risk.MAX_TIER + 1):
            api = FakeApi()
            publish_risk.reconcile_labels(api, "o/r", 1, f"risk:R{tier}")
            self.assertIn(f"risk:R{tier}", api.labels)

    def test_a_legacy_out_of_range_tier_is_still_cleaned_up(self):
        """The removal side stays broader than the applicable set on purpose:
        a `risk:R7` written by an older revision must not sit on the PR forever
        beside the current tier."""
        api = FakeApi(labels=["risk:R7"])
        res = publish_risk.reconcile_labels(api, "o/r", 1, "risk:R2")
        self.assertEqual(res["removed"], ["risk:R7"])
        self.assertEqual([n for n in api.labels if n.startswith("risk:R")], ["risk:R2"])

    def test_the_desired_label_lands_even_if_a_stale_delete_fails(self):
        """A DELETE can 404 (a concurrent run got there first) or 403. Deleting
        first meant that error propagated and the PR was left with NO tier."""
        api = FakeApi(labels=["risk:R0"])

        def flaky(method, path, body):
            if method == "DELETE":
                raise publish_risk.ApiError("DELETE -> HTTP 404: not found")
            return api(method, path, body)

        res = publish_risk.reconcile_labels(flaky, "o/r", 1, "risk:R3")
        self.assertEqual(res["added"], ["risk:R3"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(len(res["failed_removals"]), 1)
        self.assertIn("risk:R3", api.labels)

    def test_the_desired_label_is_added_before_stale_ones_are_removed(self):
        api = FakeApi(labels=["risk:R0"])
        publish_risk.reconcile_labels(api, "o/r", 1, "risk:R3")
        methods = [m for m, p, _ in api.calls if "/labels" in p and m != "GET"]
        self.assertEqual(methods, ["POST", "DELETE"])

    def test_unknown_removes_every_tier_and_adds_none(self):
        """Unknown is published as unknown — never silently defaulted to R0."""
        api = FakeApi(labels=["risk:R2", "bug"])
        res = publish_risk.reconcile_labels(api, "o/r", 1, None)
        self.assertEqual(res["added"], [])
        self.assertEqual(res["removed"], ["risk:R2"])
        self.assertEqual(api.labels, ["bug"])

    def test_leaves_unrelated_risk_shaped_labels_alone(self):
        api = FakeApi(labels=["risk-assessment-done", "risk:Rx", "riskR1"])
        publish_risk.reconcile_labels(api, "o/r", 1, "risk:R1")
        for name in ("risk-assessment-done", "risk:Rx", "riskR1"):
            self.assertIn(name, api.labels)

    def test_colon_is_encoded_in_the_delete_path(self):
        api = FakeApi(labels=["risk:R0"])
        publish_risk.reconcile_labels(api, "o/r", 1, "risk:R3")
        self.assertIn("risk%3AR0", api.of("DELETE", "/labels/")[0][1])

    def test_a_stale_tier_past_the_first_page_is_still_removed(self):
        """`/labels` defaults to 30 per page. A stale risk:R* hiding on page 2
        must not survive, or the PR carries two tiers at once."""
        api = FakeApi(labels=[f"topic-{n}" for n in range(40)] + ["risk:R0"])
        res = publish_risk.reconcile_labels(api, "o/r", 1, "risk:R3")
        self.assertEqual(res["removed"], ["risk:R0"])
        self.assertEqual([n for n in api.labels if n.startswith("risk:R")], ["risk:R3"])

    def test_pager_asks_for_full_pages(self):
        api = FakeApi(labels=["risk:R0"])
        publish_risk.reconcile_labels(api, "o/r", 1, "risk:R1")
        self.assertIn("per_page=100", api.of("GET", "/labels")[0][1])


class CheckRunTest(unittest.TestCase):
    def test_conclusion_is_always_neutral(self):
        """The 'nothing is gated' guarantee: a neutral check cannot fail a PR."""
        api = FakeApi()
        publish_risk.publish_check_run(api, "o/r", "deadbeef", "Risk: R3", "why")
        _, path, body = api.of("POST", "/check-runs")[0]
        self.assertEqual(body["conclusion"], "neutral")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["head_sha"], "deadbeef")

    def test_carries_the_tier_and_the_reason(self):
        api = FakeApi()
        publish_risk.publish_check_run(api, "o/r", "sha", "Risk: R3", "because migrations")
        body = api.of("POST", "/check-runs")[0][2]
        self.assertEqual(body["output"]["title"], "Risk: R3")
        self.assertIn("migrations", body["output"]["summary"])

    def test_oversized_output_is_truncated_not_rejected(self):
        api = FakeApi()
        publish_risk.publish_check_run(api, "o/r", "sha", "T" * 400, "S" * 70000)
        body = api.of("POST", "/check-runs")[0][2]
        self.assertLessEqual(len(body["output"]["title"]), 255)
        self.assertLessEqual(len(body["output"]["summary"]), 65535)


class StickyCommentTest(unittest.TestCase):
    def test_first_publish_creates_one_comment(self):
        api = FakeApi()
        res = publish_risk.upsert_sticky(api, "o/r", 1, publish_risk.MARKER + "\nbody")
        self.assertEqual(res["action"], "created")
        self.assertEqual(len(api.of("POST", "/comments")), 1)

    def test_re_grade_updates_in_place_and_never_duplicates(self):
        api = FakeApi(comments=[{"id": 7, "body": publish_risk.MARKER + "\nold"}])
        res = publish_risk.upsert_sticky(api, "o/r", 1, publish_risk.MARKER + "\nnew")
        self.assertEqual(res["action"], "updated")
        self.assertEqual(api.of("POST", "/comments"), [])
        self.assertEqual(api.comments[0]["body"], publish_risk.MARKER + "\nnew")

    def test_someone_elses_comment_is_not_hijacked(self):
        api = FakeApi(comments=[{"id": 7, "body": "looks good to me"}])
        res = publish_risk.upsert_sticky(api, "o/r", 1, publish_risk.MARKER + "\nnew")
        self.assertEqual(res["action"], "created")
        self.assertEqual(api.comments[0]["body"], "looks good to me")

    def test_a_human_comment_carrying_the_marker_is_not_hijacked(self):
        """The marker is public (README + every rendered comment), so a PR
        author can pre-post one. Matching it alone would PATCH their comment
        away and hand them control of the preserved dispute checkbox."""
        api = FakeApi(
            comments=[
                {
                    "id": 7,
                    "user": {"type": "User"},
                    "body": publish_risk.MARKER
                    + "\n- [x] **This grade is wrong**\nmine, not yours",
                }
            ]
        )
        fresh = grade_risk.render_comment(
            grade_risk.grade(grade_risk.parse_numstat("1\t0\ta.go\0")),
            publish_risk.MARKER,
        )
        res = publish_risk.upsert_sticky(api, "o/r", 1, fresh)
        self.assertEqual(res["action"], "created")
        self.assertEqual(api.of("PATCH", "/comments/"), [])
        self.assertIn("mine, not yours", api.comments[0]["body"])
        # ...and their forged tick did not leak into our fresh comment either.
        self.assertIn("- [ ] **This grade is wrong**", api.comments[-1]["body"])

    def test_a_registered_disagreement_survives_a_re_grade(self):
        """A push must not silently un-tick a reviewer's disagreement."""
        report = grade_risk.grade(
            grade_risk.parse_numstat("3\t1\tsvc/auth/a.go\0")
        )
        checked = grade_risk.render_comment(report, publish_risk.MARKER, disputed=True)
        fresh = grade_risk.render_comment(report, publish_risk.MARKER)
        self.assertIn("- [ ] **This grade is wrong**", fresh)

        api = FakeApi(comments=[{"id": 7, "body": checked}])
        publish_risk.upsert_sticky(api, "o/r", 1, fresh)
        self.assertIn("- [x] **This grade is wrong**", api.comments[0]["body"])
        self.assertNotIn("- [ ] **This grade is wrong**", api.comments[0]["body"])

    def test_another_installed_bots_comment_is_not_adopted(self):
        """Bot TYPE is not identity. Any other GitHub App on the repo is also a
        Bot, and comments are scanned in ascending id order — so a bot that
        quotes our sticky comment would be adopted PERMANENTLY: every re-grade
        PATCHes over its body (or 403s forever), and `upsert_sticky` reads the
        dispute checkbox back out of a foreign comment."""
        ours = grade_risk.render_comment(
            grade_risk.grade(grade_risk.parse_numstat("1\t0\ta.go\0")),
            publish_risk.MARKER,
        )
        api = FakeApi(
            comments=[
                {
                    "id": 1,
                    "user": {"type": "Bot", "login": "some-other-app[bot]"},
                    "body": "For reference:\n\n" + ours,
                }
            ]
        )
        res = publish_risk.upsert_sticky(api, "o/r", 1, ours)
        self.assertEqual(res["action"], "created")
        self.assertEqual(api.of("PATCH", "/comments/"), [])
        self.assertIn("For reference:", api.comments[0]["body"])

    def test_the_configured_app_login_is_adopted(self):
        api = FakeApi(
            comments=[
                {
                    "id": 1,
                    "user": {"type": "Bot", "login": "cloud-code-bot[bot]"},
                    "body": publish_risk.MARKER + "\nold",
                }
            ]
        )
        res = publish_risk.upsert_sticky(
            api, "o/r", 1, publish_risk.MARKER + "\nnew", ["cloud-code-bot[bot]"]
        )
        self.assertEqual(res["action"], "updated")

    def test_a_sibling_workflow_quoting_us_under_our_own_login_is_skipped(self):
        """Login matching cannot see this one: every other GITHUB_TOKEN
        workflow in the repo posts as github-actions[bot] too. The marker has
        to be the FIRST thing in the body, which a quote never is."""
        ours = publish_risk.MARKER + "\nour body"
        api = FakeApi(comments=[{"id": 1, "body": "Bot digest:\n\n> " + ours}])
        res = publish_risk.upsert_sticky(api, "o/r", 1, ours)
        self.assertEqual(res["action"], "created")
        self.assertEqual(api.of("PATCH", "/comments/"), [])

    def test_a_comment_posted_before_an_app_was_configured_is_still_ours(self):
        """github-actions[bot] is ALWAYS accepted, so turning on bot_app_id
        adopts the existing comment rather than starting a duplicate."""
        api = FakeApi(comments=[{"id": 1, "body": publish_risk.MARKER + "\nold"}])
        res = publish_risk.upsert_sticky(
            api, "o/r", 1, publish_risk.MARKER + "\nnew", ["cloud-code-bot[bot]"]
        )
        self.assertEqual(res["action"], "updated")

    def test_an_unticked_box_stays_unticked(self):
        report = grade_risk.grade(grade_risk.parse_numstat("3\t1\ta.md\0"))
        fresh = grade_risk.render_comment(report, publish_risk.MARKER)
        api = FakeApi(comments=[{"id": 7, "body": fresh}])
        publish_risk.upsert_sticky(api, "o/r", 1, fresh)
        self.assertIn("- [ ] **This grade is wrong**", api.comments[0]["body"])

    def test_a_tick_landing_after_the_scan_is_not_overwritten(self):
        """find_sticky reads the body, then the PATCH writes it back. A tick
        registered inside that window was silently lost — and the `edited`
        event our own PATCH fires then drove cmd_dispute to CLEAR the label.
        The body is re-read by id immediately before the write."""
        report = grade_risk.grade(grade_risk.parse_numstat("3\t1\ta.md\0"))
        fresh = grade_risk.render_comment(report, publish_risk.MARKER)
        api = FakeApi(comments=[{"id": 7, "body": fresh}])

        def racing(method, path, body):
            # The reviewer ticks the box after the list scan and before the
            # single-comment re-read, which is the whole window.
            if method == "GET" and "/issues/1/comments" in path:
                out = api(method, path, body)
                api.comments[0]["body"] = grade_risk.render_comment(
                    report, publish_risk.MARKER, disputed=True
                )
                return out
            return api(method, path, body)

        publish_risk.upsert_sticky(racing, "o/r", 1, fresh)
        self.assertIn("- [x] **This grade is wrong**", api.comments[0]["body"])

    def test_the_dispute_label_re_asserts_a_tick_the_body_lost(self):
        """The label is the durable record: cmd_dispute wrote it from an
        earlier tick, so even a body that comes back unticked (a lost race, or
        a fallback body that overwrote it) must not silently drop the dispute
        — and the re-ticked body keeps our own `edited` event from clearing
        the label."""
        report = grade_risk.grade(grade_risk.parse_numstat("3\t1\ta.md\0"))
        fresh = grade_risk.render_comment(report, publish_risk.MARKER)
        api = FakeApi(comments=[{"id": 7, "body": fresh}])
        publish_risk.upsert_sticky(api, "o/r", 1, fresh, (), True)
        self.assertIn("- [x] **This grade is wrong**", api.comments[0]["body"])

    def test_a_failed_body_re_read_still_writes_the_comment(self):
        """A 5xx on the re-read must not cost the whole publication; fall back
        to the body the scan already returned."""
        report = grade_risk.grade(grade_risk.parse_numstat("3\t1\ta.md\0"))
        ticked = grade_risk.render_comment(report, publish_risk.MARKER, disputed=True)
        fresh = grade_risk.render_comment(report, publish_risk.MARKER)
        api = FakeApi(comments=[{"id": 7, "body": ticked}])

        def flaky(method, path, body):
            if method == "GET" and "/issues/comments/" in path:
                raise publish_risk.ApiError("GET -> HTTP 502")
            return api(method, path, body)

        res = publish_risk.upsert_sticky(flaky, "o/r", 1, fresh)
        self.assertEqual(res["action"], "updated")
        self.assertIn("- [x] **This grade is wrong**", api.comments[0]["body"])

    def test_the_sticky_is_found_on_a_pr_with_more_than_a_thousand_comments(self):
        """The scan is bounded at 10 pages x 100. Ascending, a sticky past that
        cap made every re-grade POST a NEW comment — which itself landed past
        the cap, so the next run repeated it forever, each time resetting the
        dispute tick. Newest-first, a comment we created stays reachable."""
        ours = grade_risk.render_comment(
            grade_risk.grade(grade_risk.parse_numstat("1\t0\ta.go\0")),
            publish_risk.MARKER,
        )
        chatter = [{"id": n, "body": f"comment {n}"} for n in range(1200)]
        api = FakeApi(comments=chatter + [{"id": 9999, "body": ours}])
        res = publish_risk.upsert_sticky(api, "o/r", 1, ours)
        self.assertEqual(res["action"], "updated")
        self.assertEqual(api.of("POST", "/comments"), [])

    def test_the_scan_asks_for_newest_first(self):
        api = FakeApi()
        publish_risk.find_sticky(api, "o/r", 1)
        path = api.of("GET", "/issues/1/comments")[0][1]
        self.assertIn("direction=desc", path)
        self.assertIn("sort=created", path)


class DisputeLabelTest(unittest.TestCase):
    def test_ticking_adds_the_label(self):
        api = FakeApi()
        self.assertEqual(
            publish_risk.set_dispute_label(api, "o/r", 1, True)["action"], "added"
        )
        self.assertIn(publish_risk.DISPUTE_LABEL, api.labels)

    def test_unticking_removes_it(self):
        api = FakeApi(labels=[publish_risk.DISPUTE_LABEL])
        self.assertEqual(
            publish_risk.set_dispute_label(api, "o/r", 1, False)["action"], "removed"
        )
        self.assertNotIn(publish_risk.DISPUTE_LABEL, api.labels)

    def test_no_change_writes_nothing(self):
        api = FakeApi(labels=[publish_risk.DISPUTE_LABEL])
        self.assertEqual(
            publish_risk.set_dispute_label(api, "o/r", 1, True)["action"], "unchanged"
        )
        self.assertEqual(api.of("POST", "/labels"), [])
        self.assertEqual(api.of("DELETE", "/labels/"), [])

    def test_the_label_is_found_past_the_first_page(self):
        """Same 30-per-page default as reconcile_labels: un-ticking the box on
        a heavily-labelled PR must still remove risk-grade-disputed."""
        api = FakeApi(
            labels=[f"topic-{n}" for n in range(40)] + [publish_risk.DISPUTE_LABEL]
        )
        self.assertEqual(
            publish_risk.set_dispute_label(api, "o/r", 1, False)["action"], "removed"
        )
        self.assertNotIn(publish_risk.DISPUTE_LABEL, api.labels)

    def test_checkbox_regex_accepts_the_rendered_forms(self):
        for line in (
            "- [x] **This grade is wrong** — trailing prose",
            "- [X] **This grade is wrong**",
            "* [x] **This grade is wrong**",
        ):
            self.assertTrue(publish_risk.CHECKED_RE.search(line), line)
        self.assertIsNone(
            publish_risk.CHECKED_RE.search("- [ ] **This grade is wrong**")
        )


def _write(dirpath, name, text):
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class CmdPublishTest(unittest.TestCase):
    """End-to-end over cmd_publish, the shape pr-risk.yml actually invokes."""

    def _artifacts(self, numstat):
        d = tempfile.mkdtemp()
        report = grade_risk.grade(grade_risk.parse_numstat(numstat))
        title, summary = grade_risk.render_check(report)
        return (
            d,
            _write(d, "risk-report.json", json.dumps(report)),
            _write(d, "risk-check.md", f"{title}\n\n{summary}\n"),
            _write(
                d,
                "risk-comment.md",
                grade_risk.render_comment(report, publish_risk.MARKER),
            ),
            report,
        )

    def _args(self, report, check, comment, mode, base_ref="main"):
        return argparse.Namespace(
            repo="o/r", pr=1, sha="sha1", report=report, check=check,
            comment=comment, mode=mode, base_ref=base_ref, author_login=[],
        )

    def test_publish_mode_writes_all_three_surfaces(self):
        _, rep, chk, com, report = self._artifacts("3\t1\tsvc/auth/a.go\0")
        api = FakeApi()
        rc = publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), api)
        self.assertEqual(rc, 0)
        self.assertEqual(len(api.of("POST", "/check-runs")), 1)
        self.assertIn("risk:R3", api.labels)
        self.assertEqual(len(api.of("POST", "/comments")), 1)

    def test_shadow_mode_publishes_only_the_check_run(self):
        """Default mode changes no reviewer-facing surface — the feature flag."""
        _, rep, chk, com, _ = self._artifacts("3\t1\tsvc/auth/a.go\0")
        api = FakeApi()
        publish_risk.cmd_publish(self._args(rep, chk, com, "shadow"), api)
        self.assertEqual(len(api.of("POST", "/check-runs")), 1)
        self.assertEqual(api.labels, [])
        self.assertEqual(api.of("POST", "/comments"), [])
        self.assertEqual(api.of("DELETE", "/labels/"), [])

    def test_an_unknown_report_publishes_a_check_run_but_no_label(self):
        d = tempfile.mkdtemp()
        report = grade_risk.unknown_report("git exploded")
        title, summary = grade_risk.render_check(report)
        rep = _write(d, "risk-report.json", json.dumps(report))
        chk = _write(d, "risk-check.md", f"{title}\n\n{summary}\n")
        com = _write(
            d, "risk-comment.md", grade_risk.render_comment(report, publish_risk.MARKER)
        )
        api = FakeApi(labels=["risk:R0"])
        publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), api)
        self.assertEqual(api.of("POST", "/labels"), [])
        self.assertNotIn("risk:R0", api.labels)
        self.assertIn("unknown", api.of("POST", "/check-runs")[0][2]["output"]["title"].lower())

    def test_comment_can_be_disabled_while_the_label_still_lands(self):
        _, rep, chk, _com, _ = self._artifacts("3\t1\ta.md\0")
        api = FakeApi()
        publish_risk.cmd_publish(self._args(rep, chk, "", "publish"), api)
        self.assertIn("risk:R0", api.labels)
        self.assertEqual(api.of("POST", "/comments"), [])

    def test_a_superseded_run_does_not_overwrite_a_newer_grade(self):
        """cancel-in-progress bounds but does not eliminate a delayed older run
        landing last. Its Check Run is per-commit and still publishes; its
        label and comment must not stomp the newer head's."""
        _, rep, chk, com, _ = self._artifacts("30\t10\tdb/migrations/001.sql\0")
        api = FakeApi(labels=["risk:R0"], head_sha="newer-sha")
        rc = publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), api)
        self.assertEqual(rc, 0)
        self.assertEqual(len(api.of("POST", "/check-runs")), 1)
        self.assertEqual(api.labels, ["risk:R0"])
        self.assertEqual(api.of("POST", "/labels"), [])
        self.assertEqual(api.of("DELETE", "/labels/"), [])
        self.assertEqual(api.of("POST", "/comments"), [])

    def test_an_unreadable_head_sha_still_publishes(self):
        """A failed staleness lookup must degrade to publishing, not to
        silently dropping an otherwise good grade."""
        _, rep, chk, com, _ = self._artifacts("30\t10\tdb/migrations/001.sql\0")

        api = FakeApi()

        def flaky(method, path, body):
            if method == "GET" and "/pulls/" in path:
                raise publish_risk.ApiError("GET /pulls/1 -> HTTP 502")
            return api(method, path, body)

        publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), flaky)
        self.assertIn("risk:R3", api.labels)

    def test_a_retargeted_pr_does_not_get_its_old_base_grade_published(self):
        """Retargeting changes the three-dot diff — and so the grade — without
        moving the head SHA and without firing `synchronize`, so a head-only
        staleness check let a grade computed against the old base sail through
        and an author could hold a low tier by rebasing the base away."""
        _, rep, chk, com, _ = self._artifacts("30\t10\tdb/migrations/001.sql\0")
        api = FakeApi(labels=["risk:R0"], base_ref="release/2.0")
        publish_risk.cmd_publish(
            self._args(rep, chk, com, "publish", base_ref="main"), api
        )
        self.assertEqual(len(api.of("POST", "/check-runs")), 1)
        self.assertEqual(api.labels, ["risk:R0"])
        self.assertEqual(api.of("POST", "/comments"), [])

    def test_an_unchanged_base_publishes_normally(self):
        _, rep, chk, com, _ = self._artifacts("30\t10\tdb/migrations/001.sql\0")
        api = FakeApi(base_ref="main")
        publish_risk.cmd_publish(
            self._args(rep, chk, com, "publish", base_ref="main"), api
        )
        self.assertIn("risk:R3", api.labels)

    def test_an_unreadable_base_ref_still_publishes(self):
        """Same degrade-to-publishing rule as the head SHA: a staleness lookup
        that returns nothing must not drop an otherwise good grade."""
        _, rep, chk, com, _ = self._artifacts("30\t10\tdb/migrations/001.sql\0")
        api = FakeApi(base_ref=None)
        publish_risk.cmd_publish(
            self._args(rep, chk, com, "publish", base_ref="main"), api
        )
        self.assertIn("risk:R3", api.labels)

    def test_a_failed_check_run_does_not_suppress_the_label_and_comment(self):
        """Three independent surfaces, three independent failure modes. Letting
        the first abort the rest is how a PR ends up with a Check Run
        announcing one tier and a label and comment describing another."""
        _, rep, chk, com, _ = self._artifacts("30\t10\tdb/migrations/001.sql\0")
        api = FakeApi()

        def flaky(method, path, body):
            if method == "POST" and path.endswith("/check-runs"):
                raise publish_risk.ApiError("POST /check-runs -> HTTP 403")
            return api(method, path, body)

        rc = publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), flaky)
        self.assertEqual(rc, 1)  # degraded, so pr-risk.yml says so
        self.assertIn("risk:R3", api.labels)
        self.assertEqual(len(api.of("POST", "/comments")), 1)

    def test_a_failed_label_reconcile_does_not_suppress_the_comment(self):
        """reconcile_labels raises ApiError from the paged GET and from the
        POST (403 scope gap, secondary rate limit, 5xx), not just ValueError.
        Only ValueError was caught, so those aborted cmd_publish before the
        sticky comment — leaving the comment describing an older grade while
        the Check Run had already announced the new tier."""
        _, rep, chk, com, _ = self._artifacts("30\t10\tdb/migrations/001.sql\0")
        api = FakeApi()

        def flaky(method, path, body):
            if "/labels" in path:
                raise publish_risk.ApiError("GET /labels -> HTTP 502")
            return api(method, path, body)

        rc = publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), flaky)
        self.assertEqual(rc, 1)
        self.assertEqual(len(api.of("POST", "/check-runs")), 1)
        self.assertEqual(len(api.of("POST", "/comments")), 1)

    def test_a_malformed_label_makes_all_three_surfaces_say_unknown(self):
        """Rejecting the tier at labelling time left the Check Run and the
        comment still asserting it — no label, but two surfaces announcing a
        tier the publisher had just refused to trust."""
        d = tempfile.mkdtemp()
        report = grade_risk.grade(grade_risk.parse_numstat("3\t1\tsvc/auth/a.go\0"))
        report["label"] = "risk:R9"  # a tier this publisher may not create
        title, summary = grade_risk.render_check(report)
        rep = _write(d, "risk-report.json", json.dumps(report))
        chk = _write(d, "risk-check.md", f"{title}\n\n{summary}\n")
        com = _write(
            d, "risk-comment.md", grade_risk.render_comment(report, publish_risk.MARKER)
        )
        api = FakeApi(labels=["risk:R0"])
        publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), api)

        self.assertEqual(api.of("POST", "/labels"), [])
        self.assertNotIn("risk:R0", api.labels)
        # No surface may ASSERT the refused tier. Naming it as the rejected
        # value is the diagnostic, and is not the same thing as claiming it.
        check_out = api.of("POST", "/check-runs")[0][2]["output"]
        self.assertEqual(check_out["title"], "Risk: unknown")
        self.assertIn("no tier was published", check_out["summary"])
        self.assertNotIn("**Tier:", check_out["summary"])
        posted = api.of("POST", "/comments")[0][2]["body"]
        self.assertTrue(posted.startswith(publish_risk.MARKER))
        self.assertIn("Risk: **unknown**", posted)
        self.assertNotIn("`risk:R9`", posted)
        self.assertNotIn("Per-file breakdown", posted)
        # Still round-trippable: a dispute registered on this body is readable.
        self.assertTrue(publish_risk.UNCHECKED_RE.search(posted))

    def test_a_malformed_label_body_still_preserves_a_registered_dispute(self):
        d = tempfile.mkdtemp()
        report = grade_risk.grade(grade_risk.parse_numstat("3\t1\tsvc/auth/a.go\0"))
        ticked = grade_risk.render_comment(report, publish_risk.MARKER, disputed=True)
        report["label"] = ["not", "even", "a", "string"]
        title, summary = grade_risk.render_check(report)
        rep = _write(d, "risk-report.json", json.dumps(report))
        chk = _write(d, "risk-check.md", f"{title}\n\n{summary}\n")
        com = _write(d, "risk-comment.md", "ignored — the report was rejected")
        api = FakeApi(comments=[{"id": 7, "body": ticked}])
        publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), api)
        self.assertTrue(publish_risk.CHECKED_RE.search(api.comments[0]["body"]))

    def test_a_graded_report_with_no_label_is_malformed_too(self):
        """The guard was short-circuited by `desired is not None`, so a report
        marked `graded` with a missing/null label slipped through untouched:
        every tier was stripped and the summary said ungradable, while the
        Check Run and the comment — rendered from that same report and never
        re-rendered — still announced a tier."""
        for missing in ({"label": None}, {}):
            with self.subTest(missing=missing):
                d = tempfile.mkdtemp()
                report = grade_risk.grade(
                    grade_risk.parse_numstat("3\t1\tsvc/auth/a.go\0")
                )
                report.pop("label", None)
                report.update(missing)
                self.assertEqual(report["status"], "graded")
                title, summary = grade_risk.render_check(
                    grade_risk.grade(grade_risk.parse_numstat("3\t1\tsvc/auth/a.go\0"))
                )
                rep = _write(d, "risk-report.json", json.dumps(report))
                chk = _write(d, "risk-check.md", f"{title}\n\n{summary}\n")
                com = _write(d, "risk-comment.md", "a body still asserting R3")
                api = FakeApi(labels=["risk:R0"])
                publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), api)

                self.assertEqual(api.of("POST", "/labels"), [])
                self.assertNotIn("risk:R0", api.labels)
                out = api.of("POST", "/check-runs")[0][2]["output"]
                self.assertEqual(out["title"], "Risk: unknown")
                posted = api.of("POST", "/comments")[0][2]["body"]
                self.assertIn("Risk: **unknown**", posted)
                self.assertNotIn("asserting R3", posted)

    def test_a_corrupt_report_still_publishes_an_unknown_check_run(self):
        """pr-risk.yml's guard tests `-s`, not well-formedness, so a truncated
        or half-written report reaches here. JSONDecodeError escaping meant
        NOTHING was published — not even the unknown Check Run the design
        promises — and the previous head's `risk:R*` stayed on the PR."""
        d = tempfile.mkdtemp()
        report = grade_risk.grade(grade_risk.parse_numstat("3\t1\tsvc/auth/a.go\0"))
        title, summary = grade_risk.render_check(report)
        rep = _write(d, "risk-report.json", '{"schema": 1, "status": "grad')
        chk = _write(d, "risk-check.md", f"{title}\n\n{summary}\n")
        com = _write(
            d, "risk-comment.md", grade_risk.render_comment(report, publish_risk.MARKER)
        )
        api = FakeApi(labels=["risk:R0"])
        rc = publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), api)

        self.assertEqual(rc, 0)
        out = api.of("POST", "/check-runs")[0][2]["output"]
        self.assertEqual(out["title"], "Risk: unknown")
        self.assertNotIn("risk:R0", api.labels)
        self.assertEqual(api.of("POST", "/labels"), [])
        posted = api.of("POST", "/comments")[0][2]["body"]
        self.assertIn("Risk: **unknown**", posted)
        self.assertTrue(publish_risk.UNCHECKED_RE.search(posted))

    def test_a_stale_label_that_could_not_be_removed_reports_degraded(self):
        """Two `risk:R*` at once breaks the invariant this module calls
        load-bearing. Reporting it only in the step summary left rc==0, so
        pr-risk.yml's 'Note degraded mode' step never fired."""
        _, rep, chk, com, _ = self._artifacts("30\t10\tdb/migrations/001.sql\0")
        api = FakeApi(labels=["risk:R0"])

        def flaky(method, path, body):
            if method == "DELETE":
                raise publish_risk.ApiError("DELETE -> HTTP 403: forbidden")
            return api(method, path, body)

        rc = publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), flaky)
        self.assertEqual(rc, 1)
        # Both tiers really are on the PR, which is what makes it degraded...
        self.assertEqual(sorted(n for n in api.labels if n.startswith("risk:R")),
                         ["risk:R0", "risk:R3"])
        # ...and the comment still published, because rc is a report, not a halt.
        self.assertEqual(len(api.of("POST", "/comments")), 1)

    def test_a_disputed_pr_keeps_its_tick_through_a_re_grade(self):
        """End to end: the label says disputed, so the re-rendered body comes
        back ticked even though the comment we are overwriting is not."""
        _, rep, chk, com, _ = self._artifacts("3\t1\tsvc/auth/a.go\0")
        unticked = grade_risk.render_comment(
            grade_risk.grade(grade_risk.parse_numstat("3\t1\tsvc/auth/a.go\0")),
            publish_risk.MARKER,
        )
        api = FakeApi(
            labels=[publish_risk.DISPUTE_LABEL], comments=[{"id": 7, "body": unticked}]
        )
        publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), api)
        self.assertTrue(publish_risk.CHECKED_RE.search(api.comments[0]["body"]))

    def test_a_grown_pr_relabels_from_r0_to_r3_across_two_publishes(self):
        """The load-bearing re-grade property, end to end."""
        api = FakeApi()
        _, rep, chk, com, _ = self._artifacts("4\t0\tREADME.md\0")
        publish_risk.cmd_publish(self._args(rep, chk, com, "publish"), api)
        self.assertEqual([n for n in api.labels if n.startswith("risk:R")], ["risk:R0"])

        _, rep2, chk2, com2, _ = self._artifacts(
            "4\t0\tREADME.md\0" + "30\t10\tdb/migrations/001.sql\0"
        )
        publish_risk.cmd_publish(self._args(rep2, chk2, com2, "publish"), api)
        self.assertEqual([n for n in api.labels if n.startswith("risk:R")], ["risk:R3"])
        # Still exactly one sticky comment, updated in place.
        self.assertEqual(len(api.comments), 1)
        self.assertEqual(len(api.of("PATCH", "/comments/")), 1)


class CmdDisputeTest(unittest.TestCase):
    def _args(self, body_text, comment_id=""):
        d = tempfile.mkdtemp()
        return argparse.Namespace(
            repo="o/r", pr=1, body=_write(d, "b.md", body_text),
            comment_id=comment_id, author_login=[],
        )

    def test_a_ticked_box_labels_the_pr(self):
        body = grade_risk.render_comment(
            grade_risk.grade(grade_risk.parse_numstat("1\t0\ta.go\0")),
            publish_risk.MARKER,
            disputed=True,
        )
        api = FakeApi(comments=[{"id": 7, "body": body}])
        publish_risk.cmd_dispute(self._args(body, comment_id=7), api)
        self.assertIn(publish_risk.DISPUTE_LABEL, api.labels)

    def test_unticking_clears_the_label(self):
        body = grade_risk.render_comment(
            grade_risk.grade(grade_risk.parse_numstat("1\t0\ta.go\0")),
            publish_risk.MARKER,
        )
        api = FakeApi(
            labels=[publish_risk.DISPUTE_LABEL], comments=[{"id": 7, "body": body}]
        )
        publish_risk.cmd_dispute(self._args(body, comment_id=7), api)
        self.assertNotIn(publish_risk.DISPUTE_LABEL, api.labels)

    def test_an_unrelated_edited_comment_is_ignored(self):
        api = FakeApi()
        publish_risk.cmd_dispute(self._args("I edited my review comment"), api)
        self.assertEqual(api.calls, [])

    def test_a_missing_comment_id_refuses_rather_than_falling_back(self):
        """The id check was opt-in: `--comment-id` defaults to `""`, and an
        absent one fell straight through to marker-only matching — so a
        miswired caller silently downgraded the control to the very check
        `find_sticky` was hardened against. A security control that fails open
        when its input goes missing has failed."""
        ours = self._sticky_body(disputed=False)
        api = FakeApi(
            labels=[publish_risk.DISPUTE_LABEL], comments=[{"id": 7, "body": ours}]
        )
        rc = publish_risk.cmd_dispute(self._args(ours), api)
        self.assertEqual(rc, 0)
        # Nothing was written — in particular a genuine dispute was not CLEARED.
        self.assertIn(publish_risk.DISPUTE_LABEL, api.labels)
        self.assertEqual(api.of("DELETE", "/labels/"), [])
        self.assertEqual(api.of("POST", "/labels"), [])

    def _sticky_body(self, disputed):
        return grade_risk.render_comment(
            grade_risk.grade(grade_risk.parse_numstat("1\t0\ta.go\0")),
            publish_risk.MARKER,
            disputed=disputed,
        )

    def test_another_bot_quoting_the_marker_cannot_drive_the_label(self):
        """The marker is published in the README and in every rendered comment,
        so a review summarizer quoting our body would otherwise toggle the
        dispute label — including CLEARING a genuine one."""
        ours = self._sticky_body(disputed=True)
        api = FakeApi(
            labels=[publish_risk.DISPUTE_LABEL], comments=[{"id": 7, "body": ours}]
        )
        quoted = "Summary of this PR's bots:\n\n> " + ours.replace("\n", "\n> ")
        publish_risk.cmd_dispute(self._args(quoted, comment_id=99), api)
        self.assertIn(publish_risk.DISPUTE_LABEL, api.labels)
        self.assertEqual(api.of("DELETE", "/labels/"), [])

    def test_an_edit_to_our_own_sticky_comment_is_recorded(self):
        body = self._sticky_body(disputed=True)
        api = FakeApi(comments=[{"id": 7, "body": body}])
        publish_risk.cmd_dispute(self._args(body, comment_id=7), api)
        self.assertIn(publish_risk.DISPUTE_LABEL, api.labels)


if __name__ == "__main__":
    unittest.main()
