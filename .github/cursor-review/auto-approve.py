#!/usr/bin/env python3
"""Opt-in auto-approve: turn a cursor-review round into an approval decision.

Used by cursor-review.yml when the caller sets `approve_max_severity`. Two
subcommands, each run from a job that checks out NO PR code:

``decide`` (in `post-review`, after the consolidated review is posted)
    APPROVE when every finding of this round is at or below the threshold and
    nothing below says "don't trust this round"; REQUEST_CHANGES when any finding
    is above it; otherwise submit nothing. The review is pinned (`commit_id`) to
    the commit the panel reviewed.

``dismiss-stale`` (in `dismiss-stale-approval`, on every same-repo PR event)
    Dismiss this identity's own auto-approve APPROVALS that are not on the PR's
    live head. The caller's ruleset may keep an approval valid across pushes
    (`dismiss_stale_reviews_on_push: false`), so the bot has to withdraw its own.
    Running on every event, not only `synchronize`, means a cancelled run or a
    caller that does not deliver `synchronize` is caught by the next event.
    CHANGES_REQUESTED is left alone: only a new decision supersedes it, so a
    trivial push cannot clear the bot's veto.

Fail-closed rules for ``decide``:

* the threshold is not one of ``medium``, ``low``, ``nit`` → exit 2, red.

An untrusted round submits NO review event — neither approve nor request changes
— and withdraws this identity's own earlier approvals (its findings are still on
the PR):

* the judge did not adjudicate (degraded panel-union fallback);
* any panel cell is not ``ok`` (a short panel finding nothing proves nothing);
* the review did not land on the PR (`delivered` is not ``true``);
* the PR head moved while the panel ran. A push racing the POST itself is
  caught by re-reading the head after the write and withdrawing the review.

On a trusted round:

* any finding above the threshold, or with a missing / unrecognised severity
  (post-review.py renders those as ``medium``), → REQUEST_CHANGES;
* else any open (unresolved, non-outdated) cursor-review thread above the
  threshold, or unbadged → no review, so a finding argued away in a reply cannot
  be approved over;
* else → APPROVE.

Only reviews carrying ``APPROVE_MARKER`` and authored by the approver login are
ever dismissed, so a human's review — or another bot's — is never touched.

Every API failure on the way to a decision degrades to "no review", never to a
traceback: an approver that cannot read the PR must not approve it.
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
APPROVE_MARKER = "<!-- cursor-review-auto-approve -->"
# post-review.py prefixes every inline comment with `<emoji> **<Label>** — `.
BADGE_RE = re.compile(r"^\S+\s+\*\*(Critical|High|Medium|Low|Nit)\*\*\s+—")

APPROVE = "APPROVE"
REQUEST_CHANGES = "REQUEST_CHANGES"
NONE = "NONE"


def _load(filename: str, name: str):
    """Import a sibling script by path (the names have hyphens)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The same markdown hardening post-review.py applies to model output, so the
# review body posted under the approver identity can't carry a live @mention,
# break out of a code span, or forge sections.
_post_review = _load("post-review.py", "post_review")


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
    """Return (event, reasons, blocking_findings). Pure; no I/O.

    Trust checks come first and gate BOTH review events: a round that cannot be
    trusted neither approves nor requests changes. Its findings are still on the
    PR as threads; only the review event is withheld.
    """
    reasons = []
    if judge_status != "ok":
        reasons.append(f"the judge did not adjudicate this round (status={judge_status or 'missing'})")
    bad_cells = [c for c in panel if not isinstance(c, dict) or c.get("status") != "ok"]
    if not panel:
        reasons.append("no panel metadata")
    elif bad_cells:
        reasons.append(f"{len(bad_cells)}/{len(panel)} panel reviewers did not complete")
    if not delivered:
        reasons.append("the review did not land on the PR")
    if not reviewed_sha or reviewed_sha != live_head_sha:
        reasons.append("the PR head moved while the review ran")
    if reasons:
        return NONE, reasons, []

    blocking = [
        f for f in findings if not isinstance(f, dict) or above_threshold(f.get("severity"), threshold)
    ]
    if blocking:
        return REQUEST_CHANGES, [f"{len(blocking)} finding(s) above `{threshold}`"], blocking

    open_blocking = [s for s in open_thread_severities if s is None or above_threshold(s, threshold)]
    if open_blocking:
        return NONE, [f"{len(open_blocking)} open thread(s) above `{threshold}` (or unbadged) from an earlier round"], []
    return APPROVE, [f"every finding is at or below `{threshold}`"], []


def stale_reviews_to_dismiss(reviews: list, approver_login: str, head_sha) -> list:
    """Ids of this identity's live auto-approve APPROVALS not on head_sha.

    head_sha None → every such approval (an untrusted round withdraws them all).
    """
    ids = []
    for r in reviews:
        user = (r.get("user") or {}).get("login", "")
        if user.lower() != approver_login.lower():
            continue
        if r.get("state") != "APPROVED":
            continue
        if APPROVE_MARKER not in (r.get("body") or ""):
            continue
        if head_sha is not None and r.get("commit_id") == head_sha:
            continue
        ids.append(r["id"])
    return ids


