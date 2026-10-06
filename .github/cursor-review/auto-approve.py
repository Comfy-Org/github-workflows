#!/usr/bin/env python3
"""Opt-in auto-approve: turn a cursor-review round into an approval decision.

Used by cursor-review.yml when the caller sets `approve_max_severity`. Two
subcommands, each run from a job that checks out NO PR code:

``decide`` (in `post-review`, after the consolidated review is posted)
    APPROVE when every finding of this round is at or below the threshold and
    nothing below says "don't trust this round"; REQUEST_CHANGES when any finding
    is above it; otherwise submit nothing. The review is pinned (`commit_id`) to
    the commit the panel reviewed.

``dismiss-stale`` (in `dismiss-stale-approval`, on every same-repo
`pull_request` event of an open PR; ``--all-approvals`` on a base retarget)
    Dismiss this identity's own auto-approve APPROVALS that are stale against
    the PR's LIVE state: not on its current head, or recorded (``BASE_MARKER``)
    against a base other than its current one — and, on a retarget, every one
    of them, for approvals posted before the base was recorded. Staleness is
    read from the PR, not from the event payload, so whichever event runs next
    redoes a dismissal a cancelled run left undone. A stale marked approval by
    ANY OTHER login — one this run cannot act as (a rotated identity, or a run
    without the approver's secrets) — turns the job red rather than passing
    unchecked. The caller's ruleset may keep an
    approval valid across pushes (`dismiss_stale_reviews_on_push: false`), so the
    bot has to withdraw its own. The job runs whether or not
    `approve_max_severity` is still set: unsetting it is the kill switch for NEW
    approvals, and must not also stop old ones from being withdrawn.
    CHANGES_REQUESTED is deliberately NOT dismissed on a push: under the
    label-triggered caller a push starts no new round, so dismissing it would let
    any trivial push clear the bot's veto. Only a later round's own review event
    supersedes it (GitHub counts a reviewer's most recent review).

Fail-closed rules for ``decide``:

* the threshold is not one of ``medium``, ``low``, ``nit`` → exit 2, red.

An untrusted round submits NO review event — neither approve nor request changes
(its findings are still on the PR as threads):

* the judge did not adjudicate (degraded panel-union fallback);
* any panel cell is not ``ok`` (a short panel finding nothing proves nothing);
* the review did not land as resolvable threads (`delivered` is not ``true``,
  or any finding reached the review body only — ``ungated_findings`` > 0 — where
  the open-thread check below cannot see it);
* the PR state or its threads could not be read;
* the PR head moved, or its base branch was retargeted, while the panel ran.
  A push or retarget racing the POST itself is caught by re-reading both after
  the write and withdrawing the review;
* the reviewed diff has no content hunk — every changed path was stripped by
  ``diff_excludes`` or the generated-file classifier (or the change is a pure
  rename / mode / binary change). Zero findings over nothing is not a clean
  review, and those paths can still reach production.

On a trusted round:

* any finding above the threshold, or with a missing / unrecognised severity
  (post-review.py renders those as ``medium``), → REQUEST_CHANGES;
* else an earlier round's thread above the threshold (or unbadged) still open
  — not resolved, not outdated — → no review, so a High argued away in a reply
  cannot be approved over;
* else → APPROVE.

An untrusted round (or an open blocking thread) also WITHDRAWS this identity's
own earlier marked approvals, wherever they are pinned: GitHub counts a
reviewer's most recent review, so a round-1 APPROVED would otherwise keep
counting through a degraded re-run at the same head.

Trust model: every signal here — findings, panel status, judge status — is model
output over the PR's own content, so a diff that prompt-injects the panel and
judge can steer the round to an approval. Treat the approval as an automated
review signal, not as a substitute for a human reviewer, and read the caller
guide's trust-model section before letting it satisfy a ruleset.

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
APPROVE_MARKER = "<!-- cursor-review-auto-approve -->"
# The base branch a review was posted against, hex-encoded so no ref name can
# close the HTML comment early. dismiss-stale withdraws an approval whose
# recorded base is no longer the PR's base.
BASE_MARKER_RE = re.compile(r"<!-- cursor-review-auto-approve-base:([0-9a-f]*) -->")
# post-review.py prefixes every inline comment with `<emoji> **<Label>** — `.
BADGE_RE = re.compile(r"^\S+\s+\*\*(Critical|High|Medium|Low|Nit)\*\*\s+—")

APPROVE = "APPROVE"
REQUEST_CHANGES = "REQUEST_CHANGES"
NONE = "NONE"


def _load_sibling(filename: str, module_name: str):
    """Import a sibling script by path (its name has a hyphen)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_gate_unresolved():
    """gate-unresolved.py, for its thread query."""
    return _load_sibling("gate-unresolved.py", "gate_unresolved")


