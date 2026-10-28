import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
spec = importlib.util.spec_from_file_location('shadow_report', MODULE_DIR / 'shadow_report.py')
shadow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shadow)
from test_collect_reviews import FakeClient, SNAPSHOT, POLICY
from test_from_risk import risk_record


def risk():
    record = risk_record()
    record.update(repo=SNAPSHOT['repo'], pr=SNAPSHOT['pr'], head_sha=SNAPSHOT['head_sha'], author=SNAPSHOT['author'])
    return {'summary': {'pr': SNAPSHOT['pr'], 'base_sha': SNAPSHOT['base_sha'],
                        'tier': record['risk']['tier'], 'status': 'graded'}, 'record': record}


def input_data():
    return {'snapshot': SNAPSHOT, 'policy': POLICY}


class ShadowTests(unittest.TestCase):
    def test_connects_real_risk_grade_to_review_evaluator(self):
        result = shadow.build(input_data(), FakeClient(), lambda snapshot: risk())
        self.assertEqual(result['state'], 'WAITING')
        self.assertEqual(result['risk']['status'], 'observed')
        self.assertEqual(result['collection']['reviews'], 'observed')
        self.assertIn('finding evidence incomplete', result['reasons'])

    def test_known_open_review_thread_still_blocks(self):
        client = FakeClient()
        client.pages[0]['nodes'] = [{'isResolved': False, 'isOutdated': False}]
        result = shadow.build(input_data(), client, lambda snapshot: risk())
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertEqual(result['risk']['status'], 'observed')

    def test_changed_snapshot_discards_risk_and_reviews(self):
        client = FakeClient()
        client.last = {**client.pr, 'head': {'sha': 'd' * 40}}
        result = shadow.build(input_data(), client, lambda snapshot: risk())
        self.assertEqual(result['state'], 'WAITING')
        self.assertEqual(result['risk']['status'], 'unknown')
        self.assertEqual(result['collection']['snapshot'], 'changed')

    def test_missing_grade_is_explicit_and_other_evidence_still_works(self):
        client = FakeClient()
        client.pages[0]['nodes'] = [{'isResolved': False, 'isOutdated': False}]
        result = shadow.build(input_data(), client, lambda snapshot: None)
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertEqual(result['risk']['status'], 'unknown')
        self.assertEqual(result['risk_collection'], 'unavailable')

    def test_stale_grade_never_displays_as_current(self):
        report = risk()
        report['summary']['base_sha'] = 'd' * 40
        result = shadow.build(input_data(), FakeClient(), lambda snapshot: report)
        self.assertEqual(result['risk']['status'], 'stale')
        self.assertIsNone(result['risk']['tier'])

    def test_grader_is_forced_to_report_only_in_an_isolated_directory(self):
        r = risk()
        def invoke(args, *, cwd, env, **kwargs):
            self.assertEqual(env['REPORT_ONLY'], '1')
            self.assertEqual(env['DRY_RUN'], '1')
            self.assertEqual(env['PUBLISH_CHECK'], '0')
            self.assertEqual(env['PUBLISH_COMMENT'], '0')
            self.assertNotIn('GITHUB_OUTPUT', env)
            Path(cwd, 'pr-risk-results.jsonl').write_text(json.dumps(r['summary']) + '\n')
            Path(cwd, f"record-{SNAPSHOT['pr']}.json").write_text(json.dumps(r['record']))
            return type('Proc', (), {'returncode': 0})()
        with patch.object(shadow.subprocess, 'run', side_effect=invoke):
            self.assertEqual(shadow.grade(SNAPSHOT)['summary']['status'], 'graded')

    def test_wrong_expected_repo_rejected(self):
        with self.assertRaises(ValueError):
            shadow.build(input_data(), FakeClient(), lambda snapshot: risk(), expected_repo='other/repo')


if __name__ == '__main__':
    unittest.main()
