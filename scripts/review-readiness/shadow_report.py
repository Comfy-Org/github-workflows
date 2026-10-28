#!/usr/bin/env python3
"""Create an advisory report using the report-only grader and review collector."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import collect_reviews
import collect_actions
import from_risk
import readiness

RISK_DIR = Path(__file__).resolve().parents[1] / 'pr-risk'


def grade(snapshot):
    with tempfile.TemporaryDirectory(prefix='readiness-risk-') as directory:
        env = os.environ.copy()
        env.pop('GITHUB_OUTPUT', None)
        env.update({
            'REPO': snapshot['repo'], 'PR_NUMBERS': str(snapshot['pr']),
            'BASE_REF': '', 'TOOL_DIR': str(RISK_DIR), 'RESULTS': 'pr-risk-results.jsonl',
            'REPORT_ONLY': '1', 'DRY_RUN': '1', 'PUBLISH_CHECK': '0', 'PUBLISH_COMMENT': '0',
            'WAIT_MINUTES': '0', 'JOB_TIMEOUT_MINUTES': '10', 'MAX_TARGETS': '1',
            'MAP_PATH': '.github/risk.json', 'RB_PATH': '.github/risk-runbooks.json',
            'FLEET_LOGINS': '', 'BOT_LOGINS': '', 'LABEL_MAP': '',
            'SELF_CONTEXT': os.environ.get('GITHUB_WORKFLOW', ''),
            'SELF_RUN_ID': os.environ.get('GITHUB_RUN_ID', ''),
        })
        try:
            proc = subprocess.run(['bash', str(RISK_DIR / 'grade-targets.sh')], cwd=directory,
                                  env=env, capture_output=True, text=True, timeout=600)
            if proc.returncode:
                return None
            lines = (Path(directory) / 'pr-risk-results.jsonl').read_text().splitlines()
            if len(lines) != 1:
                return None
            summary = json.loads(lines[0])
            if not isinstance(summary, dict):
                return None
            # The target number is validated by readiness before it reaches this path.
            record_path = Path(directory) / f"record-{snapshot['pr']}.json"
            record = json.loads(record_path.read_text()) if record_path.exists() else None
            return {'summary': summary, 'record': record}
        except (OSError, ValueError, subprocess.SubprocessError):
            return None


def build(data, client, run_risk=grade, expected_repo=None):
    if not isinstance(data, dict):
        raise ValueError('input must be an object')
    # Validate the snapshot and policy before sending anything to GitHub.
    readiness.evaluate({'snapshot': data.get('snapshot'), 'policy': data.get('policy'),
                        'checks_complete': False, 'checks': []})
    collect_actions.selectors(data['policy'])
    snapshot = data['snapshot']
    if expected_repo is not None and snapshot['repo'].casefold() != expected_repo.casefold():
        raise ValueError('target repository does not match caller')
    report = run_risk(snapshot)
    risk = from_risk.evaluate_report({**data, 'report': report})['risk']
    result = collect_reviews.collect(data, client)
    if result['collection']['snapshot'] != 'matched':
        risk = {'status': 'unknown', 'tier': None}
    result['risk'] = risk
    result['risk_collection'] = 'observed' if report is not None else 'unavailable'
    return result


def main():
    try:
        data = json.load(sys.stdin)
        readiness.evaluate({'snapshot': data.get('snapshot'), 'policy': data.get('policy'),
                            'checks_complete': False, 'checks': []})
        snapshot = data['snapshot']
        result = build(data, collect_reviews.GitHub(snapshot['repo'], snapshot['pr']),
                       expected_repo=os.environ.get('SHADOW_EXPECTED_REPO'))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        print(f'invalid shadow input: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
