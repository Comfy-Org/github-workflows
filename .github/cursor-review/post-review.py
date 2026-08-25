#!/usr/bin/env python3
"""Post a single consolidated cursor-review to a GitHub PR.

The consolidate step produces one findings file (output of the judge call,
augmented with panel metadata). This script reads that file and posts ONE
PR review with line-anchored inline comments.

Findings file shape:
    {
        "findings": [
            {"file": str, "line": int, "side": "RIGHT", "severity": str, "body": str},
            ...
        ],
        "panel": [
            {"model": str, "review_type": str, "status": "ok"|"error"},
            ...
        ]
    }

Given `--diff`, findings whose line is not in the reviewed diff are rendered in
the review BODY and the rest still anchor. Without it (or if the diff cannot be
read) every finding is sent inline, as before.

Falls back to a body-only review (no inline anchors) if GitHub rejects the
inline payload anyway — the API is all-or-nothing, so one bad position costs
every anchor in the request.
"""

import argparse
import json
import os
import re
import subprocess
import sys

# Severity scale, ordered most → least urgent. Drives sort order, the inline
# comment prefix, and the summary table. The judge tool accepts one
# of these strings per finding (see prompt-judge.md); anything missing or
# unrecognized falls back to DEFAULT_SEVERITY so a malformed value can never
# drop a finding — it just lands in the middle bucket.
SEVERITY_ORDER = ["critical", "high", "medium", "low", "nit"]
SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
    "nit": "⚪",
}
SEVERITY_LABEL = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "nit": "Nit",
}
DEFAULT_SEVERITY = "medium"

# Max re-raises of an already-answered finding allowed in one review (BE-5109).
# Nothing already-answered is ever silently suppressed — a wrong or premature
# deferral must not be able to permanently bury a real Critical — but a round is
# never allowed to be all re-litigation, so the panel gets at most this many
# re-raises, on the record, with the link. Extras are dropped loudly (the review
# body says how many). Kept in sync with REPEAT_CAP in build-ledger.py, which is
# what the judge prompt block quotes.
REPEAT_CAP = 2


def normalize_severity(value) -> str:
    """Coerce a model-supplied severity into one of SEVERITY_ORDER.

    Tolerant by design: unknown, missing, or non-string values become
    DEFAULT_SEVERITY rather than dropping the finding.
    """
    if not isinstance(value, str):
        return DEFAULT_SEVERITY
    candidate = value.strip().lower()
    return candidate if candidate in SEVERITY_EMOJI else DEFAULT_SEVERITY


def severity_rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(SEVERITY_ORDER)


def build_severity_summary(enriched: list[dict]) -> str:
    """Render a CodeRabbit-style severity breakdown table, highest first.

    Only severities that actually occur get a row, so a PR with three nits
    doesn't carry four empty rows of ceremony.
    """
    counts: dict[str, int] = {}
    for item in enriched:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    rows = [
        f"| {SEVERITY_EMOJI[sev]} {SEVERITY_LABEL[sev]} | {counts[sev]} |"
        for sev in SEVERITY_ORDER
        if counts.get(sev)
    ]
    if not rows:
        return ""
    return "| Severity | Count |\n| --- | --- |\n" + "\n".join(rows)


def neutralize_mentions(text: str) -> str:
    """Insert ZWSP after each `@` so model output can't trigger GitHub mentions."""
    return str(text).replace("@", "@\u200B")


def gh_post_review(repo: str, pr_number: str, payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            "--input",
            "-",
        ],
        input=payload,
        text=True,
        capture_output=True,
    )


def is_read_only_token_error(result: subprocess.CompletedProcess) -> bool:
    """True when the POST failed because the token can't write to the PR.

    The gate skips fork PRs (which always hit this), but a read-only token can
    still occur on same-repo runs — org/repo default workflow permissions set
    to read-only, or events that downgrade the token. GitHub answers those with
    HTTP 403 'Resource not accessible by integration'. That's an environment
    constraint, not a review failure, so callers degrade to the job summary
    rather than failing the check red.
    """
    blob = result.stderr or ""
    return "Resource not accessible by integration" in blob or "HTTP 403" in blob


