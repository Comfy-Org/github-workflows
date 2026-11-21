"""Pure, network-free core of the telemetry-cost-contract gate.

A PR that adds a NEW billable telemetry call site (a Datadog metrics client call, a new
Sentry capture, a PostHog `.capture(`, a new RUM custom action — whatever regex the calling
repo configures via `patterns`) must either explain the cost impact in its own body, under a
`## Cost impact` heading (the TDD section 7.1 contract: priced unit, baseline/scale forecast,
protected signal), or carry the `cost-exempt` label. This is the lightweight repository check
that cost-governance lane C3 (comfy-observability program, D315) asked for, so the ten
zero-consumer custom metrics C2 found months after merge get caught at PR time instead.

Detection reads ONLY GitHub-owned PR metadata — the `patch` fragments `GET
.../pulls/{n}/files` returns for each changed file — never a checkout of the PR branch. That
is what makes this safe to run on a fork PR's privileged `workflow_run` validator: the
untrusted diff text is pattern-MATCHED, never executed or interpolated.

Side-effecting orchestration (resolve the PR, read its files, publish the commit status,
upsert the comment) lives in validate.py, which imports this. Standard library only, matching
this repo's Python convention.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# The hidden marker on the single PR comment this gate maintains.
MARKER = "<!-- telemetry-cost-contract-check -->"

# The commit-status context. Human-facing AND a contract (see linear-ticket's CONTEXT for why
# a rename silently retires a required check) — check every caller's ruleset before renaming.
CONTEXT = "Telemetry cost contract"

# A unified diff's hunk lines: '+' or '-' prefixed content lines, vs. the '+++'/'---' file
# headers and '@@' hunk headers, which must never be mistaken for added content.
_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)

_COST_IMPACT_HEADING_RE = re.compile(
    r"^#{1,6}[ \t]+cost impact\b.*$", re.IGNORECASE | re.MULTILINE)
_ANY_HEADING_RE = re.compile(r"^#{1,6}[ \t]", re.MULTILINE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCED_BLOCK_RE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)


def parse_patterns(raw: str) -> list[re.Pattern]:
    """Parse the caller's newline-separated ``patterns`` input into compiled regexes.

    Newline-separated, not comma-separated: a regex alternation (`a|b`) is common here and a
    comma inside a character class or quantifier is legal regex syntax, so comma-splitting
    would silently corrupt a pattern. Blank lines and lines starting with ``#`` (comments) are
    skipped. Raises ValueError — rejecting the whole run as a caller misconfiguration — when
    no pattern survives, or any surviving line fails to compile: an empty pattern set would
    make every PR trivially "not cost-sensitive", silently disabling the gate, and a pattern
    that cannot compile is a caller typo that must not degrade to "never matches" in silence.
    """
    patterns: list[re.Pattern] = []
    for line in (raw or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            patterns.append(re.compile(text))
        except re.error as exc:
            raise ValueError(f"invalid pattern {text!r}: {exc}") from exc
    if not patterns:
        raise ValueError("no patterns configured (every line was blank or a comment)")
    return patterns


def added_lines(patch: str) -> list[str]:
    """Content of every added line in a unified-diff ``patch`` fragment, '+' stripped.

    Deliberately line-moves-safe in the cheap direction GitHub already gives us: a line that
    only moved position within the same hunk still appears here as added (the diff algorithm,
    not this function, decides that) — this function only strips the `+`/`-`/`@@` diff syntax
    from genuinely added content. A rename-only diff (file moved, content untouched) carries no
    added lines and matches nothing, which is correct: nothing new was written.
    """
    return [m.group(1) for m in _ADDED_LINE_RE.finditer(patch or "")]


def first_match(files: list[dict], patterns: list[re.Pattern]) -> tuple[str, str] | None:
    """The first (filename, pattern-text) whose pattern matches an added line in some file's
    patch, in file order then pattern order; None if nothing matches.

    Each added line is matched on its own (``any(p.search(line) for line in lines)``), never
    joined into one multi-line blob: joining would make an anchored pattern like
    `^\\s*statsd\\.` see only the file's first added line (`^`/`$` bind to the whole blob, not
    each line, without `re.MULTILINE`), AND would let a pattern containing `\\s`, a negated
    character class, or `.` with `re.DOTALL` span two unrelated added lines — or even two
    different hunks — and match content nobody actually wrote. Per-line matching has neither
    failure mode: a single line has no embedded newline for an anchor to miss or a
    cross-line pattern to exploit.

    A file with no ``patch`` key (binary, or a diff too large for GitHub to render — GitHub
    omits `patch` past its ~1MB-per-file render ceiling and does not flag the omission)
    contributes no lines and is silently skipped here. Call ``unscannable_files`` first and
    fail closed on anything it returns — a large/padded file missing its patch must not get a
    free pass just because this function cannot see its content.
    """
    for entry in files or []:
        patch = entry.get("patch") if isinstance(entry, dict) else None
        if not patch:
            continue
        lines = added_lines(patch)
        for pattern in patterns:
            if any(pattern.search(line) for line in lines):
                return entry.get("filename") or "<unknown file>", pattern.pattern
    return None


def unscannable_files(files: list[dict]) -> list[str]:
    """Filenames whose diff content this check could not see at all.

    GitHub omits ``patch`` both for genuinely binary files and for text diffs too large to
    render, and the two look identical on the wire. A removed file or one with zero added
    lines can only ever shrink cost, so it is excluded; anything else missing its patch with
    ``additions > 0`` must be treated as unscannable rather than silently passed as "no
    match" — a padded or oversized new telemetry module is exactly the case this gate exists
    to catch, and ``first_match`` alone cannot distinguish it from a harmless binary asset.
    """
    names = []
    for entry in files or []:
        if not isinstance(entry, dict) or entry.get("patch"):
            continue
        if entry.get("status") == "removed":
            continue
        if (entry.get("additions") or 0) <= 0:
            continue
        names.append(entry.get("filename") or "<unknown file>")
    return names


def has_cost_impact_section(body: str) -> bool:
    """True iff ``body`` has a ``## Cost impact`` (any heading depth, case-insensitive)
    heading followed by at least one non-blank, non-placeholder line before the next heading
    or EOF.

    A bare heading with nothing under it (copy-pasted template, never filled in) does not
    count — TDD section 7.1 requires actual content (priced unit, baseline, protected signal),
    not just the section's existence. The heading match consumes the whole heading line (so
    trailing text like ``: TBD`` or ``(required)`` is never mistaken for section content), and
    the section body has HTML comments and fenced code blocks stripped before the
    content check, so an untouched PR-template placeholder (``<!-- priced unit, ... -->`` or an
    example inside a ``` fence) does not count either.
    """
    match = _COST_IMPACT_HEADING_RE.search(body or "")
    if not match:
        return False
    # Strip fenced blocks before looking for the next heading: a heading-shaped line quoted
    # inside a fence (e.g. an example of the template itself) must not be mistaken for the
    # section boundary.
    rest = _FENCED_BLOCK_RE.sub("", (body or "")[match.end():])
    next_heading = _ANY_HEADING_RE.search(rest)
    section_body = rest[:next_heading.start()] if next_heading else rest
    section_body = _HTML_COMMENT_RE.sub("", section_body)
    return any(line.strip() for line in section_body.splitlines())


def soft_fail_enabled(raw: str | None) -> bool:
    """Parse the ``SOFT_FAIL`` env var. An ABSENT value means the SILENT warn-only variant —
    see linear-ticket's identical helper for why pin-skew must never upgrade green to red."""
    return (raw or "").strip().lower() == "true"


def enforce_enabled(raw: str | None) -> bool:
    """Parse the ``ENFORCE`` env var. An ABSENT value means the documented default: warn-only
    (false) — same shape and rationale as ``soft_fail_enabled``. A caller that omits `enforce`
    gets the pilot rung the docs promise, never an accidental fail-closed gate."""
    return (raw or "").strip().lower() == "true"


_GUIDANCE = {
    "missing_cost_impact": (
        "This PR adds a new billable telemetry call site matching this repository's "
        "cost-sensitive detection patterns, but its body has no `## Cost impact` section. "
        "Per the observability program's TDD section 7.1, add one naming: the priced unit "
        "(events, sessions, custom-metric series, …), a baseline/scale forecast (current "
        "cloud MAU, opted-in Desktop cohort, the local-MAU ceiling), and the protected signal "
        "this change must not regress. If this change adds no new billable volume, apply the "
        "`cost-exempt` label instead."
    ),
    "changed_files_unavailable": (
        "This check could not be completed because the PR's changed-file list (with diff "
        "patches) could not be read completely. A partial list could miss the very file that "
        "added a billable call site, so this fails closed instead of declaring a partial scan "
        "clean. This is an infrastructure error, not a verdict on cost impact."
    ),
}


