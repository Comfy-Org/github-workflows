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

# Matches exactly the labels this workflow OWNS, so reconciliation never
# touches a human's `risk-assessment-done` or similar. Deliberately broader
# than the set we are allowed to CREATE (below): a `risk:R7` written by an
# older revision still has to be cleanable, or it sits on the PR forever
# alongside the current tier and breaks the "exactly one risk:R* at any time"
# invariant. Always used with `fullmatch`, never `match` — see below.
RISK_LABEL_RE = re.compile(r"risk:R[0-9]+")

# The tiers this workflow may CREATE. grade_risk only ever emits R0..R3, so a
# report asking for `risk:R4` is malformed and is refused rather than applied.
# `fullmatch` rather than `match` + `$`: `$` also matches immediately before a
# trailing newline, so `"risk:R2\n"` would pass validation and then splice a
# raw newline into the label DELETE path.
APPLICABLE_LABEL_RE = re.compile(r"risk:R[0-3]")

# Logins that may have authored our sticky comment. `github-actions[bot]` is
# the GITHUB_TOKEN identity and is ALWAYS accepted, so a repo that later turns
# on `bot_app_id` adopts the comment it posted before the switch instead of
# starting a duplicate.
DEFAULT_AUTHOR_LOGIN = "github-actions[bot]"

# The dispute checkbox exactly as `grade_risk.render_comment` renders it,
# unticked. Repeated here (rather than imported) so the publisher keeps working
# if the grader module is unavailable, which is exactly the case the last-resort
# renderers below exist for; a unit test asserts the two forms stay in step.
DISPUTE_LINE = "- [ ] **This grade is wrong**"

# The checkbox line as rendered by grade_risk.render_comment. `[xX]` because
# GitHub's own checkbox UI writes a lowercase x but hand-edits may not.
CHECKED_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+\*\*This grade is wrong\*\*", re.M)
UNCHECKED_RE = re.compile(r"^\s*[-*]\s+\[ \]\s+\*\*This grade is wrong\*\*", re.M)


class ApiError(RuntimeError):
    pass


def api(method: str, path: str, token: str, body: dict | None = None) -> dict | list:
    """Minimal GitHub REST call. Kept tiny and injectable so the tests can
    replace it wholesale rather than mocking a transport.

    EVERY failure leaves here as `ApiError`. That is the contract `cmd_publish`
    relies on to attempt its three publications independently: an exception
    that is not an `ApiError` escapes every `except ApiError` there and aborts
    the whole publish — after the Check Run has already announced a tier. Two
    such escapes are reachable without any HTTP error at all: a socket read
    timeout in `resp.read()` raises `TimeoutError`, and a non-JSON body (a
    proxy's HTML error page, a truncated response) raises `JSONDecodeError`.
    Both are caught here rather than at each call site.
    """
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
    except OSError as exc:  # pragma: no cover - network path
        # Covers the read timeout (TimeoutError) and any other socket-level
        # failure raised by `resp.read()` rather than by the request itself.
        raise ApiError(f"{method} {url} -> {exc}") from exc
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except ValueError as exc:  # pragma: no cover - network path
        raise ApiError(
            f"{method} {url} -> response was not JSON: {exc}"
        ) from exc


def _paged(call, path: str) -> list:
    """GET every page of a list endpoint.

    GitHub defaults these to 30 items per page. Reading only the first page of
    `/labels` would hide a stale `risk:R*` sitting on page 2 of a PR with more
    than 30 labels: it is never deleted, the desired tier is added alongside
    it, and the load-bearing "exactly one `risk:R*` at any time" invariant
    breaks. Bounded at 10 pages — 1000 labels is far past any real PR.
    """
    sep = "&" if "?" in path else "?"
    out: list = []
    page = 1
    while page <= 10:
        batch = call("GET", f"{path}{sep}per_page=100&page={page}", None)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def current_pr_state(call, repo: str, pr: int) -> tuple[str | None, str | None]:
    """The PR's head SHA and base branch as GitHub sees them NOW.

    Both halves matter for staleness. The head SHA catches a superseded push.
    The base REF catches a retarget: changing a PR's base changes its three-dot
    diff — and so its grade — without touching the head SHA and without firing
    `synchronize`, so a grade computed against the old base would otherwise
    sail past a head-only guard. (The base *SHA* is deliberately not compared:
    it advances whenever the base branch does, which is not a retarget and must
    not suppress an ordinary publish.)

    Either half is None if it can't be read.
    """
    try:
        data = call("GET", f"/repos/{repo}/pulls/{pr}", None)
    except ApiError:
        # Best effort: a failed staleness check must not block publishing an
        # otherwise good grade. Falling back to publishing keeps the previous
        # behaviour rather than silently dropping the grade.
        return None, None
    data = data or {}
    sha = (data.get("head") or {}).get("sha")
    base = (data.get("base") or {}).get("ref")
    return (
        sha if isinstance(sha, str) and sha else None,
        base if isinstance(base, str) and base else None,
    )