def write_step_summary(markdown: str) -> None:
    """Render the review into the Actions run summary as a no-write fallback.

    Used when the PR can't be written to (read-only token): the content is
    still delivered — in the run's Summary tab — instead of being lost.
    """
    note = (
        "> ℹ️ This review could not be posted on the PR because the run's "
        "`GITHUB_TOKEN` is read-only (e.g. read-only default workflow "
        "permissions). Posting it here instead.\n\n"
    )
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        # No summary file (e.g. a local run) — fall back to stdout so the
        # content isn't silently dropped.
        print(note + markdown)
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(note + markdown + "\n")


def post_or_degrade(repo, pr_number, payload, summary_markdown, context) -> bool:
    """POST a review; degrade to the step summary on a read-only token.

    Returns True when the review was delivered — either posted on the PR, or
    (when the token is read-only) written to the job step summary. Returns
    False only on a genuine POST failure the caller should handle itself
    (e.g. retry without inline anchors).
    """
    result = gh_post_review(repo, pr_number, payload)
    if result.returncode == 0:
        return True
    if is_read_only_token_error(result):
        print(
            f"{context}: token is read-only — writing the review to the job "
            "summary instead of the PR.",
            file=sys.stderr,
        )
        write_step_summary(summary_markdown)
        return True
    print(f"{context} POST failed: {result.stderr}", file=sys.stderr)
    return False


def anchorable_lines(diff_text: str) -> dict:
    """Map new-side path -> the set of line numbers a RIGHT-side comment may anchor to.

    GitHub accepts a review comment only on a line the diff actually carries — added
    or context, inside a hunk. Anything else is rejected, and the rejection is
    WHOLESALE: `POST /pulls/{n}/reviews` takes the comments array as one unit, so a
    single out-of-range position costs every anchor in the request (observed in the
    field: 10 findings, 1 line outside the hunks, 0 comments anchored, HTTP 422).
    Parsing the diff up front lets the 9 good ones land.

    Deliberately a hand-rolled scan, matching build-ledger.py/fence-diff.py: the
    consumers run on a stock runner with no third-party diff library available.
    """
    anchors: dict = {}
    path = None
    right = 0
    # splitlines(), not split("\n"): the latter yields a trailing "" for the diff's final
    # newline, which the blank-line arm below would then number as one line past the end of
    # the last hunk — an anchor GitHub rejects, i.e. the 422 this function exists to prevent.
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            # `+++ b/path`, or `+++ /dev/null` for a delete (no new side at all).
            target = raw[4:].strip()
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith(("a/", "b/")) else target
                anchors.setdefault(path, set())
            continue
        if raw.startswith("@@"):
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            # An unparseable hunk header means the following lines cannot be numbered.
            # Drop the file rather than number them from a guess: a WRONG anchor set is
            # worse than none, since it would send back the 422 this exists to avoid.
            right = int(m.group(1)) if m else 0
            if not m:
                path = None
            continue
        if path is None or right == 0:
            continue
        if raw.startswith("+") or raw.startswith(" "):
            anchors[path].add(right)
            right += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            # Removed lines advance only the OLD side; `\ No newline at end of file`
            # is a marker, not content.
            continue
        else:
            # Anything else inside a hunk is a line this parser cannot number: a bare ""
            # (unified diff renders a context blank as " ", so a truly empty line means
            # the text has been through something lossy) or a stray non-diff line. Drop
            # the FILE rather than guess, because from here on every number would be a
            # guess, and a wrong anchor is what sends back the 422. The cost of dropping
            # is that the file's findings render in the body — visible, not lost.
            path = None
    return anchors


def partition_by_anchor(enriched: list, anchors) -> tuple:
    """Split enriched findings into (inline, body_only) against the diff's anchors.

    `anchors` of None means "no diff was supplied" — everything stays inline, which
    is the pre-existing behaviour and the fail-OPEN direction: a diff we could not
    read must never cost a finding its anchor on a PR where it would have worked.
    """
    if anchors is None:
        return list(enriched), []
    inline, body_only = [], []
    for item in enriched:
        c = item["comment"]
        if c["line"] in anchors.get(c["path"], set()):
            inline.append(item)
        else:
            body_only.append(item)
    return inline, body_only


