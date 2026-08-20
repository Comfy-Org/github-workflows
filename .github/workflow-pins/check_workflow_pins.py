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
# Empty as of BE-5858: the list drained itself exactly as designed, and every
# reusable workflow here is now held to both checks with no carve-out.
KNOWN_EXEMPT = frozenset()

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
# How the guard RECEIVES the ref: through `env:` (never interpolated into the
# script body) under this one name. Half the signature — `is_guard_step` below
# checks the other half, that the step actually rejects an empty value.
# Block form only, deliberately: a guard written in flow style reads as ABSENT,
# which fails the lint loudly instead of passing a checkout it never verified.
# A trailing comment IS tolerated — unlike the flow form that is a real guard
# doing its job, so rejecting it would fail a compliant workflow, not catch one.
# BOTH bindings count: the bare input (the BE-5546 `required: true` jobs) and the
# BE-8077 `|| job.workflow_sha` fallback (groom.yml's seven guards). Matching
# only the bare form left every one of those seven UNCONSULTED — the checkouts
# passed on the fallback exemption alone, so deleting all seven guards kept this
# lint green, while dropping `|| job.workflow_sha` from one checkout reported it
# unguarded with a correct guard directly above it. Both halves of that are the
# lint failing at its own subject.
_GUARD_BINDING_RE = re.compile(
    r"""^\s*(['"]?)WORKFLOWS_REF\1\s*:\s*(['"]?)\$\{\{\s*inputs\.workflows_ref\s*"""
    r"""(?:\|\|\s*job\.workflow_sha\s*)?\}\}\2[^\S\n]*(?:#.*)?$"""
)
# …but the binding alone is NOT the guard, it is only how the guard receives the
# value. Keying on it by itself made ANY step that merely handles the ref — one
# that echoes it, or clones with it — mark its whole job guarded, so every later
# checkout in that job passed unexamined. That is the lint's own subject failing
# silently, so the step must also be seen to REJECT the empty value: an
# emptiness test and a non-zero exit, both inside that same step.
# And the two halves must be about the SAME thing. "an emptiness test somewhere,
# a non-zero exit somewhere" passes a step that tests an unrelated variable and
# exits on an unrelated condition — a near-match, and a likelier accident than
# the bare decoy. So the `-z` must name the ref (or a variable derived from it)
# and the exit must sit in THAT test's branch.
_GUARD_FAIL_RE = re.compile(r"""^\s*exit\s+[1-9]""")
# A command that IS `exit N`, for a branch written inline. The multiline path
# anchors `exit` at the start of its line, so a conditional `[ … ] && exit 1`
# does not count there; the inline paths hold to the same rule by requiring a
# `;`-separated command that is nothing but the exit.
_GUARD_FAIL_INLINE_RE = re.compile(r"""^\s*exit\s+[1-9]\d*\s*$""")


def _exits_unconditionally(text):
    """True when `text` contains a bare `exit N` as one of its `;` commands."""
    return any(_GUARD_FAIL_INLINE_RE.match(part) for part in text.split(";"))
