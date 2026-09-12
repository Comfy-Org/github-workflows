import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOW = (ROOT / ".github/workflows/groom.yml").read_text(encoding="utf-8")
BUILDER_BRIEF = (ROOT / ".github/groom/builder.md").read_text(encoding="utf-8")


class TestBuilderBounds(unittest.TestCase):
    @staticmethod
    def builder_step():
        match = re.search(
            r"(?ms)^      - name: Run builder\n(?P<body>.*?)^      - name: Unlock the clone's \.git\n",
            WORKFLOW,
        )
        if match is None:
            raise AssertionError("groom.yml has no Run builder step followed by Unlock the clone's .git")
        return match.group("body")

    def test_brief_requires_a_bounded_feasibility_decision_before_edits(self):
        self.assertIn("After at most 20 inspection tool calls", BUILDER_BRIEF)
        self.assertIn("make one feasibility decision before editing", BUILDER_BRIEF)
        self.assertIn("Only choose `patched` when the exact bounded diff is clear", BUILDER_BRIEF)
        self.assertIn("Reserve enough time to write the control file", BUILDER_BRIEF)
        self.assertIn(
            "If a tool call is denied, do not retry that operation or seek a shell-command substitute",
            BUILDER_BRIEF,
        )

    def test_complete_clean_bail_survives_a_late_cli_failure(self):
        builder_step = self.builder_step()
        self.assertIn('if [ -s "$BUILDER_OUT" ] && jq -e', builder_step)
        self.assertIn('.status == "bail"', builder_step)
        self.assertIn('test -z "$(git status --short)"', builder_step)
        self.assertIn("preserving the clean bail-out for issue filing", builder_step)
        self.assertRegex(
            builder_step,
            r'(?s)preserving the clean bail-out for issue filing\."\n\s+STATUS=0\n\s+else',
        )


if __name__ == "__main__":
    unittest.main()
