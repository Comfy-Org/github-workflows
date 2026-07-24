#!/usr/bin/env python3
"""Durable rejection-memory (dedup ledger) for the stateless groom CI run.

Studio groom keeps its dedup + rejection ledger on the local filesystem
(`.groom-state/`). A **stateless CI run** starts fresh every time, so without a
durable memory it would re-file findings that were already filed OR already
human-rejected on every scheduled run — the fastest way to make the shared
capability annoying and get it disabled.

This module gives the CI groomer that durable memory **using GitHub issue state
itself as the store** — the GitHub-native option that needs no net-new secret
and is fully auditable (the record is the issues themselves):

- When the groomer files a finding it opens a `groom`-labeled issue whose body
  carries the verifier's stable dedup signature as an HTML-comment marker
  (`signature_marker()`), invisible to human readers but machine-recoverable.
- Before filing on the NEXT run, the groomer lists every `groom`-labeled issue
  (`state=all`), recovers each signature, and classifies it (`build_ledger()`).
  A signature that already has an issue — open, resolved, or rejected — is
  **known** and is not re-filed.
- **Human rejection is remembered natively**: an issue closed as `not_planned`
  (GitHub's "close as not planned" == wontfix) OR carrying the
  `groom-rejected` label maps to `REJECTED`, so that signature is suppressed
  forever, exactly as the roundtable required ("dedup must remember
  REJECTIONS — don't re-raise a rejected finding next week").

The pure logic (signature marker round-trip, per-issue classification, ledger
build, candidate partition) is separated from the one thin `gh` I/O shell
(`fetch_groom_issues`) so it is fully unit-testable with no network.

The signature is OWNED by the verifier ("keyed on the verifier's stable dedup
signature") — this module consumes whatever opaque string the verifier emits on
each finding's `signature` field; it never invents one. It only trims
surrounding whitespace so a stray newline in a marker can't split one signature
into two ledger keys.

CLI (what the groom workflow calls right before it files):

    python3 .github/groom/ledger.py \
        --repo owner/name --candidates findings.json --out decision.json

`findings.json` is a JSON array of findings, each with a `signature` field;
`decision.json` receives {"to_file": [...], "suppressed": [...],
"invalid": [...], "ledger_size": N}. Only `to_file` should be opened as issues;
each `to_file` finding must have `signature_marker(finding["signature"])`
appended to its issue body and the `groom` label applied, or the NEXT run will
re-file it.
"""

import argparse
import base64
import binascii
import json
import re
import subprocess
import sys

# Label every groom-filed issue carries. It is how the ledger identifies "our"
# issues (list-by-label, not a full-text search — deterministic and free of the
# search index's indexing lag, so a finding filed in run N is reliably seen in
# run N+1).
GROOM_LABEL = "groom"

# Human-rejection label. Applying it to a groom issue (open OR closed) durably
# suppresses that signature — the label path exists alongside "close as not
# planned" so a maintainer can reject without necessarily closing.
REJECTED_LABEL = "groom-rejected"

# A finding replaced by another (e.g. folded into a broader finding, or its
# location drifted and the verifier re-keyed it). Suppresses re-filing without
# implying a human said "no".
SUPERSEDED_LABEL = "groom-superseded"

# The label the auto-builder (BE-4003) stamps on every PR it opens. A PR is
# admitted to the ledger as a builder record ONLY if it carries this label — the
# `groom` label alone is not enough. Signature markers are public in issue/PR
# bodies, so without this gate anyone with label/triage access could paste a live
# finding's marker into their own `groom`-labeled PR and permanently suppress the
# finding (close it unmerged → `pr-closed`, or add `groom-rejected`). Only the
# bot applies `groom-pr`, so requiring it keeps that spoof out of the ledger.
BUILDER_PR_LABEL = "groom-pr"

