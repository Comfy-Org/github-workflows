#!/usr/bin/env python3
"""Opt-in auto-approve: turn a cursor-review round into an approval decision.

Used by cursor-review.yml when the caller sets `approve_max_severity` (and,
for ``round-cap``, `max_rounds`), and by cursor-approve.yml (``approve-external``).
Five subcommands, each run from a job that checks out NO PR code:

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

``approve-external`` (in cursor-approve.yml's decide phase)
    Post an APPROVE decided outside cursor-review (by
    ``.github/cursor-approve/aggregate.py``) under the same protections — see
    ``cmd_approve_external``. Given ``--threshold`` and ``--poster-login``, an
    APPROVE that survives the re-check auto-resolves threads exactly as
    ``decide`` does (below).

``withdraw`` (in cursor-approve.yml's start phase)
    Withdraw this identity's own marked approvals while the axes run — see
    ``cmd_withdraw``.

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
* ``off`` — set by the workflow itself when `approve_max_severity` is empty, and
  by ``decide`` (``--author-enabled false``) when `approve_authors` does not list
  the PR's author: no review event is posted, though the findings still are.

Fail-closed rules for ``decide``:

* the threshold is not one of ``medium``, ``low``, ``nit`` → exit 2, red;
* the PR carries ``needs-human-review`` → no review event, ``capped``.

An untrusted round submits NO review event — neither approve nor request changes
(its findings are still on the PR as threads):

* the judge did not adjudicate (degraded panel-union fallback);
* any panel cell is not ``ok`` (a short panel finding nothing proves nothing) —
  unless ``--max-failed-reviewers N`` (the workflow's
  `approve_max_failed_reviewers`, default 0) tolerates it. Only a cell that ran
  and reported ``status: "error"`` is tolerable, at most N of them, and only
  while every review type (``PANEL_REVIEW_TYPES``) still has an ``ok`` cell. A
  non-dict cell, a missing or unknown status, or an empty panel always
  withholds. Tolerated cells are named in the decision's reason;
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

After an APPROVE that survived the post-write head/base re-check — and only
then — the poster's own threads (``--poster-login``) whose badge is at or below
the threshold, and in which no other account has commented, are resolved and
then get a reply from the approver identity (``AUTO_RESOLVE_MARKER``), so a
ruleset that requires resolved conversations does not hold the approval hostage
to nits. If any live thread is above the threshold or unbadged, nothing is
resolved. Each thread is re-read just before it is resolved; at most
``MAX_AUTO_RESOLVE`` per round, stopping after ``MAX_CONSECUTIVE_FAILURES`` in a
row. A failure on one thread is logged and skipped; it never undoes the approval.

An untrusted round (or an open blocking thread) also WITHDRAWS this identity's
own earlier marked approvals, wherever they are pinned: GitHub counts a
reviewer's most recent review, so a round-1 APPROVED would otherwise keep
counting through a degraded re-run at the same head.

``--defer-approval true`` (the workflow's `defer_approval`, for a caller that
runs cursor-approve's axes after this round): an APPROVE outcome still reports
``pass``, but posts NO review and resolves NO thread — it withdraws this
identity's own earlier marked approvals instead, exactly as a no-decision round
does, so cursor-approve's ``approve-external`` is the only thing that ever
approves and no severity-only approval can satisfy branch protection while the
axes judge (or after a failed run that never reaches them). It also dismisses
this identity's own earlier REQUEST_CHANGES, which a posted APPROVE would have
superseded. A withdrawal that fails, or a head/base/label change since the read,
downgrades the gate as on the posting path. REQUEST_CHANGES is unaffected: it
approves nothing.

Trust model: every signal here — findings, panel status, judge status — is model
output over the PR's own content, so a diff that prompt-injects the panel and
judge can steer the round to an approval. The recorded SHA is mutable too — a
review body can be edited by anyone with repo WRITE access (and by the approver
token itself), and `commit_id` cannot cross-check it, since GitHub has already
moved that field to the same head — so an EDITED approval is always treated as
stale, whatever its markers say. Treat the approval as an automated review
signal, not as a substitute for a human reviewer, and read the caller guide's
trust-model section before letting it satisfy a ruleset.

Only reviews authored by the approver login, and carrying ``APPROVE_MARKER`` or
EDITED, are ever dismissed, so a human's review — or another bot's — is never
touched.
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

# The panel matrix's review types (cursor-review.yml's `matrix.review_type`, pinned
# by test_auto_approve.py). With `--max-failed-reviewers` > 0, a round where one of
# these has no `ok` cell is still withheld: never approve with a whole review
# type missing.
PANEL_REVIEW_TYPES = ("adversarial", "edge-case")
# The one cell status meaning "ran but failed" (the workflow writes it for a cell
# that crashed, timed out, or never uploaded) — the only one that is tolerable.
TOLERABLE_CELL_STATUS = "error"

# The label the round cap applies and decide() refuses to approve over. Its
# removal (an `unlabeled` timeline event) is what resets the round count.
HUMAN_REVIEW_LABEL = "needs-human-review"
# The per-PR veto. cursor-review.yml's `gate` job and its `trigger` concurrency
# slot hard-code the same string, so a rename has to land in all three.
SKIP_REVIEW_LABEL = "skip-cursor-review"
# Labels under which `dismiss-stale` withdraws EVERY marked approval by the
# approver identity, whatever head it was pinned to: the gate runs no round on a
# vetoed PR, so nothing else would withdraw an approval already standing.
VETO_LABELS = (SKIP_REVIEW_LABEL,)
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


def _load_incremental_diff():
    """incremental-diff.py, for the same section parser its `check` uses."""
    return _load_sibling("incremental-diff.py", "incremental_diff")


# --- approve_scope (rounds 2+ gate on the delta since the last reviewed commit) ---
#
# On a round after the first, a finding above the threshold counts toward the
# gate under `delta` only when it is IN the verified incremental block (its path
# and line fall inside one of that block's new-side hunks), a LIVE REPEAT (its
# `repeat_of` names an earlier thread the ledger reads as unresolved), or SEVERE
# (High/Critical, or a severity nobody recognises) anywhere in the reviewed diff.
# Everything else is still posted, as a thread carrying NON_GATING_MARKER, but
# does not block — and that thread is never auto-resolved, so it stays for a human.
#
# The scope is resolved FAIL-CLOSED: anything short of a verified block and a
# readable ledger runs the round as `full`, exactly as before the input existed.
ALLOWED_SCOPES = ("delta", "full")
SCOPE_DELTA = "delta"
SCOPE_FULL = "full"
# diff-size's `incremental_state`: `none` (no usable last-reviewed SHA — round 1,
# or a re-run on the same head), `built` (verified; may be EMPTY on a pure
# rebase), `unavailable` (a later round whose block could not be built), and
# `discarded` (built, then thrown away by the subset fail-safe).
INCREMENTAL_NONE = "none"
INCREMENTAL_BUILT = "built"
SEVERE = ("critical", "high")
# Hard-coded by post-review.py into a non-gating thread's body, AFTER the model's
# text, which is stripped of this marker first — so a finding cannot exempt
# itself by echoing it. Read back by the open-thread check and auto-resolve.
NON_GATING_MARKER = "<!-- cursor-review-non-gating -->"
NON_GATING_NOTE = "_Outside this round's changes: not blocking auto-approve._"
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def validate_scope(value: str) -> str:
    scope = (value or SCOPE_DELTA).strip().lower()
    if scope not in ALLOWED_SCOPES:
        raise ValueError(f"approve_scope must be one of {', '.join(ALLOWED_SCOPES)}, got {value!r}")
    return scope


def hunk_ranges(patch_text: str) -> dict:
    """{path: [(first, last), ...]} of every new-side hunk in a unified diff."""
    inc = _load_incremental_diff()
    ranges = {}
    for header, lines in inc.split_sections(patch_text or ""):
        paths = inc.section_paths(header, lines)
        if not paths:
            continue
        for line in lines:
            m = HUNK_RE.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                if count > 0:
                    ranges.setdefault(paths[1], []).append((start, start + count - 1))
    return ranges


def live_thread_urls(ledger) -> frozenset:
    """Thread URLs of every earlier finding the ledger reads as still unresolved."""
    out = set()
    for entry in (ledger or {}).get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("anchored", True):
            continue
        thread = entry.get("thread") or {}
        url = entry.get("discussion_url")
        if isinstance(url, str) and url and not thread.get("resolved"):
            out.add(url.strip())
    return frozenset(out)


def resolve_scope(requested: str, incremental_state: str, incremental_text, ledger) -> dict:
    """The scope this round actually gates on. Pure; fails closed to `full`.

    Returns {"scope", "note", "ranges", "live"}: `note` says why a `delta`
    request ran as `full` (empty when it did not).
    """
    full = {"scope": SCOPE_FULL, "note": "", "ranges": {}, "live": frozenset()}
    if requested != SCOPE_DELTA:
        return full
    state = (incremental_state or "").strip().lower()
    if state == INCREMENTAL_NONE:
        return {**full, "note": "first round (no usable last-reviewed commit), so the whole diff counts"}
    if state != INCREMENTAL_BUILT:
        return {**full, "note": f"the incremental block is {state or 'unavailable'}, so the round fails closed to `full`"}
    if incremental_text is None:
        return {**full, "note": "the incremental block could not be read, so the round fails closed to `full`"}
    status = (ledger or {}).get("status") if isinstance(ledger, dict) else None
    if status not in ("ok", "empty"):
        return {**full, "note": f"the prior-review ledger is {status or 'unknown'}, so the round fails closed to `full`"}
    return {"scope": SCOPE_DELTA, "note": "", "ranges": hunk_ranges(incremental_text), "live": live_thread_urls(ledger)}


def gating_reason(finding, scope: dict):
    """Why `finding` counts toward the gate, or None when it does not. Pure."""
    if not isinstance(finding, dict):
        return "malformed finding"
    if (scope or {}).get("scope") != SCOPE_DELTA:
        return "full scope"
    sev = finding.get("severity")
    sev = sev.strip().lower() if isinstance(sev, str) else ""
    if sev not in SEVERITY_ORDER:
        return "unrecognised severity"
    if sev in SEVERE:
        return f"{sev} anywhere in the diff"
    repeat = finding.get("repeat_of")
    if isinstance(repeat, str) and repeat.strip() in scope.get("live", ()):
        return "re-raise of an unresolved earlier finding"
    line = finding.get("line")
    try:
        line = int(line)
    except (TypeError, ValueError):
        return "no usable line anchor"
    path = finding.get("file")
    for first, last in scope.get("ranges", {}).get(path, ()) if isinstance(path, str) else ():
        if first <= line <= last:
            return "inside this round's changes"
    return None


def non_gating(finding, threshold: str, scope: dict) -> bool:
    """True for an above-threshold finding the delta scope does not count."""
    return (isinstance(finding, dict) and above_threshold(finding.get("severity"), threshold)
            and gating_reason(finding, scope) is None)


def is_non_gating_thread(body: str) -> bool:
    """A thread post-review.py marked non-gating (the marker on its own line)."""
    return NON_GATING_MARKER in (body or "").splitlines()


def validate_threshold(value: str) -> str:
    threshold = (value or "").strip().lower()
    if threshold not in ALLOWED_THRESHOLDS:
        raise ValueError(
            f"approve_max_severity must be one of {', '.join(ALLOWED_THRESHOLDS)} "
            f"(or empty to disable), got {value!r}"
        )
    return threshold


def parse_max_failed_reviewers(value) -> int:
    """`approve_max_failed_reviewers` as a non-negative int; '' → 0 (the strict rule).

    A `type: number` input can render as `1` or `1.0`, so digits with an
    all-zero fraction are accepted; anything else (`1e2`, `1_0`, `+3`, `1.5`) is
    a ValueError.
    """
    raw = str(value if value is not None else "").strip() or "0"
    match = re.fullmatch(r"([0-9]+)(?:\.0+)?", raw)
    if not match:
        raise ValueError(f"approve_max_failed_reviewers must be a non-negative whole number, got {value!r}")
    return int(match.group(1))


def _label_part(value) -> str:
    """A panel model / review type for a posted note. The workflow writes these,
    not a model, but the note posts under the approver identity: echo only a
    plain token."""
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value) else "?"


def _cell_label(cell: dict) -> str:
    return f"{_label_part(cell.get('model'))}:{_label_part(cell.get('review_type'))}"


def panel_gate(panel: list, max_failed: int = 0):
    """(withhold_reason or None, tolerated_cells). Pure.

    max_failed=0 is the original rule exactly: any cell not `ok` withholds.
    Above 0, every review type must also keep a completed cell — checked even
    when no cell errored, so an all-`ok` panel missing a whole pass is withheld.
    """
    if not panel:
        return "no panel metadata", []
    bad = [c for c in panel if not isinstance(c, dict) or c.get("status") != "ok"]
    if not bad and max_failed <= 0:
        return None, []
    incomplete = f"{len(bad)}/{len(panel)} panel reviewers did not complete"
    tolerable = [c for c in bad if isinstance(c, dict) and c.get("status") == TOLERABLE_CELL_STATUS]
    if len(tolerable) != len(bad) or len(bad) > max_failed:
        return incomplete, []
    # Every review type the matrix runs — and any other the panel names — must
    # keep at least one completed cell.
    types = list(PANEL_REVIEW_TYPES) + sorted(
        {str(c.get("review_type")) for c in panel} - set(PANEL_REVIEW_TYPES)
    )
    ok_types = {str(c.get("review_type")) for c in panel if isinstance(c, dict) and c.get("status") == "ok"}
    missing = [_label_part(t) for t in types if t not in ok_types]
    if missing:
        return f"{incomplete}, leaving no completed {', '.join(f'`{t}`' for t in missing)} review", []
    return None, bad


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
    scope=None,
    max_failed_reviewers: int = 0,
):
    """Return (event, reasons, blocking_findings). Pure; no I/O.

    Trust checks come first and gate BOTH review events: a round that cannot be
    trusted neither approves nor requests changes. Its findings are still on the
    PR as threads; only the review event is withheld.
    """
    event, _, reasons, blocking = decide_gate(
        threshold, findings, panel, judge_status, delivered, reviewed_sha,
        live_head_sha, open_thread_severities, ungated, human_review,
        reviewed_diff_empty, reviewed_base, live_base, scope, max_failed_reviewers,
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
    scope=None,
    max_failed_reviewers: int = 0,
):
    """decide(), plus the approve_gate value: (event, gate, reasons, blocking).

    `scope` is resolve_scope()'s result; None (every existing caller) is `full`.
    Under `delta`, an above-threshold finding blocks only if gating_reason()
    gives one, and an open thread post-review.py marked non-gating does not
    block either. A `full` round counts both, exactly as before.
    """
    noted = scope is not None
    scope = scope or {"scope": SCOPE_FULL}
    if human_review:
        # Before the trust checks: a PR handed to a human is never approved,
        # whatever this round found. NONE also withdraws an earlier approval.
        return NONE, GATE_CAPPED, [f"the PR carries `{HUMAN_REVIEW_LABEL}`"], []
    reasons = []
    if judge_status != "ok":
        reasons.append(f"the judge did not adjudicate this round (status={judge_status or 'missing'})")
    panel_reason, tolerated = panel_gate(panel, max_failed_reviewers)
    if panel_reason:
        reasons.append(panel_reason)
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

    event, gate, reasons, blocking = _trusted_decision(threshold, findings, open_thread_severities, scope, noted)
    if tolerated:
        verb = "approved " if event == APPROVE else ""
        reasons[0] += (f" ({verb}with {len(tolerated)}/{len(panel)} reviewers errored: "
                       f"{', '.join(_cell_label(c) for c in tolerated)})")
    return event, gate, reasons, blocking


def _trusted_decision(threshold: str, findings: list, open_thread_severities: list, scope: dict, noted: bool):
    """decide_gate() past the trust checks: (event, gate, reasons, blocking)."""
    above = [
        f for f in findings if not isinstance(f, dict) or above_threshold(f.get("severity"), threshold)
    ]
    blocking = [f for f in above if gating_reason(f, scope) is not None]
    # The note rides as a second reason only for a caller that resolved a scope,
    # so reasons[0] — what every review body and log line leads with — is unchanged.
    note = [scope_note(scope, len(blocking), len(above) - len(blocking), blocking)] if noted else []
    if blocking:
        return REQUEST_CHANGES, GATE_FAIL, [f"{len(blocking)} finding(s) above `{threshold}`", *note], blocking

    # Same line as this round's findings, so a Medium thread from an earlier round
    # blocks a `low` threshold exactly as a Medium finding this round would.
    # Entries are a severity (legacy callers) or (severity, non_gating).
    delta = scope.get("scope") == SCOPE_DELTA
    open_blocking = []
    for entry in open_thread_severities:
        sev, marked = entry if isinstance(entry, tuple) else (entry, False)
        if (sev is None or above_threshold(sev, threshold)) and not (delta and marked):
            open_blocking.append(sev)
    if open_blocking:
        return NONE, GATE_FAIL, [f"{len(open_blocking)} open thread(s) above `{threshold}` (or unbadged) from an earlier round", *note], []
    if above:
        return APPROVE, GATE_PASS, [f"every finding inside this round's changes is at or below `{threshold}`", *note], []
    return APPROVE, GATE_PASS, [f"every finding is at or below `{threshold}`", *note], []


def scope_note(scope: dict, gating: int, non_gating_count: int, blocking: list) -> str:
    """The decision note's scope line: which scope, how many gated, and why."""
    scope = scope or {}
    text = f"approve_scope `{scope.get('scope') or SCOPE_FULL}`: {gating} gating, {non_gating_count} non-gating finding(s) above the threshold"
    if scope.get("note"):
        text += f" ({scope['note']})"
    if scope.get("scope") == SCOPE_DELTA and blocking:
        why = {}
        for f in blocking:
            reason = gating_reason(f, scope)
            why[reason] = why.get(reason, 0) + 1
        text += "; gating because: " + ", ".join(f"{n}× {r}" for r, n in why.items())
    return text


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
    """(login, id, marked) of every live auto-approve APPROVAL that is stale.

    Stale = not on `head_sha` (`None` selects every one, wherever pinned), or
    recorded against a base other than `live_base` (`None` skips that check), or
    edited. An EDITED approval counts even with the marker gone (`marked` False):
    deleting the marker is the cheaper forgery of the two, and without this it
    would take the approval out of every filter here for good.
    """
    out = []
    for r in reviews:
        if r.get("state") != "APPROVED":
            continue
        body = r.get("body") or ""
        marked = APPROVE_MARKER in body
        if not marked and not r.get("edited"):
            continue
        # The SHA recorded in the body, NOT `commit_id` (see REVIEWED_SHA_RE). A
        # marked approval without a recorded SHA predates that record: treat it
        # as stale rather than trust a commit_id GitHub may have moved.
        match = REVIEWED_SHA_RE.search(body)
        # An EDITED body is not evidence either. The recorded SHA (and base) is
        # the only staleness anchor an approval has, and a user with write access
        # can edit this identity's review and rewrite those markers to the live
        # head and base — which would keep a stale approval standing through
        # every later push. Nothing in this workflow edits its own review (the
        # only write to one is the dismissal PUT), so an edit is always someone
        # else's and the markers it carries cannot be trusted to say what was
        # reviewed.
        off_head = (head_sha is None or not match or bool(r.get("edited"))
                    or match.group(1) != head_sha.lower())
        base = recorded_base(body)
        off_base = live_base is not None and base is not None and base != live_base
        if off_head or off_base:
            login = (r.get("user") or {}).get("login")
            out.append((login if isinstance(login, str) else "", r["id"], marked))
    return out


