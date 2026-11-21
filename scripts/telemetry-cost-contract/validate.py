#!/usr/bin/env python3
"""Side-effecting orchestration behind telemetry-cost-contract.yml.

Imports lib.py for every decision and owns only I/O: resolve the PR from the workflow_run
event, refetch it, skip unprotected base branches, read its changed files WITH diff patches
(GitHub-owned metadata — no checkout of the PR branch, no secrets), apply the detection
patterns, require a `## Cost impact` PR-body section (or the exempt label) when a match is
found, publish the `Telemetry cost contract` commit status, and maintain one marker comment.

Every value derived from the PR (branch, title, body, labels, diff patch text) is untrusted
DATA: it is only ever pattern-MATCHED, never shell-interpolated or executed. This mirrors
linear-ticket.yml's fork-safety contract — see that workflow's header for the full rationale
on why this script runs in the PRIVILEGED `workflow_run` job rather than on the PR itself.

Contract (all via env, set by telemetry-cost-contract.yml):
    GH_TOKEN            caller GITHUB_TOKEN, for ``gh api`` (statuses:write, pull-requests:write)
    GH_REPO            owner/repo (github.repository)
    GITHUB_EVENT_PATH  workflow_run event payload
    PATTERNS           newline-separated regexes; a match against an added diff line marks
                       the PR cost-sensitive. Required; see lib.parse_patterns.
    EXEMPT_LABEL       exemption label name; empty disables exemption (default: cost-exempt,
                       set by the workflow, not this script)
    ENFORCE            "true" (fail closed) / "false" (warn-only)
    SOFT_FAIL          warn-only only: "true" publishes a red (non-blocking) status; "false"
                       or ABSENT keeps the silent always-green warn-only behaviour
    RUN_URL            html_url of this workflow run, for the status target and comment
    GITHUB_STEP_SUMMARY  written with the human-readable outcome

Standard library only; GitHub via the ``gh`` CLI, matching linear-ticket/validate.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse

import lib

# GitHub's documented ceiling on GET /repos/{owner}/{repo}/pulls/{number}/files: at most 3000
# entries, truncated SILENTLY. A short read can hide the one file that added a billable call
# site, so a PR at or past the cap fails closed rather than scanning a partial diff.
CHANGED_FILES_CAP = 3000


class ChangedFilesUnavailable(Exception):
    """The PR's changed-file list (with patches) could not be read, or could not be trusted
    to be complete. Always fails the check closed (``changed_files_unavailable``)."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(f"::error::{msg}", file=sys.stderr, flush=True)


def warning(msg: str) -> None:
    print(f"::warning::{msg}", file=sys.stderr, flush=True)


def md_code(text: str) -> str:
    """Render ``text`` as a Markdown inline code span, safe even if it contains a backtick or
    newline — either would otherwise let PR-controlled data (a filename) break out of the
    span into arbitrary Markdown, links or @-mentions in the marker comment / job summary."""
    return "`" + text.replace("`", "'").replace("\n", " ").replace("\r", " ") + "`"


