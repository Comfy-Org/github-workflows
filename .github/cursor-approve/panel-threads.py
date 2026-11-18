#!/usr/bin/env python3
"""cursor-approve: build the panel-thread context file the axes read.

Opt-in (`panel_threads_context`). The approval axes judge a PR from the diff and
their own earlier verdicts (`prior_rounds`) but never see the cursor-review
panel's line-anchored finding threads, the author's replies, or which threads
are resolved. So an axis can block on a concern the author already answered and
resolved in a panel thread, or duplicate a thread that is still open — each one
a fix-or-argue round. This builds the context that closes the gap: the start
phase runs it, uploads the result as the `panel-threads` artifact, and each
axis's ``aggregate.py render --panel-threads-file`` appends it as one fenced
DATA block (``render_panel_threads``).

Identifying a panel thread
--------------------------
The same discriminator ``build-ledger.py``'s prior-review ledger uses, for the
same reason: the thread's originating review body starts with
``CONSOLIDATED_MARKER`` AND its first comment's author is a ``Bot``. The marker
alone is not enough — this repo is public and the marker is in it, so on a
public PR any reader can open a review whose body carries it and forge a panel
thread, planting attacker text into the prompt. Requiring a Bot author is what
``_consolidated_reviews`` requires before it feeds the ledger a prompt, and it
is identity-independent: every posting identity cursor-review can use (its
``bot_app_id`` App, else ``github-actions[bot]``) is a Bot, so no per-caller
login has to be hardcoded.

Trust model
-----------
A thread's finding is the panel's text; a reply is written by the PR author and
is untrusted. Neither carries an instruction. This script only reads, truncates
and caps; the fence defang, the mention neutralisation and the DATA label that
make the block safe to splice into a prompt are ``aggregate.py``'s
``render_panel_threads`` — the same split as ``card.py prior`` /
``render_prior``. A context that cannot be built (a failed or timed-out query,
an unexpected shape) degrades to an empty file plus a ``::warning::``, never a
failed start phase: the panel context is an aid, and its absence just leaves the
axes judging exactly as they do with the feature off.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The JSON file, in UTF-8 bytes. The least relevant threads are dropped until it
# fits (see `fit`); aggregate.py's MAX_PANEL_FILE_BYTES is comfortably above it.
MAX_PANEL_PAYLOAD_BYTES = 48 * 1024
# A finding's first paragraph, and one reply body: build-ledger's MAX_BODY_CHARS
# for the finding, tighter for a reply so a long back-and-forth cannot crowd the
# findings out.
MAX_FINDING_CHARS = 600
MAX_REPLY_CHARS = 400
# Replies kept per thread; the rest are summarised as a count so an axis can see
# the conversation continued without the whole of it reaching the prompt.
MAX_REPLIES = 10
# Capping drops the least relevant thread first. Outdated threads (the hunk moved
# on) go last, resolved below open, and a higher severity ahead of a lower one,
# so the open, live, most-severe threads are the ones that survive a cap.
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "nit": 4}


def _load(rel_path: str, name: str):
    """Import a sibling script by path (the names carry hyphens)."""
    path = os.path.join(HERE, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# build-ledger.py imports gate-unresolved.py, so this one import gives the
# canonical CONSOLIDATED_MARKER, the Bot author type, and the body helpers.
_BL = _load(os.path.join("..", "cursor-review", "build-ledger.py"), "build_ledger")
CONSOLIDATED_MARKER = _BL.CONSOLIDATED_MARKER
BOT_USER_TYPE = _BL.BOT_USER_TYPE
_strip_badge = _BL._strip_badge
_truncate = _BL._truncate

# reviewThreads carrying the finding body AND the replies' bodies, which
# gate-unresolved.py's query (tuned for the gate and the ledger) does not fetch.
# Two comment aliases, as gate-unresolved splits its own: `firstComment` carries
# the finding plus its `pullRequestReview` body — read only for the marker, and
# the consolidated panel review body is LARGE, so it is fetched ONCE per thread,
# never once per reply — while `comments` carries every comment's author and
# body for the replies, with no review body repeated.
QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          isOutdated
          path
          line
          startLine
          firstComment: comments(first: 1) {
            nodes {
              # __typename tells a Bot (a GraphQL login with no `[bot]` suffix)
              # from a user who merely shares the name.
              author { __typename login }
              body
              pullRequestReview { body }
            }
          }
          comments(first: 50) {
            totalCount
            nodes {
              author { __typename login }
              body
            }
          }
        }
      }
    }
  }
}
"""


