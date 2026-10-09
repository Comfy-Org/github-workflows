#!/usr/bin/env python3
"""Opt-in auto-approve: turn a cursor-review round into an approval decision.

Used by cursor-review.yml when the caller sets `approve_max_severity` (and,
for ``round-cap``, `max_rounds`), and by cursor-approve.yml (``approve-external``).
Five subcommands, each run from a job that checks out NO PR code:

``decide`` (in `post-review`, after the consolidated review is posted)
    APPROVE when every finding of this round is at or below the threshold and
    nothing below says "don't trust this round"; REQUEST_CHANGES when any finding
    is above it; otherwise no decision, which leaves a standing REQUEST_CHANGES
    (see below). The review is pinned (`commit_id`) to the commit the panel
    reviewed. With ``--card true`` it then writes cursor-approve's status card
    (``.github/cursor-approve/card.py``) for the outcome — every outcome except
    an author ``approve_authors`` does not list.

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
    supersedes it (GitHub counts a reviewer's most recent review) — unless no
    round is coming: on a PR labelled ``skip-cursor-review`` or
    ``needs-human-review``, or with ``--threshold ''`` (the caller's
    ``approve_max_severity`` is empty), this identity's own marked, unedited
    REQUEST_CHANGES are dismissed too (``withdraw_handed_off_blocks``).

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
    ``decide`` does (below). ``--approve-scope`` carries the round's
    ``approve_scope_effective``: only ``delta`` lets a marked non-gating thread
    above the threshold stop blocking that resolution; empty or absent is ``full``.
    An outcome that withholds (``EXTERNAL_BLOCK_OUTCOMES``) leaves the same
    standing REQUEST_CHANGES a no-decision ``decide`` does (BE-19489); one that
    hands the PR to a human (``needs_human``, ``vetoed``) withdraws it instead.

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

It also emits ``approve_scope_effective`` — the scope the round gated under after
``resolve_scope`` and the open-thread snapshot (``delta``, or ``full`` on every fail-closed fallback and
whenever no decision stands: an exit before the gate decides, a ``none`` decision,
and every later downgrade to ``untrusted`` or ``capped``) — for cursor-approve's ``--approve-scope``.

Fail-closed rules for ``decide``:

* the threshold is not one of ``medium``, ``low``, ``nit`` → exit 2, red;
* ``--author-enabled`` is anything but ``true``, ``false`` or empty → exit 2,
  red, earlier approvals withdrawn;
* the PR carries ``needs-human-review`` → no review event, ``capped``.

An untrusted round never approves (its findings are still on the PR as threads).
Since BE-19489 it does leave a standing REQUEST_CHANGES — the reasons, the
next step, and the usual markers — so a PR whose threads all get resolved still
does not look done when nothing approved it. A later no-decision round replaces
that block (posts its own, then dismisses this identity's older marked ones); a
later approving round — decide's APPROVE, its ``--defer-approval`` path, or
cursor-approve's ``approve-external`` — dismisses it. A ``capped`` round posts
none: the label already hands the PR to a human. A round is untrusted when:

* the judge did not adjudicate (degraded panel-union fallback);
* any panel cell is not ``ok`` (a short panel finding nothing proves nothing) —
  unless ``--max-failed-reviewers N`` (the workflow's
  `approve_max_failed_reviewers`, default 0) tolerates it. Only a cell that ran
  and reported ``status: "error"`` is tolerable, at most N of them, and only
  while every review type (``PANEL_REVIEW_TYPES``) still has an ``ok`` cell. A
  non-dict cell, a missing or unknown status, or an empty panel always
  withholds. Tolerated cells are named in the decision's reason;
* a reviewer leg did not succeed although every panel cell reports ``ok``
  (the consolidated file's ``panel_inconsistent``, set by aggregate-panel.py
  from the matrices' results): a cell can upload a forged ``ok`` under another
  cell's artifact name, and the honest leg's own upload then fails. Not
  re-run automatically — the forged artifact persists across re-run attempts;
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
  — not resolved, not outdated — → no decision (and the standing
  REQUEST_CHANGES above), so a High argued away in a reply cannot be approved
  over;
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
import time
import urllib.parse

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


def _load_card():
    """cursor-approve's card.py: decide writes the status card every round.

    Its API calls are routed through THIS module's `gh` (looked up at call
    time), so one bounded wrapper — and one test seam — covers every write.
    """
    module = _load_sibling(os.path.join("..", "cursor-approve", "card.py"), "cursor_approve_card")
    module.gh = lambda *a, **k: gh(*a, **k)
    return module


# --- approve_scope (rounds 2+ gate on the delta since the last reviewed commit) ---
#
# On a round after the first, a finding above the threshold counts toward the
# gate under `delta` only when it is IN the verified incremental block (its path
# and line fall inside one of that block's new-side hunks), a LIVE REPEAT (its
# `repeat_of` names an earlier thread the ledger reads as unresolved, OR its path
# and line sit on an earlier round's still-open thread — the judge only emits
# `repeat_of` for an ANSWERED entry, so the anchor match is what catches a re-raise
# of an unanswered one), or SEVERE (High/Critical, or a severity nobody
# recognises) anywhere in the reviewed diff.
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
    # Empty is `full`, not `delta`: an explicitly empty flag (a caller passing an
    # unset `${{ vars.X }}`) must not widen the gate. The workflow input's own
    # `default:` is the only place `delta` is chosen. Strip BEFORE defaulting, so
    # a whitespace-only value (a `${{ vars.X }}` holding a newline) is empty too.
    scope = (value or "").strip().lower() or SCOPE_FULL
    if scope not in ALLOWED_SCOPES:
        raise ValueError(f"approve_scope must be one of {', '.join(ALLOWED_SCOPES)}, got {value!r}")
    return scope


def hunk_ranges(patch_text: str):
    """{path: [(first, last), ...]} of every new-side hunk in a unified diff.

    None when ANY section's path cannot be read (git C-quotes a path carrying a
    quote, backslash, control or non-ASCII byte, and parse_paths rejects it):
    dropping that section would record no range for a path the round DID change,
    so every non-severe finding in it would fail open as non-gating. A rename's
    `rename from`/`rename to` lines are C-quoted the same way but returned
    verbatim by section_paths, so a path that still opens with `"` (which git
    never leaves unquoted) is unreadable too: keyed raw, it would match no
    finding's decoded path.
    """
    inc = _load_incremental_diff()
    ranges = {}
    for header, lines in inc.split_sections(patch_text or ""):
        paths = inc.section_paths(header, lines)
        if not paths or any(not p or p.startswith('"') for p in paths):
            return None
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
    ranges = hunk_ranges(incremental_text)
    if ranges is None:
        return {**full, "note": "the incremental block names a path that could not be parsed, so the round fails closed to `full`"}
    if not ranges:
        # An empty delta (a pure rebase) would make every non-severe finding
        # non-gating at once, and the rebase that produced it is also what marks
        # the earlier threads outdated — so the open-thread backstop cannot see them.
        return {**full, "note": "the incremental block has no new-side hunk (e.g. a pure rebase), so the round fails closed to `full`"}
    return {"scope": SCOPE_DELTA, "note": "", "ranges": ranges, "live": live_thread_urls(ledger)}


# Wall-clock budget for the open-thread read post-review takes ahead of its one
# review POST: past it the read is abandoned (marking nothing non-gating, the
# fail-closed direction) rather than starving the post that delivers the review.
OPEN_ANCHORS_BUDGET_SEC = 90


def earlier_thread_anchors(threads) -> dict:
    """{path: [(first, last), ...]} of every open cursor-review thread in `threads`. Pure.

    Open means unresolved and not outdated, so `line` is on the current head.
    post-review reads `threads` BEFORE it posts this round's review, so every
    one belongs to an earlier round (or an earlier attempt of this one) — no
    commit filter is needed, so a head reset back to a commit an earlier round
    reviewed still sees that round's threads.
    """
    gate = _load_gate_unresolved()
    out = {}
    for thread in threads:
        if not isinstance(thread, dict) or not gate.is_cursor_thread(thread):
            continue
        if thread.get("isResolved") or thread.get("isOutdated"):
            continue
        path, last = thread.get("path"), thread.get("line")
        if not isinstance(path, str) or not path or not isinstance(last, int):
            continue
        start = thread.get("startLine")
        start = start if isinstance(start, int) and start <= last else last
        out.setdefault(path, []).append((start, last))
    return out


def with_open_anchors(scope: dict, repo: str, pr: int, budget: float = OPEN_ANCHORS_BUDGET_SEC) -> dict:
    """`scope` plus the earlier open-thread anchors gating_reason() matches. `delta` only.

    post-review's read, taken before its own threads exist. A failure (a query
    error, or the walk outrunning `budget` seconds) propagates, so post-review
    marks nothing non-gating and writes no snapshot — and decide, finding none,
    fails closed to `full` with it.
    """
    if (scope or {}).get("scope") != SCOPE_DELTA:
        return scope
    owner, _, name = repo.partition("/")
    deadline = time.monotonic() + budget

    def bounded():
        for thread in _load_gate_unresolved().iter_threads(owner, name, pr):
            if time.monotonic() > deadline:
                raise RuntimeError(f"reading the PR's review threads took over {budget:g}s")
            yield thread

    return {**scope, "open": earlier_thread_anchors(bounded())}


def write_open_anchors(path: str, scope: dict, head_sha: str) -> None:
    """Save post-review's anchor snapshot for decide (no-op without `path` or anchors)."""
    if not path or "open" not in (scope or {}):
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"commit": head_sha, "open": scope["open"]}, f)