def _load_post_review():
    """post-review.py, for the markdown sanitisers its own review body uses."""
    return _load_sibling("post-review.py", "post_review")


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
    ungated: int = 0,
    reviewed_diff_empty: bool = False,
    reviewed_base: str = "",
    live_base: str = "",
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
        reasons.append("the review did not land on the PR as resolvable threads")
    elif ungated:
        reasons.append(f"{ungated} finding(s) reached the review body only, not as resolvable threads")
    if not reviewed_sha or reviewed_sha != live_head_sha:
        reasons.append("the PR head moved while the review ran")
    if reviewed_base != live_base:
        # A retarget never moves the head, so the head check cannot see it.
        reasons.append("the PR base branch changed while the review ran")
    if reviewed_diff_empty:
        reasons.append("the reviewed diff is empty — every changed path was excluded from review, so nothing a reviewer saw can earn an approval")
    if reasons:
        return NONE, reasons, []

    blocking = [
        f for f in findings if not isinstance(f, dict) or above_threshold(f.get("severity"), threshold)
    ]
    if blocking:
        return REQUEST_CHANGES, [f"{len(blocking)} finding(s) above `{threshold}`"], blocking

    # Same line as this round's findings, so a Medium thread from an earlier round
    # blocks a `low` threshold exactly as a Medium finding this round would.
    open_blocking = [
        s for s in open_thread_severities if s is None or above_threshold(s, threshold)
    ]
    if open_blocking:
        return NONE, [f"{len(open_blocking)} open thread(s) above `{threshold}` (or unbadged) from an earlier round"], []
    return APPROVE, [f"every finding is at or below `{threshold}`"], []


def reviewed_diff_is_empty(path: str) -> bool:
    """True when the reviewed patch carries no content hunk (or cannot be read).

    A file section with no `@@` hunk — a pure rename, a mode change, a binary
    file — shows a reviewer no content either, so it does not count. An
    unreadable patch is no evidence that anything was reviewed: fail closed.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return not any(line.startswith("@@") for line in f)
    except OSError:
        return True


def recorded_base(body: str):
    """The base ref a marked review recorded, or None (pre-dates the record)."""
    match = BASE_MARKER_RE.search(body or "")
    if not match:
        return None
    try:
        return bytes.fromhex(match.group(1)).decode("utf-8")
    except ValueError:
        return ""  # garbled → matches no real base → stale


def _stale_approvals(reviews: list, head_sha, live_base):
    """(login, id) of every live marked APPROVAL that is stale.

    Stale = not on `head_sha` (`None` selects every one, wherever pinned), or
    recorded against a base other than `live_base` (`None` skips that check).
    """
    out = []
    for r in reviews:
        if r.get("state") != "APPROVED":
            continue
        body = r.get("body") or ""
        if APPROVE_MARKER not in body:
            continue
        base = recorded_base(body)
        off_head = head_sha is None or r.get("commit_id") != head_sha
        off_base = live_base is not None and base is not None and base != live_base
        if off_head or off_base:
            login = (r.get("user") or {}).get("login")
            out.append((login if isinstance(login, str) else "", r["id"]))
    return out


def stale_reviews_to_dismiss(reviews: list, approver_login: str, head_sha, live_base=None) -> list:
    """Ids of this identity's stale auto-approve APPROVALS (see _stale_approvals)."""
    return [rid for login, rid in _stale_approvals(reviews, head_sha, live_base)
            if login.lower() == approver_login.lower()]


def unactionable_stale_approvals(reviews: list, approver_login: str, head_sha, live_base=None) -> list:
    """`login:id` of stale marked APPROVALS by any login OTHER than the approver.

    This run cannot dismiss them, so it must not report clean over them. An
    empty `approver_login` (the approver's secrets are not in this run) puts
    every stale marked approval here.
    """
    return [f"{login or '?'}:{rid}" for login, rid in _stale_approvals(reviews, head_sha, live_base)
            if not approver_login or login.lower() != approver_login.lower()]


