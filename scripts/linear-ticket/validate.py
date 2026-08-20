#!/usr/bin/env python3
"""Side-effecting orchestration behind linear-ticket.yml.

Imports lib.py for every decision and owns only I/O: resolve the PR from the workflow_run
event, refetch it, publish the ``Linear ticket`` commit status, query Linear's
attachmentsForURL for the PR's canonical html_url, apply the policy gate, run one batched
diagnostic query, and maintain exactly one marker PR comment.

It runs in the PRIVILEGED workflow_run job, so every value derived from the PR (branch,
title, body, labels, URL) is untrusted DATA: it is passed to Linear through GraphQL
variables and to GitHub through ``gh api`` argument lists / stdin, never interpolated into a
query. No PR code is checked out or executed.

Contract (all via env, set by linear-ticket.yml):
    GH_TOKEN            caller GITHUB_TOKEN, for ``gh api`` (statuses:write, pull-requests:write)
    GH_REPO            owner/repo (github.repository)
    LINEAR_API_TOKEN   value placed verbatim into Linear's Authorization header
    GITHUB_EVENT_PATH  workflow_run event payload
    TEAM_KEYS          raw ``team-keys`` input (comma-separated; empty = any team)
    EXEMPT_LABEL       exemption label name; empty disables exemption
    REQUIRE_OPEN_ISSUE "true"/"false"
    ENFORCE            "true" (fail closed) / "false" (warn-only: a failing VERDICT never
                       exits nonzero; a broken run still can — see finish_fail and run())
    SOFT_FAIL          warn-only only: "true" publishes a red `failure` status — loud, and
                       non-blocking for as long as the caller's ruleset does not require the
                       `Linear ticket` context; "false" (and, deliberately, an ABSENT value)
                       keeps the silent always-green warn-only behaviour
    RUN_URL            html_url of this workflow run, for the status target and comment
    LINEAR_API_URL     optional override (default https://api.linear.app/graphql)
    GITHUB_STEP_SUMMARY  written with the human-readable outcome

Standard library only; GitHub via the ``gh`` CLI (as cursor-review's post-review.py does),
Linear via urllib (as refresh-reviewers' generate.py does).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import lib

LINEAR_API_URL = os.environ.get("LINEAR_API_URL") or "https://api.linear.app/graphql"
BACKOFF_SECONDS = (2, 4, 8, 16)  # between five attempts

ATTACHMENTS_QUERY = """query PullRequestAttachments($url: String!) {
  attachmentsForURL(url: $url, first: 20) {
    nodes { id url issue { id identifier team { key } state { type } } }
  }
}"""


# ── logging (stderr; ::error::/::warning:: are GitHub Actions annotations) ──────────────
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(f"::error::{msg}", file=sys.stderr, flush=True)


def warning(msg: str) -> None:
    print(f"::warning::{msg}", file=sys.stderr, flush=True)


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
            ["gh", "api", *args],
            input=stdin,
            text=True,
            capture_output=True,
        )

    def get(self, path: str, *, paginate: bool = False):
        """GET a JSON endpoint; returns the parsed body or None on failure.

        For a paginated array endpoint, ``gh api --paginate`` merges every page's top-level
        JSON array into a single flat array, so ``json.loads`` yields one combined list — no
        ``--slurp`` or manual flattening is needed (verified against a multi-page endpoint).
        """
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
        """Publish the ``Linear ticket`` commit status. Returns True on success.

        A TERMINAL write (success/failure/warn-only) is ``critical=True``: if it fails, the
        old status stays as the required context — a prior ``success`` could then keep gating
        open under a run that meant to publish ``failure``. So a failed critical write is an
        error the caller turns into a nonzero job exit, not a swallowed warning. A ``pending``
        write is best-effort (``critical=False``)."""
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
        """The gate's OWN marker comment, or None.

        Match on the marker body AND the Actions-bot author: the marker string is public, so
        a contributor could paste it into a comment of their own. Without the author check
        the gate would PATCH/DELETE that foreign comment (403 → a swallowed warning, so the
        real failure comment never lands and a stale one survives a passing run). The gate's
        comments are always authored by ``github-actions[bot]`` (the default GITHUB_TOKEN
        identity)."""
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
                 "-F", "body=@-"],
                stdin=body,
            )
            if result.returncode != 0:
                warning(f"Failed to update marker comment {existing}: {result.stderr.strip()}")
        else:
            result = self._run(
                ["--method", "POST", f"/repos/{self.repo}/issues/{pr}/comments",
                 "-F", "body=@-"],
                stdin=body,
            )
            if result.returncode != 0:
                warning(f"Failed to create marker comment: {result.stderr.strip()}")

    def delete_marker_comment(self, pr: int) -> None:
        existing = self.find_marker_comment(pr)
        if existing is None:
            return
        result = self._run(["--method", "DELETE", f"/repos/{self.repo}/issues/comments/{existing}"])
        if result.returncode != 0:
            warning(f"Failed to delete marker comment {existing}: {result.stderr.strip()}")

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


# ── Linear via urllib ─────────────────────────────────────────────────────────────────
class LinearResult:
    def __init__(self, payload, http_status, error_codes, transport_ok):
        self.payload = payload
        self.http_status = http_status
        self.error_codes = error_codes
        self.transport_ok = transport_ok


def linear_post(query: str, variables: dict | None, token: str) -> LinearResult:
    """POST a GraphQL request. transport_ok=False only when the HTTP exchange never
    completed; a 4xx/5xx (which Linear uses for RATELIMITED) still returns transport_ok=True
    with the parsed error codes for the caller to classify."""
    body = {"query": query}
    if variables:
        body["variables"] = variables
    data = json.dumps(body).encode("utf-8")
    # Authorization verbatim: a personal API key raw, an OAuth token as "Bearer ...". The
    # header value is set once here; docs tell the caller which form to store.
    req = urllib.request.Request(
        LINEAR_API_URL,
        data=data,
        headers={"Authorization": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, raw, headers = resp.status, resp.read().decode("utf-8"), resp.headers
    except urllib.error.HTTPError as exc:
        status, raw, headers = exc.code, exc.read().decode("utf-8", "replace"), exc.headers
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"Linear request transport failure: {exc}")
        return LinearResult(None, None, [], transport_ok=False)

    _log_rate_limit(headers)
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    codes = [
        (e.get("extensions") or {}).get("code")
        for e in (payload.get("errors") or [])
        if (e.get("extensions") or {}).get("code")
    ]
    return LinearResult(payload, status, codes, transport_ok=True)


def _log_rate_limit(headers) -> None:
    """Log Linear's rate-limit headroom for the pilot (design §8), best-effort."""
    for name in headers.keys():
        low = name.lower()
        if low.startswith("x-ratelimit-requests-") or low.startswith("x-ratelimit-complexity-"):
            log(f"{name}: {headers[name]}")