def load_anchors(diff_path):
    """Read the reviewed diff and build its anchor map, or None if unusable.

    Every failure lands on None (= keep today's all-inline behaviour) and says so on
    stderr. The one thing this must not do is return a PARTIAL map on a read error:
    that would silently demote real anchors to prose.
    """
    if not diff_path:
        return None
    try:
        with open(diff_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"Anchors: cannot read diff {diff_path} ({e}) — sending all findings inline.", file=sys.stderr)
        return None
    if not text.strip():
        print(f"Anchors: diff {diff_path} is empty — sending all findings inline.", file=sys.stderr)
        return None
    anchors = anchorable_lines(text)
    if not anchors:
        print(f"Anchors: no hunks parsed from {diff_path} — sending all findings inline.", file=sys.stderr)
        return None
    return anchors


def render_body_only_findings(items: list) -> str:
    """Render findings that could not be anchored, for inclusion in the review body."""
    if not items:
        return ""
    md = (
        "_The finding(s) below cite a line outside the reviewed diff, so they could "
        "not be anchored inline:_\n\n"
    )
    for item in items:
        c = item["comment"]
        md += f"**`{c['path']}:{c['line']}`** — {c['body']}\n\n"
    return md.rstrip("\n")


def render_findings_markdown(review_body: str, comments: list[dict]) -> str:
    """Flatten the review body + inline comments into one markdown blob.

    Inline review comments don't render in a step summary, so list them
    underneath the body when degrading to the summary or a body-only review.
    """
    md = review_body
    if comments:
        md += "\n\n---\n\n"
        for c in comments:
            md += f"**`{c['path']}:{c['line']}`** — {c['body']}\n\n"
    return md


def build_panel_summary(panel: list[dict]) -> str:
    if not panel:
        return ""
    ok = sum(1 for c in panel if c.get("status") == "ok")
    failed = [c for c in panel if c.get("status") != "ok"]
    parts = [f"_Panel: {ok}/{len(panel)} reviewers contributed findings._"]
    if failed:
        names = ", ".join(
            f"{c.get('model','?')}:{c.get('review_type','?')} ({c.get('status','?')})"
            for c in failed
        )
        parts.append(f"_Reviewers that did not contribute: {names}_")
    return "\n\n".join(parts)


def normalize_comments(findings: list[dict]) -> list[dict]:
    """Build sorted, severity-tagged inline comments from raw judge findings.

    Returns a list of {"severity": str, "comment": dict} entries sorted most
    → least urgent. The nested `comment` is the GitHub review-comment payload
    (path/line/side/body) with the severity badge prefixed into the body;
    severity is kept alongside (not inside) so the summary table can count it
    without leaking an unknown key into the GitHub API request.
    """
    enriched = []
    for finding in findings:
        if not isinstance(finding, dict):
            print(f"Skipping non-dict finding: {finding!r}", file=sys.stderr)
            continue
        path = finding.get("file", "")
        line = finding.get("line")
        body = finding.get("body", "")
        if not path or not line or not body:
            continue
        try:
            line_int = int(line)
        except (TypeError, ValueError):
            print(f"Skipping non-integer line {line!r} for {path}", file=sys.stderr)
            continue
        if line_int <= 0:
            print(f"Skipping non-positive line {line_int} for {path}", file=sys.stderr)
            continue
        severity = normalize_severity(finding.get("severity"))
        badge = f"{SEVERITY_EMOJI[severity]} **{SEVERITY_LABEL[severity]}** — "
        repeat_line = render_repeat_of(finding)
        enriched.append(
            {
                "severity": severity,
                # Truthy only for a re-raise of an already-answered finding —
                # what enforce_repeat_cap counts against REPEAT_CAP.
                "repeat_of": repeat_line,
                "comment": {
                    "path": path,
                    "line": line_int,
                    "side": "RIGHT",
                    "body": badge + neutralize_mentions(body) + repeat_line,
                },
            }
        )
    enriched.sort(key=lambda item: severity_rank(item["severity"]))
    return enriched