def reconcile_labels(call, repo: str, pr: int, desired: str | None) -> dict:
    """Make the PR carry `desired` and no other `risk:R*` label.

    `desired=None` (the unknown case) removes every `risk:R*` label and adds
    none — an ungradable PR must not keep a stale tier, and must not silently
    acquire `risk:R0`.

    `desired` is validated against `APPLICABLE_LABEL_RE`. It arrives verbatim
    from the report artifact — untyped JSON — and this job is the privileged
    one: an unvalidated name would let a malformed report create an ARBITRARY
    label (`lgtm`, `risk-assessment-done`) that no later reconciliation would
    ever clean up, because reconciliation only deletes names matching
    `RISK_LABEL_RE`. A non-match is treated as unknown. The `isinstance` guard
    is load-bearing: a non-string `desired` (a number, a list) would raise
    `TypeError` out of the regex and bypass the caller's `ValueError` handling
    entirely.
    """
    if desired is not None and not (
        isinstance(desired, str) and APPLICABLE_LABEL_RE.fullmatch(desired)
    ):
        raise ValueError(
            f"refusing to apply {desired!r}: not a risk:R0..R3 label. The report "
            "artifact is malformed; publishing as unknown instead."
        )
    current = _paged(call, f"/repos/{repo}/issues/{pr}/labels")
    names = [lbl["name"] for lbl in current]
    ours = [n for n in names if isinstance(n, str) and RISK_LABEL_RE.fullmatch(n)]

    # Add BEFORE removing. `api()` raises on any non-2xx, and a DELETE can
    # legitimately 404 (a concurrent run already removed it) or hit a 403 /
    # 5xx / rate limit. Deleting first meant one such error propagated out of
    # cmd_publish with the desired label never added — leaving the PR carrying
    # NO risk:* label while the Check Run had already announced a tier.
    added = []
    if desired and desired not in names:
        # POST .../labels creates the label in the repo if it does not exist
        # yet, so a consumer repo needs no manual label setup to opt in.
        call("POST", f"/repos/{repo}/issues/{pr}/labels", {"labels": [desired]})
        added.append(desired)

    removed, failed = [], []
    for name in [n for n in ours if n != desired]:
        try:
            # URL-safe enough: risk labels are `risk:R<n>` by construction, but
            # the colon is encoded so a future tier name can't break the path.
            call(
                "DELETE",
                f"/repos/{repo}/issues/{pr}/labels/{name.replace(':', '%3A')}",
                None,
            )
            removed.append(name)
        except ApiError as exc:
            # A stale tier we could not remove is worth reporting, but it must
            # not abort the run and take the sticky comment down with it.
            failed.append(f"{name} ({exc})")
    # `names` is the label set as it was READ, so the caller can see whether
    # `risk-grade-disputed` is on the PR without paying for a second page walk.
    return {
        "added": added,
        "removed": removed,
        "failed_removals": failed,
        "names": names,
    }


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


def _author_allowed(logins) -> set[str]:
    """The author logins that may own our sticky comment."""
    return {DEFAULT_AUTHOR_LOGIN} | {str(x) for x in (logins or ()) if x}


