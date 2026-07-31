#!/usr/bin/env python3
"""Publish a risk grade to a pull request — label, Check Run, sticky comment.

Runs in the `publish` job of pr-risk.yml, which holds the bot write token and
checks out NO PR code: it only reads the report artifact produced by the
credential-free `grade` job. That split is the security model — a write-scoped
token is never present in a job that touched PR-authored content.

Three publications, all advisory:

  label       exactly one `risk:R0..R3` at any time; stale tiers are removed,
              so a PR that grows from R0 into R3 carries `risk:R3` and only
              `risk:R3`. An UNGRADABLE PR carries no `risk:*` label at all.
  check run   immutable, timestamped, attached to the head commit, carrying
              the tier and the reason. Conclusion is ALWAYS `neutral` — this
              check can never fail a PR.
  comment     one sticky comment, updated in place on every re-grade, with the
              per-file breakdown, the risk concentration, and a "this grade is
              wrong" checkbox.

The `dispute` subcommand handles the other half of that checkbox: when the
comment body is edited (an `issue_comment: edited` event), it reads the box and
adds or removes the `risk-grade-disputed` label, giving a queryable stream of
reviewer disagreement to tune the grader against.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API_ROOT = os.environ.get("GITHUB_API_URL", "https://api.github.com")

MARKER = "<!-- ci-pr-risk -->"
CHECK_NAME = "PR risk (advisory)"
DISPUTE_LABEL = "risk-grade-disputed"

# Matches exactly the labels this workflow owns, so reconciliation never
# touches a human's `risk-assessment-done` or similar.
RISK_LABEL_RE = re.compile(r"^risk:R[0-9]+$")

# The checkbox line as rendered by grade_risk.render_comment. `[xX]` because
# GitHub's own checkbox UI writes a lowercase x but hand-edits may not.
CHECKED_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+\*\*This grade is wrong\*\*", re.M)
UNCHECKED_RE = re.compile(r"^\s*[-*]\s+\[ \]\s+\*\*This grade is wrong\*\*", re.M)


class ApiError(RuntimeError):
    pass


def api(method: str, path: str, token: str, body: dict | None = None) -> dict | list:
    """Minimal GitHub REST call. Kept tiny and injectable so the tests can
    replace it wholesale rather than mocking a transport."""
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ApiError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network path
        raise ApiError(f"{method} {url} -> {exc}") from exc
    return json.loads(payload) if payload else {}


def reconcile_labels(call, repo: str, pr: int, desired: str | None) -> dict:
    """Make the PR carry `desired` and no other `risk:R*` label.

    `desired=None` (the unknown case) removes every `risk:R*` label and adds
    none — an ungradable PR must not keep a stale tier, and must not silently
    acquire `risk:R0`.
    """
    current = call("GET", f"/repos/{repo}/issues/{pr}/labels", None)
    names = [lbl["name"] for lbl in current]
    ours = [n for n in names if RISK_LABEL_RE.match(n)]
    removed = [n for n in ours if n != desired]
    for name in removed:
        # URL-safe enough: risk labels are `risk:R<n>` by construction, but the
        # colon is encoded so a future tier name can't break the path.
        call("DELETE", f"/repos/{repo}/issues/{pr}/labels/{name.replace(':', '%3A')}", None)
    added = []
    if desired and desired not in names:
        # POST .../labels creates the label in the repo if it does not exist
        # yet, so a consumer repo needs no manual label setup to opt in.
        call("POST", f"/repos/{repo}/issues/{pr}/labels", {"labels": [desired]})
        added.append(desired)
    return {"added": added, "removed": removed}


def publish_check_run(call, repo: str, sha: str, title: str, summary: str) -> dict:
    """Create the immutable, timestamped Check Run carrying tier + reason.

    Conclusion is hardcoded `neutral`. Nothing in this workflow may produce a
    `failure`/`success` conclusion: a neutral check cannot fail a PR even if a
    repo later marks it required, which is the "nothing is gated" guarantee.
    """
    return call(
        "POST",
        f"/repos/{repo}/check-runs",
        {
            "name": CHECK_NAME,
            "head_sha": sha,
            "status": "completed",
            "conclusion": "neutral",
            "output": {"title": title[:255], "summary": summary[:65535]},
        },
    )


def find_sticky(call, repo: str, pr: int) -> dict | None:
    """Return this workflow's existing sticky comment, if any."""
    page = 1
    while page <= 10:  # bounded: 10 pages x 100 = 1000 comments is plenty
        comments = call(
            "GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}", None
        )
        if not comments:
            return None
        for c in comments:
            if MARKER in (c.get("body") or ""):
                return c
        if len(comments) < 100:
            return None
        page += 1
    return None