# The signature marker embedded in a filed issue's body. An HTML comment so it
# renders invisibly, and a stable prefix so it round-trips through the API's raw
# body. The opaque signature is URL-safe-base64 encoded before embedding: the
# raw signature could contain `-->` (which would close the comment early and
# truncate the recovered key, re-filing the finding forever) or arbitrary
# markdown/HTML (which would be injected into the public issue body). base64url
# is a delimiter-safe, injection-proof alphabet — `[A-Za-z0-9_=-]` — that can
# never contain `-->`, so the payload group is bounded to that alphabet. The
# surrounding whitespace groups are POSSESSIVE (`\s*+`): with a plain `\s*` on
# both sides of an empty-matchable payload, a body carrying the prefix followed
# by a long whitespace run and no terminator backtracks in O(n^2) (the reported
# ReDoS). Possessive quantifiers make the whitespace non-giving, so a
# non-matching body fails in linear time. (Requires Python 3.11+; CI runs 3.12.)
_MARKER_PREFIX = "groom-signature:"
_MARKER_RE = re.compile(r"<!--\s*+groom-signature:\s*+([A-Za-z0-9_=-]*)\s*+-->")

# Ledger statuses. UNKNOWN is the only one that permits filing/proposing.
FILED = "filed"
REJECTED = "rejected"
SUPERSEDED = "superseded"
UNKNOWN = "unknown"

# PR-state statuses (BE-4003 auto-builder). A groom builder PR carries the
# finding's signature in its body exactly like a filed issue does, so the same
# ledger recognizes it. Its lifecycle maps onto durable dedup states so a built
# finding is never re-proposed: an OPEN builder PR is `pr-open`, a MERGED one is
# `merged` (shipped — done), and a CLOSED-unmerged one is `pr-closed` (a human
# declined the fix — durable, exactly like a rejected issue). Every one of these
# is non-UNKNOWN, so all of them suppress re-proposing, matching the RFC:
# "a merged/closed/rejected finding is never re-proposed."
PR_OPEN = "pr-open"
MERGED = "merged"
PR_CLOSED = "pr-closed"

# Partition-time-only status: a signature that is UNKNOWN in the live ledger but
# has ALREADY been routed to `to_file` earlier in THIS candidate batch. The
# second-and-later findings that share it are suppressed under this status so a
# single run cannot open two issues for one signature before GitHub state is
# refreshed — the exact duplicate spam the ledger exists to prevent. Never a
# live ledger status (it is not a GitHub issue state), so it is not in
# `_PRECEDENCE`.
PENDING = "pending"

# Precedence when several records (issues and/or builder PRs) share one
# signature (e.g. a finding filed as an issue AND later built as a PR): surface
# the most decision-bearing status. A human "no" is the stickiest signal, so
# REJECTED (rejected issue) and PR_CLOSED (declined PR) win; a shipped state
# (MERGED) beats a still-open one; a plain filed/superseded issue and an open PR
# rank lowest. The dedup DECISION doesn't depend on this ordering — every
# non-UNKNOWN status suppresses filing/proposing equally — only the reported
# status does.
_PRECEDENCE = {REJECTED: 6, PR_CLOSED: 5, MERGED: 4, SUPERSEDED: 3, FILED: 2, PR_OPEN: 1}


def signature_marker(signature: str) -> str:
    """The HTML-comment marker the filing step must append to an issue body.

    Round-trips with `extract_signature`. The signature is URL-safe-base64
    encoded so it can carry any opaque bytes (including `-->`, newlines, or
    markdown) without closing the comment early or injecting into the public
    issue body. The filing step owns applying this (and the `groom` label); if
    it doesn't, the next run cannot recognize the issue and will re-file the
    finding.
    """
    encoded = base64.urlsafe_b64encode(normalize_signature(signature).encode("utf-8")).decode("ascii")
    return f"<!-- {_MARKER_PREFIX} {encoded} -->"


# The builder-authored PR body must LEAD with an `## ELI-5` section (the comfy-pr
# convention embedded in builder.md). `_first_heading` pulls the first markdown
# ATX heading's text; `_ELI5_HEADING_RE` matches an ELI-5 heading title. If the
# builder's body doesn't lead with one, it is treated as unusable and the
# assembler falls back to the original template — so a PR that uses the
# builder body is GUARANTEED to open with an ELI-5 section (BE-4346).
_HEADING_RE = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*$")
_ELI5_HEADING_RE = re.compile(r"(?i)^ELI[ -]?5\b")


def _leads_with_eli5(body: str) -> bool:
    m = _HEADING_RE.search(body or "")
    return bool(m and _ELI5_HEADING_RE.match(m.group(1)))


