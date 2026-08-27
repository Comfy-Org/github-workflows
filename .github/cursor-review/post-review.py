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

# GitHub rejects a review whose body exceeds 65,536 characters. Demoted findings now
# render IN the body (each up to the 20,000-char cap review-output-mcp.py allows), so a
# handful of long ones can overrun a limit the old header-plus-counts body never
# approached. Clamp under it: an oversize body 422s, and the wholesale fallback is
# strictly LARGER than what just failed, so it 422s too and the whole review is lost.
MAX_REVIEW_BODY_CHARS = 60000
# The error review's message is unbounded CLI/model text; bounded well under
# MAX_REVIEW_BODY_CHARS so the header, the fence and the re-trigger line always fit.
MAX_ERROR_MESSAGE_CHARS = 40000
# Longest ``` fence the error review will build. The fence is emitted twice, so this
# bounds the delimiters at 2 * MAX_FENCE_CHARS regardless of what the error text
# contains; a backtick run at or over the cap is broken up rather than out-fenced.
MAX_FENCE_CHARS = 64

# Max re-raises of an already-answered finding allowed in one review (BE-5109).
# Nothing already-answered is ever silently suppressed — a wrong or premature
# deferral must not be able to permanently bury a real Critical — but a round is
# never allowed to be all re-litigation, so the panel gets at most this many
# re-raises, on the record, with the link. Extras are dropped loudly (the review
# body says how many). Kept in sync with REPEAT_CAP in build-ledger.py, which is
# what the judge prompt block quotes.
REPEAT_CAP = 2

# Machine-readable handoff for findings demoted to the review body (BE-9565).
# build-ledger.py derives its entries from review-COMMENT thread roots, and a demoted
# finding has no comment — so without this it never reaches the next round's ledger and
# a fully-demoted round reads as a review that found nothing. The prose below the
# sentinel is what humans read; THIS is the contract, and the version suffix is what
# lets the reader reject a payload it does not understand instead of guessing.
BODY_ONLY_SENTINEL_PREFIX = "cursor-review:body-only-findings v1"
# Mirrors build-ledger.py's MAX_BODY_CHARS: the ledger truncates to that anyway, so
# encoding more only spends review-body budget that the clamp would take back. The
# marker mirrors its TRUNCATION_MARKER for the same reason — cutting to exactly the
# ledger's own limit would leave a cut body indistinguishable from a whole one on the
# reading side, since the ledger's `_truncate` then no-ops and adds no marker of its
# own. Duplicated rather than imported (neither module imports the other); pinned
# together by test_build_ledger.py.
BODY_ONLY_SENTINEL_BODY_CHARS = 600
BODY_ONLY_TRUNCATION_MARKER = " …[truncated]"


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


READ_ONLY_SUMMARY_NOTE = (
    "> ℹ️ This review could not be posted on the PR because the run's "
    "`GITHUB_TOKEN` is read-only (e.g. read-only default workflow "
    "permissions). Posting it here instead.\n\n"
)

POST_FAILED_SUMMARY_NOTE = (
    "> ⚠️ This review could not be posted on the PR (the API rejected the "
    "request). Posting it here instead — see the run log for the error.\n\n"
)

# "as much as fits", not "the full text": write_step_summary budgets against
# MAX_STEP_SUMMARY_BYTES, so an oversize body is cut HERE too and the remainder then
# exists in no channel at all. STEP_SUMMARY_TRUNCATED_NOTE marks where that cut landed;
# this note must not promise more than that one can deliver.
TRUNCATED_SUMMARY_NOTE = (
    "> ℹ️ The review posted on the PR was truncated at GitHub's body-size "
    "limit. As much of it as fits is below.\n\n"
)

# Actions caps $GITHUB_STEP_SUMMARY at 1 MiB per step and discards an overflowing
# upload WHOLE rather than truncating it — so an oversize write loses the summary that
# TRUNCATED_SUMMARY_NOTE and clamp_review_body both point the reader at, leaving the
# review absent past the cut on the PR *and* absent here. Budgeted under the cap in
# BYTES (the limit is on the file, and a finding body is not ASCII-only). Reachable on
# the un-adjudicated panel path: review-output-mcp.py caps only the judge at 10
# findings, reviewer mode has no count cap, and the degraded branch unions all 8 cells.
MAX_STEP_SUMMARY_BYTES = 900_000

# Promises nothing about where the rest is, because there is nowhere honest to point.
# Echoing the whole payload to stdout was tried and reverted: the budget only fires
# near 900 KB, and pushing that into the Actions log made the CI job hang for >13
# minutes against the 8s it takes otherwise — a delivery channel that stalls the run
# is not a delivery channel. It also has no claim about WHAT was cut: this note rides
# the error-review and no-findings summaries too, which carry no severity-ordered list.
STEP_SUMMARY_TRUNCATED_NOTE = (
    "\n\n_…truncated here: this run's job summary reached the Actions per-step size "
    "limit, so the remainder could not be delivered._"
)


