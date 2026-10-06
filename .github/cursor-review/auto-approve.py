#!/usr/bin/env python3
"""Opt-in auto-approve: turn a cursor-review round into an approval decision.

Used by cursor-review.yml when the caller sets `approve_max_severity` (and,
for ``round-cap``, `max_rounds`). Three subcommands, each run from a job that
checks out NO PR code:

``decide`` (in `post-review`, after the consolidated review is posted)
    APPROVE when every finding of this round is at or below the threshold and
    nothing below says "don't trust this round"; REQUEST_CHANGES when any finding
    is above it; otherwise submit nothing. The review is pinned (`commit_id`) to
    the commit the panel reviewed.

``dismiss-stale`` (in `dismiss-stale-approval`, on `synchronize`)
    Dismiss this identity's own auto-approve APPROVALS that are not on the new
    head. The caller's ruleset may keep an approval valid across pushes
    (`dismiss_stale_reviews_on_push: false`), so the bot has to withdraw its own.
    CHANGES_REQUESTED is deliberately NOT dismissed on a push: under the
    label-triggered caller a push starts no new round, so dismissing it would let
    any trivial push clear the bot's veto. Only a later round's own review event
    supersedes it (GitHub counts a reviewer's most recent review).

``round-cap`` (in `round-cap`, before the panel starts)
    Count the consolidated reviews the posting identity has already left on
    this PR — only those after the most recent removal of ``needs-human-review``,
    so removing the label resets the cap. At or over `max_rounds`, label the PR
    ``needs-human-review`` (creating the label if missing), post ONE comment
    listing the latest round's open findings above the threshold (at most once
    per cap, keyed on ``ROUND_CAP_MARKER``), and tell the workflow to skip the
    panel. A read failure fails OPEN — the panel runs, as it did before the cap
    existed — because a cap nobody can count is not evidence of a runaway loop.

``decide`` also emits ``approve_gate`` (one of ``APPROVE_GATE_VALUES``) as a
step output, for a downstream workflow that should run only after a round
passed the severity gate:

* ``pass`` — decide() returned APPROVE;
* ``fail`` — REQUEST_CHANGES, or an open thread above the threshold withheld
  approval;
* ``untrusted`` — any trust check below failed, the PR could not be read, or
  the head moved while the review was being posted;
* ``capped`` — the PR carries ``needs-human-review`` (set by ``round-cap``, or
  by a human): it is never approved;
* ``off`` — set by the workflow itself when `approve_max_severity` is empty.

Fail-closed rules for ``decide``:

* the threshold is not one of ``medium``, ``low``, ``nit`` → exit 2, red;
* the PR carries ``needs-human-review`` → no review event, ``capped``.

An untrusted round submits NO review event — neither approve nor request changes
(its findings are still on the PR as threads):

* the judge did not adjudicate (degraded panel-union fallback);
* any panel cell is not ``ok`` (a short panel finding nothing proves nothing);
* the review did not land as resolvable threads (`delivered` is not ``true``,
  or any finding reached the review body only — ``ungated_findings`` > 0 — where
  the open-thread check below cannot see it);
* the PR state or its threads could not be read;
* the PR head moved while the panel ran. A push racing the POST itself is
  caught by re-reading the head after the write and withdrawing the review.

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
# post-review.py prefixes every inline comment with `<emoji> **<Label>** — `.
BADGE_RE = re.compile(r"^\S+\s+\*\*(Critical|High|Medium|Low|Nit)\*\*\s+—")

APPROVE = "APPROVE"
REQUEST_CHANGES = "REQUEST_CHANGES"
NONE = "NONE"

GATE_PASS = "pass"
GATE_FAIL = "fail"
GATE_UNTRUSTED = "untrusted"
GATE_CAPPED = "capped"
GATE_OFF = "off"
APPROVE_GATE_VALUES = (GATE_PASS, GATE_FAIL, GATE_UNTRUSTED, GATE_CAPPED, GATE_OFF)

# The label the round cap applies and decide() refuses to approve over. Its
# removal (an `unlabeled` timeline event) is what resets the round count.
HUMAN_REVIEW_LABEL = "needs-human-review"
ROUND_CAP_MARKER = "<!-- cursor-review-round-cap -->"


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
    human_review: bool = False,
):
    """Return (event, reasons, blocking_findings). Pure; no I/O.

    Trust checks come first and gate BOTH review events: a round that cannot be
    trusted neither approves nor requests changes. Its findings are still on the
    PR as threads; only the review event is withheld.
    """
    event, _, reasons, blocking = decide_gate(
        threshold, findings, panel, judge_status, delivered, reviewed_sha,
        live_head_sha, open_thread_severities, ungated, human_review,
    )
    return event, reasons, blocking


def decide_gate(
    threshold: str,
    findings: list,
    panel: list,
    judge_status: str,
    delivered: bool,
    reviewed_sha: str,
    live_head_sha: str,
    open_thread_severities: list,
    ungated: int = 0,
    human_review: bool = False,
):
    """decide(), plus the approve_gate value: (event, gate, reasons, blocking)."""
    if human_review:
        # Before the trust checks: a PR handed to a human is never approved,
        # whatever this round found. NONE also withdraws an earlier approval.
        return NONE, GATE_CAPPED, [f"the PR carries `{HUMAN_REVIEW_LABEL}`"], []
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
    if reasons:
        return NONE, GATE_UNTRUSTED, reasons, []

    blocking = [
        f for f in findings if not isinstance(f, dict) or above_threshold(f.get("severity"), threshold)
    ]
    if blocking:
        return REQUEST_CHANGES, GATE_FAIL, [f"{len(blocking)} finding(s) above `{threshold}`"], blocking

    # Same line as this round's findings, so a Medium thread from an earlier round
    # blocks a `low` threshold exactly as a Medium finding this round would.
    open_blocking = [
        s for s in open_thread_severities if s is None or above_threshold(s, threshold)
    ]
    if open_blocking:
        return NONE, GATE_FAIL, [f"{len(open_blocking)} open thread(s) above `{threshold}` (or unbadged) from an earlier round"], []
    return APPROVE, GATE_PASS, [f"every finding is at or below `{threshold}`"], []


def stale_reviews_to_dismiss(reviews: list, approver_login: str, head_sha) -> list:
    """Ids of this identity's live auto-approve APPROVALS not on head_sha.

    `head_sha=None` selects every such approval, wherever it is pinned.
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


