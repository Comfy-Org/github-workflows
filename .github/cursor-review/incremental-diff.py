#!/usr/bin/env python3
"""Build the "hunks new since the last reviewed round" block (BE-15558).

The panel prompt calls this block "the subset of the diff above". It used to be
built as `git diff LAST_REVIEWED_SHA...HEAD_SHA`, which is not a subset of
anything: when HEAD is a merge commit that pulled the base branch into the
branch, LAST is an ancestor of HEAD, the merge base of the two IS LAST, and the
range therefore contains every commit that merge brought in. Measured on one
consumer repo, round 2 of a PR built a 9,800-line block against a 1,234-line
reviewed diff with 119 of its 133 files outside the PR, and two PRs built
~1.16M-line blocks; the prompt grew from ~98 KB to ~826 KB, most legs timed out,
and the legs that finished reviewed files from the base branch instead of the PR.

The fix is to stop diffing two commits and start diffing two PR *patches*. Both
are three-dot diffs against the base, so each contains only the branch's own
changes and neither can carry a base-branch commit:

* OLD — `git diff BASE...LAST_REVIEWED` — what the last round saw.
* NEW — the reviewed diff this round is running on (`pr-diff.patch`).

`build` emits every NEW file section whose hunk content differs from the same
file's section in OLD, plus every NEW section for a file OLD does not have. A
file OLD had and NEW does not contributes nothing: it is no longer in the PR, so
there is nothing to prioritize. Because every emitted section is copied verbatim
out of NEW, the output is a subset of NEW by construction.

Hunk content is compared with the `@@ -a,b +c,d @@` headers normalised to
`@@ @@`, so a pure rebase — identical hunks at shifted line numbers — produces
an empty block rather than re-reviewing the whole PR. `index <blob>..<blob>`
lines are excluded from the comparison for the same reason: they change whenever
the base blob moves, even when the branch's own edit did not.

`check` is the fail-safe the workflow runs afterwards, against the full reviewed
diff, so the subset property is *verified* and not merely intended — the block
is discarded whole (and the panel runs on the full diff alone, which is always
correct) if it names a file the reviewed diff does not carry or is longer than
the reviewed diff.

Subcommands:

  build   --old <patch> --new <patch> --out <patch>
  check   --new <patch> --full <patch>   # exit 0 subset, 2 not a subset

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import argparse
import sys

_DIFF_HEADER = "diff --git "


def parse_paths(header: str):
    """Return the (old, new) paths named by a `diff --git a/X b/Y` line.

    git does not escape the separator, so `a/my file b/my file` is ambiguous on
    its face. Resolve it the way git's own readers do: every candidate ` b/`
    split is tried and the one whose halves are consistent wins, preferring the
    common `a/X b/X` case. A header that cannot be parsed yields `()` — the
    caller treats that as "unknown path", which the fail-safe counts as foreign
    rather than waving through.
    """
    rest = header[len(_DIFF_HEADER):].rstrip("\n")
    if not rest.startswith("a/"):
        return ()
    candidates = []
    idx = rest.find(" b/")
    while idx != -1:
        old = rest[2:idx]
        new = rest[idx + 3:]
        if old and new:
            candidates.append((old, new))
        idx = rest.find(" b/", idx + 1)
    if not candidates:
        return ()
    for old, new in candidates:
        if old == new:
            return (old, new)
    # A rename: no split is self-confirming, so take the first, which is what
    # git produces for the unambiguous case.
    return candidates[0]


def split_sections(text: str):
    """Split a unified diff into `(header_line, [lines...])` file sections.

    Anything before the first `diff --git ` line (git emits none, but a caller's
    file could) is dropped rather than silently attached to the first section.
    """
    sections = []
    current = None
    for line in text.splitlines(keepends=True):
        if line.startswith(_DIFF_HEADER):
            current = (line, [line])
            sections.append(current)
        elif current is not None:
            current[1].append(line)
    return sections


def hunk_signature(lines):
    """The comparable content of one file section.

    From the first `@@` onward, with each hunk header collapsed to `@@ @@` so a
    pure line shift (and a changed trailing function context) does not read as a
    change. A section with no `@@` at all — a binary file, a mode-only change, a
    rename with no edit — falls back to every line after the header except
    `index`, which is base-blob dependent and would otherwise make every rebase
    look like a change.
    """
    signature = []
    seen_hunk = False
    for line in lines[1:]:
        if line.startswith("@@"):
            seen_hunk = True
            signature.append("@@ @@")
        elif seen_hunk:
            signature.append(line.rstrip("\n"))
    if seen_hunk:
        return tuple(signature)
    return tuple(
        line.rstrip("\n")
        for line in lines[1:]
        if not line.startswith("index ")
    )


def build(old_text: str, new_text: str) -> str:
    """Return the sections of NEW that OLD lacks or that changed since OLD."""
    old_by_path = {}
    for header, lines in split_sections(old_text):
        paths = parse_paths(header)
        # An unparseable header is keyed by its raw line: it can still match the
        # identical header on the NEW side, and it can never collide with a path.
        key = paths[1] if paths else header
        old_by_path[key] = hunk_signature(lines)
    out = []
    for header, lines in split_sections(new_text):
        paths = parse_paths(header)
        key = paths[1] if paths else header
        if key in old_by_path and old_by_path[key] == hunk_signature(lines):
            continue
        out.extend(lines)
    return "".join(out)


def _paths_in(text: str):
    """Every path a patch's `diff --git` headers name, old and new sides."""
    found = set()
    for header, _lines in split_sections(text):
        paths = parse_paths(header)
        if paths:
            found.update(paths)
        else:
            found.add(header.rstrip("\n"))
    return found


