#!/usr/bin/env python3
"""Evaluate caller-supplied evidence without reading or writing external systems."""

import json
import re
import sys
from urllib.parse import urlsplit

SHA = re.compile(r"^[0-9a-f]{40}$")
SCOPE_KEYS = ("repo", "pr", "base_sha", "head_sha", "policy_version")
CHECK_RESULTS = {"success", "failure", "error", "pending", "cancelled", "unavailable", "skipped", "neutral"}


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _sha(value, name):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase 40-character SHA")
    return value


def _scope(value):
    value = _object(value, "scope")
    repo = _text(value.get("repo"), "scope.repo")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("scope.repo must be owner/name")
    pr = value.get("pr")
    if type(pr) is not int or pr < 1:
        raise ValueError("scope.pr must be a positive integer")
    return (repo, pr, _sha(value.get("base_sha"), "base_sha"),
            _sha(value.get("head_sha"), "head_sha"),
            _text(value.get("policy_version"), "policy_version"))


def _proof(value):
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return url.scheme == "https" and bool(url.hostname) and not url.username and not url.password
    except ValueError:
        return False


def _list(value, name):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _complete(value, name):
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def evaluate(data):
    data = _object(data, "input")
    snapshot = _object(data.get("snapshot"), "snapshot")
    scope = _scope(snapshot)
    tested_sha = _sha(snapshot.get("tested_sha"), "tested_sha")
    author = _text(snapshot.get("author"), "snapshot.author").casefold()
    policy = _object(data.get("policy"), "policy")
    if _text(policy.get("version"), "policy.version") != scope[4]:
        raise ValueError("policy version does not match snapshot")
    required = {}
    for check in _list(policy.get("required_checks"), "required_checks"):
        check = _object(check, "required check")
        name = _text(check.get("name"), "required check.name")
        revision = check.get("revision")
        if name in required or revision not in ("head", "tested"):
            raise ValueError("required checks need unique names and a head or tested revision")
        required[name] = scope[3] if revision == "head" else tested_sha
    if not required:
        raise ValueError("required_checks must not be empty")
    count = policy.get("required_human_approvals")
    if type(count) is not int or count < 1:
        raise ValueError("required_human_approvals must be a positive integer")

    blocked, waiting = [], []
    proofs = set()
    results = {name: [] for name in required}
    if not _complete(data.get("checks_complete", False), "checks_complete"):
        waiting.append("check inventory incomplete")
    for item in _list(data.get("checks", []), "checks"):
        item = _object(item, "check")
        if _scope(item.get("scope")) != scope:
            continue
        name = _text(item.get("name"), "check.name")
        result = item.get("result")
        if result not in CHECK_RESULTS:
            raise ValueError("invalid check result")
        item_tested = _sha(item.get("tested_sha"), "check.tested_sha")
        if name in results and item_tested == required[name]:
            proof = item.get("proof")
            if _proof(proof):
                proofs.add(proof)
            results[name].append((result, _proof(proof)))
    # A reported failure can block, but does not prove which revision ran and
    # cannot satisfy a required successful check.
    for item in _list(data.get("reported_failures", []), "reported_failures"):
        item = _object(item, "reported failure")
        if _scope(item.get("scope")) != scope:
            continue
        name = _text(item.get("name"), "reported failure.name")
        if name in required and _proof(item.get("proof")):
            blocked.append(f"required check reported failure: {name}")
            proofs.add(item["proof"])

    for name, found in results.items():
        if any(result in ("failure", "error") for result, _ in found):
            blocked.append(f"required check failed: {name}")
        elif not found or any(result != "success" or not proof for result, proof in found):
            waiting.append(f"required check incomplete: {name}")

    reviews = data.get("reviews")
    if reviews is None:
        waiting.append("review evidence missing")
    else:
        reviews = _object(reviews, "reviews")
        if _scope(reviews.get("scope")) != scope:
            waiting.append("required human approvals incomplete")
        else:
            complete = _complete(reviews.get("complete"), "reviews.complete")
            if _proof(reviews.get("proof")):
                proofs.add(reviews["proof"])
            actors = {}
            for approval in _list(reviews.get("approvals"), "approvals"):
                approval = _object(approval, "approval")
                actor = _text(approval.get("actor"), "approval.actor").casefold()
                human = _complete(approval.get("human"), "approval.human")
                state = approval.get("state")
                if state not in ("approved", "dismissed", "changes_requested"):
                    raise ValueError("invalid review state")
                if human and actor != author:
                    actors.setdefault(actor, set()).add(state)
            if any("changes_requested" in states for states in actors.values()):
                blocked.append("human review requests changes")
            approved = sum(states == {"approved"} for states in actors.values())
            if not complete or not _proof(reviews.get("proof")) or approved < count:
                waiting.append("required human approvals incomplete")

    findings = data.get("findings")
    if findings is None:
        waiting.append("finding evidence missing")
    else:
        findings = _object(findings, "findings")
        if _scope(findings.get("scope")) != scope:
            waiting.append("finding evidence incomplete")
        else:
            complete = _complete(findings.get("complete"), "findings.complete")
            if _proof(findings.get("proof")):
                proofs.add(findings["proof"])
            items = _list(findings.get("items"), "findings.items")
            for item in items:
                if _object(item, "finding").get("state") not in ("open", "resolved"):
                    raise ValueError("invalid finding state")
            if any(item["state"] == "open" for item in items):
                blocked.append("unresolved finding")
            if not complete or not _proof(findings.get("proof")):
                waiting.append("finding evidence incomplete")

    state = "BLOCKED" if blocked else "WAITING" if waiting else "READY"
    return {"state": state, "scope": dict(zip(SCOPE_KEYS, scope)),
            "tested_sha": tested_sha, "reasons": blocked + waiting, "proofs": sorted(proofs)}


def main():
    try:
        result = evaluate(json.load(sys.stdin))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"invalid readiness input: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
