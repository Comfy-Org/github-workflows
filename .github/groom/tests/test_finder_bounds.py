import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOW = (ROOT / ".github/workflows/groom.yml").read_text(encoding="utf-8")
FINDER_BRIEF = (ROOT / ".github/groom/finder.md").read_text(encoding="utf-8")


class TestFinderBounds(unittest.TestCase):
    @staticmethod
    def finder_step():
        match = re.search(
            r"(?ms)^      - name: Run finder\n(?P<body>.*?)^      - name: Unlock the clone\n",
            WORKFLOW,
        )
        if match is None:
            raise AssertionError("groom.yml has no Run finder step followed by Unlock the clone")
        return match.group("body")

    def test_finder_cli_has_turn_and_dollar_caps(self):
        finder_step = self.finder_step()
        claude_command = finder_step.split('claude -p "$PROMPT"', 1)[1].split(
            "--output-format json", 1
        )[0]

        self.assertRegex(claude_command, re.compile(r"--max-turns\s+150\s+\\"))
        self.assertRegex(claude_command, re.compile(r"--max-budget-usd\s+8\s+\\"))

    def test_brief_requires_an_early_result_and_reports_why_it_stopped(self):
        self.assertNotIn("estimated inspection spend", FINDER_BRIEF)
        self.assertIn("After at most 60 inspection tool calls", FINDER_BRIEF)
        self.assertIn("Reserve enough time to write the result", FINDER_BRIEF)
        self.assertIn("Fewer than 6 findings is valid", FINDER_BRIEF)
        self.assertIn('"stop_reason":"inspection-complete|inspection-call-limit"', FINDER_BRIEF)
        self.assertIn(
            "If an inspection call is denied, do not retry that operation through another tool",
            FINDER_BRIEF,
        )

    def test_complete_handoff_survives_a_late_cli_failure(self):
        finder_step = self.finder_step()
        self.assertIn('if [ -s "$FINDER_OUT" ] && jq -e', finder_step)
        self.assertIn('(.stop_reason | IN("inspection-complete", "inspection-call-limit"))', finder_step)
        self.assertIn("preserving it for independent verification", finder_step)
        self.assertIn("STATUS=0", finder_step)

    def test_zero_at_call_limit_is_not_reported_as_clean(self):
        self.assertIn(
            "reached its inspection-call limit with zero findings",
            WORKFLOW,
        )


if __name__ == "__main__":
    unittest.main()
