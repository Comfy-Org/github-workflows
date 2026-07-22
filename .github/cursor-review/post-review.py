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
            {"model": str, "review_type": str, "status": "ok"|"empty"|"error"|"parse_error"},
            ...
        ]
    }

Falls back to a body-only review (no inline anchors) if GitHub rejects the
inline payload — typical cause is line numbers that don't match the diff.
"""

import argparse
import json
import os
import subprocess
import sys

# Severity scale, ordered most → least urgent. Drives sort order, the inline
# comment prefix, and the summary table. The judge is instructed to emit one
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
        enriched.append(
            {
                "severity": severity,
                "comment": {
                    "path": path,
                    "line": line_int,
                    "side": "RIGHT",
                    "body": badge + neutralize_mentions(body),
                },
            }
        )
    enriched.sort(key=lambda item: severity_rank(item["severity"]))
    return enriched


def post_error_review(repo, pr_number, commit_sha, header, error_message):
    safe = neutralize_mentions(error_message)
    body_text = (
        f"{header}\n\n⚠️ **Review failed**\n\n```\n{safe}\n```\n\n"
        "Re-trigger by removing and re-adding the active Cursor Review label."
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
    parser.add_argument("--triggered-by", default=None)
    parser.add_argument("--error-message", default=None, help="If set, post an error review with this message")
    parser.add_argument(
        "--notice",
        default=None,
        help="Banner prepended to the review body (e.g. a judge-failed degradation note).",
    )
    args = parser.parse_args()

    attribution = f"\n\n_Triggered by @{args.triggered_by}._" if args.triggered_by else ""
    header = f"## 🔍 Cursor Review — Consolidated panel{attribution}"
    if args.notice:
        # Surface a degradation banner (judge failed → raw panel findings) right
        # under the title so every rendered body carries it.
        header += f"\n\n{neutralize_mentions(args.notice)}"

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
    comments = [item["comment"] for item in enriched]

    review_body = f"{header}\n\nFound **{len(comments)}** finding(s)."
    severity_summary = build_severity_summary(enriched)
    if severity_summary:
        review_body += f"\n\n{severity_summary}"
    if panel_summary:
        review_body += f"\n\n{panel_summary}"
    if not comments and findings:
        review_body += "\n\n_(All findings had invalid file/line references and were dropped.)_"

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
