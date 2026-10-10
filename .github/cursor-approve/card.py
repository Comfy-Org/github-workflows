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
    Each axis shows its `headline` only — ONE line naming what decided the
    verdict. The same phase writes ``render_job_summary`` to
    ``GITHUB_STEP_SUMMARY``: the full summaries and confidences, which is what
    the card's "workflow run" link now leads to. The card is the glance; the
    job summary is the record.

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

With ``prior_rounds`` above 0 (BE-5109's continuity, for the axes), ``decide``
also writes ONE line at column 0 of the card, ``AXES_SENTINEL_OPENER`` +
base64(JSON) + `` -->``: the last ``prior_rounds`` rounds of per-axis verdict,
headline and summary, oldest first. ``upsert`` carries that line through every
later rewrite that does not write its own (``start``, ``ensure``, cursor-review's
``render_round``), and ``prior`` reads it back in the next round's start phase,
for ``aggregate.py render --prior-file``. Base64, so model text can never close
the comment or start a line; read back only from the card ``find_card`` trusts,
only at a line start, only at version ``v1``, and only after a byte cap and a
per-entry shape check — anything else is an empty history and a warning.

Every model-supplied string (an axis summary, a reason that echoes one) goes
through ``sanitize`` — post-review.py's ``neutralize_mentions`` plus escaping of
every character that could open markdown or HTML — so a summary cannot add a
heading, fire a mention or forge the card marker.
"""

import argparse
import base64
import binascii
import importlib.util
import json
import os
import re
import subprocess
import sys

CARD_MARKER = "<!-- cursor-approve-card -->"
SUMMARY_LIMIT = 200
# The job summary behind the run link is the detail surface, so an axis
# summary is not cut to one table cell there.
SUMMARY_DETAIL_LIMIT = 2000
AXES = ("business", "design", "correctness", "completeness", "conformance")
VERDICT_ICONS = {"green": "🟢 green", "yellow": "🟡 yellow", "red": "🔴 red",
                 "n/a": "⚪ n/a"}
NO_RESULT = "⚠️ no result"
SHA_RE = re.compile(r"[0-9a-f]{40}")

# Outcomes auto-approve.py `approve-external` reports; anything else renders as
# an unknown outcome rather than being trusted.
OUTCOME_APPROVED = "approved"
OUTCOME_NOT_APPROVED = "not_approved"
OUTCOME_SUPERSEDED = "superseded"
# Superseded by a base retarget alone (BE-19527): the head is unchanged, so it
# needs the push remedy a head move does not.
OUTCOME_RETARGETED = "retargeted"
OUTCOME_HUMAN = "needs_human"
OUTCOME_VETOED = "vetoed"
OUTCOME_OWN_PR = "own_pr"
OUTCOME_ERROR = "error"

# The machine-readable contract (docs/callers/cursor-approve.md). Agents key on
# these values, so a released one is not renamed. The one exception was
# `dismiss_then_relabel`, withdrawn for NEXT_PUSH below before any caller's pin
# reached it; `next` is an open set, and an unrecognised value reads as `human`.
STATE_PASS = "pass"
STATE_CHANGES = "changes_requested"
STATE_NO_DECISION = "no_decision"
STATE_CAPPED = "capped"
STATES = (STATE_PASS, STATE_CHANGES, STATE_NO_DECISION, STATE_CAPPED)
NEXT_NONE = "none"
NEXT_RESOLVE = "resolve_then_relabel"
NEXT_RELABEL = "relabel"
NEXT_HUMAN = "human"
# BE-19527. A relabel alone is a NO-OP for a round whose head never moved: the
# gate's `dup` step skips any head that already carries a bot-posted
# consolidated review, and a degraded round posts one too.
#
# The first cut of this named the gate's own `state != "DISMISSED"` escape hatch
# and called itself `dismiss_then_relabel`. That remedy does not exist:
# post-review.py submits the consolidated review with `"event": "COMMENT"`, and
# GitHub dismisses only APPROVED / CHANGES_REQUESTED reviews (422 otherwise, and
# the UI offers no Dismiss control), so that branch of the filter is unreachable
# for the one review it is matched against. It replaced an impossible remedy
# with another one.
#
# What actually clears the dedupe is MOVING THE HEAD, so that is what this says.
# An empty commit is enough. The relabel stays in the text because a label-gated
# caller no-ops on `synchronize`: the push alone restarts the round only under
# `run_without_label`.
NEXT_PUSH = "push_then_relabel"
NEXTS = (NEXT_NONE, NEXT_RESOLVE, NEXT_RELABEL, NEXT_PUSH, NEXT_HUMAN)
STATE_MARKER = "<!-- cursor-approve-state: {} -->"
NEXT_MARKER = "<!-- cursor-approve-next: {} -->"

DEFAULT_REVIEW_LABEL = "cursor-review"

# The prior-axes sentinel. `v1` is part of the literal, so a future `v2`
# payload is recognised as a sentinel and refused rather than misread.
AXES_SENTINEL_OPENER = "<!-- cursor-approve:axes v1 "
AXES_SENTINEL_CLOSER = " -->"
_AXES_SENTINEL_RE = re.compile(r"^<!-- cursor-approve:axes (v[0-9]+) (\S*) -->[ \t\r]*$", re.MULTILINE)
# Any mention of the sentinel's prefix at all, to report one that sits where
# the line-start match above (rightly) does not take it.
_AXES_SENTINEL_ANY_RE = re.compile(r"cursor-approve:axes v")
VERDICTS = ("red", "yellow", "green", "n/a")
PRIOR_ROUNDS_LIMIT = 3
# The ledger's MAX_BODY_CHARS (build-ledger.py).
PRIOR_SUMMARY_CHARS = 600
# The JSON payload, in UTF-8 bytes; the oldest round is dropped until it fits.
MAX_AXES_PAYLOAD_BYTES = 16 * 1024
# The base64 text `prior` will decode: exactly what MAX_AXES_PAYLOAD_BYTES
# encodes to, checked BEFORE decoding.
MAX_AXES_SENTINEL_CHARS = 4 * -(-MAX_AXES_PAYLOAD_BYTES // 3)


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


def next_push_text(label=DEFAULT_REVIEW_LABEL) -> str:
    """The round that a relabel alone cannot re-run (BE-19527).

    Moving the head is the only remedy the gate's dedupe actually honours; the
    review it skips on cannot be dismissed (see NEXT_PUSH).
    """
    return ("Push a commit so the head moves — an empty one (`git commit --allow-empty`) is enough, since the gate "
            f"skips a head that already carries a review — then re-run the round: {relabel(label)}.")


def next_auto_retry_text(label=DEFAULT_REVIEW_LABEL) -> str:
    """The relabel the workflow is doing itself (BE-19526).

    Still ``NEXT_RELABEL``: the next step IS a relabel, and an agent reading the
    contract marker should wait for the new round either way. Only the prose
    differs, so nobody re-does by hand what already fired. Written before the
    relabel runs (a later job; it can fail or be cancelled by a push), hence the
    "if no new round starts" fallback rather than a promise.
    """
    return (f"Nothing — the round is being re-run automatically ({relabel(label)}). Once only; "
            "if no new round starts, or it is wrong again, re-run it by hand.")


# The default-label texts. cursor-approve.yml has no `review_label` input, so
# its own phases always use these; cursor-review's decide passes its input.
RELABEL = relabel()
NEXT_RESOLVE_TEXT = next_resolve_text()
NEXT_RELABEL_TEXT = next_relabel_text()
NEXT_AUTO_RETRY_TEXT = next_auto_retry_text()
NEXT_PUSH_TEXT = next_push_text()
# The hand-off withdraws the bot's own requests for changes (BE-19492), so a
# human's review is what clears the PR; the label only resets the round count.
# Not stated as done: every caller writes this card whether or not that
# withdrawal succeeded (a failed one turns the run red instead).
NEXT_HUMAN_CAPPED_TEXT = ("A human is needed: the bot withdraws its own request for changes on this hand-off, so a "
                          "human's review decides this PR — if one still shows, its withdrawal failed (see the run "
                          "log) and it needs dismissing by hand. Removing the `needs-human-review` label resets the "
                          "round count.")

# How approve-external's outcome maps onto the contract on the decide card.
DECIDE_STATES = {
    OUTCOME_APPROVED: (STATE_PASS, NEXT_NONE),
    OUTCOME_NOT_APPROVED: (STATE_CHANGES, NEXT_RELABEL),
    OUTCOME_SUPERSEDED: (STATE_NO_DECISION, NEXT_RELABEL),
    OUTCOME_RETARGETED: (STATE_NO_DECISION, NEXT_PUSH),
    OUTCOME_HUMAN: (STATE_CAPPED, NEXT_HUMAN),
    OUTCOME_VETOED: (STATE_NO_DECISION, NEXT_HUMAN),
    OUTCOME_OWN_PR: (STATE_NO_DECISION, NEXT_HUMAN),
    OUTCOME_ERROR: (STATE_NO_DECISION, NEXT_PUSH),
}
DECIDE_NEXT_TEXT = {
    OUTCOME_NOT_APPROVED: f"Address the axis verdicts above, push, then start a new round: {RELABEL}.",
    OUTCOME_SUPERSEDED: f"A newer commit needs its own round: {RELABEL}.",
    OUTCOME_RETARGETED: f"The base was retargeted, so this diff needs its own round. {NEXT_PUSH_TEXT}",
    OUTCOME_HUMAN: NEXT_HUMAN_CAPPED_TEXT,
    OUTCOME_VETOED: "A human is needed: the PR carries `skip-cursor-review`.",
    OUTCOME_OWN_PR: "A human is needed: the approver cannot approve its own PR.",
    OUTCOME_ERROR: NEXT_PUSH_TEXT,
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
        lines += ["| Axis | Verdict | Why |", "|---|---|---|"]
        lines += [f"| {axis} | ⏳ pending | |" for axis in axes]
    lines += ["", _reviewed_line(sha, run_url)]
    return "\n".join(lines) + "\n"


def _confidence(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
        return f"{value:.2f}"
    return ""


def render_job_summary(round_no, max_rounds, sha: str, axes: list, decision, outcome: str,
                       max_yellow: str) -> str:
    """The detail the card's "workflow run" link leads to, for GITHUB_STEP_SUMMARY.

    The card is the glance: one icon and one `headline` per axis. Everything an
    axis actually wrote — the full `summary`, its confidence — lands here, so
    the link is worth following and the card does not have to carry both jobs.

    Same `sanitize` as the card: this renders model text into markdown GitHub
    displays, and a summary must not be able to open a heading or fire a mention
    here either. No contract markers — a job summary is not the card, and
    nothing parses this.
    """
    detail = decision.get("axes") if isinstance(decision, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    lines = [heading(round_no, max_rounds, sha), ""]
    lines.append("**Approved.**" if outcome == OUTCOME_APPROVED else "**Not approved.**")
    if outcome != OUTCOME_APPROVED:
        lines += [f"- {r}" for r in decide_reasons(outcome, decision)]
    for axis in axes:
        entry = detail.get(axis)
        entry = entry if isinstance(entry, dict) else {}
        verdict = entry.get("verdict")
        icon = VERDICT_ICONS.get(verdict, NO_RESULT)
        confidence = _confidence(entry.get("confidence"))
        lines += ["", f"#### {axis} — {icon}" + (f" (confidence {confidence})" if confidence else "")]
        headline = sanitize(entry.get("headline"))
        if headline:
            lines.append(f"**{headline}**")
        summary = sanitize(entry.get("summary"), SUMMARY_DETAIL_LIMIT)
        lines += ["", summary or "_No summary was reported._"]
    limit = _int_or_q(max_yellow)
    lines += ["", f"_Rule: no red, at most {limit} yellow; every axis must report "
              "(⚪ n/a counts as reporting and does not block, but all-n/a withholds)._"]
    return "\n".join(lines) + "\n"


def render_decide(round_no, max_rounds, sha: str, axes: list, decision, outcome: str,
                  max_yellow: str, run_url: str) -> str:
    state, next_step = DECIDE_STATES.get(outcome, (STATE_NO_DECISION, NEXT_PUSH))
    next_text = DECIDE_NEXT_TEXT.get(outcome, NEXT_PUSH_TEXT)
    lines = card_head(state, next_step) + [heading(round_no, max_rounds, sha), ""]
    if outcome in (OUTCOME_SUPERSEDED, OUTCOME_RETARGETED):
        what = "a newer commit" if outcome == OUTCOME_SUPERSEDED else "a base retarget"
        lines += [f"**Superseded by {what}.**", "", f"**Next step:** {next_text}",
                  "", _reviewed_line(sha, run_url)]
        return "\n".join(lines) + "\n"
    detail = decision.get("axes") if isinstance(decision, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    # Three columns, and the third is the axis's `headline` — ONE line stating
    # what decided the verdict. It used to be the first sentence of `summary`,
    # which is where the models put their process log ("Checked the only
    # changed file...", "I read root AGENTS.md and CLAUDE.md..."), so the card
    # showed what the axis DID and never why it ruled as it did. The full
    # summary and the confidence now live in the run's job summary, one click
    # behind the link below — this card is the glance, not the record.
    lines += ["| Axis | Verdict | Why |", "|---|---|---|"]
    for axis in axes:
        entry = detail.get(axis)
        verdict = entry.get("verdict") if isinstance(entry, dict) else None
        if verdict not in VERDICT_ICONS:
            lines.append(f"| {axis} | {NO_RESULT} | |")
            continue
        why = sanitize(entry.get("headline") or first_sentence(entry.get("summary")))
        lines.append(f"| {axis} | {VERDICT_ICONS[verdict]} | {why} |")
    lines.append("")
    if outcome == OUTCOME_APPROVED:
        lines.append("**Result: ✅ Approved.**")
    else:
        lines.append("**Result: ❌ Not approved.**")
        lines += [f"- {r}" for r in decide_reasons(outcome, decision)]
        lines += ["", f"**Next step:** {next_text}"]
    limit = _int_or_q(max_yellow)
    lines += ["", f"_Rule: no red, at most {limit} yellow; every axis must report "
              f"(⚪ n/a counts as reporting and does not block, but all-n/a withholds). "
              f"{_run_link(run_url)}_",
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


_AGGREGATE = None


def _clamp_headline(text: str) -> str:
    """aggregate.py's headline rules, re-applied to a headline read off the card."""
    global _AGGREGATE
    if _AGGREGATE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aggregate.py")
        spec = importlib.util.spec_from_file_location("cursor_approve_aggregate", path)
        _AGGREGATE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_AGGREGATE)
    return _AGGREGATE.clamp_headline(" ".join(text.split()))


