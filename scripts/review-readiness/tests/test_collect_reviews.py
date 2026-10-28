import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
spec = importlib.util.spec_from_file_location('collect_reviews', MODULE_DIR / 'collect_reviews.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

BASE, HEAD, TESTED = 'a' * 40, 'b' * 40, 'c' * 40
SNAPSHOT = {'repo': 'test/repo', 'pr': 42, 'base_sha': BASE, 'head_sha': HEAD,
            'tested_sha': TESTED, 'policy_version': 'v1', 'author': 'author'}
POLICY = {'version': 'v1', 'required_checks': [{'name': 'unit', 'revision': 'tested'}],
          'required_human_approvals': 1}


def review(actor='reviewer', state='APPROVED', sha=HEAD, number=1, kind='User'):
    return {'id': number, 'user': {'login': actor, 'type': kind}, 'state': state,
            'commit_id': sha, 'submitted_at': f'2026-01-01T00:00:{number:02d}Z'}


class FakeClient:
    def __init__(self):
        self.pr = {'state': 'open', 'user': {'login': 'author'},
                   'base': {'sha': BASE}, 'head': {'sha': HEAD}}
        self.last = self.pr
        self.reviews = [review()]
        self.pages = [{'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}]
        self.calls = []
        self.permission_data = {'permission': 'write', 'user': {'login': 'reviewer', 'type': 'User'}}

    def pull(self):
        self.calls.append('pull')
        return self.pr if self.calls.count('pull') == 1 else self.last

    def list_reviews(self):
        self.calls.append('reviews')
        return self.reviews

    def permission(self, login):
        self.calls.append(('permission', login))
        return self.permission_data

    def threads(self, cursor):
        self.calls.append(('threads', cursor))
        return self.pages[0 if cursor is None else 1]


def run(client):
    return collector.collect({'snapshot': SNAPSHOT, 'policy': POLICY}, client)


class CollectReviewsTests(unittest.TestCase):
    def test_review_permission_read_is_separate_from_other_incomplete_evidence(self):
        result = run(FakeClient())
        self.assertEqual(result['state'], 'WAITING')
        self.assertNotIn('required human approvals incomplete', result['reasons'])
        self.assertIn('finding evidence incomplete', result['reasons'])
        self.assertIn('check inventory incomplete', result['reasons'])

    def test_eligible_current_approval_is_recognized_after_permission_check(self):
        client = FakeClient()
        result = run(client)
        self.assertIn(('permission', 'reviewer'), client.calls)
        self.assertEqual(result['collection']['reviewer_permissions'], 'observed')
        self.assertNotIn('required human approvals incomplete', result['reasons'])
        self.assertEqual(result['state'], 'WAITING')

    def test_ineligible_or_unavailable_permissions_do_not_count_as_approval(self):
        for value in ('read', 'none', 'invalid', None):
            client = FakeClient()
            client.permission_data['permission'] = value
            result = run(client)
            self.assertIn('required human approvals incomplete', result['reasons'], value)

    def test_permission_failure_stops_further_lookups(self):
        client = FakeClient()
        calls = []
        def unavailable(login):
            calls.append(login)
            raise collector.EvidenceError('unavailable')
        client.permission = unavailable
        client.reviews = [review(), review(actor='other', number=2)]
        result = run(client)
        self.assertEqual(calls, ['reviewer'])
        self.assertEqual(result['collection']['reviewer_permissions'], 'partial')
        self.assertIn('required human approvals incomplete', result['reasons'])

    def test_permission_for_a_different_identity_does_not_count(self):
        client = FakeClient()
        client.permission_data['user']['login'] = 'someone-else'
        self.assertIn('required human approvals incomplete', run(client)['reasons'])

    def test_permission_queries_are_bounded(self):
        client = FakeClient()
        items = [{'actor': f'user-{n}', 'human': True, 'state': 'approved'} for n in range(101)]
        eligible, complete, status = collector.eligible_approvals(items, 'author', client)
        self.assertEqual(eligible, [])
        self.assertFalse(complete)
        self.assertEqual(status, 'partial')
        self.assertFalse(any(isinstance(call, tuple) and call[0] == 'permission' for call in client.calls))

    def test_permission_failure_does_not_hide_change_requests(self):
        client = FakeClient()
        client.reviews = [review(), review(actor='other', state='CHANGES_REQUESTED', number=2)]
        def unavailable(login): raise collector.EvidenceError('unavailable')
        client.permission = unavailable
        result = run(client)
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertIn('human review requests changes', result['reasons'])
        self.assertEqual(result['collection']['reviewer_permissions'], 'partial')
        self.assertIn('required human approvals incomplete', result['reasons'])

    def test_paginated_unresolved_current_thread_blocks(self):
        client = FakeClient()
        client.pages = [
            {'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}},
            {'nodes': [{'isResolved': False, 'isOutdated': False}],
             'pageInfo': {'hasNextPage': False, 'endCursor': None}},
        ]
        result = run(client)
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertIn(('threads', 'next'), client.calls)

    def test_outdated_or_resolved_threads_do_not_block(self):
        client = FakeClient()
        client.pages[0]['nodes'] = [{'isResolved': False, 'isOutdated': True},
                                    {'isResolved': True, 'isOutdated': False}]
        self.assertEqual(run(client)['state'], 'WAITING')

    def test_dismissed_or_old_head_approval_does_not_count(self):
        for reviews in ([review(sha='d' * 40)], [review(), review(state='DISMISSED', number=2)]):
            client = FakeClient()
            client.reviews = reviews
            self.assertIn('required human approvals incomplete', run(client)['reasons'])

    def test_latest_review_state_and_identity(self):
        client = FakeClient()
        client.reviews = [review(state='CHANGES_REQUESTED'), review(number=2),
                          review(actor='robot', kind='Bot', number=3)]
        self.assertEqual(run(client)['state'], 'WAITING')
        client.reviews = [review(), review(state='CHANGES_REQUESTED', number=2)]
        self.assertEqual(run(client)['state'], 'BLOCKED')

    def test_snapshot_change_discards_all_collected_evidence(self):
        client = FakeClient()
        client.pages[0]['nodes'] = [{'isResolved': False, 'isOutdated': False}]
        client.last = {**client.pr, 'head': {'sha': 'd' * 40}}
        result = run(client)
        self.assertEqual(result['state'], 'WAITING')
        self.assertIn('review evidence missing', result['reasons'])
        self.assertIn('finding evidence missing', result['reasons'])
        self.assertEqual(result['collection']['snapshot'], 'changed')

    def test_failed_or_looping_pagination_cannot_claim_complete_inventory(self):
        client = FakeClient()
        client.pages = [{'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}},
                        {'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}}]
        result = run(client)
        self.assertEqual(result['state'], 'WAITING')
        self.assertIn('finding evidence incomplete', result['reasons'])
        self.assertEqual(result['collection']['threads'], 'partial')

    def test_known_open_thread_still_blocks_when_later_page_fails(self):
        client = FakeClient()
        client.pages = [
            {'nodes': [{'isResolved': False, 'isOutdated': False}],
             'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}},
            {'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}},
        ]
        self.assertEqual(run(client)['state'], 'BLOCKED')

    def test_cli_uses_paginated_github_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            fixture = {'pull': FakeClient().pr, 'reviews': [[review()]],
                       'pages': [{'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}},
                                 {'nodes': [{'isResolved': False, 'isOutdated': False}],
                                  'pageInfo': {'hasNextPage': False, 'endCursor': None}}]}
            fixture_path = Path(directory) / 'fixture.json'
            fixture_path.write_text(json.dumps(fixture))
            gh.write_text("""#!/usr/bin/env python3
import json, os, sys
f = json.load(open(os.environ['FIXTURE_PATH']))
if 'graphql' in sys.argv:
    page = f['pages'][int('cursor=next' in sys.argv)]
    value = {'data': {'repository': {'pullRequest': {'reviewThreads': page}}}}
elif any('/collaborators/' in arg for arg in sys.argv):
    value = {'permission': 'write', 'user': {'login': 'reviewer', 'type': 'User'}}
elif '--slurp' in sys.argv:
    value = f['reviews']
else:
    value = f['pull']
print(json.dumps(value))
""")
            gh.chmod(0o755)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                   'FIXTURE_PATH': str(fixture_path)}
            proc = subprocess.run([sys.executable, str(MODULE_DIR / 'collect_reviews.py')],
                                  input=json.dumps({'snapshot': SNAPSHOT, 'policy': POLICY}),
                                  text=True, capture_output=True, env=env, check=True)
            result = json.loads(proc.stdout)
            self.assertEqual(result['state'], 'BLOCKED')
            self.assertIn('unresolved finding', result['reasons'])
            self.assertNotIn('required human approvals incomplete', result['reasons'])

    def test_unbounded_thread_pagination_is_marked_partial(self):
        class Endless(FakeClient):
            def threads(self, cursor):
                self.calls.append(('threads', cursor))
                return {'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': str(len(self.calls))}}
        client = Endless()
        result = run(client)
        self.assertEqual(result['collection']['threads'], 'partial')
        self.assertLessEqual(len([call for call in client.calls if isinstance(call, tuple) and call[0] == 'threads']), 100)

    def test_unavailable_reviews_do_not_hide_known_open_threads(self):
        class UnavailableReviews(FakeClient):
            def list_reviews(self):
                raise collector.EvidenceError('unavailable')
        client = UnavailableReviews()
        client.pages[0]['nodes'] = [{'isResolved': False, 'isOutdated': False}]
        result = run(client)
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertEqual(result['collection']['reviews'], 'unavailable')
        self.assertIn('review evidence missing', result['reasons'])

    def test_failed_final_snapshot_discards_evidence(self):
        class UnavailableFinal(FakeClient):
            def pull(self):
                if self.calls.count('pull'):
                    raise collector.EvidenceError('unavailable')
                return super().pull()
        result = run(UnavailableFinal())
        self.assertEqual(result['collection']['snapshot'], 'unavailable')
        self.assertIn('review evidence missing', result['reasons'])

    def test_invalid_api_identity_is_not_a_human_approval(self):
        client = FakeClient()
        client.reviews = [review(kind='Unknown')]
        self.assertIn('review evidence missing', run(client)['reasons'])


if __name__ == '__main__':
    unittest.main()
