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

`build` emits every NEW file section whose content differs from the same file's
section in OLD, plus every NEW section for a file OLD does not have. A file OLD
had and NEW does not contributes nothing: it is no longer in the PR, so there is
nothing to prioritize. Because every emitted section is copied verbatim out of
NEW, the output is a subset of NEW by construction.

Sections are compared with the `@@ -a,b +c,d @@` headers normalised to `@@ @@`,
so a pure rebase — identical hunks at shifted line numbers — produces an empty
block rather than re-reviewing the whole PR. Lines whose content tracks the BASE
blob rather than the branch's own edit (`index <blob>..<blob>`, the similarity
percentages) are excluded for the same reason: they move whenever the base blob
moves, even when the branch's edit did not.

`check` is the fail-safe the workflow runs afterwards, against the full reviewed
diff, so the subset property is *verified* and not merely intended — every
section of the block must appear BYTE FOR BYTE in the reviewed diff, and the
block is discarded whole if any does not (or if it is longer than the reviewed
diff). Comparing bytes rather than path names is what makes the README's "can
never contain a hunk the PR does not carry" a checked property: a path-only
check waves through fabricated, reordered or duplicated hunks as long as they
are carried under some path the PR does touch.

Subcommands:

  build   --old <patch> --new <patch> --out <patch>
  check   --new <patch> --full <patch>   # exit 0 subset, 2 not a subset

Run: python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py'
"""

import argparse
import sys
from collections import Counter

_DIFF_HEADER = "diff --git "

# Lines whose content tracks the BASE blob rather than the branch's own edit.
# They move on every rebase even when the author changed nothing, so they are
# excluded from the comparison. A binary section is the one exception — see
# `hunk_signature`.
_BASE_VOLATILE = ("index ", "similarity index ", "dissimilarity index ")


def _lf_lines(text: str):
    r"""Split on LF only, keeping the terminator.

    `str.splitlines` is wrong here: it also breaks on a lone `\r`, `\v`, `\f`,
    `\x1c`-`\x1e`, `\x85`, U+2028 and U+2029, none of which git treats as a line
    break. A patch is attacker-authored PR bytes, so a content line
    `+x\x0cdiff --git a/lib/auth.py b/lib/auth.py` would otherwise forge a file
    section boundary, putting a real file's remaining hunks under a header of
    the PR's choosing — and `check` would learn the forged path from the same
    bad split and not count the section foreign.
    """
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def parse_paths(header: str):
    """Return the (old, new) paths named by a `diff --git a/X b/Y` line.

    git does not escape the separator, so `a/my file b/my file` is ambiguous on
    its face. Resolve it the way git's own readers do: every candidate ` b/`
    split is tried and the one whose halves are consistent wins, preferring the
    common `a/X b/X` case. A header that cannot be parsed yields `()` — the
    caller treats that as "unknown path" and keys the section by its raw header.

    A rename has no self-confirming split, so this can still guess wrong on a
    header like `a/x b/c b/d`; `section_paths` is what resolves those, from the
    section's own one-path-per-line `rename from`/`rename to` lines.
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


def section_paths(header: str, lines):
    """The (old, new) paths one file section names.

    Prefers the section's own `rename from` / `rename to` lines over the
    `diff --git` header. They carry ONE path per line, so they are unambiguous
    exactly where the header is not: `a/x b/c b/d` has two readings and the
    header alone cannot tell them apart, which used to key the section under a
    path (`x` -> `c b/d`) that no later round's plain header could match,
    re-emitting the whole file every time. Falls back to the header when the
    section carries no rename pair, which is every non-rename section.
    """
    old = new = None
    for line in lines[1:]:
        if line.startswith("@@"):
            break
        if line.startswith("rename from "):
            old = line[len("rename from "):].rstrip("\n")
        elif line.startswith("rename to "):
            new = line[len("rename to "):].rstrip("\n")
    if old and new:
        return (old, new)
    return parse_paths(header)


def split_sections(text: str):
    """Split a unified diff into `(header_line, [lines...])` file sections.

    Anything before the first `diff --git ` line (git emits none, but a caller's
    file could) is dropped rather than silently attached to the first section.
    """
    sections = []
    current = None
    for line in _lf_lines(text):
        if line.startswith(_DIFF_HEADER):
            current = (line, [line])
            sections.append(current)
        elif current is not None:
            current[1].append(line)
    return sections


def hunk_signature(lines):
    """The comparable content of one file section. Two parts, both load-bearing:

    * The pre-hunk metadata — `old mode`/`new mode`, `rename from`/`rename to`,
      `deleted file mode`, a typechange — minus the base-volatile lines.
      Dropping it whenever the file also had hunks hid a round that only added
      the executable bit to an already-edited script: small, high-signal, and
      exactly the kind of change this block exists to surface.
    * The hunks, from the first `@@` onward, each hunk header collapsed to
      `@@ @@` so a pure line shift (and a changed trailing function context)
      does not read as a change.

    A section with no `@@` at all — a binary file, a mode-only change, a rename
    with no edit — is all metadata. For the `Binary files a/X and b/X differ`
    form git emits without `--binary`, that line is a CONSTANT and
    `index <old>..<new>` is the section's only content-dependent line, so the
    base-volatile exclusion is lifted for a binary section; otherwise a binary
    whose bytes changed since the last round compares equal and is dropped.
    """
    body = lines[1:]
    binary = any(line.startswith("Binary files ") for line in body)
    signature = []
    seen_hunk = False
    for line in body:
        if line.startswith("@@"):
            seen_hunk = True
            signature.append("@@ @@")
        elif seen_hunk:
            signature.append(line.rstrip("\n"))
        elif binary or not line.startswith(_BASE_VOLATILE):
            signature.append(line.rstrip("\n"))
    return tuple(signature)


def build(old_text: str, new_text: str) -> str:
    """Return the sections of NEW that OLD lacks or that changed since OLD."""
    old_by_path = {}
    for header, lines in split_sections(old_text):
        paths = section_paths(header, lines)
        # An unparseable header is keyed by its raw line: it can still match the
        # identical header on the NEW side, and it can never collide with a path.
        key = paths[1] if paths else header
        old_by_path[key] = hunk_signature(lines)
    out = []
    for header, lines in split_sections(new_text):
        paths = section_paths(header, lines)
        key = paths[1] if paths else header
        if key in old_by_path and old_by_path[key] == hunk_signature(lines):
            continue
        out.extend(lines)
    return "".join(out)


def check(new_text: str, full_text: str):
    """Return `(foreign_count, new_lines, full_lines)` for the fail-safe.

    `foreign_count` counts the file sections of the incremental block that do
    not appear BYTE FOR BYTE in the full reviewed diff. That is the property the
    README and the caller guide assert — "can never contain a hunk the PR does
    not carry" — checked directly, rather than the weaker "every section names
    SOME path the reviewed diff also names", which waves through fabricated,
    reordered or duplicated hunks under a path the PR does happen to touch.

    Counted as a multiset, so a section the block emits twice is foreign on its
    second appearance even though the reviewed diff carries it once.
    """
    remaining = Counter("".join(lines) for _header, lines in split_sections(full_text))
    foreign = 0
    for _header, lines in split_sections(new_text):
        section = "".join(lines)
        if remaining[section] > 0:
            remaining[section] -= 1
        else:
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