def read_open_anchors(path: str, head_sha: str):
    """post-review's anchor snapshot for `head_sha`, or None (missing, malformed, another commit).

    decide matches against the SAME snapshot post-review marked threads from, so
    the two can never disagree about which findings are non-gating — and decide
    takes no second, later read of its own.
    """
    data = _read_json_optional(path)
    if not data or data.get("commit") != head_sha or not isinstance(data.get("open"), dict):
        return None
    out = {}
    for file, spans in data["open"].items():
        if not isinstance(file, str) or not isinstance(spans, list):
            return None
        for span in spans:
            if (not isinstance(span, list) or len(span) != 2
                    or not all(isinstance(n, int) and not isinstance(n, bool) for n in span)):
                return None
            out.setdefault(file, []).append((span[0], span[1]))
    return out


def with_snapshot_anchors(scope: dict, path: str, head_sha: str) -> dict:
    """decide's half of with_open_anchors(): the snapshot's anchors, or `full`. `delta` only.

    No snapshot means post-review marked nothing non-gating, so decide gates on
    `full` too — every above-threshold finding counts, matching the unmarked
    threads on the PR.
    """
    if (scope or {}).get("scope") != SCOPE_DELTA:
        return scope
    anchors = read_open_anchors(path, head_sha)
    if anchors is None:
        return {"scope": SCOPE_FULL, "ranges": {}, "live": frozenset(),
                "note": "post-review's earlier open-thread snapshot is unavailable, so the round fails closed to `full`"}
    return {**scope, "open": anchors}


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
    if not isinstance(path, str) or not path:
        return "no usable file anchor"
    for first, last in scope.get("open", {}).get(path, ()):
        if first <= line <= last:
            return "on an unresolved earlier thread's line"
    for first, last in scope.get("ranges", {}).get(path, ()):
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
    panel_inconsistent: bool = False,
):
    """Return (event, reasons, blocking_findings). Pure; no I/O.

    Trust checks come first and gate BOTH review events: a round that cannot be
    trusted neither approves nor requests changes ON ITS FINDINGS (NONE). Its
    findings are still on the PR as threads. cmd_decide then posts the
    standing no-decision REQUEST_CHANGES itself; this function stays pure.
    """
    event, _, reasons, blocking = decide_gate(
        threshold, findings, panel, judge_status, delivered, reviewed_sha,
        live_head_sha, open_thread_severities, ungated, human_review,
        reviewed_diff_empty, reviewed_base, live_base, scope, max_failed_reviewers,
        panel_inconsistent,
    )
    return event, reasons, blocking


# The untrusted reasons a re-run cannot fix: the same round would land the same
# way. The status card and the standing request for changes name the cause and
# ask for a human instead of a relabel (see no_decision_next).
REASON_NOT_DELIVERED = "the review did not land on the PR as resolvable threads"
REASON_UNGATED_TAIL = "finding(s) reached the review body only, not as resolvable threads"
REASON_EMPTY_DIFF = ("the reviewed diff is empty — every changed path was excluded from review, "
                     "so nothing a reviewer saw can earn an approval")
# Structural too: artifacts are immutable once uploaded, so a forged one under
# another cell's name survives a re-run attempt and the honest leg 409s again.
REASON_PANEL_INCONSISTENT = ("a reviewer leg did not succeed although every panel cell reports ok, "
                             "so a cell's artifact may not be its own")
STRUCTURAL_CAUSES = (
    (REASON_NOT_DELIVERED, "the findings did not land as resolvable threads"),
    (REASON_UNGATED_TAIL, "some findings reached the review body only, not as resolvable threads"),
    (REASON_EMPTY_DIFF, "the reviewed diff is empty (every changed path was excluded from review)"),
    (REASON_PANEL_INCONSISTENT, "a reviewer leg failed although every panel cell reports ok (see the red leg checks)"),
)

# The two causes a fresh round on the LIVE head actually fixes, and the only
# ones auto_retry_eligible() re-runs the round for (BE-19526). Both mean the
# panel judged a commit the PR has moved past, so the next round reads
# different content and the gate's same-SHA dedupe cannot no-op it. Every other
# transient cause (a panel cell or the judge errored, the PR could not be read)
# leaves the head where it was, so the re-run would be skipped as
# already-reviewed — see the gate's `dup` step. Those still ask for a human.
REASON_HEAD_MOVED = "the PR head moved while the review ran"
REASON_BASE_CHANGED = "the PR base branch changed while the review ran"
RETRYABLE_CAUSES = (REASON_HEAD_MOVED, REASON_BASE_CHANGED)


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
    panel_inconsistent: bool = False,
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
    if panel_inconsistent:
        reasons.append(REASON_PANEL_INCONSISTENT)
    if not delivered:
        reasons.append(REASON_NOT_DELIVERED)
    elif ungated:
        reasons.append(f"{ungated} {REASON_UNGATED_TAIL}")
    if not reviewed_sha or reviewed_sha != live_head_sha:
        reasons.append(REASON_HEAD_MOVED)
    if reviewed_base != live_base:
        # A retarget never moves the head, so the head check cannot see it.
        reasons.append(REASON_BASE_CHANGED)
    if reviewed_diff_empty:
        reasons.append(REASON_EMPTY_DIFF)
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


SCOPE_NOTE_PREFIX = "approve_scope `"


def scope_note_of(reasons: list) -> str:
    """The scope note decide_gate appended to `reasons`, or "" when it has none.

    Matched by its prefix, not inferred from len(reasons): the untrusted path
    returns several trust reasons and no note, and none of those is a scope line.
    """
    last = reasons[-1] if len(reasons) > 1 else ""
    return last if isinstance(last, str) and last.startswith(SCOPE_NOTE_PREFIX) else ""


def scope_note(scope: dict, gating: int, non_gating_count: int, blocking: list) -> str:
    """The decision note's scope line: which scope, how many gated, and why."""
    scope = scope or {}
    text = f"{SCOPE_NOTE_PREFIX}{scope.get('scope') or SCOPE_FULL}`: {gating} gating, {non_gating_count} non-gating finding(s) above the threshold"
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


def open_thread_severities(repo: str, pr: int, sink=None) -> list:
    """(severity, non_gating) of every open (unresolved, non-outdated) cursor-review thread.

    This round's own threads are included on purpose: decide() only reaches the
    thread check when every finding of this round is at or below the threshold
    (never critical/high, never unbadged), so they cannot block — and not having to
    tell this round's threads from earlier ones keeps the check identity-free.

    Given a list as `sink`, each open thread is also appended to it as a dict
    (severity, non_gating, path, line, start_line, url) from the SAME read, so
    the status card can link the gating threads without a second query.
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
        if sink is not None:
            sink.append({
                "severity": out[-1][0],
                "non_gating": out[-1][1],
                "path": thread.get("path") if isinstance(thread.get("path"), str) else "",
                "line": thread.get("line") if isinstance(thread.get("line"), int) else None,
                "start_line": thread.get("startLine") if isinstance(thread.get("startLine"), int) else None,
                "url": thread_url(repo, pr, first),
            })
    return out


def thread_url(repo: str, pr, first_comment) -> str:
    """The `#discussion_r<id>` link of a thread's first comment, or ""."""
    if not isinstance(first_comment, dict):
        return ""
    cid = first_comment.get("fullDatabaseId") or first_comment.get("databaseId")
    if not re.fullmatch(r"[0-9]{1,20}", str(cid or "")) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        return ""
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+", server):
        server = "https://github.com"
    return f"{server}/{repo}/pull/{int(pr)}#discussion_r{cid}"


def thread_for(finding, threads: list):
    """The open thread a finding landed as: same path and its line inside the
    thread's range, preferring one whose badge matches the finding's severity
    (an unrecognised severity is posted under another badge). None when there
    is no such thread."""
    if not isinstance(finding, dict):
        return None
    sev = finding.get("severity")
    sev = sev.strip().lower() if isinstance(sev, str) else ""
    line = finding.get("line")
    if not isinstance(line, int) or isinstance(line, bool):
        return None
    hits = [t for t in threads or []
            if t.get("path") == finding.get("file") and t.get("line") is not None
            and (t.get("start_line") or t["line"]) <= line <= t["line"]]
    return next((t for t in hits if t.get("severity") == sev), hits[0] if hits else None)


def gating_why(finding, scope: dict, threshold: str) -> str:
    """Why a blocking finding gated, for the card: gating_reason(), with the
    `full`-scope reason spelled out."""
    reason = gating_reason(finding, scope)
    if reason == "full scope":
        return f"above `{threshold}` (approve_scope `full`: every finding above the threshold gates)"
    return reason or ""


def card_gating_rows(blocking: list, threads: list, scope: dict, threshold: str) -> list:
    """The REQUEST_CHANGES card's rows: each blocking finding with its thread."""
    rows = []
    for f in blocking:
        if not isinstance(f, dict):
            continue
        t = thread_for(f, threads)
        rows.append({"severity": f.get("severity"), "file": f.get("file"), "line": f.get("line"),
                     "url": (t or {}).get("url") or "", "why": gating_why(f, scope, threshold)})
    return rows


def open_blocking_rows(threads: list, threshold: str, scope: dict) -> list:
    """The card's rows for a round held by earlier open threads — the same
    selection _trusted_decision makes from their severities."""
    delta = (scope or {}).get("scope") == SCOPE_DELTA
    rows = []
    for t in threads or []:
        sev = t.get("severity")
        if (sev is None or above_threshold(sev, threshold)) and not (delta and t.get("non_gating")):
            rows.append({"severity": sev or "unknown", "file": t.get("path") or "?", "line": t.get("line"),
                         "url": t.get("url") or "",
                         "why": "unbadged open thread" if sev is None else "open thread from an earlier round"})
    return rows


