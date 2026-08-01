#!/usr/bin/env python3
"""Fail if a reusable workflow's `workflows_ref` is defaulted or unguarded.

Why this exists (BE-5546): every reusable workflow here that loads its backing
scripts at run time takes a `workflows_ref` input and checks this repo out at
that ref. When that input defaults to a floating branch, a caller can SHA-pin
`uses:` and still load MUTABLE scripts — into jobs that hold write permissions.
The pin then proves nothing. So `workflows_ref` carries no default at all and
each consuming job fails fast on an empty value (GitHub does NOT enforce
`required: true` for `workflow_call` inputs — an omitted input arrives as `''`
and `actions/checkout` with `ref: ''` silently takes the default branch).

Removing the default is a one-line edit to undo, hence this lint: it is the
regression guard that keeps the hole from coming back, and it deliberately
covers workflows added *later* rather than an allow-list of today's three.

Two checks, because the default is only half the hole:

1. no `default:` on the `workflows_ref` input, and
2. every job that checks out at `ref: ${{ inputs.workflows_ref }}` runs the
   fail-fast empty-ref guard first. Without (2) a *new* job — or a whole new
   reusable workflow — reintroduces the `ref: ''` default-branch fallback with
   the lint still green, since it never declared a default to begin with.

Parsing is text-level on purpose: this repo is stdlib-only (no PyYAML), same
constraint the `bump-callers.sh` awk rewrite works under. We only need to
locate one input block and look for one key inside it. Text parsing fails
*silently* when it meets a shape it can't follow, so a file that references
`inputs.workflows_ref` but whose declaration the parser cannot find is a hard
error rather than a quiet skip — "not applicable" and "I couldn't read this"
must never look the same.

Run locally:
    python3 .github/workflow-pins/check_workflow_pins.py
    python3 .github/workflow-pins/check_workflow_pins.py --workflows-dir <dir>
"""

import argparse
import os
import re
import sys

INPUT_NAME = "workflows_ref"
DEFAULT_WORKFLOWS_DIR = ".github/workflows"

# Reusable workflows that still carry a `workflows_ref` default and are tracked
# for the same fix under their own ticket. An entry here is a KNOWN debt, not a
# blessing — the checker fails on a STALE entry so the list drains itself
# instead of rotting, whether the workflow dropped its default (fixed) or no
# longer exists under that name (renamed or deleted). The latter matters most:
# left alone it would pre-exempt whatever future workflow reuses the filename.
#
#   pr-size.yml — same shape as the three fixed in BE-5546, but its caller
#   fleet (`vars.PR_SIZE_CALLERS`) was not enumerated by the BE-5543 spike, so
#   dropping its default is an unverified break of consumer CI. Needs its own
#   caller audit first.
KNOWN_EXEMPT = frozenset({"pr-size.yml"})

_ON_RE = re.compile(r"""^(['"]?)on\1\s*:(.*)$""")
_JOBS_RE = re.compile(r"""^(['"]?)jobs\1\s*:""")