def check(new_text: str, full_text: str):
    """Return `(foreign_count, new_lines, full_lines)` for the fail-safe.

    `foreign_count` counts the file sections in the incremental block naming a
    path the full reviewed diff does not carry. A block is a subset when that is
    zero AND it is no longer than the reviewed diff.
    """
    full_paths = _paths_in(full_text)
    foreign = 0
    for header, _lines in split_sections(new_text):
        paths = parse_paths(header)
        names = set(paths) if paths else {header.rstrip("\n")}
        # Foreign only when NOTHING it names is in the reviewed diff: a rename
        # the classifier rewrote should not read as foreign on one side alone.
        if not (names & full_paths):
            foreign += 1
    return foreign, _count_lines(new_text), _count_lines(full_text)


def _count_lines(text: str) -> int:
    """Line count matching `wc -l`, so the workflow's numbers agree with the log."""
    return text.count("\n")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        return fh.read()


def cmd_build(args) -> int:
    old_text = _read(args.old)
    new_text = _read(args.new)
    with open(args.out, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        fh.write(build(old_text, new_text))
    return 0


def cmd_check(args) -> int:
    foreign, new_lines, full_lines = check(_read(args.new), _read(args.full))
    print(f"foreign={foreign}")
    print(f"new_lines={new_lines}")
    print(f"full_lines={full_lines}")
    return 0 if foreign == 0 and new_lines <= full_lines else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    build_p = sub.add_parser("build", help="emit the sections of NEW that changed since OLD")
    build_p.add_argument("--old", required=True, help="the patch the last round reviewed (BASE...LAST)")
    build_p.add_argument("--new", required=True, help="the patch this round reviews (the reviewed diff)")
    build_p.add_argument("--out", required=True, help="where to write the incremental block")
    build_p.set_defaults(func=cmd_build)

    check_p = sub.add_parser("check", help="verify the block is a subset of the reviewed diff")
    check_p.add_argument("--new", required=True, help="the incremental block to verify")
    check_p.add_argument("--full", required=True, help="the reviewed diff it must be a subset of")
    check_p.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