def summary(msg: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(msg + "\n")


# ── GitHub via gh ───────────────────────────────────────────────────────────────────────
class GitHub:
    def __init__(self, repo: str):
        self.repo = repo

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["gh", "api", *args], input=stdin, text=True, capture_output=True,
        )

    def get(self, path: str, *, paginate: bool = False):
        args = [path]
        if paginate:
            args.insert(0, "--paginate")
        result = self._run(args)
        if result.returncode != 0:
            log(f"gh api {path} failed: {result.stderr.strip()}")
            return None
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError:
            return None

    def publish_status(self, sha: str, state: str, description: str, target_url: str,
                       *, critical: bool = False) -> bool:
        """Publish the commit status. A TERMINAL write is critical=True: if it fails, the old
        status stays as the required context, matching linear-ticket's identical write."""
        result = self._run([
            "--method", "POST", f"/repos/{self.repo}/statuses/{sha}",
            "-f", f"state={state}",
            "-f", f"context={lib.CONTEXT}",
            "-f", f"description={description[:140]}",
            "-f", f"target_url={target_url}",
        ])
        if result.returncode != 0:
            emit = error if critical else warning
            emit(f"Failed to publish '{state}' status on {sha}: {result.stderr.strip()}")
            return False
        return True

    def find_marker_comment(self, pr: int) -> int | None:
        comments = self.get(f"/repos/{self.repo}/issues/{pr}/comments?per_page=100",
                            paginate=True) or []
        for comment in comments:
            if lib.MARKER not in (comment.get("body") or ""):
                continue
            user = comment.get("user") or {}
            if user.get("type") == "Bot" and user.get("login") == "github-actions[bot]":
                return comment.get("id")
        return None

    def upsert_marker_comment(self, pr: int, body: str) -> None:
        existing = self.find_marker_comment(pr)
        if existing is not None:
            result = self._run(
                ["--method", "PATCH", f"/repos/{self.repo}/issues/comments/{existing}",
                 "-F", "body=@-"], stdin=body)
            if result.returncode != 0:
                warning(f"Failed to update marker comment {existing}: {result.stderr.strip()}")
        else:
            result = self._run(
                ["--method", "POST", f"/repos/{self.repo}/issues/{pr}/comments",
                 "-F", "body=@-"], stdin=body)
            if result.returncode != 0:
                warning(f"Failed to create marker comment: {result.stderr.strip()}")

    def delete_marker_comment(self, pr: int) -> None:
        existing = self.find_marker_comment(pr)
        if existing is None:
            return
        result = self._run(["--method", "DELETE", f"/repos/{self.repo}/issues/comments/{existing}"])
        if result.returncode != 0:
            warning(f"Failed to delete marker comment {existing}: {result.stderr.strip()}")

    def changed_files(self, pr: int, declared_count: int | None = None) -> list[dict]:
        """Every changed-file entry (filename + patch, if GitHub rendered one), as untrusted
        DATA. Paginated at 100/page. Raises ChangedFilesUnavailable — never returns a partial
        list — on the same conditions linear-ticket's identical method does: request failure,
        at/past CHANGED_FILES_CAP, or disagreement with the PR's own ``changed_files`` count.
        """
        if isinstance(declared_count, int) and declared_count >= CHANGED_FILES_CAP:
            raise ChangedFilesUnavailable(
                f"the PR reports {declared_count} changed files, at or past GitHub's "
                f"{CHANGED_FILES_CAP}-file ceiling for the changed-files API, which truncates "
                "silently")
        data = self.get(f"/repos/{self.repo}/pulls/{pr}/files?per_page=100", paginate=True)
        if not isinstance(data, list):
            raise ChangedFilesUnavailable(
                "the changed-files API request failed or returned a non-list body")
        if len(data) >= CHANGED_FILES_CAP:
            raise ChangedFilesUnavailable(
                f"the changed-files API returned {len(data)} entries, at or past its "
                f"{CHANGED_FILES_CAP}-file ceiling, so the list may be truncated")
        if isinstance(declared_count, int) and len(data) != declared_count:
            raise ChangedFilesUnavailable(
                f"the PR reports {declared_count} changed files but the changed-files API "
                f"returned {len(data)} entries; the list does not match the PR")
        entries: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("filename"):
                raise ChangedFilesUnavailable(
                    "a changed-file entry was malformed (not an object, or no filename)")
            entries.append(entry)
        return entries

    def current_pr_target(self, pr: int) -> tuple[str | None, str | None]:
        data = self.get(f"/repos/{self.repo}/pulls/{pr}")
        return (
            (data or {}).get("head", {}).get("sha"),
            (data or {}).get("base", {}).get("ref"),
        )

    def branch_is_protected(self, branch: str) -> bool | None:
        encoded = urllib.parse.quote(branch, safe="")
        data = self.get(f"/repos/{self.repo}/branches/{encoded}")
        protected = (data or {}).get("protected")
        return protected if isinstance(protected, bool) else None


