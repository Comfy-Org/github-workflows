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

# The shape a `repeat_of` may have when it travels STRUCTURALLY through the body-only
# sentinel (BE-12534). A demoted re-raise loses its lineage otherwise: the trailer is
# stripped out of the sentinel's prose (see strip_repeat_line) and, with no field to
# carry it, build-ledger.py rebuilt the entry as a fresh unanchorable finding — so the
# next round's re-raise of the SAME finding needed no repeat_of and cost no repeat slot.
# One demoted hop made every later hop of that chain cap-free.
#
# This is a SHAPE guard, not the integrity check: it bounds what can be written into a
# public review body and re-read from it, so the payload can never carry a link to an
# arbitrary host, a whitespace/line-break run, or an unbounded string. What actually
# decides whether the lineage is real is on the reader — build-ledger.py resolves the
# id to a ROOT comment of one of OUR consolidated reviews, so a judge-hallucinated or
# foreign URL of exactly this shape still resolves to nothing. Kept as a literal
# pattern rather than a compiled-only object because build-ledger.py duplicates it
# (neither module imports the other) and test_build_ledger.py pins the two together.
REPEAT_URL_PATTERN = r"^https://github\.com/[^/\s]+/[^/\s]+/pull/\d+#discussion_r\d+$"
REPEAT_URL_RE = re.compile(REPEAT_URL_PATTERN)
REPEAT_URL_MAX_CHARS = 512

# Machine-readable handoff for findings demoted to the review body (BE-9565).
# build-ledger.py derives its entries from review-COMMENT thread roots, and a demoted
# finding has no comment — so without this it never reaches the next round's ledger and
# a fully-demoted round reads as a review that found nothing. The prose below the
# sentinel is what humans read; THIS is the contract, and the version suffix is what
# lets the reader reject a payload it does not understand instead of guessing.
BODY_ONLY_SENTINEL_PREFIX = "cursor-review:body-only-findings v1"

# --- the blocking gate's delivery signal (BE-4691) -------------------------
# `needs.post-review.result == 'success'` cannot stand in for "a review carrying
# resolvable finding threads landed on the PR": this script exits 0 after a
# read-only-token 403 (the review went to the job summary, not the PR), after the
# body-only "Review failed" error review, after the no-findings review a run posts
# when every panel cell errored, and after the 422 inline-anchor fallback. Each of
# those satisfies a zero-exit guard while the gate's thread query legitimately finds
# nothing — a green required check over a round that never reviewed anything, which
# is the fail-OPEN the gate exists to prevent. So say it POSITIVELY instead, and let
# the gate require the statement rather than infer it from an exit code.
#
#   delivered         true only when a real review — one whose findings were actually
#                     adjudicated — reached the PR itself.
#   gated_findings    findings that carry an inline thread, i.e. that a human can
#                     resolve and that the gate can therefore hold the merge on.
#   ungated_findings  findings that reached the review BODY only (anchor missed the
#                     reviewed diff, or the whole inline payload 422'd). Real
#                     findings with nowhere to reply — no thread resolution can ever
#                     clear them, so the gate must not read their absence as clean.
#   posted            true when SOME review body reached the PR, whatever it said.
#                     Strictly weaker than `delivered` and deliberately so: it is
#                     what `notify-complete`'s DM needs, and the DM asks a different
#                     question than the gate. The gate asks "did an adjudicated
#                     review land"; the DM claims "one consolidated review is on the
#                     PR", which is true of the error review and the all-cells-failed
#                     review (both `delivered=false`) and false of the read-only-403
#                     degradation, where the review reached only the job summary and
#                     the script still exits 0. Keying the DM on the job result alone
#                     made that last case a silent green — a success DM pointing at a
#                     PR carrying no review. `delivered` implies `posted`; the
#                     converse does not hold.
#
# Emitted exactly once (first call wins): the paths below can fall through each
# other, and duplicate keys in $GITHUB_OUTPUT are ambiguous. That once-guard is also
# why `posted` rides along here rather than in an emitter of its own — it is decided
# by the same branches, so a second emitter would need a second guard kept in sync
# with this one across every path below. Nothing is written when the script dies
# before deciding, which leaves the gate reading an empty `delivered` and the DM an
# empty `posted` — false, i.e. fail-closed by default on both.
_DELIVERY_EMITTED = False


def emit_delivery(
    delivered: bool, gated: int = 0, ungated: int = 0, posted: bool = False
) -> None:
    """Write the blocking gate's delivery signal to $GITHUB_OUTPUT."""
    global _DELIVERY_EMITTED
    if _DELIVERY_EMITTED:
        return
    _DELIVERY_EMITTED = True
    # `delivered` without `posted` would be incoherent — an adjudicated review that
    # never reached the PR — and would green the gate while the DM reported a
    # degradation. No call site produces it today (both `delivered=True` sites post
    # first and pass `posted=True`); this NORMALIZES rather than asserts, so a future
    # one that forgets the kwarg cannot emit the incoherent pair. Not a raise on
    # purpose: this runs immediately AFTER a review was successfully posted, so
    # raising would turn a kwarg slip into a red check and a "did not succeed" DM on
    # a PR that has its review — strictly worse than the coherent output.
    #
    # The opposite direction — a body that DID post whose site forgets `posted=True`
    # — is not repairable here (nothing in this function can see the POST), so it
    # stays each call site's job and the parameter defaults to the fail-closed
    # `False`. Its blast radius is a spurious "degraded" DM, never a false success.
    # The stderr line below prints both keys so a run log shows which pair was
    # emitted without re-reading $GITHUB_OUTPUT.
    posted = posted or delivered
    print(
        f"delivery: delivered={'true' if delivered else 'false'} "
        f"gated_findings={gated} ungated_findings={ungated} "
        f"posted={'true' if posted else 'false'}",
        file=sys.stderr,
    )
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"delivered={'true' if delivered else 'false'}\n")
        f.write(f"gated_findings={gated}\n")
        f.write(f"ungated_findings={ungated}\n")
        f.write(f"posted={'true' if posted else 'false'}\n")
