"""The optional direct-Anthropic judge in cursor-review.yml (`judge_direct_model`).

Three properties, each one a single line in the workflow that nothing else
would notice breaking:

  * preflight's resolution — `judge_direct=true` only for a plain id with the
    key present, else a warning and the Cursor judge. EXECUTED: the real
    `Define panel models` script, run by test_workflow_panel_integrity's helper.
  * job isolation — `consolidate` follows `runs_on`, so ANTHROPIC_API_KEY must
    appear only in steps gated on the direct judge, and the job's `runs-on:`
    must resolve to `ubuntu-latest` whenever it can hold the key (the key is
    sent for every step that references it, skipped ones included).
  * the exfil guard — a final review carrying the key is replaced by the
    `--init` error seed, so `Build consolidated findings file` takes the
    degraded panel-union path. EXECUTED, both steps.

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
RUNS_ON_INPUT = "fromJSON(inputs.runs_on || '\"ubuntu-latest\"')"

CONFIGURE_STEP = "Configure structured final review"
CURSOR_JUDGE_STEP = "Run judge"
DIRECT_JUDGE_STEP = "Run judge (direct)"
GUARD_STEP = "Refuse a judge review carrying the API key"
BUILD_STEP = "Build consolidated findings file"


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


class ConsolidateJobIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.consolidate = jobs()["consolidate"]
        cls.steps = split_steps(cls.consolidate)

    def runs_on(self, judge_direct, key_present):
        line = next(l for l in code_lines(self.consolidate) if l.startswith("    runs-on:"))
        expression = line.split(":", 1)[1].strip()
        self.assertIn(RUNS_ON_INPUT, expression, "the runs_on fallback is no longer today's")
        # `fromJSON` is outside `evaluate`'s grammar; a sentinel string stands
        # in for whatever pool the caller's `runs_on` names.
        expression = expression.replace(RUNS_ON_INPUT, "'POOL'")
        return evaluate(expression, {
            "needs.preflight.outputs.judge_direct": judge_direct,
            "needs.gate.outputs.anthropic_key_present": key_present,
        })

    def test_it_is_hosted_whenever_it_can_hold_the_key(self):
        self.assertEqual(self.runs_on("true", "true"), "ubuntu-latest")
        self.assertEqual(self.runs_on("true", ""), "ubuntu-latest")
        # Key passed for the cells only: the direct judge's steps still
        # reference it, so it is still sent to this job.
        self.assertEqual(self.runs_on("false", "true"), "ubuntu-latest")

    def test_without_the_key_it_follows_runs_on_as_today(self):
        self.assertEqual(self.runs_on("false", "false"), "POOL")
        self.assertEqual(self.runs_on("", ""), "POOL")

    def test_the_key_is_only_in_steps_gated_on_the_direct_judge(self):
        holders = []
        for name, body in self.steps:
            if any("secrets.ANTHROPIC_API_KEY" in l for l in code_lines(body)):
                holders.append(name)
                self.assertIn("needs.preflight.outputs.judge_direct == 'true'", step_scalar(body, "if") or "", name)
        self.assertEqual(sorted(holders), sorted([DIRECT_JUDGE_STEP, GUARD_STEP]))

    def test_exactly_one_judge_step_runs(self):
        context = {"steps.aggregate.outputs.ok_count": "3", "steps.claude_verify.outcome": "success"}
        for judge_direct, expected in (("true", DIRECT_JUDGE_STEP), ("false", CURSOR_JUDGE_STEP), ("", CURSOR_JUDGE_STEP)):
            ran = [
                name for name in (CURSOR_JUDGE_STEP, DIRECT_JUDGE_STEP)
                if evaluate(step_scalar(step(self.consolidate, name), "if"),
                            {**context, "needs.preflight.outputs.judge_direct": judge_direct})
            ]
            self.assertEqual(ran, [expected], judge_direct)
        # A CLI that did not install or verify clean never runs: the seed then
        # takes the degraded path, like a failed Cursor judge.
        self.assertFalse(evaluate(step_scalar(step(self.consolidate, DIRECT_JUDGE_STEP), "if"), {
            **context, "needs.preflight.outputs.judge_direct": "true", "steps.claude_verify.outcome": "failure"}))

    def test_the_cursor_judge_step_is_unchanged_otherwise(self):
        body = code_lines(step(self.consolidate, CURSOR_JUDGE_STEP))
        self.assertFalse(any("ANTHROPIC" in l or "claude" in l for l in body))
        self.assertTrue(any(l.strip().startswith("cursor-agent") for l in body))
        self.assertEqual(step_scalar(step(self.consolidate, CURSOR_JUDGE_STEP), "continue-on-error"), "true")

    def test_the_direct_judge_matches_the_cursor_judges_cap_and_absorption(self):
        cursor, direct = step(self.consolidate, CURSOR_JUDGE_STEP), step(self.consolidate, DIRECT_JUDGE_STEP)
        for key in ("timeout-minutes", "continue-on-error"):
            self.assertEqual(step_scalar(direct, key), step_scalar(cursor, key), key)

    def test_the_direct_judge_is_confined(self):
        body = code_lines(step(self.consolidate, DIRECT_JUDGE_STEP))
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
        env = block_mapping(step(self.consolidate, DIRECT_JUDGE_STEP), "        env:", 10)
        self.assertEqual(env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"), "'1'")
        # The result is model output steered by PR text: never echoed.
        self.assertFalse(any(re.search(r"\b(cat|head|tail)\b.*judge-claude-result", l) for l in body))

    def test_the_cli_is_installed_from_the_manifest_only_when_direct(self):
        for name in ("Install Claude Code CLI", "Verify Claude Code CLI version"):
            body = step(self.consolidate, name)
            self.assertIn("needs.preflight.outputs.judge_direct == 'true'", step_scalar(body, "if"))
        install = "\n".join(code_lines(step(self.consolidate, "Install Claude Code CLI")))
        self.assertIn('manifest="$CURSOR_REVIEW_ASSETS/package.json"', install)
        self.assertNotRegex(install, r"claude-code@\d")
        # The Cursor CLI install is untouched and unconditional.
        cursor = step(self.consolidate, "Install Cursor agent CLI")
        self.assertIsNone(step_scalar(cursor, "if"))


class ExfilGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.consolidate = jobs()["consolidate"]

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
        body = step(self.consolidate, GUARD_STEP)
        self.assertTrue(evaluate(step_scalar(body, "if"), self.context))
        return run_step(body, self.context, self.workdir)[0]

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
        outputs, consolidated = self.build()
        self.assertEqual(outputs["degraded"], "false")
        self.assertEqual(consolidated["findings"], submitted["findings"])

    def test_a_judge_that_never_submitted_takes_the_degraded_path(self):
        self.configure("true")
        done = self.guard()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Direct judge: status=error, 0 finding(s).", done.stdout)
        outputs, _ = self.build()
        self.assertEqual(outputs["degraded"], "true")

    def test_an_untrusted_status_is_not_echoed(self):
        with open(os.path.join(self.workdir, "judge-findings.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "::error::forged", "findings": "x"}, f)
        done = self.guard()
        self.assertIn("Direct judge: status=?, 0 finding(s).", done.stdout)
        self.assertNotIn("forged", done.stdout)


if __name__ == "__main__":
    unittest.main()
