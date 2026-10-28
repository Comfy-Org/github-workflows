#!/usr/bin/env python3
"""Evaluate readiness with only a report-only risk result as available input."""

import json
import sys

import readiness


def evaluate_report(data):
    if not isinstance(data, dict):
        raise ValueError("input must be an object")
    snapshot = data.get("snapshot")
    result = readiness.evaluate({
        "snapshot": snapshot,
        "policy": data.get("policy"),
        "checks_complete": False,
        "checks": [],
        "reviews": None,
        "findings": None,
    })
    risk = {"status": "unknown", "tier": None}
    report = data.get("report")
    if isinstance(report, dict):
        summary, record = report.get("summary"), report.get("record")
        if isinstance(summary, dict) and isinstance(record, dict):
            identity = (summary.get("pr"), summary.get("base_sha"), record.get("repo"),
                        record.get("pr"), record.get("head_sha"), record.get("author"))
            expected = (snapshot["pr"], snapshot["base_sha"], snapshot["repo"],
                        snapshot["pr"], snapshot["head_sha"], snapshot["author"])
            if all(isinstance(value, (str, int)) and not isinstance(value, bool) and value for value in identity):
                matches = identity[:-1] == expected[:-1] and str(identity[-1]).casefold() == expected[-1].casefold()
                grade = record.get("risk")
                if not matches:
                    risk["status"] = "stale"
                elif (summary.get("status") == "graded" and isinstance(grade, dict)
                      and grade.get("status") == "ok" and grade.get("tier") in ("R0", "R1", "R2", "R3")
                      and summary.get("tier") == grade.get("tier")):
                    risk = {"status": "observed", "tier": grade["tier"]}
    result["risk"] = risk
    return result


def main():
    try:
        result = evaluate_report(json.load(sys.stdin))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"invalid readiness input: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