def no_decision_next(reasons: list, label: str = ""):
    """(next marker, next-step text) for a no-decision round.

    A structural cause — the findings did not land as threads, or the reviewed
    diff is empty — comes back the same on a re-run, so it names the cause and
    asks for a human.

    Of the transient ones, only a moved head can be cleared by a relabel alone:
    the gate's `dup` step skips any head that already carries a bot-posted
    consolidated review, and a degraded round posts one too, so re-applying the
    label on an UNCHANGED head starts a run that no-ops (BE-19527). Those get
    `push_then_relabel` and the remedy that works — move the head, an empty
    commit is enough — rather than advice that silently does nothing. NOT the
    dup step's `state != "DISMISSED"` clause: the consolidated review is posted
    as COMMENT, which GitHub will not dismiss, so that clause is unreachable.
    """
    card = _load_card()
    causes = [cause for needle, cause in STRUCTURAL_CAUSES
              if any(isinstance(r, str) and needle in r for r in reasons or [])]
    if causes:
        return card.NEXT_HUMAN, f"A human is needed: {'; '.join(causes)}. A re-run would land the same way."
    if any(isinstance(r, str) and r == REASON_HEAD_MOVED for r in reasons or []):
        # The head moved, so the next round reads a commit with no review on it
        # and a relabel alone starts it. Deliberately NOT auto_retry_eligible():
        # that is strictly narrower (every reason must also be retryable), so a
        # head-moved round with a flaky judge alongside would be sent down the
        # push path although the relabel would have worked.
        return card.NEXT_RELABEL, card.next_relabel_text(label)
    return card.NEXT_PUSH, card.next_push_text(label)


# BE-19526. A round the head outran is a full panel spent on a commit the PR has
# moved past, and the recovery was a human noticing the card and re-applying the
# label by hand — so the PR sat behind a standing block until someone looked.
# The run re-runs it itself instead, ONCE per PR (decide decides; the
# workflow's last job, `auto-retry`, relabels — see cmd_auto_retry).
#
# The budget is counted off the PR's own reviews, not a run-local counter: this
# job is stateless and a retry's round is a different run. Every retry marks its
# standing block with AUTO_RETRY_MARKER, and a marked block still counts after a
# later round dismisses it (a DISMISSED review is still listed), so the ceiling
# holds across rounds. Past it the card asks for a human, as before.
#
# One is deliberate. A second retry only helps when the head moved again during
# the retry — i.e. the author is still pushing — and then re-running on every
# settle is exactly the "every push becomes a panel" cost this avoids.
AUTO_RETRY_MARKER = "<!-- cursor-review-auto-retry -->"
MAX_AUTO_RETRIES = 1
# card.DEFAULT_REVIEW_LABEL, mirrored like the card contract constants below
# so the relabel never needs card.py loaded (a test pins the two equal).
DEFAULT_REVIEW_LABEL = "cursor-review"


def auto_retry_eligible(reasons: list) -> bool:
    """Would a fresh round on the live head decide what this one could not?

    Only when the head moved — that is what makes the next round read different
    content, so the gate's same-SHA dedupe cannot skip it. A base retarget
    alongside it rides along (the relabel fixes both); a retarget ALONE does
    not, because the head is unchanged and the re-run would be deduped away.
    Any other reason in the list means a re-run would hit the same wall.
    """
    reasons = [r for r in (reasons or []) if isinstance(r, str) and r.strip()]
    if not reasons or REASON_HEAD_MOVED not in reasons:
        return False
    return all(r in RETRYABLE_CAUSES for r in reasons)


def auto_retries_spent(reviews: list, approver_login: str) -> int:
    """How many auto-retries this PR has already had: this identity's own
    reviews carrying AUTO_RETRY_MARKER, dismissed ones included."""
    login = (approver_login or "").strip().lower()
    if not login:
        return 0
    spent = 0
    for r in reviews or []:
        if not isinstance(r, dict):
            continue
        if ((r.get("user") or {}).get("login") or "").lower() != login:
            continue
        if AUTO_RETRY_MARKER in (r.get("body") or ""):
            spent += 1
    return spent


def round_cap_leaves_room(args) -> bool:
    """Would the re-run's own round-cap let its panel run?

    round-cap runs the panel while the delivered rounds so far are under
    `max_rounds`, and this round (`--round`, its number) is one of them once it
    lands. So a retry from the last allowed round would cap out and skip its
    panel, spending the one retry on nothing. `max_rounds` 0 is the cap off; an
    empty or unparsable round (round-cap failed open) is unknown, so no retry.
    """
    try:
        max_rounds = int(str(getattr(args, "max_rounds", "") or "").strip())
        this_round = int(str(getattr(args, "round", "") or "").strip())
    except ValueError:
        return False
    return max_rounds <= 0 or this_round < max_rounds


def auto_retry_budget_left(args, reasons: list) -> bool:
    """auto_retry_eligible() plus the per-PR ceiling, read from live reviews.

    An unreadable review list spends the budget rather than retrying blind: a
    wrong "no retries yet" is how one becomes a loop. So does an unknown
    approver login, which would otherwise match no marked block and read as
    "no retries yet" forever.

    Only an explicit `--can-relabel true` retries. cursor-review.yml passes
    `false` when neither APPROVER_TOKEN nor the bot App token is configured, so
    decide is running on GITHUB_TOKEN: a GITHUB_TOKEN-applied label fires no
    workflow run, so the relabel would succeed and start nothing, leaving the
    PR silently waiting on a round that is never coming. The card's
    hand-recovery is correct there.
    """
    if (getattr(args, "can_relabel", "") or "").strip().lower() != "true":
        return False
    if not auto_retry_eligible(reasons):
        return False
    if not (getattr(args, "approver_login", "") or "").strip():
        return False
    if not round_cap_leaves_room(args):
        return False
    try:
        spent = auto_retries_spent(list_reviews(args.repo, args.pr_number), args.approver_login)
    except (RuntimeError, ValueError) as e:
        print(f"::warning::Could not count earlier auto-retries, so this round is not re-run: "
              f"{annotation_cause(e, 'unknown error')}")
        return False
    return spent < MAX_AUTO_RETRIES


def cmd_auto_retry(args) -> int:
    """Re-run the round: take the review label off the PR and put it back.

    NOT run by decide: decide only sets `auto_retry=true` once the marked block
    is on the PR, and cursor-review.yml's `auto-retry` job runs this LAST, after
    every job that reports on this round. The relabel fires `unlabeled` and
    `labeled` events that land in the caller's `cancel-in-progress` group, so
    whatever of this run is still going when they arrive is cancelled — the
    Blocking gate and panel-integrity checks included, had it run in decide.

    The label write must come from APPROVER_TOKEN (a PAT) or the bot App token,
    never GITHUB_TOKEN: a GITHUB_TOKEN-applied label fires no workflow run, so
    the retry would silently relabel and start nothing.

    The DELETE tolerates a 404 — the label is already off (a run_without_label
    caller, or a human removed it mid-panel) and the POST alone starts the round.
    The POST is tried twice, since a failed one after a successful DELETE leaves
    the trigger label stripped. Any failure exits 1, so the job goes red; the
    card's next step already says to re-run by hand if no round starts.
    """
    label = (args.review_label or "").strip() or DEFAULT_REVIEW_LABEL
    url = f"repos/{args.repo}/issues/{args.pr_number}/labels"
    try:
        gh(["api", "-X", "DELETE", f"{url}/{urllib.parse.quote(label, safe='')}"])
    except (RuntimeError, ValueError) as e:
        if "404" not in str(e):
            print(f"::error::Could not re-run the round automatically: removing `{label}` failed "
                  f"({annotation_cause(e, 'unknown error')}). Re-run it by hand: remove and re-add `{label}`.")
            return 1
    err = None
    for _ in range(2):
        try:
            gh(["api", "-X", "POST", url, "--input", "-"], {"labels": [label]})
            break
        except (RuntimeError, ValueError) as e:
            err = e
    else:
        print(f"::error::Could not re-run the round automatically: re-adding `{label}` failed "
              f"({annotation_cause(err, 'unknown error')}), so the PR may now be WITHOUT the label. "
              f"Add `{label}` back by hand to start the round.")
        return 1
    emit(f"🔁 **Auto-approve: re-running the round** — the head moved, so `{label}` was removed and re-added.")
    return 0


# Mirrors gate-unresolved.AUTO_RESOLVE_MARKER, which build-ledger.py reads to keep
# this reply out of its answer count (a test pins the two equal).
AUTO_RESOLVE_MARKER = "<!-- cursor-review-auto-resolve -->"
RESOLVED = "resolved"
SKIP_HUMAN = "skipped-human"
SKIP_UNBADGED = "skipped-unbadged"
SKIP_ABOVE = "skipped-above-threshold"
SKIP_NON_GATING = "skipped-non-gating"  # a delta round marked it: left for a human
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
    body = first.get("body") or ""
    severity = thread_severity(body)
    if severity is None:
        return SKIP_UNBADGED, None
    if is_non_gating_thread(body):
        # Explicit, not via the threshold: a later, looser threshold must not
        # clear the thread the docs promise "stays open for a human".
        return SKIP_NON_GATING, severity
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


