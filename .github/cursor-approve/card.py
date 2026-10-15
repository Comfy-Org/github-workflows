#!/usr/bin/env python3
"""cursor-approve: the live status card on the PR.

ONE issue comment per PR, found by ``CARD_MARKER`` (and authored by the token's
own login, so a comment anyone else plants with the marker is never taken for
it) and edited in place — never reposted. cursor-approve.yml writes it twice a
round:

``start`` (before the axis jobs)
    The round heading, every enabled axis as pending, and a link to the run.
    When cursor-review reported ``approve_gate == capped`` it writes
    "Round limit reached, needs a human" and no table instead. Under
    cursor-review's ``axes_alongside_panel`` it runs while the panel still
    does (``approve_gate == pending``): the same table, a line saying the
    panel is still running, and no ``LANE_MARKER``.

``closeout`` (cursor-review's ``axes-closeout``, after its panel decided)
    Rewrite that early card to "No decision this round" when the panel did
    not pass — only a ``pass`` card for the same commit and round with an
    axis still pending, so a card any decide already wrote is left alone.

``ensure`` (decide phase, before the review is posted)
    Print the card's URL, creating it first when the start phase never ran,
    so the approval body can link to it.

``decide`` (after auto-approve.py ``approve-external``)
    The per-axis verdicts from ``aggregate.py decide``, the overall result and
    the rule applied — or "Superseded by a newer commit" when the head moved.
    Each axis's row shows its whole `headline` — the sentence naming what
    decided the verdict — and under the result sits one collapsed
    ``<details>`` block per axis with its full `summary` and confidence
    (BE-20249), so the reasoning is one click away on the PR itself. The same
    phase writes ``render_job_summary`` to ``GITHUB_STEP_SUMMARY``, the run's
    own record of the same detail.

cursor-review's ``auto-approve.py decide`` writes it too, through
``render_round`` and ``upsert``, on every round it decides for an author
auto-approve applies to (BE-19489): ``pass``, ``changes_requested``,
``no_decision`` and ``capped`` alike. Only that job has the round's gating
findings, its threads and ``decide_gate``'s reasons in hand, and it runs
whatever the outcome, while every phase here runs only after a ``pass`` (or a
``capped``) the caller's ``if:`` let through. On a ``pass`` the start phase
then overwrites it with the axes table, as before.

Every review that decide posts links the card (BE-20246), and so must exist
before it: a review cannot be edited afterwards to add the link, because
auto-approve.py treats an edited approval as stale and never withdraws an
edited request for changes. So on a PR's first round decide calls ``ensure``
just before its review, which creates the card from ``render_pending`` when
there is none, and the round's ``render_round`` replaces that in the same step.

cursor-review's dismiss job rewrites it too (``render_dismissed``, BE-20262):
a ``pass`` card whose approved commit a push (or retarget) left behind, so it
stops saying "Approved" for an approval GitHub no longer counts; and its
``auto-rereview`` job records on it the round it starts (``mark_auto_rereview``).

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
import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys

CARD_MARKER = "<!-- cursor-approve-card -->"
SUMMARY_LIMIT = 200
# The Why cell holds a whole headline, so it is exactly aggregate.py's
# HEADLINE_MAX (a test pins the two equal): any lower and the cell would cut a
# headline aggregate.py had already let through whole.
WHY_LIMIT = 240
# The detail surfaces — the collapsed per-axis blocks on the card and the job
# summary — carry an axis summary whole rather than cut to one table cell.
SUMMARY_DETAIL_LIMIT = 2000
AXES = ("business", "design", "correctness", "completeness", "conformance")
VERDICT_ICONS = {"green": "🟢 green", "yellow": "🟡 yellow", "red": "🔴 red",
                 "n/a": "⚪ n/a"}
NO_RESULT = "⚠️ no result"
PENDING_ROW = "⏳ pending"
# A yellow's two meanings (aggregate.py's YELLOW_KINDS), told apart on the card
# so a reader sees which kind gated a round. Both keep the `🟡 yellow` prefix
# and contain no `red`/`green`/`n/a`/`pending`: a consumer that reduces a
# verdict cell to a known token by substring still reads either one as a yellow.
# Anything but an exact `concern` renders as could-not-judge, as aggregate.py
# records it. Reporting only — the rule line counts both kinds alike.
YELLOW_KIND_ICONS = {"concern": "🟡 yellow · concern", "unjudged": "🟡 yellow · could not judge"}
YELLOW_RULE = "at most {} yellow (a concern and a could-not-judge count alike)"
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


# Mirrors cursor-review.yml's clamp on `auto_retry_delay_minutes`; a test pins
# the two equal, since the sentence must not promise a wait longer than the
# one the workflow will actually take.
RETRY_DELAY_CAP = 30


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


def retry_delay_phrase(delay_minutes=0, after="", now=None) -> str:
    """" at HH:MM UTC (N min after <after>)", or "" — the advance notice for a
    delayed retry.

    The start time, not the wait: an author reads the card minutes after it
    was written, when "in about 5 minutes" no longer says when. It is `now`
    (the moment the wait is scheduled; injectable for tests) plus the clamped
    delay, in UTC, with the date only when it falls on another UTC day. A
    newer push's dismissal rewrites the card, so the time is the live schedule.

    Sanitised here as well as in the workflow that clamps the input, because
    this text is posted as the approver: the value arrives as a caller input,
    and only a small integer may reach the sentence. Out of range or
    unparsable reads as no delay, matching what the workflow would then wait.
    """
    try:
        mins = int(str(delay_minutes).strip())
    except (TypeError, ValueError):
        return ""
    if mins < 1:
        return ""
    mins = min(mins, RETRY_DELAY_CAP)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    now = now.astimezone(datetime.timezone.utc) if now.tzinfo else now.replace(tzinfo=datetime.timezone.utc)
    start = now + datetime.timedelta(minutes=mins)
    when = start.strftime("%H:%M UTC")
    if start.date() != now.date():
        when += start.strftime(" on %Y-%m-%d")
    return f" at {when} ({mins} min after {after or 'now'})"


def next_auto_retry_text(label=DEFAULT_REVIEW_LABEL, delay_minutes=0, now=None) -> str:
    """The relabel the workflow is doing itself (BE-19526).

    Still ``NEXT_RELABEL``: the next step IS a relabel, and an agent reading the
    contract marker should wait for the new round either way. Only the prose
    differs, so nobody re-does by hand what already fired. Written before the
    relabel runs (a later job; it can fail or be cancelled by a push), hence the
    "if no new round starts" fallback rather than a promise.

    ``delay_minutes`` is the caller's ``auto_retry_delay_minutes``: the wait is
    announced BEFORE it starts, which is the point of saying it at all — an
    author who reads "being re-run automatically" and sees nothing happen for
    five minutes otherwise re-runs it by hand. Zero (every caller that has not
    set it) leaves the sentence exactly as it was.
    """
    when = retry_delay_phrase(delay_minutes, "this round ended", now)
    how = f": {relabel(label)}." if when else f" ({relabel(label)})."
    return (f"Nothing — the round is being re-run automatically{when}{how} Once only; "
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


def verdict_icon(entry) -> str:
    """One axis's Verdict cell: its icon and verdict, a yellow's kind included,
    or NO_RESULT for an entry with no known verdict."""
    verdict = entry.get("verdict") if isinstance(entry, dict) else None
    if verdict == "yellow":
        kind = entry.get("yellow_kind")
        return YELLOW_KIND_ICONS[kind if isinstance(kind, str) and kind in YELLOW_KIND_ICONS else "unjudged"]
    return VERDICT_ICONS.get(verdict, NO_RESULT)


def why_text(entry: dict) -> str:
    """One axis's Why cell: its whole headline, sanitised.

    aggregate.py already clamped the headline to whole sentences within
    HEADLINE_MAX; the same clamp runs again here because the card is the last
    stop before the PR and must not depend on its input being well-formed. A
    missing headline, or one the clamp rejects as reason-free (aggregate.py
    rejects both, so this is defence only), falls back to the summary's first
    sentence rather than an empty cell.
    """
    headline = entry.get("headline")
    candidates = [headline] if isinstance(headline, str) else []
    candidates.append(first_sentence(entry.get("summary")))
    for text in candidates:
        text = " ".join(str(text or "").split())
        try:
            text = _clamp_headline(text) if text else ""
        except ValueError:
            text = ""
        if text:
            return sanitize(text, WHY_LIMIT)
    return ""


def reasoning_blocks(axes: list, detail: dict) -> list:
    """Card lines: one collapsed ``<details>`` block per axis that reported a
    summary — the full reasoning behind its row, one click away on the PR
    itself rather than behind the run link (BE-20249).

    They sit below the table and the result, so the contract markers and the
    `| Axis | Verdict | Why |` rows agents parse are exactly as before. The
    summary goes through `sanitize` like every model string on the card, which
    folds it to one line and backslash-escapes every `|` and `<`: no line here
    can open a table row, an HTML comment or a forged marker. The blank lines
    around it are what make GitHub render it as markdown inside the block.
    """
    lines = []
    for axis in axes:
        entry = detail.get(axis)
        if not isinstance(entry, dict) or entry.get("verdict") not in VERDICT_ICONS:
            continue
        summary = sanitize(entry.get("summary"), SUMMARY_DETAIL_LIMIT)
        if not summary:
            continue
        confidence = _confidence(entry.get("confidence"))
        label = f"{axis} — full reasoning" + (f" (confidence {confidence})" if confidence else "")
        lines += ["", "<details>", f"<summary>{label}</summary>", "", summary, "", "</details>"]
    return lines


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


PANEL_RUNNING_TEXT = "*The review panel is still running; the axes judge the same commit in parallel.*"


def render_start(round_no, max_rounds, sha: str, axes: list, approve_gate: str, run_url: str) -> str:
    """The start phase's card: every axis pending, or the capped hand-off.

    `approve_gate == "pending"` is cursor-review's `axes_alongside_panel`
    start, written while its panel is still running: the same `pass`/`none`
    head and table, a line saying so, and NO `LANE_MARKER`. The lane marker
    means "the last panel round passed", which `auto-approve.py
    approve_only_blocker` trusts to let a push skip the panel; an early card
    carrying it would put a round whose panel has not passed into that lane.
    """
    capped = approve_gate == "capped"
    early = approve_gate == "pending"
    lines = card_head(STATE_CAPPED if capped else STATE_PASS, NEXT_HUMAN if capped else NEXT_NONE)
    lines += [heading(round_no, max_rounds, sha), ""]
    if capped:
        lines.append("**Round limit reached, needs a human.**")
        lines += ["", f"**Next step:** {NEXT_HUMAN_CAPPED_TEXT}"]
    else:
        lines += ["| Axis | Verdict | Why |", "|---|---|---|"]
        lines += [f"| {axis} | {PENDING_ROW} | |" for axis in axes]
        if early:
            lines += ["", PANEL_RUNNING_TEXT]
    lines += ["", _reviewed_line(sha, run_url)]
    if not capped and not early:
        lines += ["", LANE_MARKER]
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
        icon = verdict_icon(entry)
        confidence = _confidence(entry.get("confidence"))
        lines += ["", f"#### {axis} — {icon}" + (f" (confidence {confidence})" if confidence else "")]
        # The card's Why cell, word for word: the same clamp and fallback, so
        # the two surfaces never show a different headline for one axis.
        headline = why_text(entry)
        if headline:
            lines.append(f"**{headline}**")
        summary = sanitize(entry.get("summary"), SUMMARY_DETAIL_LIMIT)
        lines += ["", summary or "_No summary was reported._"]
    limit = _int_or_q(max_yellow)
    lines += ["", f"_Rule: no red, {YELLOW_RULE.format(limit)}; every axis must report "
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
                  "", _reviewed_line(sha, run_url), "", LANE_MARKER]
        return "\n".join(lines) + "\n"
    detail = decision.get("axes") if isinstance(decision, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    # Three columns, and the third is the axis's `headline` — the sentence
    # stating what decided the verdict, shown whole (BE-20249: at 100
    # characters it was cut mid-sentence). It used to be the first sentence of
    # `summary`, which is where the models put their process log ("Checked the
    # only changed file...", "I read root AGENTS.md and CLAUDE.md..."), so the
    # card showed what the axis DID and never why it ruled as it did. The full
    # summary and the confidence sit in the collapsed blocks under the result.
    lines += ["| Axis | Verdict | Why |", "|---|---|---|"]
    for axis in axes:
        entry = detail.get(axis)
        verdict = entry.get("verdict") if isinstance(entry, dict) else None
        if verdict not in VERDICT_ICONS:
            lines.append(f"| {axis} | {NO_RESULT} | |")
            continue
        lines.append(f"| {axis} | {verdict_icon(entry)} | {why_text(entry)} |")
    lines.append("")
    if outcome == OUTCOME_APPROVED:
        lines.append("**Result: ✅ Approved.**")
    else:
        lines.append("**Result: ❌ Not approved.**")
        lines += [f"- {r}" for r in decide_reasons(outcome, decision)]
        lines += ["", f"**Next step:** {next_text}"]
    lines += reasoning_blocks(axes, detail)
    limit = _int_or_q(max_yellow)
    # The run link sits on the reviewed-commit line, as on every other card;
    # it used to close the rule line instead, leaving this line's "workflow
    # run" as unlinked text right under a linked one.
    lines += ["", f"_Rule: no red, {YELLOW_RULE.format(limit)}; every axis must report "
              f"(⚪ n/a counts as reporting and does not block, but all-n/a withholds)._",
              "", _reviewed_line(sha, run_url)]
    # Still past a passing panel — except once handed to a human, where no
    # push may be re-checked by the axes alone.
    if state != STATE_CAPPED:
        lines += ["", LANE_MARKER]
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


PENDING_TEXT = ("Deciding this round. This card is rewritten with the result in a moment; if it still says "
                "this, the card was not updated, so this round's latest auto-approve review is the decision and "
                "the workflow run says why.")


def render_pending(round_no, max_rounds, sha: str, run_url: str) -> str:
    """The card decide creates on a PR's first round, just before posting the
    review that links to it (BE-20246); ``render_round`` replaces it seconds
    later, in the same step.

    ``pass``/``none``, the pair ``render_start`` writes while the axes are
    pending: a round in progress, nothing to do. Agents already read it that
    way. Any other pair would ask something of an agent that reads the card
    in that window. ``no_decision``/``none`` reads as "escalate to a human",
    and a remedy in ``next`` would have it push or relabel under a round that
    may be approving.
    """
    lines = card_head(STATE_PASS, NEXT_NONE) + [heading(round_no, max_rounds, sha), "", f"**{PENDING_TEXT}**"]
    lines += ["", _reviewed_line(sha, run_url)]
    return "\n".join(lines) + "\n"


# --- the card after a push dismissed its approval ----------------------------
#
# cursor-review's dismiss job rewrites a `pass` card whose approved commit is no
# longer the head (BE-20262): GitHub stopped counting that approval, so the
# card must stop saying "Approved". Two line-start markers, read back only from
# the card `find_card` trusts (every model string on a card is `sanitize`d or
# `reason_text`ed, which escapes `<`, so neither can be forged from model text):
#
# STALE_HEAD_MARKER  the head this stale card was written for. A later event on
#                    the same head leaves the card alone; a newer head rewrites
#                    it again. Gone the moment a round rewrites the card, which
#                    is how the delayed re-run knows no round has decided since.
# REREVIEW_SENTINEL  every head the workflow already started a round for on its
#                    own (`auto_rereview_after_dismissal`), newest last. Carried
#                    through every later rewrite by `upsert`, like the axes
#                    sentinel, so a head that flips back is never auto-run twice.
STALE_HEAD_MARKER = "<!-- cursor-approve-stale-head: {} -->"
_STALE_HEAD_RE = re.compile(r"^<!-- cursor-approve-stale-head: ([0-9a-f]{40}) -->[ \t\r]*$", re.MULTILINE)
REREVIEW_SENTINEL_OPENER = "<!-- cursor-approve:auto-rereview v1 "
_REREVIEW_SENTINEL_RE = re.compile(r"^<!-- cursor-approve:auto-rereview v1 ([0-9a-f,]*) -->[ \t\r]*$", re.MULTILINE)
REREVIEW_HEADS_KEPT = 20
_HEADING_RE = re.compile(r"^### Cursor approve · round ([0-9?]+) of ([0-9?]+) · ", re.MULTILINE)
_REVIEWED_RE = re.compile(r"^_Reviewed commit: `([0-9a-f]{40})`", re.MULTILINE)
_STATE_RE = re.compile(r"\A" + re.escape(CARD_MARKER) + r"\r?\n<!-- cursor-approve-state: ([a-z_]+) -->")

# --- approver-only re-checks (cursor-review's `approve_only_on_push`) --------
#
# A push after a round whose PANEL passed re-runs only the axes on the new head,
# not the panel. Two more line-start markers, read back only from the card
# `find_card` trusts (model text is `sanitize`d or `reason_text`ed, which
# escapes `<`, so neither can be forged from it). Write access can still edit
# the card by hand, so cursor-review reads the lane only from a card whose edit
# history names no other editor (auto-approve.py `card_foreign_editors`):
#
# LANE_MARKER       written by every card that sits past a passing panel: the
#                   start card (not at the cap, nor `pending`: an
#                   `axes_alongside_panel` start runs before the panel has
#                   passed) and the decide card (not handed
#                   to a human), and carried by `render_dismissed`. Absent from
#                   cursor-review's own round card, so the moment a panel round
#                   decides — whatever it decided — the PR is out of the lane
#                   until its axes run again. That is what "the last panel
#                   round passed" means to cursor-review's `approve-only` job.
# RECHECK_SENTINEL  `<round> <cap> <head,head,…>`: the heads already re-checked
#                   for panel round <round>, oldest first, and the cap they ran
#                   under. Carried through every rewrite by `upsert`; a card
#                   whose heading names another round reads as no re-checks, so
#                   a new panel round resets the count without anyone clearing
#                   the line.
# PANEL_BLOCKED_MARKER  written only by cursor-review's own round card when its
#                   decide requested changes on the round's findings (or an
#                   earlier round's open thread above the threshold): the PANEL
#                   blocked, not the axes. With `approve_only_on_push` and
#                   `auto_rereview_after_dismissal` both on, the next push starts
#                   a full round itself (cursor-review's `dismiss-stale`). A
#                   card without it — an untrusted round, a capped one, or any
#                   card written before the marker existed — never does.
PANEL_BLOCKED_MARKER = "<!-- cursor-approve-panel: blocked -->"
_PANEL_BLOCKED_RE = re.compile(r"^<!-- cursor-approve-panel: blocked -->[ \t\r]*$", re.MULTILINE)
LANE_MARKER = "<!-- cursor-approve-lane: axes -->"
_LANE_RE = re.compile(r"^<!-- cursor-approve-lane: axes -->[ \t\r]*$", re.MULTILINE)
RECHECK_SENTINEL_OPENER = "<!-- cursor-approve:recheck v1 "
_RECHECK_SENTINEL_RE = re.compile(
    r"^<!-- cursor-approve:recheck v1 ([0-9]{1,4}) ([0-9]{1,3}) ([0-9a-f,]*) -->[ \t\r]*$", re.MULTILINE)
RECHECK_HEADS_KEPT = 50
_RECHECK_LINE_RE = re.compile(r"^(?:🔍|⏳) _Approver-only re-check[^\n]*\n?", re.MULTILINE)


def card_state(body) -> str:
    """The card's `cursor-approve-state` value, or "" when it has none."""
    match = _STATE_RE.match(body or "")
    return match.group(1) if match else ""


