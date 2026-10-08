"""Static checks on the business, design and completeness axis workflows."""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
TOKENS = ("LINEAR_KEY", "NOTION_TOKEN", "SLACK_TOKEN")

AXES = {
    # `declared` adds secrets a wrapper accepts for compatibility but never uses.
    "business": {"secrets": ["CURSOR_API_KEY", "LINEAR_KEY", "NOTION_TOKEN"],
                 "declared": ["CURSOR_API_KEY", "LINEAR_KEY", "NOTION_TOKEN", "SLACK_TOKEN"],
                 "checkout": "false", "no_shell": "true", "sources": "linear,notion"},
    "design": {"secrets": ["CURSOR_API_KEY", "LINEAR_KEY", "NOTION_TOKEN"],
               "checkout": "false", "no_shell": "true", "sources": "linear,notion"},
    "completeness": {"secrets": ["CURSOR_API_KEY", "LINEAR_KEY"],
                     "checkout": "true", "no_shell": "false", "sources": "linear"},
}
SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"
PRIVATE = "github.event.repository.private == true"


def read(name):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as f:
        return f.read()


def block(text, header, indent):
    """The lines under `header` (at `indent` spaces) up to the next line at <= indent."""
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln == " " * indent + header)
    out = []
    for ln in lines[start + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
            break
        out.append(ln)
    return out


def keys(lines, indent):
    return [ln.strip().rstrip(":") for ln in lines
            if ln.strip() and len(ln) - len(ln.lstrip()) == indent and ln.strip().endswith(":")]


def steps(text):
    """{step name: step text} for the base's single job."""
    parts = re.split(r"\n      - name: ", text)
    return {p.split("\n", 1)[0].strip(): p for p in parts[1:]}


class Wrappers(unittest.TestCase):
    def test_each_wrapper_declares_exactly_its_secrets(self):
        for axis, want in AXES.items():
            text = read(f"axis-{axis}.yml")
            on_call = block(text, "workflow_call:", 2)
            secrets = block("\n".join(on_call), "secrets:", 4)
            self.assertEqual(keys(secrets, 6), want.get("declared", want["secrets"]), axis)
            self.assertEqual(set(re.findall(r"secrets\.([A-Za-z_]+)", text)), set(want["secrets"]), axis)
            self.assertNotIn("secrets: inherit", text, axis)

    def test_no_wrapper_enables_slack(self):
        for axis in AXES:
            sources = re.search(r"context_sources: *(\S*)", read(f"axis-{axis}.yml")).group(1)
            self.assertNotIn("slack", sources.split(","), axis)

    def test_base_rejects_slack(self):
        base = read("cursor-axis-base.yml")
        self.assertIn('*,slack,*) echo "::error::context_sources may not include slack', base)

    def test_checkout_no_shell_and_sources(self):
        for axis, want in AXES.items():
            text = read(f"axis-{axis}.yml")
            self.assertIn(f"\n      checkout: {want['checkout']}\n", text, axis)
            self.assertIn(f"\n      no_shell: {want['no_shell']}\n", text, axis)
            self.assertIn(f"\n      context_sources: {want['sources']}\n", text, axis)

    def test_fork_and_public_repo_skip_conditions(self):
        for axis in AXES:
            text = read(f"axis-{axis}.yml")
            job = "\n".join(block(text, f"{axis}:", 2))
            self.assertIn(f"if: {SAME_REPO} && {PRIVATE}", job, axis)
            skipped = "\n".join(block(text, "skipped:", 2))
            self.assertIn("github.event.pull_request.head.repo.full_name != github.repository", skipped)
            self.assertIn("github.event.repository.private != true", skipped)
            self.assertIn("::notice::", skipped)


class Base(unittest.TestCase):
    def setUp(self):
        self.text = read("cursor-axis-base.yml")
        self.steps = steps(self.text)

    def test_agent_step_env_has_no_context_token(self):
        run = self.steps["Run the axis"]
        env = run[run.index("\n        env:\n"):run.index("\n        run: |")]
        self.assertIn("CURSOR_API_KEY", env)
        for token in TOKENS:
            self.assertNotIn(token, env)
        # Nor through the workflow-level env every step inherits.
        top_env = "\n".join(block(self.text, "env:", 0))
        for token in TOKENS:
            self.assertNotIn(token, top_env)

    def test_tokens_appear_only_in_the_token_file_step(self):
        for name, body in self.steps.items():
            if name == "Write the context token file":
                continue
            for token in TOKENS:
                self.assertNotIn(f"secrets.{token}", body, name)
        writer = self.steps["Write the context token file"]
        self.assertIn("0o600", writer)
        self.assertIn("O_EXCL", writer)

    def test_proxy_deletes_tokens_before_the_agent_starts(self):
        run = self.steps["Run the axis"]
        proxy = run.index("context-proxy.py")
        guard = run.index('if [ -e "$token_file" ]')
        agent = run.index("cursor-agent --print")
        self.assertLess(proxy, guard)
        self.assertLess(guard, agent)
        self.assertIn("env -u CURSOR_API_KEY", run)

    def test_no_shell_denies_shell_and_write(self):
        run = self.steps["Run the axis"]
        self.assertIn('["Shell(*)", "Write(**)", "WebFetch(*)"]', run)
        self.assertIn("rm -rf -- .cursor", run)
        self.assertIn("rm -f -- .cursorignore .cursorindexingignore", run)

    def test_checkout_axes_are_allowed_a_shell(self):
        # An empty allow list makes --print reject every shell call, so a
        # checkout axis reviewed without ever running git. The allow rule is
        # explicit, and a transcript where every shell call was rejected fails.
        run = self.steps["Run the axis"]
        self.assertIn('if os.environ["NO_SHELL"] != "true":\n              allow.append("Shell(*)")', run)
        self.assertIn('"rejected" in outcome', run)
        self.assertIn('if os.environ["NO_SHELL"] != "true" and shell_rejected and not shell_ok:', run)

    def test_every_axis_must_make_a_tool_call(self):
        run = self.steps["Run the axis"]
        self.assertIn("          if not recognized:\n", run)
        self.assertNotIn('if os.environ["NO_SHELL"] == "true" and not recognized', run)

    def test_business_and_design_require_no_shell_and_no_checkout(self):
        check = self.steps["Check inputs"]
        self.assertIn("business|design)", check)
        self.assertIn('[ "$NO_SHELL" != "true" ]', check)
        self.assertIn('[ "$REPO_PRIVATE" != "true" ]', check)

    def test_non_fetching_axes_require_a_checkout(self):
        check = self.steps["Check inputs"]
        self.assertIn('the $AXIS axis requires checkout true', check)
        self.assertIn("false/business|false/design)", self.steps["Render prompt"])

    def test_context_sources_matched_as_a_whole_value(self):
        self.assertIn('[[ "$CONTEXT_SOURCES" =~ ^', self.steps["Check inputs"])

    def test_changed_files_one_page_json_paths_and_truncation_line(self):
        fetch = self.steps["Fetch the change (no checkout)"]
        self.assertNotIn("gh api --paginate", fetch)
        self.assertIn(".filename | @json", fetch)
        self.assertIn("[file list truncated", fetch)

    def test_raw_stream_stays_out_of_the_uploaded_directory(self):
        run = self.steps["Run the axis"]
        self.assertIn('> "$RUNNER_TEMP/axis-stream.jsonl"', run)
        self.assertNotIn('> "$out/transcript.jsonl"', run)
        self.assertIn("no recognizable tool call", run)

    def test_transcript_artifact_never_matches_decides_download(self):
        upload = self.steps["Upload the transcript"]
        self.assertIn("name: transcript-axis-", upload)
        self.assertIn("name: axis-${{ inputs.axis }}", self.steps["Upload the verdict"])


if __name__ == "__main__":
    unittest.main()