def plan_thread_resolution(threads: list, poster_login: str, threshold: str, honour_non_gating: bool = False):
    """([(thread, severity)] to resolve, {verdict: count}). Pure.

    `honour_non_gating` is True only for a round that gated under `delta` — the
    same `delta and marked` condition decide_gate's open-thread check uses. Under
    `full`, or from a caller that resolved no scope at all, a marked thread above
    the threshold blocks like any other.

    All-or-nothing on the PR's state: if ANY live (unresolved, non-outdated)
    cursor-review thread is above the threshold or unbadged, the PR is not
    approvable and nothing is resolved — not even the eligible ones. decide()
    already refuses to APPROVE in that state; this re-checks it against the
    threads read after the approval, for any caller that reaches here.
    """
    gate = _load_gate_unresolved()
    counts = {RESOLVED: 0, SKIP_HUMAN: 0, SKIP_UNBADGED: 0, SKIP_ABOVE: 0, SKIP_NON_GATING: 0}
    blocked = False
    plan = []
    for thread in threads:
        if gate.is_cursor_thread(thread) and not thread.get("isResolved") and not thread.get("isOutdated"):
            first = ((thread.get("comments") or {}).get("nodes") or [{}])[0]
            body = first.get("body") or ""
            live = thread_severity(body)
            # Under `delta`, a thread a delta-scoped round marked non-gating did not
            # hold the approval back, so it does not hold the others' resolution
            # back either. It is never resolved itself (SKIP_NON_GATING below).
            exempt = honour_non_gating and is_non_gating_thread(body)
            if (live is None or above_threshold(live, threshold)) and not exempt:
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


def resolve_eligible_threads(repo: str, pr, poster_login: str, threshold: str, commit_sha: str,
                             honour_non_gating: bool = False) -> dict:
    """Reply to and resolve the poster's own at-or-below-threshold threads.

    Call ONLY after an APPROVE has been posted and the head re-checked. Every
    failure is logged and skipped: nothing here is fatal, and nothing here undoes
    the approval. Returns the counts it logged (plus ``failed``).
    """
    counts = {RESOLVED: 0, SKIP_HUMAN: 0, SKIP_UNBADGED: 0, SKIP_ABOVE: 0, SKIP_NON_GATING: 0,
              "failed": 0, "deferred": 0}
    if not poster_login:
        emit("Auto-resolve: skipped — no cursor-review poster login was passed.")
        return counts
    owner, _, name = repo.partition("/")
    try:
        gate = _load_gate_unresolved()
        threads = list(gate.iter_threads(owner, name, int(pr)))
        plan, planned = plan_thread_resolution(threads, poster_login, threshold, honour_non_gating)
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
         f"skipped-non-gating {counts[SKIP_NON_GATING]}, failed {counts['failed']}, deferred {counts['deferred']}.")
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
    # The scope note is scope_note()'s own sentence, so it is not routed
    # through card.reason_text: an unloadable card.py must not cost the APPROVE.
    if scope_note_of(reasons):
        lines.append(f"\n_Scope: {scope_note_of(reasons)}._")
    # Guarded as in render_standing_body: this body is parsed back for its markers.
    if threshold in ALLOWED_THRESHOLDS:
        lines.append(f"\n_Threshold: `{threshold}` (set by this repo's `approve_max_severity`)._")
    return "\n".join(lines)


def render_standing_body(headline: str, reasons: list, next_text: str, threshold: str,
                         reviewed_sha: str = "", base_ref: str = "", prerendered: bool = False,
                         retry: bool = False) -> str:
    """The standing REQUEST_CHANGES a no-decision round leaves (BE-19489).

    Same markers as every other auto-approve review, so dismiss_own_change_requests
    (a later approving round) withdraws it and the next no-decision round
    replaces it. Reasons are decide_gate's, verbatim (card.reason_text).
    """
    reason_text = _load_card().reason_text
    lines = [APPROVE_MARKER]
    if re.fullmatch(r"[0-9a-f]{40}", (reviewed_sha or "").lower()):
        lines.append(f"<!-- cursor-review-auto-approve:sha={reviewed_sha.lower()} -->")
    if base_ref:
        lines.append(f"<!-- cursor-review-auto-approve-base:{base_ref.encode('utf-8').hex()} -->")
    if retry:
        # The auto-retry budget's ledger: counted off this PR's reviews by
        # auto_retries_spent, and still countable once a later round dismisses
        # this block. See AUTO_RETRY_MARKER.
        lines.append(AUTO_RETRY_MARKER)
    lines.append("### 🤖 Cursor Review — auto-approve")
    lines.append(f"⏸️ {headline}")
    # `prerendered`: approve-external passes card.decide_reasons' output, which
    # is already markdown-safe (axis reasons sanitized as on the card).
    lines += [f"- {r if prerendered else reason_text(r)}" for r in reasons if isinstance(r, str) and r.strip()]
    lines.append(f"\n**Next step:** {next_text}")
    if threshold in ALLOWED_THRESHOLDS:
        lines.append(f"\n_Threshold: `{threshold}` (set by this repo's `approve_max_severity`)._")
    return "\n".join(lines)


NO_DECISION_HEADLINE = "No decision this round, so this PR is not approved."
CAPPED_HEADLINE = "Needs a human, so this PR is not approved."

# The card contract's values (card.STATES / card.NEXTS; a test pins them
# equal). Mirrored here so cmd_decide's decision path never needs card.py
# loaded: a card.py that fails to import must cost only the card.
CARD_PASS, CARD_CHANGES, CARD_NO_DECISION, CARD_CAPPED = "pass", "changes_requested", "no_decision", "capped"
CARD_NEXT_NONE, CARD_NEXT_RESOLVE, CARD_NEXT_RELABEL, CARD_NEXT_HUMAN = "none", "resolve_then_relabel", "relabel", "human"
CARD_NEXT_PUSH = "push_then_relabel"


def _card_text(name: str, args=None) -> str:
    """A card.py next-step text — a function of the review label, or a constant
    — or "" when card.py cannot be loaded (the card is then skipped anyway)."""
    try:
        value = getattr(_load_card(), name)
        return value(_review_label(args)) if callable(value) else value
    except Exception:  # noqa: BLE001 — reporting only, see write_round_card
        return ""
STANDING_REPLACED_MESSAGE = "Superseded by the latest cursor-review round's request for changes."
NEWER_APPROVAL_MESSAGE = "A newer run already approved the live head — this request for changes is withdrawn."
APPROVED_LATER_MESSAGE = "A later cursor-review round approved this PR — request for changes withdrawn."


def post_standing_change_request(args, headline: str, reasons: list, next_text: str, threshold: str,
                                 prerendered: bool = False, result=None, retry: bool = False) -> int:
    """A no-decision round's standing block: one REQUEST_CHANGES as the approver.

    Without it a NONE round leaves nothing on the PR, and once the author
    resolves the threads the PR looks done though nothing approved it. Posted
    first, THEN this identity's older marked requests for changes are
    dismissed, so a repeated NONE replaces the block rather than stacking one
    — and a dismissal that fails leaves two blocks, never none. Only blocks
    OLDER than this one (lower review id) are dismissed, so a run still
    unwinding after a newer one started cannot sweep the newer block away.

    Pinned to the reviewed commit; when GitHub refuses that pin — the commit
    left the PR in a force-push, the very case behind "the PR head moved" and
    `superseded` — it is posted once more unpinned (GitHub then uses the head),
    the body's reviewed-SHA marker still naming what was reviewed. `result`,
    when given, receives the posted review's `id`.
    """
    try:
        body = render_standing_body(headline, reasons, next_text, threshold, args.commit_sha, args.base_ref,
                                    prerendered, retry)
    except Exception as e:  # noqa: BLE001 — card.py failing to load must not traceback here
        print(f"::error::Could not render the standing request for changes: {annotation_cause(e, 'unknown error')}")
        return 1
    url = f"repos/{args.repo}/pulls/{args.pr_number}/reviews"
    posted = None
    for payload in ({"commit_id": args.commit_sha, "event": REQUEST_CHANGES, "body": body},
                    {"event": REQUEST_CHANGES, "body": body}):
        try:
            posted = json.loads(gh(["api", "-X", "POST", url, "--input", "-"], payload))
            if not isinstance(posted, dict) or posted.get("id") is None:
                raise ValueError(f"the review POST returned no review id ({type(posted).__name__})")
            break
        except (RuntimeError, ValueError) as e:
            posted = None
            if "own pull request" in str(e).lower():
                emit(f"ℹ️ **Auto-approve: no standing request for changes** — the approver authored this PR ({e}).")
                return 0
            # Only GitHub REFUSING the pin (a 422) is retried unpinned. Anything
            # else — a 5xx, a timeout, a reply without an id — may have landed,
            # and a second POST would only add a duplicate block.
            if "commit_id" not in payload or not (isinstance(e, RuntimeError) and "422" in str(e)):
                print(f"::error::Could not post the standing request for changes: {annotation_cause(e, 'unknown error')}")
                return 1
            print(f"::warning::The standing request for changes could not be pinned to {args.commit_sha[:7]} "
                  f"({annotation_cause(e, 'unknown error')}); posting it unpinned.")
    if result is not None:
        result["id"] = posted["id"]
    emit("❌ **Auto-approve: REQUEST_CHANGES (no decision)** — a standing block until a round approves.")
    return dismiss_own_change_requests(args, STANDING_REPLACED_MESSAGE, older_than=posted["id"])


def write_round_card(args, state: str, next_step: str, headline: str, reasons: list, next_text: str,
                     threshold: str, gating=None) -> None:
    """Write (or edit in place) the cursor-approve status card for this round.

    Never fatal and never changes the decision: the card is a report. It is
    keyed on --approver-login, the identity GH_TOKEN carries here, which is the
    APPROVER_TOKEN identity cursor-approve's own phases write it under.
    """
    login = (getattr(args, "approver_login", "") or "").strip()
    if not login or (getattr(args, "card", "") or "").strip().lower() != "true":
        return
    try:
        card = _load_card()
        body = card.render_round(getattr(args, "round", ""), getattr(args, "max_rounds", ""), args.commit_sha,
                                 state, next_step, headline, reasons, next_text,
                                 getattr(args, "run_url", "") or "", gating, threshold)
        card.upsert(args.repo, args.pr_number, login, body)
    except Exception as e:  # noqa: BLE001 — a report: it runs after the decision landed, never undoes it
        print(f"::warning::Could not write the cursor-approve status card: {annotation_cause(e, 'unknown error')}")