def set_output(key: str, value) -> None:
    """Append one `key=value` to $GITHUB_OUTPUT (a no-op outside Actions)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


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
    # Written before anything can fail, so every exit path below leaves a value;
    # each decision overwrites it (GITHUB_OUTPUT keeps the last write of a key).
    set_output("approve_gate", GATE_UNTRUSTED)
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
        pr = read_pr(args.repo, args.pr_number)
        live_head = (pr.get("head") or {}).get("sha", "")
        human_review = has_label(pr, HUMAN_REVIEW_LABEL)
        prior = open_thread_severities(args.repo, int(args.pr_number))
    except (RuntimeError, SystemExit, ValueError) as e:
        # run_graphql exits 2 on a query failure; neither read may go unseen.
        event, gate, reasons, blocking = NONE, GATE_UNTRUSTED, [f"could not read the PR state ({e or 'thread query failed'})"], []
    else:
        event, gate, reasons, blocking = decide_gate(
            threshold,
            findings,
            panel,
            args.judge_status,
            args.delivered == "true",
            args.commit_sha,
            live_head,
            prior,
            ungated,
            human_review,
        )
    set_output("approve_gate", gate)
    if event == NONE:
        emit(f"ℹ️ **Auto-approve: no decision** — {'; '.join(reasons)}.")
        return withdraw_own_approvals(args)

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
        head_now = read_head(args.repo, args.pr_number)
    except (RuntimeError, ValueError):
        head_now = ""  # unknown → treat as moved: withdraw rather than leave it
    if head_now != args.commit_sha:
        set_output("approve_gate", GATE_UNTRUSTED)
        try:
            dismiss(args.repo, args.pr_number, posted["id"])
        except RuntimeError as e:
            print(f"::error::The PR head moved while the {event} review was posted, and withdrawing it failed: {e}. {DISMISS_PERMISSION_HINT}")
            return 1
        emit(f"ℹ️ **Auto-approve: withdrawn** — the PR head moved while the {event} review was being posted.")
        return 0
    emit(f"{'✅' if event == APPROVE else '❌'} **Auto-approve: {event}** — {reasons[0]}.")
    return 0


DISMISS_PERMISSION_HINT = (
    "Dismissing a review needs an identity allowed to dismiss reviews on this branch: "
    "pull-requests: write is not enough when branch protection restricts dismissals "
    "(add the approver to the allowed dismissers, or lift the restriction)."
)


def read_pr(repo: str, pr_number) -> dict:
    return json.loads(gh(["api", f"repos/{repo}/pulls/{pr_number}"]))


def read_head(repo: str, pr_number) -> str:
    return (read_pr(repo, pr_number).get("head") or {}).get("sha", "")


def has_label(pr: dict, name: str) -> bool:
    return any(
        isinstance(label, dict) and (label.get("name") or "").lower() == name.lower()
        for label in pr.get("labels") or []
    )


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
UNTRUSTED_MESSAGE = "The latest cursor-review round could not be trusted to approve — auto-approve withdrawn until a round that can."


def dismiss(repo: str, pr_number, review_id, message: str = STALE_MESSAGE) -> None:
    gh(
        ["api", "-X", "PUT", f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/dismissals", "--input", "-"],
        {"message": message, "event": "DISMISS"},
    )


def cmd_dismiss_stale(args) -> int:
    ids = stale_reviews_to_dismiss(list_reviews(args.repo, args.pr_number), args.approver_login, args.head_sha)
    failed = []
    for rid in ids:
        try:
            dismiss(args.repo, args.pr_number, rid)
        except RuntimeError as e:
            failed.append(f"{rid}: {e}")
    emit(f"Auto-approve: dismissed {len(ids) - len(failed)}/{len(ids)} stale review(s) by {args.approver_login}.")
    if failed:
        # Red, not a warning: a stale approval that stays valid is the exact
        # failure this job exists to prevent.
        print(f"::error::Could not dismiss {len(failed)} stale auto-approve review(s) ({'; '.join(failed)}). {DISMISS_PERMISSION_HINT}")
        return 1
    return 0


def last_unlabeled_at(timeline: list, label: str):
    """created_at of the most recent `unlabeled` event for `label`, or None."""
    stamps = [
        e.get("created_at") or ""
        for e in timeline
        if isinstance(e, dict)
        and e.get("event") == "unlabeled"
        and ((e.get("label") or {}).get("name") or "").lower() == label.lower()
    ]
    return max(stamps) if any(stamps) else None


def _after(stamp, since) -> bool:
    # GitHub renders every timestamp as `YYYY-MM-DDTHH:MM:SSZ`, so string order
    # is time order. No reset yet → everything counts.
    return since is None or (isinstance(stamp, str) and stamp > since)


def round_reviews(reviews: list, poster_login: str, since, marker: str) -> list:
    """The consolidated reviews `poster_login` posted after `since`, oldest first.

    Body AND author: the marker is public text anyone can put in a review, so a
    body match alone would let any user burn a PR's rounds. Dismissed reviews
    still count — a round was spent either way.
    """
    out = [
        r for r in reviews
        if isinstance(r, dict)
        and ((r.get("user") or {}).get("login") or "").lower() == poster_login.lower()
        and (r.get("body") or "").startswith(marker)
        and _after(r.get("submitted_at"), since)
    ]
    return sorted(out, key=lambda r: r.get("submitted_at") or "")


def cap_comment_posted(comments: list, poster_login: str, since) -> bool:
    """True when this cap's comment is already on the PR (posted after `since`)."""
    return any(
        isinstance(c, dict)
        and ((c.get("user") or {}).get("login") or "").lower() == poster_login.lower()
        and ROUND_CAP_MARKER in (c.get("body") or "")
        and _after(c.get("created_at"), since)
        for c in comments
    )


