"""The optional direct-Anthropic judge in cursor-review.yml (`judge_direct_model`).

Five properties, each one a single line in the workflow that nothing else
would notice breaking:

  * preflight's resolution — `judge_direct=true` only for a plain id with the
    key present, else a warning and the Cursor judge. EXECUTED: the real
    `Define panel models` script, run by test_workflow_panel_integrity's helper.
  * job isolation — a job is sent every secret its steps reference, skipped
    ones included, so ANTHROPIC_API_KEY lives in its own `judge-direct` job
    (hosted, no shell agent, every stdin Python `-I`) and never in
    `consolidate`, which follows `runs_on` and runs the Cursor shell agent.
  * the hand-back — `consolidate` adopts the `judge-direct` review over its
    seed only from a job that succeeded, and only a JSON object. EXECUTED.
  * the exfil guard — a final review carrying the key is replaced by the
    `--init` error seed, so `Build consolidated findings file` takes the
    degraded panel-union path. EXECUTED, both steps.
  * token usage — the judge records the API's token counts as
    `usage-direct-judge-<model>` with the cells' script, byte for byte but
    for the result path, whenever the judge step ran, and never fails the
    job. EXECUTED.

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from test_workflow_panel_integrity import (
    ASSETS,
    AGGREGATE_STEP,
    PANEL_MODELS_STEP,
    block_mapping,
    code_lines,
    evaluate,
    read_workflow,
    render,
    run_block,
    run_step,
    split_jobs,
    split_steps,
    step_named,
    step_scalar,
)

JUDGE_MODEL = "claude-opus-5-5"
CURSOR_JUDGE = "claude-opus-4-8-thinking-max"
KEY = "sk-ant-test-0123456789abcdef"

CONFIGURE_STEP = "Configure structured final review"
CURSOR_JUDGE_STEP = "Run judge"
DIRECT_JUDGE_STEP = "Run judge (direct)"
GUARD_STEP = "Refuse a judge review carrying the API key"
BUILD_STEP = "Build consolidated findings file"
ADOPT_STEP = "Adopt direct judge review"
DOWNLOAD_STEP = "Download direct judge review"
UPLOAD_STEP = "Upload direct judge review"
RECORD_STEP = "Record token usage"
USAGE_UPLOAD_STEP = "Upload usage artifact"
ARTIFACT = "cursor-review-judge-direct"
# Rebuilt in `judge-direct` exactly as in `consolidate`, so both judges read
# the same prompt over the same panel.
SHARED_STEPS = ("Checkout PR repo", "Load cursor-review assets", "Download reviewed diff",
                "Download prior-review ledger", "Download panel findings",
                AGGREGATE_STEP, "Build judge prompt")


def jobs():
    return split_jobs(read_workflow())


def step(job_lines, name):
    body = step_named(job_lines, name)
    if body is None:
        raise AssertionError(f"step `{name}` is gone")
    return body


def run_with(step_lines, context, workdir, extra_env):
    """`run_step`, plus env the runner supplies from outside the step's own
    `env:` (the workflow-level JUDGE_MODEL)."""
    script = run_block(step_lines)
    env = {"PATH": os.environ.get("PATH", ""), "CURSOR_REVIEW_ASSETS": ASSETS,
           "GITHUB_OUTPUT": os.path.join(workdir, "github-output"), **extra_env}
    open(env["GITHUB_OUTPUT"], "w", encoding="utf-8").close()
    for name, value in block_mapping(step_lines, "        env:", 10).items():
        env[name] = render(value, context)
    return subprocess.run(
        [shutil.which("bash"), "-e", "-c", script.replace("/tmp/", workdir + "/")],
        env=env, cwd=workdir, capture_output=True, text=True, timeout=60,
    )


class PreflightResolvesTheDirectJudgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ("bash", "jq", "python3"):
            if shutil.which(tool) is None:
                raise AssertionError(f"`{tool}` is required to execute the workflow steps")
        cls.preflight = jobs()["preflight"]

    def resolve(self, model, key_present="true", effort="xhigh"):
        """(needs.preflight.outputs.*, stdout) after `Define panel models`."""
        with tempfile.TemporaryDirectory() as workdir:
            done, step_outputs = run_step(
                step(self.preflight, PANEL_MODELS_STEP),
                {
                    "inputs.judge_direct_model": model,
                    "inputs.judge_direct_effort": effort,
                    "needs.gate.outputs.anthropic_key_present": key_present,
                },
                workdir,
            )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        steps = {"steps.models.outputs.%s" % k: v for k, v in step_outputs.items()}
        outputs = {
            name: render(value, steps)
            for name, value in block_mapping(self.preflight, "    outputs:", 6).items()
        }
        return outputs, done.stdout

    def test_set_with_the_key_present_runs_direct(self):
        outputs, log = self.resolve(JUDGE_MODEL, effort="max")
        self.assertEqual(outputs["judge_direct"], "true")
        self.assertEqual(outputs["judge_direct_effort"], "max")
        self.assertNotIn("::warning::judge_direct", log)

    def test_unset_changes_nothing(self):
        outputs, log = self.resolve("", key_present="")
        baseline, _ = self.resolve("", key_present="", effort="")
        self.assertEqual(outputs["judge_direct"], "false")
        self.assertNotIn("judge_direct", log)
        # The panel list is the judge-free baseline's: the judge never edits it.
        self.assertEqual(outputs["models"], baseline["models"])
        self.assertEqual(outputs["models"], self.resolve(JUDGE_MODEL)[0]["models"])

    def test_set_without_the_key_warns_and_stays_on_cursor(self):
        for key_present in ("false", ""):
            with self.subTest(key_present=key_present):
                outputs, log = self.resolve(JUDGE_MODEL, key_present=key_present)
                self.assertEqual(outputs["judge_direct"], "false")
                self.assertIn("::warning::judge_direct_model is set but the ANTHROPIC_API_KEY", log)

    def test_a_non_plain_id_warns_and_stays_on_cursor(self):
        for model in ("claude opus", "claude/opus", "a" * 101, "claude-opus-5-5\nx", "$(id)"):
            with self.subTest(model=model):
                outputs, log = self.resolve(model)
                self.assertEqual(outputs["judge_direct"], "false")
                self.assertIn("::warning::judge_direct_model is not a plain model id", log)

    def test_an_unknown_effort_falls_back_to_xhigh(self):
        outputs, log = self.resolve(JUDGE_MODEL, effort="extreme")
        self.assertEqual(outputs["judge_direct"], "true")
        self.assertEqual(outputs["judge_direct_effort"], "xhigh")
        self.assertIn("::warning::judge_direct_effort", log)


class JudgeDirectJobIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        all_jobs = jobs()
        cls.consolidate = all_jobs["consolidate"]
        cls.direct = all_jobs["judge-direct"]

    def job_scalar(self, job_lines, key):
        line = next(l for l in code_lines(job_lines) if l.startswith(f"    {key}:"))
        return line.split(":", 1)[1].strip()

    def test_the_key_never_reaches_consolidate(self):
        self.assertFalse(any("ANTHROPIC_API_KEY" in l for l in code_lines(self.consolidate)))
        self.assertIn("runs-on: ${{ fromJSON(inputs.runs_on", "\n".join(code_lines(self.consolidate)))

    def test_the_direct_job_is_hosted_and_read_only(self):
        self.assertEqual(self.job_scalar(self.direct, "runs-on"), "ubuntu-latest")
        self.assertEqual(block_mapping(self.direct, "    permissions:", 6), {"contents": "read"})
        self.assertEqual(self.job_scalar(self.direct, "timeout-minutes"),
                         self.job_scalar(self.consolidate, "timeout-minutes"))

    def test_the_direct_job_runs_no_shell_agent_and_no_bare_stdin_python(self):
        body = code_lines(self.direct)
        self.assertFalse(any("cursor-agent" in l or "install-cursor-cli" in l for l in body))
        # A stdin script puts the cwd — the PR checkout — first on sys.path.
        self.assertFalse([l for l in body if re.search(r"python3 -(\s|$)", l)])
        # The judge's MCP config, the exfil guard's count and `Record token usage`.
        self.assertEqual([l.strip().split()[0] for l in body if re.search(r"python3 -I -", l)],
                         ["mcp_config=\"$(python3", "python3", "python3"])

    def test_it_runs_only_when_consolidate_would_and_the_judge_is_direct(self):
        direct_if = self.job_scalar(self.direct, "if")
        consolidate_if = self.job_scalar(self.consolidate, "if")
        self.assertEqual(direct_if, consolidate_if.replace(
            " }}", " && needs.preflight.outputs.judge_direct == 'true' }}"))
        self.assertEqual(self.job_scalar(self.direct, "needs"),
                         self.job_scalar(self.consolidate, "needs").replace(", judge-direct]", "]"))

    def test_the_rebuilt_judge_input_is_consolidates(self):
        for name in SHARED_STEPS:
            with self.subTest(step=name):
                self.assertEqual(step(self.direct, name), step(self.consolidate, name))

    def test_exactly_one_judge_runs(self):
        context = {"steps.aggregate.outputs.ok_count": "3"}
        for judge_direct, cursor_runs in (("true", False), ("false", True), ("", True)):
            ctx = {**context, "needs.preflight.outputs.judge_direct": judge_direct}
            self.assertEqual(bool(evaluate(step_scalar(step(self.consolidate, CURSOR_JUDGE_STEP), "if"), ctx)),
                             cursor_runs, judge_direct)
            self.assertEqual(bool(evaluate(step_scalar(step(self.consolidate, ADOPT_STEP), "if"), ctx)),
                             not cursor_runs, judge_direct)
        # A CLI that did not install or verify clean never runs: the seed then
        # takes the degraded path, like a failed Cursor judge.
        run_if = step_scalar(step(self.direct, DIRECT_JUDGE_STEP), "if")
        self.assertTrue(evaluate(run_if, {**context, "steps.claude_verify.outcome": "success"}))
        self.assertFalse(evaluate(run_if, {**context, "steps.claude_verify.outcome": "failure"}))

    def test_the_review_is_adopted_only_from_a_job_that_succeeded(self):
        download = step(self.consolidate, DOWNLOAD_STEP)
        base = {"steps.aggregate.outputs.ok_count": "3", "needs.preflight.outputs.judge_direct": "true"}
        self.assertTrue(evaluate(step_scalar(download, "if"), {**base, "needs.judge-direct.result": "success"}))
        for result in ("failure", "cancelled", "skipped"):
            self.assertFalse(evaluate(step_scalar(download, "if"), {**base, "needs.judge-direct.result": result}))
        self.assertIn(f"name: {ARTIFACT}", "\n".join(code_lines(download)))
        upload = step(self.direct, UPLOAD_STEP)
        self.assertIn(f"name: {ARTIFACT}", "\n".join(code_lines(upload)))
        # A 409 on the name must fail the job, so it is never absorbed.
        self.assertIsNone(step_scalar(upload, "continue-on-error"))

    def test_the_cursor_judge_step_is_unchanged_otherwise(self):
        body = code_lines(step(self.consolidate, CURSOR_JUDGE_STEP))
        self.assertFalse(any("ANTHROPIC" in l or "claude" in l for l in body))
        self.assertTrue(any(l.strip().startswith("cursor-agent") for l in body))
        self.assertEqual(step_scalar(step(self.consolidate, CURSOR_JUDGE_STEP), "continue-on-error"), "true")

    def test_the_direct_judge_matches_the_cursor_judges_cap_and_absorption(self):
        cursor, direct = step(self.consolidate, CURSOR_JUDGE_STEP), step(self.direct, DIRECT_JUDGE_STEP)
        for key in ("timeout-minutes", "continue-on-error"):
            self.assertEqual(step_scalar(direct, key), step_scalar(cursor, key), key)

    def test_the_direct_judge_is_confined(self):
        body = code_lines(step(self.direct, DIRECT_JUDGE_STEP))
        start = next(i for i, l in enumerate(body) if l.strip().startswith("claude -p"))
        argv = []
        for line in body[start:]:
            argv.append(line.strip())
            if not line.rstrip().endswith("\\"):
                break
        argv = " ".join(argv)
        for flag in (
            "--restricted", '--setting-sources ""', '--tools "Read,Grep,Glob"', "--strict-mcp-config",
            '--allowedTools "Read,Grep,Glob,mcp__cursor-review-output__cursor_review_submit_final"',
            '--disallowedTools "Read(//proc/**),Read(//sys/**)"', "--permission-prompts none",
            '--model "$JUDGE_DIRECT_MODEL"', '--effort "$JUDGE_DIRECT_EFFORT"',
            "--output-format json", "--no-session-persistence", "< /tmp/judge-prompt.txt",
        ):
            self.assertIn(flag, argv)
        # Drops Grep and Glob on the pinned CLI (see the cells' twin test).
        self.assertNotIn("--bare", argv)
        script = "\n".join(body)
        self.assertLess(script.index("find . -type l -delete"), script.index("claude -p"))
        self.assertIn('--mode", "judge"', script)
        self.assertIn('"--out", "/tmp/judge-findings.json"', script)
        self.assertIn('mktemp -d -p "$RUNNER_TEMP"', script)
        env = block_mapping(step(self.direct, DIRECT_JUDGE_STEP), "        env:", 10)
        self.assertEqual(env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"), "'1'")
        # The result is model output steered by PR text: never echoed.
        self.assertFalse(any(re.search(r"\b(cat|head|tail)\b.*judge-claude-result", l) for l in body))

    def test_the_cli_is_installed_from_the_manifest_and_time_boxed(self):
        for name, cap in (("Install Claude Code CLI", "5"), ("Verify Claude Code CLI version", "2")):
            body = step(self.direct, name)
            self.assertEqual(step_scalar(body, "continue-on-error"), "true", name)
            self.assertEqual(step_scalar(body, "timeout-minutes"), cap, name)
        install = "\n".join(code_lines(step(self.direct, "Install Claude Code CLI")))
        self.assertIn('manifest="$CURSOR_REVIEW_ASSETS/package.json"', install)
        self.assertNotRegex(install, r"claude-code@\d")
        # The Cursor CLI is installed only for the Cursor judge.
        cursor = step(self.consolidate, "Install Cursor agent CLI")
        self.assertEqual(step_scalar(cursor, "if"), "needs.preflight.outputs.judge_direct != 'true'")


class ExfilGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        all_jobs = jobs()
        cls.consolidate = all_jobs["consolidate"]
        cls.direct = all_jobs["judge-direct"]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workdir = tmp.name
        self.context = {
            "needs.preflight.outputs.judge_direct": "true",
            "inputs.judge_direct_model": JUDGE_MODEL,
            "secrets.ANTHROPIC_API_KEY": KEY,
        }

    def configure(self, judge_direct):
        done = run_with(step(self.consolidate, CONFIGURE_STEP),
                        {**self.context, "needs.preflight.outputs.judge_direct": judge_direct},
                        self.workdir, {"JUDGE_MODEL": CURSOR_JUDGE})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return self.read()

    def read(self):
        with open(os.path.join(self.workdir, "judge-findings.json"), encoding="utf-8") as f:
            return json.load(f)

    def submit(self, body):
        record = {"model": f"{JUDGE_MODEL} (direct)", "review_type": "judge", "status": "ok",
                  "findings": [{"file": "a.py", "line": 1, "side": "RIGHT", "severity": "high", "body": body}]}
        with open(os.path.join(self.workdir, "judge-findings.json"), "w", encoding="utf-8") as f:
            json.dump(record, f)
        return record

    def guard(self):
        body = step(self.direct, GUARD_STEP)
        self.assertEqual(step_scalar(body, "if"), "always()")
        return run_step(body, self.context, self.workdir)[0]

    def hand_back(self, record=None):
        """`judge-direct`'s file into consolidate's seed via `Adopt direct
        judge review`. `record` replaces the uploaded file when given."""
        src = os.path.join(self.workdir, "judge-direct")
        os.makedirs(src, exist_ok=True)
        if record is not None:
            with open(os.path.join(src, "judge-findings.json"), "w", encoding="utf-8") as f:
                f.write(record)
        else:
            shutil.copyfile(os.path.join(self.workdir, "judge-findings.json"),
                            os.path.join(src, "judge-findings.json"))
        self.configure("true")
        done = run_step(step(self.consolidate, ADOPT_STEP), self.context, self.workdir)[0]
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def build(self):
        with open(os.path.join(self.workdir, "panel.json"), "w", encoding="utf-8") as f:
            json.dump([{"model": "kimi-k3-max", "review_type": "adversarial", "status": "ok",
                        "findings": [{"file": "b.py", "line": 2, "body": "panel finding"}]}], f)
        done, outputs = run_step(step(self.consolidate, BUILD_STEP),
                                 {"steps.aggregate.outputs.ok_count": "1",
                                  "steps.aggregate.outputs.panel_inconsistent": "false"},
                                 self.workdir)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        with open(os.path.join(self.workdir, "consolidated.json"), encoding="utf-8") as f:
            return outputs, json.load(f)

    def test_the_seed_names_the_judge_that_will_run(self):
        self.assertEqual(self.configure("true")["model"], f"{JUDGE_MODEL} (direct)")
        self.assertEqual(self.configure("false")["model"], CURSOR_JUDGE)

    def test_a_review_carrying_the_key_is_replaced_by_the_seed(self):
        self.configure("true")
        self.submit(f"leaked {KEY} here")
        done = self.guard()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("::error::", done.stdout)
        self.assertNotIn(KEY, done.stdout + done.stderr)
        record = self.read()
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["findings"], [])
        self.assertEqual(record["model"], f"{JUDGE_MODEL} (direct)")
        self.assertNotIn(KEY, json.dumps(record))
        self.hand_back()
        outputs, consolidated = self.build()
        self.assertEqual(outputs["degraded"], "true")
        self.assertEqual([f["body"] for f in consolidated["findings"]], ["panel finding"])

    def test_a_clean_review_flows_through_unchanged(self):
        self.configure("true")
        submitted = self.submit("a real finding")
        done = self.guard()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.read(), submitted)
        # Status and count only: the run log is public.
        self.assertIn("Direct judge: status=ok, 1 finding(s).", done.stdout)
        self.assertNotIn("a real finding", done.stdout)
        self.hand_back()
        self.assertEqual(self.read(), submitted)
        outputs, consolidated = self.build()
        self.assertEqual(outputs["degraded"], "false")
        self.assertEqual(consolidated["findings"], submitted["findings"])

    def test_a_judge_that_never_submitted_takes_the_degraded_path(self):
        self.configure("true")
        done = self.guard()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Direct judge: status=error, 0 finding(s).", done.stdout)
        self.hand_back()
        outputs, _ = self.build()
        self.assertEqual(outputs["degraded"], "true")

    def test_an_untrusted_status_is_not_echoed(self):
        with open(os.path.join(self.workdir, "judge-findings.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "::error::forged", "findings": "x"}, f)
        done = self.guard()
        self.assertIn("Direct judge: status=?, 0 finding(s).", done.stdout)
        self.assertNotIn("forged", done.stdout)

    def test_a_non_object_or_missing_hand_back_leaves_the_seed(self):
        for record in ("[1, 2]", "not json", '"ok"'):
            with self.subTest(record=record):
                done = self.hand_back(record)
                self.assertIn("seed stays", done.stdout)
                self.assertEqual(self.read()["status"], "error")
                outputs, _ = self.build()
                self.assertEqual(outputs["degraded"], "true")
        os.remove(os.path.join(self.workdir, "judge-direct", "judge-findings.json"))
        self.configure("true")
        done = run_step(step(self.consolidate, ADOPT_STEP), self.context, self.workdir)[0]
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.read()["status"], "error")


class JudgeTokenUsageTest(unittest.TestCase):
    """`Record token usage` and `Upload usage artifact` in `judge-direct`: the
    direct cells' accounting, for the judge. RecordTokenUsageTest covers the
    script itself; this pins that the judge runs the same script over its own
    result, when, and that accounting can never fail or reach the hand-off."""

    CONTEXT = {"inputs.judge_direct_model": JUDGE_MODEL, "github.run_attempt": "1"}

    @classmethod
    def setUpClass(cls):
        all_jobs = jobs()
        cls.cell = all_jobs["review-anthropic-direct"]
        cls.direct = all_jobs["judge-direct"]
        cls.record_step = step(cls.direct, RECORD_STEP)
        cls.upload = step(cls.direct, USAGE_UPLOAD_STEP)

    def record(self, result):
        workdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, workdir)
        if result is not None:
            with open(os.path.join(workdir, "judge-claude-result.json"), "w", encoding="utf-8") as f:
                json.dump(result, f)
        done, _ = run_step(self.record_step, self.CONTEXT, workdir)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(os.path.join(workdir, "usage-out", "usage.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_the_script_is_the_cells_but_for_the_result_path(self):
        cell = run_block(step(self.cell, RECORD_STEP))
        self.assertIn("/tmp/claude-result.json", cell)
        self.assertEqual(run_block(self.record_step),
                         cell.replace("/tmp/claude-result.json", "/tmp/judge-claude-result.json"))
        # ...which is where `Run judge (direct)` writes it.
        self.assertIn("> /tmp/judge-claude-result.json", "\n".join(code_lines(step(self.direct, DIRECT_JUDGE_STEP))))

    def test_it_records_the_judges_counts_and_nothing_model_written(self):
        record = self.record({
            "result": "MODEL-WRITTEN TEXT",
            "total_cost_usd": 0.4321,
            "usage": {"input_tokens": 12, "output_tokens": 2100, "cache_read_input_tokens": 90000,
                      "cache_creation_input_tokens": 30000,
                      "cache_creation": {"ephemeral_5m_input_tokens": 30000, "ephemeral_1h_input_tokens": 0}},
            "modelUsage": {JUDGE_MODEL: {"inputTokens": 12, "outputTokens": 2100,
                                         "cacheReadInputTokens": 90000, "cacheCreationInputTokens": 30000}},
            "num_turns": 4,
            "duration_ms": 41000,
        })
        self.assertEqual((record["model"], record["review_type"], record["run_attempt"], record["measured"]),
                         (JUDGE_MODEL, "judge", 1, True))
        self.assertEqual((record["usage"]["output_tokens"], record["usage"]["ephemeral_5m_input_tokens"]),
                         (2100, 30000))
        for leaked in ("MODEL-WRITTEN TEXT", "0.4321"):
            self.assertNotIn(leaked, json.dumps(record))

    def test_a_judge_that_left_no_result_is_unmeasured(self):
        # Its cap killed it, or it never got a word out: tokens may still have
        # been spent, so the record says unknown, never a measured zero.
        record = self.record(None)
        self.assertFalse(record["measured"])
        self.assertNotIn("usage", record)

    def test_it_runs_whenever_the_judge_step_did(self):
        self.assertEqual(step_scalar(step(self.direct, DIRECT_JUDGE_STEP), "id"), "judge_direct")
        for body in (self.record_step, self.upload):
            condition = step_scalar(body, "if")
            for outcome, runs in (("success", True), ("failure", True), ("cancelled", True), ("skipped", False)):
                with self.subTest(outcome=outcome):
                    self.assertEqual(bool(evaluate(condition, {"steps.judge_direct.outcome": outcome})), runs)

    def test_accounting_never_fails_the_job_or_reaches_the_hand_off(self):
        for body in (self.record_step, self.upload):
            self.assertEqual(step_scalar(body, "continue-on-error"), "true")
            self.assertFalse(any("ANTHROPIC_API_KEY" in l for l in code_lines(body)))
        name = block_mapping(self.upload, "        with:", 10)["name"]
        self.assertEqual(name, "usage-direct-judge-${{ inputs.judge_direct_model }}")
        self.assertFalse(name.startswith("findings-"), "consolidate downloads `findings-*` as panel cells")
        self.assertNotEqual(name, ARTIFACT)
        # Last, after the review's own upload, so it can never delay the hand-off.
        names = [l.strip()[len("- name: "):] for l in self.direct if l.startswith("      - name: ")]
        self.assertEqual(names[-3:], [UPLOAD_STEP, RECORD_STEP, USAGE_UPLOAD_STEP])


if __name__ == "__main__":
    unittest.main()