def next_for_unchanged_head(args, head_moved: bool = False):
    """(next marker, next-step text) for a no-decision path that runs AFTER
    post-review.py posted the consolidated review at this exact SHA (BE-19527).

    Unless the head has since moved, a relabel there is a no-op: the gate's
    `dup` step skips any head already carrying that review, and it cannot be
    dismissed (see card.NEXT_PUSH). Moving the head is the remedy that works.
    """
    if head_moved:
        return CARD_NEXT_RELABEL, _card_text("next_relabel_text", args) or "Re-run the round."
    return CARD_NEXT_PUSH, (_card_text("next_push_text", args)
                            or "Push a commit so the head moves, then re-run the round.")


def _review_label(args) -> str:
    """cursor-review's `review_label` (--review-label), for the next-step text."""
    return getattr(args, "review_label", "") or ""


def card_for_none(args, gate: str, reasons: list, threshold: str, threads: list, scope: dict) -> int:
    """A NONE decision: the card, plus the standing request for changes.

    `capped` posts none: the label hands the PR to a human, and no round will
    approve it, so an earlier one is withdrawn instead (BE-19492) — else it
    would block the merge until dismissed by hand. The card says so.
    """
    if gate == GATE_CAPPED:
        rc = withdraw_handed_off_change_requests(args, HUMAN_REVIEW_MESSAGE, HUMAN_REVIEW_LABEL)
        write_round_card(args, CARD_CAPPED, CARD_NEXT_HUMAN, CAPPED_HEADLINE,
                         reasons, _card_text("NEXT_HUMAN_CAPPED_TEXT"), threshold)
        return rc
    retry = False
    if gate == GATE_FAIL:
        # Trusted round, but an earlier round's thread above the threshold is
        # still open. Never auto-retried: the round decided, and a re-run on the
        # same head would be deduped away anyway.
        state, nxt, next_text = CARD_CHANGES, CARD_NEXT_RESOLVE, _card_text("next_resolve_text", args)
        headline = f"Not approved: {reasons[0]}."
        gating = open_blocking_rows(threads, threshold, scope)
    else:
        state, headline, gating = CARD_NO_DECISION, NO_DECISION_HEADLINE, None
        try:
            nxt, next_text = no_decision_next(reasons, _review_label(args))
        except Exception:  # noqa: BLE001 — card.py unloadable: still post the block
            nxt, next_text = CARD_NEXT_PUSH, "Push a commit so the head moves, then re-run the round."
        # BE-19526: the head outran the round, and this PR has a retry left —
        # say so on the block and the card, then fire it below. Decided BEFORE
        # the block is posted so the marker it is counted by is on it.
        retry = auto_retry_budget_left(args, reasons)
    hand_text = next_text
    if retry:
        next_text = _card_text("next_auto_retry_text", args) or next_text
    posted = {}
    rc = post_standing_change_request(args, headline, reasons, next_text, threshold, retry=retry, result=posted)
    if retry and posted.get("id") is None:
        # No block landed (the approver authored the PR, the POST failed, the
        # body would not render), so no marker counts this retry: firing it
        # anyway would re-fire on every later moved head, unbounded. The card
        # goes back to the hand-recovery too, since nothing is re-running.
        retry, next_text = False, hand_text
    write_round_card(args, state, nxt, headline, reasons, next_text, threshold, gating)
    if retry:
        # cursor-review.yml's `auto-retry` job relabels; see cmd_auto_retry.
        set_output("auto_retry", "true")
    return rc


def cmd_decide(args) -> int:
    # Written before anything can fail, so every exit path below leaves a value;
    # each decision overwrites it (GITHUB_OUTPUT keeps the last write of a key).
    set_output("approve_gate", GATE_UNTRUSTED)
    # The scope this round gated under, for cursor-approve's approve-external
    # (`approve_scope_effective`). `full` until a decision stands on a `delta`
    # scope, so no exit without one tells cursor-approve to honour non-gating marks.
    set_output("approve_scope_effective", SCOPE_FULL)
    try:
        threshold = validate_threshold(args.threshold)
    except ValueError as e:
        print(f"::error::{e}")
        return 2
    try:
        max_failed = parse_max_failed_reviewers(getattr(args, "max_failed_reviewers", ""))
    except ValueError as e:
        print(f"::error::{e}")
        # The caller most likely edited this input mid-PR: fail closed, so an
        # earlier round's approval does not keep satisfying branch protection.
        withdraw_own_approvals(args)
        return 2
    try:
        requested_scope = validate_scope(getattr(args, "approve_scope", "") or "")
    except ValueError as e:
        print(f"::error::{e}")
        # Same as max_failed_reviewers above: a mid-PR edit must not leave an
        # earlier round's approval satisfying branch protection.
        withdraw_own_approvals(args)
        return 2
    # Every decision input is validated above, so a caller misconfiguration
    # fails red the same way whoever opened the PR.
    author_enabled = (getattr(args, "author_enabled", "") or "").strip().lower()
    if author_enabled not in ("", "true", "false"):
        # Only the gate job's `true`/`false` (or nothing, for no restriction) is
        # meaningful: anything else is a wiring fault, never a silent opt-in.
        print(f"::error::--author-enabled must be 'true', 'false' or empty, got {args.author_enabled!r}")
        withdraw_own_approvals(args)
        return 2
    if author_enabled == "false":
        # approve_authors (resolved in the workflow's `gate` job) does not list
        # this PR's author: auto-approve is off for the PR, exactly as if
        # approve_max_severity were empty. No review event is posted, and this
        # identity's own earlier verdicts are withdrawn: narrowing the list
        # (e.g. via a repo variable) moves no head SHA, so dismiss-stale would
        # leave an approval — or a REQUEST_CHANGES veto — at the current head.
        set_output("approve_gate", GATE_OFF)
        login = re.sub(r"[^A-Za-z0-9_\-\[\]]", "", getattr(args, "pr_author", "") or "") or "?"
        emit(f"ℹ️ **Auto-approve: off** — auto-approve not enabled for author {login} (not in `approve_authors`).")
        rc = withdraw_own_approvals(args, AUTHOR_OFF_MESSAGE)
        if rc:
            # An approval that could not be withdrawn still satisfies branch
            # protection: the gate must not read `off` over it.
            set_output("approve_gate", GATE_UNTRUSTED)
        if dismiss_own_change_requests(args, AUTHOR_OFF_MESSAGE):
            rc = 1
        return rc
    with open(args.findings, encoding="utf-8") as f:
        data = json.load(f)
    findings = data.get("findings") or []
    panel = data.get("panel") or []
    # A JSON bool written by `Build consolidated findings file`; absent (an
    # older payload) reads as consistent, exactly as before.
    panel_inconsistent = data.get("panel_inconsistent") is True
    try:
        ungated = int(args.ungated or 0)
    except ValueError:
        ungated = 1  # unparseable → assume something missed a thread
    scope = resolve_scope(requested_scope, getattr(args, "incremental_state", "") or "",
                          _read_optional(getattr(args, "incremental", "") or ""),
                          _read_json_optional(getattr(args, "ledger", "") or ""))
    scope = with_snapshot_anchors(scope, getattr(args, "open_anchors", "") or "", args.commit_sha)

    # The open threads behind `prior`, from the same read: the card links them.
    threads = []
    try:
        pr = read_pr(args.repo, args.pr_number)
        live_head, live_base = pr_head_base(pr)
        human_review = has_label(pr, HUMAN_REVIEW_LABEL)
        prior = open_thread_severities(args.repo, int(args.pr_number), threads)
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
            panel_inconsistent,
        )
    set_output("approve_gate", gate)
    if event != NONE:
        # Only a decision that ran reports its scope; withdraw_decision() puts
        # `full` back on every later path that stops standing behind it.
        set_output("approve_scope_effective", scope.get("scope") or SCOPE_FULL)
    if scope_note_of(reasons):
        emit(f"ℹ️ **Auto-approve scope** — {scope_note_of(reasons)}.")
    if event == NONE:
        emit(f"ℹ️ **Auto-approve: no decision** — {'; '.join(reasons)}.")
        rc = withdraw_own_approvals(args)
        # The standing request for changes and the card (BE-19489): a NONE
        # round used to leave nothing on the PR at all.
        try:
            if card_for_none(args, gate, reasons, threshold, threads, scope):
                rc = 1
        except Exception as e:  # noqa: BLE001 — the withdrawal above already ran; report, don't traceback
            print(f"::error::Could not leave the no-decision block or card: {annotation_cause(e, 'unknown error')}")
            rc = 1
        return rc
    if event == APPROVE and (getattr(args, "defer_approval", "") or "").strip().lower() == "true":
        result = {}
        rc = defer_to_cursor_approve(args, reasons[0], result)
        final = result.get("gate", GATE_PASS)
        if final == GATE_PASS:
            write_round_card(args, CARD_PASS, CARD_NEXT_NONE,
                             "Passed the severity gate; approval is left to cursor-approve's axes.",
                             reasons, "", threshold)
        elif final == GATE_CAPPED:
            write_round_card(args, CARD_CAPPED, CARD_NEXT_HUMAN, CAPPED_HEADLINE,
                             [f"the PR was labelled `{HUMAN_REVIEW_LABEL}`"], _card_text("NEXT_HUMAN_CAPPED_TEXT"),
                             threshold)
        else:
            # defer_to_cursor_approve has already dismissed every earlier block
            # and cursor-approve will not run on a non-`pass` gate, so without a
            # block here the PR would carry neither approval nor veto.
            why = [result.get("why") or "the deferred approval could not be stood behind"]
            nxt, next_text = next_for_unchanged_head(args, head_moved=bool(result.get("head_moved")))
            if post_standing_change_request(args, NO_DECISION_HEADLINE, why, next_text, threshold):
                rc = 1
            write_round_card(args, CARD_NO_DECISION, nxt, NO_DECISION_HEADLINE,
                             why, next_text, threshold)
        return rc

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
        withdraw_decision(GATE_UNTRUSTED)
        print(f"::error::Could not submit the {event} review: {annotation_cause(e, 'unknown error')}")
        withdraw_own_approvals(args)
        # A no-decision outcome like any other: leave the standing block (the
        # same POST may fail again; then it is red twice, and says so).
        why = [f"the {event} review could not be posted"]
        nxt, next_text = next_for_unchanged_head(args)
        post_standing_change_request(args, NO_DECISION_HEADLINE, why, next_text, threshold)
        write_round_card(args, CARD_NO_DECISION, nxt, NO_DECISION_HEADLINE, why, next_text, threshold)
        return 1

    # Close the read → POST race. A push or retarget landing in that window fires
    # an event whose dismissal scan can finish before this review exists, so
    # nothing else would ever withdraw it. Re-read the head and base now that the
    # review is written; if either moved, withdraw our own review here.
    #
    # The same window can label the PR `needs-human-review` — a human, or a
    # concurrent run hitting the round cap — and an APPROVE must not outlive
    # that hand-off either. A REQUEST_CHANGES withholds nothing, so it is not
    # undone as a decision; it goes with the hand-off's other blocks below.
    try:
        pr_now = read_pr(args.repo, args.pr_number)
        head_now, base_now = pr_head_base(pr_now)
        labelled_now = has_label(pr_now, HUMAN_REVIEW_LABEL)
    except (RuntimeError, ValueError):
        head_now = base_now = None  # unknown → treat as moved: withdraw rather than leave it
        labelled_now = False
    moved = head_now != args.commit_sha or base_now != args.base_ref
    if moved or labelled_now:
        withdraw_decision(GATE_UNTRUSTED if moved else GATE_CAPPED)
        why = "the PR head or base moved" if moved else f"the PR was labelled `{HUMAN_REVIEW_LABEL}`"
        # By the id just posted, not a re-list: the reviews listing can lag the
        # POST, and would then withdraw nothing while claiming otherwise.
        try:
            dismiss(args.repo, args.pr_number, posted["id"], HUMAN_REVIEW_MESSAGE if labelled_now else STALE_MESSAGE)
        except RuntimeError as e:
            print(f"::error::{why[0].upper()}{why[1:]} while the {event} review was posted, and withdrawing it failed: {annotation_cause(e, 'unknown error')}. {DISMISS_PERMISSION_HINT}")
            return 1
        emit(f"ℹ️ **Auto-approve: withdrawn** — {why} while the {event} review was being posted.")
        if not labelled_now:
            # The review just dismissed was this round's only verdict: without a
            # standing block the PR would carry neither approval nor veto.
            reasons_moved = [f"{why} while the {event} review was being posted"]
            # Only a HEAD move frees the relabel; a retarget alone leaves this
            # SHA carrying the consolidated review. An unreadable PR (head_now
            # None) counts as unmoved: advise the remedy that works either way.
            nxt, next_text = next_for_unchanged_head(args, head_moved=bool(head_now) and head_now != args.commit_sha)
            rc = post_standing_change_request(args, NO_DECISION_HEADLINE, reasons_moved, next_text, threshold)
            write_round_card(args, CARD_NO_DECISION, nxt, NO_DECISION_HEADLINE,
                             reasons_moved, next_text, threshold)
            return rc
        # Handed to a human (checked ahead of a move, which would re-post the
        # block): the withdrawn review may have been the only thing superseding
        # an earlier block, and a PR handed to a human keeps none (card_for_none).
        rc = withdraw_handed_off_change_requests(args, HUMAN_REVIEW_MESSAGE, HUMAN_REVIEW_LABEL,
                                                 older_than=posted["id"])
        write_round_card(args, CARD_CAPPED, CARD_NEXT_HUMAN, CAPPED_HEADLINE,
                         [f"the PR was labelled `{HUMAN_REVIEW_LABEL}`"], _card_text("NEXT_HUMAN_CAPPED_TEXT"), threshold)
        return rc
    emit(f"{'✅' if event == APPROVE else '❌'} **Auto-approve: {event}** — {reasons[0]}.")
    if event == APPROVE:
        # Only here: the APPROVE is posted AND the head/base re-check passed.
        # Never fatal, never undoes the approval (see resolve_eligible_threads).
        resolve_eligible_threads(args.repo, args.pr_number, getattr(args, "poster_login", "") or "",
                                 threshold, args.commit_sha, scope.get("scope") == SCOPE_DELTA)
        # The APPROVE already supersedes an earlier request for changes (GitHub
        # counts a reviewer's latest review); dismissing it too withdraws a
        # no-decision round's standing block visibly. Not fatal: it vetoes
        # nothing once the approval stands.
        dismiss_own_change_requests(args, APPROVED_LATER_MESSAGE, older_than=posted["id"])
        write_round_card(args, CARD_PASS, CARD_NEXT_NONE, f"Approved: {reasons[0]}.", reasons, "", threshold)
    else:
        write_round_card(args, CARD_CHANGES, CARD_NEXT_RESOLVE,
                         f"Not approved: {len(blocking)} finding(s) above `{threshold}` gate this round.",
                         reasons, _card_text("next_resolve_text", args), threshold,
                         card_gating_rows(blocking, threads, scope, threshold))
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