def reviewed_sha(body) -> str:
    """The full commit SHA on the card's "Reviewed commit" line, or ""."""
    match = _REVIEWED_RE.search(body or "")
    return match.group(1) if match else ""


def stale_head(body) -> str:
    """The head a dismissal-stale card was written for, or ""."""
    match = _STALE_HEAD_RE.search(body or "")
    return match.group(1) if match else ""


def card_round(body) -> str:
    """The round number on the card's heading, or ""."""
    match = _HEADING_RE.search(body or "")
    return match.group(1) if match else ""


def rereview_heads(body) -> list:
    """The heads already given an automatic round, oldest first. Malformed
    entries are dropped, so a damaged line can only under-record — and the
    worst an under-recorded head costs is one more round, bounded by
    `max_rounds`."""
    match = _REREVIEW_SENTINEL_RE.search(body or "")
    heads = match.group(1).split(",") if match else []
    return [h for h in heads if SHA_RE.fullmatch(h)]


def rereview_sentinel(heads: list) -> str:
    heads = [h for h in heads if SHA_RE.fullmatch(h or "")][-REREVIEW_HEADS_KEPT:]
    return f"{REREVIEW_SENTINEL_OPENER}{','.join(heads)} -->" if heads else ""


def with_rereview_history(body: str, existing_body) -> str:
    """`body` carrying the existing card's auto-rereview heads, unless it
    writes its own."""
    if _REREVIEW_SENTINEL_RE.search(body):
        return body
    return _append_line(body, rereview_sentinel(rereview_heads(existing_body)))


