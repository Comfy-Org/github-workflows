import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

mcp = load('review_output', 'review-output-mcp.py')
evaluator = load('reviewer_eval', 'reviewer-eval.py')
HEAD = 'b' * 40
BASE = 'a' * 40
PROOF = 'https://example.com/evidence'
FINDING = {'file': 'src/app.py', 'line': 5, 'side': 'RIGHT', 'severity': 'high', 'body': 'An issue.'}


def record(findings):
    with tempfile.TemporaryDirectory() as directory:
        args = types.SimpleNamespace(mode='judge', out=str(Path(directory) / 'findings.json'), model='test', review_type='judge')
        mcp.call_tool(args, 'cursor_review_submit_final', {'findings': findings})
        return json.loads(Path(args.out).read_text())


def run(findings, judgments):
    output = record(findings)
    digest = hashlib.sha256(json.dumps(output, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return {'repo': 'example/repo', 'base_sha': BASE, 'head_sha': HEAD, 'reviewer_version': 'v1', 'run_proof': PROOF, 'record': output, 'record_sha256': digest,
            'adjudication_complete': True, 'adjudication_proof': PROOF, 'judgments': judgments}


def judgment(index, verdict, bug=None):
    return {'index': index, 'verdict': verdict, 'bug_id': bug, 'proof': PROOF}


def case(kind='bug'):
    findings = [FINDING] if kind == 'bug' else []
    labels = [judgment(0, 'bug', 'issue-1')] if kind == 'bug' else []
    return {'id': kind, 'repo': 'example/repo', 'base_sha': BASE, 'head_sha': HEAD,
            'truth': {'kind': kind, 'proof': PROOF, 'bug_ids': ['issue-1'] if kind == 'bug' else []},
            'baseline': run(findings, labels), 'candidate': run(findings, labels)}


class ReviewerEvaluationTests(unittest.TestCase):
    def test_real_output_scores_matched_bug_and_clean_case(self):
        result = evaluator.evaluate({'baseline_version': 'v1', 'candidate_version': 'v1', 'cases': [case(), case('clean')]})
        self.assertEqual(result['paired_cases'], 2)
        self.assertEqual(result['baseline']['bugs_found'], 1)
        self.assertEqual(result['candidate']['bugs_total'], 1)
        self.assertEqual(result['candidate']['clean_cases_with_findings'], 0)
        self.assertEqual(result['candidate']['clean_cases'], 1)

    def test_missed_bug_and_false_positive_are_distinct(self):
        bug = case()
        bug['candidate'] = run([], [])
        clean = case('clean')
        clean['candidate'] = run([FINDING], [judgment(0, 'false_positive')])
        result = evaluator.evaluate({'baseline_version': 'v1', 'candidate_version': 'v1', 'cases': [bug, clean]})
        self.assertEqual(result['candidate']['bugs_found'], 0)
        self.assertEqual(result['candidate']['bugs_total'], 1)
        self.assertEqual(result['candidate']['false_positive_findings'], 1)
        self.assertEqual(result['candidate']['clean_cases_with_findings'], 1)

    def test_incomplete_or_failed_review_is_excluded_from_both_sides(self):
        for mode in ('unfinished', 'unjudged', 'wrong_head', 'changed_record'):
            item = case()
            if mode == 'unfinished': item['candidate']['record']['status'] = 'error'
            if mode == 'unjudged': item['candidate']['judgments'] = []
            if mode == 'wrong_head': item['candidate']['head_sha'] = 'c' * 40
            if mode == 'changed_record': item['candidate']['record']['findings'][0]['body'] = 'Changed.'
            result = evaluator.evaluate({'baseline_version': 'v1', 'candidate_version': 'v1', 'cases': [item]})
            self.assertEqual(result['paired_cases'], 0, mode)
            self.assertEqual(result['candidate']['bugs_total'], 0, mode)
            self.assertEqual(result['excluded'][0]['case'], 'bug')

    def test_other_base_and_mixed_reviewer_versions_are_excluded(self):
        for field, value in [('base_sha', 'd' * 40), ('repo', 'other/repo'), ('reviewer_version', 'other'), ('run_proof', None)]:
            item = case()
            item['candidate'][field] = value
            result = evaluator.evaluate({'baseline_version': 'v1', 'candidate_version': 'v1', 'cases': [item]})
            self.assertEqual(result['paired_cases'], 0, field)

    def test_duplicate_bug_findings_do_not_inflate_detection(self):
        item = case()
        item['candidate'] = run([FINDING, FINDING], [judgment(0, 'bug', 'issue-1'), judgment(1, 'bug', 'issue-1')])
        result = evaluator.evaluate({'baseline_version': 'v1', 'candidate_version': 'v1', 'cases': [item]})
        self.assertEqual(result['candidate']['bugs_found'], 1)
        self.assertEqual(result['candidate']['duplicate_findings'], 1)

    def test_invalid_truth_or_unknown_bug_is_rejected(self):
        item = case('clean')
        item['candidate'] = run([FINDING], [judgment(0, 'bug', 'invented')])
        with self.assertRaises(ValueError): evaluator.evaluate({'baseline_version': 'v1', 'candidate_version': 'v1', 'cases': [item]})
        item = case()
        item['truth']['proof'] = None
        with self.assertRaises(ValueError): evaluator.evaluate({'baseline_version': 'v1', 'candidate_version': 'v1', 'cases': [item]})

    def test_unknown_truth_and_empty_denominators_are_explicit(self):
        item = case()
        item['truth']['kind'] = 'unknown'
        result = evaluator.evaluate({'baseline_version': 'v1', 'candidate_version': 'v1', 'cases': [item]})
        self.assertEqual(result['excluded'], [{'case': 'bug', 'reason': 'truth unavailable'}])
        self.assertIsNone(result['baseline']['bug_recall'])
        self.assertIsNone(result['baseline']['finding_precision'])
        self.assertIsNone(result['baseline']['clean_case_alarm_rate'])

    def test_documented_example_is_runnable(self):
        text = (ROOT / 'README.md').read_text()
        example = text.split('For example, this **synthetic** confirmed-bug case', 1)[1].split('```json\n', 1)[1].split('\n```', 1)[0]
        result = evaluator.evaluate(json.loads(example))
        self.assertEqual(result['paired_cases'], 1)
        self.assertEqual(result['candidate']['bugs_total'], 1)
        self.assertEqual(result['candidate']['bugs_found'], 0)

    def test_cli_outputs_paired_comparison(self):
        proc = subprocess.run([sys.executable, str(ROOT / 'reviewer-eval.py')], input=json.dumps({'baseline_version': 'v1', 'candidate_version': 'v1', 'cases': [case()]}),
                              text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(proc.stdout)['paired_cases'], 1)


if __name__ == '__main__':
    unittest.main()