# Mirrors build-ledger.py's MAX_BODY_CHARS: the ledger truncates to that anyway, so
# encoding more only spends review-body budget that the clamp would take back. The
# marker mirrors its TRUNCATION_MARKER for the same reason — cutting to exactly the
# ledger's own limit would leave a cut body indistinguishable from a whole one on the
# reading side, since the ledger's `_truncate` then no-ops and adds no marker of its
# own. Duplicated rather than imported (neither module imports the other); pinned
# together by test_build_ledger.py.
BODY_ONLY_SENTINEL_BODY_CHARS = 600
# The prose half of the body-only contract. build-ledger.py keys on this sentence to
# tell "this round demoted findings and the sentinel is unreadable" apart from "this
# round demoted nothing", so it is a constant here rather than a literal in the render.
BODY_ONLY_PROSE_MARKER = "could not be anchored to a line the reviewed diff carries"
BODY_ONLY_TRUNCATION_MARKER = " …[truncated]"
# What render_findings_markdown puts between the head and the first finding. A
# constant because the wholesale fallback's size guard has to measure everything that
# precedes finding one, and a guard that re-spelled this separator would drift from it.
FINDINGS_SEPARATOR = "\n\n---\n\n"
# What clamp_review_body appends in place of what it cut. Also a constant because the
# same size guard has to RESERVE it: the clamp cuts at `limit - len(note)`, so a head
# that merely fits under the limit can still have its tail — the sentinel — taken.
CLAMP_TRUNCATION_NOTE = (
    "\n\n_…truncated here: the review body reached GitHub's size limit. As much "
    "of it as fits is in the job summary of this run._"
)
# The share of the fallback body the sentinel may take. It has TWO readers and the
# HUMAN comes first: the prose findings are the review a person actually reads on the
# PR, and the sentinel is a best-effort machine-readable copy for next round's ledger.
# Uncapped, the sentinel wins that contest — its per-finding JSON is nearly as long as
# the prose entry it duplicates, so it can consume the whole budget ahead of finding
# one and leave the clamp nothing but the head to keep. Measured before this cap: 89
# findings of ~700 chars posted 58,720 characters of JSON and rendered ZERO findings,
# while the same round at 90 findings — one over the all-or-nothing guard, so the
# sentinel was dropped whole — rendered 79 of them. The cliff ran the wrong way.
# Half the budget is the prose FLOOR; the sentinel takes the most-urgent prefix of the
# findings that fits the other half (see fit_sentinel_items).
FALLBACK_SENTINEL_MAX_CHARS = MAX_REVIEW_BODY_CHARS // 2


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


# The 403 wordings that mean "slow down", not "you may not write". GitHub answers a
# primary rate limit, a secondary rate limit and abuse detection with 403 as readily
# as with 429 — and, unlike every other 403, one of those can be raised on a request
# the API went on to SERVE, so it is not evidence the write was rejected before it was
# committed. Matched case-insensitively as substrings, which is how `gh` hands over
# GitHub's `message`: echoed into stderr rather than parsed out of the JSON body.
# Only ever consulted once the status is already known to be 403, so a finding body
# quoting one of these phrases cannot reach it through a 422 — and matched against
# `gh`'s own error LINE rather than the whole blob, so it cannot reach it through the
# same PR's review body either (see gh_error_line).
THROTTLE_403_MESSAGES = (
    # "API rate limit exceeded for ..." / "You have exceeded a secondary rate limit."
    "rate limit",
    # "You have triggered an abuse detection mechanism."
    "abuse detection",
    # the older wording of the same secondary-limit refusal
    "submitted too quickly",
)


def is_throttled_403(result: subprocess.CompletedProcess) -> bool:
    """True when a 403's message is GitHub asking us to slow down.

    Read out of `gh`'s error line, not out of all of stderr. With `GH_DEBUG=api` set
    on the step — a documented `gh` knob a caller workflow can add — stderr also
    carries the request trace, which echoes the review body being POSTed; a review
    that DISCUSSES rate-limit handling would otherwise turn a standing permission 403
    into a "throttle", costing it a doomed read, a doomed fallback and a red step
    where it used to degrade green.
    """
    line = gh_error_line(result).lower()
    return any(message in line for message in THROTTLE_403_MESSAGES)


def is_read_only_token_error(result: subprocess.CompletedProcess) -> bool:
    """True when the POST failed because the ENVIRONMENT forbids writing to the PR.

    The gate skips fork PRs (which always hit this), but a read-only token can
    still occur on same-repo runs — org/repo default workflow permissions set
    to read-only, or events that downgrade the token. GitHub answers those with
    HTTP 403 'Resource not accessible by integration'. That's an environment
    constraint, not a review failure, so callers degrade to the job summary
    rather than failing the check red.

    STATUS AND MESSAGE, not either alone (BE-12612).

    The status must be 403. The permission wording also travels inside a 422's
    `errors[].message` list, which `gh` joins into the same stderr blob, and reading
    that as a read-only token would return from `main()` before the landed-review
    check — the same silent skip this guard is being narrowed to remove, arrived at
    from the other direction.

    The message must then NOT be a throttle. A rate limit and an abuse-detection
    refusal are neither an environment constraint nor proof that nothing was written,
    so those alone fall through to the landed-review check (see
    RETRYABLE_4XX_STATUSES). Everything else a 403 can carry — the permission refusal
    above, whatever principal it names (`by integration` for both of
    cursor-review.yml's token arms, `by personal access token` for a fine-grained
    PAT), an SSO/IP-allowlist
    or org-policy block, an archived repo, a future rewording of any of them — is a
    STANDING refusal that no retry fixes and that wrote nothing, so it degrades to the
    job summary. Matching the throttles rather than the permission phrase is what
    keeps a SAML-blocked or IP-allowlisted org on that green degrade instead of the
    permanently red check "everything but the permission phrase" would hand it.

    The fall-through reaches the landed-review check on ALL THREE failure paths —
    `main()`'s inline branch, its no-inline-comments branch, and `post_or_degrade` —
    because `post_may_have_landed` gates each of them the same way. A throttle raised
    on a request GitHub went on to serve is therefore reported as delivered wherever
    it happens, rather than red with `posted=false` over a review sitting on the PR.
    Only the inline path REPOSTS on the other two answers; the other two read and,
    unless the answer is PRESENT, behave exactly as they always have.

    One residual, named rather than implied: the read runs on the same token GitHub
    just throttled, so it can be throttled too. That yields UNKNOWN, and on the inline
    path UNKNOWN posts the fallback — which duplicates a first write that did land.
    Closing that needs `Retry-After`/backoff, which `gh api` does not surface on the
    default path; it is tracked separately (BE-12679) rather than half-done here.
    """
    return gh_http_status(result) == 403 and not is_throttled_403(result)


# The discriminator for "a review of THIS panel is already on the PR". Mirrors
# gate-unresolved.py's constant of the same name (and the inline jq the workflow's
# dup-check uses) — duplicated as a literal rather than imported because neither module
# imports the other, and pinned equal to the gate's by test_post_review.py, exactly the
# way build-ledger.py pins its own copy.
CONSOLIDATED_MARKER = "## 🔍 Cursor Review — Consolidated panel"

# `gh` reports an API error's HTTP status in its stderr, but not in ONE shape. The
# common `gh api` rendering trails it in parentheses (`gh: Unprocessable Entity
# (HTTP 422)`), but go-gh's HTTPError leads with it whenever it has a request URL to
# report and either no message at all — `HTTP 403 (https://api.github.com/...)`, what
# a proxy, a WAF or a GHES edge that sent no JSON body produces — or a message plus an
# `errors[]` tail, `HTTP 422: Validation Failed (https://...)\n<rest>`. Both
# alternatives are matched.
#
# Matching only the parenthesized trailer was a REGRESSION risk once
# `is_read_only_token_error` began conjoining the status (BE-12612): the leading
# shapes would have read as "no status at all", so a STANDING permission or SSO 403
# rendered that way would leave the green degrade for a doomed read, a doomed fallback
# and SystemExit(1) on every run — which the bare `"HTTP 403" in blob` it replaced did
# not do.
#
# A transport failure (DNS, TLS, a dropped connection) carries no status in either
# shape, which is why the caller treats "no match" as unknown rather than as a server
# error.
_GH_HTTP_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)|\bHTTP (\d{3})\b")


