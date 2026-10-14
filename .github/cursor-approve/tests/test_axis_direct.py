"""The opt-in direct-Anthropic runner of the axis workflows (`axis_direct_model`).

Static checks on cursor-axis-base.yml and the two checkout wrappers, plus the
token-usage step EXECUTED against a fake result:

  * plumbing — the wrappers declare the input (default '') and the optional
    ANTHROPIC_API_KEY, and pass both to the base unchanged;
  * exactly one runner — the Cursor job is gated on the input being empty, the
    separate `axis-direct` job (hard-pinned to `ubuntu-latest`) on it being
    set, and nothing falls back;
  * confinement — the cells' flags: Read/Grep/Glob, `--restricted`,
    `--setting-sources ""`, /proc and /sys denied, never `--bare`;
  * the key is referenced by the `axis-direct` job alone, never the Cursor job;
  * usage — `usage-direct-axis-<axis>-<model>`, the cells' script byte for byte
    but for the result path, best effort and time-boxed;
  * the API-error diagnostic — agent-error.py runs on the OUTCOME, not on the
    exit code alone, so a capped or revoked key is NAMED instead of reading as
    an axis that answered nothing.

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
    """{step name: step text} for one job's text."""
    parts = re.split(r"\n      - name: ", text)
    return {p.split("\n", 1)[0].strip(): p for p in parts[1:]}


def job(text, name):
    """One top-level job's text, from its `  <name>:` line to the next job."""
    start = text.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [A-Za-z0-9_-]+:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


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
CURSOR_JOB = job(BASE, "axis")
DIRECT_JOB = job(BASE, "axis-direct")
# Without comment lines, for the "never mentioned" checks below.
CURSOR_CODE = "\n".join(ln for ln in CURSOR_JOB.split("\n") if not ln.lstrip().startswith("#"))
DIRECT_CODE = "\n".join(ln for ln in DIRECT_JOB.split("\n") if not ln.lstrip().startswith("#"))
CURSOR = steps(CURSOR_JOB)
STEPS = steps(DIRECT_JOB)


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
        self.assertEqual(check.count("GITHUB_STEP_SUMMARY"), 1)
        self.assertEqual(CURSOR["Check inputs"].count("GITHUB_STEP_SUMMARY"), 1)
        # The direct job reads neither, so it does not gate on them.
        self.assertNotIn("inputs.model", DIRECT_JOB)
        self.assertNotIn("inputs.runs_on", DIRECT_JOB)

    def test_the_check_executes(self):
        script = run_block(STEPS["Check inputs"])
        base_env = dict(os.environ, AXIS="correctness", COMMIT_SHA="a" * 40,
                        CONTEXT_SOURCES="", CHECKOUT="true", PRIOR_ROUNDS="0",
                        PR_NUMBER="1", HEAD_REPO="o/r", BASE_REPO="o/r", WORKFLOW_SHA="b" * 40)
        for model, extra, ok in (("claude-opus-5-5", {}, True), ("", {}, False),
                                 # A blanked or garbage Cursor `model` is not this job's concern.
                                 ("claude-opus-5-5", {"MODEL": ""}, True),
                                 ("claude opus", {}, False), ("$(id)", {}, False),
                                 ("claude-opus-5-5", {"AXIS": "completeness", "CONTEXT_SOURCES": "linear"}, False),
                                 ("claude-opus-5-5", {"AXIS": "business", "CHECKOUT": "false"}, False),
                                 ("claude-opus-5-5", {"CHECKOUT": "false"}, False),
                                 ("claude-opus-5-5", {"HEAD_REPO": "fork/r"}, False),
                                 ("claude-opus-5-5", {"WORKFLOW_SHA": ""}, False),
                                 ("claude-opus-5-5", {"PRIOR_ROUNDS": "4"}, False)):
            with self.subTest(model=model, extra=extra), tempfile.NamedTemporaryFile() as summary:
                env = dict(base_env, AXIS_DIRECT_MODEL=model, GITHUB_STEP_SUMMARY=summary.name, **extra)
                done = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
                self.assertEqual(done.returncode == 0, ok, done.stdout + done.stderr)