# ── orchestration ───────────────────────────────────────────────────────────────────────
class Validator:
    def __init__(self, gh: GitHub, token: str, team_keys, require_open, enforce, soft_fail,
                 run_url):
        self.gh = gh
        self.token = token
        self.team_keys = team_keys
        self.require_open = require_open
        self.enforce = enforce
        self.soft_fail = soft_fail
        self.run_url = run_url
        self.exempt_label = os.environ.get("EXEMPT_LABEL", "")
        self.exempt_actors = lib.parse_actor_list(os.environ.get("EXEMPT_ACTORS", ""))
        self.pr_number: int | None = None
        self.validated_sha: str | None = None
        self.validated_base_branch: str | None = None

    # -- terminal outcomes -----------------------------------------------------------------
    def _guard_supersession(self) -> bool:
        """True unless the PR head or base changed after the validated snapshot."""
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

    def finish_pass(self, identifiers: str) -> int:
        summary("## linear-ticket: ✅ pass")
        summary("")
        summary(f"Linear has linked this PR to: **{identifiers}**")
        self.gh.delete_marker_comment(self.pr_number)
        if self._guard_supersession():
            if not self.gh.publish_status(self.validated_sha, "success",
                                          f"Linked Linear issue: {identifiers}", self.run_url,
                                          critical=True):
                return 1
        return 0

    def finish_exempt(self, headline: str, status_desc: str) -> int:
        summary("## linear-ticket: ✅ exempt")
        summary("")
        summary(headline)
        self.gh.delete_marker_comment(self.pr_number)
        if self._guard_supersession():
            if not self.gh.publish_status(self.validated_sha, "success", status_desc,
                                          self.run_url, critical=True):
                return 1
        return 0

    def finish_not_required(self, base_branch: str) -> int:
        if not self._guard_supersession():
            return 0
        summary("## linear-ticket: not required")
        summary("")
        summary(f"PR targets the unprotected `{base_branch}` branch.")
        self.gh.delete_marker_comment(self.pr_number)
        return 0

    def finish_fail(self, category: str, detail: str) -> int:
        guidance = lib.failure_guidance(category)
        outcome = lib.failure_outcome(category, self.enforce, self.soft_fail)

        body_lines = [
            lib.MARKER,
            "",
            f"### Linear ticket check — {outcome.verdict}",
            "",
            guidance,
        ]
        if detail:
            body_lines += ["", detail]
        if outcome.advisory:
            body_lines += ["", lib.advisory_note(category)]
        body_lines += [
            "",
            "---",
            (f"After linking an issue, re-run this check from the [workflow run]({self.run_url}) "
             "or edit the PR title/body to trigger a fresh run. A repository maintainer can "
             f"waive the requirement by applying the `{self.exempt_label or 'linear-exempt'}` label."),
        ]

        # Bail before mutating the PR if a newer run already owns the result: otherwise a
        # superseded run overwrites the newer run's comment (leaving a stale failure beside a
        # green status) even though its status write is suppressed.
        if not self._guard_supersession():
            return outcome.exit_code

        self.gh.upsert_marker_comment(self.pr_number, "\n".join(body_lines))

        summary(f"## linear-ticket: {outcome.verdict}")
        summary("")
        summary(guidance)
        if detail:
            summary(detail)

        # A failed terminal status write is always exit 1, even in warn-only: the point of
        # soft-fail is that the red status LANDS, and a silently-missing one leaves a stale
        # prior status standing as this commit's answer.
        if not self.gh.publish_status(self.validated_sha, outcome.state, outcome.description,
                                      self.run_url, critical=True):
            return 1
        return outcome.exit_code

    # -- the run ---------------------------------------------------------------------------
    def run(self, event: dict) -> int:
        wr = event.get("workflow_run") or {}
        head_sha = wr.get("head_sha") or ""
        if not head_sha:
            error("workflow_run.head_sha missing from event")
            return 1

        # The signal workflow runs only on `pull_request`, so the triggering run's event must
        # be `pull_request`. The caller's workflow-name + conclusion filters do not prove that;
        # a miswired caller pointed at a `push` workflow could otherwise resolve an open PR from
        # the pushed head SHA and publish `Linear ticket` for it. Fail fast instead.
        wr_event = wr.get("event") or ""
        if wr_event != "pull_request":
            error(f"Triggering workflow_run event was '{wr_event}', not 'pull_request'. The "
                  "signal workflow must run on pull_request; refusing to validate.")
            return 1

        self.pr_number = self._resolve_pr(event, head_sha)
        if self.pr_number is None:
            return 1  # error already reported

        pr = self.gh.get(f"/repos/{self.gh.repo}/pulls/{self.pr_number}")
        if not pr:
            error(f"Could not fetch PR #{self.pr_number}")
            return 1
        html_url = pr.get("html_url") or ""
        self.validated_sha = pr.get("head", {}).get("sha")
        branch = pr.get("head", {}).get("ref") or ""
        base_branch = pr.get("base", {}).get("ref") or ""
        self.validated_base_branch = base_branch
        title = pr.get("title") or ""
        body = pr.get("body") or ""
        labels = [lbl.get("name") for lbl in (pr.get("labels") or [])]
        author = (pr.get("user") or {}).get("login") or ""

        log(f"Validating PR #{self.pr_number} ({html_url}) at head {self.validated_sha}")
        if not base_branch:
            error(f"PR #{self.pr_number} has no base branch; refusing to validate.")
            return 1
        protected = self.gh.branch_is_protected(base_branch)
        if protected is None:
            error(f"Could not determine whether base branch '{base_branch}' is protected; "
                  "failing closed.")
            return 1
        if not protected:
            log(f"PR targets unprotected branch '{base_branch}' — Linear ticket not required.")
            return self.finish_not_required(base_branch)

        self.gh.publish_status(self.validated_sha, "pending",
                               "Checking for a linked Linear issue…", self.run_url)

        # Exemptions short-circuit before the Linear query. The actor allow-list is the
        # non-manual hatch for bot PRs (Dependabot/Renovate never carry a ticket); it is
        # opt-in and defaults empty, so there is still no built-in bypass.
        if self.exempt_label and self.exempt_label in labels:
            log(f"PR carries the '{self.exempt_label}' label — exempt.")
            return self.finish_exempt(
                f"PR carries the `{self.exempt_label}` label — the Linear-ticket requirement "
                "is waived.",
                f"Exempt via {self.exempt_label} label")
        if self.exempt_actors and author.lower() in self.exempt_actors:
            log(f"PR author @{author} is in exempt-actors — exempt.")
            return self.finish_exempt(
                f"PR author `@{author}` is in `exempt-actors` — the Linear-ticket requirement "
                "is waived for this bot/automation account.",
                f"Exempt via exempt-actors (@{author})")

        nodes, infra_error = self._query_attachments(html_url)

        if not infra_error:
            passing = lib.filter_issues(nodes, self.team_keys, self.require_open)
            if passing:
                joined = ", ".join(passing)
                log(f"PASS — linked issue(s): {joined}")
                return self.finish_pass(joined)

        return self._diagnose_and_fail(nodes, infra_error, branch, title, body)

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
        for number in dict.fromkeys(candidates):  # de-dup, preserve order
            data = self.gh.get(f"/repos/{self.gh.repo}/pulls/{number}")
            if data and data.get("state") == "open":
                open_prs.append(number)

        if len(open_prs) != 1:
            error(f"Expected exactly one open PR associated with {head_sha}, found "
                  f"{len(open_prs)} (event={wr.get('event')}). Refusing to publish an "
                  "ambiguous result.")
            return None
        return open_prs[0]

    def _query_attachments(self, html_url: str):
        """attachmentsForURL(this PR) with bounded retry for the async-link race (design §5
        step 4). Returns (nodes, infra_error)."""
        nodes: list = []
        infra_error = False
        queried_ok = False
        for attempt in range(5):
            result = linear_post(ATTACHMENTS_QUERY, {"url": html_url}, self.token)
            if not result.transport_ok:
                log(f"Linear request transport failure (attempt {attempt + 1})")
            elif result.error_codes or (result.http_status and result.http_status >= 400):
                kind = lib.classify_linear_error(result.http_status, result.error_codes)
                if kind == "terminal":
                    error(f"Linear returned a terminal error (HTTP {result.http_status}, "
                          f"codes: {result.error_codes or 'none'}). Failing closed as an "
                          "infrastructure error.")
                    infra_error = True
                    break
                log(f"Linear returned a retryable error (HTTP {result.http_status}, codes: "
                    f"{result.error_codes or 'none'}) on attempt {attempt + 1}")
            else:
                queried_ok = True
                nodes = (((result.payload or {}).get("data") or {}).get("attachmentsForURL")
                         or {}).get("nodes") or []
                if lib.count_linked(nodes) > 0:
                    break  # attachments present — evaluate policy, no reason to retry
                log(f"No attachment linked to this PR yet (attempt {attempt + 1})")
            if attempt < 4:
                time.sleep(BACKOFF_SECONDS[attempt])
        # A run that never once reached Linear (all attempts transport/retryable failures) is an
        # infrastructure outage, not a missing ticket — fail closed rather than blaming the
        # author (or, in warn-only, going green with the wrong diagnosis). Matches lib.py's
        # documented infra_error contract.
        if not infra_error and not queried_ok:
            error("Linear could not be queried after 5 attempts (transport/retryable errors "
                  "only). Failing closed as an infrastructure error.")
            infra_error = True
        return nodes, infra_error

    def _diagnose_and_fail(self, nodes, infra_error, branch, title, body) -> int:
        """Diagnostics only — never turns red into green."""
        linked_count = lib.count_linked(nodes)
        referenced_count = 0
        resolved_count = 0
        detail = ""

        if not infra_error and linked_count == 0:
            candidates = lib.extract_candidates("\n".join([branch, title, body]))
            if candidates:
                referenced_count = len(candidates)
                joined = ", ".join(candidates)
                try:
                    diag_query = lib.build_diagnostic_query(candidates)
                    diag = linear_post(diag_query, None, self.token)
                    if diag.transport_ok:
                        resolved_count = lib.count_resolved_candidates(diag.payload)
                except ValueError:
                    pass
                if resolved_count > 0:
                    detail = (f"Referenced identifiers (not linked): {joined} — at least one "
                              "resolves to a real Linear issue; link it to this PR.")
                else:
                    detail = f"Referenced identifiers (not linked): {joined}"

        category = lib.select_failure_category(infra_error, linked_count, referenced_count)
        if category == "policy_mismatch":
            linked_ids = ", ".join(
                issue["identifier"]
                for node in nodes
                if (issue := (node or {}).get("issue")) and issue.get("identifier")
            )
            detail = f"Linked but not accepted: {linked_ids}"

        log(f"FAIL category={category} linked={linked_count} referenced={referenced_count} "
            f"resolved={resolved_count}")
        return self.finish_fail(category, detail)