def stale_reviews_to_dismiss(reviews: list, approver_login: str, head_sha, live_base=None) -> list:
    """Ids of this identity's stale auto-approve APPROVALS (see _stale_approvals).

    That includes an edited approval by this identity whose marker was removed.
    It cannot be told apart from a manual approval the identity later edited, so
    an APPROVER_TOKEN that is a human account loses that one too; an unedited
    unmarked approval is still never touched.
    """
    return [rid for login, rid, _ in _stale_approvals(reviews, head_sha, live_base)
            if login.lower() == approver_login.lower()]


def unactionable_stale_approvals(reviews: list, approver_login: str, head_sha, live_base=None) -> list:
    """`login:id` of stale marked APPROVALS by any login OTHER than the approver.

    This run cannot dismiss them, so it must not report clean over them. An
    empty `approver_login` (the approver's secrets are not in this run) puts
    every stale marked approval here. An unmarked one is not: by another login
    it is a human's own approval that they edited, not an auto-approval.
    """
    return [f"{login or '?'}:{rid}" for login, rid, marked in _stale_approvals(reviews, head_sha, live_base)
            if marked and (not approver_login or login.lower() != approver_login.lower())]


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
    """(severity, non_gating) of every open (unresolved, non-outdated) cursor-review thread.

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
        body = first.get("body") or ""
        out.append((thread_severity(body), is_non_gating_thread(body)))
    return out


# Mirrors gate-unresolved.AUTO_RESOLVE_MARKER, which build-ledger.py reads to keep
# this reply out of its answer count (a test pins the two equal).
AUTO_RESOLVE_MARKER = "<!-- cursor-review-auto-resolve -->"
RESOLVED = "resolved"
SKIP_HUMAN = "skipped-human"
SKIP_UNBADGED = "skipped-unbadged"
SKIP_ABOVE = "skipped-above-threshold"
NOT_OURS = "not-ours"  # resolved already, or not a thread the poster started

REPLY_MUTATION = """
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id }
  }
}
"""
RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { isResolved } }
}
"""
# One thread's live state, re-read just before it is resolved: the plan was
# built from a snapshot taken a whole page-walk (and every earlier thread's
# mutations) ago, and a human reply in that window must still stop it.
THREAD_RECHECK_QUERY = """
query($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      isResolved
      participants: comments(first: 100) {
        totalCount
        nodes { author { __typename login } }
      }
    }
  }
}
"""
# A bound on one round's writes, like every other list here: resolveReviewThread
# and the reply are content-creating calls under GitHub's secondary rate limit,
# and the job's timeout-minutes covers the review post too. The rest wait for
# the next approving round.
MAX_AUTO_RESOLVE = 30
# Consecutive failures that end the loop: past this it is a missing permission
# or a rate limit, and every further call only extends the block.
MAX_CONSECUTIVE_FAILURES = 3