def _utf8(text: str) -> str:
    """`text` with any lone UTF-16 surrogate (an escaped `\\ud800` that
    `json.loads` decoded) replaced by `?`, so it can always be UTF-8 encoded:
    the sentinel, the `prior` file and the axis prompt all write UTF-8."""
    return text.encode("utf-8", "replace").decode("utf-8")


def parse_prior_rounds(value) -> int:
    """`prior_rounds`, 0..PRIOR_ROUNDS_LIMIT; a `type: number` may render `1.0`."""
    text = _int_or_q(value) if str(value or "").strip() else "0"
    n = int(text) if text.isdigit() else -1
    if not 0 <= n <= PRIOR_ROUNDS_LIMIT:
        raise ValueError(f"--prior-rounds must be an integer from 0 to {PRIOR_ROUNDS_LIMIT}, got {value!r}")
    return n


def _valid_round(r) -> dict:
    """One sentinel round, normalised, or raise ValueError."""
    if not isinstance(r, dict):
        raise ValueError("a round is not an object")
    number, sha, axes = r.get("round"), r.get("commit_sha"), r.get("axes")
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise ValueError(f"round {number!r} is not a non-negative integer")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ValueError("commit_sha is not a full 40-hex SHA")
    if not isinstance(axes, dict):
        raise ValueError("axes is not an object")
    out = {}
    for axis, entry in axes.items():
        if axis not in AXES or not isinstance(entry, dict):
            raise ValueError(f"unknown axis entry {str(axis)[:20]!r}")
        if entry.get("verdict") not in VERDICTS:
            raise ValueError(f"{axis}: verdict is not one of {', '.join(VERDICTS)}")
        headline, summary = entry.get("headline"), entry.get("summary")
        if not isinstance(headline, str) or not headline.strip():
            raise ValueError(f"{axis}: headline is missing")
        if summary is not None and not isinstance(summary, str):
            raise ValueError(f"{axis}: summary is not a string")
        out[axis] = {"verdict": entry["verdict"], "headline": _clamp_headline(_utf8(headline)),
                     "summary": _utf8(summary or "")[:PRIOR_SUMMARY_CHARS]}
    return {"round": number, "commit_sha": sha, "axes": out}


