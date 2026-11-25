"""Pure, network-free core of the telemetry-cost-contract gate.

A PR that adds a NEW billable telemetry call site (a Datadog metrics client call, a new
Sentry capture, a PostHog `.capture(`, a new RUM custom action — whatever regex the calling
repo configures via `patterns`) must either explain the cost impact in its own body, under a
`## Cost impact` heading (the TDD section 7.1 contract: priced unit, baseline/scale forecast,
protected signal), or carry the `cost-exempt` label. This is the lightweight repository check
that cost-governance lane C3 (comfy-observability program, D315) asked for, so the ten
zero-consumer custom metrics C2 found months after merge get caught at PR time instead.

Detection reads ONLY GitHub-owned PR metadata — the `patch` fragments `GET
.../pulls/{n}/files` returns for each changed file, plus the whole-PR unified diff from `GET
.../pulls/{n}` under the `application/vnd.github.diff` media type when the former omits a
file's `patch` — never a checkout of the PR branch. Both are the same trust class (rendered
by GitHub, delivered as text) and neither is a checkout, which is what makes this safe to run
on a fork PR's privileged `workflow_run` validator: the untrusted diff text is
pattern-MATCHED, never executed or interpolated.

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

# A unified diff's hunk lines: '+' or '-' prefixed content lines. GitHub's per-file `patch`
# fragments start at the first `@@` hunk header and never carry the `+++`/`---` file-header
# lines a full multi-file diff would have before it, so there is nothing here to exclude by
# content shape. A `(?!\+\+)` lookahead would look like it is guarding against those headers
# but actually drops real added lines whose content itself starts with `++` (e.g.
# `++i, posthog.capture(...)`), which the diff renders as `+++i, ...`.
_ADDED_LINE_RE = re.compile(r"^\+(.*)$", re.MULTILINE)

_COST_IMPACT_HEADING_RE = re.compile(
    r"^(#{1,6})[ \t]+cost impact\b.*$", re.IGNORECASE | re.MULTILINE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE_MARKER_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def _strip_fenced_blocks(text: str) -> str:
    """Remove CommonMark-fenced code blocks (both ``` and ~~~ families, any length >= 3).

    A single ``^```.*?^```'' regex only recognizes the triple-backtick family and accepts any
    line starting with ``` as a closing fence regardless of length — so a 4-backtick opening
    (` ```` `) is "closed" by the first stray triple-backtick line, and a ~~~ fence isn't
    recognized as a fence at all, leaving its content eligible to count as real section text.
    This instead tracks the actual opening marker (character and length) and only closes on a
    line that is itself only that same character, at least that many times — the CommonMark
    rule — so content inside any valid fence is dropped regardless of marker family or length.
    """
    out_lines = []
    fence_char = None
    fence_len = 0
    for line in text.split("\n"):
        if fence_char is None:
            match = _FENCE_MARKER_RE.match(line)
            if match:
                fence_char = match.group(1)[0]
                fence_len = len(match.group(1))
                continue
            out_lines.append(line)
            continue
        stripped = line.strip()
        if (stripped and set(stripped) == {fence_char}
                and len(stripped) >= fence_len):
            fence_char = None
            fence_len = 0
    return "\n".join(out_lines)


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


_DIFF_HEADER_RE = re.compile(r"^diff --git ", re.MULTILINE)


def _unquote_diff_path(raw: str) -> str:
    """The path out of a ``+++ b/...`` line, undoing git's C-style quoting when present.

    git quotes a path containing a space, a quote or a non-ASCII byte (``+++ "b/a b.go"``)
    unless ``core.quotePath=false``, so a naive ``line[6:]`` yields ``a b.go"`` for exactly
    the large generated/fixture files this recovery path exists to read.
    """
    text = raw.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        try:
            text = text[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        except UnicodeDecodeError:
            text = text[1:-1]
    return text[2:] if text.startswith("b/") else text


def split_unified_diff(diff_text: str) -> dict[str, str]:
    """Per-file, hunk-only diff fragments out of a WHOLE-PR unified diff, keyed by new path.

    The recovery half of the unscannable-file problem. ``GET .../pulls/{n}/files`` omits a
    file's ``patch`` past GitHub's per-file render ceiling, but ``GET .../pulls/{n}`` with the
    ``application/vnd.github.diff`` media type still returns that file's hunks — same class of
    GitHub-owned metadata, still no checkout of the PR branch, so the fork-safety contract is
    unchanged. Measured against a live consumer repo on 2026-11-25: an 18.3 MB whole-PR diff
    whose largest JSON fixture (+289,490 lines) carries no API `patch` is fully present here,
    as is every generated-client file that had taken the gate out.

    Each returned value is shaped EXACTLY like GitHub's ``patch`` field — it starts at the
    first ``@@`` hunk header, with the ``diff --git``/``index``/``---``/``+++``/mode lines
    dropped — so ``added_lines`` and ``first_match`` consume it unchanged. That shape is the
    whole point and is not cosmetic: a full multi-file diff DOES carry ``+++ b/<path>`` header
    lines, which ``_ADDED_LINE_RE`` would otherwise report as an added line with content
    ``++ b/<path>``, inventing added content nobody wrote (and a caller pattern matching
    ``+`` or a path fragment would fire on it).

    A section with no ``@@`` at all contributes no entry: that is a pure rename, a mode
    change, or a genuinely binary file (``Binary files a/x and b/y differ``). Returning
    nothing for those is correct and keeps ``unscannable_files`` failing closed on a real
    binary rather than passing it as recovered-and-clean.
    """
    fragments: dict[str, str] = {}
    bounds = [m.start() for m in _DIFF_HEADER_RE.finditer(diff_text or "")]
    for index, start in enumerate(bounds):
        end = bounds[index + 1] if index + 1 < len(bounds) else len(diff_text)
        section = diff_text[start:end]
        lines = section.split("\n")
        name = None
        hunk_at = None
        for offset, line in enumerate(lines):
            if line.startswith("+++ "):
                candidate = _unquote_diff_path(line[4:])
                if candidate and candidate != "/dev/null":
                    name = candidate
            elif line.startswith("@@"):
                hunk_at = offset
                break
        if not name or hunk_at is None:
            continue
        fragments[name] = "\n".join(lines[hunk_at:]).rstrip("\n")
    return fragments


def unscannable_files(files: list[dict]) -> list[str]:
    """Filenames whose diff content this check could not see at all.

    GitHub omits ``patch`` both for genuinely binary files and for text diffs too large to
    render, and the two look identical on the wire. A removed file or one with zero added
    lines can only ever shrink cost, so it is excluded; anything else missing its patch with
    ``additions > 0`` must be treated as unscannable rather than silently passed as "no
    match" — a padded or oversized new telemetry module is exactly the case this gate exists
    to catch, and ``first_match`` alone cannot distinguish it from a harmless binary asset.

    Call this AGAIN after splicing in whatever ``split_unified_diff`` recovered: the API's
    per-file ``patch`` ceiling is far lower than the whole-PR diff endpoint's, so in practice
    almost every file this returns on the first pass IS readable, and only a still-nonempty
    list after recovery is a genuine cannot-verify (a real binary).
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
    heading followed by at least one non-blank, non-placeholder line before the next
    same-or-shallower heading or EOF.

    A bare heading with nothing under it (copy-pasted template, never filled in) does not
    count — TDD section 7.1 requires actual content (priced unit, baseline, protected signal),
    not just the section's existence. The heading match consumes the whole heading line (so
    trailing text like ``: TBD`` or ``(required)`` is never mistaken for section content).
    Fenced code blocks are stripped from the *whole* body before the heading search even
    starts — not just from the text after a match — so an example in the PR template (a
    fenced block that itself contains a ``## Cost impact`` line) can never be mistaken for the
    real heading. The section ends only at a heading of the same or shallower depth: a
    ``## Cost impact`` section legitimately contains deeper ``###`` sub-headings (priced unit,
    baseline, protected signal) without those ending the section early. HTML comments are
    stripped from the remaining section body before the content check, so an untouched
    PR-template placeholder (``<!-- priced unit, ... -->``) does not count either.
    """
    text = _strip_fenced_blocks(body or "")
    match = _COST_IMPACT_HEADING_RE.search(text)
    if not match:
        return False
    level = len(match.group(1))
    rest = text[match.end():]
    next_heading = re.compile(rf"^#{{1,{level}}}[ \t]", re.MULTILINE).search(rest)
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
        "this change must not regress.{exemption}"
    ),
    "changed_files_unavailable": (
        "This check could not be completed because the PR's changed-file list (with diff "
        "patches) could not be read completely, **and** the whole-PR unified diff could not "
        "supply the missing content either. A partial list could miss the very file that "
        "added a billable call site, so this fails closed instead of declaring a partial scan "
        "clean. This is an infrastructure error, not a verdict on cost impact: it is reported "
        "as the commit-status state `error`, never `failure`, so it is distinguishable from a "
        "real contract violation both by eye and by any tool reading the status."
    ),
}

# The commit-status state each failing category publishes. These are deliberately DIFFERENT,
# and that is the point: `failure` is this gate's verdict that the contract was broken, while
# `error` is this gate reporting that it could not reach a verdict at all. Collapsing both to
# `failure` is what made the gate lie in the direction that costs the most — a PR carrying a
# red "could not verify" is indistinguishable at a glance from a real failing test, and it
# pushes the PR to `mergeable_state: unstable` either way. Measured on a consumer repo three
# days into the warn-only pilot (2026-11-25): 8 of the 10 red statuses across 339 open PRs
# were this category, and all 8 were substantively clean. Both states are non-success, so
# fail-closed behaviour under `enforce: true` is unchanged: a required context still blocks.
_FAILING_STATE = {
    "missing_cost_impact": "failure",
    "changed_files_unavailable": "error",
}


def failure_guidance(category: str, exempt_label: str = "") -> str:
    """The fixed, category-specific paragraph for the PR comment and job summary.

    ``exempt_label`` only shapes the ``missing_cost_impact`` text: a caller that set
    ``EXEMPT_LABEL=''`` disabled the exemption entirely, so the guidance must not name a
    label that can never waive the check — see validate.py's identical footer rule."""
    text = _GUIDANCE[category]
    if category == "missing_cost_impact":
        exemption = (
            f" If this change adds no new billable volume, apply the `{exempt_label}` "
            "label instead." if exempt_label else ""
        )
        text = text.format(exemption=exemption)
    return text


_STATUS_HEADLINE = {
    "missing_cost_impact": "New telemetry call site with no Cost impact section",
    "changed_files_unavailable": "Could not verify — the PR's changed-file list is unreadable",
}

_ADVISORY = (
    "> ⚠️ **Advisory — this check is warn-only.** It is reported as not passing so it shows up "
    "in the PR's check list, but nothing this workflow does blocks a merge: what blocks is this "
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
    state = _FAILING_STATE[category]
    if enforce:
        return FailureOutcome(
            verdict=f"❌ fail ({category})",
            state=state,
            description=headline,
            exit_code=1,
            advisory=False,
        )
    if soft_fail:
        return FailureOutcome(
            verdict=f"❌ fail ({category}) — warn-only",
            state=state,
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
