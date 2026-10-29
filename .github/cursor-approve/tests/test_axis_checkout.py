"""The checkout axes clone narrowly and fail closed on the merge base.

Both PR-head checkouts in cursor-axis-base.yml (the Cursor `axis` job and
`axis-direct`) are depth 1, followed by the shared fetch-history.sh, which
fetches back to the merge base of BASE_SHA and commit_sha or fails the job.
A full-history clone took minutes per axis on a large monorepo.

Run: python3 -m unittest discover -s .github/cursor-approve/tests -p 'test_*.py'
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
with open(os.path.join(ROOT, ".github", "workflows", "cursor-axis-base.yml"), encoding="utf-8") as f:
    BASE = f.read()

CHECKOUT = "Check out the PR head (read-only)"
FETCH = "Fetch the merge-base history"
RENDER = "Render prompt"
SCRIPT = ('run: bash "$GITHUB_WORKSPACE/_cursor_approve_assets/'
          'scripts/check-pr-size/fetch-history.sh"')


def job(name):
    start = BASE.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [A-Za-z0-9_-]+:\n", BASE[start + 1:])
    return BASE[start:start + 1 + nxt.start()] if nxt else BASE[start:]


def steps(text):
    """[(name, text)] in order."""
    parts = re.split(r"\n      - name: ", text)
    return [(p.split("\n", 1)[0].strip(), p) for p in parts[1:]]


class NarrowCheckout(unittest.TestCase):
    def test_no_full_history_clone(self):
        self.assertNotRegex(BASE, r"fetch-depth:\s*0\b")

    def test_each_checkout_job_fetches_the_merge_base_before_rendering(self):
        for name, gate in (("axis", "if: inputs.checkout"), ("axis-direct", None)):
            with self.subTest(job=name):
                st = steps(job(name))
                names = [n for n, _ in st]
                self.assertLess(names.index(CHECKOUT), names.index(FETCH))
                self.assertLess(names.index(FETCH), names.index(RENDER))
                checkout, fetch = dict(st)[CHECKOUT], dict(st)[FETCH]
                self.assertIn("fetch-depth: 1\n", checkout)
                self.assertIn("ref: ${{ inputs.commit_sha }}", checkout)
                self.assertIn(SCRIPT, fetch)
                self.assertIn("working-directory: pr\n", fetch)
                self.assertIn("HEAD_SHA: ${{ inputs.commit_sha }}", fetch)
                self.assertIn("BASE_SHA: ${{ github.event.pull_request.base.sha }}", fetch)
                self.assertIn("FETCH_TOKEN: ${{ github.token }}", fetch)
                if gate:
                    self.assertIn(gate + "\n", fetch)

    def test_render_reads_the_vouched_merge_base_with_no_fallback(self):
        # A silent fallback to BASE_SHA would show every base-branch commit
        # since the fork point as reverted by the PR.
        for name in ("axis", "axis-direct"):
            with self.subTest(job=name):
                render = dict(steps(job(name)))[RENDER]
                self.assertIn('merge_base="$(git -C pr merge-base "$BASE_SHA" "$COMMIT_SHA")"', render)
                self.assertNotIn("origin/$BASE_REF", render)


if __name__ == "__main__":
    unittest.main()