def author_login(node) -> str:
    """REST-shaped login of a GraphQL comment author: a Bot gets its `[bot]`
    suffix back (GraphQL drops it), so it compares equal to the login the
    workflow passes in and never to a user account of the same name."""
    author = (node or {}).get("author") or {}
    login = author.get("login") or ""
    if author.get("__typename") == "Bot" and login and not login.endswith("[bot]"):
        login += "[bot]"
    return login


def classify_thread(thread: dict, poster_login: str, threshold: str, gate=None):
    """(verdict, severity) for one review thread under the auto-resolve rule.

    RESOLVED only when ALL hold: unresolved; a cursor-review consolidated
    thread whose first comment `poster_login` wrote; that comment carries a
    badge at or below `threshold`; and no other account commented in it. The
    poster is passed in, never read from thread content.
    """
    gate = gate or _load_gate_unresolved()
    if thread.get("isResolved") or not gate.is_cursor_thread(thread):
        return NOT_OURS, None
    first = ((thread.get("comments") or {}).get("nodes") or [{}])[0]
    if not poster_login or author_login(first).lower() != poster_login.lower():
        return NOT_OURS, None
    severity = thread_severity(first.get("body") or "")
    if severity is None:
        return SKIP_UNBADGED, None
    if above_threshold(severity, threshold):
        return SKIP_ABOVE, severity
    if not only_poster_spoke(thread, poster_login):
        return SKIP_HUMAN, severity
    return RESOLVED, severity


def only_poster_spoke(thread: dict, poster_login: str) -> bool:
    """True when every comment in `thread` is visibly `poster_login`'s.

    Every comment must be visible to prove nobody else spoke: a missing page, a
    short page or a deleted (null) author all leave the thread for a person.
    """
    participants = (thread or {}).get("participants") or {}
    nodes = participants.get("nodes") or []
    return bool(poster_login) and bool(nodes) and participants.get("totalCount") == len(nodes) and all(
        author_login(n).lower() == poster_login.lower() for n in nodes
    )


def plan_thread_resolution(threads: list, poster_login: str, threshold: str):
    """([(thread, severity)] to resolve, {verdict: count}). Pure.

    All-or-nothing on the PR's state: if ANY live (unresolved, non-outdated)
    cursor-review thread is above the threshold or unbadged, the PR is not
    approvable and nothing is resolved — not even the eligible ones. decide()
    already refuses to APPROVE in that state; this re-checks it against the
    threads read after the approval, for any caller that reaches here.
    """
    gate = _load_gate_unresolved()
    counts = {RESOLVED: 0, SKIP_HUMAN: 0, SKIP_UNBADGED: 0, SKIP_ABOVE: 0}
    blocked = False
    plan = []
    for thread in threads:
        if gate.is_cursor_thread(thread) and not thread.get("isResolved") and not thread.get("isOutdated"):
            first = ((thread.get("comments") or {}).get("nodes") or [{}])[0]
            body = first.get("body") or ""
            live = thread_severity(body)
            # A thread a delta-scoped round marked non-gating did not hold the
            # approval back, so it does not hold the others' resolution back
            # either. It is never resolved itself (SKIP_ABOVE below).
            if (live is None or above_threshold(live, threshold)) and not is_non_gating_thread(body):
                blocked = True
        verdict, severity = classify_thread(thread, poster_login, threshold, gate)
        if verdict == NOT_OURS:
            continue
        counts[verdict] += 1
        if verdict == RESOLVED:
            plan.append((thread, severity))
    if blocked:
        counts[SKIP_ABOVE] += len(plan)
        counts[RESOLVED] = 0
        return [], counts
    return plan, counts