def next_auto_rereview_text(label=DEFAULT_REVIEW_LABEL, delay_minutes=0, now=None) -> str:
    """The relabel the dismiss path schedules itself (BE-20262), announced
    before the wait. A promise it may not keep — a newer push, a human's own
    relabel or the round cap all cancel it — hence the fallback."""
    when = retry_delay_phrase(delay_minutes, "the latest push", now)
    return (f"Nothing — a new round starts automatically{when} if no further "
            f"commit lands ({relabel(label)}). If none starts, re-run it by hand.")


def panel_blocked(body) -> bool:
    """Whether the card is cursor-review's own round card for a round whose
    panel requested changes (PANEL_BLOCKED_MARKER)."""
    return bool(_PANEL_BLOCKED_RE.search(body or ""))


def next_panel_rereview_text(label=DEFAULT_REVIEW_LABEL, delay_minutes=0, now=None) -> str:
    """The next step once a push lands on a panel-blocked round and the
    workflow schedules the full round itself."""
    when = retry_delay_phrase(delay_minutes, "the latest push", now)
    return (f"Nothing — the panel blocked the last round, so a new full round starts automatically{when} if no "
            f"further commit lands ({relabel(label)}). If none starts, re-run it by hand.")


_NEXT_LINE_RE = re.compile(r"^\*\*Next step:\*\* [^\n]*$", re.MULTILINE)
_NEXT_MARKER_RE = re.compile(r"^<!-- cursor-approve-next: [a-z_]+ -->$", re.MULTILINE)