def read_axes_sentinel(body):
    """(rounds, problem) from a card body. ([], None) when there is no sentinel;
    ([], reason) when there is one this module will not trust."""
    matches = _AXES_SENTINEL_RE.findall(body or "")
    if not matches:
        if _AXES_SENTINEL_ANY_RE.search(body or ""):
            return [], "an axes sentinel that is not at a line start was ignored"
        return [], None
    if len(matches) > 1:
        return [], "the card carries more than one axes sentinel"
    version, payload = matches[0]
    if version != "v1":
        return [], f"unknown axes sentinel version {version[:8]!r}"
    if len(payload) > MAX_AXES_SENTINEL_CHARS:
        return [], f"the axes sentinel is larger than {MAX_AXES_SENTINEL_CHARS} characters"
    try:
        data = json.loads(base64.b64decode(payload, validate=True).decode("utf-8"))
    except (binascii.Error, ValueError, RecursionError):
        return [], "the axes sentinel is not valid base64 JSON"
    if not isinstance(data, list):
        return [], "the axes sentinel is not a list of rounds"
    try:
        return [_valid_round(r) for r in data], None
    except (ValueError, RecursionError) as e:
        return [], f"the axes sentinel holds an invalid round ({e})"


def round_entry(round_no, sha: str, decision):
    """This round's sentinel entry from `aggregate.py decide`'s result, or None
    when there is nothing to record (unknown round or commit, no axis verdict)."""
    number = _int_or_q(round_no)
    if not number.isdigit() or not SHA_RE.fullmatch(sha or ""):
        return None
    detail = decision.get("axes") if isinstance(decision, dict) else None
    axes = {}
    for axis, entry in (detail.items() if isinstance(detail, dict) else ()):
        if axis not in AXES or not isinstance(entry, dict) or entry.get("verdict") not in VERDICTS:
            continue
        headline = entry.get("headline")
        try:
            headline = _clamp_headline(_utf8(headline)) if isinstance(headline, str) and headline.strip() else ""
        except ValueError:
            headline = ""
        if not headline:
            continue
        summary = entry.get("summary")
        axes[axis] = {"verdict": entry["verdict"], "headline": headline,
                      "summary": _utf8(summary)[:PRIOR_SUMMARY_CHARS] if isinstance(summary, str) else ""}
    return {"round": int(number), "commit_sha": sha, "axes": axes} if axes else None