def find_sticky(call, repo: str, pr: int, logins=()) -> dict | None:
    """Return this workflow's existing sticky comment, if any.

    The marker is public — it appears in the README and in every rendered
    comment — so matching on it alone lets a PR author pre-post a comment
    carrying the marker and have this publisher PATCH it: the author's body is
    destroyed, and they thereafter control the preserved dispute-checkbox state
    (or, if the token cannot edit a foreign comment, every re-grade 403s).

    Three things must therefore agree before a comment is adopted:

      author type   `Bot` — our comment is always written by a bot token.
      author login  `github-actions[bot]` or an app login passed in `logins`
                    (pr-risk.yml derives it from the minted token's app slug).
                    Bot TYPE alone is not identity: any other GitHub App
                    installed on the repo is also a Bot, and whichever one
                    sorts first by id would be adopted PERMANENTLY — every
                    re-grade PATCHing over that bot's body, or 403ing forever,
                    while the dispute checkbox is read back out of a foreign
                    comment.
      marker at [0] our body is rendered with the marker as its FIRST line. A
                    bot that QUOTES the sticky comment carries the marker too,
                    but nested inside its own prose — which is the case login
                    matching alone still cannot see, because every other
                    `GITHUB_TOKEN` workflow in the repo posts as
                    `github-actions[bot]` as well.

    Scanned NEWEST-FIRST, and the scan is bounded at 10 pages. Ascending order
    made that bound unrecoverable: on a PR with more than 1000 comments the
    sticky is past the cap, so every re-grade POSTs a fresh comment — which
    itself lands past the cap, so the next run does it again, forever, and each
    new body resets the dispute tick. Descending, a comment we create is found
    on page 1 from then on, so the breakage costs at most one duplicate rather
    than one per push.
    """
    allowed = _author_allowed(logins)
    page = 1
    while page <= 10:  # bounded: 10 pages x 100 = 1000 comments is plenty
        comments = call(
            "GET",
            f"/repos/{repo}/issues/{pr}/comments"
            f"?per_page=100&page={page}&sort=created&direction=desc",
            None,
        )
        if not comments:
            return None
        for c in comments:
            user = c.get("user") or {}
            if user.get("type") != "Bot" or user.get("login") not in allowed:
                continue
            if (c.get("body") or "").startswith(MARKER):
                return c
        if len(comments) < 100:
            return None
        page += 1
    return None


def upsert_sticky(
    call, repo: str, pr: int, body: str, logins=(), disputed: bool = False
) -> dict:
    """Create or update the single sticky comment.

    Preserves the reviewer's "this grade is wrong" checkbox across re-grades:
    a push must not silently un-tick a disagreement someone registered. The
    body arrives rendered with an UNCHECKED box, so when the tick is set we
    flip the fresh body to checked before writing it.

    The tick is read from TWO sources, because the comment body alone is racy.
    `find_sticky` reads the body and the PATCH writes it back, so a reviewer
    ticking the box inside that window is overwritten — and the `edited` event
    our own PATCH then fires drives `cmd_dispute` to CLEAR `risk-grade-disputed`,
    turning a lost tick into a lost label:

      `disputed`  the caller's view of the `risk-grade-disputed` label, which
                  `cmd_dispute` already wrote from an earlier tick. Unlike the
                  body, it survives this overwrite, so a dispute registered on
                  any previous run is re-asserted here instead of dropped —
                  and because the body we write is then ticked, the `edited`
                  event it fires reads as still-disputed and the label stands.
      body        re-read by id IMMEDIATELY before the PATCH rather than reused
                  from the `find_sticky` scan, which may be several paginated
                  requests old. That does not make the write atomic — GitHub
                  offers no conditional comment update — but it shrinks the
                  window from the whole scan to a single round trip.
    """
    existing = find_sticky(call, repo, pr, logins)
    if existing:
        seen_body = existing.get("body") or ""
        try:
            fresh = call(
                "GET", f"/repos/{repo}/issues/comments/{existing['id']}", None
            )
            if isinstance(fresh, dict) and fresh.get("body") is not None:
                seen_body = fresh["body"]
        except ApiError:
            # A failed re-read is not a reason to drop the write: fall back to
            # the body the scan already returned.
            pass
        disputed = disputed or bool(CHECKED_RE.search(seen_body))
    if disputed:
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
    current = _paged(call, f"/repos/{repo}/issues/{pr}/labels")
    names = [lbl["name"] for lbl in current]
    if disputed and DISPUTE_LABEL not in names:
        call("POST", f"/repos/{repo}/issues/{pr}/labels", {"labels": [DISPUTE_LABEL]})
        return {"action": "added"}
    if not disputed and DISPUTE_LABEL in names:
        call("DELETE", f"/repos/{repo}/issues/{pr}/labels/{DISPUTE_LABEL}", None)
        return {"action": "removed"}
    return {"action": "unchanged"}