def upsert_sticky(call, repo: str, pr: int, body: str) -> dict:
    """Create or update the single sticky comment.

    Preserves the reviewer's "this grade is wrong" checkbox across re-grades:
    a push must not silently un-tick a disagreement someone registered. The
    body arrives rendered with an UNCHECKED box, so when the existing comment
    is checked we flip the fresh body to checked before writing it.
    """
    existing = find_sticky(call, repo, pr)
    if existing and CHECKED_RE.search(existing.get("body") or ""):
        body = UNCHECKED_RE.sub(
            lambda m: m.group(0).replace("[ ]", "[x]", 1), body, count=1
        )
    if existing:
        call("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", {"body": body})
        return {"action": "updated", "id": existing["id"]}
    call("POST", f"/repos/{repo}/issues/{pr}/comments", {"body": body})
    return {"action": "created", "id": None}


def set_dispute_label(call, repo: str, pr: int, disputed: bool) -> dict:
    """Add or remove `risk-grade-disputed` to match the checkbox."""
    current = call("GET", f"/repos/{repo}/issues/{pr}/labels", None)
    names = [lbl["name"] for lbl in current]
    if disputed and DISPUTE_LABEL not in names:
        call("POST", f"/repos/{repo}/issues/{pr}/labels", {"labels": [DISPUTE_LABEL]})
        return {"action": "added"}
    if not disputed and DISPUTE_LABEL in names:
        call("DELETE", f"/repos/{repo}/issues/{pr}/labels/{DISPUTE_LABEL}", None)
        return {"action": "removed"}
    return {"action": "unchanged"}


def _summary(line: str) -> None:
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def cmd_publish(args, call) -> int:
    with open(args.report, encoding="utf-8") as fh:
        report = json.load(fh)
    with open(args.check, encoding="utf-8") as fh:
        check_text = fh.read()
    title, _, summary = check_text.partition("\n\n")

    # The Check Run is the audit artifact and publishes in EVERY mode —
    # including shadow, where it is the only output. It is invisible to the
    # PR's label filter and comment thread, so it changes no reviewer-facing
    # behaviour while still recording an immutable, timestamped grade.
    check = publish_check_run(call, args.repo, args.sha, title.strip(), summary.strip())
    _summary(f"Check Run published: {title.strip()} (id {check.get('id', '?')})")

    if args.mode != "publish":
        _summary(
            f"mode=`{args.mode}` (shadow) — Check Run only. No label and no "
            "comment were published. Set `mode: publish` in the caller to put "
            "the tier on the PR."
        )
        return 0

    desired = report.get("label") if report.get("status") == "graded" else None
    lab = reconcile_labels(call, args.repo, args.pr, desired)
    if desired:
        _summary(f"Label: `{desired}` (removed: {lab['removed'] or 'none'})")
    else:
        _summary(
            "Label: none — this PR could not be graded, so it is published as "
            f"UNKNOWN rather than defaulted to `risk:R0` (removed: {lab['removed'] or 'none'})"
        )

    if args.comment:
        with open(args.comment, encoding="utf-8") as fh:
            body = fh.read()
        res = upsert_sticky(call, args.repo, args.pr, body)
        _summary(f"Sticky comment {res['action']}.")
    return 0


def cmd_dispute(args, call) -> int:
    with open(args.body, encoding="utf-8") as fh:
        body = fh.read()
    if MARKER not in body:
        _summary("Edited comment is not the risk sticky comment — nothing to record.")
        return 0
    disputed = bool(CHECKED_RE.search(body))
    res = set_dispute_label(call, args.repo, args.pr, disputed)
    _summary(
        f"Grade dispute checkbox is {'CHECKED' if disputed else 'unchecked'} — "
        f"`{DISPUTE_LABEL}` {res['action']}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("publish", help="Publish label + Check Run + sticky comment.")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--sha", required=True, help="PR head SHA the Check Run attaches to")
    p.add_argument("--report", required=True)
    p.add_argument("--check", required=True)
    p.add_argument("--comment", default="")
    p.add_argument(
        "--mode",
        default="shadow",
        help="`shadow` publishes the Check Run only; `publish` also applies the "
        "label and the sticky comment. Neither ever gates.",
    )
    p.set_defaults(func=cmd_publish)

    d = sub.add_parser("dispute", help="Record a 'this grade is wrong' toggle.")
    d.add_argument("--repo", required=True)
    d.add_argument("--pr", type=int, required=True)
    d.add_argument("--body", required=True, help="File holding the edited comment body")
    d.set_defaults(func=cmd_dispute)

    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("publish_risk: GITHUB_TOKEN is empty — nothing published.", file=sys.stderr)
        return 1

    def call(method, path, body):
        return api(method, path, token, body)

    return args.func(args, call)


if __name__ == "__main__":
    raise SystemExit(main())