def render_repeat_of(finding: dict) -> str:
    """Render the re-raise line for a finding the judge marked as a repeat.

    `repeat_of` is the prior round's `discussion_url` from the ledger. Showing
    it inline is the whole point of the repeat policy: a re-raise happens on the
    record, linked to the thread that already answered it, so the author can see
    at a glance that this is round N of the same conversation.
    """
    url = finding.get("repeat_of")
    if not isinstance(url, str) or not url.strip():
        return ""
    return f"\n\n↩︎ re-raise of {neutralize_mentions(url.strip())}{render_repeat_round(finding)}"


def render_repeat_round(finding: dict) -> str:
    """Render the ``(round N)`` suffix, or nothing.

    This is judge output and the judge reads the ledger — untrusted PR text — so
    the field is model-relayed content like any other body. The control that
    actually holds is the type: a *positive integer* can't carry an `@handle` or
    markup at all, unlike the previous "int or str" check, which passed arbitrary
    text through to the rendered comment. (That check also admitted `bool`, a
    subclass of `int`, so `repeat_round: true` rendered as "(round True)".)
    neutralize_mentions stays on the render as defense in depth for whoever
    loosens the type next.
    """
    round_no = finding.get("repeat_round")
    if isinstance(round_no, bool):
        return ""
    if isinstance(round_no, str):
        round_no = round_no.strip()
        if not round_no.isdigit():
            return ""
        round_no = int(round_no)
    if not isinstance(round_no, int) or round_no <= 0:
        return ""
    return f" (round {neutralize_mentions(str(round_no))})"


def enforce_repeat_cap(enriched: list[dict], cap: int = REPEAT_CAP) -> tuple[list[dict], int]:
    """Keep at most `cap` re-raises, most severe first; report how many were cut.

    Enforced here rather than trusted to the judge: the cap is a hard property of
    the review, and a model that emits five re-raises should not be able to turn
    a round into pure re-litigation. `enriched` is already severity-sorted, so
    the survivors are the most severe repeats.
    """
    kept, dropped = [], 0
    repeats = 0
    for item in enriched:
        if item.get("repeat_of"):
            if repeats >= cap:
                dropped += 1
                continue
            repeats += 1
        kept.append(item)
    return kept, dropped