def builder_pr_body(*, banner: str, eli5_body: str, verifier_rationale: str, signature: str) -> str:
    """Assemble the groom auto-builder PR body from its load-bearing parts (BE-4346).

    The builder agent — which made the change and knows what it did — authors
    `eli5_body`: an `## ELI-5`-first, structured what/why description written one
    line per paragraph (no hard-wrap), following the team's comfy-pr convention.
    That body LEADS the PR. The `banner` (auto-built / review-only / never
    auto-merged) is prepended and the ledger `signature_marker` is appended LAST;
    both are load-bearing — the banner sets review expectations, and
    `extract_signature` reads the LAST marker, so appending the authoritative
    marker AFTER the model-authored body keeps the ledger key un-spoofable even
    if the body embeds a marker-shaped comment. The verifier's rationale is
    retained as a secondary, collapsed `<details>` section under the ELI-5.

    Falls back to the original template (banner + `## Verifier rationale` +
    marker) when `eli5_body` is empty or does not lead with an ELI-5 heading (a
    builder bail, an empty/oversized file zeroed upstream, or a malformed body) —
    it never returns an empty-body PR.
    """
    marker = signature_marker(signature)
    body = (eli5_body or "").strip()
    rationale = (verifier_rationale or "").strip()
    if body and _leads_with_eli5(body):
        rationale_section = (
            "<details>\n<summary><strong>Verifier rationale</strong></summary>\n\n"
            f"{rationale}\n\n</details>"
        )
        return f"{banner}\n\n{body}\n\n{rationale_section}\n\n{marker}\n"
    return f"{banner}\n\n## Verifier rationale\n\n{rationale}\n\n{marker}\n"


def normalize_signature(signature) -> str:
    """Canonicalize a signature for use as a ledger key.

    Only strips surrounding whitespace — the signature is an opaque,
    case-sensitive token owned by the verifier, so we must not lowercase or
    otherwise rewrite it (that could collide two distinct findings). A missing
    or non-string signature returns "" (never the literal "None"), so a
    malformed candidate is routed to `invalid` rather than filed under a
    bogus shared key.
    """
    if not isinstance(signature, str):
        return ""
    return signature.strip()