def withdraw_decision(gate: str) -> None:
    """The round no longer stands behind its decision: downgrade the gate, and
    report `full` so cursor-approve never honours this round's non-gating marks."""
    set_output("approve_gate", gate)
    set_output("approve_scope_effective", SCOPE_FULL)


def defer_to_cursor_approve(args, reason: str, result=None) -> int:
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
    withdrew_failed = bool(rc)
    if rc:
        withdraw_decision(GATE_UNTRUSTED)
    # Posting path: the APPROVE event supersedes this identity's earlier
    # REQUEST_CHANGES. Nothing is posted here, so dismiss those instead — else a
    # fixed High keeps vetoing the merge until cursor-approve approves, which it
    # never does on a red axis. Failing to is red but withholds nothing.
    if dismiss_own_change_requests(args):
        rc = 1
    # `result` (optional) receives the gate this ends on, and why, for the card.
    result = result if result is not None else {}
    result["gate"] = GATE_UNTRUSTED if withdrew_failed else GATE_PASS
    if result["gate"] != GATE_PASS:
        result["why"] = "an earlier approval by the approver could not be withdrawn"
    # Same post-decision re-check the posting path runs: a push, retarget or
    # `needs-human-review` label landing since the read must not leave `pass`.
    try:
        pr_now = read_pr(args.repo, args.pr_number)
        head_now, base_now = pr_head_base(pr_now)
        labelled_now = has_label(pr_now, HUMAN_REVIEW_LABEL)
    except (RuntimeError, ValueError):
        head_now = base_now = None  # unknown → treat as moved
        labelled_now = False
    moved = head_now != args.commit_sha or base_now != args.base_ref
    # Hand-off ranked ahead of a move, as on the posting path: an untrusted
    # result makes cmd_decide post a block, and a PR handed to a human keeps none.
    if labelled_now:
        withdraw_decision(GATE_UNTRUSTED if moved else GATE_CAPPED)
        result["gate"] = GATE_CAPPED
        emit(f"ℹ️ **Auto-approve: deferred round superseded** — the PR was labelled `{HUMAN_REVIEW_LABEL}`.")
    elif moved:
        withdraw_decision(GATE_UNTRUSTED)
        result["gate"], result["why"] = GATE_UNTRUSTED, "the PR head or base moved after the round was decided"
        # Only a HEAD move frees the relabel (see next_for_unchanged_head); an
        # unreadable PR counts as unmoved, so the card advises what works either way.
        result["head_moved"] = bool(head_now) and head_now != args.commit_sha
        emit("ℹ️ **Auto-approve: deferred round superseded** — the PR head or base moved.")
    return rc


def dismiss_own_change_requests(args, message: str = "", older_than=None) -> int:
    """Dismiss this identity's own unedited, marked REQUEST_CHANGES reviews —
    given `older_than` (the id of a review just posted), only those with a
    lower id, i.e. posted before it: review ids grow monotonically, and a run
    that finishes late must not dismiss a block a NEWER run posted. An edited
    one is someone else's words (see _stale_approvals) and stays."""
    if not (args.approver_login or "").strip():
        return 0
    try:
        ids = own_change_requests(list_reviews(args.repo, args.pr_number), args.approver_login, older_than)
    except (RuntimeError, ValueError) as e:
        print(f"::warning::Could not list reviews to withdraw an earlier request for changes: {annotation_cause(e, 'unknown error')}")
        return 1
    return _dismiss_change_requests(args, ids, message)