def _gh_status_match(result: subprocess.CompletedProcess):
    """The LAST status rendering on stderr, as a match, or None if there is none.

    Last, not first: with `GH_DEBUG=api` stderr also carries the request/response
    trace, and the request it echoes is the review body being POSTed — which can quote
    anything, `HTTP 403` included. `gh` writes its own error AFTER that trace, so the
    final match is the one describing the response rather than something quoting one.
    """
    matches = list(_GH_HTTP_STATUS_RE.finditer(result.stderr or ""))
    return matches[-1] if matches else None


def gh_http_status(result: subprocess.CompletedProcess):
    """The HTTP status `gh` reported on stderr, or None when it reported none."""
    match = _gh_status_match(result)
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def gh_error_line(result: subprocess.CompletedProcess) -> str:
    """The one stderr line carrying that status — `gh`'s own error line, or "".

    The scope for anything that reads the WORDING of a failure. Both of `gh`'s
    renderings put GitHub's `message` on the same line as the status, so this is all
    of what GitHub said and none of what an `errors[]` continuation, a `GH_DEBUG=api`
    trace or a quoted review body put around it.
    """
    match = _gh_status_match(result)
    if match is None:
        return ""
    blob = result.stderr or ""
    start = blob.rfind("\n", 0, match.start()) + 1
    end = blob.find("\n", match.end())
    return blob[start:] if end == -1 else blob[start:end]


# 4xx statuses that are NOT evidence the request was rejected before it was written.
# The no-read short-circuit rests on a 4xx meaning "GitHub validated this and refused
# it", which holds for the rejections it was built for (422 over an inline position,
# and the 4xx family of malformed/unauthorized/absent) but NOT for these: a timeout,
# a secondary rate limit or an early-hint refusal can come from an edge or a proxy in
# front of GitHub, about a request the API went on to serve. Treating one of those as
# "absent by construction" would repost the fallback and tag every anchored finding
# `lost_to_fallback` on an assumption that does not apply, so they take the read like
# a 5xx does.
#
# 403 is here because `is_read_only_token_error` now excludes the throttle wordings:
# a 403 that reaches THIS decision has already been classified as one of them, so it
# is a rate limit or an abuse-detection refusal and nothing else — every standing
# 403 (permission, SSO/IP allowlist, archived repo) returned from `main()` on the
# degrade path well above. A throttle can be raised on a request the API went on to
# serve, exactly like the 429 beside it, so it takes the read too.
RETRYABLE_4XX_STATUSES = frozenset({403, 408, 425, 429})


def post_may_have_landed(result: subprocess.CompletedProcess) -> bool:
    """Could GitHub have committed this write despite erroring on the request?

    False only for the 4xx that mean "GitHub VALIDATED this and refused it before
    writing anything" — every 4xx outside RETRYABLE_4XX_STATUSES, whose members are
    4xx without carrying that meaning. A 5xx, and a transport error with no status at
    all, leave the write genuinely undecided.

    True is not "the review landed"; it is "the PR is worth asking". Shared by all
    three failure paths so the question is answered the same way on each — the
    no-inline branch and post_or_degrade used to skip it entirely, which reported
    `posted=false` for a review that was on the PR the whole time (BE-12612).
    """
    status = gh_http_status(result)
    return not (
        status is not None
        and 400 <= status < 500
        and status not in RETRYABLE_4XX_STATUSES
    )


# This read sits on the RECOVERY path: the fallback POST and write_step_summary both
# come after it, so a call that hangs takes the round out of BOTH channels — the job's
# `timeout-minutes: 10` kills the process before either runs, where the pre-BE-12528
# code posted the fallback immediately. Bounded well under that budget so a slow or
# wedged read degrades to the UNKNOWN branch (fallback posted, nothing tagged) instead
# of costing the round entirely. Generous enough that ordinary pagination over a busy
# PR finishes inside it.
GH_LIST_REVIEWS_TIMEOUT_SECONDS = 60


def gh_list_reviews(repo: str, pr_number: str) -> subprocess.CompletedProcess:
    """Every review on the PR, oldest first, ALL pages.

    Paginated deliberately: the review this asks about is the newest one, so on a PR
    with more than a page of reviews it sits on the LAST page. The workflow's own
    dup-check (`cursor-review.yml`, the `already_reviewed` step) is the same
    discriminator without pagination — it can afford that, because it only has to
    notice a review that already exists before spending the panel, while a wrong
    answer here decides whether findings are labelled lost. Pagination means this is
    one *command* but not necessarily one HTTP request.

    `--slurp` wraps each page in an outer array (gh >= 2.43; the runner uses a current
    gh), so the caller flattens one level.

    A timeout is reported as a nonzero CompletedProcess rather than raised, so the
    caller reads it through the same "could not tell" branch as any other failed read.
    """
    argv = [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"/repos/{repo}/pulls/{pr_number}/reviews",
    ]
    try:
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=GH_LIST_REVIEWS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=argv,
            returncode=124,
            stdout="",
            stderr=(
                f"gh api timed out after {GH_LIST_REVIEWS_TIMEOUT_SECONDS}s listing "
                f"reviews for {repo}#{pr_number}"
            ),
        )


# The states GitHub uses for a review that has actually been SUBMITTED. Matched as an
# allowlist rather than by excluding DISMISSED, because `GET /pulls/{n}/reviews`
# also returns the authenticated identity's own PENDING (unsubmitted) reviews — and
# that identity is the very bot whose POST just errored, so a half-committed write is
# exactly what could appear here as PENDING. A pending review is invisible to everyone
# else and publishes no resolvable thread, so reading one as "landed" would suppress
# the fallback and report `delivered=true` over findings no thread query can find.
SUBMITTED_REVIEW_STATES = frozenset({"COMMENTED", "APPROVED", "CHANGES_REQUESTED"})


