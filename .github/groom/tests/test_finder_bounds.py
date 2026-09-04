import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOW = (ROOT / ".github/workflows/groom.yml").read_text(encoding="utf-8")
FINDER_BRIEF = (ROOT / ".github/groom/finder.md").read_text(encoding="utf-8")


class TestFinderBounds(unittest.TestCase):
    def test_finder_cli_has_turn_and_dollar_caps(self):
        finder_step = WORKFLOW.split("- name: Run finder", 1)[1].split(
            "- name: Unlock the clone", 1
        )[0]

        self.assertRegex(finder_step, re.compile(r"--max-turns\s+100(?:\s|\\)"))
        self.assertRegex(finder_step, re.compile(r"--max-budget-usd\s+8(?:\.0+)?(?:\s|\\)"))

    def test_brief_requires_an_early_result_and_no_denial_retries(self):
        self.assertIn("After at most 60 inspection tool calls", FINDER_BRIEF)
        self.assertIn("Fewer than 6 findings is valid", FINDER_BRIEF)
        self.assertIn("If an inspection call is denied, do not retry it", FINDER_BRIEF)


if __name__ == "__main__":
    unittest.main()
