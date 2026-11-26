#!/usr/bin/env python3
"""Side-effecting orchestration behind telemetry-cost-contract.yml.

Imports lib.py for every decision and owns only I/O: resolve the PR from the workflow_run
event, refetch it, skip unprotected base branches, read its changed files WITH diff patches
(GitHub-owned metadata — no checkout of the PR branch, no secrets), recover any file whose
`patch` GitHub omitted from the whole-PR unified diff, apply the detection patterns, require
a `## Cost impact` PR-body section (or the exempt label) when a match is found, publish the
`Telemetry cost contract` commit status, and maintain one marker comment.

Two non-success states, and the distinction is load-bearing: `failure` means the contract
was broken, `error` means this gate could not reach a verdict. See lib._FAILING_STATE.

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


class _AlreadyMerged:
    """Sentinel type for _resolve_pr: the commit's only associated PR merged in the window
    between the signal workflow uploading and this workflow_run executing. The verdict is
    moot post-merge (nothing left to publish a status against that could still block
    anything), so this is distinct from genuine ambiguity, which still fails closed."""


_ALREADY_MERGED = _AlreadyMerged()


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(f"::error::{msg}", file=sys.stderr, flush=True)


def warning(msg: str) -> None:
    print(f"::warning::{msg}", file=sys.stderr, flush=True)


def log_safe(text: str) -> str:
    """Render PR-controlled text safe to pass to ``log``/``warning``/``error``.

    A workflow command is newline-delimited, so a filename is a command-injection vector in
    this privileged, fork-triggerable ``workflow_run`` job: git permits ``\\n`` in a path, and
    a file named ``x\\n::stop-commands::tok`` embedded in a ``::warning::`` line starts a NEW
    runner command — enough to forge annotations, register a mask, or suppress the later
    ``::error::`` that reports the real verdict. ``repr`` escapes the newline (and any other
    control character) while staying readable, and ``%`` is percent-encoded because the runner
    decodes ``%0A``/``%0D`` in command data. The summary/comment path has always been safe via
    ``md_code``; this is the equivalent for the log path.
    """
    return repr(text).replace("%", "%25")


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

    def _run_bytes(self, args: list[str]) -> subprocess.CompletedProcess:
        """``gh api`` with the payload captured as RAW BYTES.

        ``text=True`` is wrong for a response that is repository content rather than JSON,
        in two independent ways, and ``_run`` must keep it for every JSON call:

        * **strict UTF-8 decode.** One non-UTF-8 text file anywhere in the PR (a Latin-1 or
          CP-1252 fixture, no NUL so git treats it as text) makes the implicit decode raise
          ``UnicodeDecodeError``. Nothing upstream catches it, so the validator would die
          AFTER publishing `pending` and leave that non-terminal status as the PR's last
          word — strictly worse than the bug this fix exists to remove.
        * **universal-newline translation.** It rewrites a lone ``\\r`` inside file content
          into a real ``\\n``. That silently splits one added line into two, and the second
          half loses its ``+`` prefix so ``added_lines`` drops it — a detection bypass in
          exactly the oversized file this recovery path exists to scan. It also lets file
          content forge a line-start ``diff --git``/``+++``/``@@`` section (caught
          independently by ``split_unified_diff``'s duplicate-key refusal).
        """
        return subprocess.run(["gh", "api", *args], capture_output=True)

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

    def pull_request_diff(self, pr: int) -> str | None:
        """The PR's whole-PR unified diff, or ``None`` if GitHub would not render one.

        Same trust class as the per-file ``patch`` fragments and read for the same reason:
        GitHub-owned metadata, no checkout of the PR branch, no secret, and the text is only
        ever pattern-MATCHED downstream. The reason this endpoint is worth a second request
        is purely its ceiling — ``pulls/{n}/files`` drops ``patch`` per file far earlier than
        ``pulls/{n}`` drops the whole diff, so the file this gate most needs to read (a large
        generated client, a big JSON fixture) is typically absent from the first and present
        in the second.

        GitHub answers 406 when even this is too large to generate, which surfaces here as a
        nonzero ``gh`` exit and a ``None`` — the caller then reports a genuine cannot-verify.

        Read as bytes and decoded here with ``errors="replace"`` and newline translation
        OFF; see ``_run_bytes`` for why text mode would be a crash and a detection bypass.
        A replacement character can only ever make a pattern fail to match, never match
        something that is not there, and an undecodable file still cannot hide a call site:
        the added-line coverage check in ``_recover_omitted_patches`` leaves any file whose
        recovered fragment does not account for all its additions unscannable.
        """
        result = self._run_bytes([f"/repos/{self.repo}/pulls/{pr}",
                                  "-H", "Accept: application/vnd.github.diff"])
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
            log(f"whole-PR diff for #{pr} is unavailable: {stderr}")
            return None
        return (result.stdout or b"").decode("utf-8", "replace") or None

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

    def current_pr_target(self, pr: int) -> tuple[str, str, str, frozenset[str]] | None:
        """The PR's live head/base/body/labels, or ``None`` if the refetch failed or came
        back without a head or base. Every field the supersession guard compares must be
        refetched together: the body and labels decide this gate's verdict just as much as
        the SHA does, so a stale read of either lets a newer run's result be overwritten."""
        data = self.get(f"/repos/{self.repo}/pulls/{pr}")
        if not isinstance(data, dict):
            return None
        sha = data.get("head", {}).get("sha")
        base = data.get("base", {}).get("ref")
        if not sha or not base:
            return None
        body = data.get("body") or ""
        labels = frozenset(
            lbl.get("name") for lbl in (data.get("labels") or []) if lbl.get("name"))
        return (sha, base, body, labels)

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
        self.validated_body: str | None = None
        self.validated_labels: frozenset[str] = frozenset()

    def _guard_supersession(self) -> bool:
        """Whether this run is still the latest word on the PR. Everything this gate's
        verdict can depend on — head SHA, base, body (the ``## Cost impact`` section) and
        labels (the exempt label) — is refetched and compared; a mismatch on any of them, or
        a failed refetch, means a newer run already has (or will) read fresher state, so this
        run fails closed and skips the terminal write rather than risk overwriting it."""
        current = self.gh.current_pr_target(self.pr_number)
        if current is None:
            log("Could not refetch the PR to confirm this run is still current; failing "
                "closed and not writing a terminal status.")
            return False
        now_sha, now_base, now_body, now_labels = current
        if now_sha != self.validated_sha:
            log(f"PR head moved {self.validated_sha} -> {now_sha}; a newer run supersedes "
                "this one. Not writing a terminal status.")
            return False
        if now_base != self.validated_base_branch:
            log(f"PR base moved {self.validated_base_branch} -> {now_base}; a newer run "
                "supersedes this one. Not writing a terminal status.")
            return False
        if now_body != self.validated_body:
            log("PR body changed since this run started; a newer run supersedes this one. "
                "Not writing a terminal status.")
            return False
        if now_labels != self.validated_labels:
            log("PR labels changed since this run started; a newer run supersedes this one. "
                "Not writing a terminal status.")
            return False
        return True

    def finish_pass(self, reason: str, status_desc: str) -> int:
        if not self._guard_supersession():
            return 0
        summary("## telemetry-cost-contract: ✅ pass")
        summary("")
        summary(reason)
        self.gh.delete_marker_comment(self.pr_number)
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

    def _fail_closed_pre_pr(self, sha: str, message: str, *, log_message: bool = True) -> int:
        """Fail closed before ``self.validated_sha`` exists: PR resolution, PR fetch, or the
        base-branch checks failed. ``sha`` is the triggering commit from the workflow_run
        event, known from the very first line of ``run``, so the status can still be keyed
        correctly. Without this write, a transient failure here on a PR whose SHA already
        carries an earlier run's `success` (because the body or labels — not the SHA —
        changed since, e.g. a cost-impact section was removed) would leave that stale
        `success` as the terminal status.

        Published as `error`, not `failure`, for the same reason `changed_files_unavailable`
        is: every condition reaching here — PR fetch failure, ambiguous PR resolution,
        unreadable branch protection, a non-`pull_request` trigger — is this gate unable to
        reach a verdict, never a finding about the PR's cost impact. Leaving these as
        `failure` would have contradicted the contract this module's docstring states.
        """
        if log_message:
            error(message)
        self.gh.publish_status(sha, "error", message[:140], self.run_url, critical=True)
        return 1

    def finish_fail(self, category: str, detail: str) -> int:
        guidance = lib.failure_guidance(category, self.exempt_label)
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
        footer = (f"After updating the PR body, re-run this check from the "
                  f"[workflow run]({self.run_url}) or edit the PR to trigger a fresh run.")
        if self.exempt_label:
            footer += (f" A repository maintainer can waive the requirement by applying "
                       f"the `{self.exempt_label}` label.")
        body_lines += ["", "---", footer]

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

    def _recover_omitted_patches(self, files: list[dict], unscannable: list[str]) -> list[str]:
        """Splice whole-PR-diff hunks into the entries whose API ``patch`` GitHub omitted, and
        return the names still unreadable afterwards.

        This is the difference between a gate that checks and a gate that reports it could not
        look. ``pulls/{n}/files`` drops ``patch`` past a per-file render ceiling and does not
        flag the omission, so a PR touching one large generated file — this repo family's
        `*.gen.go` clients and `object_info.json` fixtures — used to take the whole gate out
        even when every genuinely interesting file in the same PR was readable.

        Mutates ``files`` in place so the already-tested ``first_match`` consumes recovered
        fragments by exactly the same path as API-supplied ones; the fragments are shaped
        identically by ``lib.split_unified_diff``, so no detection code changes.

        The recovery is NEVER silent, in either direction. A degradation that only showed up
        as a red check nobody read is how this defect survived, so a successful recovery logs
        a ``::warning::`` naming the files and the summary records it, and a partial recovery
        says which files are still unreadable rather than quietly scanning the rest.

        A recovered fragment is accepted ONLY when its added-line count equals the entry's
        ``additions``, which the changed-files API reported independently of the diff text.
        That keeps this path inside the module's existing fail-closed invariant — the same
        one behind ``CHANGED_FILES_CAP`` and the ``changed_files`` count comparison — so a
        truncated section, a mis-split one, or a file mangled by an undecodable byte stays
        unscannable instead of being treated as a complete scan. Verified against 404 real
        file/source pairs: the two counts agree exactly whenever the fragment is intact.
        """
        wanted = set(unscannable)
        diff_text = self.gh.pull_request_diff(self.pr_number)
        if not diff_text:
            warning(
                f"{len(unscannable)} changed file(s) have no API diff patch and the whole-PR "
                "unified diff could not be read either, so their content was never scanned: "
                + ", ".join(log_safe(n) for n in sorted(wanted)))
            return sorted(wanted)

        fragments = lib.split_unified_diff(diff_text)
        recovered: list[str] = []
        for entry in files:
            name = entry.get("filename")
            if name not in wanted:
                continue
            fragment = fragments.get(name)
            if not fragment:
                continue
            declared = entry.get("additions") or 0
            found = len(lib.added_lines(fragment))
            if found != declared:
                warning(
                    f"the whole-PR diff section for {log_safe(name)} accounts for {found} "
                    f"added line(s) but the changed-files API reports {declared}; treating "
                    "the file as unread rather than scanning a partial section.")
                continue
            entry["patch"] = fragment
            recovered.append(name)

        still = sorted(wanted - set(recovered))
        if recovered:
            warning(
                f"{len(recovered)} changed file(s) had no diff patch from the changed-files "
                "API (GitHub omits `patch` past its per-file render ceiling) and were scanned "
                "from the whole-PR unified diff instead: "
                + ", ".join(log_safe(n) for n in sorted(recovered)))
            summary(
                f"> ℹ️ Recovered the diff of {len(recovered)} file(s) from the whole-PR "
                "unified diff after the changed-files API omitted their `patch`: "
                + ", ".join(md_code(n) for n in sorted(recovered)) + ".")
        if still:
            warning(f"{len(still)} changed file(s) remain unreadable after the whole-PR diff "
                    "fallback: " + ", ".join(log_safe(n) for n in still))
        return still

    def run(self, event: dict) -> int:
        wr = event.get("workflow_run") or {}
        head_sha = wr.get("head_sha") or ""
        if not head_sha:
            error("workflow_run.head_sha missing from event")
            return 1

        wr_event = wr.get("event") or ""
        if wr_event != "pull_request":
            return self._fail_closed_pre_pr(
                head_sha,
                f"Triggering workflow_run event was '{wr_event}', not 'pull_request'. The "
                "signal workflow must run on pull_request; refusing to validate.")

        resolved = self._resolve_pr(event, head_sha)
        if isinstance(resolved, _AlreadyMerged):
            log(f"The PR for {head_sha} merged before this validate run could execute; the "
                "verdict is moot post-merge. Skipping without publishing a status.")
            return 0
        if resolved is None:
            return self._fail_closed_pre_pr(
                head_sha, "Could not resolve exactly one open PR for this commit; refusing "
                "to publish an ambiguous result.", log_message=False)
        self.pr_number = resolved

        pr = self.gh.get(f"/repos/{self.gh.repo}/pulls/{self.pr_number}")
        if not pr:
            return self._fail_closed_pre_pr(
                head_sha, f"Could not fetch PR #{self.pr_number}")
        self.validated_sha = pr.get("head", {}).get("sha")
        base_branch = pr.get("base", {}).get("ref") or ""
        self.validated_base_branch = base_branch
        self.validated_body = pr.get("body") or ""
        self.validated_labels = frozenset(
            lbl.get("name") for lbl in (pr.get("labels") or []) if lbl.get("name"))

        log(f"Validating PR #{self.pr_number} at head {self.validated_sha}")
        if not base_branch:
            return self._fail_closed_pre_pr(
                head_sha, f"PR #{self.pr_number} has no base branch; refusing to validate.")
        protected = self.gh.branch_is_protected(base_branch)
        if protected is None:
            return self._fail_closed_pre_pr(
                head_sha, f"Could not determine whether base branch '{base_branch}' is "
                "protected; failing closed.")
        if not protected:
            log(f"PR targets unprotected branch '{base_branch}' — contract not required.")
            return self.finish_not_required(base_branch)

        if not self.gh.publish_status(self.validated_sha, "pending",
                                      "Scanning diff for cost-sensitive call sites…",
                                      self.run_url, critical=True):
            return 1

        if self.exempt_label and self.exempt_label in self.validated_labels:
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
            unscannable = self._recover_omitted_patches(files, unscannable)
        if unscannable:
            error(f"{len(unscannable)} changed file(s) have added content but no readable "
                  "diff content in EITHER the changed-files API or the whole-PR unified "
                  "diff; cannot rule out a cost-sensitive call site. Reporting "
                  "could-not-verify (status state `error`, not `failure`).")
            return self.finish_fail(
                "changed_files_unavailable",
                f"Unreadable file(s): {', '.join(md_code(n) for n in unscannable[:10])}"
                + ("…" if len(unscannable) > 10 else "") + ".")

        match = lib.first_match(files, self.patterns)
        if match is None:
            log(f"No cost-sensitive pattern matched across {len(files)} changed file(s) — pass.")
            return self.finish_pass(
                "No added line matched this repository's cost-sensitive detection patterns.",
                "No new cost-sensitive telemetry call site detected")

        filename, pattern_text = match
        log(f"Cost-sensitive pattern matched in {filename!r} (pattern: {pattern_text!r})")
        if lib.has_cost_impact_section(self.validated_body):
            return self.finish_pass(
                f"A new telemetry call site was detected (matched `{pattern_text}` in "
                f"{md_code(filename)}), and the PR body carries a `## Cost impact` section.",
                "Cost impact section present")

        return self.finish_fail(
            "missing_cost_impact",
            f"Matched pattern `{pattern_text}` against an added line in {md_code(filename)}.")

    def _resolve_pr(self, event: dict, head_sha: str) -> int | _AlreadyMerged | None:
        """Exactly one open PR, ``_ALREADY_MERGED`` if the commit's one associated PR merged
        before this run could execute, or ``None`` if genuinely ambiguous (zero or multiple
        candidates with none merged). Same-repo runs carry workflow_run.pull_requests; fork
        runs do not, so fall back to the commit->PR association (GitHub-owned data either
        way)."""
        wr = event.get("workflow_run") or {}
        candidates = [pr.get("number") for pr in (wr.get("pull_requests") or []) if pr.get("number")]
        if not candidates:
            assoc = self.gh.get(f"/repos/{self.gh.repo}/commits/{head_sha}/pulls") or []
            candidates = [
                pr.get("number") for pr in assoc
                if (pr.get("base") or {}).get("repo", {}).get("full_name") == self.gh.repo
            ]

        open_prs: list[int] = []
        merged_prs: list[int] = []
        lookup_failed = False
        for number in dict.fromkeys(candidates):
            data = self.gh.get(f"/repos/{self.gh.repo}/pulls/{number}")
            if not data:
                lookup_failed = True
                continue
            if data.get("state") == "open":
                open_prs.append(number)
            elif data.get("merged_at"):
                merged_prs.append(number)

        if len(open_prs) == 1:
            return open_prs[0]

        if not lookup_failed and not open_prs and len(merged_prs) == 1:
            log(f"PR #{merged_prs[0]} associated with {head_sha} already merged before this "
                "validate run executed.")
            return _ALREADY_MERGED

        error(f"Expected exactly one open PR associated with {head_sha}, found "
              f"{len(open_prs)} (event={wr.get('event')}). Refusing to publish an "
              "ambiguous result.")
        return None


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

    enforce = lib.enforce_enabled(os.environ.get("ENFORCE"))
    soft_fail = lib.soft_fail_enabled(os.environ.get("SOFT_FAIL"))
    run_url = os.environ.get("RUN_URL", "")
    exempt_label = os.environ.get("EXEMPT_LABEL", "")

    validator = Validator(GitHub(repo), patterns, enforce, soft_fail, run_url, exempt_label)
    return validator.run(event)


if __name__ == "__main__":
    sys.exit(main())
