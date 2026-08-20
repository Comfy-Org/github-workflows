"""Pure, network-free core of the linear-ticket gate.

Everything here is a function with no side effect: pull candidate identifiers out of
untrusted PR text, validate the team-key policy input, apply the team/state policy to
Linear's attachment response, classify a Linear error as retryable or terminal, pick a
failure category, and render the copy that explains it. tests/test_lib.py imports this
module and exercises the whole decision path without a single API call. The side-effecting
orchestration (resolve the PR, query Linear, publish the commit status, upsert the comment)
lives in validate.py, which imports this.

The invariant this file underwrites (design §3): the ONLY thing that turns the check green
is an attachment Linear returns for THIS PR's canonical html_url whose issue satisfies
policy — filter_issues below. Candidate identifiers extracted from branch, title, and body
(extract_candidates) are diagnostics ONLY; they explain why a check is red, they can never
make it green. That is why the extractor is deliberately generic and the policy filter reads
the resolved issue's real API ``team.key`` and ``state.type``, never a prefix the author typed.

Standard library only (no third-party deps), matching this repo's Python convention.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# The hidden marker on the single PR comment this gate maintains. validate.py finds its
# existing comment by this exact string, so it must never change casually.
MARKER = "<!-- linear-ticket-check -->"

# The stable commit-status context branch protection requires. Publishing is done in
# validate.py; the name lives here so the tests can pin it.
CONTEXT = "linear-ticket"

# The design's case-insensitive identifier shape: [A-Z][A-Z0-9]*-\d+.
_CANDIDATE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*-[0-9]+")
# A valid Linear team key: uppercase, starts with a letter.
_TEAM_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*$")

MAX_CANDIDATES = 20
_CLOSED_STATE_TYPES = frozenset({"completed", "canceled"})


def extract_candidates(text: str) -> list[str]:
    """Up to 20 unique UPPERCASE candidate identifiers, first-seen order.

    Diagnostics only: they let a failure comment say "you referenced BE-1234 but Linear
    has not linked it", never a pass. The 20-cap means author-controlled text cannot fan
    out into an unbounded diagnostic query.
    """
    seen: dict[str, None] = {}
    for match in _CANDIDATE_RE.findall(text or ""):
        seen.setdefault(match.upper(), None)
        if len(seen) >= MAX_CANDIDATES:
            break
    return list(seen)


def normalize_team_keys(raw: str) -> list[str]:
    """Parse the caller's comma-separated ``team-keys`` input into normalized keys.

    Empty/whitespace-only input -> ``[]`` (the "accept any visible team" default). Raises
    ValueError — rejecting the whole run as a caller misconfiguration — if any non-empty
    entry is malformed or a duplicate (design §5.1). Fail closed here rather than silently
    dropping a key, because a dropped key would quietly widen the policy the caller asked
    to narrow.
    """
    if not (raw or "").strip():
        return []
    keys: list[str] = []
    for part in raw.split(","):
        part = part.strip().upper()
        if not part:
            continue  # tolerate a stray "BE,,ENG" empty field
        if not _TEAM_KEY_RE.match(part):
            raise ValueError(f"malformed team key: {part!r}")
        if part in keys:
            raise ValueError(f"duplicate team key: {part!r}")
        keys.append(part)
    return keys


def parse_actor_list(raw: str) -> list[str]:
    """Parse the caller's comma-separated ``exempt-actors`` input into lowercased logins.

    Empty/whitespace-only input -> ``[]`` (no actor is exempt — the secure default; the
    design ships no built-in bot bypass). Logins carry ``[bot]`` suffixes and other punctuation
    so, unlike team keys, they are not shape-validated — an unknown login simply never matches.
    Lowercased because GitHub logins are compared case-insensitively.
    """
    return [part.strip().lower() for part in (raw or "").split(",") if part.strip()]


def filter_issues(nodes: list[dict], team_keys: list[str], require_open: bool) -> list[str]:
    """THE GATE. Identifiers of the linked issues that satisfy policy, sorted unique.

    Not every link is validated: this returns the SUBSET that passes, and the caller treats a
    non-empty result as a pass. So a PR linked to one open and one canceled issue passes on the
    open one — "any linked issue satisfies policy", by design (below), not "every link is valid".

    ``nodes`` is Linear's ``attachmentsForURL.nodes`` array; each node carries
    ``issue{identifier, team{key}, state{type}}``. Empty result == nothing passes. Policy:

    * team  — keep an issue iff ``team_keys`` is empty OR its API-returned ``team.key`` is in
              the allow-list. Matched against the RESOLVED key, never a prefix.
    * state — when ``require_open``, reject ``state.type`` completed/canceled; backlog,
              unstarted, started, and triage pass. A missing ``state.type`` does not block.

    "Any issue remains" is the rule: a PR linked to several tickets passes when at least one
    linked issue satisfies policy (design §5).
    """
    passing: set[str] = set()
    for node in nodes or []:
        issue = (node or {}).get("issue")
        if not issue:
            continue
        if team_keys:
            key = ((issue.get("team") or {}).get("key")) or ""
            if key not in team_keys:
                continue
        if require_open:
            state_type = ((issue.get("state") or {}).get("type")) or ""
            if state_type in _CLOSED_STATE_TYPES:
                continue
        identifier = issue.get("identifier")
        if identifier:
            passing.add(identifier)
    return sorted(passing)


def count_linked(nodes: list[dict]) -> int:
    """How many attachments carry a non-null issue (linked issues, before policy).

    Distinguishes "nothing linked yet" (retry / not-linked copy) from "something linked but
    fails policy" (policy_mismatch).
    """
    return sum(1 for node in (nodes or []) if (node or {}).get("issue"))


def classify_linear_error(http_status: int | None, error_codes: list[str]) -> str:
    """"retryable" or "terminal".

    Linear signals rate limiting as HTTP 400 with GraphQL error code RATELIMITED (design
    §8), so the codes are checked first. 408/429/5xx are transient. Everything else — auth,
    schema, malformed — is terminal and fails closed as an infrastructure error, never as an
    invalid ticket.
    """
    if "RATELIMITED" in (error_codes or []):
        return "retryable"
    if http_status in (408, 429, 500, 502, 503, 504):
        return "retryable"
    return "terminal"


def select_failure_category(infra_error: bool, linked: int, referenced: int) -> str:
    """The one category that explains a red check, in priority order (design §5 step 6).

    * infra_error       — Linear could not be queried (auth/schema/exhausted retries).
    * policy_mismatch   — issue(s) ARE linked to this PR but every one fails team/state policy.
    * exists_not_linked — no link, but an identifier was REFERENCED in branch/title/body.
    * no_candidate      — no link and no identifier referenced anywhere.

    The boundary between the last two is whether an identifier was referenced at all; the
    batched diagnostic lookup (count_resolved_candidates) only enriches the DETAIL line, it
    does not move the category — so the copy can never say "no identifier detected" while the
    detail lists one.
    """
    if infra_error:
        return "infra_error"
    if linked > 0:
        return "policy_mismatch"
    if referenced > 0:
        return "exists_not_linked"
    return "no_candidate"


_GUIDANCE = {
    "no_candidate": (
        "No linked Linear issue was found for this PR, and no issue identifier was detected "
        "in the branch name, title, or body. Link a Linear issue by any supported method — "
        "put its identifier (e.g. `BE-1234`) in the branch name, PR title, or body "
        "(`Closes BE-1234`), or paste this PR's URL into the issue in Linear."
    ),
    "exists_not_linked": (
        "An issue identifier was referenced, but Linear has not linked that issue to this PR "
        "yet. A referenced identifier in text is not a link. Either use a supported auto-link "
        "(identifier in the branch name, title, or a `Closes`/`Fixes`/`Resolves` line in the "
        "body) or paste this PR's canonical URL into the issue in Linear. Automatic links are "
        "created a few seconds after the PR event, so a brand-new link may just need a re-run."
    ),
    "policy_mismatch": (
        "A Linear issue is linked to this PR, but no linked issue satisfies this repository's "
        "policy — every linked issue is either in a completed/canceled state or belongs to a "
        "team this check does not accept. Link an issue from an accepted team that is not "
        "closed, or move the existing issue back to an open state."
    ),
    "infra_error": (
        "This check could not be completed because Linear could not be queried "
        "(authentication, schema, timeout, or rate-limit exhaustion). This is an "
        "infrastructure error, not a verdict on your ticket — it fails closed on purpose so a "
        "broken credential cannot silently disable the control. Re-run the check; if it keeps "
        "failing, contact the repository owners."
    ),
}


def failure_guidance(category: str) -> str:
    """The fixed, category-specific paragraph shown in the PR comment and job summary.

    Specifics (identifiers, candidate list, rerun link) are appended by validate.py; this
    keeps the reusable copy testable and consistent across repos. Raises KeyError on an
    unknown category so a typo fails loudly rather than shipping empty copy.
    """
    return _GUIDANCE[category]


# ── how a failure is REPORTED (loudness, not diagnosis) ─────────────────────────────────
# select_failure_category above says WHY a check is red; the mode below says how loudly it is
# reported. Whether a red check BLOCKS a merge is never decided here — that is branch
# protection deciding whether the `linear-ticket` context is required:
#
#   enforce                    -> failure status, exit 1. The gating configuration.
#   warn-only + soft-fail      -> failure status, exit 0. Shows on the PR exactly like enforce
#                                 does, but blocks nothing while the context is not required.
#                                 The loud pilot rung: a green check nobody looks at teaches a
#                                 repo nothing, and the pilot's whole purpose is observation.
#   warn-only, soft-fail off   -> success status, exit 0. Silent; only the job summary and the
#                                 marker comment carry the verdict.
#
# The job's OWN exit code is not what a reviewer sees. This validator runs on the default
# branch off workflow_run, so its run is not attached to the PR — the commit status is the
# only PR-visible signal, which is why soft-fail moves the STATUS and leaves the run green.
# A red default-branch run for a check that is deliberately not gating would read as "main is
# broken" in the Actions tab, which is louder in the wrong place.

# Appended to the marker comment whenever the check is red but non-gating, so nobody reads the
# red X as a merge block (and nobody assumes it will stay non-blocking forever).
ADVISORY_NOTE = (
    "> ⚠️ **This does not block the merge.** The check is reported as failing so it is visible "
    "in the PR's check list, but `linear-ticket` is not a required status while this repository "
    "is in the warn-only pilot. Linking the ticket now is what keeps it from blocking you once "
    "the pilot starts enforcing."
)


class FailureOutcome(NamedTuple):
    """How one failing verdict is reported.

    verdict     headline for the marker comment and job summary
    state       commit-status state published on the PR head SHA
    description commit-status description (validate.py truncates to GitHub's 140 chars)
    exit_code   the validator process's exit code
    advisory    whether to append ADVISORY_NOTE to the comment
    """

    verdict: str
    state: str
    description: str
    exit_code: int
    advisory: bool


def failure_outcome(category: str, enforce: bool, soft_fail: bool) -> FailureOutcome:
    """Map (category, mode) to the reported outcome. See the mode table above.

    ``soft_fail`` is read only when ``enforce`` is false — enforcing already publishes the red
    status soft-fail exists to produce, and an enforcing run must still exit nonzero. Raises
    KeyError on an unknown category, matching failure_guidance, so a typo fails loudly instead
    of shipping a status nobody can explain.
    """
    if category not in _GUIDANCE:
        raise KeyError(category)
    if enforce:
        return FailureOutcome(
            verdict=f"\u274c fail ({category})",
            state="failure",
            description=f"No linked Linear issue ({category})",
            exit_code=1,
            advisory=False,
        )
    if soft_fail:
        return FailureOutcome(
            verdict=f"\u274c fail ({category}) \u2014 advisory, not blocking",
            state="failure",
            description=f"Advisory (warn-only): no linked Linear issue ({category})",
            exit_code=0,
            advisory=True,
        )
    return FailureOutcome(
        verdict=f"\u26a0\ufe0f warn-only (would fail: {category})",
        state="success",
        description=f"warn-only: would fail ({category})",
        exit_code=0,
        advisory=False,
    )


def build_diagnostic_query(candidates: list[str]) -> str:
    """ONE aliased GraphQL query (``c0: issueSearch(...) c1: ...``) resolving all candidates.

    Design §8: at most one batched diagnostic query. Raises ValueError if there are no
    candidates. Candidates from extract_candidates are strictly ``[A-Z0-9-]+``, so embedding
    them in a double-quoted GraphQL string literal is safe; this re-asserts that invariant so
    a future caller cannot smuggle a quote through.
    """
    aliases = []
    for i, identifier in enumerate(candidates or []):
        if not re.match(r"^[A-Z0-9-]+$", identifier):
            continue
        aliases.append(f'  c{i}: issueSearch(query: "{identifier}", first: 1) {{ nodes {{ identifier }} }}')
    if not aliases:
        raise ValueError("no candidates to build a diagnostic query from")
    return "query {\n" + "\n".join(aliases) + "\n}\n"


def count_resolved_candidates(response: dict) -> int:
    """How many aliased issueSearch results returned at least one node.

    Best-effort: a malformed/absent ``data`` counts as 0, so a diagnostic hiccup degrades to
    the "no_candidate"/"exists_not_linked" copy rather than erroring.
    """
    if not isinstance(response, dict):
        return 0
    data = response.get("data") or {}
    if not isinstance(data, dict):
        return 0
    resolved = 0
    for value in data.values():
        nodes = (value or {}).get("nodes") if isinstance(value, dict) else None
        if nodes:
            resolved += 1
    return resolved