# `ref: ${{ inputs.workflows_ref }}` — the checkout the guard exists to protect.
# Any `ref:` mentioning the input counts, not just the bare expression: a
# `${{ inputs.workflows_ref || 'main' }}` fallback is the same hole wearing a
# different hat, and it should trip the lint rather than slip past it.
#
# Two spellings, because a key at line start is not the only way to write one.
# The flow-mapping form puts the whole `with:` on one line —
#   with: {repository: Comfy-Org/github-workflows, ref: "${{ inputs.workflows_ref }}"}
# — which is the same unguarded checkout, and the same one-line bypass already
# barred for `default:`. The flow pattern stops the value at the entry boundary
# (`[^,}]`) so a sibling entry mentioning the input can't be misread as the ref.
_REF_USE_BLOCK_RE = re.compile(r"""^\s*(['"]?)ref\1\s*:.*inputs\.%s\b""" % INPUT_NAME)
_REF_USE_FLOW_RE = re.compile(r"""[{,]\s*(['"]?)ref\1\s*:[^,}]*inputs\.%s\b""" % INPUT_NAME)
# …and a third, because the value does not have to share the key's line at all:
#   ref: >-              ref: |              ref:              ref:  # pinned
#     ${{ … }}             ${{ … }}            ${{ … }}          ${{ … }}
# A block scalar (`|`/`>`, with any chomping or explicit-indent modifier), or a
# plain multi-line scalar, or a quote opened at end of line — all leave the key
# line with no `inputs.` on it, so BOTH same-line patterns read the checkout as
# absent. Same bypass as the flow form, spelled vertically. The key line only
# OPENS a window; a hit needs the input to actually appear in the continuation.
#
# A trailing `#` comment does not close that window: `ref:  # pinned` still
# takes its value from the line below, and YAML also allows a comment after a
# block header (`ref: |  # pinned`). Requiring end-of-line right after the key
# read both as ordinary scalars and lost the continuation. Not after an opening
# QUOTE, though — there a `#` is string content, not a comment.
_REF_KEY_OPEN_RE = re.compile(
    r"""^\s*(['"]?)ref\1\s*:[^\S\n]*(?:["'][^\S\n]*$|(?:[|>][+-]?\d*)?[^\S\n]*(?:#.*)?$)"""
)
_INPUT_MENTION_RE = re.compile(r"""inputs\.%s\b""" % INPUT_NAME)
# The guard step's signature: it takes the ref through `env:` (never
# interpolated into the script body) under this one name, used nowhere else.
# Block form only, deliberately: a guard written in flow style reads as ABSENT,
# which fails the lint loudly instead of passing a checkout it never verified.
# A trailing comment IS tolerated — unlike the flow form that is a real guard
# doing its job, so rejecting it would fail a compliant workflow, not catch one.
_GUARD_RE = re.compile(
    r"""^\s*(['"]?)WORKFLOWS_REF\1\s*:\s*(['"]?)\$\{\{\s*inputs\.workflows_ref\s*\}\}\2"""
    r"""[^\S\n]*(?:#.*)?$"""
)
# A mapping value that IS the input (`ref:`/`WORKFLOWS_REF:` etc.) — used to
# tell "not applicable" apart from "the parser lost this file". Deliberately
# narrower than "the string appears somewhere": the test workflow's own shell
# fixtures name the input in prose and in a `sed` script, and neither is a use.
# Flow form included for the same reason as above — otherwise a file whose only
# use is one-line escapes the "NOT covering this file" error too.
# (`:[^\S\n]*(?:#…)?\s*` rather than a plain `\s*`, so a comment sitting between
# the key and a value on the next line does not hide the use — the same gap, in
# the backstop that is supposed to catch exactly this kind of miss.)
_CONSUMES_BLOCK_RE = re.compile(
    r"""(?m)^\s*(['"]?)[\w.-]+\1\s*:[^\S\n]*(?:#[^\n]*)?\s*"""
    r"""(['"]?)\$\{\{\s*inputs\.%s\s*\}\}\2\s*$""" % INPUT_NAME
)
_CONSUMES_FLOW_RE = re.compile(
    r"""[{,]\s*(['"]?)[\w.-]+\1\s*:\s*(['"]?)\$\{\{\s*inputs\.%s\s*\}\}\2\s*[,}]""" % INPUT_NAME
)
# The block-scalar form, for the same reason again. (The plain multi-line form
# already lands in _CONSUMES_BLOCK_RE, whose `\s*` spans the newline; only the
# `|`/`>` indicator sits between the colon and the value and defeats it.)
_CONSUMES_SCALAR_RE = re.compile(
    r"""(?m)^\s*(['"]?)[\w.-]+\1\s*:\s*[|>][+-]?\d*[^\S\n]*(?:#[^\n]*)?\n"""
    r"""\s*\$\{\{\s*inputs\.%s\s*\}\}""" % INPUT_NAME
)

# A `default` key inside a flow mapping: `{type: string, default: main}`.
_FLOW_DEFAULT_RE = re.compile(r"""[{,]\s*(['"]?)default\1\s*:""")

# A `#` opens a comment at the start of a value or after whitespace.
_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")


def is_ref_use(line):
    """True when `line` checks out at the input — block or flow-mapping form."""
    return bool(_REF_USE_BLOCK_RE.match(line) or _REF_USE_FLOW_RE.search(line))


def _consumes_input(text):
    """True when `text` uses the input as a mapping value in any YAML style."""
    return bool(
        _CONSUMES_BLOCK_RE.search(text)
        or _CONSUMES_FLOW_RE.search(text)
        or _CONSUMES_SCALAR_RE.search(text)
    )


def _strip_comment(value):
    """Drop a trailing `# …` comment from a scalar value."""
    return _COMMENT_RE.sub("", value).strip()