def axes_sentinel(rounds: list) -> str:
    """The sentinel line for `rounds` (oldest first), or "" when none fit.

    The oldest round is dropped until the JSON is within MAX_AXES_PAYLOAD_BYTES.
    A newest round too large on its own (`json.dumps` escapes a control
    character as six bytes) loses its summaries first, so it never costs the
    earlier rounds their place.
    """
    rounds = list(rounds)
    if rounds and len(json.dumps(rounds[-1], ensure_ascii=False, separators=(",", ":")).encode("utf-8")) \
            > MAX_AXES_PAYLOAD_BYTES:
        newest = rounds[-1]
        rounds[-1] = dict(newest, axes={a: dict(e, summary="") for a, e in newest["axes"].items()})
    while rounds:
        payload = json.dumps(rounds, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) <= MAX_AXES_PAYLOAD_BYTES:
            return AXES_SENTINEL_OPENER + base64.b64encode(payload).decode("ascii") + AXES_SENTINEL_CLOSER
        rounds.pop(0)
    return ""


def merge_rounds(rounds: list, entry, keep: int) -> list:
    """`rounds` plus `entry`, ordered by round, at most the last `keep`. A
    re-run of the same (round, commit_sha) replaces its entry rather than
    appending a second; a late decide for an older round sorts into place, so
    trimming always drops the oldest round, not the newest."""
    if entry is not None:
        key = (entry["round"], entry["commit_sha"])
        rounds = [r for r in rounds if (r["round"], r["commit_sha"]) != key] + [entry]
    rounds = sorted(rounds, key=lambda r: r["round"])
    return rounds[-keep:] if keep > 0 else []