def cap_findings(review_comments: list, open_ids, threshold: str) -> list:
    """(severity, path, line) of the latest round's findings still above `threshold`.

    `open_ids` is the set of first-comment ids of unresolved, non-outdated
    threads, or None when that could not be read (then nothing is filtered:
    over-listing beats hiding a finding). An empty threshold lists every finding.
    """
    out = []
    for c in review_comments:
        if not isinstance(c, dict) or c.get("in_reply_to_id"):
            continue
        if open_ids is not None and str(c.get("id")) not in open_ids:
            continue
        sev = thread_severity(c.get("body") or "")
        if threshold and sev is not None and not above_threshold(sev, threshold):
            continue
        line = c.get("line") or c.get("original_line")
        out.append((sev or "unknown", c.get("path") if isinstance(c.get("path"), str) else "?", line))
    return out


def render_cap_comment(rounds: int, max_rounds: int, findings: list, threshold: str, review_url: str) -> str:
    render_code_ref = _load_post_review().render_code_ref
    scope = f"above `{threshold}`" if threshold else "still open"
    lines = [
        ROUND_CAP_MARKER,
        "### 🛑 Cursor Review — round cap reached",
        f"This PR has had {rounds} review round(s) (`max_rounds: {max_rounds}`), so no new "
        f"panel ran and it is labelled `{HUMAN_REVIEW_LABEL}`. Auto-approve will not "
        "approve it while the label is on; removing the label resets the round count.",
    ]
    if findings:
        lines.append(f"\nFindings from the latest round {scope}:")
        for sev, path, line in findings[:30]:
            line = str(line) if isinstance(line, int) and not isinstance(line, bool) else "?"
            lines.append(f"- **{sev}** — {render_code_ref(path[:300], line)}")
        if len(findings) > 30:
            lines.append(f"- …and {len(findings) - 30} more.")
    else:
        lines.append(f"\nThe latest round left no open finding {scope}.")
    if review_url.startswith("https://"):
        lines.append(f"\nLatest round: {review_url}")
    return "\n".join(lines)