def withdraw_handed_off_change_requests(args, message: str, label: str = "", reviews=None, older_than=None) -> int:
    """A hand-off's withdrawal (BE-19492): this identity's own marked
    REQUEST_CHANGES, once no round is coming to supersede them.

    List FIRST, then confirm `label` is still on the live PR: every listed
    block was posted before that confirmation, so a run that read the label
    and finishes late cannot sweep a block a newer round posted after the
    label came off (a re-added label is a hand-off again). An unconfirmable
    label withdraws nothing and is red, since the block may wrongly stand."""
    if not (args.approver_login or "").strip():
        return 0
    try:
        if reviews is None:
            reviews = list_reviews(args.repo, args.pr_number)
        ids = own_change_requests(reviews, args.approver_login, older_than)
    except (RuntimeError, ValueError) as e:
        print(f"::warning::Could not list reviews to withdraw an earlier request for changes: {annotation_cause(e, 'unknown error')}")
        return 1
    if not ids:
        return 0
    if label:
        try:
            pr = read_pr(args.repo, args.pr_number)
            readable = live_labels_readable(pr)
        except (RuntimeError, ValueError) as e:
            pr, readable = None, False
            cause = annotation_cause(e, "unknown error")
        else:
            cause = "the PR payload carries no label list"
        if not readable:
            print(f"::warning::Could not confirm `{label}` is still on the PR ({cause}) — leaving this identity's "
                  "request for changes standing; the next event redoes the check.")
            return 1
        if not has_label(pr, label):
            emit(f"Auto-approve: `{label}` is no longer on the PR — leaving the request for changes to the next round.")
            return 0
    return _dismiss_change_requests(args, ids, message)


def own_change_requests(reviews: list, approver_login: str, older_than=None) -> list:
    """Ids of `approver_login`'s own unedited, marked REQUEST_CHANGES reviews
    (older than review id `older_than`, when given). Empty login → none."""
    login = (approver_login or "").strip().lower()
    if not login:
        return []
    return [r["id"] for r in reviews
            if r.get("state") == "CHANGES_REQUESTED" and not r.get("edited")
            and APPROVE_MARKER in (r.get("body") or "")
            and (r.get("user") or {}).get("login", "").lower() == login
            and (older_than is None or int(r["id"]) < int(older_than))]


def _dismiss_change_requests(args, ids: list, message: str = "") -> int:
    failed = []
    for rid in ids:
        try:
            dismiss(args.repo, args.pr_number, rid, message or PASSED_MESSAGE)
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
AUTHOR_OFF_MESSAGE = "cursor-review auto-approve is not enabled for this PR's author (`approve_authors`) — earlier auto-approve verdict withdrawn."
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
    human_review = False
    if read_error is None:
        if live_labels_readable(pr):
            vetoed = next((label for label in VETO_LABELS if has_label(pr, label)), None)
            human_review = has_label(pr, HUMAN_REVIEW_LABEL)
        else:
            print(f"::warning::The live labels of {args.repo}#{args.pr_number} are not a list of named labels — "
                  f"could not check for {', '.join(f'`{label}`' for label in VETO_LABELS)} this run; judging "
                  "staleness by head and base only. The next event redoes the check.")
    # So does `needs-human-review`: an approval must not outlive the hand-off
    # (cmd_decide), and withdrawing the block below would otherwise let an
    # older on-head approval count again.
    handed_off = vetoed or (HUMAN_REVIEW_LABEL if human_review else None)
    withdraw_all = args.all_approvals or handed_off is not None
    why_all = f"the PR carries `{handed_off}`" if handed_off else "--all-approvals is set"
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
    if handed_off:
        # Takes precedence over the retarget wording: the label is why every
        # approval goes, whether or not the base also moved.
        message = SKIP_REVIEW_WITHDRAWN_MESSAGE if vetoed else HUMAN_REVIEW_MESSAGE
        emit(f"Auto-approve: the PR carries `{handed_off}` — withdrawing every marked approval by "
             f"{args.approver_login or '(approver unavailable)'}.")
    ids = stale_reviews_to_dismiss(reviews, args.approver_login, head, live_base)
    others = unactionable_stale_approvals(reviews, args.approver_login, head, live_base)
    failed = []
    withdrawn = set()
    for rid in ids:
        try:
            dismiss(args.repo, args.pr_number, rid, message)
        except RuntimeError as e:
            # Collapsed per id: these are joined into an ::error:: annotation, and
            # each carries `gh`'s multi-line stderr.
            failed.append(f"{rid}: {annotation_cause(e, 'unknown error')}")
        else:
            withdrawn.add(rid)
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
    approval_stands = any(rid not in withdrawn for rid in own_approvals(reviews, args.approver_login))
    blocks_failed = withdraw_handed_off_blocks(args, reviews, vetoed, human_review, approval_stands)
    return 1 if failed or others or blocks_failed else 0


THRESHOLD_OFF_MESSAGE = ("cursor-review auto-approve is off for this repo (`approve_max_severity` is empty) — "
                         "earlier request for changes withdrawn.")


def own_approvals(reviews: list, approver_login: str) -> list:
    """Ids of `approver_login`'s own marked APPROVALS (stale or not)."""
    login = (approver_login or "").strip().lower()
    return [r["id"] for r in reviews
            if login and r.get("state") == "APPROVED" and APPROVE_MARKER in (r.get("body") or "")
            and (r.get("user") or {}).get("login", "").lower() == login]


def withdraw_handed_off_blocks(args, reviews: list, vetoed, human_review: bool, approval_stands: bool = False) -> int:
    """dismiss-stale's other half (BE-19492): withdraw this identity's own
    marked REQUEST_CHANGES once auto-approve has stopped deciding the PR.

    A push alone never clears one — a label-triggered caller starts no round
    on a push, so only a later round may supersede it. But on a vetoed PR
    (`skip-cursor-review`), one handed to a human (`needs-human-review`), or
    a caller whose threshold is empty (`--threshold ''`, the kill switch), no
    approving round is coming to withdraw it, and it would block the merge
    until someone dismissed it by hand. `--threshold` absent (None) checks
    the labels only. Red when a dismissal fails, like a stale approval.

    A label is re-confirmed after `reviews` was listed (see
    withdraw_handed_off_change_requests). Both labels withdrew every approval
    above; the kill switch does not, so while one of this identity's
    approvals still stands the block stays: dismissing the newer review would
    let that older approval count again.
    """
    if vetoed:
        return withdraw_handed_off_change_requests(args, SKIP_REVIEW_WITHDRAWN_MESSAGE, vetoed, reviews)
    if human_review:
        return withdraw_handed_off_change_requests(args, HUMAN_REVIEW_MESSAGE, HUMAN_REVIEW_LABEL, reviews)
    if getattr(args, "threshold", None) is None or args.threshold.strip():
        return 0
    if approval_stands:
        if own_change_requests(reviews, args.approver_login):
            emit("Auto-approve: `approve_max_severity` is empty, but an approval by this identity still stands — "
                 "leaving its newer request for changes, which a dismissal would let that approval outrank.")
        return 0
    return _dismiss_change_requests(args, own_change_requests(reviews, args.approver_login), THRESHOLD_OFF_MESSAGE)


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

# `retargeted` is `superseded` by a base change alone (BE-19527): handled the
# same here, but the head never moved, so the card advises the push remedy.
EXTERNAL_OUTCOMES = ("approved", "not_approved", "superseded", "retargeted", "needs_human", "vetoed", "own_pr",
                     "error")
SUPERSEDED_OUTCOMES = ("superseded", "retargeted")
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


# approve-external outcomes that WITHHOLD (BE-19489): each leaves the same
# standing REQUEST_CHANGES a no-decision cursor-review round does, so under
# `defer_approval` — where the passing cursor-review round already dismissed
# every earlier block — a red axis still leaves the PR visibly blocked until
# something approves. `own_pr` (nothing can approve) does not; neither does an
# approval.
EXTERNAL_BLOCK_OUTCOMES = ("not_approved", "error", "superseded", "retargeted")
# Outcomes that hand the PR to a human (BE-19492): no round will approve it,
# so no approving round would ever withdraw a block either. These post none
# and withdraw this identity's earlier ones, as cursor-review's capped round
# and dismiss-stale's veto do.
EXTERNAL_HANDOFF_MESSAGES = {
    "needs_human": (f"The PR was labelled `{HUMAN_REVIEW_LABEL}` — cursor-approve's request for changes withdrawn.",
                    HUMAN_REVIEW_LABEL),
    "vetoed": (f"The PR was labelled `{SKIP_REVIEW_LABEL}` — cursor-approve's request for changes withdrawn.",
               SKIP_REVIEW_LABEL),
}
EXTERNAL_BLOCK_HEADLINE = "cursor-approve did not approve this round, so this PR is not approved."


def own_approval_on(args, head: str) -> bool:
    """Whether this identity has an unedited marked approval recorded for
    `head` — a newer run's, when this one is superseded. Unreadable → False."""
    login = (args.approver_login or "").strip().lower()
    try:
        reviews = list_reviews(args.repo, args.pr_number)
    except (RuntimeError, ValueError):
        return False
    for r in reviews:
        match = REVIEWED_SHA_RE.search(r.get("body") or "")
        if (r.get("state") == "APPROVED" and not r.get("edited") and match
                and APPROVE_MARKER in (r.get("body") or "") and match.group(1) == (head or "").lower()
                and (r.get("user") or {}).get("login", "").lower() == login):
            return True
    return False


