#!/usr/bin/env python3
"""Grade a pull request's diff into a risk tier (R0..R3).

Pure, stdlib-only, and side-effect free: it reads a `git diff --numstat -z`
stream on stdin (or from a file) and writes a JSON report plus the rendered
Check Run / sticky-comment markdown. It never talks to GitHub — publishing is
`publish_risk.py`'s job, which runs in a separate, PR-code-free workflow job.

Tiers, lowest to highest attention required:

    R0  docs, lockfiles, non-executable metadata
    R1  tests and fixtures
    R2  ordinary application source (the default)
    R3  sensitive surfaces — auth, secrets, migrations, CI/CD, infra, billing

The PR's tier is the MAX of its file tiers (one R3 file makes the PR R3, no
matter how much R0 surrounds it), with a size escalation on top. The sticky
comment then reports the *concentration* — how much of the diff actually sits
at the top tier — so a reviewer can see that the 6% that made it R3 is two
files and 40 lines.

RISK MAP PROVENANCE
-------------------
`RISK_RULES` and the escalation thresholds below are a documented FIRST CUT,
not derived thresholds. BE-5507 (offline grader + backfill over merged
history) owns deriving and tuning them; when it lands, replace `RISK_RULES` /
`FILE_ESCALATE_LINES` / `SIZE_ESCALATE_LINES` in place. Nothing else in this
file or in `pr-risk.yml` depends on their values, and nothing anywhere is
gated on the result, so a wrong threshold costs a mislabelled PR and nothing
else.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys

SCHEMA_VERSION = 1

# Hidden marker that makes the PR comment sticky. MUST equal publish_risk.MARKER
# — the grader renders it into the body, the publisher searches for it to decide
# update-vs-create. If they drift, every push posts a NEW comment instead of
# updating the existing one. A unit test asserts the two stay equal.
DEFAULT_MARKER = "<!-- ci-pr-risk -->"

# Tier constants, so the rule table below reads as prose.
R0, R1, R2, R3 = 0, 1, 2, 3
MAX_TIER = R3

# Ordinary source code — the tier a file gets when no rule matches.
DEFAULT_TIER = R2
DEFAULT_REASON = "application source"

# --- risk map (placeholder pending BE-5507 — see module docstring) ----------
#
# Evaluated IN ORDER; first match wins. The ordering is deliberate:
#
#   1. tests first  — a test-only change is low risk regardless of what it
#                     tests, so `services/auth/auth_test.go` is R1, not R3.
#   2. docs next    — `docs/auth.md` is prose about a sensitive surface, not
#                     the surface itself.
#   3. sensitive    — everything that can break production, leak, or migrate.
#   4. default R2   — ordinary source.
#
# A pattern containing `/` is matched against the full repo-relative path; one
# without is matched against the base name at any depth (same convention as
# pr-size.yml's `extra_generated_globs`).
RISK_RULES: list[tuple[str, int, str]] = [
    # 1. tests and fixtures
    ("*_test.go", R1, "test code"),
    ("test_*.py", R1, "test code"),
    ("*_test.py", R1, "test code"),
    ("*_test.sh", R1, "test code"),
    ("*.test.ts", R1, "test code"),
    ("*.test.tsx", R1, "test code"),
    ("*.spec.ts", R1, "test code"),
    ("*.spec.tsx", R1, "test code"),
    ("**/tests/**", R1, "test code"),
    ("**/test/**", R1, "test code"),
    ("**/__tests__/**", R1, "test code"),
    ("**/testdata/**", R1, "test fixture"),
    ("**/fixtures/**", R1, "test fixture"),
    # 2. docs, lockfiles, non-executable metadata
    ("*.md", R0, "documentation"),
    ("*.mdx", R0, "documentation"),
    ("*.txt", R0, "documentation"),
    ("*.rst", R0, "documentation"),
    ("docs/**", R0, "documentation"),
    ("LICENSE*", R0, "repository metadata"),
    ("CODEOWNERS", R0, "repository metadata"),
    (".gitignore", R0, "repository metadata"),
    (".github/ISSUE_TEMPLATE/**", R0, "repository metadata"),
    ("go.sum", R0, "dependency lockfile"),
    ("go.work.sum", R0, "dependency lockfile"),
    ("package-lock.json", R0, "dependency lockfile"),
    ("pnpm-lock.yaml", R0, "dependency lockfile"),
    ("yarn.lock", R0, "dependency lockfile"),
    ("Cargo.lock", R0, "dependency lockfile"),
    ("poetry.lock", R0, "dependency lockfile"),
    ("uv.lock", R0, "dependency lockfile"),
    # 3. sensitive surfaces
    ("**/migrations/**", R3, "database migration"),
    ("**/migrate/**", R3, "database migration"),
    ("*.sql", R3, "database schema / DDL"),
    (".github/workflows/**", R3, "CI/CD pipeline definition"),
    (".github/actions/**", R3, "CI/CD pipeline definition"),
    ("Dockerfile", R3, "container build / deploy surface"),
    ("Dockerfile.*", R3, "container build / deploy surface"),
    ("*.tf", R3, "infrastructure as code"),
    ("*.tfvars", R3, "infrastructure as code"),
    ("**/charts/**", R3, "deploy manifest"),
    ("**/helm/**", R3, "deploy manifest"),
    ("**/auth/**", R3, "authentication / authorization"),
    ("*auth*.go", R3, "authentication / authorization"),
    ("*auth*.py", R3, "authentication / authorization"),
    ("*auth*.ts", R3, "authentication / authorization"),
    ("**/iam/**", R3, "access control"),
    ("**/rbac/**", R3, "access control"),
    ("**/permissions/**", R3, "access control"),
    ("*secret*", R3, "secret material handling"),
    ("*credential*", R3, "credential handling"),
    ("*.pem", R3, "key material"),
    ("*.key", R3, "key material"),
    ("**/billing/**", R3, "billing / payments"),
    ("**/payments/**", R3, "billing / payments"),
    ("**/security/**", R3, "security-sensitive code"),
]

# A single file changing this many lines escalates that file one tier (capped
# at R3): size is itself a review-attention signal, independent of what the
# file is.
FILE_ESCALATE_LINES = 300

# Whole-PR line count that escalates the PR's overall tier one step (capped at
# R3). This is what makes a grade go UP as a PR grows — the failure mode named
# in the ticket (a size classifier that grades once and never re-analyses) is
# addressed by re-running on `synchronize`, and this threshold is what that
# re-run actually moves.
SIZE_ESCALATE_LINES = 800

TIER_NAMES = {
    R0: "R0 — trivial",
    R1: "R1 — low",
    R2: "R2 — standard",
    R3: "R3 — needs a careful read",
}


def _matches(pattern: str, path: str) -> bool:
    """Glob-match `path` against `pattern`.

    A pattern with `/` matches the full repo-relative path; one without
    matches the base name at any depth. `**/` is normalised so `**/tests/**`
    also matches a top-level `tests/...` (fnmatch's `*` crosses `/`, so the
    leading `**/` would otherwise require at least one parent directory).
    """
    if "/" in pattern:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if pattern.startswith("**/"):
            return fnmatch.fnmatchcase(path, pattern[3:])
        return False
    return fnmatch.fnmatchcase(os.path.basename(path), pattern)


def classify_path(path: str) -> tuple[int, str]:
    """Return `(tier, reason)` for one repo-relative path."""
    for pattern, tier, reason in RISK_RULES:
        if _matches(pattern, path):
            return tier, reason
    return DEFAULT_TIER, DEFAULT_REASON


def parse_numstat(data: str) -> list[dict]:
    """Parse `git diff --numstat -z` output into file records.

    `-z` is used so paths containing spaces or newlines round-trip safely. In
    `-z` mode git emits `added\\tdeleted\\t` followed by a NUL-terminated path;
    for renames it emits the path fields as two further NUL-terminated
    entries (old, new). We keep the NEW path and remember the old one as
    `old_path`, so `grade` can score the rename at the higher of the two ends.

    A binary file's counts are `-`; it is recorded with zero counted lines
    (there is no line count to reason about) but still classified, so a binary
    blob dropped into `**/security/**` still shows up in the breakdown.
    """
    files: list[dict] = []
    parts = data.split("\0")
    i = 0
    while i < len(parts):
        chunk = parts[i]
        i += 1
        if not chunk.strip():
            continue
        # Split at most twice: a path may legally contain a TAB, and `-z` emits
        # it unquoted, so an unbounded split would truncate `a\tb/auth.go` to
        # `a` and grade it R2 instead of R3.
        fields = chunk.split("\t", 2)
        if len(fields) < 3:
            # Not a numstat record (trailing junk); skip rather than crash —
            # a malformed line must not turn a gradable PR into "unknown".
            continue
        added_s, deleted_s, path = fields[0], fields[1], fields[2]
        binary = added_s == "-" or deleted_s == "-"
        old_path = ""
        if path == "":
            # Rename/copy: the old and new paths follow as separate records.
            if i + 1 >= len(parts):
                continue
            old_path, path = parts[i], parts[i + 1]
            i += 2
            if not path:
                # Truncated rename record — the new path never arrived. Skip it
                # rather than append an entry with an empty path, which would
                # grade as the R2 default and render a blank table row.
                continue
        if binary:
            added = deleted = 0
        else:
            try:
                added = int(added_s or 0)
                deleted = int(deleted_s or 0)
            except ValueError:
                # A non-numeric count is one malformed record, not a whole-diff
                # failure: skip just this file, per the contract above. Letting
                # ValueError escape would downgrade the entire PR to "unknown".
                continue
        record = {
            "path": path,
            "added": added,
            "deleted": deleted,
            "changed": added + deleted,
            "binary": binary,
        }
        if old_path:
            record["old_path"] = old_path
        files.append(record)
    return files


def grade(files: list[dict]) -> dict:
    """Grade parsed numstat records into a full report dict."""
    graded = []
    for f in files:
        tier, reason = classify_path(f["path"])
        old_path = f.get("old_path")
        if old_path:
            old_tier, old_reason = classify_path(old_path)
            if old_tier > tier:
                # A rename that moves a file OFF a sensitive surface still
                # removes that surface, so grade it at the higher of the two
                # ends: `.github/workflows/deploy.yml` -> `docs/deploy.yml` is
                # not an R0 change. The old path itself stays out of the reason
                # string — it is PR-controlled text and belongs only in the
                # JSON report, not in rendered markdown.
                tier, reason = old_tier, f"renamed from {old_reason}"
        escalated = False
        if f["changed"] >= FILE_ESCALATE_LINES and tier < MAX_TIER:
            tier += 1
            reason = f"{reason}, escalated (+{f['changed']} lines in one file)"
            escalated = True
        graded.append({**f, "tier": tier, "tier_reason": reason, "escalated": escalated})

    total_lines = sum(f["changed"] for f in graded)
    tier_lines = {str(t): 0 for t in range(MAX_TIER + 1)}
    for f in graded:
        tier_lines[str(f["tier"])] += f["changed"]

    if not graded:
        # An empty diff is a real, gradable answer (R0) — distinct from
        # "unknown", which means the grader could not read the diff at all.
        base_tier = R0
        base_reason = "no files changed"
    else:
        base_tier = max(f["tier"] for f in graded)
        top = [f for f in graded if f["tier"] == base_tier]
        base_reason = "; ".join(sorted({f["tier_reason"] for f in top}))

    tier = base_tier
    size_escalated = False
    if total_lines >= SIZE_ESCALATE_LINES and tier < MAX_TIER:
        tier += 1
        size_escalated = True

    top_tier_files = sorted(
        [f for f in graded if f["tier"] == base_tier],
        key=lambda f: -f["changed"],
    )
    top_lines = sum(f["changed"] for f in top_tier_files)

    reason = f"{TIER_NAMES[tier]}: {base_reason}"
    if size_escalated:
        reason += f"; escalated one tier ({total_lines} changed lines >= {SIZE_ESCALATE_LINES})"

    return {
        "schema": SCHEMA_VERSION,
        "status": "graded",
        "tier": tier,
        "label": f"risk:R{tier}",
        "reason": reason,
        "base_tier": base_tier,
        "size_escalated": size_escalated,
        "total_lines": total_lines,
        "tier_lines": tier_lines,
        "top_tier_lines": top_lines,
        "files": graded,
        "top_tier_files": [f["path"] for f in top_tier_files],
    }


def unknown_report(reason: str) -> dict:
    """The report for a PR the grader could not read.

    Published as UNKNOWN, never as R0: a silent default to the safest tier is
    exactly the stale-grade failure this check exists to avoid.
    """
    return {
        "schema": SCHEMA_VERSION,
        "status": "unknown",
        "tier": None,
        "label": None,
        "reason": reason,
        "total_lines": 0,
        "tier_lines": {},
        "files": [],
        "top_tier_files": [],
    }


def _pct(part: int, whole: int) -> int:
    return 0 if whole <= 0 else round(100 * part / whole)


def concentration_sentence(report: dict) -> str:
    """One sentence naming how much of the diff sits below the top tier."""
    total = report["total_lines"]
    if total <= 0:
        return "This diff changes no counted lines."
    base_tier = report["base_tier"]
    top_lines = report["top_tier_lines"]
    below = total - top_lines
    if base_tier == R0 or below <= 0:
        return f"All {total} changed lines are R{base_tier}."
    names = "/".join(f"R{t}" for t in range(base_tier))
    n_files = len(report["top_tier_files"])
    return (
        f"**{_pct(below, total)}% of this diff is {names}**; the "
        f"{_pct(top_lines, total)}% that makes it R{base_tier} is "
        f"{n_files} file{'s' if n_files != 1 else ''}, {top_lines} lines."
    )


DISPUTE_CHECKBOX = "**This grade is wrong**"

# Every ASCII punctuation character CommonMark lets you backslash-escape. Used
# only on the fallback path in `_md_path`, where an inline-code span cannot
# hold the text safely.
_MD_ESCAPE = str.maketrans({c: "\\" + c for c in "\\`*_{}[]()#+-.!<>|~"})


def _md_path(path: str) -> str:
    """Render a PR-controlled path safely inside a markdown table cell.

    Paths come straight from the diff and git permits `|`, backticks and
    newlines in a filename. Unescaped, such a path breaks out of its inline
    code span and out of the table row, letting a PR author inject arbitrary
    markdown into this bot-authored comment — including a checked
    `- [x] **This grade is wrong**` line that a later re-grade reads back as a
    genuine reviewer dispute, or a remote image that logs reviewer IPs.

    Newlines are flattened (nothing PR-controlled may ever start a line) and
    pipes are backslash-escaped — GFM honours `\\|` inside inline spans too. A
    path containing a backtick or a backslash falls back to fully escaped plain
    text: no inline-code span can quote a backtick reliably, and a literal
    backslash in front of a pipe (`a\\|b`) would otherwise escape the ESCAPE
    and hand the pipe back to the table splitter.
    """
    flat = path.replace("\r", " ").replace("\n", " ")
    if "`" in flat or "\\" in flat:
        return flat.translate(_MD_ESCAPE)
    return "`" + flat.replace("|", "\\|") + "`"


def _md_text(text: str) -> str:
    """Flatten and pipe-escape a reason string before rendering it.

    Tier reasons are our own constants, but an UNKNOWN report's reason carries
    git's stderr — which quotes PR-authored path names — so it is no less
    attacker-influenced than a path. Flattening newlines is the load-bearing
    part: nothing PR-controlled may ever start a line of this comment, or it
    could forge the dispute checkbox. Backslashes are doubled BEFORE pipes are
    escaped, so a literal `\\` in front of a `|` cannot escape the escape.
    """
    flat = text.replace("\r", " ").replace("\n", " ")
    return flat.replace("\\", "\\\\").replace("|", "\\|")


def render_comment(report: dict, marker: str, disputed: bool = False) -> str:
    """Render the sticky PR comment.

    `disputed` carries the current state of the reviewer's checkbox so a
    re-grade updates the body IN PLACE without silently un-ticking a
    disagreement someone already registered.
    """
    box = "x" if disputed else " "
    lines = [marker, ""]
    if report["status"] != "graded":
        lines += [
            "## ⚪ Risk: **unknown**",
            "",
            f"The risk grader could not read this diff: {_md_text(report['reason'])}",
            "",
            "No `risk:*` label was applied — an ungradable PR is published as "
            "unknown rather than defaulted to `risk:R0`. Re-run the check (or "
            "push) to try again.",
        ]
    else:
        tier = report["tier"]
        lines += [
            f"## Risk: **`risk:R{tier}`** — {TIER_NAMES[tier].split(' — ', 1)[1]}",
            "",
            _md_text(report["reason"]),
            "",
            concentration_sentence(report),
            "",
            "<details><summary>Per-file breakdown</summary>",
            "",
            "| File | +/- | Tier | Why |",
            "|---|---|---|---|",
        ]
        shown = sorted(report["files"], key=lambda f: (-f["tier"], -f["changed"]))
        for f in shown[:50]:
            lines.append(
                f"| {_md_path(f['path'])} | +{f['added']}/-{f['deleted']} | "
                f"R{f['tier']} | {_md_text(f['tier_reason'])} |"
            )
        if len(shown) > 50:
            lines.append(f"| _…and {len(shown) - 50} more files_ | | | |")
        lines += ["", "</details>"]

    lines += [
        "",
        f"- [{box}] {DISPUTE_CHECKBOX} — tick this box if the tier above is off. "
        "Nothing is gated on it either way; ticking labels the PR "
        "`risk-grade-disputed` so the grader can be tuned against real "
        "reviewer disagreement.",
        "",
        "<sub>Advisory only — this check never fails, never blocks merge, and "
        "no automation reads the label. It re-grades on every push.</sub>",
    ]
    return "\n".join(lines) + "\n"


def render_check(report: dict) -> tuple[str, str]:
    """Render the Check Run `(title, summary)` — the immutable audit artifact."""
    if report["status"] != "graded":
        return (
            "Risk: unknown",
            "The risk grader could not read this diff: "
            f"{_md_text(report['reason'])}\n\nNo tier was assigned and no "
            "`risk:*` label was applied. This check is advisory and never fails.",
        )
    tier = report["tier"]
    summary = "\n".join(
        [
            f"**Tier: `risk:R{tier}`** ({TIER_NAMES[tier]})",
            "",
            f"Reason: {_md_text(report['reason'])}",
            "",
            concentration_sentence(report),
            "",
            f"Counted lines: {report['total_lines']} across "
            f"{len(report['files'])} file(s).",
            "",
            "Per-tier line counts: "
            + ", ".join(
                f"R{t}={report['tier_lines'].get(str(t), 0)}"
                for t in range(MAX_TIER + 1)
            ),
            "",
            "This check is advisory: it never fails, never blocks merge, and "
            "no automation consumes the tier.",
        ]
    )
    return f"Risk: R{tier}", summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Grade a PR diff into a risk tier.")
    ap.add_argument(
        "--numstat",
        default="-",
        help="File holding `git diff --numstat -z` output ('-' = stdin).",
    )
    ap.add_argument(
        "--out-dir", required=True, help="Directory to write the report artifacts to."
    )
    ap.add_argument(
        "--marker",
        default=DEFAULT_MARKER,
        help="Hidden marker that makes the PR comment sticky.",
    )
    args = ap.parse_args(argv)

    if args.numstat == "-":
        data = sys.stdin.read()
    else:
        with open(args.numstat, encoding="utf-8", errors="replace") as fh:
            data = fh.read()

    try:
        report = grade(parse_numstat(data))
    except Exception as exc:  # noqa: BLE001 - any parse failure is "unknown", not a crash
        report = unknown_report(f"could not parse the diff ({type(exc).__name__}: {exc})")

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "risk-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(args.out_dir, "risk-comment.md"), "w", encoding="utf-8") as fh:
        fh.write(render_comment(report, args.marker))
    title, summary = render_check(report)
    with open(os.path.join(args.out_dir, "risk-check.md"), "w", encoding="utf-8") as fh:
        fh.write(f"{title}\n\n{summary}\n")

    # Human-readable echo for the job log / step summary.
    print(f"{title}\n\n{summary}")
    # Always 0: this grader is advisory and must never redden a check.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