_SHELL_ASSIGN_RE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_]\w*)=(.*)$""")
_BRANCH_END_RE = re.compile(r"""^\s*(?:fi|else|elif)\b""")
# The same boundary mid-line, for a branch written inline after `then`.
_INLINE_BRANCH_END_RE = re.compile(r"""\b(?:fi|else|elif)\b""")
# Nesting, so a conditional `exit` one level in is not read as the branch's own.
_IF_OPEN_RE = re.compile(r"""\bif\b""")
_FI_RE = re.compile(r"""\bfi\b""")
# Step-level keys that can stop a correct guard from actually guarding.
_STEP_IF_RE = re.compile(r"""^\s*(['"]?)if\1\s*:""")
_STEP_CONTINUE_RE = re.compile(r"""^\s*(['"]?)continue-on-error\1\s*:\s*(.*)$""")


def _empty_test_re(names):
    """`[ -z "$NAME" ]` / `[[ -z $NAME ]]` / `test -z …` for any of `names`."""
    alt = "|".join(sorted(re.escape(n) for n in names))
    return re.compile(r"""(?:\[\[?|\btest)\s+-z\s+"?\$\{?(?:%s)\b""" % alt)


def _length_ne_test_re(names):
    """`${#NAME} -ne <N>` for `N > 0` — an empty NAME has length 0, so this
    rejects it exactly like `-z`, just spelled as a shape check (e.g. "must be
    a full 40-hex SHA") rather than an emptiness check. pr-risk.yml's guard
    uses this form to reject both an empty ref AND a malformed one in one test.
    """
    alt = "|".join(sorted(re.escape(n) for n in names))
    return re.compile(r"""^\$\{#(?:%s)\}\s*-ne\s*[1-9]\d*$""" % alt)


def _bare_empty_test_re(names):
    """`-z "$NAME"` with no enclosing `[ … ]`/`[[ … ]]`/`test` — the shape an
    OR-branch is left in once the enclosing brackets are stripped for `_or_terms`.
    """
    alt = "|".join(sorted(re.escape(n) for n in names))
    var = r""""?\$\{?(?:%s)\}?"?""" % alt
    return re.compile(r"""^-z\s+%s$""" % var)


def _or_terms(cond):
    """Split a `[[ … ]]` / `[ … ]` condition into its top-level `||` branches.

    `||` only WIDENS a condition (adds failure cases) — never narrows it like
    `&&` does — so if ONE branch alone guarantees rejection of an empty ref,
    the whole OR'd condition does too, whatever the other branches test.
    """
    inner = cond.strip()
    if inner.startswith("[[") and inner.endswith("]]"):
        inner = inner[2:-2]
    elif inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return [term.strip() for term in inner.split("||")]


def _rejects_empty_ref(cond, names):
    """True when `cond` (the whole bracketed test) or some top-level OR-branch
    of it guarantees rejection of an empty value in `names` — the canonical
    `-z` whole-test, or the length-check shape above. Both are string-equality
    on the WHOLE branch, matching `_whole_empty_test_re`'s own fail-closed
    stance on compounds: a branch that merely CONTAINS one of these (e.g.
    `-z "$REF" && "$OTHER" = x`) does not count, only one that IS it.
    """
    if _whole_empty_test_re(names).match(cond.strip()):
        return True
    bare_re = _bare_empty_test_re(names)
    length_re = _length_ne_test_re(names)
    return any(bare_re.match(term) or length_re.match(term) for term in _or_terms(cond))


def _whole_empty_test_re(names):
    """The same test as the ENTIRE condition — nothing ANDed onto it.

    `if [ -z "$REF" ] && [ "$OTHER" = blocked ]; then exit 1; fi` contains the
    emptiness test but does not fail for every empty ref: empty + `OTHER`
    unset falls through to the checkout. A text lint cannot evaluate shell, so
    it accepts only a condition that is exactly the emptiness test and rejects
    every compound as ambiguous — including a widening `||`, which is safe in
    fact but not worth a special case in a detector that fails closed.
    """
    alt = "|".join(sorted(re.escape(n) for n in names))
    var = r""""?\$\{?(?:%s)\}?"?""" % alt
    return re.compile(
        r"""^(?:\[\[?\s+-z\s+%s\s+\]\]?|test\s+-z\s+%s)$""" % (var, var)
    )


# An `if`/`elif` split into its condition and whatever follows `then` (which is
# the branch body itself when the whole statement is written on one line).
_IF_COND_RE = re.compile(r"""^\s*(?:el)?if\s+(.*?)\s*;?\s*then\b(.*)$""")
# A single-line `run:` puts the shell on the key's own line — the `run:` is
# YAML, not part of the condition being judged.
_RUN_PREFIX_RE = re.compile(r"""^(['"]?)run\1\s*:\s*""")


def _ref_derived_names(body):
    """Shell variables carrying the ref: `WORKFLOWS_REF` and anything set from it.

    The real guard tests `$REF`, assigned from `$WORKFLOWS_REF` through a
    `printf | tr` strip, so following one assignment hop is what makes the test
    recognizable at all — but only a hop that actually carries the value.
    """
    names = {"WORKFLOWS_REF"}
    for _ in range(3):  # a short chain of derivations; converges immediately
        grew = False
        for line in body:
            match = _SHELL_ASSIGN_RE.match(line)
            if not match or match.group(1) in names:
                continue
            if re.search(r"""\$\{?(?:%s)\b""" % "|".join(sorted(names)), match.group(2)):
                names.add(match.group(1))
                grew = True
        if not grew:
            break
    return names
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

# An `env:` binding of the input to a NAME (`WORKFLOWS_REF: ${{ inputs… }}`).
# A checkout does not have to name the input directly: hoist it to a job-level
# `env:` — the natural refactor once several steps want it — and every
# `ref: ${{ env.WORKFLOWS_REF }}` below reads as no ref use at all, dropping
# the very checkouts this lint exists to cover. So the names bound to the input
# are collected first, and a `ref:` reaching one of them counts as a use.
_ENV_ALIAS_RE = re.compile(
    r"""^\s*(['"]?)([A-Za-z_]\w*)\1\s*:[^\S\n]*"""
    r"""(['"]?)\$\{\{\s*inputs\.%s\s*\}\}\3[^\S\n]*(?:#.*)?$""" % INPUT_NAME
)
# Scoped to `env:` blocks, not every mapping key bound to the input: the
# checkout's own `ref: ${{ inputs.workflows_ref }}` is such a binding too, and
# treating `ref` as an alias would make `env.ref`/`$ref` anywhere read as the
# input. (Block form only — a flow-style `env: {…}` binds no alias here, which
# loses nothing the `_CONSUMES_*` backstop does not already catch.)
_ENV_KEY_RE = re.compile(r"""^\s*(['"]?)env\1\s*:[^\S\n]*(?:#.*)?$""")

# A `default` key inside a flow mapping: `{type: string, default: main}`.
_FLOW_DEFAULT_RE = re.compile(r"""[{,]\s*(['"]?)default\1\s*:""")

# A `#` opens a comment at the start of a value or after whitespace.
_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")

# The BE-4169 self-pinning fallback (see its use in check_dir): the LITERAL
# expression `inputs.workflows_ref || job.workflow_sha`, no other spelling.
# `job.workflow_sha` is the exact commit THIS reusable workflow was resolved
# from via the caller's `uses:` pin — never mutable — so an input defaulted to
# `''` and OR'd with it this way can't reach checkout on a moving ref, the same
# guarantee `required: true` + the runtime guard buys, bought a different way.
#
# It is `job.workflow_sha` and NOT `github.job_workflow_sha` (BE-8077). The
# latter is what this regex accepted until then, and it is a trap: it is an
# OIDC token CLAIM, not a property of the `github` context, so Actions expands
# it to '' and `actions/checkout` reads `ref: ''` as "the default branch" — the
# precise hole this lint exists to close, blessed by the lint itself. The
# populated accessor is the `job`-context one added in runner v2.334.0 (Apr
# 2026). Anything still spelling it the old way is now FLAGGED, not exempt.
#
# `job.workflow_sha` is only never-empty on a current runner, so unlike the
# BE-4169 story this expression is not self-sufficient: groom.yml pairs every
# one of these checkouts with a fail-closed empty-ref guard step, and
# cursor-review.yml's ledger job (which must never fail) resolves it in a step
# that warns and skips the checkout. This exemption is about the ref not being
# MUTABLE; those runtime guards cover the empty case.
# ANCHORED to the close of the interpolation, and matched against the
# comment-stripped line (`_pins_to_job_workflow_sha` below). Unanchored, this
# read "contains the fallback" rather than "IS the fallback", so
# `${{ inputs.workflows_ref || job.workflow_sha || \'main\' }}` was exempted by
# both users of this regex — and that expression resolves to the mutable default
# branch in exactly the pre-v2.334.0 case the fallback exists for. Requiring
# `}}` means a third operand is a hole again, which is the whole point.
_JOB_WORKFLOW_SHA_FALLBACK_RE = re.compile(
    r"""inputs\.%s\s*\|\|\s*job\.workflow_sha\s*\}\}""" % INPUT_NAME
)


def _pins_to_job_workflow_sha(line):
    """True when `line`'s CODE — not its comments — uses the BE-4169 fallback.

    Comments are stripped first because `check_dir` asks this of every line in
    the file: prose merely NAMING the expression (this repo\'s workflows discuss
    it at length) would otherwise buy the `default: \'\'` carve-out for a file
    where no checkout uses it at all.
    """
    return bool(_JOB_WORKFLOW_SHA_FALLBACK_RE.search(_strip_comment(line)))


def _default_value(line):
    """The comment-stripped RHS of a `default:` key line."""
    return _strip_comment(line.split(":", 1)[1])


def env_aliases(lines):
    """Names bound to the input by an `env:` mapping, e.g. `WORKFLOWS_REF`."""
    names = set()
    for i, line in enumerate(lines):
        if not _ENV_KEY_RE.match(line):
            continue
        for _, child in _block_body(lines, i, _indent(line)):
            match = _ENV_ALIAS_RE.match(child)
            if match:
                names.add(match.group(2))
    return frozenset(names)


def _mention_alt(aliases):
    """Regex alternation for "reaches the input" — directly or via an alias.

    File-wide rather than scope-aware on purpose: `env:` is scoped per job and
    per step, but over-approximating can only ever DEMAND a guard, never excuse
    a missing one — the safe direction for a detector whose job is absence.
    """
    alt = r"""inputs\.%s\b""" % INPUT_NAME
    if aliases:
        names = "|".join(sorted(re.escape(a) for a in aliases))
        alt += r"""|env\.(?:%s)\b|\$\{?(?:%s)\b""" % (names, names)
    return alt


def _ref_use_res(aliases):
    """The (block, flow) `ref:` patterns, widened to the input's env aliases."""
    alt = _mention_alt(aliases)
    return (
        re.compile(r"""^\s*(['"]?)ref\1\s*:.*(?:%s)""" % alt),
        re.compile(r"""[{,]\s*(['"]?)ref\1\s*:[^,}]*(?:%s)""" % alt),
    )


def is_ref_use(line, res=None):
    """True when `line` checks out at the input — block or flow-mapping form."""
    block_re, flow_re = res or (_REF_USE_BLOCK_RE, _REF_USE_FLOW_RE)
    return bool(block_re.match(line) or flow_re.search(line))


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


def _step_bounds(lines, idx):
    """(start, end, key_indent) of the STEP whose `env:` holds the binding at `idx`.

    None when the binding is not inside a step at all — a job-level `env:`
    hoists the value out of every step, which is a binding but not a guard.
    """
    ind = _indent(lines[idx])
    key_indent = None  # the step's own key column, i.e. where `env:`/`run:` sit
    for j in range(idx - 1, -1, -1):
        if _is_skippable(lines[j]):
            continue
        if _indent(lines[j]) < ind:
            key_indent = _indent(lines[j])
            break
    if key_indent is None:
        return None

    start = None  # the step's `- …` list-item line
    for j in range(idx, -1, -1):
        if _is_skippable(lines[j]) or _indent(lines[j]) >= key_indent:
            continue
        if lines[j].lstrip().startswith("- "):
            start = j
        break  # first shallower line decides it: a step, or not one at all
    if start is None:
        return None

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _is_skippable(lines[j]):
            continue
        if _indent(lines[j]) < key_indent:
            end = j
            break
    return start, end, key_indent


def is_guard_step(lines, idx):
    """True when the binding at `idx` sits in a step that REJECTS an empty ref.

    Fail-closed: a step the parser cannot resolve, or one that takes the value
    without testing it, is not a guard — which reports the checkout it precedes
    rather than passing a checkout nothing verified.
    """
    bounds = _step_bounds(lines, idx)
    if bounds is None:
        return False
    start, end, key_indent = bounds
    body = lines[start:end]

    # Two Actions-level ways a perfectly-written guard still guards nothing —
    # and they never touch the shell, so every check below would pass them:
    # `continue-on-error: true` means the `exit 1` does not fail the job and
    # the checkout runs anyway, and a step-level `if:` can skip the guard
    # outright for some events while the checkout still runs. Neither is
    # evaluable here, so both disqualify the step rather than being assumed
    # benign.
    for i, line in enumerate(body):
        if _indent(line) != key_indent:
            continue
        if _STEP_IF_RE.match(line):
            return False
        cont = _STEP_CONTINUE_RE.match(line)
        if cont:
            value = _strip_comment(cont.group(2))
            if not value:
                # A comment-only (or bare) `continue-on-error:` is not
                # necessarily unset — YAML lets the real scalar continue on
                # the next, more-indented line, and Actions reads THAT as
                # the value. Stopping at the colon would read this as
                # absent and assume `false`, when the continuation can just
                # as well say `true`.
                nxt = body[i + 1] if i + 1 < len(body) else ""
                if _indent(nxt) > key_indent and not _is_skippable(nxt):
                    value = _strip_comment(nxt.strip())
            if value.lower() not in ("false", ""):
                return False
    names = _ref_derived_names(body)
    empty_re = _empty_test_re(names)
    whole_re = _whole_empty_test_re(names)
    length_mention_re = re.compile(
        r"""\$\{#(?:%s)\}""" % "|".join(sorted(re.escape(n) for n in names))
    )
    for i, line in enumerate(body):
        if not (empty_re.search(line) or length_mention_re.search(line)):
            continue
        code = _RUN_PREFIX_RE.sub("", line.strip())
        cond_match = _IF_COND_RE.match(code)
        if cond_match:
            # An `if`: the emptiness test must BE (or OR-widen) the condition.
            if not _rejects_empty_ref(cond_match.group(1).strip(), names):
                continue
            # `if [ -z "$REF" ]; then exit 1; fi` all on one line. The branch
            # still ends at its `fi` — `then echo "missing"; fi; exit 1` exits
            # AFTER the branch, so the empty ref never triggers it. Same
            # boundary the multiline path below applies, which is where this
            # inline path had quietly stopped agreeing with it.
            inline = _INLINE_BRANCH_END_RE.split(cond_match.group(2), 1)[0]
            if _exits_unconditionally(inline):
                return True
            # Otherwise the exit must be inside this test's own branch, which
            # ends at the matching `fi`/`else` — an exit after it answers to
            # something else entirely.
            # …at the branch's OWN depth. An `exit` nested inside an inner
            # `if` is conditional on that inner test, so the empty ref can
            # still fall through — the multiline twin of the inline rule above.
            depth = 0
            for rest in body[i + 1:]:
                if depth == 0:
                    if _BRANCH_END_RE.match(rest):
                        break
                    if _GUARD_FAIL_RE.match(rest):
                        return True
                # `\bif\b` does not match inside `elif`, and a nested one-liner
                # `if …; then …; fi` opens and closes on the same line.
                depth = max(0, depth + len(_IF_OPEN_RE.findall(rest)) - len(_FI_RE.findall(rest)))
        else:
            # The one-liner `[ -z "$REF" ] && exit 1` — everything left of the
            # first `&&` is the condition, and it is held to the same rule.
            # The exit must be the command the `&&` actually reaches, so only
            # the first `;`-segment counts: `… && echo warn; … && exit 1`
            # leaves the empty ref walking on.
            head, sep, tail = code.partition("&&")
            if sep and _rejects_empty_ref(head.strip(), names):
                if _GUARD_FAIL_INLINE_RE.match(tail.split(";")[0]):
                    return True
    return False


def find_unguarded_ref_checkouts(lines):
    """1-based line numbers of `ref: ${{ inputs.workflows_ref }}` uses with no guard.

    A use is guarded when the empty-ref guard step appears earlier in the SAME
    job — jobs run independently, so a guard in job A does nothing for job B.
    """
    aliases = env_aliases(lines)
    ref_res = _ref_use_res(aliases)
    mention_re = re.compile(_mention_alt(aliases))
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
                    if mention_re.search(line):
                        if not guarded:
                            unguarded.append(pending[0] + 1)
                        pending = None
                    continue
                # Scalar closed — fall through and judge this line normally.
                pending = None
            if _GUARD_BINDING_RE.match(line):
                guarded = guarded or is_guard_step(lines, i)
            elif is_ref_use(line, ref_res):
                # NO fallback exception here, deliberately (BE-8077). The
                # BE-4169 `inputs.workflows_ref || job.workflow_sha` form
                # cannot resolve to a MUTABLE ref — that is what earns it the
                # `default: ''` carve-out in `check_dir` — but it is NOT
                # self-sufficient the way that story assumed:
                # `job.workflow_sha` needs runner v2.334.0+ and expands to ''
                # on anything older, and checkout reads `ref: ''` as the
                # DEFAULT BRANCH. So the fallback answers MUTABILITY and the
                # guard answers EMPTINESS, and this lint has to require both.
                # Exempting the fallback from the guard check meant deleting
                # every one of groom.yml's seven guard steps kept this lint
                # green, while the comments here had already made the
                # empty-ref safety depend on them.
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

        # A safe alternative to `required: true` alone (BE-4169, corrected by
        # BE-8077): default the input to '' and OR every checkout's `ref:` with
        # `job.workflow_sha` — the exact commit THIS reusable workflow was itself
        # resolved from via the caller's `uses:` pin. That value is never
        # mutable, so an omitted `workflows_ref` self-pins instead of silently
        # taking the default branch, which is what makes the `default: ''`
        # tolerable here. `find_unguarded_ref_checkouts` already recognizes the
        # checkout side of this (the LITERAL fallback `inputs.workflows_ref ||
        # job.workflow_sha`); this covers the matching `default: ''`, which is
        # otherwise indistinguishable from the `default: main` hole. See
        # groom.yml — which ALSO runs a fail-closed empty-ref guard in every one
        # of those jobs, because `job.workflow_sha` needs runner v2.334.0+ and
        # expands to '' on anything older. The old `github.job_workflow_sha`
        # spelling is deliberately NOT accepted: it is an OIDC claim and expands
        # to '' on EVERY runner, so a file "self-pinning" with it pins nothing.
        self_pins_to_job_workflow_sha = any(
            _pins_to_job_workflow_sha(line) for line in lines
        )

        for lineno in defaults:
            if self_pins_to_job_workflow_sha and _default_value(lines[lineno - 1]) in ("''", '""'):
                continue
            errors.append(
                "::error file=%s,line=%d::%s declares a `default:` for the `%s` "
                "input. Delete it (and keep `required: true` + the runtime "
                "empty-ref guard): a default lets a caller SHA-pin `uses:` while "
                "loading scripts from a mutable ref. See BE-5546."
                % (path, lineno, name, INPUT_NAME)
            )

        for lineno in find_unguarded_ref_checkouts(lines):
            if _pins_to_job_workflow_sha(lines[lineno - 1]):
                # The `|| job.workflow_sha` form: immutable, but empty on a
                # pre-v2.334.0 runner, so it still needs the guard (BE-8077).
                errors.append(
                    "::error file=%s,line=%d::%s checks out at `ref: ${{ inputs.%s "
                    "|| job.workflow_sha }}` with no empty-ref guard earlier in the "
                    "same job. The fallback stops the ref being MUTABLE, not being "
                    "EMPTY: `job.workflow_sha` needs an Actions runner >= v2.334.0 "
                    "and expands to '' on anything older, which checkout reads as "
                    "the default branch. Pair it with a `Require a resolvable "
                    "workflows_ref` step. See BE-8077."
                    % (path, lineno, name, INPUT_NAME)
                )
                continue
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
