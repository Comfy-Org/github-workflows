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

# The signature marker embedded in a filed issue's body. An HTML comment so it
# renders invisibly, and a stable prefix so it round-trips through the API's raw
# body. Kept deliberately simple; the signature itself is opaque.
_MARKER_PREFIX = "groom-signature:"
_MARKER_RE = re.compile(r"<!--\s*groom-signature:\s*(.+?)\s*-->", re.DOTALL)

# Ledger statuses. UNKNOWN is the only one that permits filing.
FILED = "filed"
REJECTED = "rejected"
SUPERSEDED = "superseded"
UNKNOWN = "unknown"

# Precedence when several issues share one signature (shouldn't happen, but be
# robust): surface the most decision-bearing status. Rejection is the stickiest
# human signal, so it wins; a superseded marker beats a plain filed one. The
# dedup DECISION doesn't depend on this ordering — every non-UNKNOWN status
# suppresses filing equally — only the reported status does.
_PRECEDENCE = {REJECTED: 3, SUPERSEDED: 2, FILED: 1}


def signature_marker(signature: str) -> str:
    """The HTML-comment marker the filing step must append to an issue body.

    Round-trips with `extract_signature`. The filing step owns applying this
    (and the `groom` label); if it doesn't, the next run cannot recognize the
    issue and will re-file the finding.
    """
    return f"<!-- {_MARKER_PREFIX} {normalize_signature(signature)} -->"


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

    Tolerant of the surrounding markdown/prose an issue body carries; returns
    the FIRST marker found (a groom issue embeds exactly one).
    """
    if not body:
        return None
    match = _MARKER_RE.search(body)
    if not match:
        return None
    signature = normalize_signature(match.group(1))
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
    """Map one groom issue to a ledger status (never UNKNOWN — it exists).

    Rejection is recognized two ways, either of which is durable:
      * the `groom-rejected` label (open or closed), or
      * closed as `not_planned` — GitHub's "Close as not planned" == wontfix.
    A `groom-superseded` label marks a replaced finding. Everything else
    (open, or closed as completed/fixed) is FILED: already handled, don't
    re-file.
    """
    labels = _labels(issue)
    closed_not_planned = (
        issue.get("state") == "closed" and issue.get("state_reason") == "not_planned"
    )
    if REJECTED_LABEL in labels or closed_not_planned:
        return REJECTED
    if SUPERSEDED_LABEL in labels:
        return SUPERSEDED
    return FILED


def build_ledger(issues) -> dict:
    """Build a {signature -> status} map from a list of groom issues.

    Issues without a recoverable signature marker are skipped: a `groom`-labeled
    issue a human opened by hand (no marker) is not one of ours and must not
    poison a signature key. Pull requests (the `/issues` endpoint returns them
    too) are skipped. When two issues share a signature, the higher-precedence
    status wins (`_PRECEDENCE`).
    """
    statuses: dict = {}
    for issue in issues:
        if issue.get("pull_request"):
            continue
        signature = extract_signature(issue.get("body"))
        if not signature:
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
        """A finding is filed only if its signature is genuinely new."""
        return self.status(signature) == UNKNOWN

    def partition(self, findings):
        """Split candidate findings into to_file / suppressed / invalid.

        `to_file`  — signature is UNKNOWN: open an issue (remember to embed the
                     marker + apply the `groom` label).
        `suppressed` — signature is known (filed/rejected/superseded): each
                     annotated with `ledger_status` for auditable reporting.
        `invalid`  — no usable signature: cannot be deduped, so it is NOT filed
                     (filing it would risk the exact duplicate-spam this ledger
                     exists to prevent). The workflow should surface these as a
                     producer error rather than silently dropping or spamming.
        """
        to_file, suppressed, invalid = [], [], []
        for finding in findings:
            signature = normalize_signature(finding.get("signature")) if isinstance(finding, dict) else ""
            if not signature:
                invalid.append(finding)
                continue
            status = self.status(signature)
            if status == UNKNOWN:
                to_file.append(finding)
            else:
                suppressed.append({**finding, "ledger_status": status})
        return to_file, suppressed, invalid


def fetch_groom_issues(repo: str, run=subprocess.run):
    """List every `groom`-labeled issue in `repo` (state=all) via `gh api`.

    Paginated so a repo with many groom issues is fully covered. `run` is
    injectable so tests can stub the subprocess. Raises on a non-zero exit —
    a failure to read the ledger must fail loudly, never silently degrade to an
    empty ledger (which would re-file everything).
    """
    result = run(
        [
            "gh",
            "api",
            "--paginate",
            f"/repos/{repo}/issues?labels={GROOM_LABEL}&state=all&per_page=100",
        ],
        text=True,
        capture_output=True,
    )
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
        status = ledger.status(args.check)
        print(status)
        return 0 if status == UNKNOWN else 1

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