def render_panel_blocked_stale(body: str, head_sha: str, next_text: str) -> str:
    """A panel-blocked round card once a push moved the head and a full round
    is scheduled: same state, gating rows and heading; `next` becomes `relabel`
    (the workflow is relabelling), the next step says so, and the stale-head
    marker names the head the round is scheduled for (`auto-rereview` checks
    it is still that head)."""
    body = _NEXT_MARKER_RE.sub(NEXT_MARKER.format(NEXT_RELABEL), body, count=1)
    line = f"**Next step:** {reason_text(next_text)}"
    if _NEXT_LINE_RE.search(body):
        body = _NEXT_LINE_RE.sub(lambda _: line, body, count=1)
    else:
        body = _append_line(body, line)
    body = _STALE_HEAD_RE.sub("", body).rstrip("\n") + "\n"
    return _append_line(body, STALE_HEAD_MARKER.format(head_sha))


def in_axes_lane(body) -> bool:
    """Whether the card sits past a passing panel round (LANE_MARKER)."""
    return bool(_LANE_RE.search(body or ""))



def recheck_record(body) -> tuple:
    """(round, cap, heads) from the card's re-check sentinel, or ("", 0, [])."""
    match = _RECHECK_SENTINEL_RE.search(body or "")
    if not match:
        return "", 0, []
    heads = [h for h in match.group(3).split(",") if SHA_RE.fullmatch(h)]
    return str(int(match.group(1))), int(match.group(2)), heads