def _paginate(path: str) -> list:
    pages = json.loads(gh(["api", "--paginate", "--slurp", path]))
    return [x for page in pages for x in page] if pages and isinstance(pages[0], list) else pages


def open_thread_ids(repo: str, pr: int):
    """First-comment ids of every open cursor-review thread, or None if unreadable."""
    gate = _load_gate_unresolved()
    owner, _, name = repo.partition("/")
    ids = set()
    try:
        for thread in gate.iter_threads(owner, name, pr):
            if not gate.is_cursor_thread(thread) or thread.get("isResolved") or thread.get("isOutdated"):
                continue
            first = ((thread.get("comments") or {}).get("nodes") or [{}])[0]
            ident = first.get("fullDatabaseId") or first.get("databaseId")
            if ident is not None:
                ids.add(str(ident))
    except (RuntimeError, SystemExit, ValueError, KeyError, TypeError):
        return None
    return ids


def ensure_label(repo: str, pr_number) -> None:
    try:
        gh(["api", f"repos/{repo}/labels/{HUMAN_REVIEW_LABEL}"])
    except RuntimeError:
        gh(
            ["api", "-X", "POST", f"repos/{repo}/labels", "--input", "-"],
            {
                "name": HUMAN_REVIEW_LABEL,
                "color": "d93f0b",
                "description": "cursor-review hit its round cap; a human has to review this PR",
            },
        )
    gh(
        ["api", "-X", "POST", f"repos/{repo}/issues/{pr_number}/labels", "--input", "-"],
        {"labels": [HUMAN_REVIEW_LABEL]},
    )


