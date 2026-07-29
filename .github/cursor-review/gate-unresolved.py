#!/usr/bin/env python3
"""Blocking gate: fail when a PR has unresolved cursor-review finding threads.

Used by cursor-review.yml when the caller opts in with `blocking: true`. Queries
the PR's review threads (GraphQL) and exits non-zero when any cursor-review
finding thread is still unresolved — turning the check red so a required-status-
check configuration blocks the merge until the threads are addressed. With
`blocking: false` (the default) this script is never run and the review stays
purely advisory.

Identifying a cursor-review thread
----------------------------------
A review thread counts as a cursor-review finding thread when its originating
review's body starts with ``CONSOLIDATED_MARKER`` — the exact discriminator the
workflow's own dup-check uses to recognize its consolidated reviews. This was
chosen over matching the posting identity (e.g. ``cloud-code-bot`` /
``github-actions[bot]``) on purpose:

* It is identity-independent. The consolidated review posts as
  ``github-actions[bot]`` by default, or under a dedicated bot App when the
  caller sets ``bot_app_id`` — the gate must not have to resolve a bot login
  that varies per caller.
* It is the canonical cursor-review signal already used elsewhere in the
  workflow, so there is one source of truth for "is this our review?".

Only the consolidated review that carries inline findings creates review
threads; the "no findings", error, and inline-anchoring-fallback reviews are
body-only and create none. So a thread existing here already means a real
finding was anchored to a line.

Outdated threads
----------------
Threads whose hunk has changed since the finding was posted (``isOutdated``)
are NOT counted. A re-review on a new commit re-posts anything still wrong as a
fresh, non-outdated thread, so blocking on outdated ones would force resolution
of superseded findings. Resolving the live threads — or pushing a fix and
re-triggering the review — is what turns the gate green, which makes it
idempotent on re-run.
"""

import argparse
import json
import os
import subprocess
import sys

CONSOLIDATED_MARKER = "## 🔍 Cursor Review — Consolidated panel"

QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          isOutdated
          comments(first: 1) {
            nodes {
              # fullDatabaseId (BigInt, serialized as a String) alongside
              # databaseId, which the schema declares as a 32-bit `Int` while
              # live review-comment ids are already past 2^31−1. GitHub happens
              # to serialize the oversized value today, but the ledger joins on
              # this id, so it reads the field that is actually typed for it and
              # keeps databaseId only as a fallback.
              databaseId
              fullDatabaseId
              author { login }
              pullRequestReview { body }
            }
          }
        }
      }
    }
  }
}
"""


def run_graphql(owner: str, name: str, pr: int, cursor):
    """Run one page of the reviewThreads query via the gh CLI."""
    args = [
        "gh", "api", "graphql",
        "-f", f"query={QUERY}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        "-F", f"pr={pr}",
    ]
    # gh's -F coerces the literal `null` to a JSON null, which GraphQL reads as
    # "from the start"; subsequent pages pass the opaque string cursor via -f.
    args += ["-F", "cursor=null"] if cursor is None else ["-f", f"cursor={cursor}"]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        # A query failure must not silently pass the gate — exit 2 (distinct
        # from the "found unresolved" exit 1) so the check fails loudly.
        print(f"GraphQL query failed: {result.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(result.stdout)


def is_cursor_thread(thread: dict) -> bool:
    """True when the thread's originating review is a cursor-review consolidation."""
    comments = (thread.get("comments") or {}).get("nodes") or []
    if not comments:
        return False
    review = comments[0].get("pullRequestReview") or {}
    body = review.get("body") or ""
    return body.startswith(CONSOLIDATED_MARKER)


def iter_threads(owner: str, name: str, pr: int):
    """Yield every review thread node on the PR, paging transparently.

    The single reader of ``reviewThreads`` state for the whole cursor-review
    workflow: this gate consumes it below, and ``build-ledger.py`` imports it to
    carry ``isResolved`` / ``isOutdated`` into the prior-review ledger. Keeping
    one query (and one ``CONSOLIDATED_MARKER``) means the gate and the ledger can
    never disagree about which threads are ours.
    """
    cursor = None
    while True:
        data = run_graphql(owner, name, pr, cursor)
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        for thread in threads["nodes"]:
            yield thread
        page = threads["pageInfo"]
        if not page["hasNextPage"]:
            return
        cursor = page["endCursor"]


def collect_unresolved(owner: str, name: str, pr: int) -> int:
    """Count live (non-outdated, unresolved) cursor-review finding threads."""
    count = 0
    for thread in iter_threads(owner, name, pr):
        if not is_cursor_thread(thread):
            continue
        if thread.get("isResolved") or thread.get("isOutdated"):
            continue
        count += 1
    return count


def emit(text: str) -> None:
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="owner/name of the PR repo")
    parser.add_argument("--pr-number", required=True, type=int)
    args = parser.parse_args()

    owner, _, name = args.repo.partition("/")
    if not owner or not name:
        print(f"--repo must be owner/name, got {args.repo!r}", file=sys.stderr)
        raise SystemExit(2)

    count = collect_unresolved(owner, name, args.pr_number)

    if count:
        emit(
            f"❌ **Blocking gate: {count} unresolved cursor-review finding "
            f"thread(s).**\n\nResolve each open cursor-review thread on this PR "
            "(or push a fix and re-trigger the review), then re-run this check."
        )
        raise SystemExit(1)

    emit("✅ **Blocking gate: no unresolved cursor-review finding threads.**")


if __name__ == "__main__":
    main()