def extract_signature(body):
    """Recover the embedded signature from an issue body, or None.

    Tolerant of the surrounding markdown/prose an issue body carries. Returns
    the signature from the LAST marker, not the first: the filing contract
    appends the authoritative marker after the finding text, so a marker-shaped
    comment planted earlier in an attacker-controlled quoted snippet cannot
    shadow the genuine one. The base64 payload is decoded back to the original
    opaque signature; a marker whose payload is not valid base64/UTF-8 is
    ignored (returns None) rather than poisoning a ledger key.
    """
    if not body:
        return None
    matches = _MARKER_RE.findall(body)
    if not matches:
        return None
    try:
        decoded = base64.urlsafe_b64decode(matches[-1].encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    signature = normalize_signature(decoded)
    return signature or None


def _labels(issue) -> set:
    """Lowercased set of an issue's label names (tolerant of shapes/None)."""
    names = set()
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.add(name.lower())
    return names


def classify_issue(issue) -> str:
    """Map one groom record (issue OR builder PR) to a ledger status.

    Never UNKNOWN — the record exists, so it is at least known. The
    `/repos/{repo}/issues` listing returns both issues and pull requests; a PR
    carries a `pull_request` object (with `merged_at`), which is how we tell the
    two apart here.

    Human rejection wins first and is durable, recognized three ways:
      * the `groom-rejected` label (open or closed, issue or PR), or
      * an issue closed as `not_planned` — GitHub's "Close as not planned", or
      * (for PRs) closed unmerged — a human declined the fix (`pr-closed`).
    A `groom-superseded` label marks a replaced finding. For a builder PR: open
    is `pr-open`, merged is `merged` (shipped). Everything else (an open issue,
    or one closed as completed/fixed) is FILED: already handled, don't re-file.
    """
    labels = _labels(issue)
    closed_not_planned = (
        issue.get("state") == "closed" and issue.get("state_reason") == "not_planned"
    )
    if REJECTED_LABEL in labels or closed_not_planned:
        return REJECTED
    if SUPERSEDED_LABEL in labels:
        return SUPERSEDED
    pr = issue.get("pull_request")
    if pr:
        if issue.get("state") == "open":
            return PR_OPEN
        # Closed: a merge stamps `merged_at`; an unmerged close is a decline.
        return MERGED if pr.get("merged_at") else PR_CLOSED
    return FILED


def build_ledger(issues) -> dict:
    """Build a {signature -> status} map from a list of groom records.

    Records without a recoverable signature marker are skipped: a `groom`-labeled
    issue or PR a human opened by hand (no marker) is not one of ours and must
    not poison a signature key. That marker check is what makes it safe to
    include pull requests here: the `/issues` endpoint returns groom-labeled PRs
    too, and a groom builder PR (BE-4003) DOES carry a signature marker, so it is
    a first-class ledger record — a merged/open/closed builder PR suppresses
    re-proposing its finding. (BE-3874 skipped all PRs because groom filed only
    issues then; the builder makes signed PRs part of the durable record.)

    A PR is admitted ONLY if it ALSO carries the `groom-pr` label the bot stamps
    on its own builder PRs. Markers are public, so the `groom` label + a pasted
    marker alone must not let a hand-opened PR masquerade as a builder record and
    suppress a live finding — requiring `groom-pr` (bot-applied) closes that.

    When several records share a signature, the higher-precedence status wins
    (`_PRECEDENCE`).
    """
    statuses: dict = {}
    for issue in issues:
        signature = extract_signature(issue.get("body"))
        if not signature:
            continue
        # Gate PRs on the bot-applied `groom-pr` label (see docstring): a signed
        # but non-builder PR is not one of ours and must not enter the ledger.
        if issue.get("pull_request") and BUILDER_PR_LABEL not in _labels(issue):
            continue
        status = classify_issue(issue)
        current = statuses.get(signature)
        if current is None or _PRECEDENCE[status] > _PRECEDENCE[current]:
            statuses[signature] = status
    return statuses


class Ledger:
    """A signature -> status view with the dedup decision baked in."""

    def __init__(self, statuses: dict):
        self._statuses = dict(statuses)

    def __len__(self) -> int:
        return len(self._statuses)

    def status(self, signature) -> str:
        """Ledger status for a signature (UNKNOWN if never filed/rejected)."""
        return self._statuses.get(normalize_signature(signature), UNKNOWN)

    def is_known(self, signature) -> bool:
        return self.status(signature) != UNKNOWN

    def should_file(self, signature) -> bool:
        """A finding is filed only if its signature is genuinely new.

        A missing/blank/non-string signature is NOT filable: it has no
        recoverable marker, so filing it would re-file on every subsequent run.
        This mirrors `partition`, which routes such a candidate to `invalid`
        rather than `to_file`.
        """
        return normalize_signature(signature) != "" and self.status(signature) == UNKNOWN

    def partition(self, findings):
        """Split candidate findings into to_file / suppressed / invalid.

        `to_file`  — signature is UNKNOWN *and* first-seen in this batch: open
                     an issue (remember to embed the marker + apply the `groom`
                     label).
        `suppressed` — signature is known (filed/rejected/superseded) OR was
                     already routed to `to_file` earlier in this same batch
                     (`pending`): each annotated with `ledger_status` for
                     auditable reporting.
        `invalid`  — no usable signature: cannot be deduped, so it is NOT filed
                     (filing it would risk the exact duplicate-spam this ledger
                     exists to prevent). The workflow should surface these as a
                     producer error rather than silently dropping or spamming.

        Intra-batch dedup: two candidates that share one new signature must not
        both be filed — the ledger is only refreshed from GitHub between runs,
        so the first opens the issue and later duplicates are suppressed as
        `pending` (not falsely labeled `filed`, which they are not yet).
        """
        to_file, suppressed, invalid = [], [], []
        filed_this_batch = set()
        for finding in findings:
            signature = normalize_signature(finding.get("signature")) if isinstance(finding, dict) else ""
            if not signature:
                invalid.append(finding)
                continue
            status = self.status(signature)
            if status != UNKNOWN:
                suppressed.append({**finding, "ledger_status": status})
            elif signature in filed_this_batch:
                suppressed.append({**finding, "ledger_status": PENDING})
            else:
                filed_this_batch.add(signature)
                to_file.append(finding)
        return to_file, suppressed, invalid


# A repo must be exactly `owner/name` — no extra path segments or URL
# metacharacters (`?`, `&`, `#`) that could override the `labels=…&state=all`
# query or redirect the endpoint and silently return the wrong issue set.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# Bound the `gh api` call so a stalled network/API connection can't block the
# groom run until the coarse Actions job timeout, wasting runner minutes.
_FETCH_TIMEOUT_SECONDS = 60


def fetch_groom_issues(repo: str, run=subprocess.run):
    """List every `groom`-labeled issue in `repo` (state=all) via `gh api`.

    Paginated so a repo with many groom issues is fully covered. `run` is
    injectable so tests can stub the subprocess. Raises on a non-zero exit or a
    timeout — a failure to read the ledger must fail loudly, never silently
    degrade to an empty ledger (which would re-file everything).
    """
    if not _REPO_RE.match(repo or ""):
        raise ValueError(f"invalid repo {repo!r}: expected owner/name")
    try:
        result = run(
            [
                "gh",
                "api",
                "--paginate",
                f"/repos/{repo}/issues?labels={GROOM_LABEL}&state=all&per_page=100",
            ],
            text=True,
            capture_output=True,
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"gh api timed out after {_FETCH_TIMEOUT_SECONDS}s listing groom issues"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(f"gh api failed to list groom issues: {result.stderr.strip()}")
    return _parse_paginated_json(result.stdout)


def _parse_paginated_json(stdout: str):
    """Parse `gh api --paginate` output into one flat list of issues.

    `--paginate` concatenates each page's JSON array. `gh` normally stitches
    them into one array, but be tolerant of the concatenated-arrays shape too
    (multiple top-level `[...]` values) so a gh behavior change can't silently
    truncate the ledger to page one.
    """
    text = stdout.strip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    issues, idx, n = [], 0, len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        value, end = decoder.raw_decode(text, idx)
        if isinstance(value, list):
            issues.extend(value)
        elif isinstance(value, dict):
            issues.append(value)
        idx = end
    return issues


def load_ledger(repo: str, run=subprocess.run) -> Ledger:
    """Read live GitHub issue state and return the dedup Ledger."""
    return Ledger(build_ledger(fetch_groom_issues(repo, run=run)))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Durable groom dedup/rejection ledger backed by GitHub issue state."
    )
    parser.add_argument("--repo", required=True, help="owner/name of the target repo")
    parser.add_argument(
        "--candidates",
        help="Path to a JSON array of candidate findings (each with a 'signature').",
    )
    parser.add_argument(
        "--out",
        help="Path to write the {to_file, suppressed, invalid, ledger_size} decision JSON.",
    )
    parser.add_argument(
        "--check",
        metavar="SIGNATURE",
        help="Print the ledger status of one signature and exit 0 if it should be filed, 1 if suppressed.",
    )
    args = parser.parse_args(argv)

    if args.check is None and not args.candidates:
        parser.error("one of --candidates or --check is required")

    ledger = load_ledger(args.repo)

    if args.check is not None:
        # A blank/unusable signature is `invalid`, not filable — mirror
        # `partition` (which routes it to `invalid`) instead of reporting
        # `unknown` and exiting 0, which would file an un-dedupable issue.
        if normalize_signature(args.check) == "":
            print("invalid")
            return 1
        status = ledger.status(args.check)
        print(status)
        return 0 if ledger.should_file(args.check) else 1

    with open(args.candidates, encoding="utf-8") as f:
        findings = json.load(f)
    if not isinstance(findings, list):
        parser.error("--candidates must be a JSON array of findings")

    to_file, suppressed, invalid = ledger.partition(findings)
    decision = {
        "to_file": to_file,
        "suppressed": suppressed,
        "invalid": invalid,
        "ledger_size": len(ledger),
    }

    payload = json.dumps(decision, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
    else:
        print(payload)

    print(
        f"ledger: {len(ledger)} known signature(s); "
        f"{len(to_file)} to file, {len(suppressed)} suppressed, {len(invalid)} invalid.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