def resolve_reply(severity: str, threshold: str, commit_sha: str) -> str:
    return (f"Resolved by auto-approve: {severity.capitalize()} finding, at or below the "
            f"`{threshold}` threshold, on commit {(commit_sha or '')[:7]}.\n\n{AUTO_RESOLVE_MARKER}")


def _graphql(query: str, **variables) -> dict:
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-f", f"{key}={value}"]
    data = json.loads(gh(args))
    if not isinstance(data, dict):
        raise RuntimeError(f"GraphQL returned a non-object body: {type(data).__name__}")
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data


def still_eligible(thread_id: str, poster_login: str) -> bool:
    """Re-read one thread right before resolving it: still unresolved, and
    still nobody but the poster has spoken in it. Raises on a failed read."""
    node = (_graphql(THREAD_RECHECK_QUERY, threadId=thread_id).get("data") or {}).get("node")
    if not isinstance(node, dict):
        raise RuntimeError("thread re-read returned no node")
    return not node.get("isResolved") and only_poster_spoke(node, poster_login)


def resolve_eligible_threads(repo: str, pr, poster_login: str, threshold: str, commit_sha: str) -> dict:
    """Reply to and resolve the poster's own at-or-below-threshold threads.

    Call ONLY after an APPROVE has been posted and the head re-checked. Every
    failure is logged and skipped: nothing here is fatal, and nothing here undoes
    the approval. Returns the counts it logged (plus ``failed``).
    """
    counts = {RESOLVED: 0, SKIP_HUMAN: 0, SKIP_UNBADGED: 0, SKIP_ABOVE: 0, "failed": 0, "deferred": 0}
    if not poster_login:
        emit("Auto-resolve: skipped — no cursor-review poster login was passed.")
        return counts
    owner, _, name = repo.partition("/")
    try:
        gate = _load_gate_unresolved()
        threads = list(gate.iter_threads(owner, name, int(pr)))
        plan, planned = plan_thread_resolution(threads, poster_login, threshold)
    except (Exception, SystemExit) as e:  # noqa: BLE001 - never fatal: the APPROVE has landed
        print(f"::warning::Auto-resolve: could not read this PR's review threads, so none were resolved: {e or 'thread query failed'}")
        return counts
    counts.update(planned)
    counts[RESOLVED] = 0
    if len(plan) > MAX_AUTO_RESOLVE:
        counts["deferred"] = len(plan) - MAX_AUTO_RESOLVE
        plan = plan[:MAX_AUTO_RESOLVE]
    streak = 0
    for index, (thread, severity) in enumerate(plan):
        if streak >= MAX_CONSECUTIVE_FAILURES:
            counts["deferred"] += len(plan) - index
            print(f"::warning::Auto-resolve: stopped after {streak} consecutive failures; "
                  f"{len(plan) - index} eligible thread(s) left for the next approving round.")
            break
        thread_id = thread.get("id") or ""
        try:
            if not thread_id:
                raise RuntimeError("thread has no node id")
            if not still_eligible(thread_id, poster_login):
                counts[SKIP_HUMAN] += 1
                streak = 0
                continue
            # Resolve FIRST, reply second. The reply is from the approver
            # identity, so a reply left on a thread whose resolve then failed
            # would make it SKIP_HUMAN forever (or, approver == poster, be
            # duplicated every round). A resolved thread is never re-planned,
            # so a failed reply costs only the explanation.
            _graphql(RESOLVE_MUTATION, threadId=thread_id)
        except Exception as e:  # noqa: BLE001 - never fatal: the APPROVE has landed
            counts["failed"] += 1
            streak += 1
            print(f"::warning::Auto-resolve: could not resolve thread {thread_id or '?'}: {e}")
            continue
        streak = 0
        counts[RESOLVED] += 1
        try:
            _graphql(REPLY_MUTATION, threadId=thread_id, body=resolve_reply(severity, threshold, commit_sha))
        except Exception as e:  # noqa: BLE001 - the thread is already resolved
            print(f"::warning::Auto-resolve: resolved thread {thread_id} but could not reply on it: {e}")
    emit(f"Auto-resolve: resolved {counts[RESOLVED]}, skipped-human {counts[SKIP_HUMAN]}, "
         f"skipped-unbadged {counts[SKIP_UNBADGED]}, skipped-above-threshold {counts[SKIP_ABOVE]}, "
         f"failed {counts['failed']}, deferred {counts['deferred']}.")
    return counts


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
    if len(reasons) > 1:
        lines.append(f"\n_Scope: {reasons[-1]}._")
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
    if (getattr(args, "author_enabled", "") or "").strip().lower() == "false":
        # approve_authors (resolved in the workflow's `gate` job) does not list
        # this PR's author: auto-approve is off for the PR, exactly as if
        # approve_max_severity were empty. No review event, no withdrawal —
        # dismiss-stale still withdraws a stale marked approval on its own.
        set_output("approve_gate", GATE_OFF)
        login = re.sub(r"[^A-Za-z0-9\-\[\]]", "", getattr(args, "pr_author", "") or "") or "?"
        emit(f"ℹ️ **Auto-approve: off** — auto-approve not enabled for author {login} (not in `approve_authors`).")
        return 0
    try:
        max_failed = parse_max_failed_reviewers(getattr(args, "max_failed_reviewers", ""))
    except ValueError as e:
        print(f"::error::{e}")
        # The caller most likely edited this input mid-PR: fail closed, so an
        # earlier round's approval does not keep satisfying branch protection.
        withdraw_own_approvals(args)
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
        requested_scope = validate_scope(getattr(args, "approve_scope", "") or "")
    except ValueError as e:
        print(f"::error::{e}")
        return 2
    scope = resolve_scope(requested_scope, getattr(args, "incremental_state", "") or "",
                          _read_optional(getattr(args, "incremental", "") or ""),
                          _read_json_optional(getattr(args, "ledger", "") or ""))

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
            scope,
            max_failed,
        )
    set_output("approve_gate", gate)
    if len(reasons) > 1:
        emit(f"ℹ️ **Auto-approve scope** — {reasons[-1]}.")
    if event == NONE:
        emit(f"ℹ️ **Auto-approve: no decision** — {'; '.join(reasons)}.")
        return withdraw_own_approvals(args)
    if event == APPROVE and (getattr(args, "defer_approval", "") or "").strip().lower() == "true":
        return defer_to_cursor_approve(args, reasons[0])

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
        # The id is what the race check below dismisses by. A `gh` that exits 0
        # with an unparseable body, `null`, or an object without an id would
        # otherwise raise past this `except` AFTER the review may have landed —
        # the job green on a `continue-on-error` step, nothing withdrawn. Treat
        # it as the ambiguous POST it is (JSONDecodeError is a ValueError).
        if not isinstance(posted, dict) or posted.get("id") is None:
            raise ValueError(f"the review POST returned no review id ({type(posted).__name__})")
    except (RuntimeError, ValueError) as e:
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
        print(f"::error::Could not submit the {event} review: {annotation_cause(e, 'unknown error')}")
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
            print(f"::error::{why[0].upper()}{why[1:]} while the {event} review was posted, and withdrawing it failed: {annotation_cause(e, 'unknown error')}. {DISMISS_PERMISSION_HINT}")
            return 1
        emit(f"ℹ️ **Auto-approve: withdrawn** — {why} while the {event} review was being posted.")
        return 0
    emit(f"{'✅' if event == APPROVE else '❌'} **Auto-approve: {event}** — {reasons[0]}.")
    if event == APPROVE:
        # Only here: the APPROVE is posted AND the head/base re-check passed.
        # Never fatal, never undoes the approval (see resolve_eligible_threads).
        resolve_eligible_threads(args.repo, args.pr_number, getattr(args, "poster_login", "") or "",
                                 threshold, args.commit_sha)
    return 0