def _append_line(body: str, line: str) -> str:
    return body if not line else body.rstrip("\n") + "\n\n" + line + "\n"


def with_axes_history(body: str, existing_body, entry=None, keep=None) -> str:
    """`body` with the axes sentinel it should carry.

    With `keep` above 0 (decide, `prior_rounds` set), the existing card's rounds
    plus `entry`, trimmed and re-encoded. With `keep` 0 (decide, `prior_rounds`
    off or invalid) no sentinel at all: turning the feature off drops the
    stored history rather than keeping stale rounds on the card. With `keep`
    None (every other writer) the existing card's valid sentinel is carried
    through unchanged — unless `body` writes its own — so the start card,
    ensure and cursor-review's round card never erase history. With no
    sentinel on the card this returns `body` unchanged.
    """
    if _AXES_SENTINEL_RE.search(body) or keep == 0:
        return body
    rounds, problem = read_axes_sentinel(existing_body)
    if problem:
        print(f"::warning::Dropping the cursor-approve card's prior-axes history: {problem}")
    if keep is not None:
        return _append_line(body, axes_sentinel(merge_rounds(rounds, entry, keep)))
    return _append_line(body, axes_sentinel(rounds)) if rounds else body


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


def upsert(repo: str, pr_number, login: str, body: str, entry=None, keep=None) -> dict:
    """Edit the existing card in place, or create it when there is none.

    The card's axes sentinel is carried into `body` (see `with_axes_history`);
    `entry` and `keep` are decide's; `keep` stays None for every other writer.
    """
    card = find_card(list_comments(repo, pr_number), login)
    body = with_axes_history(body, card.get("body") if card is not None else "", entry, keep)
    if card is not None:
        return json.loads(gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{card['id']}", "--input", "-"],
                             {"body": body}))
    return json.loads(gh(["api", "-X", "POST", f"repos/{repo}/issues/{pr_number}/comments", "--input", "-"],
                         {"body": body}))