def gh(args: list, payload=None) -> str:
    # Every failure mode is a RuntimeError, so one `except` covers a refused call,
    # a missing binary and a wedged one alike.
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


def render_body(event: str, reasons: list, threshold: str, blocking: list, base_ref: str = "") -> str:
    lines = [
        APPROVE_MARKER,
        f"<!-- cursor-review-auto-approve-base:{base_ref.encode('utf-8').hex()} -->",
        "### 🤖 Cursor Review — auto-approve",
    ]
    if event == APPROVE:
        lines.append(f"✅ Approved: {reasons[0]}. A new push or a base retarget dismisses this approval.")
    else:
        lines.append(f"❌ Changes requested: {reasons[0]}. Fix them, push, and re-run the review.")
        # severity/file/line are model output over PR content, posted under the
        # approver identity: only a known severity word is echoed, and the
        # path goes through post-review.py's own code-span renderer (backticks,
        # newlines, @mentions).
        render_code_ref = _load_post_review().render_code_ref
        for f in blocking[:20]:
            if isinstance(f, dict):
                sev = f.get("severity")
                sev = sev.strip().lower() if isinstance(sev, str) else ""
                sev = sev if sev in SEVERITY_ORDER else "unknown"
                line = f.get("line")
                line = str(line) if isinstance(line, int) and not isinstance(line, bool) else "?"
                path = f.get("file") if isinstance(f.get("file"), str) else "?"
                lines.append(f"- **{sev}** — {render_code_ref(path[:300], line)}")
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
        ungated = int(args.ungated or 0)
    except ValueError:
        ungated = 1  # unparseable → assume something missed a thread

    try:
        live_head, live_base = read_pr(args.repo, args.pr_number)
        prior = open_thread_severities(args.repo, int(args.pr_number))
    except (RuntimeError, SystemExit, ValueError) as e:
        # run_graphql exits 2 on a query failure; neither read may go unseen.
        event, reasons, blocking = NONE, [f"could not read the PR state ({e or 'thread query failed'})"], []
    else:
        event, reasons, blocking = decide(
            threshold,
            findings,
            panel,
            args.judge_status,
            args.delivered == "true",
            args.commit_sha,
            live_head,
            prior,
            ungated,
            reviewed_diff_is_empty(args.reviewed_diff),
            args.base_ref,
            live_base,
        )
    if event == NONE:
        emit(f"ℹ️ **Auto-approve: no decision** — {'; '.join(reasons)}.")
        return withdraw_own_approvals(args)

    body = render_body(event, reasons, threshold, blocking, args.base_ref)
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

    # Close the read → POST race. A push or retarget landing in that window fires
    # an event whose dismissal scan can finish before this review exists, so
    # nothing else would ever withdraw it. Re-read the head and base now that the
    # review is written; if either moved, withdraw our own review here.
    try:
        head_now, base_now = read_pr(args.repo, args.pr_number)
    except (RuntimeError, ValueError):
        head_now = base_now = None  # unknown → treat as moved: withdraw rather than leave it
    if head_now != args.commit_sha or base_now != args.base_ref:
        try:
            dismiss(args.repo, args.pr_number, posted["id"])
        except RuntimeError as e:
            print(f"::error::The PR head or base moved while the {event} review was posted, and withdrawing it failed: {e}. {DISMISS_PERMISSION_HINT}")
            return 1
        emit(f"ℹ️ **Auto-approve: withdrawn** — the PR head or base moved while the {event} review was being posted.")
        return 0
    emit(f"{'✅' if event == APPROVE else '❌'} **Auto-approve: {event}** — {reasons[0]}.")
    return 0


DISMISS_PERMISSION_HINT = (
    "Dismissing a review needs an identity allowed to dismiss reviews on this branch: "
    "pull-requests: write is not enough when branch protection restricts dismissals "
    "(add the approver to the allowed dismissers, or lift the restriction)."
)


def read_pr(repo: str, pr_number) -> tuple:
    """(head sha, base ref) of the PR as it is now."""
    pr = json.loads(gh(["api", f"repos/{repo}/pulls/{pr_number}"]))
    return (pr.get("head") or {}).get("sha", ""), (pr.get("base") or {}).get("ref", "")


def list_reviews(repo: str, pr_number) -> list:
    pages = json.loads(gh(["api", "--paginate", "--slurp", f"repos/{repo}/pulls/{pr_number}/reviews?per_page=100"]))
    return [r for page in pages for r in page] if pages and isinstance(pages[0], list) else pages


