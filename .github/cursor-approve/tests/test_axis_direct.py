"""The opt-in direct-Anthropic runner of the axis workflows (`axis_direct_model`).

Static checks on cursor-axis-base.yml and the two checkout wrappers, plus the
token-usage step EXECUTED against a fake result:

  * plumbing — the wrappers declare the input (default '') and the optional
    ANTHROPIC_API_KEY, and pass both to the base unchanged;
  * exactly one runner — every Cursor step is gated on the input being empty,
    every direct step on it being set, and nothing falls back;
  * confinement — the cells' flags: Read/Grep/Glob, `--restricted`,
    `--setting-sources ""`, /proc and /sys denied, never `--bare`;
  * the key lives in the run step's env alone;
  * usage — `usage-direct-axis-<axis>-<model>`, the cells' script byte for byte
    but for the result path, best effort and time-boxed.

Run: python3 -m unittest discover -s .github/cursor-approve/tests -p 'test_*.py'
"""

import json
import os
import re
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
DIRECT_IF = "inputs.axis_direct_model != ''"
CURSOR_IF = "inputs.axis_direct_model == ''"


def read(name):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as f:
        return f.read()


def steps(text):
    """{step name: step text} for the base's single job."""
    parts = re.split(r"\n      - name: ", text)
    return {p.split("\n", 1)[0].strip(): p for p in parts[1:]}


def run_block(step_text):
    """The `run: |` body of a step, dedented by its 10 spaces."""
    lines = step_text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln == "        run: |")
    out = []
    for ln in lines[start + 1:]:
        if ln.strip() and not ln.startswith("          "):
            break
        out.append(ln[10:])
    return "\n".join(out).rstrip("\n") + "\n"



BASE = read("cursor-axis-base.yml")
STEPS = steps(BASE)


class Plumbing(unittest.TestCase):
    def test_base_declares_the_input_with_an_empty_default(self):
        block = BASE[BASE.index("      axis_direct_model:"):]
        block = block[:block.index("    secrets:")]
        self.assertIn("type: string", block)
        self.assertIn("default: ''", block)
        self.assertRegex(BASE, r"\n      ANTHROPIC_API_KEY:\n        required: false\n")

    def test_wrappers_pass_the_input_and_key_through(self):
        for axis in ("correctness", "conformance"):
            text = read(f"axis-{axis}.yml")
            with self.subTest(axis=axis):
                self.assertRegex(text, r"\n      axis_direct_model:\n        type: string\n        default: ''\n")
                self.assertRegex(text, r"\n      ANTHROPIC_API_KEY:\n        required: false\n")
                self.assertIn("      axis_direct_model: ${{ inputs.axis_direct_model }}\n", text)
                self.assertIn("      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n", text)

    def test_the_id_is_validated_and_never_combined_with_context_tools(self):
        check = STEPS["Check inputs"]
        self.assertIn("AXIS_DIRECT_MODEL: ${{ inputs.axis_direct_model }}", check)
        self.assertIn("^[A-Za-z0-9._-]{1,100}$", check)
        self.assertIn("cannot be combined with context_sources", check)
        # The run names its runner, on both paths.
        self.assertIn("::notice::", check)
        self.assertEqual(check.count("GITHUB_STEP_SUMMARY"), 2)

    def test_the_check_executes(self):
        script = run_block(STEPS["Check inputs"])
        base_env = dict(os.environ, AXIS="correctness", COMMIT_SHA="a" * 40, MODEL="claude-opus-5-5-xhigh",
                        CONTEXT_SOURCES="", CHECKOUT="true", NO_SHELL="false", PRIOR_ROUNDS="0",
                        REPO_PRIVATE="true", PR_NUMBER="1", HEAD_REPO="o/r", BASE_REPO="o/r",
                        WORKFLOW_SHA="b" * 40)
        for model, extra, ok in (("", {}, True), ("claude-opus-5-5", {}, True),
                                 ("claude opus", {}, False), ("$(id)", {}, False),
                                 ("claude-opus-5-5", {"AXIS": "completeness", "CONTEXT_SOURCES": "linear"}, False)):
            with self.subTest(model=model, extra=extra), tempfile.NamedTemporaryFile() as summary:
                env = dict(base_env, AXIS_DIRECT_MODEL=model, GITHUB_STEP_SUMMARY=summary.name, **extra)
                done = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
                self.assertEqual(done.returncode == 0, ok, done.stdout + done.stderr)


