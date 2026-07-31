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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grade_risk  # noqa: E402
import publish_risk  # noqa: E402


class FakeApi:
    """Records calls and serves canned GETs.

    `labels` is the PR's live label set (mutated by the calls under test, so a
    sequence of publishes reads as a real reconciliation would).
    """

    def __init__(self, labels=None, comments=None, head_sha="sha1"):
        self.labels = list(labels or [])
        # Comments default to a Bot author: ours are posted by the app token or
        # by github-actions[bot], and find_sticky requires that.
        self.comments = [
            {"user": {"type": "Bot"}, **c} for c in list(comments or [])
        ]
        self.head_sha = head_sha
        self.calls = []
        self.next_comment_id = 1000

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "GET" and "/labels" in path:
            # Paginated like the real endpoint, so a pager bug shows up here.
            return self._page([{"name": n} for n in self.labels], path)
        if method == "GET" and "/pulls/" in path:
            return {"head": {"sha": self.head_sha}}
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
                    "user": {"type": "Bot"},
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
        """Serve `items` the way GitHub pages a list endpoint."""
        per_page = 100 if "per_page=100" in path else 30
        page = int(path.rsplit("page=", 1)[1]) if "page=" in path else 1
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
        self.assertEqual(res, {"added": [], "removed": []})
        self.assertEqual(api.of("POST", "/labels"), [])
        self.assertEqual(api.of("DELETE", "/labels/"), [])

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

    def test_an_unticked_box_stays_unticked(self):
        report = grade_risk.grade(grade_risk.parse_numstat("3\t1\ta.md\0"))
        fresh = grade_risk.render_comment(report, publish_risk.MARKER)
        api = FakeApi(comments=[{"id": 7, "body": fresh}])
        publish_risk.upsert_sticky(api, "o/r", 1, fresh)
        self.assertIn("- [ ] **This grade is wrong**", api.comments[0]["body"])


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

    def _args(self, report, check, comment, mode):
        return argparse.Namespace(
            repo="o/r", pr=1, sha="sha1", report=report, check=check,
            comment=comment, mode=mode,
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
    def _args(self, body_text):
        d = tempfile.mkdtemp()
        return argparse.Namespace(repo="o/r", pr=1, body=_write(d, "b.md", body_text))

    def test_a_ticked_box_labels_the_pr(self):
        body = grade_risk.render_comment(
            grade_risk.grade(grade_risk.parse_numstat("1\t0\ta.go\0")),
            publish_risk.MARKER,
            disputed=True,
        )
        api = FakeApi()
        publish_risk.cmd_dispute(self._args(body), api)
        self.assertIn(publish_risk.DISPUTE_LABEL, api.labels)

    def test_unticking_clears_the_label(self):
        body = grade_risk.render_comment(
            grade_risk.grade(grade_risk.parse_numstat("1\t0\ta.go\0")),
            publish_risk.MARKER,
        )
        api = FakeApi(labels=[publish_risk.DISPUTE_LABEL])
        publish_risk.cmd_dispute(self._args(body), api)
        self.assertNotIn(publish_risk.DISPUTE_LABEL, api.labels)

    def test_an_unrelated_edited_comment_is_ignored(self):
        api = FakeApi()
        publish_risk.cmd_dispute(self._args("I edited my review comment"), api)
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