def external_block(args, outcome: str, decision, why: str, live_head: str = "") -> int:
    """Post the standing REQUEST_CHANGES for a withholding outcome; 0 when none is due.

    The body is the decide card's own reasons (card.decide_reasons) and next
    step. A `superseded` decide that finishes late must not veto a newer run's
    approval of the live head (GitHub counts a reviewer's LATEST review), so
    it posts nothing when one stands. A hand-off outcome
    (EXTERNAL_HANDOFF_MESSAGES) withdraws the earlier blocks instead.
    """
    if outcome in EXTERNAL_HANDOFF_MESSAGES:
        message, label = EXTERNAL_HANDOFF_MESSAGES[outcome]
        return withdraw_handed_off_change_requests(args, message, label)
    if outcome not in EXTERNAL_BLOCK_OUTCOMES:
        return 0
    card = _load_card()
    if outcome in SUPERSEDED_OUTCOMES:
        if live_head and own_approval_on(args, live_head):
            emit("ℹ️ **cursor-approve: no standing request for changes** — a newer run already approved the live head.")
            return 0
        reasons = [card.reason_text(why)]
    else:
        reasons = card.decide_reasons(outcome, decision)
    posted = {}
    rc = post_standing_change_request(args, EXTERNAL_BLOCK_HEADLINE, reasons, card.DECIDE_NEXT_TEXT[outcome],
                                      getattr(args, "threshold", "") or "", prerendered=True, result=posted)
    # The check above is a read; a newer run's approval can land between it
    # and the POST, and the block — now the latest review — would override
    # it. Re-read after the write and take the block back if so.
    if (outcome in SUPERSEDED_OUTCOMES and live_head and posted.get("id") is not None
            and own_approval_on(args, live_head)):
        try:
            dismiss(args.repo, args.pr_number, posted["id"], NEWER_APPROVAL_MESSAGE)
            emit("ℹ️ **cursor-approve: standing request for changes withdrawn** — a newer run approved the live head meanwhile.")
        except RuntimeError as e:
            print(f"::error::A newer run approved the live head while this block was posted, and withdrawing it "
                  f"failed: {annotation_cause(e, 'unknown error')}. {DISMISS_PERMISSION_HINT}")
            return 1
    return rc


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
        external_block(args, "error", decision, "")
        return 1
    # The veto is PR-wide, so it outranks `superseded`: that path withdraws only
    # what is stale against the live head, and would leave an approval already
    # standing on the live head in place on a vetoed PR.
    # The hand-off outranks it too: a superseded outcome posts a block, which
    # a PR handed to a human must not keep (BE-19492).
    if vetoed:
        outcome, why = "vetoed", f"the PR is labelled `{SKIP_REVIEW_LABEL}`"
    elif human_review:
        outcome, why = "needs_human", f"the PR is labelled `{HUMAN_REVIEW_LABEL}`"
    elif live_head != args.commit_sha:
        outcome, why = "superseded", f"the PR head moved from {args.commit_sha[:7]} to {live_head[:7] or '?'}"
    elif live_base != args.base_ref:
        # Same head, different base: the axes diffed against a base the PR no
        # longer targets, so their verdicts are about a diff nobody reviewed.
        outcome, why = "retargeted", "the PR base changed since the axes ran"
    elif not external_decision_approves(decision, axes):
        outcome, why = "not_approved", "the axes did not approve"
    else:
        outcome, why = "approved", ""
    if outcome != "approved":
        set_output("outcome", outcome)
        emit(f"ℹ️ **cursor-approve: no approval** — {why}.")
        if outcome in SUPERSEDED_OUTCOMES and live_head:
            # A newer head or base is a newer run's to judge: withdraw only what
            # is stale against the LIVE head and base, so a decide that finishes
            # late cannot dismiss an approval that run already posted.
            rc = withdraw_own_approvals(args, STALE_MESSAGE, live_head, live_base)
        elif outcome == "vetoed":
            rc = withdraw_own_approvals(args, SKIP_REVIEW_MESSAGE)
        else:
            rc = withdraw_own_approvals(args)
        if external_block(args, outcome, decision, why, live_head):
            rc = 1
        return rc

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
        external_block(args, "error", decision, "")
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
        # Veto, then hand-off, first, as before the POST.
        if vetoed_now:
            outcome, message = "vetoed", SKIP_REVIEW_MESSAGE
        elif labelled_now:
            outcome, message = "needs_human", HUMAN_REVIEW_MESSAGE
        elif moved:
            # `retargeted` only when the re-read SHOWS the head unchanged: an
            # unreadable PR (head_now None) stays `superseded`, as before the
            # split, rather than reporting a retarget nobody observed.
            retargeted = head_now == args.commit_sha
            outcome, message = ("retargeted" if retargeted else "superseded"), STALE_MESSAGE
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
        # The withdrawn approval leaves the PR unblocked unless this does.
        blocked = external_block(args, outcome, decision, "the PR head or base moved while the approval was being posted",
                                 head_now or "")
        if outcome == "error":
            print(f"::error::{LABELS_UNREADABLE_MESSAGE}")
            return 1
        emit("ℹ️ **cursor-approve: withdrawn** — the PR changed while the approval was being posted.")
        return 1 if blocked else 0
    set_output("outcome", "approved")
    emit("✅ **cursor-approve: APPROVE**")
    # Withdraw a standing request for changes an earlier no-decision round left
    # (BE-19489) — the same path cursor-review's own approving decide takes.
    # Not fatal: the APPROVE, as this identity's latest review, already
    # supersedes it.
    dismiss_own_change_requests(args, APPROVED_LATER_MESSAGE, older_than=posted["id"])
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
        # The scope the cursor-review round actually gated under (its
        # `approve_scope_effective` output), so a marked non-gating thread is
        # exempt here exactly when it was exempt in that round's decide. Empty or
        # absent is `full`; an unknown value is `full` too, never `delta`.
        try:
            scope = validate_scope(getattr(args, "approve_scope", "") or "")
        except ValueError as e:
            print(f"::warning::Auto-resolve: {e}; resolving as `{SCOPE_FULL}`")
            scope = SCOPE_FULL
        resolve_eligible_threads(args.repo, args.pr_number, getattr(args, "poster_login", "") or "",
                                 threshold, args.commit_sha, scope == SCOPE_DELTA)
    return 0


AXES_PENDING_MESSAGE = "cursor-approve's axes are judging this PR — auto-approve withdrawn until they agree."


def cmd_withdraw(args) -> int:
    """cursor-approve.yml's start phase: withdraw this identity's own marked
    approvals before the axes run. Unless the caller passes cursor-review
    `defer_approval: true` (``--defer-approval``), its `decide` has ALREADY
    approved when it reports `approve_gate == 'pass'`, so without this the axes
    would judge a PR that is approved for their whole run, and branch protection
    or auto-merge could land it before a red axis withdrew the approval. With
    defer on it is a backstop.

    With ``--approve-gate capped`` it also withdraws this identity's standing
    REQUEST_CHANGES (BE-19492): when the round cap is what labelled the PR, no
    `decide` ran to do it, and no round will approve a PR handed to a human.
    The gate is the caller's word, so the label is re-read off the live PR
    first. A failure is red, but the start card still runs (its `if:`)."""
    if not (args.approver_login or "").strip():
        print("::error::--approver-login is empty; refusing to run without an identity to withdraw approvals for")
        return 2
    rc = withdraw_own_approvals(args, AXES_PENDING_MESSAGE)
    if (getattr(args, "approve_gate", "") or "").strip() == GATE_CAPPED:
        if withdraw_handed_off_change_requests(args, HUMAN_REVIEW_MESSAGE, HUMAN_REVIEW_LABEL):
            rc = 1
    return rc


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
    d.add_argument("--open-anchors", default="", help="post-review's --open-anchors-out snapshot")
    # approve_max_failed_reviewers: errored panel cells to tolerate (see panel_gate).
    d.add_argument("--max-failed-reviewers", default="0")
    # approve_authors, as resolved by the workflow's `gate` job: `false` = the PR
    # author is not listed, so auto-approve is off for this PR. Anything else
    # (including the default empty) = decide as usual.
    d.add_argument("--author-enabled", default="")
    d.add_argument("--pr-author", default="")
    # For the status card decide writes every round (round-cap's next_round /
    # max_rounds, and this run's URL). All optional: they only label the card.
    # `true` = write the card (the workflow always passes it); anything else
    # leaves the PR's comments alone, e.g. a local run.
    d.add_argument("--card", default="")
    d.add_argument("--round", default="")
    d.add_argument("--max-rounds", default="")
    d.add_argument("--run-url", default="")
    # cursor-review's `review_label`, named in the next-step line; '' = the default.
    d.add_argument("--review-label", default="")
    # Whether the token decide holds can start a run by relabelling (BE-19526):
    # `false` on the GITHUB_TOKEN fallback, which applies labels that fire
    # nothing. Fails closed: only an explicit `true` retries.
    d.add_argument("--can-relabel", default="false")
    s = sub.add_parser("dismiss-stale")
    s.add_argument("--repo", required=True)
    s.add_argument("--pr-number", required=True)
    # Empty = the approver identity is not available to this run: dismiss
    # nothing, and go red over any stale marked approval.
    s.add_argument("--approver-login", required=True)
    s.add_argument("--all-approvals", action="store_true")
    # The event's head: used ONLY when the live PR head cannot be read.
    s.add_argument("--head-sha", default="")
    # The caller's approve_max_severity. Passed but empty = auto-approve is
    # off, so its standing requests for changes are withdrawn; absent = unknown.
    s.add_argument("--threshold", default=None)
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
    # cursor-review's `approve_scope_effective`: `delta` exempts marked
    # non-gating threads from blocking resolution, as decide did. Default `full`.
    e.add_argument("--approve-scope", default=SCOPE_FULL)
    r = sub.add_parser("auto-retry")
    r.add_argument("--repo", required=True)
    r.add_argument("--pr-number", required=True)
    r.add_argument("--review-label", default="")
    w = sub.add_parser("withdraw")
    w.add_argument("--repo", required=True)
    w.add_argument("--pr-number", required=True)
    w.add_argument("--approver-login", required=True)
    # cursor-review's approve_gate: `capped` also withdraws the standing block.
    w.add_argument("--approve-gate", default="")
    args = parser.parse_args()
    if args.cmd == "approve-external":
        return cmd_approve_external(args)
    if args.cmd == "withdraw":
        return cmd_withdraw(args)
    if args.cmd == "round-cap":
        return cmd_round_cap(args)
    if args.cmd == "auto-retry":
        return cmd_auto_retry(args)
    return cmd_decide(args) if args.cmd == "decide" else cmd_dismiss_stale(args)


if __name__ == "__main__":
    sys.exit(main())