def gh(args: list, payload=None) -> str:
    """Run gh; EVERY failure — non-zero exit, timeout, missing binary — is a RuntimeError."""
    try:
        result = subprocess.run(
            ["gh", *args],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {e}") from e
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
    thread check when every finding of this round is at or below the threshold,
    so they cannot block — and not having to tell this round's threads from
    earlier ones keeps the check identity-free.
    """
    gate = _load("gate-unresolved.py", "gate_unresolved")
    owner, _, name = repo.partition("/")
    out = []
    try:
        threads = list(gate.iter_threads(owner, name, pr))
    except SystemExit as e:  # run_graphql exits 2 on a failed query
        raise RuntimeError(f"review-thread query failed (exit {e.code})") from e
    for thread in threads:
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
                raw = f.get("severity")
                sev = raw.strip().lower() if isinstance(raw, str) else ""
                sev = sev if sev in SEVERITY_ORDER else "unrecognised severity"
                line = f.get("line") if isinstance(f.get("line"), int) else "?"
                ref = _post_review.render_code_ref(str(f.get("file", "?"))[:300], line)
                lines.append(f"- **{sev}** — {ref}")
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

    try:
        live_head = live_head_sha(args.repo, args.pr_number)
        prior = open_thread_severities(args.repo, int(args.pr_number))
    except (RuntimeError, ValueError) as e:
        emit(f"ℹ️ **Auto-approve: no decision** — could not read the PR state ({e}).")
        return withdraw_approvals(args, None)

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
        # A round that can't be trusted must not leave an earlier approval by
        # this identity counting — GitHub keeps a reviewer's latest state.
        return withdraw_approvals(args, None)

    body = render_body(event, reasons, threshold, blocking)
    try:
        posted = json.loads(
            gh(
                ["api", "-X", "POST", f"repos/{args.repo}/pulls/{args.pr_number}/reviews", "--input", "-"],
                {"commit_id": args.commit_sha, "event": event, "body": body},
            )
        )
    except RuntimeError as e:
        # GitHub refuses an approval of your own PR (422). That is a property of
        # who authored the PR, not a broken review — report it, don't go red.
        if "own pull request" in str(e).lower():
            emit(f"ℹ️ **Auto-approve: skipped** — the approver authored this PR ({e}).")
            return 0
        print(f"::error::Could not submit the {event} review: {e}")
        return 1

    # Close the head-read → POST race. A push landing in that window fires a
    # `synchronize` whose dismissal scan can finish before this review exists, so
    # nothing else would ever withdraw it. Re-read the head now that the review
    # is written; if it moved, withdraw our own review here.
    try:
        head_now = live_head_sha(args.repo, args.pr_number)
    except RuntimeError:
        head_now = ""  # can't confirm the head → treat as moved, withdraw
    if head_now != args.commit_sha:
        try:
            dismiss(args.repo, args.pr_number, posted["id"])
        except RuntimeError as e:
            print(f"::error::The PR head moved while the {event} review was posted, and withdrawing it failed: {e}. {DISMISS_PERMISSION_HINT}")
            return 1
        emit(f"ℹ️ **Auto-approve: withdrawn** — the PR head moved while the {event} review was being posted.")
        return 0
    emit(f"{'✅' if event == APPROVE else '❌'} **Auto-approve: {event}** — {reasons[0]}.")
    return 0


def live_head_sha(repo: str, pr_number) -> str:
    sha = (json.loads(gh(["api", f"repos/{repo}/pulls/{pr_number}"])).get("head") or {}).get("sha", "")
    if not sha:
        raise RuntimeError("the PR has no head sha")
    return sha


def list_reviews(repo: str, pr_number) -> list:
    pages = json.loads(gh(["api", "--paginate", "--slurp", f"repos/{repo}/pulls/{pr_number}/reviews?per_page=100"]))
    return [r for page in pages for r in page] if pages and isinstance(pages[0], list) else pages


def withdraw_approvals(args, head_sha) -> int:
    """Dismiss this identity's marked approvals not on head_sha (None → all)."""
    try:
        ids = stale_reviews_to_dismiss(list_reviews(args.repo, args.pr_number), args.approver_login, head_sha)
    except RuntimeError as e:
        print(f"::error::Could not list reviews to withdraw stale auto-approvals: {e}")
        return 1
    failed = []
    for rid in ids:
        try:
            dismiss(args.repo, args.pr_number, rid)
        except RuntimeError as e:
            failed.append(f"{rid}: {e}")
    if ids:
        emit(f"Auto-approve: withdrew {len(ids) - len(failed)}/{len(ids)} approval(s) by {args.approver_login}.")
    if failed:
        # Red, not a warning: an approval that stays valid is the exact failure
        # this exists to prevent.
        print(f"::error::Could not dismiss {len(failed)} auto-approval(s) ({'; '.join(failed)}). {DISMISS_PERMISSION_HINT}")
        return 1
    return 0


DISMISS_PERMISSION_HINT = (
    "Dismissing a review needs an identity allowed to dismiss reviews on this branch: "
    "pull-requests: write is not enough when branch protection restricts dismissals "
    "(add the approver to the allowed dismissers, or lift the restriction)."
)


def dismiss(repo: str, pr_number, review_id) -> None:
    gh(
        ["api", "-X", "PUT", f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/dismissals", "--input", "-"],
        {"message": "cursor-review auto-approve withdrawn: it no longer applies to this PR's head.", "event": "DISMISS"},
    )


def cmd_dismiss_stale(args) -> int:
    # The LIVE head, not the event's: an older event's run landing after a newer
    # push would otherwise dismiss the approval of the newer head.
    try:
        head = live_head_sha(args.repo, args.pr_number)
    except RuntimeError as e:
        print(f"::error::Could not read the PR head: {e}")
        return 1
    return withdraw_approvals(args, head)


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
    d.add_argument("--approver-login", required=True)
    s = sub.add_parser("dismiss-stale")
    s.add_argument("--repo", required=True)
    s.add_argument("--pr-number", required=True)
    s.add_argument("--approver-login", required=True)
    args = parser.parse_args()
    return cmd_decide(args) if args.cmd == "decide" else cmd_dismiss_stale(args)


if __name__ == "__main__":
    sys.exit(main())