class ExactlyOneRunner(unittest.TestCase):
    def test_cursor_steps_skip_when_direct(self):
        for name in ("Install Cursor agent CLI", "Run the axis"):
            self.assertIn(f"        if: {CURSOR_IF}\n", STEPS[name], name)

    def test_direct_steps_run_only_when_direct(self):
        for name in ("Check the Anthropic API key", "Install Claude Code CLI", "Verify Claude Code CLI version",
                     "Prepare the checkout for the direct runner", "Run the axis (direct)"):
            self.assertIn(f"        if: {DIRECT_IF}\n", STEPS[name], name)

    def test_no_fallback_a_missing_key_fails_the_axis(self):
        body = run_block(STEPS["Check the Anthropic API key"])
        self.assertIn("exit 1", body)
        self.assertNotIn("continue-on-error", STEPS["Check the Anthropic API key"])
        self.assertNotIn("continue-on-error", STEPS["Run the axis (direct)"])

    def test_both_runners_feed_the_same_extraction_and_artifact(self):
        self.assertIn('"$RUNNER_TEMP/axis-raw.txt"', STEPS["Run the axis (direct)"])
        self.assertIn('--raw "$RUNNER_TEMP/axis-raw.txt"', STEPS["Extract the verdict"])
        self.assertNotIn("if:", STEPS["Extract the verdict"].split("run:")[0])
        self.assertIn("name: axis-${{ inputs.axis }}\n", STEPS["Upload the verdict"])

    def test_the_direct_step_is_capped_below_the_job(self):
        self.assertIn("timeout-minutes: 15", STEPS["Run the axis (direct)"])
        self.assertIn("    timeout-minutes: 20\n", BASE)


class Confinement(unittest.TestCase):
    def test_the_cells_flags(self):
        run = STEPS["Run the axis (direct)"]
        for flag in ('--restricted', '--setting-sources ""', '--tools "Read,Grep,Glob"',
                     '--allowedTools "Read,Grep,Glob"', "--strict-mcp-config",
                     '--disallowedTools "Read(//proc/**),Read(//sys/**)"', "--no-session-persistence"):
            self.assertIn(flag, run, flag)
        command = run[run.index("claude -p"):run.index("&& rc=0")]
        self.assertNotIn("--bare", command)
        self.assertNotIn("--mcp-config", command)

    def test_the_key_is_only_in_the_run_and_check_steps(self):
        for name, body in STEPS.items():
            if "secrets.ANTHROPIC_API_KEY" in body:
                self.assertIn(name, ("Check the Anthropic API key", "Run the axis (direct)"), name)
        self.assertNotIn("ANTHROPIC_API_KEY", STEPS["Run the axis"])
        self.assertNotIn("CURSOR_API_KEY", STEPS["Run the axis (direct)"])

    def test_every_stdin_python_in_the_checkout_is_isolated(self):
        run = run_block(STEPS["Run the axis (direct)"])
        for line in run.splitlines():
            if re.search(r"\bpython3\b", line):
                self.assertIn("python3 -I", line, line)

    def test_planted_config_and_symlinks_are_removed(self):
        body = run_block(STEPS["Prepare the checkout for the direct runner"])
        self.assertIn("find pr -type l -delete", body)
        self.assertIn("rm -rf pr/.claude pr/.mcp.json pr/CLAUDE.local.md", body)

    def test_the_prompt_points_at_the_written_diff(self):
        render = STEPS["Render prompt"]
        self.assertIn('git -C pr diff "$merge_base...$COMMIT_SHA" > "$ctx_dir/cursor-approve-pr.diff"', render)
        self.assertIn("prompt-direct-runner.md", render)
        with open(os.path.join(ROOT, ".github", "cursor-approve", "prompt-direct-runner.md"), encoding="utf-8") as f:
            self.assertIn("`.git/cursor-approve-pr.diff`", f.read())


