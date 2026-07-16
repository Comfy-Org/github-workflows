#!/usr/bin/env python3
"""Regression tests for wire-bot-identity.py.

The helper injects the cloud-code-bot identity (bot_app_id input +
BOT_APP_PRIVATE_KEY secret) into a cursor-review caller as the fan-out step of
BE-1814. The properties that matter — and that these tests pin — are:

  * the two anchors get exactly the ticket's mapping (vars.APP_ID /
    secrets.CLOUD_CODE_BOT_PRIVATE_KEY),
  * injection is idempotent (an already-wired caller is a byte-for-byte no-op),
  * only the wiring changes — comments, folded diff_excludes, and the SHA-pin
    line are preserved (a PyYAML round-trip would destroy them), and
  * indentation is inherited from the caller, not hard-coded.

Run: python3 .github/cursor-review/tests/test_wire_bot_identity.py
"""

import importlib.util
import os
import unittest

# wire-bot-identity.py has a hyphen, so import it by path rather than `import`.
_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "wire-bot-identity.py")
_spec = importlib.util.spec_from_file_location("wire_bot_identity", _MODULE_PATH)
wbi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wbi)


# A representative unwired caller (comfy-inapp-agent's shape: `with:` + `secrets:`
# both present, no bot identity yet).
UNWIRED = """\
jobs:
  cursor-review:
    permissions:
      contents: read
      pull-requests: write
    # SHA-pinned per zizmor `unpinned-uses: hash-pin`.
    uses: Comfy-Org/github-workflows/.github/workflows/cursor-review.yml@df507e6bae179c567ad3849370f99dae588985dc # github-workflows main (df507e6)
    with:
      workflows_ref: df507e6bae179c567ad3849370f99dae588985dc
      # Minimal excludes for a small Node + TS extension.
      diff_excludes: >-
        :!**/package-lock.json
        :!**/node_modules/**
    secrets:
      CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
      SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
"""


class WireBotIdentityTest(unittest.TestCase):
    def test_injects_both_anchors_with_exact_mapping(self):
        out = wbi.wire(UNWIRED)
        self.assertIn("bot_app_id: ${{ vars.APP_ID }}", out)
        self.assertIn(
            "BOT_APP_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}", out
        )

    def test_bot_app_id_nested_under_with_not_secrets(self):
        out = wbi.wire(UNWIRED).split("\n")
        with_idx = next(i for i, l in enumerate(out) if l.strip() == "with:")
        secrets_idx = next(i for i, l in enumerate(out) if l.strip() == "secrets:")
        app_idx = next(i for i, l in enumerate(out) if "bot_app_id:" in l)
        key_idx = next(i for i, l in enumerate(out) if "BOT_APP_PRIVATE_KEY:" in l)
        # bot_app_id sits inside the with: block; the private key inside secrets:.
        self.assertTrue(with_idx < app_idx < secrets_idx)
        self.assertTrue(secrets_idx < key_idx)

    def test_inherits_child_indentation(self):
        out = wbi.wire(UNWIRED).split("\n")
        app_line = next(l for l in out if "bot_app_id:" in l)
        key_line = next(l for l in out if "BOT_APP_PRIVATE_KEY:" in l)
        # `with:`/`secrets:` are at 4 spaces, so children land at 6.
        self.assertTrue(app_line.startswith("      bot_app_id:"))
        self.assertTrue(key_line.startswith("      BOT_APP_PRIVATE_KEY:"))

    def test_idempotent_on_already_wired(self):
        once = wbi.wire(UNWIRED)
        twice = wbi.wire(once)
        self.assertEqual(once, twice)

    def test_already_wired_is_exact_no_op(self):
        # An already-wired caller must be returned byte-for-byte unchanged.
        already = wbi.wire(UNWIRED)
        self.assertEqual(wbi.wire(already), already)

    def test_preserves_comments_and_diff_excludes(self):
        out = wbi.wire(UNWIRED)
        self.assertIn("# SHA-pinned per zizmor", out)
        self.assertIn("diff_excludes: >-", out)
        self.assertIn(":!**/node_modules/**", out)
        self.assertIn("# github-workflows main (df507e6)", out)
        # Every original line survives (only additions, no deletions/edits).
        for line in UNWIRED.split("\n"):
            self.assertIn(line, out.split("\n"))

    def test_partial_wire_completes_the_missing_half(self):
        # Caller that already has bot_app_id but is missing the secret.
        half = UNWIRED.replace(
            "      workflows_ref:",
            "      bot_app_id: ${{ vars.APP_ID }}\n      workflows_ref:",
            1,
        )
        out = wbi.wire(half)
        # The secret is added...
        self.assertIn(
            "BOT_APP_PRIVATE_KEY: ${{ secrets.CLOUD_CODE_BOT_PRIVATE_KEY }}", out
        )
        # ...and bot_app_id is not duplicated.
        self.assertEqual(out.count("bot_app_id:"), 1)

    def test_only_wiring_lines_are_added(self):
        before = UNWIRED.split("\n")
        after = wbi.wire(UNWIRED).split("\n")
        added = [l for l in after if l not in before]
        # Only the two key lines + their explanatory comments are new; every
        # added line is a comment or one of the two wiring keys.
        for line in added:
            stripped = line.strip()
            self.assertTrue(
                stripped.startswith("#")
                or stripped.startswith("bot_app_id:")
                or stripped.startswith("BOT_APP_PRIVATE_KEY:"),
                f"unexpected added line: {line!r}",
            )
        self.assertTrue(any("bot_app_id:" in l for l in added))
        self.assertTrue(any("BOT_APP_PRIVATE_KEY:" in l for l in added))


if __name__ == "__main__":
    unittest.main(verbosity=2)