def _normalize_review_body(text) -> str:
    """`text` with the differences GitHub is known to introduce when it stores a body.

    Line endings (it rewrites CRLF) and trailing whitespace only. Deliberately NOT a
    loose normalization: the comparison this feeds is the identity check, so anything
    that makes two DIFFERENT review bodies compare equal defeats it.
    """
    lines = (text or "").replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def review_already_posted(
    repo: str, pr_number: str, commit_sha: str, posted_body: str
):
    """Did the review this run tried to post actually land on the PR?

    Three-valued on purpose, because two of the three drive different behaviour and
    the third must never be mistaken for either: ``True`` a review from this run is on
    the PR, ``False`` confirmed absent, ``None`` could not tell (the list read failed
    or came back unparseable). A read that failed is not a zero — see BE-4785 — so the
    caller degrades rather than claiming the review is missing.

    The gate's and the workflow's three-part filter first — a SUBMITTED state, a Bot
    author (the poster is caller-configurable, so the TYPE is the only thing this can
    know), and the run's own head SHA — and then, unlike them, an identity check on
    the BODY: it must be `posted_body`, up to the normalization GitHub applies when it
    stores one.

    That last part is what this cannot borrow from the gate. `CONSOLIDATED_MARKER` is
    a fine discriminator for the question THEY ask ("does a panel review already exist
    at this SHA, so should we spend the panel at all?"), but it is the wrong one here.
    A previous round's body-only fallback, and any `post_error_review` body, both open
    with the marker, are Bot-authored and carry this same `commit_id` — so accepting a
    prefix match would answer "yes, your review landed" on the strength of some OTHER
    review entirely. That answer is not a harmless duplicate: the caller returns
    without posting the fallback and reports `delivered=true` with
    `gated_findings=len(comments)`, so THIS round's findings reach neither the PR nor
    the job summary while the blocking gate goes green over threads that belong to a
    different round. The pre-change behaviour in that same scenario was a duplicate
    review — noisy, but with the findings still visible — so a loose match here would
    trade a duplicate for a silent loss. It is reachable, too: the workflow's
    `already_reviewed` dup-check fails OPEN on an API error, and two runs can both pass
    it before either posts.

    Requiring the body means the residual is now the strictly narrower "a previous
    round posted a byte-identical body at the same head SHA", which is the same
    findings, in the same order, with the same anchors — a review whose threads do
    carry this round's findings.
    """
    result = gh_list_reviews(repo, pr_number)
    if result.returncode != 0:
        # The UNKNOWN branch withholds `lost_to_fallback` and reposts the fallback
        # without saying why, so the reason has to be logged HERE or it exists nowhere:
        # a 60s timeout, an auth failure and an older `gh` without `--slurp` are
        # indistinguishable to an operator otherwise.
        print(
            f"Review: could not list reviews for {repo}#{pr_number} "
            f"(exit {result.returncode}): {(result.stderr or '').strip()[:300]}",
            file=sys.stderr,
        )
        return None
    # An exit-0 read with nothing in it INSPECTED nothing; defaulting it to `[]` would
    # launder that into "this PR has no reviews" and tag every finding lost on the
    # strength of it. Same rule as the unparseable and wrong-shape cases below
    # (BE-4785): only a list this actually read can answer False.
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    try:
        pages = json.loads(raw)
    except ValueError:
        return None
    # `--slurp` promises a NON-EMPTY list OF PAGES, each itself a list. Anything else —
    # a flat array of reviews, a single object, a bare scalar — is a payload shape this
    # does not know how to read, so it is UNKNOWN rather than empty. Silently dropping
    # the pages that fail the check would turn an unrecognized shape into "no reviews".
    #
    # `[]` is in that set, and deliberately: `all()` is vacuously true over it, so it
    # would otherwise fall through to `reviews = []` and answer "confirmed absent" —
    # the same laundering the empty-stdout guard above rejects, on a read that
    # inspected no page at all. `[[]]` is what --slurp really returns for a PR with no
    # reviews, and that is the genuine absence.
    if (
        not isinstance(pages, list)
        or not pages
        or not all(isinstance(page, list) for page in pages)
    ):
        return None
    reviews = [r for page in pages for r in page]
    for review in reviews:
        if not isinstance(review, dict):
            continue
        if review.get("state") not in SUBMITTED_REVIEW_STATES:
            continue
        # Types are trusted no further than shapes were: this runs on a payload the
        # process cannot re-fetch, and an AttributeError here escapes `main()` and
        # kills it ahead of BOTH the fallback POST and write_step_summary — the same
        # both-channel loss the timeout above exists to prevent.
        user = review.get("user")
        if not isinstance(user, dict) or user.get("type") != "Bot":
            continue
        if review.get("commit_id") != commit_sha:
            continue
        # Cheap prefix reject before the equality; every body this script posts opens
        # with the marker, so it can only skip reviews the identity check would reject
        # anyway. Applied to the NORMALIZED body, not the raw one, or it would be
        # STRICTER than the check it guards: `_normalize_review_body` strips leading
        # whitespace, so a stored body differing only by a leading newline would pass
        # the equality yet never reach it — answering "absent" for the run's own landed
        # review, which is precisely this path's worst outcome.
        body = review.get("body")
        if not isinstance(body, str):
            continue
        normalized = _normalize_review_body(body)
        if not normalized.startswith(CONSOLIDATED_MARKER):
            continue
        if normalized == _normalize_review_body(posted_body):
            return True
    return False


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


def post_or_degrade(
    repo,
    pr_number,
    payload,
    summary_markdown,
    context,
    truncated=False,
    delivers=True,
    gated=0,
    ungated=0,
) -> bool:
    """POST a review; degrade to the step summary on a read-only token.

    Returns True when the review was delivered — either posted on the PR, or
    (when the token is read-only) written to the job step summary. Returns
    False only on a genuine POST failure the caller should handle itself
    (e.g. retry without inline anchors).

    `truncated` says the posted body was clamped, so the whole of it goes to the
    summary even on success — otherwise the clamp note points at a summary that
    was never written.

    `delivers` says whether a SUCCESSFUL post of this particular body counts as a
    review for the blocking gate. It does not for the two bodies that report a
    failure rather than a review — the "Review failed" error review and the
    all-cells-failed no-findings review — which is the whole reason the gate
    cannot read this function's `True` as "a review happened". Note that the
    read-only branch returns True as well and is never a delivery: the review
    reached a job summary, not the PR, so no thread exists to hold the merge on.
    """

    def report_posted():
        """This body is on the PR. Shared by the two ways of finding that out."""
        # `posted` regardless of `delivers`: a body that reports a failure still
        # reached the PR, and the DM's claim is about the PR, not about adjudication.
        # A clamped-but-posted review is posted too — hence before the `truncated`
        # handling, not after it.
        emit_delivery(delivers, gated, ungated, posted=True)
        if truncated:
            print(
                f"{context}: body hit GitHub's size limit — full text written to "
                "the job summary.",
                file=sys.stderr,
            )
            write_step_summary(summary_markdown, note=TRUNCATED_SUMMARY_NOTE)

    result = gh_post_review(repo, pr_number, payload)
    if result.returncode == 0:
        report_posted()
        return True
    if is_read_only_token_error(result):
        print(
            f"{context}: token is read-only — writing the review to the job "
            "summary instead of the PR.",
            file=sys.stderr,
        )
        emit_delivery(False)
        write_step_summary(summary_markdown)
        return True
    print(f"{context} POST failed: {result.stderr}", file=sys.stderr)
    # A nonzero `gh` is not proof the write was refused. Once the throttle wordings
    # stopped being read as a read-only token (BE-12612), the 403 GitHub raises on a
    # request it went on to SERVE reaches here — and every caller answers a False by
    # writing the same text to the job summary and exiting 1. That publishes a second
    # copy of a review already on the PR and reports `posted=false` for it, which the
    # fresh-review gate then holds the check red over. So ask the PR, on exactly the
    # statuses the inline path asks on. This is a READ, never a repost: on ABSENT and
    # on UNKNOWN this returns False and the caller behaves as it always has.
    if post_may_have_landed(result):
        # Read the commit and the body back out of the REQUEST rather than taking them
        # as parameters: `review_already_posted` answers True only for a byte-identical
        # body at the same head SHA, so the two have to be the ones this call actually
        # sent. `payload` is that request, and every caller builds it with json.dumps.
        request = json.loads(payload)
        landed = review_already_posted(
            repo, pr_number, request.get("commit_id") or "", request.get("body") or ""
        )
        if landed is True:
            print(
                f"{context}: the POST errored but this exact review is on the PR — "
                "treating it as delivered rather than reporting it lost.",
                file=sys.stderr,
            )
            report_posted()
            return True
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


