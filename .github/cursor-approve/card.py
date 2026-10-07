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

cursor-review's ``auto-approve.py decide`` writes it too, through
``render_round`` and ``upsert``, on every round it decides for an author
auto-approve applies to (BE-19489): ``pass``, ``changes_requested``,
``no_decision`` and ``capped`` alike. Only that job has the round's gating
findings, its threads and ``decide_gate``'s reasons in hand, and it runs
whatever the outcome, while every phase here runs only after a ``pass`` (or a
``capped``) the caller's ``if:`` let through. On a ``pass`` the start phase
then overwrites it with the axes table, as before.

Every card carries two machine-readable markers on the lines right after
``CARD_MARKER`` — ``STATE_MARKER`` and ``NEXT_MARKER`` — a stable contract
for agents, documented in docs/callers/cursor-approve.md.

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
OUTCOME_VETOED = "vetoed"
OUTCOME_OWN_PR = "own_pr"
OUTCOME_ERROR = "error"

# The machine-readable contract (docs/callers/cursor-approve.md). Values are
# never renamed: agents key on them.
STATE_PASS = "pass"
STATE_CHANGES = "changes_requested"
STATE_NO_DECISION = "no_decision"
STATE_CAPPED = "capped"
STATES = (STATE_PASS, STATE_CHANGES, STATE_NO_DECISION, STATE_CAPPED)
NEXT_NONE = "none"
NEXT_RESOLVE = "resolve_then_relabel"
NEXT_RELABEL = "relabel"
NEXT_HUMAN = "human"
NEXTS = (NEXT_NONE, NEXT_RESOLVE, NEXT_RELABEL, NEXT_HUMAN)
STATE_MARKER = "<!-- cursor-approve-state: {} -->"
NEXT_MARKER = "<!-- cursor-approve-next: {} -->"

DEFAULT_REVIEW_LABEL = "cursor-review"


def safe_label(label) -> str:
    """cursor-review's `review_label` for a next-step line: the default when
    none is given, "" when it is not a plain label name.

    A caller input, but it lands in a code span on a comment posted as the
    approver: only a plain label name is echoed, never markdown or a marker.
    Anything else is NOT replaced by the default — that would name a label the
    repo may not have — the line then says "the review label".
    """
    label = str(label or "").strip()
    if not label:
        return DEFAULT_REVIEW_LABEL
    return label if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.:/+-]{0,49}", label) else ""


def relabel(label=DEFAULT_REVIEW_LABEL) -> str:
    name = safe_label(label)
    return f"remove and re-add the `{name}` label" if name else "remove and re-add the review label"


def next_resolve_text(label=DEFAULT_REVIEW_LABEL) -> str:
    return f"Fix or reply to the gating threads, resolve them, then start a new round: {relabel(label)}."


def next_relabel_text(label=DEFAULT_REVIEW_LABEL) -> str:
    return f"Re-run the round: {relabel(label)}."


# The default-label texts. cursor-approve.yml has no `review_label` input, so
# its own phases always use these; cursor-review's decide passes its input.
RELABEL = relabel()
NEXT_RESOLVE_TEXT = next_resolve_text()
NEXT_RELABEL_TEXT = next_relabel_text()
NEXT_HUMAN_CAPPED_TEXT = "A human is needed: review the PR, then remove the `needs-human-review` label to reset the round count."