def rechecks_this_round(body) -> list:
    """The heads re-checked under the panel round the card's heading names.
    A record for any other round (an older panel round) counts as none."""
    round_no, _cap, heads = recheck_record(body)
    current = card_round(body)
    return heads if round_no and current.isdigit() and str(int(current)) == round_no else []


def recheck_sentinel(round_no, cap, heads: list) -> str:
    number = _int_or_q(round_no)
    heads = [h for h in heads if SHA_RE.fullmatch(h or "")][-RECHECK_HEADS_KEPT:]
    if not number.isdigit() or not heads:
        return ""
    cap = max(0, min(int(_int_or_q(cap)) if _int_or_q(cap).isdigit() else 0, 999))
    return f"{RECHECK_SENTINEL_OPENER}{int(number)} {cap} {','.join(heads)} -->"


def recheck_note(n: int, cap: int, head_sha: str, label=DEFAULT_REVIEW_LABEL) -> str:
    """The visible line a re-check's cards carry, under the heading."""
    of = f" of {cap}" if cap else ""
    return (f"🔍 _Approver-only re-check {n}{of} for `{head_sha[:7]}`: this push re-ran the axes only, not the "
            f"panel. To run the full panel on it, {relabel(label)}._")


def _insert_after_heading(body: str, line: str) -> str:
    lines = body.split("\n")
    for i, text in enumerate(lines):
        if _HEADING_RE.match(text):
            return "\n".join(lines[:i + 1] + ["", line] + lines[i + 1:])
    return _append_line(body, line)