def _read_optional(path: str):
    """A text file's contents, or None when no path was given or it cannot be read."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
            return f.read()
    except OSError:
        return None


def _read_json_optional(path: str):
    """A JSON object from `path`, or None (missing, unreadable, or not an object)."""
    text = _read_optional(path)
    try:
        data = json.loads(text) if text is not None else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def defer_to_cursor_approve(args, reason: str) -> int:
    """`--defer-approval true` on an APPROVE outcome: post nothing, keep the gate.

    The gate stays `pass` for cursor-approve, whose decide approves (and resolves
    threads) only once its axes agree — unless one of the steps below says this
    round can no longer stand behind it, exactly as the posting path would.
    """
    emit(f"ℹ️ **Auto-approve: deferred** — {reason}; approval is left to cursor-approve.")
    # Also clears an approval this identity posted before defer was switched on.
    # One that cannot be withdrawn keeps satisfying branch protection while the
    # axes judge, so the gate must not read `pass` over it (same as a failed POST).
    rc = withdraw_own_approvals(args, DEFERRED_MESSAGE)
    if rc:
        set_output("approve_gate", GATE_UNTRUSTED)
    # Posting path: the APPROVE event supersedes this identity's earlier
    # REQUEST_CHANGES. Nothing is posted here, so dismiss those instead — else a
    # fixed High keeps vetoing the merge until cursor-approve approves, which it
    # never does on a red axis. Failing to is red but withholds nothing.
    if dismiss_own_change_requests(args):
        rc = 1
    # Same post-decision re-check the posting path runs: a push, retarget or
    # `needs-human-review` label landing since the read must not leave `pass`.
    try:
        pr_now = read_pr(args.repo, args.pr_number)
        head_now, base_now = pr_head_base(pr_now)
        labelled_now = has_label(pr_now, HUMAN_REVIEW_LABEL)
    except (RuntimeError, ValueError):
        head_now = base_now = None  # unknown → treat as moved
        labelled_now = False
    if head_now != args.commit_sha or base_now != args.base_ref:
        set_output("approve_gate", GATE_UNTRUSTED)
        emit("ℹ️ **Auto-approve: deferred round superseded** — the PR head or base moved.")
    elif labelled_now:
        set_output("approve_gate", GATE_CAPPED)
        emit(f"ℹ️ **Auto-approve: deferred round superseded** — the PR was labelled `{HUMAN_REVIEW_LABEL}`.")
    return rc


def dismiss_own_change_requests(args) -> int:
    """Dismiss this identity's own unedited, marked REQUEST_CHANGES reviews.
    An edited one is someone else's words (see _stale_approvals) and stays."""
    login = (args.approver_login or "").strip().lower()
    if not login:
        return 0
    try:
        ids = [r["id"] for r in list_reviews(args.repo, args.pr_number)
               if r.get("state") == "CHANGES_REQUESTED" and not r.get("edited")
               and APPROVE_MARKER in (r.get("body") or "")
               and (r.get("user") or {}).get("login", "").lower() == login]
    except (RuntimeError, ValueError) as e:
        print(f"::warning::Could not list reviews to withdraw an earlier request for changes: {annotation_cause(e, 'unknown error')}")
        return 1
    failed = []
    for rid in ids:
        try:
            dismiss(args.repo, args.pr_number, rid, PASSED_MESSAGE)
        except RuntimeError as e:
            failed.append(f"{rid}: {annotation_cause(e, 'unknown error')}")
    if ids:
        emit(f"Auto-approve: withdrew {len(ids) - len(failed)}/{len(ids)} earlier request(s) for changes by {args.approver_login}.")
    if failed:
        print(f"::warning::Could not withdraw {len(failed)} earlier request(s) for changes ({'; '.join(failed)}). {DISMISS_PERMISSION_HINT}")
        return 1
    return 0


DISMISS_PERMISSION_HINT = (
    "Dismissing a review needs an identity allowed to dismiss reviews on this branch: "
    "pull-requests: write is not enough when branch protection restricts dismissals "
    "(add the approver to the allowed dismissers, or lift the restriction)."
)


def _str_field(pr: dict, section: str, key: str) -> str:
    """``pr[section][key]`` when it is a string, else "".

    The top level is an object by the time this is called, but the NESTED shapes
    are still whatever the payload said. A truthy non-mapping `head`/`base` (a
    string, a number, a list) makes `.get` raise AttributeError, and a non-string
    `sha` survives an emptiness check only to reach `.lower()` in
    `_stale_approvals` later. Both are outside every caller's
    `except (RuntimeError, ValueError)`, so both escape as a traceback and bypass
    the announced degradation this read exists to feed. Coerce instead: "" is the
    shapeless value those fallbacks already handle and announce.
    """
    section_value = pr.get(section)
    if not isinstance(section_value, dict):
        return ""
    value = section_value.get(key)
    return value if isinstance(value, str) else ""


def read_pr(repo: str, pr_number) -> dict:
    """The PR as it is now. One read serves head, base AND labels."""
    pr = json.loads(gh(["api", f"repos/{repo}/pulls/{pr_number}"]))
    if not isinstance(pr, dict):
        # Valid JSON that is not an object: `null`, or the ARRAY the collection
        # endpoint returns when the number is empty (argparse's `required=True`
        # accepts `--pr-number ""`). Every consumer below calls `.get` on this,
        # which raises AttributeError — outside every caller's
        # `except (RuntimeError, ValueError)`, so it escapes as a traceback and
        # bypasses the announced degradation that exists for exactly this.
        raise ValueError(f"expected a PR object, got {type(pr).__name__}")
    return pr


def pr_head_base(pr: dict) -> tuple:
    """(head sha, base ref) of a PR payload — "" for either when it is shapeless."""
    return _str_field(pr, "head", "sha"), _str_field(pr, "base", "ref")


def live_labels_readable(pr: dict) -> bool:
    """Whether the PR payload carries a label list `has_label` can fully read.
    `has_label` reads a missing or shapeless `labels` — or skips an entry that is
    not a dict with a string `name` — as "no such label", which fails OPEN for a
    veto label; a caller that must fail closed checks this first."""
    labels = pr.get("labels")
    return isinstance(labels, list) and all(
        isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels
    )


def has_label(pr: dict, name: str) -> bool:
    # A truthy non-list `labels` (a number, `true`) is not iterable, and the
    # TypeError would escape every caller's `except (RuntimeError, ValueError)`.
    labels = pr.get("labels")
    return any(
        isinstance(label, dict) and isinstance(label.get("name"), str)
        and label["name"].lower() == name.lower()
        for label in (labels if isinstance(labels, list) else [])
    )