def withdraw_own_approvals(args) -> int:
    """Dismiss every marked approval by this identity. Red if any could not be."""
    try:
        ids = stale_reviews_to_dismiss(list_reviews(args.repo, args.pr_number), args.approver_login, None)
    except (RuntimeError, ValueError) as e:
        print(f"::error::Could not list reviews to withdraw an earlier auto-approval: {e}")
        return 1
    failed = []
    for rid in ids:
        try:
            dismiss(args.repo, args.pr_number, rid, UNTRUSTED_MESSAGE)
        except RuntimeError as e:
            failed.append(f"{rid}: {e}")
    if ids:
        emit(f"Auto-approve: withdrew {len(ids) - len(failed)}/{len(ids)} earlier approval(s) by {args.approver_login}.")
    if failed:
        print(f"::error::Could not withdraw {len(failed)} earlier auto-approval(s) ({'; '.join(failed)}). {DISMISS_PERMISSION_HINT}")
        return 1
    return 0


STALE_MESSAGE = "New commits pushed — cursor-review auto-approve withdrawn until the next review round."
BASE_CHANGED_MESSAGE = "The base branch changed — cursor-review auto-approve withdrawn until the next review round."
UNTRUSTED_MESSAGE = "The latest cursor-review round could not be trusted to approve — auto-approve withdrawn until a round that can."


def dismiss(repo: str, pr_number, review_id, message: str = STALE_MESSAGE) -> None:
    gh(
        ["api", "-X", "PUT", f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/dismissals", "--input", "-"],
        {"message": message, "event": "DISMISS"},
    )


def cmd_dismiss_stale(args) -> int:
    # On a retarget the head is unchanged, so an approval pinned to it is
    # exactly as stale as an off-head one: select every marked approval. That
    # covers approvals posted before the base was recorded; recorded ones are
    # caught by the live-base comparison on ANY event, retarget or not.
    message = BASE_CHANGED_MESSAGE if args.all_approvals else STALE_MESSAGE
    try:
        live_head, live_base = read_pr(args.repo, args.pr_number)
        reviews = list_reviews(args.repo, args.pr_number)
    except (RuntimeError, ValueError) as e:
        print(f"::error::Could not read the PR to dismiss stale auto-approvals: {e}")
        return 1
    head = None if args.all_approvals else live_head
    ids = stale_reviews_to_dismiss(reviews, args.approver_login, head, live_base)
    others = unactionable_stale_approvals(reviews, args.approver_login, head, live_base)
    failed = []
    for rid in ids:
        try:
            dismiss(args.repo, args.pr_number, rid, message)
        except RuntimeError as e:
            failed.append(f"{rid}: {e}")
    emit(f"Auto-approve: dismissed {len(ids) - len(failed)}/{len(ids)} stale review(s) by {args.approver_login or '(approver unavailable)'}.")
    if others:
        # Red, not clean: a stale approval this identity cannot touch still counts.
        print(f"::error::{len(others)} stale auto-approval(s) by another identity ({', '.join(others)}) — this run "
              f"cannot dismiss them (the approver changed, or its secrets are not available to this run, e.g. a "
              f"Dependabot PR). Dismiss them by hand, or re-run with the approver's secrets.")
    if failed:
        # Red, not a warning: a stale approval that stays valid is the exact
        # failure this job exists to prevent.
        print(f"::error::Could not dismiss {len(failed)} stale auto-approve review(s) ({'; '.join(failed)}). {DISMISS_PERMISSION_HINT}")
    return 1 if failed or others else 0


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
    d.add_argument("--ungated", default="0")
    d.add_argument("--approver-login", required=True)
    d.add_argument("--reviewed-diff", required=True)
    # The base the panel diffed against (the triggering event's base.ref).
    d.add_argument("--base-ref", required=True)
    s = sub.add_parser("dismiss-stale")
    s.add_argument("--repo", required=True)
    s.add_argument("--pr-number", required=True)
    # Empty = the approver identity is not available to this run: dismiss
    # nothing, and go red over any stale marked approval.
    s.add_argument("--approver-login", required=True)
    s.add_argument("--all-approvals", action="store_true")
    args = parser.parse_args()
    return cmd_decide(args) if args.cmd == "decide" else cmd_dismiss_stale(args)


if __name__ == "__main__":
    sys.exit(main())