def with_recheck_history(body: str, existing_body, recheck_sha=None) -> str:
    """`body` carrying the existing card's re-check record, unless it writes
    its own, and — for a card written for `recheck_sha` (the start and decide
    phases) when that head is the record's latest under this round — the
    visible re-check line, so a re-check's card never reads as a full round."""
    if not _RECHECK_SENTINEL_RE.search(body):
        round_no, cap, heads = recheck_record(existing_body)
        body = _append_line(body, recheck_sentinel(round_no, cap, heads))
    heads = rechecks_this_round(body)
    if recheck_sha and heads and heads[-1] == recheck_sha and not _RECHECK_LINE_RE.search(body):
        _r, cap, _h = recheck_record(body)
        body = _insert_after_heading(body, recheck_note(len(heads), cap, recheck_sha))
    return body


def mark_recheck_scheduled(body: str, head_sha: str, delay_minutes=0, label=DEFAULT_REVIEW_LABEL, now=None) -> str:
    """`body` with the re-check cursor-review's `approve-only` job scheduled,
    announced before its debounce: one visible line, replacing any earlier
    re-check line. A promise it may not keep — a newer push or a relabel
    cancels it — hence the fallback."""
    when = retry_delay_phrase(delay_minutes, "the latest push", now)
    line = (f"⏳ _Approver-only re-check of `{head_sha[:7]}` starts{when} if no further commit lands: the axes "
            f"judge the new head, the panel does not re-run. For the full panel instead, {relabel(label)}._")
    body = _RECHECK_LINE_RE.sub("", body)
    return _insert_after_heading(body.rstrip("\n") + "\n", line).rstrip("\n") + "\n"


def mark_recheck_started(body: str, round_no, cap, head_sha: str, label=DEFAULT_REVIEW_LABEL) -> str:
    """`body` with `head_sha` recorded as re-checked under `round_no` — the
    record that stops a second re-check of the same head — and the visible
    line saying it started."""
    heads = rechecks_this_round(body)
    if head_sha not in heads:
        heads = heads + [head_sha]
    body = _RECHECK_LINE_RE.sub("", _RECHECK_SENTINEL_RE.sub("", body)).rstrip("\n") + "\n"
    body = _insert_after_heading(body, recheck_note(len(heads), int(_int_or_q(cap)) if _int_or_q(cap).isdigit()
                                                    else 0, head_sha, label))
    return _append_line(body, recheck_sentinel(round_no, cap, heads))


def next_recheck_text(label=DEFAULT_REVIEW_LABEL, delay_minutes=0, now=None) -> str:
    """The dismissed card's next step when the push is re-checked by the axes
    alone (`approve_only_on_push`) rather than by a new round."""
    when = retry_delay_phrase(delay_minutes, "the latest push", now)
    return (f"If cursor-approve cannot re-check this push, {relabel(label)}. If it can, cursor-approve "
            f"re-checks the new head automatically{when} if no further commit lands (the axes only; the panel "
            f"does not re-run).")


def next_capped_unlabelled_text() -> str:
    """The capped card's next step when nothing has applied `needs-human-review`
    (a push found the PR at the cap; the round-cap job runs on rounds only)."""
    return ("A human is needed: the round limit is reached, so no round starts by itself, and a human's review "
            "decides this PR. Adding and then removing the `needs-human-review` label resets the round count.")


def render_dismissed(old_body, approved_sha: str, head_sha: str, max_rounds, capped: bool, next_text: str,
                     run_url: str, same_head: bool = False, vetoed: bool = False, why=None,
                     capped_text=None) -> str:
    """The card once the approval of `approved_sha` stopped counting.

    Below the cap: `no_decision` / `relabel` — the new head needs its own
    round. `same_head`: the approval stopped counting with the head where it
    was (the base was retargeted, or the head moved and came back), so a
    relabel is deduped away: `push_then_relabel`, as decide's `retargeted`. At
    the cap (or already handed to a human): `capped` / `human`, as the cap
    path. Vetoed (`skip-cursor-review`): `no_decision` / `human`, as decide's
    `vetoed`. `why` replaces the `same_head` reason in parentheses ("" drops
    it), for an approval withdrawn on its own head and base. `capped_text`
    replaces the capped next step (default: the hand-off's).

    The round heading of the card it replaces is kept (its round, and the
    approved commit), and so is the axes history (`upsert`).
    """
    old7 = approved_sha[:7] if SHA_RE.fullmatch(approved_sha or "") else "unknown"
    new7 = head_sha[:7] if SHA_RE.fullmatch(head_sha or "") else "unknown"
    if why is None:
        why = "the base branch changed, or the head moved and came back"
    moved = (f"Approved `{old7}`; that approval was withdrawn" + (f" ({why})" if why else "")
             if same_head else f"Approved `{old7}`; the head moved to `{new7}`")
    if capped:
        state, next_step, next_text = STATE_CAPPED, NEXT_HUMAN, capped_text or NEXT_HUMAN_CAPPED_TEXT
        headline = f"{moved}, and the round limit is reached, so a human is needed."
    elif vetoed:
        state, next_step, next_text = STATE_NO_DECISION, NEXT_HUMAN, DECIDE_NEXT_TEXT[OUTCOME_VETOED]
        headline = f"{moved}, and the PR carries `skip-cursor-review`."
    elif same_head:
        state, next_step = STATE_NO_DECISION, NEXT_PUSH
        headline = f"{moved}, so this commit needs its own round."
    else:
        state, next_step = STATE_NO_DECISION, NEXT_RELABEL
        headline = f"{moved}, which needs its own round."
    match = _HEADING_RE.search(old_body or "")
    lines = card_head(state, next_step)
    lines += [heading(match.group(1) if match else "", max_rounds if not match else match.group(2), approved_sha), ""]
    lines += [f"**{headline}**", "", f"**Next step:** {reason_text(next_text)}",
              "", _reviewed_line(approved_sha, run_url) if run_url else
              f"_Reviewed commit: `{approved_sha}`_" if SHA_RE.fullmatch(approved_sha or "") else "_Reviewed commit: unknown_",
              "", STALE_HEAD_MARKER.format(head_sha)]
    if in_axes_lane(old_body) and not capped and not vetoed:
        lines += ["", LANE_MARKER]
    return "\n".join(lines) + "\n"


