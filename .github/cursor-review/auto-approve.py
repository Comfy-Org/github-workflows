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

``dismiss-stale`` (in `dismiss-stale-approval`, on every same-repo
`pull_request` event of an open PR; ``--all-approvals`` on a base retarget)
    Dismiss this identity's own auto-approve APPROVALS that are stale against
    the PR's LIVE state: not on its current head, or recorded (``BASE_MARKER``)
    against a base other than its current one — and, on a retarget, every one
    of them, for approvals posted before the base was recorded. Staleness is
    read from the PR, not from the event payload, so whichever event runs next
    redoes a dismissal a cancelled run left undone. (Only when the PR cannot be
    read does it fall back to the event's head, skipping the base check.) A stale marked approval by
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

``round-cap`` (in `round-cap`, before the panel starts)
    Count the consolidated reviews the posting identity has already left on
    this PR — only those after the most recent removal of ``needs-human-review``,
    so removing the label resets the cap. At or over `max_rounds`, label the PR
    ``needs-human-review`` (creating the label if missing), post ONE comment
    listing the latest round's open findings above the threshold (at most once
    per cap, keyed on ``ROUND_CAP_MARKER``), and tell the workflow to skip the
    panel. A read failure fails OPEN — the panel runs, as it did before the cap
    existed — because a cap nobody can count is not evidence of a runaway loop.
    So does a label that cannot be applied: the label is the only reset, and a
    cap with nothing for a human to remove would never lift.

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
judge can steer the round to an approval. The recorded SHA shares that ceiling:
a review body is mutable by anyone with repo WRITE access (and by the approver
token itself), so rewriting the marker to the current head keeps a stale approval
valid — a strictly higher privilege than planting text in a diff, and not one
`commit_id` can cross-check, since GitHub has already moved that field to the
same head. Treat the approval as an automated review signal, not as a substitute
for a human reviewer, and read the caller guide's trust-model section before
letting it satisfy a ruleset.