def drop_unterminated_comment(cut: str) -> str:
    """Remove a trailing `<!--` the size clamp left with no `-->` to close it.

    A CommonMark HTML block opened by `<!--` ends only at `-->`, so a cut landing inside
    the body-only sentinel's JSON does not merely lose the sentinel — GitHub swallows
    everything after the dangling opener as part of the unterminated comment, including
    clamp_review_body's own "as much of it as fits is in the job summary" note. The
    review then renders as a header with no visible findings and no explanation of why.

    Fixed HERE rather than by giving the sentinel a byte budget at render time, because
    a budget cannot actually promise this: whether the sentinel survives depends on how
    much body precedes it, which render_body_only_findings does not know. The clamp is
    the one place that knows where the cut lands, and closing it here covers every HTML
    comment in every posted body rather than the one we happen to be thinking about.

    Dropping the fragment is safe on its own terms: the section's prose marker sits
    ABOVE the sentinel, so a cut deep enough to reach it still leaves build-ledger.py
    the evidence that findings WERE demoted, and that round degrades loudly instead of
    reading as a round that found nothing.
    """
    opener = cut.rfind("<!--")
    if opener == -1 or "-->" in cut[opener:]:
        return cut
    return cut[:opener].rstrip()


def clamp_review_body(body: str, limit: int = MAX_REVIEW_BODY_CHARS) -> str:
    """Trim a review body to GitHub's size limit, saying where it was cut.

    Also the choke point every POSTed body passes through, so the surrogate scrub runs
    here too — see the note in normalize_comments. Applied before the length test so a
    replacement never lands after the cut was measured.
    """
    body = encodable(body)
    if len(body) <= limit:
        return body
    note = CLAMP_TRUNCATION_NOTE
    if limit <= len(note):
        # Degenerate limit (tests, a future tightening): the cut still has to hold.
        return body[:limit]
    return drop_unterminated_comment(body[: limit - len(note)].rstrip()) + note


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


def defang_body_only_contract(text: str) -> str:
    """Break both halves of the body-only sentinel contract inside imported text.

    build-ledger.py accepts a sentinel only when the prose marker sits on the line
    above it, and both are matched as exact literals. Forging the sentinel alone would
    fabricate ledger entries with attacker-chosen path/severity/body; forging the
    marker alone would fabricate a "findings were lost" truncation note in the next
    round's prompt. Neither is much of a prize, but neither is text we wrote.

    A zero-width space is invisible where the text is rendered for a human and defeats
    both literals, so what was quoted still reads exactly as it arrived.

    This only works because the READER matches both halves as exact literals too. While
    its sentinel pattern still tolerated whitespace runs, `…body-only-findings\\tv1` was
    a spelling that satisfied the reader and carried none of the literal replaced here —
    undefanged, and accepted. The reader is pinned to the single-spaced opener for that
    reason, and test_build_ledger.py pins the two spellings together.

    Belt and braces, not the only control: build-ledger.py ALSO refuses the error-review
    shape outright, which is what covers error reviews already sitting on consumer PRs
    from before this existed — bodies no writer-side change can reach.
    """
    return text.replace(
        BODY_ONLY_SENTINEL_PREFIX,
        BODY_ONLY_SENTINEL_PREFIX.replace(":", ":\u200b", 1),
    ).replace(
        BODY_ONLY_PROSE_MARKER,
        BODY_ONLY_PROSE_MARKER.replace(" ", "\u200b ", 1),
    )