# The reviews list, read through GraphQL because REST has no field for the one
# thing the staleness decision now turns on: whether a review's body was EDITED
# after it was posted.
REVIEWS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviews(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          # Dismissal is a REST call, so this list carries the REST id.
          # `fullDatabaseId` first for the same reason gate-unresolved.py reads
          # it: live review ids are already past 2^31-1, which `databaseId` is
          # typed for (`Int`). `databaseId` stays as the fallback.
          fullDatabaseId
          databaseId
          state
          body
          # The whole reason this is not REST: null until someone edits the
          # review.
          lastEditedAt
          # The round cap reads these two: `round_reviews` orders and windows
          # the count on `submittedAt`, and the cap comment links the latest
          # round by `url`. REST names them `submitted_at` and `html_url`.
          submittedAt
          url
          # GraphQL reports a Bot's login WITHOUT the `[bot]` suffix that REST
          # reports and that the workflow passes as --approver-login. The suffix
          # is restored in `_review_node`; without it the identity comparison in
          # `stale_reviews_to_dismiss` matches nothing, no stale approval is
          # ever dismissed, and the job still exits green.
          author { __typename login }
        }
      }
    }
  }
}
"""


def _review_node(node: dict) -> dict:
    """One GraphQL review in the REST shape this module's readers expect.

    Every field is coerced to the type its readers assume, or raises ValueError:
    `_stale_approvals` runs outside its callers' `try`, so a wrong-shaped field
    there would escape as a traceback instead of reaching the error path.
    """
    if not isinstance(node, dict):
        # Not dropped: both callers read a missing review as "nothing to dismiss"
        # and exit green, and the dropped one may be the newest approval.
        raise ValueError(f"a review in the GraphQL response is not an object ({type(node).__name__})")
    author = node.get("author")
    author = author if isinstance(author, dict) else {}
    login = author.get("login")
    login = login if isinstance(login, str) else ""
    if login and author.get("__typename") == "Bot":
        login = f"{login}[bot]"
    rid = node.get("fullDatabaseId")
    if rid is None:
        rid = node.get("databaseId")
    if rid is None:
        # Raise here rather than carry None into a dismissal URL, where it would
        # surface as a misleading 404 "could not dismiss".
        raise ValueError("a review in the GraphQL response has no database id")
    if isinstance(rid, bool) or not isinstance(rid, (int, str)):
        raise ValueError(f"a review in the GraphQL response has a non-numeric id ({type(rid).__name__})")
    state, body = node.get("state"), node.get("body")
    return {
        "id": int(rid),
        "user": {"login": login},
        "state": state if isinstance(state, str) else "",
        "body": body if isinstance(body, str) else "",
        "edited": node.get("lastEditedAt") is not None,
        "submitted_at": node.get("submittedAt"),
        "html_url": node.get("url") or "",
    }


def list_reviews(repo: str, pr_number) -> list:
    """Every review on the PR, REST-shaped, plus `edited`.

    A missing `pullRequest` or `reviews` connection, a page claiming more with
    no new cursor to follow, and a review with no id all raise rather than return
    a short list: both callers treat a missing review as "nothing to dismiss" and
    exit green, and reviews come back oldest-first, so a truncated read drops
    exactly the newest approval.
    """
    owner, _, name = repo.partition("/")
    out = []
    cursor = None
    while True:
        args = [
            "api", "graphql",
            "-f", f"query={REVIEWS_QUERY}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-F", f"pr={int(pr_number)}",
        ]
        # Same -F/-f split as gate-unresolved.py: the first page needs a JSON
        # null, later pages an opaque string cursor.
        args += ["-F", "cursor=null"] if cursor is None else ["-f", f"cursor={cursor}"]
        body = json.loads(gh(args))
        # Valid JSON that is not an object (`null`, an array, a string) cannot
        # answer `.get`, and the AttributeError would escape every caller's
        # `except (RuntimeError, ValueError)` as a traceback — same contract as
        # read_pr's top-level guard. Each nested level is checked for the same
        # reason before it is indexed.
        data = body.get("data") if isinstance(body, dict) else None
        repository = data.get("repository") if isinstance(data, dict) else None
        pr = repository.get("pullRequest") if isinstance(repository, dict) else None
        if not isinstance(pr, dict):
            raise ValueError(f"no pullRequest in the GraphQL reviews response for {repo}#{pr_number}")
        reviews = pr.get("reviews")
        page = reviews.get("pageInfo") if isinstance(reviews, dict) else None
        if not isinstance(page, dict):
            raise ValueError(f"no reviews connection in the GraphQL response for {repo}#{pr_number}")
        nodes = reviews.get("nodes") or []
        if not isinstance(nodes, list):
            raise ValueError(f"the GraphQL reviews of {repo}#{pr_number} are not a list ({type(nodes).__name__})")
        out += [_review_node(n) for n in nodes]
        if not page.get("hasNextPage"):
            return out
        nxt = page.get("endCursor")
        # No cursor, or the same one again: following it would loop on one page
        # until the job times out; stopping would return a truncated list.
        if not nxt or nxt == cursor:
            raise ValueError(f"the GraphQL reviews of {repo}#{pr_number} report another page with no new cursor")
        cursor = nxt


def withdraw_own_approvals(args, message: str = "", head_sha=None, live_base=None) -> int:
    """Dismiss every marked (or edited) approval by this identity — or, given
    `head_sha`, only those stale against it and `live_base` (see
    _stale_approvals). Red if any could not be."""
    try:
        ids = stale_reviews_to_dismiss(list_reviews(args.repo, args.pr_number), args.approver_login, head_sha, live_base)
    except (RuntimeError, ValueError) as e:
        print(f"::error::Could not list reviews to withdraw an earlier auto-approval: {annotation_cause(e, 'unknown error')}")
        return 1
    failed = []
    for rid in ids:
        try:
            dismiss(args.repo, args.pr_number, rid, message or UNTRUSTED_MESSAGE)
        except RuntimeError as e:
            # Collapsed per id: these are joined into an ::error:: annotation, and
            # each carries `gh`'s multi-line stderr.
            failed.append(f"{rid}: {annotation_cause(e, 'unknown error')}")
    if ids:
        emit(f"Auto-approve: withdrew {len(ids) - len(failed)}/{len(ids)} earlier approval(s) by {args.approver_login}.")
    if failed:
        print(f"::error::Could not withdraw {len(failed)} earlier auto-approval(s) ({'; '.join(failed)}). {DISMISS_PERMISSION_HINT}")
        return 1
    return 0


STALE_MESSAGE = "New commits pushed — cursor-review auto-approve withdrawn until the next review round."
BASE_CHANGED_MESSAGE = "The base branch changed — cursor-review auto-approve withdrawn until the next review round."
UNTRUSTED_MESSAGE = "The latest cursor-review round could not be trusted to approve — auto-approve withdrawn until a round that can."
DEFERRED_MESSAGE = "cursor-review defers approval to cursor-approve — auto-approve withdrawn until its axes agree."
PASSED_MESSAGE = "The latest cursor-review round passed its severity threshold — request for changes withdrawn; approval is left to cursor-approve."
HUMAN_REVIEW_MESSAGE = f"The PR was labelled `{HUMAN_REVIEW_LABEL}` — cursor-review auto-approve withdrawn."
SKIP_REVIEW_WITHDRAWN_MESSAGE = f"The PR was labelled `{SKIP_REVIEW_LABEL}` — cursor-review auto-approve withdrawn."


def dismiss(repo: str, pr_number, review_id, message: str = STALE_MESSAGE) -> None:
    gh(
        ["api", "-X", "PUT", f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/dismissals", "--input", "-"],
        {"message": message, "event": "DISMISS"},
    )


ANNOTATION_CAUSE_MAX = 500


def annotation_cause(error, fallback: str) -> str:
    """One single-line cause for a ``::warning::``/``::error::`` annotation.

    Workflow commands are line-oriented, and a `gh` failure carries its captured
    stderr — usually several lines (an HTTP status plus a docs URL). Interpolated
    raw, everything after the first newline falls out of the annotation into plain
    log output, and a continuation line beginning with ``::`` is re-parsed as a
    new workflow command — truncating the very diagnostic this emits. Collapse
    the whitespace. The type is named because ``str(RuntimeError())`` is empty.
    """
    if not error:
        return fallback
    # Escape `%` FIRST — the standard `escapeData` ordering. The runner
    # percent-decodes `%0A`/`%0D` when it RENDERS an annotation, so an encoded
    # sequence in captured stderr (common in URLs echoed by API errors) would
    # reintroduce the very line break collapsing just removed. Display-only:
    # command parsing is per-line and happens before decoding.
    text = f"{type(error).__name__}: {error}".replace("%", "%25")
    text = " ".join(text.split()).rstrip(":")
    # Bounded: `gh` captures stderr uncapped (a proxy's HTML error page is
    # kilobytes), and the failure lists join one cause per review id before
    # DISMISS_PERMISSION_HINT — which an over-long annotation would push out of
    # what the renderer shows.
    return text if len(text) <= ANNOTATION_CAUSE_MAX else text[:ANNOTATION_CAUSE_MAX - 1] + "…"


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
    #
    # Both degradations below are ANNOUNCED. They are the paths on which this job
    # can still withdraw an approval that is valid for the PR's live state, and it
    # exits green when it does, so a reader of a green run has no other way to
    # learn that the comparison was not against the real head and base.
    try:
        pr = read_pr(args.repo, args.pr_number)
        live_head, live_base = pr_head_base(pr)
    except (RuntimeError, ValueError) as e:
        live_head, live_base, read_error = "", None, e
    else:
        read_error = None
    # A veto label withdraws EVERY marked approval, wherever pinned (the
    # `--all-approvals` selection): the gate runs no round on a vetoed PR, so an
    # approval standing when the label lands would otherwise keep satisfying
    # branch protection until the next push. A failed read checks nothing — the
    # head/base pass below still runs and the next event redoes this. So does a
    # malformed `labels`: this job runs on every event, so neither goes red, and
    # neither mass-withdraws on a payload it cannot read.
    vetoed = None
    if read_error is None:
        if live_labels_readable(pr):
            vetoed = next((label for label in VETO_LABELS if has_label(pr, label)), None)
        else:
            print(f"::warning::The live labels of {args.repo}#{args.pr_number} are not a list of named labels — "
                  f"could not check for {', '.join(f'`{label}`' for label in VETO_LABELS)} this run; judging "
                  "staleness by head and base only. The next event redoes the check.")
    withdraw_all = args.all_approvals or vetoed is not None
    why_all = f"the PR carries `{vetoed}`" if vetoed else "--all-approvals is set"
    # An empty base is not None either: it would match no recorded base and
    # dismiss every marked approval. No base → skip the base check, as below,
    # where the skip is also ANNOUNCED.
    live_base = live_base or None
    # Every branch below states the consequence for the mode it is actually in:
    # `--all-approvals` ignores the head and withdraws everything (see `head`
    # below), so the non-retarget wording — a named SHA, a skipped base check, a
    # wrongly withdrawn head-valid approval — is false on exactly that path, and
    # a reader investigating a mass withdrawal is the one it would misdirect.
    if not live_head:
        cause = annotation_cause(read_error, "no head in the response")
        # A read that SUCCEEDED but carried no head may still carry a usable base;
        # keep it, so an off-base approval is still withdrawn. Only a failed read
        # leaves nothing to compare the base against.
        # Only a full SHA can stand in: the recorded marker is always lowercase
        # 40-hex, so an abbreviated SHA or a ref name would match none of them
        # and withdraw every marked approval — the same reason "" is refused.
        live_head = args.head_sha.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", args.head_sha or "") else ""
        if read_error is not None:
            live_base = None
        if not live_head and not withdraw_all:
            # A retarget or a veto needs no head to do its job, so neither is blocked
            # by the absence of one; every other mode compares against it and cannot run.
            fallback = (f"the event's head {args.head_sha!r} is not a full commit SHA to fall back to"
                        if args.head_sha else "there is no event head to fall back to")
            print(f"::error::Could not read the PR head to dismiss stale auto-approvals ({cause}), and {fallback}.")
            return 1
        print(f"::warning::Could not read the live PR head of {args.repo}#{args.pr_number} ({cause}) — "
              + (f"withdrawing every marked approval regardless of head, because {why_all}. The "
                 "base check is moot when they are all withdrawn anyway."
                 if withdraw_all else
                 f"judging staleness against the event's head {live_head!r}, "
                 + ("with the base check against the live base" if live_base else "without the base check")
                 + ", for this run. A queued or redelivered event can therefore withdraw an approval that is "
                 "valid for the head as it stands now."))
    elif not live_base:
        # The same guard as the head, on the other axis. An empty live base is NOT
        # None, and `_stale_approvals` treats "" as a real base that no recorded
        # base equals — so letting it through withdraws every approval that
        # recorded one, green and unannounced. Skip the base check instead, and say
        # what skipping it costs rather than implying the run was complete.
        live_base = None
        print(f"::warning::Read the live head of {args.repo}#{args.pr_number} but no base ref — "
              + (f"withdrawing every marked approval, because {why_all}; the base check is moot when they are "
                 "all withdrawn anyway."
                 if withdraw_all else
                 "comparing on head alone for this run. An approval recorded against a DIFFERENT base "
                 "therefore SURVIVES this run and keeps counting; the next event redoes the base check from "
                 "live state."))
    try:
        reviews = list_reviews(args.repo, args.pr_number)
    except (RuntimeError, ValueError) as e:
        print(f"::error::Could not list reviews to dismiss stale auto-approvals: {annotation_cause(e, 'unknown error')}")
        return 1
    head = None if withdraw_all else live_head
    if vetoed:
        # Takes precedence over the retarget wording: the veto is why every
        # approval goes, whether or not the base also moved.
        message = SKIP_REVIEW_WITHDRAWN_MESSAGE
        emit(f"Auto-approve: the PR carries `{vetoed}` — withdrawing every marked approval by "
             f"{args.approver_login or '(approver unavailable)'}.")
    ids = stale_reviews_to_dismiss(reviews, args.approver_login, head, live_base)
    others = unactionable_stale_approvals(reviews, args.approver_login, head, live_base)
    failed = []
    for rid in ids:
        try:
            dismiss(args.repo, args.pr_number, rid, message)
        except RuntimeError as e:
            # Collapsed per id: these are joined into an ::error:: annotation, and
            # each carries `gh`'s multi-line stderr.
            failed.append(f"{rid}: {annotation_cause(e, 'unknown error')}")
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


def _lf(body) -> str:
    """`body` with GitHub's stored CRLF line endings turned back into LF.

    NON_ROUND_BANNERS carry `\n` anchors, and GitHub rewrites a stored body to
    CRLF (post-review.py's `_normalize_review_body` exists for the same reason),
    so an unnormalised body would match neither banner and count a round that
    reviewed nothing.
    """
    return body.replace("\r\n", "\n") if isinstance(body, str) else ""


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
        and not any(b in _lf(r.get("body")) for b in NON_ROUND_BANNERS)
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
    """Every item of a paginated list endpoint; ValueError on any other shape.

    `--slurp` wraps each page in an outer array. A body that is not a list of
    lists (`null`, an API error object, a page that is not an array) would
    otherwise raise KeyError/TypeError past `cmd_round_cap`'s fail-open
    `except (RuntimeError, ValueError)`, turning `round-cap` red and skipping
    the panel that hangs off it.
    """
    pages = json.loads(gh(["api", "--paginate", "--slurp", path]))
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise ValueError(f"expected a list of pages from {path.split('?')[0]}, got {type(pages).__name__}")
    return [x for page in pages for x in page]


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
    # The EFFECTIVE cap, for the workflow's `max_rounds` output: an invalid value
    # is not applied, so it reports 0 (no cap), not what the caller typed.
    set_output("max_rounds", 0)
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
    set_output("max_rounds", max_rounds)
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
    # For the workflow's `round` output: the number THIS run's round takes if it
    # delivers one. Unset on a fail-open read error, so `round` is empty rather
    # than a guess.
    set_output("next_round", len(rounds) + 1)
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


# --- approve-external: an approval decided OUTSIDE cursor-review ------------
#
# cursor-approve.yml decides with .github/cursor-approve/aggregate.py and posts
# here, so the protections above apply unchanged: the head and base are re-read
# and the review withheld when either moved from what the axes judged, the reviewed SHA and
# base ride in the body's markers (so `dismiss-stale` withdraws it on a push or
# retarget like any other auto-approval), this identity's own earlier approvals
# are withdrawn on every non-approval, and a `needs-human-review` PR is never
# approved. Nor is a PR labelled `skip-cursor-review`: a caller's label-keyed
# concurrency group cannot cancel an in-flight decide when that veto lands
# mid-axes, so decide re-reads it live. The outcome is written as the step
# output `outcome` for the card.

EXTERNAL_OUTCOMES = ("approved", "not_approved", "superseded", "needs_human", "vetoed", "own_pr", "error")
SKIP_REVIEW_MESSAGE = f"The PR was labelled `{SKIP_REVIEW_LABEL}` — cursor-approve approval withdrawn."
LABELS_UNREADABLE_MESSAGE = (
    f"The PR's labels could not be read to rule out `{SKIP_REVIEW_LABEL}` — cursor-approve approval withdrawn."
)
EXTERNAL_AXES = ("business", "design", "correctness", "completeness", "conformance")


def external_decision_approves(decision, axes: list) -> bool:
    """True only for an APPROVE decision whose every expected axis reported a
    non-red verdict. aggregate.py already guarantees that; re-checking it here
    keeps a hand-edited or truncated decision file from reaching an approval."""
    if not isinstance(decision, dict) or decision.get("event") != APPROVE or not axes:
        return False
    verdicts = decision.get("verdicts")
    if not isinstance(verdicts, dict):
        return False
    return all(verdicts.get(axis) in ("green", "yellow") for axis in axes)


def render_external_body(card_url: str, reviewed_sha: str, base_ref: str) -> str:
    lines = [APPROVE_MARKER]
    if re.fullmatch(r"[0-9a-f]{40}", (reviewed_sha or "").lower()):
        lines.append(f"<!-- cursor-review-auto-approve:sha={reviewed_sha.lower()} -->")
    if base_ref:
        lines.append(f"<!-- cursor-review-auto-approve-base:{base_ref.encode('utf-8').hex()} -->")
    if re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_./#-]+", card_url or ""):
        lines.append(f"Approved, see the [cursor-approve card]({card_url}).")
    else:
        lines.append("Approved, see the cursor-approve card.")
    return "\n".join(lines)


def cmd_approve_external(args) -> int:
    set_output("outcome", "error")
    axes = [a.strip().lower() for a in (args.axes or "").split(",") if a.strip()]
    if not axes or any(a not in EXTERNAL_AXES for a in axes):
        print(f"::error::--axes must name some of {', '.join(EXTERNAL_AXES)}, got {args.axes!r}")
        return 2
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit_sha or ""):
        print(f"::error::--commit-sha must be a full 40-hex SHA, got {args.commit_sha!r}")
        return 2
    # An empty login matches no review, so every withdrawal below would silently
    # be a no-op and leave an earlier approval standing.
    if not (args.approver_login or "").strip():
        print("::error::--approver-login is empty; refusing to run without an identity to withdraw approvals for")
        return 2
    try:
        with open(args.decision, encoding="utf-8") as f:
            decision = json.load(f)
    except (OSError, ValueError):
        decision = None
    try:
        pr = read_pr(args.repo, args.pr_number)
        live_head, live_base = pr_head_base(pr)
        if not live_labels_readable(pr):
            # The veto below must be read, not assumed absent.
            raise ValueError("the PR payload carries no label list")
        human_review = has_label(pr, HUMAN_REVIEW_LABEL)
        vetoed = has_label(pr, SKIP_REVIEW_LABEL)
    except (RuntimeError, ValueError) as e:
        print(f"::error::Could not read the PR state: {e}")
        withdraw_own_approvals(args)
        return 1
    # The veto is PR-wide, so it outranks `superseded`: that path withdraws only
    # what is stale against the live head, and would leave an approval already
    # standing on the live head in place on a vetoed PR.
    if vetoed:
        outcome, why = "vetoed", f"the PR is labelled `{SKIP_REVIEW_LABEL}`"
    elif live_head != args.commit_sha:
        outcome, why = "superseded", f"the PR head moved from {args.commit_sha[:7]} to {live_head[:7] or '?'}"
    elif live_base != args.base_ref:
        # Same head, different base: the axes diffed against a base the PR no
        # longer targets, so their verdicts are about a diff nobody reviewed.
        outcome, why = "superseded", "the PR base changed since the axes ran"
    elif human_review:
        outcome, why = "needs_human", f"the PR is labelled `{HUMAN_REVIEW_LABEL}`"
    elif not external_decision_approves(decision, axes):
        outcome, why = "not_approved", "the axes did not approve"
    else:
        outcome, why = "approved", ""
    if outcome != "approved":
        set_output("outcome", outcome)
        emit(f"ℹ️ **cursor-approve: no approval** — {why}.")
        if outcome == "superseded" and live_head:
            # A newer head or base is a newer run's to judge: withdraw only what
            # is stale against the LIVE head and base, so a decide that finishes
            # late cannot dismiss an approval that run already posted.
            return withdraw_own_approvals(args, STALE_MESSAGE, live_head, live_base)
        if outcome == "vetoed":
            return withdraw_own_approvals(args, SKIP_REVIEW_MESSAGE)
        return withdraw_own_approvals(args)

    body = render_external_body(args.card_url, args.commit_sha, args.base_ref)
    try:
        posted = json.loads(
            gh(
                ["api", "-X", "POST", f"repos/{args.repo}/pulls/{args.pr_number}/reviews", "--input", "-"],
                {"commit_id": args.commit_sha, "event": APPROVE, "body": body},
            )
        )
        # As cmd_decide: the id is what a late veto or move dismisses by, so a
        # `gh` that exits 0 without one is an ambiguous POST, not a success.
        if not isinstance(posted, dict) or posted.get("id") is None:
            raise ValueError(f"the review POST returned no review id ({type(posted).__name__})")
    except (RuntimeError, ValueError) as e:
        if "own pull request" in str(e).lower():
            set_output("outcome", "own_pr")
            emit(f"ℹ️ **cursor-approve: skipped** — the approver authored this PR ({e}).")
            return 0
        print(f"::error::Could not submit the APPROVE review: {e}")
        withdraw_own_approvals(args)
        return 1

    # Same read → POST race cmd_decide closes: re-read after the write.
    try:
        pr_now = read_pr(args.repo, args.pr_number)
        head_now, base_now = pr_head_base(pr_now)
        labelled_now = has_label(pr_now, HUMAN_REVIEW_LABEL)
        # An unreadable label list cannot clear the veto: withdraw, but under
        # its own cause rather than claiming a label nobody applied.
        labels_unreadable = not live_labels_readable(pr_now)
        vetoed_now = has_label(pr_now, SKIP_REVIEW_LABEL)
    except (RuntimeError, ValueError):
        head_now = base_now = None
        labelled_now = vetoed_now = labels_unreadable = False
    moved = head_now != args.commit_sha or base_now != args.base_ref
    if moved or labelled_now or vetoed_now or labels_unreadable:
        # Veto first, as before the POST: it is PR-wide.
        if vetoed_now:
            outcome, message = "vetoed", SKIP_REVIEW_MESSAGE
        elif moved:
            outcome, message = "superseded", STALE_MESSAGE
        elif labelled_now:
            outcome, message = "needs_human", HUMAN_REVIEW_MESSAGE
        else:
            outcome, message = "error", LABELS_UNREADABLE_MESSAGE
        set_output("outcome", outcome)
        try:
            dismiss(args.repo, args.pr_number, posted["id"], message)
        except RuntimeError as e:
            print(f"::error::The PR changed while the approval was posted, and withdrawing it failed: {e}. {DISMISS_PERMISSION_HINT}")
            return 1
        if outcome in ("vetoed", "error"):
            # Not only the review just posted: any other standing approval by
            # this identity (an earlier run's, or one posted concurrently) would
            # still count toward branch protection on a PR that cannot be cleared.
            if withdraw_own_approvals(args, message):
                return 1
        if outcome == "error":
            print(f"::error::{LABELS_UNREADABLE_MESSAGE}")
            return 1
        emit("ℹ️ **cursor-approve: withdrawn** — the PR changed while the approval was being posted.")
        return 0
    set_output("outcome", "approved")
    emit("✅ **cursor-approve: APPROVE**")
    # Only here, exactly as cmd_decide: the APPROVE is posted AND the head/base
    # re-check passed. Never fatal, never undoes the approval (see
    # resolve_eligible_threads). Without a threshold there is no "at or below"
    # to resolve against, so nothing is resolved.
    threshold = (getattr(args, "threshold", "") or "").strip()
    if threshold:
        try:
            threshold = validate_threshold(threshold)
        except ValueError as e:
            print(f"::warning::Auto-resolve: skipped — {e}")
            return 0
        resolve_eligible_threads(args.repo, args.pr_number, getattr(args, "poster_login", "") or "",
                                 threshold, args.commit_sha)
    return 0


AXES_PENDING_MESSAGE = "cursor-approve's axes are judging this PR — auto-approve withdrawn until they agree."


def cmd_withdraw(args) -> int:
    """cursor-approve.yml's start phase: withdraw this identity's own marked
    approvals before the axes run. Unless the caller passes cursor-review
    `defer_approval: true` (``--defer-approval``), its `decide` has ALREADY
    approved when it reports `approve_gate == 'pass'`, so without this the axes
    would judge a PR that is approved for their whole run, and branch protection
    or auto-merge could land it before a red axis withdrew the approval. With
    defer on it is a backstop."""
    if not (args.approver_login or "").strip():
        print("::error::--approver-login is empty; refusing to run without an identity to withdraw approvals for")
        return 2
    return withdraw_own_approvals(args, AXES_PENDING_MESSAGE)


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
    # The login that posts cursor-review findings (the bot_app_id App's
    # `<slug>[bot]`, else github-actions[bot]). On APPROVE, only threads it
    # started are auto-resolved; empty = resolve nothing.
    d.add_argument("--poster-login", default="")
    # `true` = report the gate but never post an APPROVE (see the docstring).
    d.add_argument("--defer-approval", default="")
    d.add_argument("--approve-scope", default=SCOPE_FULL)
    d.add_argument("--incremental", default="")
    d.add_argument("--incremental-state", default="")
    d.add_argument("--ledger", default="")
    # approve_max_failed_reviewers: errored panel cells to tolerate (see panel_gate).
    d.add_argument("--max-failed-reviewers", default="0")
    # approve_authors, as resolved by the workflow's `gate` job: `false` = the PR
    # author is not listed, so auto-approve is off for this PR. Anything else
    # (including the default empty) = decide as usual.
    d.add_argument("--author-enabled", default="")
    d.add_argument("--pr-author", default="")
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
    e = sub.add_parser("approve-external")
    e.add_argument("--repo", required=True)
    e.add_argument("--pr-number", required=True)
    e.add_argument("--commit-sha", required=True)
    e.add_argument("--axes", required=True)
    e.add_argument("--decision", required=True)
    e.add_argument("--approver-login", required=True)
    e.add_argument("--base-ref", required=True)
    e.add_argument("--card-url", default="")
    # cursor-review's `approve_max_severity` and poster login (as for decide):
    # on an APPROVE that stands, the poster's own at-or-below-threshold threads
    # are auto-resolved. Either empty = resolve nothing.
    e.add_argument("--threshold", default="")
    e.add_argument("--poster-login", default="")
    w = sub.add_parser("withdraw")
    w.add_argument("--repo", required=True)
    w.add_argument("--pr-number", required=True)
    w.add_argument("--approver-login", required=True)
    args = parser.parse_args()
    if args.cmd == "approve-external":
        return cmd_approve_external(args)
    if args.cmd == "withdraw":
        return cmd_withdraw(args)
    if args.cmd == "round-cap":
        return cmd_round_cap(args)
    return cmd_decide(args) if args.cmd == "decide" else cmd_dismiss_stale(args)


if __name__ == "__main__":
    sys.exit(main())
