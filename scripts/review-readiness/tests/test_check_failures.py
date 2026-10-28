import json
import os
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect_reviews
import readiness
import shadow_report
from test_collect_reviews import FakeClient, SNAPSHOT, POLICY, HEAD, BASE, review


def policy():
    return {**POLICY, 'actions_checks': [{'name': 'unit', 'workflow_path': '.github/workflows/ci.yml'}]}


def check(identifier=101, state='completed', conclusion='failure', started='2026-01-01T00:00:00Z'):
    return {'id': identifier, 'name': 'unit', 'head_sha': HEAD, 'status': state,
            'conclusion': conclusion, 'started_at': started, 'app': {'id': 15368}}


class CheckClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.checks = [check()]
        self.job_data = {'id': 101, 'name': 'unit', 'head_sha': HEAD, 'run_id': 500,
                         'run_attempt': 1, 'status': 'completed', 'conclusion': 'failure'}
        self.run_data = {'id': 500, 'event': 'pull_request', 'run_attempt': 1,
                         'path': '.github/workflows/ci.yml', 'head_sha': HEAD,
                         'pull_requests': [{'number': 42, 'base': {'sha': BASE}, 'head': {'sha': HEAD}}]}

    def list_check_runs(self, head):
        return self.checks

    def job(self, job_id):
        return {**self.job_data, 'id': job_id}

    def run(self, run_id):
        return self.run_data


def run(client):
    return collect_reviews.collect({'snapshot': SNAPSHOT, 'policy': policy()}, client)