def mark_auto_rereview(body: str, head_sha: str, delay_note: str = "") -> str:
    """`body` (a stale card) with the auto-started round recorded: one visible
    line, and `head_sha` appended to the auto-rereview sentinel."""
    body = body.rstrip("\n")
    heads = rereview_heads(body)
    body = _REREVIEW_SENTINEL_RE.sub("", body).rstrip("\n")
    lines = [body, "", f"🔁 _A new round for `{head_sha[:7]}` was started automatically{delay_note}._",
             "", rereview_sentinel(heads + [head_sha])]
    return "\n".join(lines) + "\n"


def mark_auto_rereview_failed(body: str, head_sha: str, label=DEFAULT_REVIEW_LABEL) -> str:
    """`body` (from `mark_auto_rereview`) once the relabel itself failed: the
    "started automatically" line says it did not, and the record stays."""
    started = re.compile(r"^🔁 _A new round for `" + re.escape(head_sha[:7]) + r"` was started automatically[^\n]*$",
                         re.MULTILINE)
    failed = (f"⚠️ _Starting a new round for `{head_sha[:7]}` automatically failed: "
              f"{relabel(label)} to start it._")
    return started.sub(lambda _: failed, body, count=1)


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
    if state == STATE_CHANGES:
        lines += ["", PANEL_BLOCKED_MARKER]
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
        # A yellow's kind, when the round recorded one. A round written before
        # the split carries none and stays kind-unknown; a stray or unknown kind
        # is dropped rather than failing the round — it is reported, not judged.
        kind = entry.get("yellow_kind")
        if entry["verdict"] == "yellow" and isinstance(kind, str) and kind in YELLOW_KIND_ICONS:
            out[axis]["yellow_kind"] = kind
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
        if entry["verdict"] == "yellow":
            # Normalised as aggregate.py records it: an unsaid kind is `unjudged`.
            kind = entry.get("yellow_kind")
            axes[axis]["yellow_kind"] = kind if isinstance(kind, str) and kind in YELLOW_KIND_ICONS \
                else "unjudged"
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


def _comment(text: str) -> dict:
    """A comment write's response, which must name the comment it wrote."""
    comment = json.loads(text)
    if not isinstance(comment, dict) or comment.get("id") is None:
        raise ValueError(f"the comment write returned no comment id ({type(comment).__name__})")
    return comment


def ensure(repo: str, pr_number, login: str, body: str) -> tuple:
    """(card, created): this PR's card, created with `body` when there is none.

    For a review that links the card, which needs the comment's URL before it
    is posted: this file's ``ensure`` phase, and cursor-review's decide on a
    PR's first round (BE-20246).
    """
    card = find_card(list_comments(repo, pr_number), login)
    if card is not None:
        return card, False
    try:
        return _comment(gh(["api", "-X", "POST", f"repos/{repo}/issues/{pr_number}/comments", "--input", "-"],
                           {"body": body})), True
    except (RuntimeError, ValueError):
        # A POST that failed ambiguously (a timeout, a reply without an id)
        # may have written the card anyway. Look once more, so the caller edits
        # that card by id rather than posting a second one.
        card = find_card(list_comments(repo, pr_number), login)
        if card is None:
            raise
        return card, True


