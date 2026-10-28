#!/usr/bin/env python3
"""Compare two structured review outputs against human-adjudicated cases."""

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

spec = importlib.util.spec_from_file_location('review_output', Path(__file__).with_name('review-output-mcp.py'))
review_output = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_output)
SHA = re.compile(r'^[a-f0-9]{40}$')
DIGEST = re.compile(r'^[a-f0-9]{64}$')


class Incomplete(Exception):
    pass


def proof(value):
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return url.scheme == 'https' and bool(url.hostname) and not url.username and not url.password
    except ValueError:
        return False


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a nonempty string')
    return value


def digest(record):
    encoded = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def truth_for(case):
    truth = case.get('truth')
    if not isinstance(truth, dict):
        raise Incomplete('truth unavailable')
    kind = truth.get('kind')
    if kind == 'unknown':
        raise Incomplete('truth unavailable')
    if kind not in ('bug', 'clean') or not proof(truth.get('proof')):
        raise ValueError('truth needs a kind and an HTTPS proof')
    bugs = truth.get('bug_ids')
    if (not isinstance(bugs, list) or any(not isinstance(x, str) or not x.strip() for x in bugs)
            or len(set(bugs)) != len(bugs) or (kind == 'bug') != bool(bugs)):
        raise ValueError('bug cases require unique bug ids; clean cases require none')
    return kind, set(bugs)


def adjudicated(run, case, reviewer_version, bug_ids):
    if (not isinstance(run, dict) or run.get('repo') != case['repo']
            or run.get('head_sha') != case['head_sha']
            or run.get('base_sha') != case['base_sha']
            or run.get('reviewer_version') != reviewer_version or not proof(run.get('run_proof'))):
        raise Incomplete('review missing or stale')
    record = run.get('record')
    if not isinstance(record, dict) or record.get('status') != 'ok':
        raise Incomplete('review did not finish')
    expected = run.get('record_sha256')
    if not isinstance(expected, str) or not DIGEST.fullmatch(expected) or expected != digest(record):
        raise Incomplete('record digest missing or changed')
    if run.get('adjudication_complete') is not True or not proof(run.get('adjudication_proof')):
        raise Incomplete('adjudication incomplete')
    findings = record.get('findings')
    labels = run.get('judgments')
    if not isinstance(findings, list) or not isinstance(labels, list):
        raise Incomplete('findings or judgments unavailable')
    for finding in findings:
        try:
            review_output.validate_finding(finding, allow_repeat=True)
        except (ValueError, TypeError, KeyError) as exc:
            raise Incomplete('invalid structured finding') from exc
    judgments = {}
    for label in labels:
        if not isinstance(label, dict):
            raise ValueError('invalid judgment')
        index = label.get('index')
        if type(index) is not int or index < 0 or index >= len(findings) or index in judgments:
            raise ValueError('judgment indices must be unique and in range')
        verdict, bug_id = label.get('verdict'), label.get('bug_id')
        if verdict not in ('bug', 'false_positive') or not proof(label.get('proof')):
            raise Incomplete('judgment unavailable')
        if (verdict == 'bug' and bug_id not in bug_ids) or (verdict == 'false_positive' and bug_id is not None):
            raise ValueError('judgment does not match confirmed bugs')
        judgments[index] = (verdict, bug_id)
    if len(judgments) != len(findings):
        raise Incomplete('judgment unavailable')
    bug_matches = [bug for verdict, bug in judgments.values() if verdict == 'bug']
    return {'found': len(set(bug_matches)), 'duplicates': len(bug_matches) - len(set(bug_matches)),
            'false_positives': sum(verdict == 'false_positive' for verdict, _ in judgments.values()),
            'findings': len(findings)}


def empty_metrics():
    return {'bugs_total': 0, 'bugs_found': 0, 'false_positive_findings': 0, 'duplicate_findings': 0,
            'findings_total': 0, 'clean_cases': 0, 'clean_cases_with_findings': 0}


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def evaluate(data):
    if not isinstance(data, dict) or not isinstance(data.get('cases'), list):
        raise ValueError('cases must be a list')
    versions = {name: text(data.get(f'{name}_version'), f'{name}_version') for name in ('baseline', 'candidate')}
    results = {'versions': versions, 'paired_cases': 0, 'excluded': [], 'baseline': empty_metrics(), 'candidate': empty_metrics()}
    seen = set()
    for case in data['cases']:
        if not isinstance(case, dict):
            raise ValueError('case must be an object')
        identifier = text(case.get('id'), 'case.id')
        if identifier in seen:
            raise ValueError('case ids must be unique')
        seen.add(identifier)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', text(case.get('repo'), 'case.repo')):
            raise ValueError('case.repo must be owner/name')
        if not all(isinstance(case.get(k), str) and SHA.fullmatch(case[k]) for k in ('base_sha', 'head_sha')):
            raise ValueError('case needs base and head SHAs')
        try:
            kind, bugs = truth_for(case)
            pair = {name: adjudicated(case.get(name), case, versions[name], bugs) for name in ('baseline', 'candidate')}
        except Incomplete as exc:
            results['excluded'].append({'case': identifier, 'reason': str(exc)})
            continue
        results['paired_cases'] += 1
        for name, measurement in pair.items():
            metrics = results[name]
            metrics['bugs_total'] += len(bugs)
            metrics['bugs_found'] += measurement['found']
            metrics['false_positive_findings'] += measurement['false_positives']
            metrics['duplicate_findings'] += measurement['duplicates']
            metrics['findings_total'] += measurement['findings']
            if kind == 'clean':
                metrics['clean_cases'] += 1
                metrics['clean_cases_with_findings'] += int(measurement['findings'] > 0)
    for name in ('baseline', 'candidate'):
        metrics = results[name]
        metrics['bug_recall'] = ratio(metrics['bugs_found'], metrics['bugs_total'])
        metrics['finding_precision'] = ratio(metrics['bugs_found'], metrics['findings_total'])
        metrics['clean_case_alarm_rate'] = ratio(metrics['clean_cases_with_findings'], metrics['clean_cases'])
    return results


def main():
    try:
        result = evaluate(json.load(sys.stdin))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f'invalid evaluation input: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