def failure_guidance(category: str) -> str:
    """The fixed, category-specific paragraph for the PR comment and job summary."""
    return _GUIDANCE[category]


_STATUS_HEADLINE = {
    "missing_cost_impact": "New telemetry call site with no Cost impact section",
    "changed_files_unavailable": "Could not verify — the PR's changed-file list is unreadable",
}

_ADVISORY = (
    "> ⚠️ **Advisory — this check is warn-only.** It is reported as failing so it shows up in "
    "the PR's check list, but nothing this workflow does blocks a merge: what blocks is this "
    "repository's ruleset requiring the `Telemetry cost contract` status context, and a "
    "warn-only pilot is meant to run *before* that context is required. If your merge button "
    "is actually blocked on `Telemetry cost contract`, the context is already required while "
    "the pilot is still warn-only — tell the repository owners, because that combination is a "
    "misconfiguration."
)


def advisory_note(category: str) -> str:
    if category not in _GUIDANCE:
        raise KeyError(category)
    return _ADVISORY


class FailureOutcome(NamedTuple):
    verdict: str
    state: str
    description: str
    exit_code: int
    advisory: bool


def failure_outcome(category: str, enforce: bool, soft_fail: bool) -> FailureOutcome:
    """Map (category, mode) to the reported outcome — see linear-ticket's identical table for
    the full rationale: enforce gates for real; warn-only+soft-fail is loud but non-blocking;
    warn-only without soft-fail is silent (status green, verdict only in the summary/comment).
    """
    if category not in _GUIDANCE:
        raise KeyError(category)
    headline = _STATUS_HEADLINE[category]
    if enforce:
        return FailureOutcome(
            verdict=f"❌ fail ({category})",
            state="failure",
            description=headline,
            exit_code=1,
            advisory=False,
        )
    if soft_fail:
        return FailureOutcome(
            verdict=f"❌ fail ({category}) — warn-only",
            state="failure",
            description=f"Warn-only: {headline[0].lower()}{headline[1:]}",
            exit_code=0,
            advisory=True,
        )
    return FailureOutcome(
        verdict=f"⚠️ warn-only (would fail: {category})",
        state="success",
        description=f"Warn-only (would fail): {headline[0].lower()}{headline[1:]}",
        exit_code=0,
        advisory=False,
    )