Only reviews carrying ``APPROVE_MARKER`` and authored by the approver login are
ever dismissed, so a human's review — or another bot's — is never touched.
"""

import argparse
import importlib.util
import json
import math
import os
import re
import subprocess
import sys

SEVERITY_ORDER = ["critical", "high", "medium", "low", "nit"]
ALLOWED_THRESHOLDS = ("medium", "low", "nit")
APPROVE_MARKER = "<!-- cursor-review-auto-approve -->"
# The commit this decision reviewed, recorded IN the body. A review's API
# `commit_id` cannot be trusted for staleness: with `dismiss_stale_reviews_on_push`
# off, GitHub moves a still-valid approval's `commit_id` forward to each new head
# (seen live: an approval on b01cf98 read back as edaf99f after a push), so a
# commit_id comparison never finds the approval stale.
REVIEWED_SHA_RE = re.compile(r"<!-- cursor-review-auto-approve:sha=([0-9a-f]{40}) -->")
# The base branch a review was posted against, hex-encoded so no ref name can
# close the HTML comment early. dismiss-stale withdraws an approval whose
# recorded base is no longer the PR's base.
BASE_MARKER_RE = re.compile(r"<!-- cursor-review-auto-approve-base:([0-9a-f]*) -->")
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
# post-review.py opens two bodies with CONSOLIDATED_MARKER that report a round
# which reviewed NOTHING — the "Review failed" error review and the
# all-panel-cells-failed review (both `delivers=False`). Neither spent a panel's
# judgement on the PR, so neither counts toward `max_rounds`: five transient
# CLI failures must not cap a PR no panel ever looked at. Pinned against
# post-review.py's source by a test, so a reworded banner fails CI here.
NON_ROUND_BANNERS = ("\n⚠️ **Review failed**\n", "\n⚠️ **Panel did not produce any findings.**\n")


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
        reviewed_diff_empty, reviewed_base, live_base,
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
    reviewed_diff_empty: bool = False,
    reviewed_base: str = "",
    live_base: str = "",
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
    if reviewed_base != live_base:
        # A retarget never moves the head, so the head check cannot see it.
        reasons.append("the PR base branch changed while the review ran")
    if reviewed_diff_empty:
        reasons.append("the reviewed diff is empty — every changed path was excluded from review, so nothing a reviewer saw can earn an approval")
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
        # The SHA recorded in the body, NOT `commit_id` (see REVIEWED_SHA_RE). A
        # marked approval without a recorded SHA predates that record: treat it
        # as stale rather than trust a commit_id GitHub may have moved.
        match = REVIEWED_SHA_RE.search(body)
        off_head = head_sha is None or not match or match.group(1) != head_sha.lower()
        base = recorded_base(body)
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


def render_body(event: str, reasons: list, threshold: str, blocking: list, reviewed_sha: str = "",
                base_ref: str = "") -> str:
    lines = [APPROVE_MARKER]
    if re.fullmatch(r"[0-9a-f]{40}", (reviewed_sha or "").lower()):
        lines.append(f"<!-- cursor-review-auto-approve:sha={reviewed_sha.lower()} -->")
    if base_ref:
        lines.append(f"<!-- cursor-review-auto-approve-base:{base_ref.encode('utf-8').hex()} -->")
    lines.append("### 🤖 Cursor Review — auto-approve")
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
        live_head, live_base = pr_head_base(pr)
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
            reviewed_diff_is_empty(args.reviewed_diff),
            args.base_ref,
            live_base,
        )
    set_output("approve_gate", gate)
    if event == NONE:
        emit(f"ℹ️ **Auto-approve: no decision** — {'; '.join(reasons)}.")
        return withdraw_own_approvals(args)

    body = render_body(event, reasons, threshold, blocking, args.commit_sha, args.base_ref)
    if event == APPROVE and not REVIEWED_SHA_RE.search(body):
        # The recorded SHA is the only staleness anchor an approval has (see
        # REVIEWED_SHA_RE), and render_body drops the marker for anything that is
        # not a full 40-hex SHA. decide() cannot reach APPROVE unless commit_sha
        # equals the head read back from the API, so this is unreachable today —
        # but a future caller that loosened that would otherwise post an approval
        # dismiss-stale reads as legacy/stale forever, with no diagnostic anywhere.
        print(
            f"::warning::Approving without a reviewed-SHA marker: --commit-sha {args.commit_sha!r} "
            "is not a full 40-hex SHA, so dismiss-stale will treat this approval as stale."
        )
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
        # No review landed, so this round's verdict stands behind nothing: the
        # gate must not read pass/fail, and an earlier approval must not keep
        # satisfying branch protection — dismiss-stale leaves one alone while its
        # recorded head and base still match. A POST that failed ambiguously (a
        # timeout) may have written an APPROVE anyway; the withdrawal lists live
        # reviews, so it catches that one too.
        set_output("approve_gate", GATE_UNTRUSTED)
        print(f"::error::Could not submit the {event} review: {e}")
        withdraw_own_approvals(args)
        return 1

    # Close the read → POST race. A push or retarget landing in that window fires
    # an event whose dismissal scan can finish before this review exists, so
    # nothing else would ever withdraw it. Re-read the head and base now that the
    # review is written; if either moved, withdraw our own review here.
    #
    # The same window can label the PR `needs-human-review` — a human, or a
    # concurrent run hitting the round cap — and an APPROVE must not outlive
    # that hand-off either. (A REQUEST_CHANGES withholds nothing, so it stays.)
    try:
        pr_now = read_pr(args.repo, args.pr_number)
        head_now, base_now = pr_head_base(pr_now)
        labelled_now = has_label(pr_now, HUMAN_REVIEW_LABEL)
    except (RuntimeError, ValueError):
        head_now = base_now = None  # unknown → treat as moved: withdraw rather than leave it
        labelled_now = False
    moved = head_now != args.commit_sha or base_now != args.base_ref
    if moved or (event == APPROVE and labelled_now):
        set_output("approve_gate", GATE_UNTRUSTED if moved else GATE_CAPPED)
        why = "the PR head or base moved" if moved else f"the PR was labelled `{HUMAN_REVIEW_LABEL}`"
        try:
            dismiss(args.repo, args.pr_number, posted["id"], STALE_MESSAGE if moved else HUMAN_REVIEW_MESSAGE)
        except RuntimeError as e:
            print(f"::error::{why[0].upper()}{why[1:]} while the {event} review was posted, and withdrawing it failed: {e}. {DISMISS_PERMISSION_HINT}")
            return 1
        emit(f"ℹ️ **Auto-approve: withdrawn** — {why} while the {event} review was being posted.")
        return 0
    emit(f"{'✅' if event == APPROVE else '❌'} **Auto-approve: {event}** — {reasons[0]}.")
    return 0


DISMISS_PERMISSION_HINT = (
    "Dismissing a review needs an identity allowed to dismiss reviews on this branch: "
    "pull-requests: write is not enough when branch protection restricts dismissals "
    "(add the approver to the allowed dismissers, or lift the restriction)."
)


def read_pr(repo: str, pr_number) -> dict:
    """The PR as it is now. One read serves head, base AND labels."""
    return json.loads(gh(["api", f"repos/{repo}/pulls/{pr_number}"]))


def pr_head_base(pr: dict) -> tuple:
    """(head sha, base ref) of a PR payload — "" for either when it is shapeless."""
    return (pr.get("head") or {}).get("sha", ""), (pr.get("base") or {}).get("ref", "")


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
BASE_CHANGED_MESSAGE = "The base branch changed — cursor-review auto-approve withdrawn until the next review round."
UNTRUSTED_MESSAGE = "The latest cursor-review round could not be trusted to approve — auto-approve withdrawn until a round that can."
HUMAN_REVIEW_MESSAGE = f"The PR was labelled `{HUMAN_REVIEW_LABEL}` — cursor-review auto-approve withdrawn."


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
    # The LIVE head and base, not the event's. The caller's
    # `github.event.pull_request.head.sha` is the head at event-delivery time, so
    # a queued or REDELIVERED `synchronize` for an older push would otherwise
    # dismiss an approval that is valid for the head as it stands now, and do it
    # silently: a wrong-but-successful dismissal exits green, so nothing reports
    # the loss. `cmd_decide` reads the PR for this same reason.
    #
    # A failed or shapeless read falls back to the event's head (`--head-sha`),
    # the older comparison, rather than skip the scan this job exists to perform;
    # the base check is skipped for that run (no live base to compare), and the
    # next event redoes it from live state. The `or` matters as much as the
    # `except`: an empty string is not None, so it would match no recorded SHA
    # and dismiss every marked approval on the PR.
    try:
        live_head, live_base = pr_head_base(read_pr(args.repo, args.pr_number))
    except (RuntimeError, ValueError) as e:
        live_head, live_base, read_error = "", None, e
    else:
        read_error = None
    # An empty base is not None either: it would match no recorded base and
    # dismiss every marked approval. No base → skip the base check, as below.
    live_base = live_base or None
    if not live_head:
        live_head, live_base = args.head_sha, None
        if not live_head:
            print(f"::error::Could not read the PR head to dismiss stale auto-approvals: {read_error or 'no head in the response'}")
            return 1
        print(f"::warning::Could not read the live PR head ({read_error or 'no head in the response'}) — judging "
              "staleness against the event's head, without the base check, for this run.")
    try:
        reviews = list_reviews(args.repo, args.pr_number)
    except (RuntimeError, ValueError) as e:
        print(f"::error::Could not list reviews to dismiss stale auto-approvals: {e}")
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
    still count — a round was spent either way. A body reporting that the round
    reviewed nothing (NON_ROUND_BANNERS) does not.
    """
    out = [
        r for r in reviews
        if isinstance(r, dict)
        and ((r.get("user") or {}).get("login") or "").lower() == poster_login.lower()
        and (r.get("body") or "").startswith(marker)
        and not any(b in (r.get("body") or "") for b in NON_ROUND_BANNERS)
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
    """Apply HUMAN_REVIEW_LABEL to the PR, creating it repo-side first if missing.

    The probe and the create are advisory, not a gate — the same posture as
    scripts/pr-risk/apply-risk-label.sh. Only a 404 means "missing"; a 403 or a
    5xx on the probe says nothing about the label. Creating a repo label needs
    `issues: write`, which a `pull-requests: write`-only token lacks, and a
    concurrent run can create it first (422 already_exists); either way the add
    below is still attempted, and it is the add's result that decides.
    """
    try:
        gh(["api", f"repos/{repo}/labels/{HUMAN_REVIEW_LABEL}"])
    except RuntimeError as e:
        if "HTTP 404" in str(e) or "Not Found" in str(e):
            try:
                gh(
                    ["api", "-X", "POST", f"repos/{repo}/labels", "--input", "-"],
                    {
                        "name": HUMAN_REVIEW_LABEL,
                        "color": "d93f0b",
                        "description": "cursor-review hit its round cap; a human has to review this PR",
                    },
                )
            except RuntimeError as create_err:
                print(f"::warning::Could not create the `{HUMAN_REVIEW_LABEL}` label ({create_err}); "
                      "trying to apply it anyway.")
        else:
            print(f"::warning::The `{HUMAN_REVIEW_LABEL}` label probe failed with a non-404 ({e}); "
                  "trying to apply it anyway.")
    gh(
        ["api", "-X", "POST", f"repos/{repo}/issues/{pr_number}/labels", "--input", "-"],
        {"labels": [HUMAN_REVIEW_LABEL]},
    )


def parse_max_rounds(value) -> int:
    """`max_rounds` as an int; ValueError unless it is a finite whole number.

    A `type: number` input can render as `5` or `5.0`, so a float that is
    integral is accepted — `2.9`, `inf` and `nan` are not silently truncated.
    """
    raw = str(value).strip() or "0"
    try:
        number = float(raw)
    except ValueError:
        raise ValueError(f"max_rounds must be a whole number, got {value!r}") from None
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"max_rounds must be a whole number, got {value!r}")
    return int(number)