# Whitelist for any value this module renders into markdown. The report
# artifact and the base ref are not attacker-authored, but they are shaped by
# PR content, and everything this module writes lands either in a bot-authored
# comment or in the step summary — where a stray `[`, `<` or newline is enough
# for an inline link, a remote image that logs reviewer IPs, or a forged
# dispute checkbox. Substituting rather than escaping keeps this independent of
# the grader's escape table.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9 ._:/'-]")


def _safe(text, limit: int = 300) -> str:
    """Flatten an untrusted string to punctuation-free, single-line text."""
    return _UNSAFE_RE.sub(" ", str(text))[:limit]


def _malformed_check(detail: str) -> tuple[str, str]:
    """Check Run text for a report this publisher refuses to trust."""
    return (
        "Risk: unknown",
        "The risk report produced for this commit was malformed, so no tier was "
        f"published: {detail}.\n\nNo `risk:*` label was applied — a report this "
        "publisher cannot trust is published as unknown rather than defaulted "
        "to `risk:R0`. This check is advisory and never fails.",
    )


def _malformed_body(detail: str) -> str:
    """Sticky-comment body for a report this publisher refuses to trust.

    Rendered here rather than reusing the grader's renderer because the report
    that would drive it is exactly what has been rejected. It carries the
    marker on its FIRST line (so `find_sticky` still recognises it) and an
    UNTICKED dispute checkbox in the form `UNCHECKED_RE` matches (so
    `upsert_sticky` can still carry a registered dispute across this write
    instead of silently dropping it).
    """
    return (
        "\n".join(
            [
                MARKER,
                "",
                "## ⚪ Risk: **unknown**",
                "",
                "The risk report produced for this commit was malformed, so no "
                f"tier was published: {detail}.",
                "",
                "No `risk:*` label was applied — a report this publisher cannot "
                "trust is published as unknown rather than defaulted to "
                "`risk:R0`. Push again to re-grade.",
                "",
                f"{DISPUTE_LINE} — tick this box if the tier above is off. "
                "Nothing is gated on it either way; ticking labels the PR "
                "`risk-grade-disputed` so the grader can be tuned against real "
                "reviewer disagreement.",
                "",
                "<sub>Advisory only — this check never fails, never blocks "
                "merge, and no automation reads the label. It re-grades on "
                "every push.</sub>",
            ]
        )
        + "\n"
    )