# ── orchestration ───────────────────────────────────────────────────────────────────────
class Validator:
    def __init__(self, gh: GitHub, patterns, enforce: bool, soft_fail: bool, run_url: str,
                 exempt_label: str):
        self.gh = gh
        self.patterns = patterns
        self.enforce = enforce
        self.soft_fail = soft_fail
        self.run_url = run_url
        self.exempt_label = exempt_label
        self.pr_number: int | None = None
        self.validated_sha: str | None = None
        self.validated_base_branch: str | None = None

    def _guard_supersession(self) -> bool:
        now_sha, now_base = self.gh.current_pr_target(self.pr_number)
        if now_sha and now_sha != self.validated_sha:
            log(f"PR head moved {self.validated_sha} -> {now_sha}; a newer run supersedes "
                "this one. Not writing a terminal status.")
            return False
        if now_base and now_base != self.validated_base_branch:
            log(f"PR base moved {self.validated_base_branch} -> {now_base}; a newer run "
                "supersedes this one. Not writing a terminal status.")
            return False
        return True

    def finish_pass(self, reason: str, status_desc: str) -> int:
        summary("## telemetry-cost-contract: ✅ pass")
        summary("")
        summary(reason)
        self.gh.delete_marker_comment(self.pr_number)
        if self._guard_supersession():
            if not self.gh.publish_status(self.validated_sha, "success", status_desc,
                                          self.run_url, critical=True):
                return 1
        return 0

    def finish_not_required(self, base_branch: str) -> int:
        if not self._guard_supersession():
            return 0
        summary("## telemetry-cost-contract: not required")
        summary("")
        summary(f"PR targets the unprotected `{base_branch}` branch.")
        self.gh.delete_marker_comment(self.pr_number)
        return 0

    def finish_fail(self, category: str, detail: str) -> int:
        guidance = lib.failure_guidance(category)
        outcome = lib.failure_outcome(category, self.enforce, self.soft_fail)

        body_lines = [
            lib.MARKER, "",
            f"### Telemetry cost contract check — {outcome.verdict}", "",
            guidance,
        ]
        if detail:
            body_lines += ["", detail]
        if outcome.advisory:
            body_lines += ["", lib.advisory_note(category)]
        body_lines += [
            "", "---",
            (f"After updating the PR body, re-run this check from the "
             f"[workflow run]({self.run_url}) or edit the PR to trigger a fresh run. A "
             f"repository maintainer can waive the requirement by applying the "
             f"`{self.exempt_label or 'cost-exempt'}` label."),
        ]

        if not self._guard_supersession():
            return outcome.exit_code

        self.gh.upsert_marker_comment(self.pr_number, "\n".join(body_lines))

        summary(f"## telemetry-cost-contract: {outcome.verdict}")
        summary("")
        summary(guidance)
        if detail:
            summary(detail)

        if not self.gh.publish_status(self.validated_sha, outcome.state, outcome.description,
                                      self.run_url, critical=True):
            return 1
        return outcome.exit_code

    def run(self, event: dict) -> int:
        wr = event.get("workflow_run") or {}
        head_sha = wr.get("head_sha") or ""
        if not head_sha:
            error("workflow_run.head_sha missing from event")
            return 1

        wr_event = wr.get("event") or ""
        if wr_event != "pull_request":
            error(f"Triggering workflow_run event was '{wr_event}', not 'pull_request'. The "
                  "signal workflow must run on pull_request; refusing to validate.")
            return 1

        self.pr_number = self._resolve_pr(event, head_sha)
        if self.pr_number is None:
            return 1

        pr = self.gh.get(f"/repos/{self.gh.repo}/pulls/{self.pr_number}")
        if not pr:
            error(f"Could not fetch PR #{self.pr_number}")
            return 1
        self.validated_sha = pr.get("head", {}).get("sha")
        base_branch = pr.get("base", {}).get("ref") or ""
        self.validated_base_branch = base_branch
        body = pr.get("body") or ""
        labels = [lbl.get("name") for lbl in (pr.get("labels") or [])]

        log(f"Validating PR #{self.pr_number} at head {self.validated_sha}")
        if not base_branch:
            error(f"PR #{self.pr_number} has no base branch; refusing to validate.")
            return 1
        protected = self.gh.branch_is_protected(base_branch)
        if protected is None:
            error(f"Could not determine whether base branch '{base_branch}' is protected; "
                  "failing closed.")
            return 1
        if not protected:
            log(f"PR targets unprotected branch '{base_branch}' — contract not required.")
            return self.finish_not_required(base_branch)

        self.gh.publish_status(self.validated_sha, "pending",
                               "Scanning diff for cost-sensitive call sites…", self.run_url)

        if self.exempt_label and self.exempt_label in labels:
            log(f"PR carries the '{self.exempt_label}' label — exempt.")
            return self.finish_pass(
                f"PR carries the `{self.exempt_label}` label — the cost-contract requirement "
                "is waived.",
                f"Exempt via {self.exempt_label} label")

        try:
            files = self.gh.changed_files(self.pr_number, pr.get("changed_files"))
        except ChangedFilesUnavailable as exc:
            error(f"Could not read the changed-file list for PR #{self.pr_number}: {exc}. "
                  "Failing closed.")
            return self.finish_fail("changed_files_unavailable", f"Changed-file lookup: {exc}.")

        unscannable = lib.unscannable_files(files)
        if unscannable:
            error(f"{len(unscannable)} changed file(s) have added content but no renderable "
                  "diff patch (GitHub omits `patch` past its render ceiling); cannot rule "
                  "out a cost-sensitive call site. Failing closed.")
            return self.finish_fail(
                "changed_files_unavailable",
                f"Unscannable file(s): {', '.join(md_code(n) for n in unscannable[:10])}"
                + ("…" if len(unscannable) > 10 else "") + ".")

        match = lib.first_match(files, self.patterns)
        if match is None:
            log(f"No cost-sensitive pattern matched across {len(files)} changed file(s) — pass.")
            return self.finish_pass(
                "No added line matched this repository's cost-sensitive detection patterns.",
                "No new cost-sensitive telemetry call site detected")

        filename, pattern_text = match
        log(f"Cost-sensitive pattern matched in {filename!r} (pattern: {pattern_text!r})")
        if lib.has_cost_impact_section(body):
            return self.finish_pass(
                f"A new telemetry call site was detected (matched `{pattern_text}` in "
                f"{md_code(filename)}), and the PR body carries a `## Cost impact` section.",
                "Cost impact section present")

        return self.finish_fail(
            "missing_cost_impact",
            f"Matched pattern `{pattern_text}` against an added line in {md_code(filename)}.")

    def _resolve_pr(self, event: dict, head_sha: str) -> int | None:
        """Exactly one open PR. Same-repo runs carry workflow_run.pull_requests; fork runs do
        not, so fall back to the commit->PR association (GitHub-owned data either way)."""
        wr = event.get("workflow_run") or {}
        candidates = [pr.get("number") for pr in (wr.get("pull_requests") or []) if pr.get("number")]
        if not candidates:
            assoc = self.gh.get(f"/repos/{self.gh.repo}/commits/{head_sha}/pulls") or []
            candidates = [
                pr.get("number") for pr in assoc
                if pr.get("state") == "open"
                and (pr.get("base") or {}).get("repo", {}).get("full_name") == self.gh.repo
            ]

        open_prs: list[int] = []
        for number in dict.fromkeys(candidates):
            data = self.gh.get(f"/repos/{self.gh.repo}/pulls/{number}")
            if data and data.get("state") == "open":
                open_prs.append(number)

        if len(open_prs) != 1:
            error(f"Expected exactly one open PR associated with {head_sha}, found "
                  f"{len(open_prs)} (event={wr.get('event')}). Refusing to publish an "
                  "ambiguous result.")
            return None
        return open_prs[0]


def main() -> int:
    repo = os.environ.get("GH_REPO")
    event_path = os.environ.get("GITHUB_EVENT_PATH")

    if not repo:
        error("GH_REPO is required")
        return 1
    if not event_path or not os.path.isfile(event_path):
        error("GITHUB_EVENT_PATH is required and must point at the event payload")
        return 1

    try:
        patterns = lib.parse_patterns(os.environ.get("PATTERNS", ""))
    except ValueError as exc:
        error(f"Invalid patterns input: {exc}. Provide one regex per line (blank lines and "
              "'#' comments are skipped).")
        return 1

    with open(event_path, encoding="utf-8") as handle:
        event = json.load(handle)

    enforce = os.environ.get("ENFORCE", "true") != "false"
    soft_fail = lib.soft_fail_enabled(os.environ.get("SOFT_FAIL"))
    run_url = os.environ.get("RUN_URL", "")
    exempt_label = os.environ.get("EXEMPT_LABEL", "")

    validator = Validator(GitHub(repo), patterns, enforce, soft_fail, run_url, exempt_label)
    return validator.run(event)


if __name__ == "__main__":
    sys.exit(main())
