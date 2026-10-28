"""Find scoped GitHub Actions failures without claiming tested-revision proof."""

from datetime import datetime
import re

import readiness
from evidence_errors import EvidenceError

ACTIONS_APP_ID = 15368
FAILURES = {'failure', 'timed_out', 'action_required', 'startup_failure'}


def selectors(policy):
    rules = policy.get('actions_checks', [])
    if not isinstance(rules, list) or len(rules) > 20:
        raise ValueError('actions_checks must be a list of at most 20 checks')
    required = {item['name'] for item in policy['required_checks']}
    result = {}
    for item in rules:
        if not isinstance(item, dict):
            raise ValueError('invalid actions check')
        name, path = item.get('name'), item.get('workflow_path')
        if (not isinstance(name, str) or name not in required or name in result
                or not isinstance(path, str)
                or not re.fullmatch(r'\.github/workflows/[A-Za-z0-9_-]+\.ya?ml', path)):
            raise ValueError('actions check needs a required name and a workflow path')
        result[name] = path
    return result


def run_matches(run, snapshot, path, attempt):
    if (not isinstance(run, dict) or run.get('event') != 'pull_request'
            or run.get('path') != path or run.get('head_sha') != snapshot['head_sha']
            or run.get('run_attempt') != attempt):
        return False
    prs = run.get('pull_requests')
    if not isinstance(prs, list):
        return False
    return any(isinstance(pr, dict) and pr.get('number') == snapshot['pr']
               and isinstance(pr.get('base'), dict) and pr['base'].get('sha') == snapshot['base_sha']
               and isinstance(pr.get('head'), dict) and pr['head'].get('sha') == snapshot['head_sha']
               for pr in prs)


def collect(snapshot, rules, client):
    if not rules:
        return [], 'not_requested', []
    checks = client.list_check_runs(snapshot['head_sha'])
    if not isinstance(checks, list):
        raise ValueError('invalid checks')
    signals = []
    matched = []
    partial = False
    scope = {key: snapshot[key] for key in readiness.SCOPE_KEYS}
    for name, workflow_path in rules.items():
        relevant = [c for c in checks if isinstance(c, dict) and c.get('name') == name
                    and isinstance(c.get('app'), dict) and c['app'].get('id') == ACTIONS_APP_ID]
        if len(relevant) > 100:
            partial = True
            continue
        candidates = []
        try:
            for check in relevant:
                check_id = check.get('id')
                if type(check_id) is not int or check_id <= 0 or check.get('head_sha') != snapshot['head_sha']:
                    raise ValueError('invalid check')
                started = datetime.fromisoformat(check['started_at'].replace('Z', '+00:00'))
                if started.utcoffset() is None:
                    raise ValueError('invalid check time')
                job = client.job(check_id)
                if (not isinstance(job, dict) or job.get('id') != check_id or job.get('name') != name
                        or job.get('head_sha') != snapshot['head_sha']
                        or job.get('status') != check.get('status')
                        or job.get('conclusion') != check.get('conclusion')
                        or type(job.get('run_id')) is not int or type(job.get('run_attempt')) is not int):
                    raise ValueError('inconsistent job')
                run = client.run(job['run_id'])
                if not isinstance(run, dict) or run.get('id') != job['run_id']:
                    raise ValueError('inconsistent run')
                if run_matches(run, snapshot, workflow_path, job['run_attempt']):
                    candidates.append((started, check, job['run_id']))
            candidates.sort(key=lambda item: item[0], reverse=True)
            if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
                raise ValueError('ambiguous latest check')
            if candidates:
                matched.append(name)
                _, latest, run_id = candidates[0]
                if latest['status'] == 'completed' and latest['conclusion'] in FAILURES:
                    proof = f"https://github.com/{snapshot['repo']}/actions/runs/{run_id}/job/{latest['id']}"
                    signals.append({'scope': scope, 'name': name, 'proof': proof})
        except (EvidenceError, ValueError, TypeError, KeyError, AttributeError):
            # A missing or malformed page/job/run cannot make an older job current.
            partial = True
    return signals, 'partial' if partial else 'observed', matched