# How approve-external's outcome maps onto the contract on the decide card.
DECIDE_STATES = {
    OUTCOME_APPROVED: (STATE_PASS, NEXT_NONE),
    OUTCOME_NOT_APPROVED: (STATE_CHANGES, NEXT_RELABEL),
    OUTCOME_SUPERSEDED: (STATE_NO_DECISION, NEXT_RELABEL),
    OUTCOME_HUMAN: (STATE_CAPPED, NEXT_HUMAN),
    OUTCOME_VETOED: (STATE_NO_DECISION, NEXT_HUMAN),
    OUTCOME_OWN_PR: (STATE_NO_DECISION, NEXT_HUMAN),
    OUTCOME_ERROR: (STATE_NO_DECISION, NEXT_RELABEL),
}
DECIDE_NEXT_TEXT = {
    OUTCOME_NOT_APPROVED: f"Address the axis verdicts above, push, then start a new round: {RELABEL}.",
    OUTCOME_SUPERSEDED: f"A newer commit needs its own round: {RELABEL}.",
    OUTCOME_HUMAN: NEXT_HUMAN_CAPPED_TEXT,
    OUTCOME_VETOED: "A human is needed: the PR carries `skip-cursor-review`.",
    OUTCOME_OWN_PR: "A human is needed: the approver cannot approve its own PR.",
    OUTCOME_ERROR: f"Re-run the round: {RELABEL}.",
}

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


def card_head(state: str, next_step: str) -> list:
    """The card marker, then the two contract markers. An unknown value is a
    bug here, never something to publish: agents act on these."""
    if state not in STATES or next_step not in NEXTS:
        raise ValueError(f"unknown card state/next: {state!r}/{next_step!r}")
    return [CARD_MARKER, STATE_MARKER.format(state), NEXT_MARKER.format(next_step)]


def _reviewed_line(sha: str, run_url: str) -> str:
    reviewed = f"`{sha}`" if SHA_RE.fullmatch(sha or "") else "unknown"
    return f"_Reviewed commit: {reviewed} · {_run_link(run_url)}_"


def render_start(round_no, max_rounds, sha: str, axes: list, approve_gate: str, run_url: str) -> str:
    capped = approve_gate == "capped"
    lines = card_head(STATE_CAPPED if capped else STATE_PASS, NEXT_HUMAN if capped else NEXT_NONE)
    lines += [heading(round_no, max_rounds, sha), ""]
    if capped:
        lines.append("**Round limit reached, needs a human.**")
        lines += ["", f"**Next step:** {NEXT_HUMAN_CAPPED_TEXT}"]
    else:
        lines += ["| Axis | Verdict | Confidence | Summary |", "|---|---|---|---|"]
        lines += [f"| {axis} | ⏳ pending | | |" for axis in axes]
    lines += ["", _reviewed_line(sha, run_url)]
    return "\n".join(lines) + "\n"