def _summary(line: str) -> None:
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def cmd_publish(args, call) -> int:
    # A corrupt-but-non-empty report is a real artifact state: the grade job's
    # upload is `if-no-files-found: warn` and its shell fallbacks write the
    # report with printf, so a truncated or half-written file reaches here.
    # pr-risk.yml's guard only tests `-s` (non-empty), never well-formedness,
    # so letting JSONDecodeError escape published NOTHING — not even the
    # unknown Check Run the design promises — and left the previous head's
    # `risk:R*` on the PR. Degrade to the same malformed path an out-of-range
    # label already takes.
    report_error = None
    try:
        with open(args.report, encoding="utf-8") as fh:
            report = json.load(fh)
        if not isinstance(report, dict):
            raise ValueError(f"top level is {type(report).__name__}, not an object")
    except (ValueError, OSError) as exc:
        # `_safe` because `detail` is interpolated straight into the Check Run
        # summary and the sticky comment: a decoder message quotes position,
        # not content, but that is a property of today's stdlib, not a
        # guarantee — and everything else rendered here is whitelisted.
        report = {}
        report_error = f"the report artifact could not be read ({_safe(exc, 200)})"

    with open(args.check, encoding="utf-8") as fh:
        check_text = fh.read()
    title, _, summary = check_text.partition("\n\n")
    title, summary = title.strip(), summary.strip()

    body = None
    if args.comment:
        with open(args.comment, encoding="utf-8") as fh:
            body = fh.read()

    logins = getattr(args, "author_login", None) or []

    # Validate the label BEFORE anything is published, not after. The rendered
    # check text and comment body both assert the tier the report claims, so
    # discovering the claim is bogus only at labelling time used to leave three
    # surfaces contradicting each other: no label, but a Check Run and a sticky
    # comment still announcing it. Rejecting the report up here re-renders all
    # three as UNKNOWN together.
    #
    # A report marked `graded` MUST carry a usable label. Testing only
    # `desired is not None` let a graded report with a missing or null `label`
    # slip through the guard entirely: `reconcile_labels(None)` stripped every
    # tier and the summary said the PR could not be graded, while the check
    # text and the comment body — rendered from that same report and never
    # re-rendered as unknown — still announced one. `status` not being
    # `graded` is the ordinary unknown case and stays untouched.
    graded = report.get("status") == "graded"
    desired = report.get("label") if graded else None
    usable = isinstance(desired, str) and bool(APPLICABLE_LABEL_RE.fullmatch(desired))
    if report_error:
        detail = report_error
    elif graded and desired is None:
        detail = "the report is marked graded but carries no label"
    elif graded and not usable:
        detail = (
            f"the report asked for {_safe(repr(desired))}, which is not a "
            "risk:R0..R3 label"
        )
    else:
        detail = None
    if detail:
        _summary(
            f"Report artifact is malformed — {detail}. Publishing the Check Run "
            "and the comment as UNKNOWN, and applying no label."
        )
        desired = None
        title, summary = _malformed_check(detail)
        if body is not None:
            body = _malformed_body(detail)

    # Each publication is attempted INDEPENDENTLY. They are three separate
    # surfaces with three separate failure modes (a missing scope, a secondary
    # rate limit, a 5xx), and letting the first failure abort the rest is how
    # the PR ends up with a Check Run announcing a new tier while the label and
    # the sticky comment still describe the old one — the exact split the
    # tolerant DELETE handler in `reconcile_labels` was written to avoid.
    # Failures are collected and reported, and make this command exit non-zero
    # so pr-risk.yml's "Note degraded mode" step fires. Nothing gates on it:
    # that step is `continue-on-error`, so the job still goes green.
    failures: list[str] = []

    # The Check Run is the audit artifact and publishes in EVERY mode —
    # including shadow, where it is the only output. It is invisible to the
    # PR's label filter and comment thread, so it changes no reviewer-facing
    # behaviour while still recording an immutable, timestamped grade.
    try:
        check = publish_check_run(call, args.repo, args.sha, title, summary)
        _summary(f"Check Run published: {title} (id {check.get('id', '?')})")
    except ApiError as exc:
        failures.append(f"Check Run: {exc}")
        _summary(f"Check Run NOT published: {exc}")

    if args.mode != "publish":
        _summary(
            f"mode=`{args.mode}` (shadow) — Check Run only. No label and no "
            "comment were published. Set `mode: publish` in the caller to put "
            "the tier on the PR."
        )
        return 1 if failures else 0

    # The Check Run above is per-commit and immutable, so it is always safe to
    # write. The label and the sticky comment are not: they describe the PR as
    # it is NOW. `cancel-in-progress` bounds but does not eliminate a delayed
    # older run finishing after a newer one, and `reconcile_labels` is a
    # non-atomic GET-then-DELETE/POST — so a superseded run could republish a
    # stale tier over a fresh one, the exact failure this design forbids.
    base_ref = getattr(args, "base_ref", "") or ""
    live_sha, live_base = current_pr_state(call, args.repo, args.pr)
    stale = ""
    if live_sha and live_sha != args.sha:
        stale = (
            f"this run graded `{args.sha[:12]}` but the PR's head is now "
            f"`{live_sha[:12]}`"
        )
    elif base_ref and live_base and live_base != base_ref:
        # A retarget changes the three-dot diff — and so the grade — without
        # moving the head SHA and without firing `synchronize`.
        stale = (
            f"this run graded against base `{_safe(base_ref)}` but the PR "
            f"now targets `{_safe(live_base)}`"
        )
    if stale:
        _summary(
            f"Superseded: {stale}. The Check Run was published (it is attached "
            "to the commit it graded); the label and the sticky comment were "
            "left to the newer run so a stale tier cannot overwrite a fresh one."
        )
        return 1 if failures else 0

    # Whether a dispute is already on record, read from the label rather than
    # from the comment body — see `upsert_sticky`. Defaults to False when the
    # label read failed, which is the pre-existing behaviour.
    disputed = False
    try:
        lab = reconcile_labels(call, args.repo, args.pr, desired)
        disputed = DISPUTE_LABEL in lab["names"]
        if desired:
            _summary(f"Label: `{desired}` (removed: {lab['removed'] or 'none'})")
        else:
            _summary(
                "Label: none — this PR could not be graded, so it is published "
                "as UNKNOWN rather than defaulted to `risk:R0` (removed: "
                f"{lab['removed'] or 'none'})"
            )
        if lab["failed_removals"]:
            # A stale tier we could not remove leaves the PR carrying TWO
            # `risk:R*` labels — the invariant this module calls load-bearing.
            # Reporting it only in the step summary meant `cmd_publish` still
            # returned 0, so pr-risk.yml's "Note degraded mode" step never
            # fired and the breakage was visible nowhere a reviewer looks.
            failures.append(
                "stale label removal: " + "; ".join(lab["failed_removals"])
            )
            _summary(
                "Warning: these stale risk labels could not be removed, so the "
                f"PR may show more than one tier: {'; '.join(lab['failed_removals'])}"
            )
    except (ApiError, ValueError) as exc:
        failures.append(f"label: {exc}")
        _summary(
            f"Label NOT reconciled: {exc}. The PR may still carry a stale "
            "`risk:R*`; the sticky comment below is published regardless so the "
            "two surfaces do not silently diverge."
        )

    if body is not None:
        try:
            res = upsert_sticky(call, args.repo, args.pr, body, logins, disputed)
            _summary(f"Sticky comment {res['action']}.")
        except ApiError as exc:
            failures.append(f"sticky comment: {exc}")
            _summary(f"Sticky comment NOT published: {exc}")

    if failures:
        _summary(
            "Some surfaces were not published: " + "; ".join(failures) + ". Nothing "
            "is gated on this workflow, so no PR is blocked by it."
        )
        return 1
    return 0