def cmd_round_cap(args) -> int:
    set_output("capped", "false")
    set_output("labelled", "false")
    try:
        max_rounds = parse_max_rounds(args.max_rounds)
    except ValueError as e:
        # Fail OPEN like every other way this step can go wrong: a caller typo
        # must not take the whole panel down (diff-size hangs off this job).
        print(f"::error::{e} — the round cap is not applied this run.")
        return 0
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
        # For the `approve_gate` output only: a PR a human already handed to a
        # human reports `capped` even when auto-approve (and so decide) is off.
        if has_label(read_pr(args.repo, args.pr_number), HUMAN_REVIEW_LABEL):
            set_output("labelled", "true")
    except (RuntimeError, ValueError) as e:
        print(f"::warning::Could not read this PR's labels: {e}")
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

    # The label is the hand-off AND the only reset, so it goes on BEFORE the PR
    # is committed to `capped`. Capped-but-unlabelled would be permanent: every
    # later trigger recounts, retries the same failing write and skips the panel,
    # with no label for a human to remove. So a label that cannot be applied
    # fails OPEN, like an unreadable count.
    try:
        ensure_label(args.repo, args.pr_number)
    except RuntimeError as e:
        print(f"::warning::Round cap reached ({len(rounds)}/{max_rounds}) but the `{HUMAN_REVIEW_LABEL}` label "
              f"could not be applied ({e}), so the cap is not applied this run and the panel runs. Create the "
              f"label in this repo (creating one needs `issues: write`), or check the posting token's "
              f"`pull-requests: write`.")
        return 0
    set_output("capped", "true")
    set_output("labelled", "true")
    emit(f"🛑 **Round cap reached** — {len(rounds)}/{max_rounds} round(s); skipped the panel and labelled `{HUMAN_REVIEW_LABEL}`.")
    failed = []
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
        # Still capped (the panel stays skipped, and the label is on, so a human
        # can reset it); red so the missing comment is seen.
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
    # The event's head: used ONLY when the live PR head cannot be read.
    s.add_argument("--head-sha", default="")
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