def _write_job_summary(body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(body)
    except OSError as e:
        print(f"::warning::Could not write the cursor-approve job summary: {e}")


def _write_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prior")
    p.add_argument("--repo", required=True)
    p.add_argument("--pr-number", required=True)
    p.add_argument("--login", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--round", default="")
    p.add_argument("--prior-rounds", default="")
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
            p.add_argument("--prior-rounds", default="0")
    args = parser.parse_args(argv)
    # An empty login matches no comment, so every phase would post a fresh card.
    if not (args.login or "").strip():
        print("::error::--login is empty; refusing to post a card that can never be found again")
        return 2
    if args.cmd == "prior":
        return cmd_prior(args)
    axes = parse_axes(args.axes)
    entry, keep = None, None
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
            keep = parse_prior_rounds(args.prior_rounds)
        except ValueError as e:
            print(f"::warning::{e}; writing no prior-axes history")
            keep = 0
        if keep:
            entry = round_entry(args.round, args.commit_sha, decision)
        # The other half of the card's "workflow run" link. Written before the
        # card so a GitHub API failure below cannot cost us the detail too, and
        # a failure HERE is never fatal: the card is what the author reads.
        _write_job_summary(render_job_summary(args.round, args.max_rounds, args.commit_sha, axes,
                                              decision, args.outcome, args.max_yellow_axes))
    try:
        upsert(args.repo, args.pr_number, args.login, body, entry, keep)
    except (RuntimeError, ValueError) as e:
        print(f"::warning::Could not update the cursor-approve card: {e}")
    return 0


def _prior_history(args) -> list:
    """The card's rounds that came before this one, at most `--prior-rounds`.

    A re-run of this round (or a later one recorded by a re-run of an older
    round) is not a PRIOR verdict: showing the axis its own output for this
    round would anchor it on itself. So only rounds below `--round` are kept,
    and only the last `--prior-rounds` of those, so lowering the input takes
    effect in the very next round rather than after decide re-trims the card.
    """
    try:
        comments = list_comments(args.repo, args.pr_number)
        card = find_card(comments, args.login)
    except (RuntimeError, ValueError) as e:
        print(f"::warning::Could not read the cursor-approve card for prior verdicts: {e}")
        return []
    if card is None:
        if any(isinstance(c, dict) and _AXES_SENTINEL_ANY_RE.search(c.get("body") or "") for c in comments):
            print(f"::warning::Ignoring an axes sentinel in a comment not written as the card by {args.login}")
        return []
    rounds, problem = read_axes_sentinel(card.get("body"))
    if problem:
        print(f"::warning::Ignoring the card's prior-axes history: {problem}")
    current = _int_or_q(args.round)
    if current.isdigit():
        rounds = [r for r in rounds if r["round"] < int(current)]
    if str(args.prior_rounds or "").strip():
        keep = parse_prior_rounds(args.prior_rounds)
        rounds = sorted(rounds, key=lambda r: r["round"])[-keep:] if keep else []
    return rounds


def cmd_prior(args) -> int:
    """Write `{"rounds": [...]}`, the card's prior-axes history, to --out.

    Never an error past the arguments: no card, no sentinel, an untrusted one,
    a failed read or even a failed write is an empty (or absent) history with a
    warning when something was wrong, so the axes run exactly as they do
    without `prior_rounds`.
    """
    try:
        rounds = _prior_history(args)
    except Exception as e:  # noqa: BLE001 — history is an aid; never fail the start phase for it
        print(f"::warning::Could not read the prior-axes history ({e.__class__.__name__}: {e}); using none")
        rounds = []
    try:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"rounds": rounds}, f, ensure_ascii=False)
            f.write("\n")
    except (OSError, ValueError) as e:
        print(f"::warning::Could not write the prior-axes history to {args.out}: {e.__class__.__name__}")
        return 0
    print(f"prior rounds read from the card: {len(rounds)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