def main() -> int:
    repo = os.environ.get("GH_REPO")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    token = os.environ.get("LINEAR_API_TOKEN")

    if not repo:
        error("GH_REPO is required")
        return 1
    if not event_path or not os.path.isfile(event_path):
        error("GITHUB_EVENT_PATH is required and must point at the event payload")
        return 1
    if not token:
        error("LINEAR_API_TOKEN secret is not set; failing closed (infrastructure error)")
        return 1

    try:
        team_keys = lib.normalize_team_keys(os.environ.get("TEAM_KEYS", ""))
    except ValueError as exc:
        error(f"Invalid team-keys input: {exc}. Entries must be uppercase alphanumeric team "
              "keys, unique, comma-separated.")
        return 1

    with open(event_path, encoding="utf-8") as handle:
        event = json.load(handle)

    require_open = os.environ.get("REQUIRE_OPEN_ISSUE", "true") != "false"
    enforce = os.environ.get("ENFORCE", "true") != "false"
    # NOT defaulted true here even though the workflow input is: linear-ticket.yml always
    # passes SOFT_FAIL, so an absent value means a skewed pin (old workflow YAML, new scripts)
    # rather than a caller choosing the loud mode. See lib.soft_fail_enabled.
    soft_fail = lib.soft_fail_enabled(os.environ.get("SOFT_FAIL"))
    run_url = os.environ.get("RUN_URL", "")

    validator = Validator(GitHub(repo), token, team_keys, require_open, enforce, soft_fail,
                          run_url)
    return validator.run(event)


if __name__ == "__main__":
    sys.exit(main())
