#!/usr/bin/env python3
"""Collect advisory GitHub review evidence for a caller-supplied snapshot."""

from datetime import datetime
import json
import subprocess
import sys

import readiness
import collect_actions
from evidence_errors import EvidenceError

THREAD_QUERY = '''
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved isOutdated }
      }
    }
  }
}
'''


class GitHub:
    def __init__(self, repo, pr):
        self.owner, self.name = repo.split('/')
        self.path = f'repos/{repo}/pulls/{pr}'
        self.pr = pr

    def _api(self, args):
        try:
            result = subprocess.run(['gh', 'api', *args], capture_output=True,
                                    text=True, timeout=120, check=True)
            return json.loads(result.stdout)
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            raise EvidenceError('GitHub request failed') from exc

    def pull(self):
        return self._api([self.path])

    def list_reviews(self):
        pages = self._api(['--paginate', '--slurp', f'{self.path}/reviews?per_page=100'])
        if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
            raise EvidenceError('invalid review pages')
        return [review for page in pages for review in page]

    def list_check_runs(self, head):
        path = f'repos/{self.owner}/{self.name}/commits/{head}/check-runs?per_page=100&filter=all'
        pages = self._api(['--paginate', '--slurp', path])
        if not isinstance(pages, list) or not pages:
            raise EvidenceError('invalid check pages')
        checks = []
        totals = set()
        for page in pages:
            if not isinstance(page, dict) or type(page.get('total_count')) is not int or not isinstance(page.get('check_runs'), list):
                raise EvidenceError('invalid check pages')
            totals.add(page['total_count'])
            checks.extend(page['check_runs'])
        if len(totals) != 1 or len(checks) != next(iter(totals)):
            raise EvidenceError('incomplete check pages')
        return checks

    def job(self, job_id):
        return self._api([f'repos/{self.owner}/{self.name}/actions/jobs/{job_id}'])

    def run(self, run_id):
        return self._api([f'repos/{self.owner}/{self.name}/actions/runs/{run_id}'])

    def threads(self, cursor):
        args = ['graphql', '-f', f'query={THREAD_QUERY}', '-f', f'owner={self.owner}',
                '-f', f'name={self.name}', '-F', f'pr={self.pr}']
        args += ['-F', 'cursor=null'] if cursor is None else ['-f', f'cursor={cursor}']
        try:
            return self._api(args)['data']['repository']['pullRequest']['reviewThreads']
        except (KeyError, TypeError) as exc:
            raise EvidenceError('invalid thread response') from exc


def matches(pull, snapshot):
    try:
        return (pull['state'] == 'open' and pull['base']['sha'] == snapshot['base_sha']
                and pull['head']['sha'] == snapshot['head_sha']
                and pull['user']['login'].casefold() == snapshot['author'].casefold())
    except (KeyError, TypeError, AttributeError):
        return False


def approvals(reviews, head):
    if not isinstance(reviews, list):
        raise EvidenceError('invalid reviews')
    latest = {}
    for item in reviews:
        try:
            state = item['state']
            if state in ('COMMENTED', 'PENDING'):
                continue
            if state not in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
                raise EvidenceError('unknown review state')
            user = item['user']
            login, kind = user['login'], user['type']
            timestamp = datetime.fromisoformat(item['submitted_at'].replace('Z', '+00:00'))
            review_id = item['id']
            if (not isinstance(login, str) or not login.strip() or kind not in ('User', 'Bot')
                    or timestamp.utcoffset() is None or type(review_id) is not int
                    or not isinstance(item['commit_id'], str)):
                raise EvidenceError('invalid reviewer identity')
            key = login.casefold()
            order = (timestamp, review_id)
            if key not in latest or order > latest[key][0]:
                latest[key] = (order, item)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise EvidenceError('invalid review') from exc
    return [{'actor': item['user']['login'], 'human': item['user']['type'] == 'User',
             'state': item['state'].lower()}
            for _, item in latest.values() if item['commit_id'] == head]


def thread_findings(client):
    cursor = None
    seen = set()
    for _ in range(100):
        page = client.threads(cursor)
        try:
            nodes, info = page['nodes'], page['pageInfo']
            if not isinstance(nodes, list) or type(info['hasNextPage']) is not bool:
                raise EvidenceError('invalid thread page')
            for node in nodes:
                if type(node['isResolved']) is not bool or type(node['isOutdated']) is not bool:
                    raise EvidenceError('invalid thread state')
                if not node['isOutdated']:
                    yield {'state': 'resolved' if node['isResolved'] else 'open'}
            if not info['hasNextPage']:
                return
            cursor = info['endCursor']
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise EvidenceError('invalid thread cursor')
            seen.add(cursor)
        except (KeyError, TypeError) as exc:
            raise EvidenceError('invalid thread page') from exc
    raise EvidenceError('thread page limit reached')


def collect(data, client):
    # Validate the complete input contract before any network call.
    base = {'snapshot': data.get('snapshot'), 'policy': data.get('policy'),
            'checks_complete': False, 'checks': [], 'reviews': None, 'findings': None}
    readiness.evaluate(base)
    rules = collect_actions.selectors(base['policy'])
    snapshot = base['snapshot']
    scope = {key: snapshot[key] for key in readiness.SCOPE_KEYS}
    proof = f"https://github.com/{snapshot['repo']}/pull/{snapshot['pr']}"
    matched_checks = []
    status = {'snapshot': 'unavailable', 'reviews': 'not_attempted', 'threads': 'not_attempted', 'actions_checks': 'not_attempted'}
    try:
        if matches(client.pull(), snapshot):
            status['snapshot'] = 'matched'
            try:
                base['reviews'] = {'scope': scope, 'complete': False, 'proof': proof,
                                   'approvals': approvals(client.list_reviews(), snapshot['head_sha'])}
                status['reviews'] = 'observed'
            except EvidenceError:
                status['reviews'] = 'unavailable'
            base['findings'] = {'scope': scope, 'complete': False, 'proof': proof, 'items': []}
            try:
                for item in thread_findings(client):
                    base['findings']['items'].append(item)
                status['threads'] = 'observed'
            except EvidenceError:
                status['threads'] = 'partial'
            try:
                base['reported_failures'], status['actions_checks'], matched_checks = collect_actions.collect(snapshot, rules, client)
            except (EvidenceError, ValueError, TypeError):
                status['actions_checks'] = 'unavailable'
            if not matches(client.pull(), snapshot):
                status['snapshot'] = 'changed'
                base['reviews'], base['findings'], base['reported_failures'] = None, None, []
        else:
            status['snapshot'] = 'changed'
    except EvidenceError:
        status['snapshot'] = 'unavailable'
        base['reviews'], base['findings'], base['reported_failures'] = None, None, []
    if status['snapshot'] != 'matched':
        for key in ('reviews', 'threads', 'actions_checks'):
            if status[key] != 'not_attempted':
                status[key] = 'discarded'
    result = readiness.evaluate(base)
    result['actions_checks_matched'] = matched_checks if status['snapshot'] == 'matched' else []
    result['collection'] = status
    return result


def main():
    try:
        data = json.load(sys.stdin)
        # Evaluate before constructing a client, so repo and PR are validated.
        readiness.evaluate({'snapshot': data.get('snapshot'), 'policy': data.get('policy'),
                            'checks_complete': False, 'checks': []})
        snapshot = data['snapshot']
        result = collect(data, GitHub(snapshot['repo'], snapshot['pr']))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        print(f'invalid readiness input: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