def _is_skippable(line):
    """Blank lines and whole-line comments never open or close a YAML block."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _key_re(indent, key):
    """`key:` at exactly `indent`, bare or quoted (both are valid Actions YAML)."""
    return re.compile(r"""^ {%d}(['"]?)%s\1\s*:""" % (indent, re.escape(key)))


def _block_body(lines, start, indent):
    """Yield (lineno, line) for the block nested under `lines[start]`.

    The block runs until the first non-skippable line indented at or above
    (i.e. numerically at or below) `indent` — that line belongs to the parent.
    """
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if _is_skippable(line):
            continue
        if _indent(line) <= indent:
            return
        yield i, line


def _find_key(lines, key, start, indent, stop_indent):
    """Line index of `key:` at exactly `indent` inside the block opened at `start`.

    Returns None if the block ends (a line at or shallower than `stop_indent`)
    before the key appears.
    """
    pattern = _key_re(indent, key)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if _is_skippable(line):
            continue
        if _indent(line) <= stop_indent:
            return None
        if pattern.match(line):
            return i
    return None


def _first_child_indent(lines, start, indent):
    """Indent of the first child line under `lines[start]`, or None if childless."""
    for _, line in _block_body(lines, start, indent):
        return _indent(line)
    return None


def find_workflows_ref_defaults(lines):
    """Line numbers (1-based) of `default:` keys inside the input's block.

    Returns None when the file is not a `workflow_call` workflow declaring a
    `workflows_ref` input — i.e. "nothing to check here", which is distinct
    from the empty list ("checked, and clean").
    """
    on_line = None
    for i, line in enumerate(lines):
        if _is_skippable(line):
            continue
        match = _ON_RE.match(line)
        if match and _indent(line) == 0:
            # `on: [push]` / `on: push` inline forms declare no workflow_call
            # inputs, so only the block form can hold what we look for. A
            # trailing comment (`on:  # triggers`) is NOT an inline value —
            # treating it as one would silently drop the file from the lint.
            if _strip_comment(match.group(2)):
                return None
            on_line = i
            break
    if on_line is None:
        return None

    on_child = _first_child_indent(lines, on_line, 0)
    if on_child is None:
        return None
    call_line = _find_key(lines, "workflow_call", on_line, on_child, 0)
    if call_line is None:
        return None

    call_child = _first_child_indent(lines, call_line, on_child)
    if call_child is None:
        return None
    inputs_line = _find_key(lines, "inputs", call_line, call_child, on_child)
    if inputs_line is None:
        return None

    input_indent = _first_child_indent(lines, inputs_line, call_child)
    if input_indent is None:
        return None
    ref_line = _find_key(lines, INPUT_NAME, inputs_line, input_indent, call_child)
    if ref_line is None:
        return None

    hits = []

    # The flow-mapping form puts the whole input on one line:
    #   workflows_ref: {type: string, default: main}
    # It has no child lines at all, so the block scan below would call it clean
    # — and it is the shortest possible way to write the regression.
    inline = _strip_comment(lines[ref_line].split(":", 1)[1])
    if inline.startswith("{") and _FLOW_DEFAULT_RE.search(inline):
        hits.append(ref_line + 1)

    # The input's own block: from the `workflows_ref:` line down to the next key
    # at the same indentation (the next input, or the end of the inputs map).
    # Only lines at the input's OWN property indent count — a `default:` deeper
    # than that belongs to something else, e.g. a wrapped line of a folded
    # `description: >-` scalar, which must not fail a compliant workflow.
    prop_indent = _first_child_indent(lines, ref_line, input_indent)
    if prop_indent is not None:
        default_re = _key_re(prop_indent, "default")
        hits.extend(
            i + 1
            for i, line in _block_body(lines, ref_line, input_indent)
            if default_re.match(line)
        )
    return hits


def find_unguarded_ref_checkouts(lines):
    """1-based line numbers of `ref: ${{ inputs.workflows_ref }}` uses with no guard.

    A use is guarded when the empty-ref guard step appears earlier in the SAME
    job — jobs run independently, so a guard in job A does nothing for job B.
    """
    jobs_line = None
    for i, line in enumerate(lines):
        if _is_skippable(line):
            continue
        if _indent(line) == 0 and _JOBS_RE.match(line):
            jobs_line = i
            break
    if jobs_line is None:
        return []

    job_indent = _first_child_indent(lines, jobs_line, 0)
    if job_indent is None:
        return []
    job_starts = [
        i
        for i, line in _block_body(lines, jobs_line, 0)
        if _indent(line) == job_indent
    ]

    unguarded = []
    for start in job_starts:
        guarded = False
        # An open `ref:` whose value continues below, as (line index, indent).
        # Continuation lines are the more-indented ones that follow; the first
        # line back at or above the key's indent closes the scalar.
        pending = None
        for i, line in _block_body(lines, start, job_indent):
            if pending is not None:
                if _indent(line) > pending[1]:
                    if _INPUT_MENTION_RE.search(line):
                        if not guarded:
                            unguarded.append(pending[0] + 1)
                        pending = None
                    continue
                # Scalar closed — fall through and judge this line normally.
                pending = None
            if _GUARD_RE.match(line):
                guarded = True
            elif is_ref_use(line):
                if not guarded:
                    unguarded.append(i + 1)
            elif _REF_KEY_OPEN_RE.match(line):
                pending = (i, _indent(line))
    return unguarded


def check_dir(workflows_dir, exempt=KNOWN_EXEMPT):
    """Returns (errors, checked, exempt_ok) — errors are annotation-ready strings."""
    errors = []
    checked = []
    exempt_ok = []
    seen_exempt = set()

    names = sorted(
        n
        for n in os.listdir(workflows_dir)
        if n.endswith((".yml", ".yaml")) and os.path.isfile(os.path.join(workflows_dir, n))
    )
    for name in names:
        path = os.path.join(workflows_dir, name)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        lines = text.split("\n")

        defaults = find_workflows_ref_defaults(lines)
        if defaults is None:
            # "Nothing to check" — unless the file plainly USES the input, in
            # which case the text parser lost a declaration that must exist and
            # this file is silently uncovered. Fail loudly instead.
            if _consumes_input(text):
                errors.append(
                    "::error file=%s::%s references `inputs.%s` but the checker "
                    "could not find its input declaration — the lint is NOT "
                    "covering this file. Fix the workflow's shape or teach "
                    ".github/workflow-pins/check_workflow_pins.py the new one."
                    % (path, name, INPUT_NAME)
                )
            continue
        checked.append(name)

        if name in exempt:
            seen_exempt.add(name)
            if defaults:
                exempt_ok.append(name)
            else:
                errors.append(
                    "::error file=%s::%s is in KNOWN_EXEMPT but its %s input no "
                    "longer has a default — delete it from KNOWN_EXEMPT in "
                    ".github/workflow-pins/check_workflow_pins.py"
                    % (path, name, INPUT_NAME)
                )
            # An exempt workflow still has its default, so an omitted input can
            # never reach checkout as ''. The guard is moot until its own ticket
            # drops the default — which puts it back under the check below.
            continue

        for lineno in defaults:
            errors.append(
                "::error file=%s,line=%d::%s declares a `default:` for the `%s` "
                "input. Delete it (and keep `required: true` + the runtime "
                "empty-ref guard): a default lets a caller SHA-pin `uses:` while "
                "loading scripts from a mutable ref. See BE-5546."
                % (path, lineno, name, INPUT_NAME)
            )

        for lineno in find_unguarded_ref_checkouts(lines):
            errors.append(
                "::error file=%s,line=%d::%s checks out at `ref: ${{ inputs.%s }}` "
                "with no empty-ref guard earlier in the same job. Copy the "
                "`Require a pinned workflows_ref` step in ahead of it: `required: "
                "true` is unenforced for workflow_call, so an omitted input "
                "arrives as '' and checkout silently takes the default branch. "
                "See BE-5546." % (path, lineno, name, INPUT_NAME)
            )

    # A KNOWN_EXEMPT entry naming a workflow that no longer declares the input
    # at all — renamed, deleted, or fixed. Left alone it would silently
    # pre-exempt whatever future workflow reuses the filename.
    for name in sorted(set(exempt) - seen_exempt):
        errors.append(
            "::error::%s is in KNOWN_EXEMPT but no workflow in %s declares a `%s` "
            "input under that name (renamed, deleted, or already fixed) — delete "
            "it from KNOWN_EXEMPT in "
            ".github/workflow-pins/check_workflow_pins.py"
            % (name, workflows_dir, INPUT_NAME)
        )

    return errors, checked, exempt_ok


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workflows-dir",
        default=DEFAULT_WORKFLOWS_DIR,
        help="Directory of workflow files to check (default: %s)." % DEFAULT_WORKFLOWS_DIR,
    )
    args = parser.parse_args(argv)

    if not os.path.isdir(args.workflows_dir):
        print("::error::no such directory: %s" % args.workflows_dir)
        return 2

    # KNOWN_EXEMPT names files in THIS repo's workflows dir, and the staleness
    # check reads "an entry with no matching workflow is dead". Applied to an
    # ad-hoc --workflows-dir (a fixture, another repo) every entry looks stale,
    # so the list only applies where it means something.
    exempt = KNOWN_EXEMPT if args.workflows_dir == DEFAULT_WORKFLOWS_DIR else frozenset()
    errors, checked, exempt_ok = check_dir(args.workflows_dir, exempt=exempt)

    for name in checked:
        note = " (KNOWN_EXEMPT — tracked separately)" if name in exempt_ok else ""
        print("checked %s%s" % (name, note))
    if not checked:
        print("no reusable workflow declares a `%s` input" % INPUT_NAME)

    for err in errors:
        print(err)
    if errors:
        print(
            "\n%d problem(s): a reusable workflow's `%s` input must have NO "
            "default, and every job checking out at it must guard against an "
            "empty value first." % (len(errors), INPUT_NAME)
        )
        return 1

    print(
        "\nOK — %d workflow(s) declare `%s`, none with a default, every ref "
        "checkout guarded (%d exempt)."
        % (len(checked), INPUT_NAME, len(exempt_ok))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