def run_graphql(owner: str, name: str, pr: int, cursor):
    """One page of the reviewThreads query via the gh CLI.

    Raises RuntimeError on a failed or timed-out query; `main` catches it and
    degrades to an empty context rather than failing the (advisory) start phase.
    """
    args = [
        "gh", "api", "graphql",
        "-f", f"query={QUERY}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        "-F", f"pr={pr}",
    ]
    args += ["-F", "cursor=null"] if cursor is None else ["-f", f"cursor={cursor}"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("GraphQL query timed out after 120s") from None
    if result.returncode != 0:
        raise RuntimeError(f"GraphQL query failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def iter_review_threads(owner: str, name: str, pr: int):
    """Yield every review thread node on the PR, paging transparently."""
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


def _first_comment(thread: dict) -> dict:
    """The thread's anchoring comment (the finding), or {}."""
    nodes = (thread.get("firstComment") or {}).get("nodes") or []
    return nodes[0] if nodes and isinstance(nodes[0], dict) else {}


def is_panel_thread(thread: dict) -> bool:
    """True when the thread is a cursor-review panel finding thread.

    Marker on the originating review AND a Bot first author — both, because this
    is about to feed a prompt (see the module docstring).
    """
    first = _first_comment(thread)
    if not first:
        return False
    review = first.get("pullRequestReview") or {}
    if not (review.get("body") or "").startswith(CONSOLIDATED_MARKER):
        return False
    return ((first.get("author") or {}).get("__typename") or "") == BOT_USER_TYPE


def _utf8(text: str) -> str:
    """`text` with any lone UTF-16 surrogate replaced by `?`, so it can always be
    UTF-8 encoded — the panel-threads file and the axis prompt are both UTF-8."""
    return str(text or "").encode("utf-8", "replace").decode("utf-8")


def _first_paragraph(text: str) -> str:
    """The finding's first paragraph: everything up to the first blank line."""
    text = (text or "").lstrip()
    out = []
    for line in text.splitlines():
        if not line.strip() and out:
            break
        out.append(line)
    return "\n".join(out).strip()


def build_entry(thread: dict) -> dict:
    """One panel thread reduced to the fields an axis needs, each bounded."""
    first = _first_comment(thread)
    severity, rest = _strip_badge(first.get("body") or "")
    finding = _truncate(_first_paragraph(rest), MAX_FINDING_CHARS)

    comment_block = thread.get("comments") or {}
    nodes = comment_block.get("nodes") or []
    replies = []
    # nodes[0] is the finding again (the `comments` alias starts from the first);
    # the replies are everything after it.
    for comment in nodes[1:]:
        if not isinstance(comment, dict):
            continue
        author = (comment.get("author") or {}).get("login") or "(unknown)"
        replies.append({"author": _utf8(author),
                        "body": _utf8(_truncate(comment.get("body") or "", MAX_REPLY_CHARS))})
    # More than the page carried, or more than we keep, is a truncation.
    total = comment_block.get("totalCount")
    replies_truncated = (isinstance(total, int) and total > len(nodes)) or len(replies) > MAX_REPLIES
    replies = replies[:MAX_REPLIES]

    line = thread.get("line")
    if isinstance(line, bool) or not isinstance(line, int):
        line = thread.get("startLine")
    if isinstance(line, bool) or not isinstance(line, int):
        line = None

    return {
        "path": _utf8(thread.get("path") or ""),
        "line": line,
        "severity": severity,  # a lowercase label or None
        "resolved": bool(thread.get("isResolved")),
        "outdated": bool(thread.get("isOutdated")),
        "finding": _utf8(finding),
        "replies": replies,
        "replies_truncated": replies_truncated,
    }


def _sort_key(entry: dict):
    return (entry["outdated"], entry["resolved"], SEVERITY_RANK.get(entry["severity"], 99))


def _payload_bytes(threads: list) -> int:
    return len(json.dumps({"threads": threads}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def fit(threads: list, limit: int) -> list:
    """`threads` trimmed to within `limit` UTF-8 bytes.

    The least relevant thread (last after the sort) is dropped until it fits. A
    lone thread too large on its own loses its replies first, then its finding is
    hard-cut; if even that will not fit, nothing is returned rather than an
    oversized file.
    """
    threads = list(threads)
    while threads:
        if _payload_bytes(threads) <= limit:
            return threads
        if len(threads) == 1:
            only = threads[0]
            if only["replies"]:
                only["replies"] = []
                only["replies_truncated"] = True
                continue
            only["finding"] = only["finding"][:max(0, limit // 2)]
            return threads if _payload_bytes(threads) <= limit else []
        threads.pop()
    return threads


def collect(owner: str, name: str, pr: int) -> list:
    """Every panel thread on the PR, reduced, sorted and capped."""
    entries = [build_entry(t) for t in iter_review_threads(owner, name, pr) if is_panel_thread(t)]
    entries.sort(key=_sort_key)
    return fit(entries, MAX_PANEL_PAYLOAD_BYTES)


def cmd_build(args) -> int:
    owner, _, name = args.repo.partition("/")
    if not owner or not name:
        print(f"::error::--repo must be owner/name, got {args.repo!r}", file=sys.stderr)
        return 2
    try:
        threads = collect(owner, name, args.pr_number)
    except Exception as e:  # noqa: BLE001 — a query/shape failure degrades, never fails the round
        print(f"::warning::panel-threads: no context built ({e.__class__.__name__}: {e}); "
              "the axes get no panel-thread block this run", file=sys.stderr)
        threads = []
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"threads": threads}, f, ensure_ascii=False)
    print(f"panel threads: {len(threads)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--repo", required=True, help="owner/name of the PR repo")
    b.add_argument("--pr-number", required=True, type=int)
    b.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    return cmd_build(args)


if __name__ == "__main__":
    sys.exit(main())