class CheckFailuresTests(unittest.TestCase):
    def test_matching_failed_actions_job_blocks_despite_missing_positive_evidence(self):
        result = run(CheckClient())
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertIn('required check reported failure: unit', result['reasons'])
        self.assertIn('check inventory incomplete', result['reasons'])
        self.assertEqual(result['collection']['actions_checks'], 'observed')
        self.assertEqual(result['actions_checks_matched'], ['unit'])

    def test_success_does_not_prove_what_was_tested(self):
        client = CheckClient()
        client.checks[0]['conclusion'] = 'success'
        client.job_data['conclusion'] = 'success'
        result = run(client)
        self.assertEqual(result['state'], 'WAITING')
        self.assertIn('required check incomplete: unit', result['reasons'])

    def test_old_base_head_wrong_workflow_and_wrong_app_do_not_block(self):
        for mutation in ('base', 'head', 'workflow', 'app', 'attempt'):
            client = CheckClient()
            if mutation == 'base': client.run_data['pull_requests'][0]['base']['sha'] = 'd' * 40
            if mutation == 'head': client.run_data['pull_requests'][0]['head']['sha'] = 'd' * 40
            if mutation == 'workflow': client.run_data['path'] = '.github/workflows/other.yml'
            if mutation == 'app': client.checks[0]['app']['id'] = 123
            if mutation == 'attempt': client.job_data['run_attempt'] = 2
            self.assertEqual(run(client)['state'], 'WAITING', mutation)

    def test_newer_pending_check_supersedes_older_failure(self):
        client = CheckClient()
        client.checks.append(check(102, 'in_progress', None, '2026-01-01T00:01:00Z'))
        def job(job_id):
            return {**client.job_data, 'id': job_id, 'status': 'in_progress' if job_id == 102 else 'completed',
                    'conclusion': None if job_id == 102 else 'failure'}
        client.job = job
        self.assertEqual(run(client)['state'], 'WAITING')

    def test_api_failure_is_visible_and_cannot_count_as_clean(self):
        client = CheckClient()
        def fail(): raise collect_reviews.EvidenceError('unavailable')
        client.list_check_runs = lambda head: fail()
        result = run(client)
        self.assertEqual(result['state'], 'WAITING')
        self.assertEqual(result['collection']['actions_checks'], 'unavailable')

    def test_real_cli_processes_paginated_github_check_response(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CheckClient()
            data = {'pull': fixture.pr, 'reviews': [[review()]], 'checks': fixture.checks,
                    'job': fixture.job_data, 'run': fixture.run_data}
            path = Path(directory) / 'fixture.json'
            path.write_text(json.dumps(data))
            gh = Path(directory) / 'gh'
            gh.write_text("""#!/usr/bin/env python3
import json, os, sys
f = json.load(open(os.environ['FIXTURE_PATH']))
args = sys.argv[1:]
if 'graphql' in args:
    value = {'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}}
elif any('check-runs?' in arg for arg in args):
    assert '--paginate' in args and '--slurp' in args and any('filter=all' in arg for arg in args)
    value = [{'total_count': len(f['checks']), 'check_runs': f['checks']}]
elif any('/reviews?' in arg for arg in args): value = f['reviews']
elif any('/actions/jobs/' in arg for arg in args): value = f['job']
elif any('/actions/runs/' in arg for arg in args): value = f['run']
else: value = f['pull']
print(json.dumps(value))
""")
            gh.chmod(0o755)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'], 'FIXTURE_PATH': str(path)}
            proc = subprocess.run([sys.executable, str(Path(collect_reviews.__file__))],
                                  input=json.dumps({'snapshot': SNAPSHOT, 'policy': policy()}),
                                  capture_output=True, text=True, env=env, check=True)
            result = json.loads(proc.stdout)
            self.assertEqual(result['state'], 'BLOCKED')
            self.assertIn('required check reported failure: unit', result['reasons'])

    def test_inconsistent_pages_fail_closed(self):
        client = collect_reviews.GitHub(SNAPSHOT['repo'], SNAPSHOT['pr'])
        client._api = lambda args: [{'total_count': 2, 'check_runs': [check()]}]
        with self.assertRaises(collect_reviews.EvidenceError):
            client.list_check_runs(HEAD)

    def test_indeterminate_newer_job_cannot_revive_old_failure(self):
        client = CheckClient()
        client.checks.append(check(102, 'queued', None, None))
        result = run(client)
        self.assertEqual(result['state'], 'WAITING')
        self.assertEqual(result['collection']['actions_checks'], 'partial')

    def test_snapshot_change_discards_check_failure(self):
        client = CheckClient()
        client.last = {**client.pr, 'head': {'sha': 'd' * 40}}
        result = run(client)
        self.assertEqual(result['state'], 'WAITING')
        self.assertEqual(result['collection']['actions_checks'], 'discarded')
        self.assertEqual(result['actions_checks_matched'], [])

    def test_invalid_source_policy_rejected_before_grading(self):
        data = {'snapshot': SNAPSHOT, 'policy': {**POLICY, 'actions_checks': [{'name': 'unit', 'workflow_path': '../unsafe.yml'}]}}
        with self.assertRaises(ValueError):
            shadow_report.build(data, CheckClient(), lambda snapshot: self.fail('unexpected risk read'))

    def test_no_selector_makes_no_api_calls(self):
        client = CheckClient()
        client.list_check_runs = lambda head: self.fail('unexpected API read')
        result = collect_reviews.collect({'snapshot': SNAPSHOT, 'policy': POLICY}, client)
        self.assertEqual(result['state'], 'WAITING')

    def test_reported_failure_never_satisfies_success_or_unrelated_check(self):
        scope = {k: SNAPSHOT[k] for k in readiness.SCOPE_KEYS}
        data = {'snapshot': SNAPSHOT, 'policy': POLICY, 'checks_complete': False, 'checks': [],
                'reported_failures': [{'scope': scope, 'name': 'other', 'proof': 'https://example.com/job'}]}
        self.assertEqual(readiness.evaluate(data)['state'], 'WAITING')
        data['reported_failures'][0]['name'] = 'unit'
        self.assertEqual(readiness.evaluate(data)['state'], 'BLOCKED')
        data['reported_failures'][0]['scope'] = {**scope, 'base_sha': 'd' * 40}
        self.assertEqual(readiness.evaluate(data)['state'], 'WAITING')


if __name__ == '__main__':
    unittest.main()