def encodable(text: str) -> str:
    """Return `text` with anything UTF-8 cannot encode replaced.

    Python's JSON decoder happily produces a lone surrogate from `"\\ud800"`, and
    review-output-mcp.py's `validate_finding` checks type/non-empty/length only — so a
    finding body can carry one all the way here, where `.encode("utf-8")` (and the
    step-summary file write) would raise `UnicodeEncodeError` and take down the very
    channel that exists so nothing is lost. Round-trip through `errors="replace"` once,
    up front, so every later encode on this text is total.
    """
    return text.encode("utf-8", "replace").decode("utf-8")


def clamp_to_bytes(text: str, limit: int) -> str:
    """Trim `text` to at most `limit` UTF-8 bytes, never splitting a character."""
    text = encodable(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    # errors="ignore" drops a partial trailing sequence rather than emitting U+FFFD.
    return encoded[:limit].decode("utf-8", "ignore")


def write_step_summary(markdown: str, note: str = READ_ONLY_SUMMARY_NOTE) -> None:
    """Render the review into the Actions run summary when the PR copy is lossy.

    The banner says which degradation happened: nothing could be posted (read-only
    token, or the API rejected the request), or the post SUCCEEDED but had to be
    clamped — `clamp_review_body`'s note promises the full text is here, and this is
    what makes that promise true.
    """
    payload = encodable(note + markdown)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        # No summary file (e.g. a local run) — fall back to stdout so the content
        # isn't silently dropped. No budget applies: the cap is on the file.
        print(payload)
        return
    # Leave room for the trailing newline and the note that says where the cut landed.
    budget = MAX_STEP_SUMMARY_BYTES - len(STEP_SUMMARY_TRUNCATED_NOTE.encode("utf-8")) - 1
    if len(payload.encode("utf-8")) > MAX_STEP_SUMMARY_BYTES - 1:
        print(
            "Step summary: content exceeds the Actions per-step size limit — cutting "
            "it rather than letting the whole upload be discarded. The remainder past "
            "the cut is not delivered anywhere.",
            file=sys.stderr,
        )
        payload = clamp_to_bytes(payload, budget).rstrip() + STEP_SUMMARY_TRUNCATED_NOTE
    with open(path, "a", encoding="utf-8") as f:
        f.write(payload + "\n")


def post_or_degrade(repo, pr_number, payload, summary_markdown, context, truncated=False) -> bool:
    """POST a review; degrade to the step summary on a read-only token.

    Returns True when the review was delivered — either posted on the PR, or
    (when the token is read-only) written to the job step summary. Returns
    False only on a genuine POST failure the caller should handle itself
    (e.g. retry without inline anchors).

    `truncated` says the posted body was clamped, so the whole of it goes to the
    summary even on success — otherwise the clamp note points at a summary that
    was never written.
    """
    result = gh_post_review(repo, pr_number, payload)
    if result.returncode == 0:
        if truncated:
            print(
                f"{context}: body hit GitHub's size limit — full text written to "
                "the job summary.",
                file=sys.stderr,
            )
            write_step_summary(summary_markdown, note=TRUNCATED_SUMMARY_NOTE)
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


# `@@ -old_start[,old_count] +new_start[,new_count] @@`. The counts are what let the
# scan know where hunk CONTENT ends, which is what keeps a content line that happens
# to read like a header from being parsed as one.
HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# git's C-style escapes, minus the octal ones handled separately below.
C_ESCAPES = {
    "a": 0x07, "b": 0x08, "f": 0x0C, "n": 0x0A,
    "r": 0x0D, "t": 0x09, "v": 0x0B, "\\": 0x5C, '"': 0x22,
}


def unquote_header_path(target: str):
    """Undo git's C-style quoting of a header path, or return None if it won't decode.

    `core.quotePath` defaults to ON, so any path with a non-ASCII byte, a `"`, a `\\`
    or a control character arrives as `+++ "b/caf\\303\\251.py"`. Used raw, the quotes
    ride along into the key and no finding in that file can ever anchor.

    Only the BACKSLASH escapes are resolved; every other character is taken verbatim.
    That covers both quoting modes: with `quotePath` off git still quotes a name
    containing `"`, `\\` or a control character but leaves its UTF-8 bytes alone, so a
    blanket latin-1 round-trip would mangle a real `é` into U+FFFD (or raise on an
    astral character) and silently strand every finding in that file.
    """
    body = target[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out += ch.encode("utf-8")
            i += 1
            continue
        i += 1
        if i >= len(body):
            return None  # trailing backslash: not quoting git wrote
        esc = body[i]
        if esc in C_ESCAPES:
            out.append(C_ESCAPES[esc])
            i += 1
            continue
        digits = body[i : i + 3]
        if len(digits) == 3 and all(d in "01234567" for d in digits):
            value = int(digits, 8)
            if value > 0xFF:
                return None  # \400+ is not a byte git emits
            out.append(value)
            i += 3
            continue
        return None  # an escape git does not emit — do not guess at the path
    return out.decode("utf-8", "replace")


def header_new_path(raw: str):
    """`+++ <target>` -> the repo-relative new-side path, or None when there is none.

    None covers both `/dev/null` (a delete has no new side) and a header this parser
    cannot decode — both fail SAFE, since a file with no anchors demotes its findings
    to the review body rather than sending a wrong position.
    """
    # rstrip("\r") for a CRLF-terminated diff, but no .strip(): a trailing space is a
    # legal (if unusual) part of a filename, and eating it breaks the key.
    target = raw[4:].rstrip("\r")
    # git terminates the header path with a TAB whenever the name contains a space
    # (verified against git itself: `--- a/my dir/app.py\t`, and `--- "a/caf\\303\\251 x.py"\t`
    # when quoted — the tab lands AFTER the closing quote, so this has to run BEFORE the
    # quoted-path test). Keeping it would key the file as `my dir/app.py\t`, which no
    # finding's `file` can ever match, so every finding in a spacey-path file would lose
    # its anchor. Splitting on the FIRST tab is safe in both quoting modes: a name that
    # really contains a tab is a control character, which git quotes as `\\t` even with
    # `core.quotePath=false`, so an UNQUOTED target never carries a literal one. A legal
    # trailing SPACE still survives (`--- a/sp.py \t` -> `sp.py `).
    target = target.split("\t", 1)[0]
    if len(target) >= 2 and target.startswith('"') and target.endswith('"'):
        target = unquote_header_path(target)
        if target is None:
            return None
    if target == "/dev/null":
        return None
    # git's default prefixes. The reviewed diff is generated by this workflow with a
    # plain `git diff`, so `diff.noprefix`/`diff.mnemonicPrefix` are not in play.
    return target[2:] if target.startswith(("a/", "b/")) else target


def anchorable_lines(diff_text: str):
    """Map new-side path -> the set of line numbers a RIGHT-side comment may anchor to.

    Returns None when the text carries no recognizable diff marker at all — i.e. it is
    not a diff this parser understands, so the caller must fail OPEN rather than demote
    every finding on a map it has no confidence in. An empty/valueless map is a real
    answer: a delete-only, binary-only or mode-only diff genuinely has nowhere to
    anchor, and sending THOSE findings inline is precisely the 422.

    GitHub accepts a review comment only on a line the diff actually carries — added
    or context, inside a hunk. Anything else is rejected, and the rejection is
    WHOLESALE: `POST /pulls/{n}/reviews` takes the comments array as one unit, so a
    single out-of-range position costs every anchor in the request (observed in the
    field: 10 findings, 1 line outside the hunks, 0 comments anchored, HTTP 422).
    Parsing the diff up front lets the 9 good ones land.

    The scan tracks each hunk's remaining line budget from its `@@` counts, so a
    header test only ever runs OUTSIDE hunk content. Without that budget an added
    line reading `++ b/other.py` is emitted as `+++ b/other.py` and would be taken
    for a new-file header, numbering the rest of the file under a spoofed path —
    which is exactly the wrong-position 422 this function exists to prevent.

    Deliberately a hand-rolled scan, matching build-ledger.py/fence-diff.py: the
    consumers run on a stock runner with no third-party diff library available.
    """
    anchors: dict = {}
    path = None
    right = 0
    pending_old = 0
    pending_new = 0
    # True once a line only git's diff format produces has been seen — a `diff --git`
    # or an honoured `+++` header. Until then the text is not a diff at all.
    saw_marker = False
    # `--- ` on the PREVIOUS line. git always emits the old-side header immediately
    # before the new-side one, so requiring the pair is a second guard (after the hunk
    # budget) against a content line being read as a file header.
    saw_old_header = False
    # True once a hunk ended somewhere other than its declared budget. The diff is no
    # longer trustworthy line-by-line from here, so headers stay ignored until a
    # `diff --git` line — which content can never impersonate, since every content
    # line carries a +/-/space/backslash prefix — resynchronizes the scan.
    desynced = False
    # A `diff --git ` line has been seen, i.e. this really is git's own output rather
    # than a bare concatenated `diff -u`. When it is, git ALWAYS emits `diff --git `
    # before each file's `--- `/`+++ ` pair, so a SECOND pair under the same
    # `diff --git ` is not something git wrote — see `awaiting_header` below.
    saw_git_header = False
    # This file's `--- `/`+++ ` pair has not been honoured yet. Set by `diff --git `,
    # cleared by an honoured `+++`. git emits exactly one pair per `diff --git `, so in
    # git-output mode a pair arriving while this is False is content impersonating a
    # header — which is stricter than "inside a hunk region": it also refuses a pair
    # sitting between an honoured `+++` and that file's first `@@`.
    awaiting_header = True
    # The PREVIOUS line was a `--- ` header FORM consumed as hunk content. Both git and
    # a bare `diff -u` emit the old-side header immediately before the new-side one, so
    # a `+++ ` form arriving right after one means an over-declared hunk is eating the
    # next file's header pair — see the `+` arm below.
    ate_old_header = False
    # split("\n") with the trailing element dropped, not splitlines(): splitlines also
    # breaks on \v, \f, \x1c-\x1e, U+0085 and U+2028/9, none of which advance git's
    # line numbering. A form feed in a Python file would otherwise split one content
    # line into two and shift every later anchor by one.
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for raw in lines:
        saw_old_header_before, saw_old_header = saw_old_header, False
        ate_old_header_before, ate_old_header = ate_old_header, False
        if pending_old > 0 or pending_new > 0:
            # Inside hunk content: consume the budget so the hunk's end is known. Each
            # prefix is gated on ITS OWN side's counter, because the branch is entered
            # while EITHER side still has budget: on `@@ -1,2 +1,1 @@` a second `+`
            # would otherwise record an anchor and drive `pending_new` negative, and on
            # a too-large `+count` the next file's `--- `/`+++ ` pair is swallowed as
            # content — `+++ b/y.py` counted as an added line FABRICATES an anchor in
            # the previous file. A side that overruns means the declared counts
            # disagree with the body, which is exactly the untrustworthy state the
            # fall-through below already drops the file for.
            if raw.startswith("+") and pending_new > 0:
                if ate_old_header_before and raw.startswith("+++ "):
                    # Gating each prefix on its own counter (above) only covers an
                    # over-declaration on ONE side. When BOTH are over by one, the next
                    # file's `--- `/`+++ ` pair is swallowed whole — the `--- ` on the
                    # `-` arm, this `+++ ` here — so neither that gate nor the
                    # spent-budget arm below ever fires: counting this line as added
                    # FABRICATES an anchor in the previous file, and the following `@@`
                    # is then honoured with `path` still pointing at it, renumbering the
                    # next file's lines under the previous file's key (observed:
                    # `{"x.py": {1, 2, 3, 500, 501}}`, y.py absent). An eaten header pair
                    # means the declared counts disagree with the body, so drop the file
                    # and stay desynced until a `diff --git ` resynchronizes. A genuine
                    # diff OF a patch file trips this too; demoting that one file's
                    # findings to the review body is the fail-SAFE direction, because a
                    # single wrong anchor costs every anchor in the request.
                    path = None
                    right = 0
                    pending_old = pending_new = 0
                    desynced = True
                    continue
                pending_new -= 1
                if path is not None and right:
                    anchors[path].add(right)
                right += 1
                continue
            if raw.startswith(" ") and pending_old > 0 and pending_new > 0:
                pending_old -= 1
                pending_new -= 1
                if path is not None and right:
                    anchors[path].add(right)
                right += 1
                continue
            if raw.startswith("-") and pending_old > 0:
                # Removed lines advance only the OLD side.
                pending_old -= 1
                ate_old_header = raw.startswith("--- ")
                continue
            if raw.startswith("\\"):
                # `\ No newline at end of file` is a marker, not content.
                continue
            # Not a content line, but the budget says the hunk is not over: the diff
            # went through something lossy (a bare "" where a context " " belongs) or
            # the counts disagree with the body. Drop the file — from here every
            # number would be a guess, and a wrong anchor is what sends back the 422 —
            # and fall through to re-read this line as diff metadata.
            path = None
            right = 0
            pending_old = pending_new = 0
            desynced = True
        elif raw[:1] in ("+", "-", " ") and not (
            (awaiting_header or not saw_git_header)
            and (raw.startswith("--- ") or raw.startswith("+++ "))
        ):
            # The mirror of the branch above. There the declared counts were too LARGE
            # (budget left, content gone); here they were too SMALL — the budget is
            # spent but content lines keep coming. Those lines used to fall through and
            # match nothing, silently, which put the `--- `/`+++ ` header test back in
            # force INSIDE hunk content: a removed `-- x` emits as `--- x`, the added
            # `++ b/app.py` after it emits as `+++ b/app.py`, and the pair would number
            # the rest of the hunk under a real file's key — the wrong-position 422
            # this parser exists to prevent. Desync instead, until a `diff --git`.
            # A `\ No newline` marker legitimately arrives on a spent budget, and the
            # header forms are excluded here because a prefix-less multi-file `diff -u`
            # really does start its next file that way — but ONLY when the input could
            # legitimately be doing that. In git's own output every header pair is
            # preceded by a `diff --git `, so `awaiting_header` is exactly "the pair git
            # owes us has not arrived yet": a `--- ` reaching a spent budget with the
            # pair already honoured is not a header git wrote. Without this gate that
            # `--- ` fell straight through to the metadata branch below — whose
            # `saw_git_header and not awaiting_header` guard sits only on the `+++ `
            # side — leaving `path` and `desynced` untouched, so the NEXT `@@` was
            # honoured under the stale path and fabricated anchors in it.
            path = None
            right = 0
            desynced = True

        if raw.startswith("diff --git "):
            # The one line a content line can never be (every content line carries a
            # +/-/space/backslash prefix), so it resynchronizes the scan. It also opens
            # a new file: park `path` until this file's own `+++` header sets it, so a
            # header pair that fails to parse leaves the file anchorless rather than
            # attributing its lines to the PREVIOUS file.
            desynced = False
            saw_marker = True
            saw_git_header = True
            awaiting_header = True
            path = None
            right = 0
            continue
        if raw.startswith("--- "):
            saw_old_header = True
            continue
        if raw.startswith("+++ "):
            if not saw_old_header_before or desynced or (saw_git_header and not awaiting_header):
                # A `+++` with no `---` in front of it is not a header git wrote. Do not
                # trust it, and drop the current file rather than keep numbering lines
                # that may belong to another one.
                #
                # `saw_git_header and not awaiting_header` closes the remaining seam: if
                # the miscount is exactly two lines, the overflow lines ARE the
                # `--- `/`+++ ` pair and the desync above never fires. git always emits
                # `diff --git ` before a file's header pair and exactly one pair after
                # it, so a SECOND pair in git's own output is content impersonating one
                # — including one landing between the honoured `+++` and the first `@@`,
                # which a hunk-region test would still have honoured. Gated on
                # saw_git_header so a prefix-less concatenated `diff -u`, whose files
                # legitimately follow one another with no `diff --git`, still parses.
                path = None
                right = 0
                desynced = True
                continue
            saw_marker = True
            path = header_new_path(raw)
            right = 0
            awaiting_header = False
            if path is not None:
                anchors.setdefault(path, set())
            continue
        if raw.startswith("@@") and not desynced:
            m = HUNK_HEADER_RE.match(raw)
            if not m:
                # An unparseable hunk header means the following lines cannot be
                # numbered. Drop the file rather than number them from a guess — and
                # desync, because without the hunk's counts the scan no longer knows
                # where its CONTENT ends: a `-- x` / `++ b/evil.py` pair inside that
                # content would otherwise be read as a file header and number the rest
                # under a spoofed path. A `diff --git` line resynchronizes.
                path = None
                right = 0
                desynced = True
                continue
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            if new_start == 0 and new_count > 0:
                # Real diff output pairs a `+0` start only with a count of 0 (an empty
                # new side). `@@ -1,0 +0,3 @@` would number its added lines from 0, and
                # the `path is not None and right` test in the `+` arm reads that 0 as
                # "no hunk header yet" and silently skips it — recording the SECOND and
                # third added lines as 1 and 2, a set shifted by one and anchored on
                # lines the diff never carried. Drop the file and desync, which is what
                # every other unusable-header case in this scan does.
                path = None
                right = 0
                pending_old = pending_new = 0
                desynced = True
                continue
            right = new_start
            pending_old = int(m.group(2)) if m.group(2) is not None else 1
            pending_new = new_count
    if pending_old > 0 or pending_new > 0:
        # The text ran out mid-hunk, so this file's map is a PREFIX of its real one:
        # `@@ -1,3 +1,3 @@` followed by a single ` one` yields `{"x.py": {1}}` and
        # findings on the lines 2 and 3 the real diff carries demote to the body.
        # Say so, because that demotion is otherwise indistinguishable from a finding
        # the model simply put outside the diff.
        #
        # The map is NOT dropped, and this is the one counts-disagree case where that
        # is right: the other arms desync because the scan LOST TRACK of the numbering
        # and would record wrong lines from there on. Here nothing follows — every line
        # already recorded sits inside a real hunk and anchors correctly. Dropping the
        # file (or failing open with None) would cost the good anchors too, and None
        # would additionally send the unanchorable findings inline — the 422 this whole
        # function exists to prevent. A short map still honours the contract: every line
        # in it is one a RIGHT-side comment may use.
        # `path` is None when the cut file has no new side at all (a delete's
        # `+++ /dev/null`), which has nothing to anchor either way — name the file only
        # when there is one to name.
        where = f"{path!r}'s anchors stop" if path is not None else "its anchors stop"
        print(
            f"Anchors: the diff ends mid-hunk, so {where} at the cut — findings past "
            "it will render in the review body.",
            file=sys.stderr,
        )
    if not saw_marker:
        return None
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
    that would silently demote real anchors to prose. "Unusable" means the text is not
    a diff this parser understands — NOT "the diff has no anchors", which is a real
    answer a delete-only or binary-only diff legitimately gives.
    """
    if not diff_path:
        return None
    try:
        # newline="" so universal-newline mode does NOT rewrite a lone \r (or a \r\n)
        # to \n before the parser sees it: a content line carrying a bare CR — mixed
        # line endings, a minified asset — would otherwise be split in two, desyncing
        # the hunk budget or shifting every later anchor in that file. The parser owns
        # the splitting, and header_new_path's rstrip("\r") handles the CRLF case.
        with open(diff_path, encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
    except OSError as e:
        print(f"Anchors: cannot read diff {diff_path} ({e}) — sending all findings inline.", file=sys.stderr)
        return None
    if not text.strip():
        print(f"Anchors: diff {diff_path} is empty — sending all findings inline.", file=sys.stderr)
        return None
    anchors = anchorable_lines(text)
    if anchors is None:
        print(f"Anchors: no file headers parsed from {diff_path} — sending all findings inline.", file=sys.stderr)
        return None
    if not any(anchors.values()):
        # A parsed diff with nowhere to anchor (delete-only, binary-only, mode-only) is
        # a real answer, NOT an unusable one: sending those findings inline is the 422.
        print(f"Anchors: {diff_path} carries no right-side lines — every finding will render in the review body.", file=sys.stderr)
    return anchors


def clamp_review_body(body: str, limit: int = MAX_REVIEW_BODY_CHARS) -> str:
    """Trim a review body to GitHub's size limit, saying where it was cut.

    Also the choke point every POSTed body passes through, so the surrogate scrub runs
    here too — see the note in normalize_comments. Applied before the length test so a
    replacement never lands after the cut was measured.
    """
    body = encodable(body)
    if len(body) <= limit:
        return body
    note = (
        "\n\n_…truncated here: the review body reached GitHub's size limit. As much "
        "of it as fits is in the job summary of this run._"
    )
    if limit <= len(note):
        # Degenerate limit (tests, a future tightening): the cut still has to hold.
        return body[:limit]
    return body[: limit - len(note)].rstrip() + note


# Every line ending CommonMark recognizes. `\r` alone is one of them, so a blockquote
# built by splitting on `\n` only would leak the text after a bare CR out of the quote.
MD_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


def render_code_ref(path, line) -> str:
    """Render a `path:line` reference safe to drop into markdown.

    `path` is model-supplied and only checked upstream for traversal (absolute paths,
    backslashes, NUL, `..`), so it can still carry backticks — which close the code
    span — `@`, which fires a live mention from the bot account, and newlines, which
    forge sections in the posted review. `body` already goes through
    neutralize_mentions in normalize_comments; this closes the same hole on the path,
    which demotion to the body made a routine render rather than a rare fallback one.
    """
    text = neutralize_mentions(path).replace("\r", " ").replace("\n", " ")
    text = f"{text}:{line}"
    # CommonMark: a code span may contain backticks as long as its fence is longer
    # than the longest backtick run inside it.
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # A leading backtick would otherwise merge into the fence; one space is stripped
    # back off by the renderer only when BOTH ends are padded, so pad both.
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def render_finding_entry(c: dict) -> str:
    """One finding, as a blockquote its own markdown cannot break out of.

    `c["body"]` is model output derived from PR content and only passes through
    neutralize_mentions, which touches mentions and nothing structural. Interpolated
    flat, it could open a heading, a rule, a forged `**path:line** — …` row, or an
    unterminated code fence that swallows the rest of the review — and demotion turned
    that from a rare fallback render into a per-run one. Inside a blockquote the
    damage is confined: the block ends at the blank line before the next finding, so
    an unclosed fence closes with it.
    """
    text = f"**{render_code_ref(c['path'], c['line'])}** — {c['body']}"
    # MD_LINE_BREAK_RE, not split("\n"): CommonMark (and GitHub's cmark-gfm) ends a
    # line on a bare \r too, and nothing upstream strips control characters —
    # review-output-mcp.py's validate_finding checks only type/non-empty/length, and
    # neutralize_mentions touches `@` alone. A body of "safe\r## Forged" split on \n
    # alone keeps the heading inside ONE element, so it is emitted with no "> " prefix
    # and renders outside the blockquote — the exact forged-heading escape this
    # function exists to contain.
    return "\n".join(f"> {ln}" if ln else ">" for ln in MD_LINE_BREAK_RE.split(text))


def strip_severity_badge(severity: str, body: str) -> str:
    """Remove the badge normalize_comments prefixed, leaving the finding's own prose.

    Reconstructed from the same two tables that built it rather than re-matched with a
    regex, so the two can never drift: if the badge format changes, this stops matching
    and the sentinel carries a badge-prefixed body — cosmetic — instead of silently
    eating the first line of a finding the way a loose pattern would.
    """
    badge = f"{SEVERITY_EMOJI.get(severity, '')} **{SEVERITY_LABEL.get(severity, '')}** — "
    return body[len(badge):] if body.startswith(badge) else body


def truncate_sentinel_body(body: str) -> str:
    """Cut a finding body to the ledger's own limit, marked, so the cut is visible.

    Sized so the RESULT is within the limit: build-ledger.py truncates to the same
    number, so a body cut to exactly it would arrive looking complete.
    """
    if len(body) <= BODY_ONLY_SENTINEL_BODY_CHARS:
        return body
    keep = BODY_ONLY_SENTINEL_BODY_CHARS - len(BODY_ONLY_TRUNCATION_MARKER)
    return body[:keep].rstrip() + BODY_ONLY_TRUNCATION_MARKER


def render_body_only_sentinel(items: list) -> str:
    """The machine-readable half of the demoted-findings section (BE-9565).

    One HTML comment carrying the demoted findings as compact JSON, so build-ledger.py
    can recover from the posted review body what has no thread to be recovered from.

    The `-` escaping is the load-bearing part. Finding bodies are model output derived
    from PR content, so one can contain `-->` and close the comment early — which would
    spill the remainder of the JSON into the rendered review AND hand the ledger a
    truncated payload. Escaping EVERY `-` as `\\u002d` after encoding (JSON decodes it
    back to `-`) removes the character entirely, so no `--` run can exist inside the
    comment at all. Post-encode is the only place this works: escaping before encoding
    would have json.dumps escape the backslash and the reader would decode six literal
    characters instead of a dash.

    That blanket replace is safe because the only JSON tokens outside string literals
    here are `[`, `]`, `{`, `}`, `,`, `:` and the digits of `line` — normalize_comments
    guarantees `line` is a POSITIVE int, so no `-` can appear as a number's sign.
    """
    payload = [
        {
            # neutralize_mentions, like render_code_ref does for the prose half. The
            # body was already neutralized in normalize_comments, but `path` is raw
            # model output until it is rendered — and this render is still a POSTed
            # review body, so an `@handle` in it would fire a live mention from the bot
            # account. An HTML comment is not a reliable place to hide one.
            "path": neutralize_mentions(item["comment"]["path"]),
            "line": item["comment"]["line"],
            "severity": item["severity"],
            "body": truncate_sentinel_body(
                strip_severity_badge(item["severity"], item["comment"]["body"])
            ),
        }
        for item in items
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    escaped = encoded.replace("-", "\\u002d")
    return f"<!-- {BODY_ONLY_SENTINEL_PREFIX} {escaped} -->"


def render_body_only_findings(items: list) -> str:
    """Render findings that could not be anchored, for inclusion in the review body."""
    if not items:
        return ""
    # Sentinel FIRST, before the prose. clamp_review_body cuts the tail, so anchoring
    # the machine-readable copy at the head of the section is what keeps it recoverable
    # for as long as any of the section survives the clamp at all.
    md = (
        f"{render_body_only_sentinel(items)}\n\n"
        "_The finding(s) below could not be anchored to a line the reviewed diff "
        "carries, so they are reported here instead of inline:_\n\n"
    )
    for item in items:
        md += render_finding_entry(item["comment"]) + "\n\n"
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
            md += render_finding_entry(c) + "\n\n"
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
                    # encodable() here, not just in write_step_summary: json.load turns
                    # `"\ud800"` into a lone surrogate and review-output-mcp.py's
                    # validate_finding checks only type/non-empty/length, so one rides
                    # this far. json.dumps would then hand the API a literal \ud800
                    # escape; if GitHub rejects it the wholesale fallback carries the
                    # same escape and fails identically, leaving the review only in the
                    # job summary — the one copy that WAS sanitized, so the two channels
                    # would disagree on the finding's text.
                    "body": encodable(badge + neutralize_mentions(body) + repeat_line),
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
    """Post the "why the review failed" review, with the message bounded and fenced.

    `error_message` is `$JUDGE_ERROR` on the judge-failure path — read straight out of
    judge-findings.json's `error` field, i.e. unbounded CLI/model text. Over GitHub's
    65,536-char body limit the POST 422s and this raises, losing the error review
    entirely: the one path whose whole job is to report why the review failed. So the
    message is cut to its own budget (which keeps the trailing re-trigger instruction,
    where clamping the assembled body would drop it and leave the fence open), and the
    body is clamped afterwards as a hard guarantee.
    """
    safe = neutralize_mentions(error_message)
    # Break the fence-defeating runs BEFORE the length budget, so the markers this
    # substitution inserts are themselves inside the budget rather than added after it.
    if re.search("`" * MAX_FENCE_CHARS, safe):
        safe = re.sub(
            "`{%d,}" % MAX_FENCE_CHARS,
            "`" * (MAX_FENCE_CHARS - 1) + "…(backtick run truncated)",
            safe,
        )
    if len(safe) > MAX_ERROR_MESSAGE_CHARS:
        safe = safe[:MAX_ERROR_MESSAGE_CHARS].rstrip() + (
            "\n…(truncated: the error text hit the review body's size limit — see "
            "the run log for the whole of it)"
        )
    # A ``` run inside the message would close the fence early and let the rest of the
    # error render as markdown; CommonMark lets the fence be longer instead. But the
    # fence is emitted TWICE and `safe` may be MAX_ERROR_MESSAGE_CHARS of nothing but
    # backticks — a degenerate model/CLI repetition loop is both what fills `error` and
    # what produces a run that long — so an unbounded fence blows past
    # MAX_REVIEW_BODY_CHARS on the delimiters alone and `clamp_review_body` cuts the
    # END: exactly the closing fence and the re-trigger line the message-side budget
    # exists to preserve. Break any run at or over the cap instead; a run that long is
    # noise, and no fence could contain it inside the body budget anyway.
    longest = min(
        max((len(run) for run in re.findall(r"`+", safe)), default=0),
        MAX_FENCE_CHARS - 1,
    )
    fence = "`" * max(3, longest + 1)
    unclamped = (
        f"{header}\n\n⚠️ **Review failed**\n\n{fence}\n{safe}\n{fence}\n\n"
        "Re-trigger by removing and re-adding the `cursor-review` label."
    )
    body_text = clamp_review_body(unclamped)
    payload = json.dumps(
        {"body": body_text, "event": "COMMENT", "commit_id": commit_sha}
    )
    # The whole text as the summary copy, not the already-cut one — the read-only
    # degradation exists to deliver what the PR could not — and `truncated` so a
    # SUCCESSFUL but clamped POST still writes the summary its clamp note points at.
    if not post_or_degrade(
        repo,
        pr_number,
        payload,
        unclamped,
        "Error review",
        truncated=body_text != unclamped,
    ):
        # Same contract as the review paths: a genuine POST failure still delivers the
        # text somewhere. post_or_degrade writes the summary itself on the paths that
        # return True, so this cannot double-write.
        write_step_summary(unclamped, note=POST_FAILED_SUMMARY_NOTE)
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
            # Same contract as the other three exit paths: a genuine POST failure still
            # delivers the text somewhere. On the all-failed branch that text is the one
            # artifact explaining why no review happened — the panel summary naming
            # which cells errored — so losing it is exactly when it is needed most.
            write_step_summary(body_text, note=POST_FAILED_SUMMARY_NOTE)
            raise SystemExit(1)
        return

    enriched = normalize_comments(findings)
    enriched, repeats_dropped = enforce_repeat_cap(enriched)
    # Anchor-aware split. The COUNT below stays the total across both halves — a finding
    # that lands in the body is still a finding, and a headline that shrank because an
    # anchor missed would misreport the review.
    inline_items, body_only_items = partition_by_anchor(enriched, load_anchors(args.diff))
    comments = [item["comment"] for item in inline_items]

    # The head is every finding-independent part of the review. Kept separate from the
    # demoted-findings block below so the body-only renders (step summary, wholesale
    # fallback) can list ALL findings once, in severity order, instead of appending the
    # inline half AFTER a block that already ends with the demoted half — which put a
    # demoted nit ahead of a lost critical and made the size clamp cut the wrong end.
    review_head = f"{header}\n\nFound **{len(enriched)}** finding(s)."
    if repeats_dropped:
        review_head += (
            f"\n\n_{repeats_dropped} re-raise(s) of already-answered findings were dropped "
            f"(cap: {REPEAT_CAP} per review). They are still open on their original threads._"
        )
    severity_summary = build_severity_summary(enriched)
    if severity_summary:
        review_head += f"\n\n{severity_summary}"
    if panel_summary:
        review_head += f"\n\n{panel_summary}"
    if not enriched and findings:
        review_head += "\n\n_(All findings had invalid file/line references and were dropped.)_"

    review_body = review_head
    body_only_md = render_body_only_findings(body_only_items)
    if body_only_md:
        # A demoted finding still carries no THREAD — there is no place to answer or
        # resolve it — but since BE-9565 it does reach the next round's ledger: the
        # section opens with the `cursor-review:body-only-findings` sentinel that
        # build-ledger.py parses back out of this body, so a fully-demoted round can no
        # longer read as a review that found nothing. Those entries are permanently
        # UNANSWERED, hence cap-exempt, which is the same rule an unanswered thread
        # already gets. What is still open: the demoted half of a WHOLESALE fallback
        # body (the 422 path below) carries no sentinel, because it is not rendered
        # through render_body_only_findings.
        review_body += f"\n\n---\n\n{body_only_md}"

    # Every finding, most → least urgent, for any render that has no inline half.
    prose_body = render_findings_markdown(review_head, [i["comment"] for i in enriched])

    posted_body = clamp_review_body(review_body)
    payload = json.dumps(
        {
            "body": posted_body,
            "event": "COMMENT",
            "commit_id": args.commit_sha,
            "comments": comments,
        }
    )

    result = gh_post_review(args.repo, args.pr_number, payload)

    if result.returncode == 0:
        if posted_body != review_body:
            # The clamp note tells the reader the full text is in the job summary.
            # Nothing else on this path writes one, so write it here or the note lies
            # and the cut findings are gone from both places.
            print(
                "Review: body hit GitHub's size limit — full text written to the "
                "job summary.",
                file=sys.stderr,
            )
            write_step_summary(prose_body, note=TRUNCATED_SUMMARY_NOTE)
        return

    # A read-only token rejects any write, so the inline-less fallback below
    # would fail the same way — degrade straight to the job summary instead.
    if is_read_only_token_error(result):
        print(
            "Review: token is read-only — writing the review to the job "
            "summary instead of the PR.",
            file=sys.stderr,
        )
        write_step_summary(prose_body)
        return

    print(f"Review POST failed: {result.stderr}", file=sys.stderr)
    if not comments:
        # There is no inline half to drop, so a fallback POST would carry the same
        # findings as the request that just failed (only the demotion intro and the
        # anchor note differ) — it cannot fix a size or malformed-body rejection, and
        # if GitHub committed the write before erroring it publishes a DUPLICATE
        # review no one can un-post. That duplicate risk, not byte-identity, is the
        # reason to skip it. Deliver the text to the summary and let the step go red.
        print(
            "Review: no inline comments to drop — the fallback would repost the same "
            "body, so writing it to the job summary instead.",
            file=sys.stderr,
        )
        write_step_summary(prose_body, note=POST_FAILED_SUMMARY_NOTE)
        raise SystemExit(1)

    # Fallback: same findings without inline anchors. Typical cause is line
    # numbers that fall outside the diff context — often the model picked
    # a line near the change but not on the change.
    fallback_body = (
        prose_body
        + "\n_(Inline comments could not be anchored to the diff; listed above instead.)_"
    )
    clamped_fallback = clamp_review_body(fallback_body)
    fallback_payload = json.dumps(
        {
            # Clamped for the API; the step-summary copy stays whole.
            "body": clamped_fallback,
            "event": "COMMENT",
            "commit_id": args.commit_sha,
        }
    )
    if not post_or_degrade(
        args.repo,
        args.pr_number,
        fallback_payload,
        fallback_body,
        "Fallback review",
        truncated=clamped_fallback != fallback_body,
    ):
        # Both attempts failed for a non-403 reason (an API outage, a stale commit_id
        # after a force-push, a body-level rejection dropping the anchors cannot fix).
        # Without this the whole review is gone from the PR *and* the summary, which
        # contradicts the no-inline branch above — and this is the branch carrying
        # MORE content, since it has an inline half. post_or_degrade only writes a
        # summary on the paths that return True, so there is no double write here.
        write_step_summary(fallback_body, note=POST_FAILED_SUMMARY_NOTE)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