def post_error_review(repo, pr_number, commit_sha, header, error_message):
    safe = neutralize_mentions(error_message)
    body_text = (
        f"{header}\n\n⚠️ **Review failed**\n\n```\n{safe}\n```\n\n"
        "Re-trigger by removing and re-adding the `cursor-review` label."
    )
    payload = json.dumps(
        {"body": body_text, "event": "COMMENT", "commit_id": commit_sha}
    )
    if not post_or_degrade(repo, pr_number, payload, body_text, "Error review"):
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--findings", required=True, help="Path to consolidated findings JSON")
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument(
        "--diff",
        default=None,
        help=(
            "Path to the reviewed diff. Findings citing a line the diff does not carry "
            "are rendered in the review body so the rest still anchor inline. Omitted "
            "or unreadable means every finding is sent inline (pre-existing behaviour)."
        ),
    )
    parser.add_argument("--triggered-by", default=None)
    parser.add_argument("--error-message", default=None, help="If set, post an error review with this message")
    parser.add_argument(
        "--notice",
        default=None,
        help="Banner prepended to the review body (e.g. a judge-failed degradation note).",
    )
    parser.add_argument(
        "--ledger-note",
        default=None,
        help=(
            "Prior-review ledger line for the header — either the round/ledger summary "
            "or the 'context unavailable' banner. Empty/absent renders nothing."
        ),
    )
    args = parser.parse_args()

    attribution = f"\n\n_Triggered by @{args.triggered_by}._" if args.triggered_by else ""
    header = f"## 🔍 Cursor Review — Consolidated panel{attribution}"
    if args.notice:
        # Surface a degradation banner (judge failed → raw panel findings) right
        # under the title so every rendered body carries it.
        header += f"\n\n{neutralize_mentions(args.notice)}"
    if args.ledger_note and args.ledger_note.strip():
        # Either "Round N — ledger: …" or the ledger-unavailable banner. The
        # banner case matters most: a re-review that ran WITHOUT prior context
        # must never look identical to a genuine first-round review.
        header += f"\n\n_{neutralize_mentions(args.ledger_note.strip())}_"

    if args.error_message:
        post_error_review(args.repo, args.pr_number, args.commit_sha, header, args.error_message)
        return

    try:
        with open(args.findings, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        post_error_review(
            args.repo,
            args.pr_number,
            args.commit_sha,
            header,
            f"Could not load findings file: {e}",
        )
        return

    findings = data.get("findings", []) or []
    panel = data.get("panel", []) or []
    panel_summary = build_panel_summary(panel)

    if not findings:
        # Distinguish two cases that both produce zero findings:
        # 1. Panel ran, judge picked nothing → genuinely no high-signal issues.
        # 2. Every panel cell errored → judge was skipped, no judging happened.
        # Headlining (1) and (2) the same way ("No high-signal findings") is
        # misleading on (2), so check the panel metadata explicitly.
        all_failed = bool(panel) and all(c.get("status") != "ok" for c in panel)
        if all_failed:
            body_text = (
                f"{header}\n\n⚠️ **Panel did not produce any findings.**\n\n"
                "Every reviewer in the matrix failed to contribute — see the "
                "panel summary for which cells errored, and the run logs for "
                "the underlying cause."
            )
        else:
            body_text = f"{header}\n\n✅ No high-signal findings."
        if panel_summary:
            body_text += f"\n\n{panel_summary}"
        payload = json.dumps(
            {"body": body_text, "event": "COMMENT", "commit_id": args.commit_sha}
        )
        if not post_or_degrade(
            args.repo, args.pr_number, payload, body_text, "No-findings review"
        ):
            raise SystemExit(1)
        return

    enriched = normalize_comments(findings)
    enriched, repeats_dropped = enforce_repeat_cap(enriched)
    # Anchor-aware split. The COUNT below stays the total across both halves — a finding
    # that lands in the body is still a finding, and a headline that shrank because an
    # anchor missed would misreport the review.
    inline_items, body_only_items = partition_by_anchor(enriched, load_anchors(args.diff))
    comments = [item["comment"] for item in inline_items]

    review_body = f"{header}\n\nFound **{len(enriched)}** finding(s)."
    if repeats_dropped:
        review_body += (
            f"\n\n_{repeats_dropped} re-raise(s) of already-answered findings were dropped "
            f"(cap: {REPEAT_CAP} per review). They are still open on their original threads._"
        )
    severity_summary = build_severity_summary(enriched)
    if severity_summary:
        review_body += f"\n\n{severity_summary}"
    if panel_summary:
        review_body += f"\n\n{panel_summary}"
    if not enriched and findings:
        review_body += "\n\n_(All findings had invalid file/line references and were dropped.)_"
    body_only_md = render_body_only_findings(body_only_items)
    if body_only_md:
        review_body += f"\n\n---\n\n{body_only_md}"

    payload = json.dumps(
        {
            "body": review_body,
            "event": "COMMENT",
            "commit_id": args.commit_sha,
            "comments": comments,
        }
    )

    result = gh_post_review(args.repo, args.pr_number, payload)

    if result.returncode != 0:
        # A read-only token rejects any write, so the inline-less fallback below
        # would fail the same way — degrade straight to the job summary instead.
        if is_read_only_token_error(result):
            print(
                "Review: token is read-only — writing the review to the job "
                "summary instead of the PR.",
                file=sys.stderr,
            )
            write_step_summary(render_findings_markdown(review_body, comments))
            return

        print(f"Review POST failed: {result.stderr}", file=sys.stderr)
        # Fallback: same body without inline anchors. Typical cause is line
        # numbers that fall outside the diff context — often the model picked
        # a line near the change but not on the change.
        fallback_body = render_findings_markdown(review_body, comments)
        fallback_body += "\n_(Inline comments could not be anchored to the diff; listed above instead.)_"

        fallback_payload = json.dumps(
            {
                "body": fallback_body,
                "event": "COMMENT",
                "commit_id": args.commit_sha,
            }
        )
        if not post_or_degrade(
            args.repo, args.pr_number, fallback_payload, fallback_body, "Fallback review"
        ):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