class ReplyGuards(unittest.TestCase):
    """The python3 -I guard in `Run the axis (direct)`, executed."""

    KEY = "sk-ant-test-0123456789abcdef"

    def guard(self, result, rc="0"):
        body = run_block(STEPS["Run the axis (direct)"])
        script = body[body.index("<<'PY'\n") + len("<<'PY'\n"):body.rindex("PY\n")]
        work = tempfile.mkdtemp()
        res, raw, kept = (os.path.join(work, n) for n in ("result.json", "raw.txt", "kept.json"))
        if result is not None:
            with open(res, "w", encoding="utf-8") as f:
                f.write(result if isinstance(result, str) else json.dumps(result))
        env = dict(os.environ, AGENT_RC=rc, ANTHROPIC_API_KEY=self.KEY)
        done = subprocess.run(["python3", "-I", "-", res, raw, kept], input=script, env=env,
                              capture_output=True, text=True)
        reply = open(raw, encoding="utf-8").read() if os.path.exists(raw) else None
        return done, reply, os.path.exists(kept)

    def test_a_clean_reply_is_handed_to_extraction(self):
        done, reply, kept = self.guard({"result": '{"verdict": "green"}', "num_turns": 5, "is_error": False})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(reply, '{"verdict": "green"}')
        self.assertTrue(kept)

    def test_failures_leave_no_reply(self):
        for result, rc in (({"result": "x", "num_turns": 5}, "1"), (None, "0"), ("not json", "0"),
                           ({"result": "x", "num_turns": 5, "is_error": True}, "0"),
                           ({"result": "x", "num_turns": 1}, "0"), ({"num_turns": 5}, "0")):
            with self.subTest(result=result, rc=rc):
                done, reply, _ = self.guard(result, rc)
                self.assertNotEqual(done.returncode, 0)
                self.assertIsNone(reply)

    def test_a_reply_carrying_the_key_is_never_published(self):
        done, reply, kept = self.guard({"result": f"green {self.KEY}", "num_turns": 5})
        self.assertNotEqual(done.returncode, 0)
        self.assertIsNone(reply)
        self.assertFalse(kept)


class Usage(unittest.TestCase):
    def test_the_artifact_name(self):
        upload = STEPS["Upload usage artifact"]
        self.assertIn("name: usage-direct-axis-${{ inputs.axis }}-${{ inputs.axis_direct_model }}\n", upload)
        # Never `axis-*`: decide downloads that pattern as verdicts.
        self.assertNotRegex(upload, r"name: axis-")

    def test_the_script_is_the_cells_but_for_the_result_path(self):
        review = read("cursor-review.yml")
        cell_job = review[review.index("\n  review-anthropic-direct:"):review.index("\n  judge-direct:")]
        cell = run_block(steps(cell_job)["Record token usage"])
        self.assertIn("/tmp/claude-result.json", cell)
        self.assertEqual(run_block(STEPS["Record token usage"]),
                         cell.replace("/tmp/claude-result.json", "/tmp/axis-claude-result.json"))
        self.assertIn("> /tmp/axis-claude-result.json", STEPS["Run the axis (direct)"])
        self.assertIn("REVIEW_TYPE: axis-${{ inputs.axis }}", STEPS["Record token usage"])

    def test_accounting_is_best_effort_time_boxed_and_last(self):
        names = list(STEPS)
        self.assertEqual(names[-3:], ["Record token usage", "Upload usage artifact", "Warn on unrecorded usage"])
        for name in names[-3:]:
            self.assertIn("continue-on-error: true", STEPS[name], name)
            self.assertRegex(STEPS[name], r"timeout-minutes: [12]\n", name)
            self.assertNotIn("ANTHROPIC_API_KEY", STEPS[name], name)
        for name in names[-3:-1]:
            self.assertIn("if: always() && steps.axis_direct.outcome != 'skipped'", STEPS[name], name)
        self.assertIn("id: axis_direct", STEPS["Run the axis (direct)"])
        self.assertIn("id: usage_upload", STEPS["Upload usage artifact"])
        self.assertIn("steps.usage_upload.outcome == 'failure'", STEPS["Warn on unrecorded usage"])
        self.assertIn("::warning::", STEPS["Warn on unrecorded usage"])

    def test_the_record_executes(self):
        script = run_block(STEPS["Record token usage"])
        script = script.replace("/tmp/", tempfile.mkdtemp() + "/")
        work = script[script.index("open(\"") + 6:script.index("/axis-claude-result.json")]
        with open(work + "/axis-claude-result.json", "w", encoding="utf-8") as f:
            json.dump({"result": "MODEL TEXT", "usage": {"input_tokens": 3, "output_tokens": 40,
                                                         "cache_read_input_tokens": 900},
                       "modelUsage": {"claude-opus-5-5": {"inputTokens": 3}}, "num_turns": 6}, f)
        env = dict(os.environ, MODEL="claude-opus-5-5", REVIEW_TYPE="axis-correctness", RUN_ATTEMPT="1")
        done = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(work + "/usage-out/usage.json", encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual((record["review_type"], record["measured"], record["usage"]["cache_read_input_tokens"]),
                         ("axis-correctness", True, 900))
        self.assertNotIn("MODEL TEXT", json.dumps(record))


if __name__ == "__main__":
    unittest.main()