def _confidence(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
        return f"{value:.2f}"
    return ""


def render_decide(round_no, max_rounds, sha: str, axes: list, decision, outcome: str,
                  max_yellow: str, run_url: str) -> str:
    state, next_step = DECIDE_STATES.get(outcome, (STATE_NO_DECISION, NEXT_RELABEL))
    next_text = DECIDE_NEXT_TEXT.get(outcome, NEXT_RELABEL_TEXT)
    lines = card_head(state, next_step) + [heading(round_no, max_rounds, sha), ""]
    if outcome == OUTCOME_SUPERSEDED:
        lines += ["**Superseded by a newer commit.**", "", f"**Next step:** {next_text}",
                  "", _reviewed_line(sha, run_url)]
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
    if outcome == OUTCOME_APPROVED:
        lines.append("**Result: ✅ Approved.**")
    else:
        lines.append("**Result: ❌ Not approved.**")
        lines += [f"- {r}" for r in decide_reasons(outcome, decision)]
        lines += ["", f"**Next step:** {next_text}"]
    limit = _int_or_q(max_yellow)
    lines += ["", f"_Rule: no red, at most {limit} yellow; every axis must report. {_run_link(run_url)}_",
              "", _reviewed_line(sha, "")]
    return "\n".join(lines) + "\n"


def decide_reasons(outcome: str, decision) -> list:
    """The decide card's "not approved" reasons, markdown-ready. Also the body
    of the standing REQUEST_CHANGES approve-external posts when it withholds,
    so the two always say the same thing. Axis reasons echo model text and go
    through `sanitize`; the fixed ones are this module's own."""
    why = {
        OUTCOME_HUMAN: ["the PR is labelled `needs-human-review`"],
        OUTCOME_VETOED: ["vetoed: the PR is labelled `skip-cursor-review`"],
        OUTCOME_OWN_PR: ["the approver authored this PR"],
        OUTCOME_ERROR: ["cursor-approve could not complete its decision (see the workflow run)"],
    }.get(outcome)
    if why is not None:
        return why
    reasons = decision.get("reasons") if isinstance(decision, dict) else None
    reasons = [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else []
    why = reasons if outcome == OUTCOME_NOT_APPROVED and reasons else ["no decision was reached"]
    return [sanitize(r, 300) for r in why]


def reason_text(text) -> str:
    """One of decide_gate's reasons, rendered VERBATIM (backticks and all).

    These are auto-approve.py's own sentences, not model output: what they
    interpolate is a count, a threshold word, a `_label_part`-filtered panel
    label or `gh` stderr. So they are not markdown-escaped like an axis summary
    (that would print `\\`low\\``); they are folded to one line, mentions are
    neutralized, and `<` is entity-escaped so nothing can open an HTML comment
    and forge a marker.
    """
    text = " ".join(str(text or "").split())
    if len(text) > 500:
        text = text[:499].rstrip() + "…"
    return neutralize_mentions(text).replace("<", "&lt;")


def _gating_row(finding: dict) -> str:
    """One gating finding: severity, file:line, thread link and why it gated.
    Severity/file/line are model output, filtered exactly as auto-approve.py's
    review body filters them; `url` and `why` are built by auto-approve.py."""
    sev = finding.get("severity")
    sev = sev.strip().lower() if isinstance(sev, str) else ""
    sev = sev if sev in ("critical", "high", "medium", "low", "nit") else "unknown"
    line = finding.get("line")
    line = str(line) if isinstance(line, int) and not isinstance(line, bool) else "?"
    path = finding.get("file") if isinstance(finding.get("file"), str) else "?"
    # `<` could start a forged contract marker inside the code span's raw text.
    ref = _load_post_review().render_code_ref(path[:300].replace("<", "‹"), line)
    url = finding.get("url") or ""
    link = f"[thread]({url})" if re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_./#-]+", url) else "no thread found"
    why = reason_text(finding.get("why") or "")
    return f"- **{sev}** — {ref} — {link}" + (f" — gated: {why}" if why else "")


def render_round(round_no, max_rounds, sha: str, state: str, next_step: str, headline: str,
                 reasons: list, next_text: str, run_url: str, gating=None, threshold: str = "") -> str:
    """The card cursor-review's decide writes for the round it just decided.

    `headline` and `next_text` are auto-approve.py's fixed sentences; `reasons`
    are decide_gate's, verbatim; `gating` is the findings (or open threads)
    that held the approval back, each a dict with severity/file/line/url/why.
    """
    # The headline too: callers build it from a reason (`Not approved: …`), and
    # it sits two lines below the contract markers agents parse.
    lines = card_head(state, next_step) + [heading(round_no, max_rounds, sha), "", f"**{reason_text(headline)}**"]
    rows = [_gating_row(f) for f in (gating or [])[:20] if isinstance(f, dict)]
    if rows:
        lines += ["", *rows]
        if len(gating) > 20:
            lines.append(f"- … and {len(gating) - 20} more (see the review)")
    clean = [reason_text(r) for r in (reasons or []) if isinstance(r, str) and r.strip()]
    if clean:
        lines += ["", "Reasons (from the auto-approve decision):", *[f"- {r}" for r in clean]]
    if next_step != NEXT_NONE and next_text:
        lines += ["", f"**Next step:** {next_text}"]
    if threshold in ("medium", "low", "nit"):
        lines += ["", f"_Threshold: `{threshold}` (this repo's `approve_max_severity`)._"]
    lines += ["", _reviewed_line(sha, run_url)]
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