def cmd_dispute(args, call) -> int:
    with open(args.body, encoding="utf-8") as fh:
        body = fh.read()
    if MARKER not in body:
        _summary("Edited comment is not the risk sticky comment — nothing to record.")
        return 0
    # The marker alone is not proof of authorship: it is published in the
    # README and in every rendered comment, so any OTHER bot that quotes our
    # comment (a review summarizer, say) would drive `risk-grade-disputed` —
    # including CLEARING a genuine dispute if the quoted copy shows an unticked
    # box. `find_sticky` was hardened against exactly this; match the id here.
    #
    # An ABSENT id refuses the request rather than falling through to the
    # weaker marker-only check. pr-risk.yml always passes one, so the only way
    # to reach this is a miswired caller — and a security control that
    # degrades to fail-open when its input goes missing is the control failing
    # silently, which is worse than not recording one checkbox toggle.
    if not args.comment_id:
        _summary(
            "No --comment-id was passed, so the edited comment cannot be "
            "matched against this workflow's sticky comment — refusing to "
            "touch `" + DISPUTE_LABEL + "` on marker text alone. This is a "
            "caller bug: pr-risk.yml always passes --comment-id."
        )
        return 0
    sticky = find_sticky(
        call, args.repo, args.pr, getattr(args, "author_login", None) or []
    )
    if not sticky or str(sticky.get("id")) != str(args.comment_id):
        _summary(
            f"Edited comment {_safe(args.comment_id, 40)} carries the marker but "
            "is not this workflow's sticky comment (probably another bot "
            "quoting it) — nothing to record."
        )
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
        "--base-ref",
        default="",
        help="Base BRANCH the grade was computed against. Compared with the "
        "PR's live base so a retarget — which changes the diff without moving "
        "the head SHA or firing `synchronize` — cannot have a grade computed "
        "against the old base published over it.",
    )
    p.add_argument(
        "--author-login",
        action="append",
        default=[],
        help="Extra bot login that may own our sticky comment (repeatable). "
        "`github-actions[bot]` is always accepted; pass the app's "
        "`<slug>[bot]` when publishing under a GitHub App, so another "
        "installed bot's comment can never be adopted as ours.",
    )
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
    d.add_argument(
        "--comment-id",
        default="",
        help="Id of the edited comment. Checked against the sticky comment so "
        "another bot quoting the marker cannot drive the dispute label.",
    )
    d.add_argument(
        "--author-login",
        action="append",
        default=[],
        help="Extra bot login that may own our sticky comment (repeatable).",
    )
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