def strip_repeat_line(repeat_line: str, body: str) -> str:
    """Drop the `↩︎ re-raise of <url> (round N)` trailer normalize_comments appended.

    build-ledger.py deliberately OMITS `discussion_url:` for an unanchorable entry so
    the judge can never emit it as a `repeat_of` — a demoted finding has no thread for
    one to point at. Carrying the trailer inside the sentinel's `body` puts a live
    thread URL straight back into the prose the judge reads, and on a likely path: a
    re-raise often cites a line the NEW diff no longer carries, which is exactly what
    gets demoted.

    Reconstructed from the string normalize_comments actually appended rather than
    re-matched with a regex, for the same reason strip_severity_badge is: if the format
    changes this stops matching and the sentinel carries a visible trailer — cosmetic —
    instead of silently eating the tail of a finding.

    Since BE-12534 stripping the trailer no longer LOSES the lineage: the URL travels
    structurally instead, as the sentinel's optional `repeat_of` key
    (render_body_only_sentinel), which build-ledger.py resolves back to the ancestor
    thread — recovering its round from that thread rather than from the payload. Before that they were carried in no field at all, so a re-raise
    demoted to the body was rebuilt next round as a fresh unanchorable finding and
    every later hop of that chain became cap-free. The prose half is unchanged — the
    trailer still shows the re-raise to whoever reads the review.
    """
    if repeat_line and body.endswith(repeat_line):
        return body[: -len(repeat_line)].rstrip()
    return body


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

    `lost_to_fallback` (BE-10002) is emitted ONLY for an item that carries it, so a
    success-path payload stays byte-identical to what this rendered before the key
    existed. It marks a finding that anchored fine and lost its thread to the failed
    POST rather than to the diff — presentation only on the reading side, since the
    mechanical consequences of `anchored: false` are correct for it either way. The
    key name carries no `-`, so the escape above already covers it.

    `repeat_of` (BE-12534) follows the same optional-key discipline and carries the
    re-raise lineage strip_repeat_line just removed from `body`. Unlike
    `lost_to_fallback` it is NOT presentation-only: the reader resolves the URL to the
    ancestor thread and reads that thread's real answer state, which is what makes a
    demoted re-raise of an ANSWERED finding cost a repeat slot next round instead of
    being cap-exempt. The URL is the ONLY lineage key emitted — the ancestor's round is
    not carried, because the reader derives it from the resolved comment's own review
    rather than from the payload, and a field nobody reads would still be paid for out
    of the two size guards' budget.
    """
    payload = []
    for item in items:
        entry = {
            # neutralize_mentions, like render_code_ref does for the prose half. The
            # body was already neutralized in normalize_comments, but `path` is raw
            # model output until it is rendered — and this render is still a POSTed
            # review body, so an `@handle` in it would fire a live mention from the bot
            # account. An HTML comment is not a reliable place to hide one.
            "path": MD_LINE_BREAK_RE.sub(" ", neutralize_mentions(item["comment"]["path"])),
            "line": item["comment"]["line"],
            "severity": item["severity"],
            "body": truncate_sentinel_body(
                strip_repeat_line(
                    item.get("repeat_of") or "",
                    strip_severity_badge(item["severity"], item["comment"]["body"]),
                )
            ),
        }
        # `is True`, matching build-ledger.py's reader exactly. It reads the key that
        # way so a stray `"lost_to_fallback": "no"` in a relayed payload cannot count
        # as set; emitting on mere truthiness here would normalize such a value to
        # JSON `true` and defeat that guard from the writing side.
        if item.get("lost_to_fallback") is True:
            entry["lost_to_fallback"] = True
        # BE-12534: the re-raise lineage, carried STRUCTURALLY because
        # strip_repeat_line just took it out of `body`. Same optional-key discipline as
        # `lost_to_fallback` — emitted only for an item that has it, so a payload with
        # no re-raise in it stays byte-identical to what this rendered before the key
        # existed (which is also what keeps the two size guards' measurements honest).
        #
        # The URL alone. The ancestor's ROUND is deliberately not carried: the reader
        # takes it from the review the resolved comment belongs to, which is truthful
        # by construction and available whenever the URL resolves at all, so a
        # `repeat_round` field here would be payload nothing reads — and this body is
        # under a hard size cap that can drop a real finding to make room for it.
        #
        # Validated HERE rather than trusted to the reader, even though the reader
        # re-validates: this string is judge output landing in a review body on a
        # public PR, so what gets WRITTEN is bounded too. The URL is one anchored
        # GitHub discussion permalink and nothing else — no other host, no whitespace
        # or line break (which would ride through JSON losslessly and land on the
        # ledger's own `re_raise_of:` line), and no unbounded string. The `-` → `-`
        # escape below already covers it: it is applied post-encode to the whole
        # payload, so the URL's own dashes cannot close the HTML comment early.
        repeat_url = item.get("repeat_url") or ""
        if (
            isinstance(repeat_url, str)
            and len(repeat_url) <= REPEAT_URL_MAX_CHARS
            # fullmatch, not match: `$` also matches just BEFORE a trailing
            # newline, and JSON round-trips that losslessly — it would arrive on
            # the ledger's own `re_raise_of:` line as a real line break, at column
            # 0 of the prompt. repeat_url_of already stripped, so this is the
            # second of the two halves rather than the only one.
            and REPEAT_URL_RE.fullmatch(repeat_url)
        ):
            entry["repeat_of"] = repeat_url
        payload.append(entry)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    escaped = encoded.replace("-", "\\u002d")
    return f"<!-- {BODY_ONLY_SENTINEL_PREFIX} {escaped} -->"


def fit_sentinel_items(items: list, budget: int) -> list:
    """The longest leading run of `items` whose rendered sentinel fits `budget` chars.

    A PREFIX rather than a subset, because `items` arrives most → least urgent: the
    findings kept are the ones next round most needs back, and they stay in the same
    order as the prose below them. `[]` means "nothing fits" — the caller drops the
    sentinel and posts the prose marker alone, which is what this path did before the
    sentinel existed and which the ledger still reads as a disclosed truncation.

    Recovering SOME findings is strictly better than the all-or-nothing rule it
    replaces: that rule made the sentinel free to eat the whole body budget as long as
    it fit at all, so the round with the most findings to report was the one that
    rendered none of them. A partial sentinel is not a partial truth on the reading
    side either — the findings it leaves out are simply absent from the ledger, exactly
    as all of them were when the whole sentinel was dropped.

    Binary search: the render grows monotonically with the prefix length, so the
    boundary is well defined and found in ~log2(n) renders rather than n.
    """
    if budget <= 0 or not items:
        return []
    if len(render_body_only_sentinel(items)) <= budget:
        return items
    # Invariant: `lo` fits (or is 0, the drop case the caller handles), `hi` does not.
    lo, hi = 0, len(items)
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if len(render_body_only_sentinel(items[:mid])) <= budget:
            lo = mid
        else:
            hi = mid
    return items[:lo]


def render_body_only_findings(items: list) -> str:
    """Render findings that could not be anchored, for inclusion in the review body."""
    if not items:
        return ""
    # Order is load-bearing, and it is marker → sentinel → prose.
    #
    # clamp_review_body cuts the TAIL, so the machine-readable copy sits as near the
    # head of the section as it can and stays recoverable for as long as any of the
    # section survives. But it cannot be first: a clamp landing INSIDE the JSON takes
    # the closing `-->` with it, and with the marker below that it took the evidence
    # too — build-ledger.py saw neither a parseable sentinel nor the marker, and a
    # fully-demoted round read as a review that found nothing. That is the one cut that
    # actually happens, and it was the silent one.
    #
    # One short line above the sentinel costs ~140 chars of recoverability and makes
    # every such cut LOUD. It is also the sentinel's required predecessor on the read
    # side, which is what scopes build-ledger.py's search to this section.
    md = (
        f"_The finding(s) below {BODY_ONLY_PROSE_MARKER}, so they are reported here "
        "instead of inline:_\n\n"
        f"{render_body_only_sentinel(items)}\n\n"
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
        md += FINDINGS_SEPARATOR
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
                # The same lineage, unrendered (BE-12534). `repeat_of` above is the
                # TRAILER — the prose the reader sees — and strip_repeat_line takes it
                # back out of a demoted finding's sentinel body, because a live thread
                # URL must never travel inside the prose the judge reads. This field is
                # how it travels instead: structurally, as a sentinel key
                # render_body_only_sentinel validates on the way out and
                # build-ledger.py resolves against the PR's real comments on the way
                # in. Kept beside `repeat_of` rather than replacing it so
                # enforce_repeat_cap's count and strip_repeat_line's reconstruction are
                # both untouched.
                "repeat_url": repeat_url_of(finding),
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
    url = repeat_url_of(finding)
    if not url:
        return ""
    return f"\n\n↩︎ re-raise of {url}{render_repeat_round(finding)}"


def repeat_url_of(finding: dict) -> str:
    """The judge's `repeat_of` URL, neutralized and stripped — or `""`.

    Split out of render_repeat_of (BE-12534) so the RENDERED trailer and the RAW url
    are two separate things. normalize_comments keeps storing the trailer in
    `item["repeat_of"]` — that is what enforce_repeat_cap counts and what
    strip_repeat_line reconstructs — and stores this alongside it as
    `item["repeat_url"]`, which is what render_body_only_sentinel emits as a field.
    The ROUND has no such twin: it stays in the trailer only, because the ledger reads
    a resolved ancestor's round off that ancestor's own review rather than off the
    payload, so carrying it structurally would cost sentinel bytes nothing reads.
    """
    url = finding.get("repeat_of")
    if not isinstance(url, str) or not url.strip():
        return ""
    return neutralize_mentions(url.strip())


def coerce_repeat_round(finding: dict):
    """The judge's `repeat_round` as a POSITIVE int, or None.

    The type IS the control (see render_repeat_round): a positive integer cannot carry
    an `@handle` or markup at all. `bool` is rejected explicitly because it is a
    subclass of `int`, so `repeat_round: true` would otherwise render "(round True)"
    and be emitted into the sentinel as JSON `true`.
    """
    round_no = finding.get("repeat_round")
    if isinstance(round_no, bool):
        return None
    if isinstance(round_no, str):
        round_no = round_no.strip()
        if not round_no.isdigit():
            return None
        try:
            round_no = int(round_no)
        except ValueError:
            return None
    if not isinstance(round_no, int) or round_no <= 0:
        return None
    return round_no


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

    The coercion itself lives in coerce_repeat_round (BE-12534): it is the guard that
    keeps a non-decimal digit (`str.isdigit()` is true for characters `int()` rejects)
    from raising straight out of normalize_comments and taking down the whole review
    post.
    """
    round_no = coerce_repeat_round(finding)
    if round_no is None:
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
    # `safe` lands in a FENCE, not a blockquote, so unlike every rendered finding its
    # lines sit at column 0 — this is the only consolidated review body where imported
    # text can satisfy build-ledger.py's line-anchored sentinel match. Same idea as the
    # backtick run above: break it at the WRITER, before the length budget, and keep
    # what was quoted readable.
    safe = defang_body_only_contract(safe)
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
        # This body REPORTS a failure; it adjudicates nothing and anchors no
        # thread. Posting it must never green the blocking gate.
        delivers=False,
    ):
        # Same contract as the review paths: a genuine POST failure still delivers the
        # text somewhere. post_or_degrade writes the summary itself on the paths that
        # return True, so this cannot double-write.
        emit_delivery(False)
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
            args.repo,
            args.pr_number,
            payload,
            body_text,
            "No-findings review",
            # `all_failed` is "every reviewer errored", not "the reviewers found
            # nothing" — zero threads there means nothing was reviewed, so it is
            # exactly the round the gate must refuse to pass.
            delivers=not all_failed,
        ):
            # Same contract as the other three exit paths: a genuine POST failure still
            # delivers the text somewhere. On the all-failed branch that text is the one
            # artifact explaining why no review happened — the panel summary naming
            # which cells errored — so losing it is exactly when it is needed most.
            emit_delivery(False)
            write_step_summary(body_text, note=POST_FAILED_SUMMARY_NOTE)
            raise SystemExit(1)
        return

    enriched = normalize_comments(findings)
    enriched, repeats_dropped = enforce_repeat_cap(enriched)
    # Anchor-aware split. The COUNT below stays the total across both halves — a finding
    # that lands in the body is still a finding, and a headline that shrank because an
    # anchor missed would misreport the review.
    # `anchors` is kept, not just consumed: None means the diff could not be read and
    # partition_by_anchor failed OPEN without testing a single finding, which the
    # wholesale fallback below has to know before it can claim anything anchored.
    anchors = load_anchors(args.diff)
    inline_items, body_only_items = partition_by_anchor(enriched, anchors)
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
        # already gets. Since BE-10002 the WHOLESALE fallback body (the 422 path below)
        # carries a sentinel of its own too, so a round whose findings were lost to a
        # failed POST reaches the ledger as well.
        #
        # Cap-exempt is about the entry ITSELF, not about its lineage. Since BE-12534 a
        # demoted finding that was a RE-RAISE carries the ancestor thread's URL
        # structurally, in the sentinel's `repeat_of` key — never in the prose, which
        # strip_repeat_line still clears. build-ledger.py resolves that URL against the
        # PR's real comments and reads the ANCESTOR's answer state (and round), so
        # re-raising an answered finding keeps costing a repeat slot even after one hop
        # of the chain was demoted. Without the field the chain went cap-free from that
        # hop on.
        review_body += f"{FINDINGS_SEPARATOR}{body_only_md}"

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

    def finish_posted_review():
        """Report the inline review as delivered, and write the clamp's job summary.

        Shared by the two paths on which THIS body is on the PR: the `gh` POST
        returned 0, and the POST errored but the review turned out to have landed
        anyway (below). Those two outcomes are the same fact about the PR, so they
        report it through one implementation rather than two that can drift.
        """
        # The split the gate needs: `comments` are the findings that got a thread a
        # human can resolve; `body_only_items` reached the body and can never be
        # resolved. A round where the second is non-empty and the first is empty is
        # a review whose every finding is invisible to a thread query.
        emit_delivery(
            True, gated=len(comments), ungated=len(body_only_items), posted=True
        )
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

    result = gh_post_review(args.repo, args.pr_number, payload)

    if result.returncode == 0:
        finish_posted_review()
        return

    # A read-only token rejects any write, so the inline-less fallback below
    # would fail the same way — degrade straight to the job summary instead.
    if is_read_only_token_error(result):
        print(
            "Review: token is read-only — writing the review to the job "
            "summary instead of the PR.",
            file=sys.stderr,
        )
        emit_delivery(False)
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
        # "No fallback to post" is not "no question to ask", though. A throttled 403
        # (or a 5xx, or a dropped connection) can be raised on a request GitHub went
        # on to SERVE, and reporting THAT as `posted=false` leaves the review on the
        # PR while the fresh-review gate holds the check red for a review that landed
        # and the job summary publishes a second copy of it. Same read as the inline
        # path below, on the same statuses, and still no repost: only a PRESENT answer
        # changes anything here.
        if post_may_have_landed(result) and review_already_posted(
            args.repo, args.pr_number, args.commit_sha, posted_body
        ) is True:
            print(
                f"Review: the POST errored ({(result.stderr or '').strip()[:200]}) but "
                f"a review for {args.commit_sha[:7]} is on the PR — treating as "
                "delivered.",
                file=sys.stderr,
            )
            finish_posted_review()
            return
        print(
            "Review: no inline comments to drop — the fallback would repost the same "
            "body, so writing it to the job summary instead.",
            file=sys.stderr,
        )
        emit_delivery(False)
        write_step_summary(prose_body, note=POST_FAILED_SUMMARY_NOTE)
        raise SystemExit(1)

    # Did that POST really fail to land? A nonzero `gh` is not proof it did not —
    # the `not comments` branch above asks the same question for the same reason, and
    # declines to repost whatever the answer — and here the answer decides two things
    # below: whether to post the fallback at all, and whether the findings that
    # anchored may be labelled lost.
    #
    # Cheapest sufficient evidence first, which is what `post_may_have_landed` weighs:
    # a 4xx is GitHub VALIDATING and rejecting the request before writing anything
    # (every firing observed in the field is a 422 over an inline position), so the
    # review is absent by construction and no read is worth the call — with the
    # exception carved out by RETRYABLE_4XX_STATUSES, whose members are 4xx without
    # carrying that meaning: an edge or a proxy said so, or GitHub throttled a request
    # it may well have gone on to serve. Anything else — a 5xx, or a transport error
    # that carries no status at all — leaves the write genuinely undecided, so ask the
    # PR. Three outcomes follow:
    # PRESENT (the review landed: report it delivered, post nothing more), ABSENT
    # (behave exactly as this path always has), and UNKNOWN (post the fallback, but tag
    # nothing `lost_to_fallback` — the flag is a claim, and an unreadable list supports
    # none). UNKNOWN is why the read failing is not answered as a `False`: that would
    # be indistinguishable from a confirmed-absent review and would relabel findings on
    # the strength of a transient blip.
    if post_may_have_landed(result):
        landed = review_already_posted(
            args.repo, args.pr_number, args.commit_sha, posted_body
        )
    else:
        landed = False

    if landed is True:
        print(
            f"Review: the POST errored ({(result.stderr or '').strip()[:200]}) but a "
            f"review for {args.commit_sha[:7]} is on the PR — not reposting; treating "
            "as delivered.",
            file=sys.stderr,
        )
        finish_posted_review()
        return
    if landed is None:
        print(
            "Review: could not confirm whether the first POST landed (review list "
            "unreadable) — posting the fallback with no finding tagged [post-failed].",
            file=sys.stderr,
        )

    # Fallback: same findings without inline anchors. Typical cause is line
    # numbers that fall outside the diff context — often the model picked
    # a line near the change but not on the change.
    # The note carries BODY_ONLY_PROSE_MARKER deliberately. Without it, next round's
    # build_ledger saw neither entries NOR a degradation for a round on which EVERY
    # finding is body-only, so a fallback-posted round read as a review that found
    # nothing and the round after it looked like a first round. The marker alone costs a
    # sentence and keeps the disclosure honest.
    #
    # It goes in the HEAD, for exactly the reason the section marker had to move above
    # the sentinel: clamp_review_body cuts the TAIL. Appended after the findings the
    # note was the FIRST thing any clamp took, so a fallback over MAX_REVIEW_BODY_CHARS
    # posted without it and the silent round came straight back — on the one path where
    # EVERY finding is body-only, and a reachable one: this body carries every finding
    # at its full length, with no count cap on the un-adjudicated panel path.
    # review_head is finding-independent and bounded (a header, a count, a severity
    # table, a panel summary), so the note sits within a few hundred chars of the top
    # and outlives every cut that leaves a body at all.
    fallback_head = review_head + (
        f"\n\n_(Inline comments {BODY_ONLY_PROSE_MARKER}, so every finding is listed "
        "below instead. None of them has a review thread, so there is nowhere to "
        "reply to one or resolve it.)_"
    )
    # …and the sentinel goes DIRECTLY under that note (BE-10002), in the same
    # marker → sentinel → prose order render_body_only_findings uses and for the same
    # two reasons: build-ledger.py accepts a sentinel only when the marker line sits
    # immediately above it, and a tail clamp then eats the least-urgent PROSE rather
    # than the JSON. Until this, the fallback posted the marker alone, so the ledger
    # disclosed the degradation loudly and recovered ZERO entries — including for the
    # findings that anchored perfectly well and lost their thread only to the failed
    # POST. Every finding of the round is OFFERED to it — the ones from `inline_items`
    # tagged `lost_to_fallback` only when the first review is confirmed ABSENT, the
    # ones already unanchorable left untagged, since the POST outcome changed nothing
    # for them — and the size guard below decides how many of them the body can
    # actually afford to carry.
    #
    # That confirmation is the check above, and it has three outcomes: PRESENT returns
    # before reaching here (nothing is reposted and nothing is relabelled), ABSENT is
    # this path with the tag applied, and UNKNOWN is this path with the tag withheld —
    # the fallback still carries every finding, each reading as [unanchorable], which
    # is what an unread review list can honestly support.
    #
    # Tagged by identity, not by value: `inline_items` and `body_only_items` hold the
    # very objects `enriched` does, and two findings can be equal without being the
    # same one. Iterating `enriched` is what keeps the sentinel in the same most →
    # least urgent order as the prose below it.
    #
    # And tagged only where the anchors were actually CHECKED. With `anchors is None`
    # partition_by_anchor put every finding inline without testing one, so
    # `inline_items` is not evidence of anything — least of all on a 422, whose typical
    # cause IS an anchor GitHub would not take. Untagged, those findings render as
    # [unanchorable]: the conservative reading, and the one this path gave them before
    # BE-10002. The claim the flag makes is "this passed the diff-anchor check", and
    # that is a claim only a real check can make.
    #
    # Both conditions are required, and for the same reason: `lost_to_fallback` says
    # "this finding anchored, and the failed POST is what cost it its thread". The
    # anchor half needs a real diff check (`anchors is not None`); the lost half needs
    # the first review to be confirmed ABSENT (`landed is False`), since a review that
    # landed — or one nobody could look for — leaves that second claim unsupported.
    lost_ids = (
        {id(item) for item in inline_items}
        if (anchors is not None and landed is False)
        else set()
    )
    sentinel_items = [
        {**item, "lost_to_fallback": True} if id(item) in lost_ids else item
        for item in enriched
    ]
    # Size guard, in two parts.
    #
    # A PROSE FLOOR first. The sentinel duplicates the findings in JSON at nearly the
    # length of the prose entries below it, so left to take whatever fits it displaces
    # the review a human reads: measured at 89 findings it posted 58,720 characters of
    # comment and rendered no findings at all, where the same round one finding larger
    # dropped the sentinel and rendered 79. The sentinel gets at most half the body;
    # fit_sentinel_items then keeps the most-urgent prefix that fits, so a round too big
    # for a whole sentinel recovers part of one instead of none of it.
    #
    # The clamp's own note is RESERVED in that budget, not merely the limit tested. The
    # clamp cuts at `limit - len(note)`, so a head+sentinel that fits the limit by less
    # than that can still be cut mid-JSON — and drop_unterminated_comment then removes
    # the sentinel back to its opener, taking every finding after it with it. That was a
    # ~120-char window (measured: 89 findings, one long path) in which the review
    # collapsed from 60,000 characters of findings to a 494-character header. The
    # sentinel is posted whole or not at all; it is never posted where the clamp cuts.
    sentinel_budget = min(
        FALLBACK_SENTINEL_MAX_CHARS,
        MAX_REVIEW_BODY_CHARS
        - len(CLAMP_TRUNCATION_NOTE)
        - len(fallback_head)
        - len("\n\n")
        - len(FINDINGS_SEPARATOR),
    )
    kept = fit_sentinel_items(sentinel_items, sentinel_budget)
    if kept:
        fallback_head_with_sentinel = (
            f"{fallback_head}\n\n{render_body_only_sentinel(kept)}"
        )
        if len(kept) < len(sentinel_items):
            print(
                f"Review: the fallback's body-only sentinel carries the "
                f"{len(kept)} most urgent of {len(sentinel_items)} finding(s) — the "
                "rest would have displaced the findings a reader can see.",
                file=sys.stderr,
            )
    else:
        # Nothing fits: post exactly what this path posted before the sentinel existed.
        # The prose marker is still in the head, so next round's ledger reads a
        # disclosed truncation rather than a round that found nothing.
        print(
            "Review: no part of the fallback's body-only sentinel fits under the size "
            "limit — posting the marker alone, so next round's ledger discloses the "
            "loss instead of recovering the findings.",
            file=sys.stderr,
        )
        fallback_head_with_sentinel = fallback_head
    fallback_body = render_findings_markdown(
        fallback_head_with_sentinel, [i["comment"] for i in enriched]
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
        # This body reached the PR, so it IS a delivery — but the inline half is
        # exactly what was dropped to make it postable, so none of its findings
        # carries a thread. Reported as ungated so the gate refuses to read the
        # empty thread query as "nothing was found".
        gated=0,
        ungated=len(enriched),
    ):
        # Both attempts failed for a reason the read-only degradation does not cover
        # (an API outage, a throttle, a stale commit_id after a force-push, a
        # body-level rejection dropping the anchors cannot fix).
        # Without this the whole review is gone from the PR *and* the summary, which
        # contradicts the no-inline branch above — and this is the branch carrying
        # MORE content, since it has an inline half. post_or_degrade only writes a
        # summary on the paths that return True, so there is no double write here.
        emit_delivery(False)
        write_step_summary(fallback_body, note=POST_FAILED_SUMMARY_NOTE)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
