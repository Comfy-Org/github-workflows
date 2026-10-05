#!/usr/bin/env python3
"""Opt-in auto-approve: turn a cursor-review round into an approval decision.

Used by cursor-review.yml when the caller sets `approve_max_severity`. Two
subcommands, each run from a job that checks out NO PR code:

``decide`` (in `post-review`, after the consolidated review is posted)
    APPROVE when every finding of this round is at or below the threshold and
    nothing below says "don't trust this round"; REQUEST_CHANGES when any finding
    is above it; otherwise submit nothing. The review is pinned (`commit_id`) to
    the commit the panel reviewed.

``dismiss-stale`` (in `dismiss-stale-approval`, on `synchronize`)
    Dismiss this identity's own auto-approve reviews that are not on the new head.
    The caller's ruleset may keep an approval valid across pushes
    (`dismiss_stale_reviews_on_push: false`), so the bot has to withdraw its own.
    CHANGES_REQUESTED is dismissed too: the next round replaces it, and a stale
    one would otherwise veto the PR after the author fixed it and a human approved.

Fail-closed rules for ``decide`` (any one → no approval):

* the threshold is not one of ``medium``, ``low``, ``nit`` (→ exit 2, red);
* the judge did not adjudicate (degraded panel-union fallback);
* any panel cell is not ``ok`` (a short panel finding nothing proves nothing);
* the review did not land as resolvable threads (`delivered` is not ``true``);
* the PR head moved while the panel ran;
* a finding's severity is missing or unrecognised (post-review.py renders those
  as ``medium``; for approval they count as above every threshold);
* an earlier round's critical/high thread is still open (not resolved, not
  outdated) — so a High argued away in a reply cannot be approved over.

Only reviews carrying ``APPROVE_MARKER`` and authored by the approver login are
ever dismissed, so a human's review — or another bot's — is never touched.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys

SEVERITY_ORDER = ["critical", "high", "medium", "low", "nit"]
ALLOWED_THRESHOLDS = ("medium", "low", "nit")
# Prior-round threads at these severities block approval while still open.
BLOCKING_THREAD_SEVERITIES = ("critical", "high")
APPROVE_MARKER = "<!-- cursor-review-auto-approve -->"
# post-review.py prefixes every inline comment with `<emoji> **<Label>** — `.
BADGE_RE = re.compile(r"^\S+\s+\*\*(Critical|High|Medium|Low|Nit)\*\*\s+—")

APPROVE = "APPROVE"
REQUEST_CHANGES = "REQUEST_CHANGES"
NONE = "NONE"


def _load_gate_unresolved():
    """Import gate-unresolved.py by path (its name has a hyphen) for its thread query."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate-unresolved.py")
    spec = importlib.util.spec_from_file_location("gate_unresolved", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_threshold(value: str) -> str:
    threshold = (value or "").strip().lower()
    if threshold not in ALLOWED_THRESHOLDS:
        raise ValueError(
            f"approve_max_severity must be one of {', '.join(ALLOWED_THRESHOLDS)} "
            f"(or empty to disable), got {value!r}"
        )
    return threshold


def above_threshold(severity, threshold: str) -> bool:
    """True when `severity` is worse than `threshold`; unrecognised counts as worse."""
    sev = severity.strip().lower() if isinstance(severity, str) else ""
    if sev not in SEVERITY_ORDER:
        return True
    return SEVERITY_ORDER.index(sev) < SEVERITY_ORDER.index(threshold)


def thread_severity(body: str):
    """Severity of a cursor-review inline comment, or None when it has no badge."""
    match = BADGE_RE.match(body or "")
    return match.group(1).lower() if match else None


def decide(
    threshold: str,
    findings: list,
    panel: list,
    judge_status: str,
    delivered: bool,
    reviewed_sha: str,
    live_head_sha: str,
    open_thread_severities: list,
):
    """Return (event, reasons, blocking_findings). Pure; no I/O."""
    if judge_status != "ok":
        # Un-adjudicated panel-union output: neither approve nor veto on it.
        return NONE, [f"the judge did not adjudicate this round (status={judge_status or 'missing'})"], []
    blocking = [
        f for f in findings if not isinstance(f, dict) or above_threshold(f.get("severity"), threshold)
    ]
    if blocking:
        return REQUEST_CHANGES, [f"{len(blocking)} finding(s) above `{threshold}`"], blocking

    reasons = []
    bad_cells = [c for c in panel if not isinstance(c, dict) or c.get("status") != "ok"]
    if not panel:
        reasons.append("no panel metadata")
    elif bad_cells:
        reasons.append(f"{len(bad_cells)}/{len(panel)} panel reviewers did not complete")
    if not delivered:
        reasons.append("the review did not land on the PR as resolvable threads")
    if not reviewed_sha or reviewed_sha != live_head_sha:
        reasons.append("the PR head moved while the review ran")
    open_blocking = [
        s for s in open_thread_severities if s is None or s in BLOCKING_THREAD_SEVERITIES
    ]
    if open_blocking:
        reasons.append(f"{len(open_blocking)} open critical/high (or unbadged) thread(s) from an earlier round")
    if reasons:
        return NONE, reasons, []
    return APPROVE, [f"every finding is at or below `{threshold}`"], []


def stale_reviews_to_dismiss(reviews: list, approver_login: str, head_sha: str) -> list:
    """Ids of this identity's auto-approve reviews that are live and not on head_sha."""
    ids = []
    for r in reviews:
        user = (r.get("user") or {}).get("login", "")
        if user.lower() != approver_login.lower():
            continue
        if r.get("state") not in ("APPROVED", "CHANGES_REQUESTED"):
            continue
        if APPROVE_MARKER not in (r.get("body") or ""):
            continue
        if r.get("commit_id") == head_sha:
            continue
        ids.append(r["id"])
    return ids


def gh(args: list, payload=None) -> str:
    result = subprocess.run(
        ["gh", *args],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout


def emit(text: str) -> None:
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def open_thread_severities(repo: str, pr: int) -> list:
    """Severities of every open (unresolved, non-outdated) cursor-review thread.

    This round's own threads are included on purpose: decide() only reaches the
    thread check when every finding of this round is at or below the threshold
    (never critical/high, never unbadged), so they cannot block — and not having to
    tell this round's threads from earlier ones keeps the check identity-free.
    """
    gate = _load_gate_unresolved()
    owner, _, name = repo.partition("/")
    out = []
    for thread in gate.iter_threads(owner, name, pr):
        if not gate.is_cursor_thread(thread):
            continue
        if thread.get("isResolved") or thread.get("isOutdated"):
            continue
        first = ((thread.get("comments") or {}).get("nodes") or [{}])[0]
        out.append(thread_severity(first.get("body") or ""))
    return out


def render_body(event: str, reasons: list, threshold: str, blocking: list) -> str:
    lines = [APPROVE_MARKER, "### 🤖 Cursor Review — auto-approve"]
    if event == APPROVE:
        lines.append(f"✅ Approved: {reasons[0]}. A new push dismisses this approval.")
    else:
        lines.append(f"❌ Changes requested: {reasons[0]}. Fix them, push, and re-run the review.")
        for f in blocking[:20]:
            if isinstance(f, dict):
                sev = f.get("severity") if isinstance(f.get("severity"), str) else "unknown"
                lines.append(f"- **{sev}** — `{f.get('file', '?')}:{f.get('line', '?')}`")
    lines.append(f"\n_Threshold: `{threshold}` (set by this repo's `approve_max_severity`)._")
    return "\n".join(lines)


def cmd_decide(args) -> int:
    try:
        threshold = validate_threshold(args.threshold)
    except ValueError as e:
        print(f"::error::{e}")
        return 2
    with open(args.findings, encoding="utf-8") as f:
        data = json.load(f)
    findings = data.get("findings") or []
    panel = data.get("panel") or []

    pr = json.loads(gh(["api", f"repos/{args.repo}/pulls/{args.pr_number}"]))
    live_head = (pr.get("head") or {}).get("sha", "")
    prior = open_thread_severities(args.repo, int(args.pr_number))

    event, reasons, blocking = decide(
        threshold,
        findings,
        panel,
        args.judge_status,
        args.delivered == "true",
        args.commit_sha,
        live_head,
        prior,
    )
    if event == NONE:
        emit(f"ℹ️ **Auto-approve: no decision** — {'; '.join(reasons)}.")
        return 0

    body = render_body(event, reasons, threshold, blocking)
    try:
        gh(
            ["api", "-X", "POST", f"repos/{args.repo}/pulls/{args.pr_number}/reviews", "--input", "-"],
            {"commit_id": args.commit_sha, "event": event, "body": body},
        )
    except RuntimeError as e:
        # GitHub refuses an approval of your own PR (422). That is a property of
        # who authored the PR, not a broken review — report it, don't go red.
        if "own pull request" in str(e).lower():
            emit(f"ℹ️ **Auto-approve: skipped** — the approver authored this PR ({e}).")
            return 0
        print(f"::error::Could not submit the {event} review: {e}")
        return 1
    emit(f"{'✅' if event == APPROVE else '❌'} **Auto-approve: {event}** — {reasons[0]}.")
    return 0


def cmd_dismiss_stale(args) -> int:
    reviews = json.loads(
        gh(["api", "--paginate", "--slurp", f"repos/{args.repo}/pulls/{args.pr_number}/reviews?per_page=100"])
    )
    flat = [r for page in reviews for r in page] if reviews and isinstance(reviews[0], list) else reviews
    ids = stale_reviews_to_dismiss(flat, args.approver_login, args.head_sha)
    for rid in ids:
        gh(
            ["api", "-X", "PUT", f"repos/{args.repo}/pulls/{args.pr_number}/reviews/{rid}/dismissals", "--input", "-"],
            {"message": "New commits pushed — cursor-review auto-approve withdrawn until the next review round.", "event": "DISMISS"},
        )
    emit(f"Auto-approve: dismissed {len(ids)} stale review(s) by {args.approver_login}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--threshold", required=True)
    d.add_argument("--findings", required=True)
    d.add_argument("--repo", required=True)
    d.add_argument("--pr-number", required=True)
    d.add_argument("--commit-sha", required=True)
    d.add_argument("--judge-status", default="")
    d.add_argument("--delivered", default="")
    s = sub.add_parser("dismiss-stale")
    s.add_argument("--repo", required=True)
    s.add_argument("--pr-number", required=True)
    s.add_argument("--head-sha", required=True)
    s.add_argument("--approver-login", required=True)
    args = parser.parse_args()
    return cmd_decide(args) if args.cmd == "decide" else cmd_dismiss_stale(args)


if __name__ == "__main__":
    sys.exit(main())
