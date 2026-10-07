#!/usr/bin/env python3
"""cursor-approve: the live status card on the PR.

ONE issue comment per PR, found by ``CARD_MARKER`` (and authored by the token's
own login, so a comment anyone else plants with the marker is never taken for
it) and edited in place — never reposted. cursor-approve.yml writes it twice a
round:

``start`` (before the axis jobs)
    The round heading, every enabled axis as pending, and a link to the run.
    When cursor-review reported ``approve_gate == capped`` it writes
    "Round limit reached, needs a human" and no table instead.

``ensure`` (decide phase, before the review is posted)
    Print the card's URL, creating it first when the start phase never ran,
    so the approval body can link to it.

``decide`` (after auto-approve.py ``approve-external``)
    The per-axis verdicts from ``aggregate.py decide``, the overall result and
    the rule applied — or "Superseded by a newer commit" when the head moved.

Every model-supplied string (an axis summary, a reason that echoes one) goes
through ``sanitize`` — post-review.py's ``neutralize_mentions`` plus escaping of
every character that could open markdown or HTML — so a summary cannot add a
heading, fire a mention or forge the card marker.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys

CARD_MARKER = "<!-- cursor-approve-card -->"
SUMMARY_LIMIT = 200
AXES = ("business", "design", "correctness", "completeness", "conformance")
VERDICT_ICONS = {"green": "🟢 green", "yellow": "🟡 yellow", "red": "🔴 red"}
NO_RESULT = "⚠️ no result"
SHA_RE = re.compile(r"[0-9a-f]{40}")

# Outcomes auto-approve.py `approve-external` reports; anything else renders as
# an unknown outcome rather than being trusted.
OUTCOME_APPROVED = "approved"
OUTCOME_NOT_APPROVED = "not_approved"
OUTCOME_SUPERSEDED = "superseded"
OUTCOME_HUMAN = "needs_human"
OUTCOME_OWN_PR = "own_pr"
OUTCOME_ERROR = "error"

# Markdown/HTML-significant characters, backslash-escaped. `<` alone would be
# enough to kill a forged `<!-- cursor-approve-card -->`; the rest stop a
# summary from opening emphasis, links, code spans or a new table cell.
_ESCAPE_RE = re.compile(r"([\\`*_{}\[\]()#+\-!|<>~=])")


def _load_post_review():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cursor-review", "post-review.py")
    spec = importlib.util.spec_from_file_location("post_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_NEUTRALIZE = None


def neutralize_mentions(text: str) -> str:
    global _NEUTRALIZE
    if _NEUTRALIZE is None:
        _NEUTRALIZE = _load_post_review().neutralize_mentions
    return _NEUTRALIZE(text)


def first_sentence(text: str) -> str:
    text = " ".join(str(text or "").split())
    match = re.search(r"[.!?](\s|$)", text)
    return text[: match.start() + 1] if match else text


def sanitize(text, limit: int = SUMMARY_LIMIT) -> str:
    """One line of model text, safe to drop into a markdown table cell."""
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    text = neutralize_mentions(text)
    text = text.replace("&", "&amp;")
    return _ESCAPE_RE.sub(r"\\\1", text)


def parse_axes(value: str) -> list:
    axes = []
    for name in (value or "").split(","):
        name = name.strip().lower()
        if name in AXES and name not in axes:
            axes.append(name)
    return axes


def _int_or_q(value) -> str:
    text = str(value or "").strip()
    # A `type: number` input can render as `0.0`; show it as the integer it is.
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text if text.isdigit() else "?"


def heading(round_no, max_rounds, sha: str) -> str:
    short = sha[:7] if SHA_RE.fullmatch(sha or "") else "unknown"
    return f"### Cursor approve · round {_int_or_q(round_no)} of {_int_or_q(max_rounds)} · `{short}`"


def _run_link(run_url: str) -> str:
    if re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_./-]+", run_url or ""):
        return f"[workflow run]({run_url})"
    return "workflow run"


def render_start(round_no, max_rounds, sha: str, axes: list, approve_gate: str, run_url: str) -> str:
    lines = [CARD_MARKER, heading(round_no, max_rounds, sha), ""]
    if approve_gate == "capped":
        lines.append("**Round limit reached, needs a human.**")
    else:
        lines += ["| Axis | Verdict | Confidence | Summary |", "|---|---|---|---|"]
        lines += [f"| {axis} | ⏳ pending | | |" for axis in axes]
    lines += ["", f"_{_run_link(run_url)}_"]
    return "\n".join(lines) + "\n"


def _confidence(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
        return f"{value:.2f}"
    return ""


def render_decide(round_no, max_rounds, sha: str, axes: list, decision, outcome: str,
                  max_yellow: str, run_url: str) -> str:
    lines = [CARD_MARKER, heading(round_no, max_rounds, sha), ""]
    if outcome == OUTCOME_SUPERSEDED:
        lines += ["**Superseded by a newer commit.**", "", f"_{_run_link(run_url)}_"]
        return "\n".join(lines) + "\n"
    detail = decision.get("axes") if isinstance(decision, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    lines += ["| Axis | Verdict | Confidence | Summary |", "|---|---|---|---|"]
    for axis in axes:
        entry = detail.get(axis)
        verdict = entry.get("verdict") if isinstance(entry, dict) else None
        if verdict not in VERDICT_ICONS:
            lines.append(f"| {axis} | {NO_RESULT} | | |")
            continue
        summary = sanitize(first_sentence(entry.get("summary")))
        lines.append(f"| {axis} | {VERDICT_ICONS[verdict]} | {_confidence(entry.get('confidence'))} | {summary} |")
    lines.append("")
    reasons = decision.get("reasons") if isinstance(decision, dict) else None
    reasons = [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else []
    if outcome == OUTCOME_APPROVED:
        lines.append("**Result: ✅ Approved.**")
    else:
        why = {
            OUTCOME_HUMAN: ["the PR is labelled `needs-human-review`"],
            OUTCOME_OWN_PR: ["the approver authored this PR"],
            OUTCOME_ERROR: ["the approval could not be posted (see the workflow run)"],
        }.get(outcome)
        if why is None:
            why = reasons if outcome == OUTCOME_NOT_APPROVED and reasons else ["no decision was reached"]
            why = [sanitize(r, 300) for r in why]
        lines.append("**Result: ❌ Not approved.**")
        lines += [f"- {r}" for r in why]
    limit = _int_or_q(max_yellow)
    lines += ["", f"_Rule: no red, at most {limit} yellow; every axis must report. {_run_link(run_url)}_"]
    return "\n".join(lines) + "\n"


def gh(args: list, payload=None) -> str:
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


def find_card(comments: list, login: str):
    """The card comment authored by `login`, or None. The first one wins."""
    for c in comments:
        if not isinstance(c, dict):
            continue
        author = ((c.get("user") or {}).get("login") or "").lower()
        if author == (login or "").lower() and (c.get("body") or "").startswith(CARD_MARKER):
            return c
    return None


def list_comments(repo: str, pr_number) -> list:
    pages = json.loads(gh(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{pr_number}/comments?per_page=100"]))
    return [c for page in pages for c in page] if pages and isinstance(pages[0], list) else pages


def upsert(repo: str, pr_number, login: str, body: str) -> dict:
    """Edit the existing card in place, or create it when there is none."""
    card = find_card(list_comments(repo, pr_number), login)
    if card is not None:
        return json.loads(gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{card['id']}", "--input", "-"],
                             {"body": body}))
    return json.loads(gh(["api", "-X", "POST", f"repos/{repo}/issues/{pr_number}/comments", "--input", "-"],
                         {"body": body}))


def _write_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("start", "ensure", "decide"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--pr-number", required=True)
        p.add_argument("--login", required=True)
        p.add_argument("--commit-sha", required=True)
        p.add_argument("--round", default="")
        p.add_argument("--max-rounds", default="")
        p.add_argument("--axes", required=True)
        p.add_argument("--run-url", default="")
        if name == "start":
            p.add_argument("--approve-gate", default="")
        if name == "decide":
            p.add_argument("--decision", required=True)
            p.add_argument("--outcome", required=True)
            p.add_argument("--max-yellow-axes", default="0")
    args = parser.parse_args(argv)
    # An empty login matches no comment, so every phase would post a fresh card.
    if not (args.login or "").strip():
        print("::error::--login is empty; refusing to post a card that can never be found again")
        return 2
    axes = parse_axes(args.axes)
    if args.cmd == "start":
        body = render_start(args.round, args.max_rounds, args.commit_sha, axes, args.approve_gate, args.run_url)
    elif args.cmd == "ensure":
        try:
            card = find_card(list_comments(args.repo, args.pr_number), args.login)
            if card is None:
                body = render_start(args.round, args.max_rounds, args.commit_sha, axes, "", args.run_url)
                card = upsert(args.repo, args.pr_number, args.login, body)
        except (RuntimeError, ValueError) as e:
            print(f"::warning::Could not find or create the cursor-approve card: {e}")
            return 0
        _write_output("card_url", card.get("html_url") or "")
        return 0
    else:
        try:
            with open(args.decision, encoding="utf-8") as f:
                decision = json.load(f)
        except (OSError, ValueError):
            decision = {}
        body = render_decide(args.round, args.max_rounds, args.commit_sha, axes, decision, args.outcome,
                             args.max_yellow_axes, args.run_url)
    try:
        upsert(args.repo, args.pr_number, args.login, body)
    except (RuntimeError, ValueError) as e:
        print(f"::warning::Could not update the cursor-approve card: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