class ExactlyOneRunner(unittest.TestCase):
    def test_each_job_is_gated_on_the_input(self):
        self.assertIn(f"\n    if: {CURSOR_IF}\n", CURSOR_JOB)
        self.assertIn(f"\n    if: {DIRECT_IF}\n", DIRECT_JOB)
        self.assertEqual(re.findall(r"^  ([A-Za-z0-9_-]+):$", BASE[BASE.index("\njobs:\n"):], re.MULTILINE),
                         ["axis", "axis-direct"])

    def test_the_direct_job_is_hosted_and_named_apart(self):
        # Never the caller's `runs_on` pool, as cursor-review's direct cells.
        self.assertIn("\n    runs-on: ubuntu-latest\n", DIRECT_JOB)
        self.assertIn("\n    name: 'axis: ${{ inputs.axis }} (direct)'\n", DIRECT_JOB)
        self.assertIn("\n    name: 'axis: ${{ inputs.axis }}'\n", CURSOR_JOB)

    def test_each_job_carries_only_its_own_runner(self):
        self.assertNotIn("claude", CURSOR_CODE)
        self.assertNotIn("cursor-agent", DIRECT_CODE)
        self.assertNotIn("install-cursor-cli", DIRECT_CODE)
        self.assertNotIn("axis_direct_model", CURSOR_CODE.replace(f"if: {CURSOR_IF}", ""))

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

    def test_the_key_is_only_in_the_direct_job(self):
        # Not even skipped steps of the Cursor job may reference it: the runner
        # of a job that names a secret is handed that secret.
        self.assertNotIn("ANTHROPIC_API_KEY", CURSOR_CODE)
        for name, body in STEPS.items():
            if "secrets.ANTHROPIC_API_KEY" in body:
                self.assertIn(name, ("Check the Anthropic API key", "Run the axis (direct)",
                                     "Extract the verdict"), name)
        self.assertEqual(BASE.count("secrets.ANTHROPIC_API_KEY"), DIRECT_JOB.count("secrets.ANTHROPIC_API_KEY"))
        self.assertNotIn("CURSOR_API_KEY", DIRECT_JOB)

    def test_every_stdin_python_with_the_key_is_isolated(self):
        for name in ("Run the axis (direct)", "Extract the verdict"):
            for line in run_block(STEPS[name]).splitlines():
                if re.search(r"\bpython3 .*- ", line):
                    self.assertIn("python3 -I", line, (name, line))

    def test_planted_config_symlinks_and_ignore_files_are_removed(self):
        body = run_block(STEPS["Prepare the checkout for the direct runner"])
        self.assertIn("find pr -type l -delete", body)
        self.assertIn("rm -rf pr/.claude pr/.mcp.json pr/CLAUDE.local.md", body)
        work = tempfile.mkdtemp()
        for rel in ("pr/.ignore", "pr/sub/.rgignore", "pr/.gitignore", "pr/.git/.ignore", "pr/keep.py"):
            os.makedirs(os.path.dirname(os.path.join(work, rel)), exist_ok=True)
            open(os.path.join(work, rel), "w").close()
        done = subprocess.run(["bash", "-e", "-c", body], cwd=work, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        for rel, kept in (("pr/.ignore", False), ("pr/sub/.rgignore", False), ("pr/.gitignore", True),
                          ("pr/.git/.ignore", True), ("pr/keep.py", True)):
            self.assertEqual(os.path.exists(os.path.join(work, rel)), kept, rel)

    def test_the_prompt_points_at_the_written_diff(self):
        render = STEPS["Render prompt"]
        self.assertIn('git -C pr diff --text --no-textconv --no-ext-diff \\\n'
                      '            "$merge_base...$COMMIT_SHA" > "$ctx_dir/cursor-approve-pr.diff"', render)
        self.assertIn("prompt-direct-runner.md", render)
        self.assertNotIn("prompt-direct-runner.md", CURSOR["Render prompt"])
        with open(os.path.join(ROOT, ".github", "cursor-approve", "prompt-direct-runner.md"), encoding="utf-8") as f:
            self.assertIn("`.git/cursor-approve-pr.diff`", f.read())

    def test_the_written_diff_ignores_the_checkouts_attributes(self):
        # A PR-added `-diff` attribute must not shrink its change to "Binary files differ".
        render = run_block(STEPS["Render prompt"])
        cmd = render[render.index("git -C pr diff"):render.index("\n", render.index("cursor-approve-pr.diff\""))]
        work = tempfile.mkdtemp()
        git = ["git", "-C", os.path.join(work, "pr"), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", os.path.join(work, "pr")], check=True)
        with open(os.path.join(work, "pr", "auth.py"), "w") as f:
            f.write("check()\n")
        subprocess.run(git + ["add", "."], check=True)
        subprocess.run(git + ["commit", "-qm", "base"], check=True)
        with open(os.path.join(work, "pr", ".gitattributes"), "w") as f:
            f.write("auth.py -diff\n")
        with open(os.path.join(work, "pr", "auth.py"), "w") as f:
            f.write("pass\n")
        subprocess.run(git + ["commit", "-qam", "x"], check=True)
        subprocess.run(git + ["add", "."], check=True)
        subprocess.run(git + ["commit", "-qm", "attrs"], check=True)
        head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        base = subprocess.run(git + ["rev-parse", "HEAD~2"], capture_output=True, text=True, check=True).stdout.strip()
        os.makedirs(os.path.join(work, "pr", ".git"), exist_ok=True)
        env = dict(os.environ, merge_base=base, COMMIT_SHA=head, ctx_dir="pr/.git")
        done = subprocess.run(["bash", "-e", "-c", cmd], cwd=work, env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(os.path.join(work, "pr", ".git", "cursor-approve-pr.diff"), encoding="utf-8") as f:
            diff = f.read()
        self.assertIn("-check()", diff)
        self.assertNotIn("Binary files", diff)


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
                           ({"result": "x", "num_turns": 1}, "0"), ({"num_turns": 5}, "0"),
                           # An unrecognized turn count fails closed, never skips the check.
                           ({"result": "x"}, "0"), ({"result": "x", "num_turns": True}, "0"),
                           ({"result": "x", "num_turns": "5"}, "0"), ({"result": "x", "num_turns": 5.0}, "0")):
            with self.subTest(result=result, rc=rc):
                done, reply, _ = self.guard(result, rc)
                self.assertNotEqual(done.returncode, 0)
                self.assertIsNone(reply)

    def test_a_reply_carrying_the_key_is_never_published(self):
        escaped = "".join(f"\\u{ord(c):04x}" for c in self.KEY)
        for raw in (json.dumps({"result": f"green {self.KEY}", "num_turns": 5}),
                    # JSON-escaped in the result file: literal only once decoded.
                    '{"result": "green ' + escaped + '", "num_turns": 5}'):
            with self.subTest(raw=raw[:40]):
                done, reply, kept = self.guard(raw)
                self.assertNotEqual(done.returncode, 0)
                self.assertIsNone(reply)
                self.assertFalse(kept)


class ApiErrorDiagnostic(unittest.TestCase):
    """agent-error.py is gated on the OUTCOME, not on the exit code alone.

    Claude Code reports an API-level failure as
    `{"type":"result","subtype":"success","is_error":true,"result":"API Error:
    400 ..."}` and EXITS 0. An `rc != 0` gate therefore skipped the one shape
    `_api_error_cause` exists to name: the axis uploaded no verdict, the card
    read `no output`, and a spend-capped key was indistinguishable from a model
    that answered nothing — diagnosable only by opening the job log. The panel's
    direct cell has always gated this on `status != ok`; these pin the axis to
    the same rule.
    """

    # The shape a capped key actually produces, `subtype` included.
    CAPPED = {"type": "result", "subtype": "success", "is_error": True,
              "num_turns": 1, "api_error_status": 400,
              "result": "API Error: 400 {\"type\":\"error\",\"error\":{\"type\":"
                        "\"invalid_request_error\",\"message\":\"You have reached your "
                        "specified API usage limits. You will regain access on "
                        "2026-11-01 at 00:00 UTC.\"}}"}

    def gate(self):
        """The `if` condition guarding agent-error.py, read out of the workflow."""
        body = run_block(STEPS["Run the axis (direct)"])
        lines = body.split("\n")
        # The INVOCATION, not a comment that merely names the script.
        at = next(i for i, ln in enumerate(lines)
                  if '"$CURSOR_REVIEW_ASSETS/agent-error.py"' in ln)
        cond = next(lines[j] for j in range(at, -1, -1) if lines[j].startswith("if "))
        return cond[len("if "):].rstrip().removesuffix("; then")

    def fires(self, result, rc="0"):
        """Does the real condition fire for this result file and exit code?"""
        work = tempfile.mkdtemp()
        res = os.path.join(work, "axis-claude-result.json")
        if result is not None:
            with open(res, "w", encoding="utf-8") as f:
                f.write(result if isinstance(result, str) else json.dumps(result))
        cond = self.gate().replace("/tmp/axis-claude-result.json", res)
        done = subprocess.run(["bash", "-c", f'rc={rc}; if {cond}; then echo FIRES; fi'],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip() == "FIRES"

    def test_an_api_error_on_a_zero_exit_is_diagnosed(self):
        # The regression: rc 0, is_error true. An rc-only gate stays silent.
        self.assertTrue(self.fires(self.CAPPED, rc="0"))

    def test_a_non_zero_exit_is_still_diagnosed(self):
        self.assertTrue(self.fires({"result": "x", "num_turns": 5}, rc="1"))

    def test_a_clean_run_is_not_diagnosed(self):
        # No noise on the happy path, which is why this is gated at all.
        self.assertFalse(self.fires({"result": "ok", "num_turns": 5, "is_error": False}))

    def test_an_unreadable_result_never_breaks_the_gate(self):
        # Fails closed toward "no extra diagnostic"; the guards below still
        # report, so a missing or junk file must not abort the step.
        for result in (None, "not json", "[]", '{"is_error": "true"}', ""):
            with self.subTest(result=result):
                self.assertFalse(self.fires(result))

    def test_the_cause_reaches_the_log(self):
        """End to end: the gate fires AND agent-error.py names the cause."""
        work = tempfile.mkdtemp()
        res = os.path.join(work, "result.json")
        with open(res, "w", encoding="utf-8") as f:
            json.dump(self.CAPPED, f)
        script = os.path.join(ROOT, ".github", "cursor-review", "agent-error.py")
        done = subprocess.run(["python3", "-I", script, "--events", res, "--label", "axis-correctness/m"],
                              capture_output=True, text=True,
                              env=dict(os.environ, ANTHROPIC_API_KEY=ReplyGuards.KEY))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("api_error_status=400", done.stdout)
        self.assertIn("API Error: 400", done.stdout)

    def test_the_terminal_message_points_at_the_printed_cause(self):
        # The guard's own line must not re-strand the reader after all this.
        body = run_block(STEPS["Run the axis (direct)"])
        self.assertIn("claude reported an error result; the cause", body)


class VerdictTripwire(unittest.TestCase):
    """The python3 -I check in the direct job's `Extract the verdict`, executed."""

    KEY = ReplyGuards.KEY

    def check(self, verdict_text):
        body = run_block(STEPS["Extract the verdict"])
        script = body[body.index("<<'PY'\n") + len("<<'PY'\n"):body.rindex("PY\n")]
        path = os.path.join(tempfile.mkdtemp(), "correctness.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(verdict_text)
        env = dict(os.environ, ANTHROPIC_API_KEY=self.KEY)
        done = subprocess.run(["python3", "-I", "-", path], input=script, env=env, capture_output=True, text=True)
        return done, os.path.exists(path)

    def test_the_check_runs_before_the_verdict_is_printed(self):
        body = run_block(STEPS["Extract the verdict"])
        self.assertLess(body.index("aggregate.py\" extract"), body.index("python3 -I -"))
        self.assertLess(body.index("python3 -I -"), body.index("cat \"$RUNNER_TEMP/axis-out/$AXIS.json\""))
        self.assertIn("ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}", STEPS["Extract the verdict"])

    def test_a_clean_verdict_passes(self):
        done, kept = self.check(json.dumps({"verdict": "green", "headline": "ok"}))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(kept)

    def test_a_verdict_carrying_the_key_is_removed(self):
        escaped = "".join(f"\\u{ord(c):04x}" for c in self.KEY)
        for text in (json.dumps({"verdict": "green", "headline": self.KEY}),
                     '{"verdict": "green", "headline": "' + escaped + '"}'):
            with self.subTest(text=text[:40]):
                done, kept = self.check(text)
                self.assertNotEqual(done.returncode, 0)
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
        # A record step that wrote nothing must reach the warning, not upload nothing.
        self.assertIn("if-no-files-found: error", STEPS["Upload usage artifact"])
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