def upsert(repo: str, pr_number, login: str, body: str, entry=None, keep=None, card=None,
           recheck_sha=None) -> dict:
    """Edit the existing card in place, or create it when there is none.

    The card's axes sentinel is carried into `body` (see `with_axes_history`);
    `entry` and `keep` are decide's; `keep` stays None for every other writer.
    `card` is a card this run created seconds ago (``ensure``): it is edited
    without listing the comments again, since a listing taken that soon after
    the write could miss it and post a second card. `recheck_sha` is the
    commit a start or decide card is for (``with_recheck_history``).
    """
    if card is None:
        card = find_card(list_comments(repo, pr_number), login)
    existing = card.get("body") if card is not None else ""
    body = with_rereview_history(with_axes_history(body, existing, entry, keep), existing)
    body = with_recheck_history(body, existing, recheck_sha)
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
    # The commit this round's axes judge: an approver-only re-check shares its
    # panel round's number, so a same-round entry for ANOTHER commit is prior.
    p.add_argument("--commit-sha", default="")
    p = sub.add_parser("closeout")
    p.add_argument("--repo", required=True)
    p.add_argument("--pr-number", required=True)
    p.add_argument("--login", required=True)
    p.add_argument("--commit-sha", required=True)
    p.add_argument("--round", default="")
    p.add_argument("--approve-gate", default="")
    p.add_argument("--max-rounds", default="")
    p.add_argument("--review-label", default=DEFAULT_REVIEW_LABEL)
    p.add_argument("--run-url", default="")
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
    if args.cmd == "closeout":
        return cmd_closeout(args)
    axes = parse_axes(args.axes)
    entry, keep = None, None
    if args.cmd == "start":
        body = render_start(args.round, args.max_rounds, args.commit_sha, axes, args.approve_gate, args.run_url)
    elif args.cmd == "ensure":
        try:
            card, _ = ensure(args.repo, args.pr_number, args.login,
                             render_start(args.round, args.max_rounds, args.commit_sha, axes, "", args.run_url))
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
        upsert(args.repo, args.pr_number, args.login, body, entry, keep, recheck_sha=args.commit_sha)
    except (RuntimeError, ValueError) as e:
        print(f"::warning::Could not update the cursor-approve card: {e}")
    return 0


def _prior_history(args) -> list:
    """The card's rounds that came before this one, at most `--prior-rounds`.

    A re-run of this round (or a later one recorded by a re-run of an older
    round) is not a PRIOR verdict: showing the axis its own output for this
    round would anchor it on itself. The one exception is this round's entry
    for ANOTHER commit (`--commit-sha`): an approver-only re-check keeps its
    panel round's number, and the verdict on the commit before the push is
    exactly the prior it should say is resolved or not. So only rounds below `--round` are kept,
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
    sha = (getattr(args, "commit_sha", "") or "").strip().lower()
    if current.isdigit():
        rounds = [r for r in rounds if r["round"] < int(current)
                  or (r["round"] == int(current) and SHA_RE.fullmatch(sha) and r["commit_sha"] != sha)]
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


_PENDING_ROW_RE = re.compile(r"^\| [a-z]+ \| " + re.escape(PENDING_ROW) + r" \|", re.MULTILINE)


def closeout_body(body, commit_sha: str, round_no, approve_gate: str, max_rounds="",
                  label=DEFAULT_REVIEW_LABEL, run_url: str = ""):
    """(new body or None, why): the close-out of an `axes_alongside_panel` round.

    cursor-review's early start card says every axis is pending while its panel
    runs. When that panel then did NOT pass, nothing else rewrites the card on
    every path (the caller's decide runs only on `pass`), so it would keep
    saying "pending" for a round whose axes' verdicts nobody will read. Only
    THAT card is rewritten: state `pass`, this commit, this round, at least one
    pending axis row. Anything else — a card a decide already wrote, one for
    another commit or round — is left alone, and so is every card on `pass`.
    """
    gate = (approve_gate or "").strip()
    if gate == "pass":
        return None, "the panel passed; the caller's decide writes the verdicts"
    if card_state(body) != STATE_PASS:
        return None, f"the card's state is {card_state(body) or 'unreadable'!r}, not 'pass'"
    if reviewed_sha(body) != (commit_sha or "").strip().lower():
        return None, "the card is for another commit"
    if card_round(body) != _int_or_q(round_no):
        return None, "the card is for another round"
    if not _PENDING_ROW_RE.search(body or ""):
        return None, "the card has no pending axis"
    headline = (f"No decision this round: the review panel did not pass ({gate or 'no verdict'}), "
                "so the axes' verdicts for this commit were discarded.")
    return render_round(round_no, max_rounds, commit_sha, STATE_NO_DECISION, NEXT_PUSH, headline, [],
                        next_push_text(label), run_url), "rewritten"


def cmd_closeout(args) -> int:
    """Rewrite the early start card when its round's panel did not pass.

    Never an error past the arguments, like `prior`: a card that cannot be read
    or written is a warning, and the round's outcome is already decided.
    """
    try:
        found = find_card(list_comments(args.repo, args.pr_number), args.login)
        if found is None:
            print("No cursor-approve card on this PR; nothing to close out.")
            return 0
        body, why = closeout_body(found.get("body") or "", args.commit_sha, args.round, args.approve_gate,
                                  args.max_rounds, args.review_label, args.run_url)
        if body is None:
            print(f"Leaving the cursor-approve card alone: {why}.")
            return 0
        upsert(args.repo, args.pr_number, args.login, body, card=found)
        print("Closed out the early cursor-approve card: the panel did not pass.")
    except Exception as e:  # noqa: BLE001 — a report; the round is already decided
        print(f"::warning::Could not close out the cursor-approve card ({e.__class__.__name__}: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