def cmd_round_cap(args) -> int:
    set_output("capped", "false")
    try:
        # A `type: number` input can render as `5` or `5.0`.
        max_rounds = int(float(str(args.max_rounds).strip() or 0))
    except ValueError:
        print(f"::error::max_rounds must be a whole number, got {args.max_rounds!r}")
        return 2
    if max_rounds <= 0:
        emit("Round cap: off (`max_rounds: 0`).")
        return 0
    # The threshold only shapes the comment here; an invalid one is decide()'s
    # to fail on, so it must not stop the cap.
    try:
        threshold = validate_threshold(args.threshold) if (args.threshold or "").strip() else ""
    except ValueError:
        threshold = ""
    marker = _load_post_review().CONSOLIDATED_MARKER
    try:
        since = last_unlabeled_at(
            _paginate(f"repos/{args.repo}/issues/{args.pr_number}/timeline?per_page=100"), HUMAN_REVIEW_LABEL
        )
        rounds = round_reviews(list_reviews(args.repo, args.pr_number), args.poster_login, since, marker)
    except (RuntimeError, ValueError) as e:
        # Fail OPEN: the panel runs exactly as it did before the cap existed.
        print(f"::warning::Could not count this PR's review rounds, so the round cap is not applied this run: {e}")
        return 0
    set_output("rounds", len(rounds))
    if len(rounds) < max_rounds:
        emit(f"Round cap: {len(rounds)}/{max_rounds} round(s) so far — running the panel.")
        return 0

    set_output("capped", "true")
    emit(f"🛑 **Round cap reached** — {len(rounds)}/{max_rounds} round(s); skipping the panel and labelling `{HUMAN_REVIEW_LABEL}`.")
    failed = []
    try:
        ensure_label(args.repo, args.pr_number)
    except RuntimeError as e:
        failed.append(f"label: {e}")
    try:
        comments = _paginate(f"repos/{args.repo}/issues/{args.pr_number}/comments?per_page=100")
        if cap_comment_posted(comments, args.poster_login, since):
            emit("Round cap: the cap comment is already on this PR — not posting it again.")
        else:
            latest = rounds[-1]
            review_comments = _paginate(
                f"repos/{args.repo}/pulls/{args.pr_number}/reviews/{latest['id']}/comments?per_page=100"
            )
            findings = cap_findings(review_comments, open_thread_ids(args.repo, int(args.pr_number)), threshold)
            body = render_cap_comment(len(rounds), max_rounds, findings, threshold, latest.get("html_url") or "")
            gh(["api", "-X", "POST", f"repos/{args.repo}/issues/{args.pr_number}/comments", "--input", "-"], {"body": body})
    except (RuntimeError, ValueError, KeyError) as e:
        failed.append(f"comment: {e}")
    if failed:
        # Still capped (the panel stays skipped); red so the missing label or
        # comment is seen.
        print(f"::error::Round cap reached but could not finish handing the PR to a human ({'; '.join(failed)}).")
        return 1
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
    d.add_argument("--ungated", default="0")
    d.add_argument("--approver-login", required=True)
    s = sub.add_parser("dismiss-stale")
    s.add_argument("--repo", required=True)
    s.add_argument("--pr-number", required=True)
    s.add_argument("--head-sha", required=True)
    s.add_argument("--approver-login", required=True)
    c = sub.add_parser("round-cap")
    c.add_argument("--repo", required=True)
    c.add_argument("--pr-number", required=True)
    c.add_argument("--max-rounds", required=True)
    c.add_argument("--threshold", default="")
    c.add_argument("--poster-login", required=True)
    args = parser.parse_args()
    if args.cmd == "round-cap":
        return cmd_round_cap(args)
    return cmd_decide(args) if args.cmd == "decide" else cmd_dismiss_stale(args)


if __name__ == "__main__":
    sys.exit(main())
