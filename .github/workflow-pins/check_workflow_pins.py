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

Check (2) follows the ref through an earlier step, too (BE-8130): a job that
must never fail cannot run a fail-closed guard, so it resolves the ref in a
warn-only step and checks out at `ref: ${{ steps.<id>.outputs.<name> }}`. That
`ref:` names no input, so it used to leave the lint entirely — with nothing but
a hand-written `if:` between an unresolvable ref and a silent default-branch
checkout of the scripts the job EXECUTES. The idiom is documented (see
`.github/workflow-pins/README.md`), so copies of it inherit the coverage now
rather than the blind spot.

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

# A bracket-index accessor for an alternation of names — `[ 'NAME' ]`,
# `[ "NAME" ]`, or `[ ''NAME'' ]` — shared by every "does this NAME the input
# (or an alias of it)" pattern below. `\s*` because Actions allows whitespace
# around an index expression (`inputs[ 'workflows_ref' ]` is the same access
# as the tight spelling, and `inputs ['workflows_ref']` — space before the
# bracket too — is the same access again); a near-miss that reads as no ref
# use at all is the exact failure mode BE-8146 exists to close. The doubled
# `''NAME''` alternative is not a typo: a YAML *single*-quoted scalar escapes
# an inner `'` by doubling it, so `ref: '${{ inputs[''workflows_ref''] }}'`
# — the whole value single-quoted, forcing that escape — decodes to the exact
# same bracket access as the unescaped spelling and must read the same way.
def _bracket_body(name_alt):
    return r"""\[\s*(?:''(?:%s)''|'(?:%s)'|"(?:%s)")\s*\]""" % (
        name_alt,
        name_alt,
        name_alt,
    )


# How a workflow can NAME the input, defined ONCE (BE-8146). Actions expression
# syntax offers two interchangeable accessors for the same value — property
# (`inputs.workflows_ref`) and index (`inputs['workflows_ref']`) — so a lint
# that knows only the first reads a bracket-spelled checkout as no ref use at
# all and passes it unguarded. Every "does this reach the input" pattern is
# built from this body so the two spellings cannot drift apart again:
# `_REF_USE_*`, `_mention_alt`, `_ENV_ALIAS_RES`, and the `_CONSUMES_*` trio.
#
# Widening what counts as a ref USE is the safe direction (see `_mention_alt`):
# it can only ever DEMAND a guard, never excuse a missing one. `_GUARD_BINDING_RE`
# uses it too now, but only on the ACCESSOR SPELLING, which carries none of that
# risk — it is still the exact same value, `inputs.workflows_ref`, just written
# a different way. What stays deliberately narrow there is the `fallback` group
# (`|| job.workflow_sha`, no other spelling): that is what proves IMMUTABILITY,
# a question the accessor spelling has nothing to do with. `_FALLBACK_RES` (the
# self-pin EXEMPTION) is not widened at all — it would EXCUSE rather than demand.
# (`\s*` before the bracket too — Actions allows whitespace between `inputs`
# and `[`, and `inputs ['workflows_ref']` is the same access as the tight
# spelling.)
_INPUT_MENTION_BODY = r"""inputs\s*\.\s*%s\b|inputs\s*%s""" % (
    INPUT_NAME,
    _bracket_body(re.escape(INPUT_NAME)),
)

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
_REF_USE_BLOCK_RE = re.compile(r"""^\s*(['"]?)ref\1\s*:.*(?:%s)""" % _INPUT_MENTION_BODY)
_REF_USE_FLOW_RE = re.compile(r"""[{,]\s*(['"]?)ref\1\s*:[^,}]*(?:%s)""" % _INPUT_MENTION_BODY)
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

# …and a fourth shape, because a `ref:` does not have to NAME the input at all.
# cursor-review.yml's never-fail ledger job resolves the ref one step earlier
# and the checkout consumes that step's OUTPUT —
#   ref: ${{ steps.resolve_ref.outputs.ref }}
# — which mentions no input, so every pattern above reads it as no ref use and
# the checkout leaves the lint entirely (BE-8130). The README documents this
# resolve-then-consume shape as THE idiom for a job that must never fail, so
# copies of it would inherit the blind spot rather than hit it by accident.
#
# These patterns only IDENTIFY the producing step; they prove nothing on their
# own. `ref_checkouts` resolves `<id>` against the steps it has ALREADY walked
# past in the same job and treats the checkout as a ref use only when that step
# binds the input through `_GUARD_BINDING_RE` — a `ref:` resolved from a step
# unrelated to `workflows_ref` (a `git rev-parse`, a version lookup) is not this
# lint's subject and must not be dragged into it.
#
# Three spellings again, mirroring the `_REF_USE_*` pair plus the continuation
# form: block, flow (bounded at the entry boundary `[^,}]`, exactly as
# `_REF_USE_FLOW_RE` bounds its own value, so a SIBLING entry carrying a step
# output cannot be misread as the ref), and a continuation line that IS the
# expression, for the `ref: >-` / `ref: |` spellings `_REF_KEY_OPEN_RE` opens.
#
# `<id>` and `<out>` each get their own dot/bracket alternative (BE-8146),
# named distinctly per branch (`id`/`id_idx`, `out`/`out_idx`) because Python's
# `re` rejects a duplicate group name even across alternatives that can never
# both match — `steps_output_ref` reads whichever pair actually fired. Bracket
# access on `steps`/`outputs` themselves stays out of scope: unlike `<id>` and
# `<out>`, which are workflow-chosen identifiers an author writes either way,
# the literal property names `steps`/`outputs` are never spelled with a
# bracket in practice, and reaching for it here would be scope with no
# observed shape behind it.
_STEPS_OUTPUT_BODY = (
    r"""\$\{\{\s*(?:steps\.(?P<id>[A-Za-z0-9_-]+)|steps\[\s*['"](?P<id_idx>[A-Za-z0-9_-]+)['"]\s*\])"""
    r"""\.(?:outputs\.(?P<out>[A-Za-z0-9_-]+)|outputs\[\s*['"](?P<out_idx>[A-Za-z0-9_-]+)['"]\s*\])"""
    r"""\s*\}\}"""
)
_STEPS_OUTPUT_BLOCK_RE = re.compile(r"""^\s*(['"]?)ref\1\s*:.*%s""" % _STEPS_OUTPUT_BODY)
_STEPS_OUTPUT_FLOW_RE = re.compile(r"""[{,]\s*(['"]?)ref\1\s*:[^,}]*%s""" % _STEPS_OUTPUT_BODY)
_STEPS_OUTPUT_CONT_RE = re.compile(
    r"""^\s*(?P<q>['"]?)%s(?P=q)[^\S\n]*$""" % _STEPS_OUTPUT_BODY
)
# The producing step's `id:`, read at the step's own key column. Quoted or bare
# — both are valid Actions YAML — and `\S+` stops before a trailing comment on
# its own, because YAML needs whitespace ahead of a `#` for it to open one.
_STEP_ID_RE = re.compile(r"""^\s*(['"]?)id\1\s*:\s*(\S+)""")
# How the guard RECEIVES the ref: through `env:` (never interpolated into the
# script body) under this one name. Half the signature — `is_guard_step` below
# checks the other half, that the step actually rejects an empty value.
# Block form only, deliberately: a guard written in flow style reads as ABSENT,
# which fails the lint loudly instead of passing a checkout it never verified.
# A trailing comment IS tolerated — unlike the flow form that is a real guard
# doing its job, so rejecting it would fail a compliant workflow, not catch one.
#
# BOTH bindings are recognized, but they are NOT equivalent, and the `fallback`
# group is what keeps them apart. A guard on the bare input proves the INPUT is
# non-empty; a guard on `inputs.workflows_ref || job.workflow_sha` proves only
# that the OR EXPRESSION is. Treating the second as blanket job-wide coverage is
# a live hole: with the input omitted the guard passes on `job.workflow_sha`
# while a sibling `ref: ${{ inputs.workflows_ref }}` in the same job still gets
# '' and checkout takes the default branch. `find_unguarded_ref_checkouts`
# therefore records the STRENGTH of each guard and requires the checkout's own
# `ref:` to be no weaker.
#
# The RHS DOES use `_INPUT_MENTION_BODY` (BE-8146), unlike the rest of this
# binding, which stays deliberately narrow. This regex plays a second role
# `find_unguarded_ref_checkouts` leans on: it is also the ONLY thing that
# registers a step in `resolvers` (via `_binding_step_id`) for the resolve-then-
# consume idiom below, and an unrecognized RHS there does not "read as ABSENT
# and report the checkout" the way an unrecognized ref use does — it leaves the
# step out of `resolvers` entirely, so `_record_steps_output` returns silently
# for its id and the checkout it feeds never enters `found` at all. A bracket-
# spelled resolver (`WORKFLOWS_REF: ${{ inputs['workflows_ref'] }}`) must not
# vanish from the lint that way, so the accessor spelling is widened here too —
# it is still the exact same value, just written differently, and widening it
# carries none of the risk widening the `fallback` group would: that group is
# what proves IMMUTABILITY and stays exact, `|| job.workflow_sha` and nothing
# else.
_GUARD_BINDING_RE = re.compile(
    r"""^\s*(['"]?)WORKFLOWS_REF\1\s*:\s*(['"]?)\$\{\{\s*(?:%s)\s*"""
    r"""(?P<fallback>\|\|\s*job\.workflow_sha\s*)?\}\}\2[^\S\n]*(?:#.*)?$"""
    % _INPUT_MENTION_BODY
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
# use is one-line escapes the "NOT covering this file" error too. Both accessor
# spellings, for the same reason again (BE-8146): a file whose only use is
# `${{ inputs['workflows_ref'] }}` would otherwise be a silent skip rather than
# a loud "the lint is NOT covering this file". The bracket alternative carries
# the same whole-value anchoring as the dot form — the interpolation has to BE
# the scalar, not merely appear in it.
# (`:[^\S\n]*(?:#…)?\s*` rather than a plain `\s*`, so a comment sitting between
# the key and a value on the next line does not hide the use — the same gap, in
# the backstop that is supposed to catch exactly this kind of miss.)
_CONSUMES_BLOCK_RE = re.compile(
    r"""(?m)^\s*(['"]?)[\w.-]+\1\s*:[^\S\n]*(?:#[^\n]*)?\s*"""
    r"""(['"]?)\$\{\{\s*(?:%s)\s*\}\}\2\s*$""" % _INPUT_MENTION_BODY
)
_CONSUMES_FLOW_RE = re.compile(
    r"""[{,]\s*(['"]?)[\w.-]+\1\s*:\s*(['"]?)\$\{\{\s*(?:%s)\s*\}\}\2\s*[,}]""" % _INPUT_MENTION_BODY
)
# The block-scalar form, for the same reason again. (The plain multi-line form
# already lands in _CONSUMES_BLOCK_RE, whose `\s*` spans the newline; only the
# `|`/`>` indicator sits between the colon and the value and defeats it.)
_CONSUMES_SCALAR_RE = re.compile(
    r"""(?m)^\s*(['"]?)[\w.-]+\1\s*:\s*[|>][+-]?\d*[^\S\n]*(?:#[^\n]*)?\n"""
    r"""\s*\$\{\{\s*(?:%s)\s*\}\}""" % _INPUT_MENTION_BODY
)

# An `env:` binding of the input to a NAME (`WORKFLOWS_REF: ${{ inputs… }}`).
# A checkout does not have to name the input directly: hoist it to a job-level
# `env:` — the natural refactor once several steps want it — and every
# `ref: ${{ env.WORKFLOWS_REF }}` below reads as no ref use at all, dropping
# the very checkouts this lint exists to cover. So the names bound to the input
# are collected first, and a `ref:` reaching one of them counts as a use.
# ANY `env:` value that MENTIONS the input registers a binding — not only the
# bare input or the `|| job.workflow_sha` fallback. Enumerating blessed
# spellings made an unrecognized one fail OPEN, which is the wrong direction for
# a detector whose job is absence: `WORKFLOWS_REF: ${{ inputs.workflows_ref ||
# 'main' }}` registered no alias, so `ref: ${{ env.WORKFLOWS_REF }}` read as no
# ref use at all and that checkout left the lint entirely — carrying the exact
# mutable fallback the lint exists to catch. Registering it instead hands the
# checkout to the guard and mutability checks, which is the posture the module
# states everywhere else (a flow-form guard "reads as ABSENT, which fails the
# lint loudly"). It answers ONLY "does this name reach the input" — never "is
# it strong": an alias never earns the self-pin exemption (see `_FALLBACK_RES`),
# because `env:` shadows and is scoped per step/job while this scan is
# file-wide. Matched against the comment-STRIPPED child, or this becomes the one
# place in the module reading a comment as code: `ASSETS: _dir  # checked out at
# inputs.workflows_ref` would bind `ASSETS` and fail a compliant workflow.
#
# Two shapes, because an `env:` entry does not have to sit on its own line: the
# block form's child lines, and the flow form's entries on the `env: {…}` line
# (or lines — see `_flow_mapping_text`). Group 2 is the bound NAME in the block
# pattern; the flow form is walked structurally by `_flow_entries` instead of a
# single bounded regex (see there for why).
#
# The NAME grammar allows a hyphen (`WORKFLOWS-REF`), not just `\w`: it is not
# read as a shell variable here, only as an `env.NAME`/`env['NAME']` expression
# key, and Actions accepts a hyphenated one (via the bracket spelling, since
# `env.WORKFLOWS-REF` is not valid property-access syntax) — excluding it just
# means `ref: ${{ env['WORKFLOWS-REF'] }}` reads as no ref use at all.
#
# Parameterized by the "reaches" test because the alias scan asks the same
# question twice: once for the input itself, and once per pass of
# `env_aliases`' fixpoint for `env.<a name already known to reach it>`.
def _env_alias_res(mention):
    """The (block-same-line, mention) patterns for `_env_bindings`.

    `block_re` binds a name whose value mentions `mention` on the KEY's own
    line. `mention_re` is `mention` alone, compiled — used both to test a flow
    entry's value (`_flow_entries` already isolates it) and a block key's
    CONTINUATION line, when the value sits on the line below instead
    (`REF:` / `REF: >-` with the mention one line down, the same shape
    `_REF_KEY_OPEN_RE` opens for a `ref:`).
    """
    return (
        re.compile(r"""^\s*(['"]?)([A-Za-z_][\w-]*)\1\s*:[^\S\n]*.*(?:%s)""" % mention),
        re.compile(mention),
    )


# A `NAME:` (or `NAME: >-` / `NAME: |`) that opens a scalar continued on the
# NEXT, more-indented line rather than carrying its value on its own — the
# same shape `_REF_KEY_OPEN_RE` recognizes for `ref:`, generalized to any env
# key name so the alias scan can follow it too. Group 2 is the NAME.
_ALIAS_KEY_OPEN_RE = re.compile(
    r"""^\s*(['"]?)([A-Za-z_][\w-]*)\1\s*:[^\S\n]*(?:["'][^\S\n]*$|(?:[|>][+-]?\d*)?[^\S\n]*(?:#.*)?$)"""
)
# The same NAME grammar as `_ALIAS_KEY_OPEN_RE`'s group 2, standalone — used to
# validate a flow entry's key, which `_flow_entries` reads structurally rather
# than through a keyed regex.
_ALIAS_NAME_RE = re.compile(r"""^[A-Za-z_][\w-]*$""")

_ENV_ALIAS_RES = _env_alias_res(_INPUT_MENTION_BODY)
# Every `env:` entry, whatever its value — the alias fixpoint's hard bound: no
# pass of `env_aliases` can add a name that is not bound somewhere in this file.
_ENV_ANY_RES = _env_alias_res(r"")
# Scoped to `env:` blocks, not every mapping key bound to the input: the
# checkout's own `ref: ${{ inputs.workflows_ref }}` is such a binding too, and
# treating `ref` as an alias would make `env.ref`/`$ref` anywhere read as the
# input.
#
# BOTH `env:` spellings bind (BE-8146). The flow form used to bind nothing, on
# the stated belief that this "loses nothing the `_CONSUMES_*` backstop does not
# already catch" — which was false: that backstop only runs when `defaults is
# None`, i.e. when the input DECLARATION itself was unparseable, so it caught
# none of this. A well-formed workflow writing `env: {WORKFLOWS_REF: ${{
# inputs.workflows_ref }}}` bound no alias, its `ref: ${{ env.WORKFLOWS_REF }}`
# read as no ref use at all, and the checkout got a green lint unguarded. The
# flow form binds directly now; `_env_bindings` scans it via `_flow_entries`.
#
# `(?:-\s+)?` tolerates the list marker sitting on THIS key's own line: an
# `env:` written as a step's first key (`- env:` / `- env: {…}`) carries it,
# and without this the marker character sits where `env` is expected and the
# match fails outright — the block body is never walked, or the flow line
# never recognized, and every alias that step would have bound is gone.
_ENV_KEY_RE = re.compile(r"""^\s*(?:-\s+)?(['"]?)env\1\s*:[^\S\n]*(?:#.*)?$""")
_ENV_FLOW_KEY_RE = re.compile(r"""^\s*(?:-\s+)?(['"]?)env\1\s*:\s*\{""")

# A `default` key inside a flow mapping: `{type: string, default: main}`.
_FLOW_DEFAULT_RE = re.compile(r"""[{,]\s*(['"]?)default\1\s*:""")

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
# ANCHORED TO THE WHOLE YAML VALUE, not just to the interpolation, and matched
# against the comment-stripped line. Unanchored, this read "contains the
# fallback" rather than "IS the fallback", and every boundary matters because an
# extra operand — or an extra character — on either side reintroduces a mutable
# ref: `${{ inputs.workflows_ref || job.workflow_sha || 'main' }}` resolves to
# the default branch in exactly the pre-v2.334.0 case the fallback exists for,
# `${{ inputs.override || inputs.workflows_ref || job.workflow_sha }}` resolves
# to whatever the LEADING operand names, and `refs/heads/${{ … }}` /
# `${{ inputs.override }}${{ … }}` bury it in a concatenation. The runtime guard
# cannot catch any of them: a guard proves non-emptiness, not immutability.
#
# Three spellings, mirroring the `_REF_USE_*` trio, because a `${{ … }}` sitting
# somewhere on the line is not the same as being the REF's value:
#   block  `ref: ${{ … }}`                  — the value is the whole rest of the line
#   flow   `{…, ref: ${{ … }}, …}`          — bounded at the entry boundary, exactly
#                                             as `_REF_USE_FLOW_RE` bounds its own
#                                             value, so a SIBLING entry carrying the
#                                             fallback cannot be misread as the ref
#   cont.  a continuation line that IS the expression, for `ref: >-` and friends
_FALLBACK_BODY = r"""inputs\.%s\s*\|\|\s*job\.workflow_sha""" % INPUT_NAME


# The self-pin is judged from the LITERAL expression on the checkout line, and
# an `env:` alias is deliberately NOT accepted as a spelling of it.
#
# Carrying an alias binding's strength to `ref: ${{ env.NAME }}` was tried and
# reverted: `env:` is scoped per step and per job and it SHADOWS, while these
# scans are file-wide, so a file-wide set of "names bound to the fallback"
# grants the exemption at checkouts the binding never reaches. Both directions
# were live. A binding sitting in a GUARD step's `env:` is invisible to the
# sibling checkout step at run time, so `ref: ${{ env.NAME }}` there expands to
# '' and takes the default branch — while scoring as a guarded self-pin. And a
# step-local `WORKFLOWS_REF: ${{ inputs.workflows_ref || 'main' }}` inherited
# fallback strength from any OTHER step binding that name strictly;
# cursor-review.yml binds `WORKFLOWS_REF` both ways today (line 420 with the
# fallback, six more without it), so that cross-talk was not hypothetical.
#
# There is also no valid refactor left to serve. The fallback cannot be hoisted
# to a shared `env:` at all: the `job` context is not available in
# `jobs.<job_id>.env` (actionlint: `context "job" is not allowed here`), and a
# step-level `env:` does not reach a sibling step. So groom.yml's seven
# duplicated bindings are duplicated of necessity, and a `ref:` reaching the
# input through an alias is required to carry a BARE guard — the fail-closed
# answer, and the one the module's stated posture asks for.
_FALLBACK_RES = (
    re.compile(
        r"""^\s*(?P<k>['"]?)ref(?P=k)\s*:[^\S\n]*"""
        r"""(?P<q>['"]?)\$\{\{\s*%s\s*\}\}(?P=q)[^\S\n]*$""" % _FALLBACK_BODY
    ),
    re.compile(
        r"""[{,]\s*(?P<k>['"]?)ref(?P=k)\s*:[^\S\n]*"""
        r"""(?P<q>['"]?)\$\{\{\s*%s\s*\}\}(?P=q)[^\S\n]*(?=[,}])""" % _FALLBACK_BODY
    ),
    re.compile(
        r"""^\s*(?P<q>['"]?)\$\{\{\s*%s\s*\}\}(?P=q)[^\S\n]*$""" % _FALLBACK_BODY
    ),
)


def _outside_quotes(line, pos):
    """True when `pos` sits outside any quoted scalar on `line`.

    YAML-aware, not a bare toggle: a single-quoted scalar escapes an inner `'`
    by doubling it (`''`), and a double-quoted one escapes a `"` with a
    backslash. Toggling on every quote character misreads either as closing
    the scalar, which then reads whatever comes after as string content (or
    vice versa) until an unrelated quote happens to toggle it back.
    """
    quote = None
    i = 0
    while i < pos:
        ch = line[i]
        if quote:
            if quote == "'" and ch == "'":
                if i + 1 < pos and line[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            elif quote == '"' and ch == "\\":
                i += 2
                continue
            elif quote == '"' and ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        i += 1
    return quote is None


# The `${{ … }}` body of the FIRST interpolation on a line, and its leading
# `||` operand. Splitting on `||` is enough here: Actions has no operator that
# can contain one, and a `||` inside a quoted literal would still leave a
# leading operand that does not reach the input, which is the answer we want.
_INTERPOLATION_RE = re.compile(r"""\$\{\{(.*?)\}\}""", re.S)


def _leading_operand_reaches_input(line, mention_re):
    """True when the ref expression's first `||` operand reaches the input.

    Lines with no interpolation at all (a literal `ref: main` never reaches
    here — it is not a ref use) answer True, so this only ever narrows.
    """
    match = _INTERPOLATION_RE.search(_strip_comment(line))
    if not match:
        return True
    return bool(mention_re.search(match.group(1).split("||")[0]))


def _pins_to_job_workflow_sha(line):
    """True when `line`'s CODE — not its comments — IS the BE-4169 fallback ref.

    Comments are stripped first because prose merely NAMING the expression
    (this repo's workflows discuss it at length) must not buy the `default: ''`
    carve-out for a file where no checkout actually uses it.
    """
    block_re, flow_re, cont_re = _FALLBACK_RES
    code = _strip_comment(line)
    if block_re.match(code) or cont_re.match(code):
        return True
    # The flow form `search`es mid-line, so its `[{,]` boundary can be met by a
    # comma INSIDE a quoted sibling scalar — planting a decoy `ref:` that scores
    # the line a self-pin while the real `ref:` on it is bare. Require the entry
    # boundary to be real YAML punctuation, not string content.
    match = flow_re.search(code)
    return bool(match and _outside_quotes(code, match.start()))


def _default_value(line):
    """The comment-stripped RHS of a `default:` key line."""
    return _strip_comment(line.split(":", 1)[1])


def _flow_mapping_text(lines, start, open_col):
    """The flow mapping opening at `lines[start][open_col]` (a `{`), joined
    across as many following lines as it takes to reach the matching `}` —
    `None` if the file ends first.

    A flow mapping is not required to close on the line that opens it
    (`env: {` alone, with its entries on the lines below, is valid YAML), and
    the single-line scan used to miss that shape entirely: `_ENV_KEY_RE`
    rejects a line ending in `{`, so the block-body walk never runs either,
    and the line-bound flow scan finds no entries on a key line that IS just
    `env: {`. Comments are stripped PER LINE before joining — a comment is
    scoped to its own line, and joining the raw text first would let one
    swallow the lines after it. Brace-depth aware while scanning for the
    close, so a nested `${{ … }}` (every one opens and closes a balanced
    pair) is never misread as the mapping's own end.
    """
    text = _strip_comment(lines[start])[open_col:]
    depth = 0
    quote = None
    i = 0
    j = start
    while True:
        if i >= len(text):
            j += 1
            if j >= len(lines):
                return None
            text += "\n" + _strip_comment(lines[j])
            continue
        ch = text[i]
        if quote:
            if quote == "'" and ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            elif quote == '"' and ch == "\\":
                i += 2
                continue
            elif quote == '"' and ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[: i + 1]
        i += 1


def _flow_entries(text, open_pos):
    """Yield (key, value) for each top-level entry of the flow mapping whose
    `{` sits at `text[open_pos]`.

    A hand-rolled walk rather than a bounded regex (`[^,}]*` and its kin),
    because that bound is not safe: it stops at the FIRST `,` or `}`, but
    either can sit inside the value without ending the entry — a quoted
    scalar (`"${{ inputs.dir }}/${{ inputs.workflows_ref }}"`, one value, two
    interpolations) or a nested expression call (`format('{0}', …)`, braces
    of its own). This walk tracks quote state (with the same `''`/`\\"`
    escapes `_outside_quotes` does) and `{}` depth together, so only a `,` or
    `}` that is real top-level YAML punctuation — outside any quote, at
    depth 0 — ends an entry or the mapping, and a quoted key or value
    containing either cannot manufacture a decoy boundary.
    """
    n = len(text)
    i = open_pos + 1
    while i < n:
        while i < n and text[i] in " \t\n":
            i += 1
        if i >= n or text[i] == "}":
            return
        key_start = i
        quote = None
        while i < n:
            ch = text[i]
            if quote:
                if quote == "'" and ch == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    quote = None
                elif quote == '"' and ch == "\\":
                    i += 2
                    continue
                elif quote == '"' and ch == '"':
                    quote = None
                i += 1
                continue
            if ch in "'\"":
                quote = ch
                i += 1
                continue
            if ch == ":":
                break
            i += 1
        key = text[key_start:i].strip().strip("'\"")
        i += 1  # past the ':'
        while i < n and text[i] in " \t\n":
            i += 1
        value_start = i
        depth = 0
        quote = None
        while i < n:
            ch = text[i]
            if quote:
                if quote == "'" and ch == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    quote = None
                elif quote == '"' and ch == "\\":
                    i += 2
                    continue
                elif quote == '"' and ch == '"':
                    quote = None
                i += 1
                continue
            if ch in "'\"":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                break
            i += 1
        yield key, text[value_start:i]
        if i < n and text[i] == ",":
            i += 1


def _env_bindings(lines, res):
    """Names bound inside an `env:` mapping by `res`, e.g. `WORKFLOWS_REF`.

    Both spellings: the block form's child lines (including a NAME whose
    value continues on the line below), and the flow form's entries, walked
    structurally by `_flow_entries` over `_flow_mapping_text` — which also
    covers a mapping that wraps across lines. Matched against the
    comment-STRIPPED source, or this becomes the one place in the module
    reading a comment as code.
    """
    block_re, mention_re = res
    names = set()
    for i, line in enumerate(lines):
        if _ENV_KEY_RE.match(line):
            children = list(_block_body(lines, i, _indent(line)))
            for idx, (_, child) in enumerate(children):
                stripped = _strip_comment(child)
                match = block_re.match(stripped)
                if match:
                    names.add(match.group(2))
                    continue
                # The value may not share the key's own line at all (`REF:`
                # or `REF: >-`, mention on the line below) — the same shape
                # `_REF_KEY_OPEN_RE` opens for `ref:`, generalized here to any
                # key name via `_ALIAS_KEY_OPEN_RE`.
                open_match = _ALIAS_KEY_OPEN_RE.match(child)
                if not open_match or idx + 1 >= len(children):
                    continue
                _, nxt = children[idx + 1]
                if _indent(nxt) > _indent(child) and mention_re.search(_strip_comment(nxt)):
                    names.add(open_match.group(2))
            continue
        code = _strip_comment(line)
        if not _ENV_FLOW_KEY_RE.match(code):
            continue
        text = _flow_mapping_text(lines, i, code.index("{"))
        if text is None:
            continue
        for key, value in _flow_entries(text, 0):
            if _ALIAS_NAME_RE.match(key) and mention_re.search(value):
                names.add(key)
    return frozenset(names)


def env_aliases(lines):
    """Names whose `env:` binding REACHES the input, e.g. `WORKFLOWS_REF`.

    Directly — `WORKFLOWS_REF: ${{ inputs.workflows_ref }}` — or through another
    env name that already does (BE-8146). `BASE: ${{ inputs.workflows_ref }}`
    followed by `REF: ${{ env.BASE }}` binds BOTH, so a `ref: ${{ env.REF }}`
    two hops from the input is still a ref use. Following exactly one hop was
    the whole scan, and the second hop's checkout left the lint entirely.

    A fixpoint rather than a fixed hop count, bounded by the number of names
    bound anywhere in the file — no pass can add a name that is not one of them,
    and every pass either adds one or stops. Mirrors `_ref_derived_names`, which
    does the same for the shell variables inside a guard step. File-wide
    over-approximation across `env:` scopes is intended; see `_mention_alt`.
    """
    names = set(_env_bindings(lines, _ENV_ALIAS_RES))
    if not names:
        # Nothing reaches the input directly, so no chain can reach it either —
        # and an empty alternation below would degenerate to `env\.(?:)\b`,
        # which matches the `env.` of ANY name and would bind the whole file.
        return frozenset()
    for _ in range(len(_env_bindings(lines, _ENV_ANY_RES))):
        alt = "|".join(sorted(re.escape(n) for n in names))
        chained = _env_bindings(
            lines,
            _env_alias_res(r"""env\s*\.\s*(?:%s)\b|env\s*%s""" % (alt, _bracket_body(alt))),
        )
        if chained <= names:
            break
        names |= chained
    return frozenset(names)


def _mention_alt(aliases):
    """Regex alternation for "reaches the input" — directly or via an alias.

    File-wide rather than scope-aware on purpose: `env:` is scoped per job and
    per step, but over-approximating can only ever DEMAND a guard, never excuse
    a missing one — the safe direction for a detector whose job is absence.

    Both accessor spellings on both halves (BE-8146): `inputs.x` / `inputs['x']`
    for the input itself, through `_INPUT_MENTION_BODY`, and `env.NAME` /
    `env['NAME']` for an alias, each tolerating whitespace inside the brackets.
    `${NAME}` covers the shell reading of a bound name.
    """
    alt = _INPUT_MENTION_BODY
    if aliases:
        names = "|".join(sorted(re.escape(a) for a in aliases))
        alt += r"""|env\s*\.\s*(?:%s)\b|env\s*%s|\$\{?(?:%s)\b""" % (
            names,
            _bracket_body(names),
            names,
        )
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


def steps_output_ref(line, cont=False):
    """(step id, output name) when `line`'s `ref:` IS a step output, else None.

    Answers only "which step produced this ref" — never whether the checkout is
    covered. `ref_checkouts` decides that, because the answer depends on which
    steps the job has already walked past. `cont=True` judges a CONTINUATION
    line instead: the bare expression under a `ref: >-` / `ref: |` key, which
    carries no `ref:` of its own.

    Comments are stripped first, for the same reason `_pins_to_job_workflow_sha`
    strips them: prose merely naming a step output (`ref: main  # was
    ${{ steps.x.outputs.ref }}`) is not a checkout resolved from one.
    """
    # Asked of nearly every line of every job (the `else` arm of the walk), and
    # a `steps.`/`steps[` substring is a precondition of all three patterns —
    # so answer the common case before paying for the comment strip and the
    # regexes.
    if "steps." not in line and "steps[" not in line:
        return None
    code = _strip_comment(line)
    if cont:
        match = _STEPS_OUTPUT_CONT_RE.match(code)
    else:
        match = _STEPS_OUTPUT_BLOCK_RE.match(code)
        if match is None:
            # The flow form `search`es mid-line, so its `[{,]` entry boundary can
            # be met by a comma INSIDE a quoted sibling — planting a decoy `ref:`
            # that resolves to a step no checkout consumes, while the real `ref:`
            # further along the line is never examined. `_pins_to_job_workflow_sha`
            # answers that by requiring its match to sit outside quotes; here the
            # walk must also keep LOOKING past the decoy, because stopping at it
            # drops the site out of coverage entirely rather than merely scoring
            # it weaker.
            for candidate in _STEPS_OUTPUT_FLOW_RE.finditer(code):
                if _outside_quotes(code, candidate.start()):
                    match = candidate
                    break
    if not match:
        return None
    step_id = match.group("id") or match.group("id_idx")
    out = match.group("out") or match.group("out_idx")
    return (step_id, out)


def _consumes_input(text):
    """True when `text` uses the input as a mapping value in any YAML style."""
    return bool(
        _CONSUMES_BLOCK_RE.search(text)
        or _CONSUMES_FLOW_RE.search(text)
        or _CONSUMES_SCALAR_RE.search(text)
    )


def _strip_comment(value):
    """Drop a trailing `# …` comment from a scalar value.

    Quote-aware, with the same escape rules `_outside_quotes` uses (`''`
    inside a single-quoted scalar, `\\"` inside a double-quoted one): a `#`
    that sits inside a quoted entry's own value (`MSG: "a # b"`) is string
    content, not a comment opener, and stripping from the FIRST `#` on the
    line — as a plain regex does — truncates every sibling entry after it
    too. Falls back to the whole value when no unquoted `#` is found.
    """
    quote = None
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if quote:
            if quote == "'" and ch == "'":
                if i + 1 < n and value[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            elif quote == '"' and ch == "\\":
                i += 2
                continue
            elif quote == '"' and ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i].strip()
        i += 1
    return value.strip()


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


def _binding_step_id(lines, idx):
    """The `id:` of the step whose `env:` holds the binding at `idx`, or None.

    Read at the step's OWN key column, so an `id:` belonging to a nested mapping
    — or to the next step down — is never attributed to this one. A step with no
    id produces no output anything can consume, so None simply means no checkout
    can resolve from here.
    """
    bounds = _step_bounds(lines, idx)
    if bounds is None:
        return None
    start, end, key_indent = bounds
    for j in range(start, end):
        line = lines[j]
        if _is_skippable(line):
            continue
        if j == start and line.lstrip().startswith("- "):
            # The list marker occupies the step's key column, so `- id: resolve`
            # declares the id there exactly as a later `id:` line does.
            line = " " * key_indent + line.lstrip()[2:]
        if _indent(line) != key_indent:
            continue
        match = _STEP_ID_RE.match(line)
        if match:
            return match.group(2).strip("'\"")
    return None


def _consuming_step_bounds(lines, idx):
    """`_step_bounds` for a `ref:` line, tolerating the flow form.

    `_step_bounds` resolves a step from a line nested BELOW its key column (an
    `env:`/`with:` member) and answers None at the key column itself, by design.
    A block `ref:` sits under `with:`, one level deeper, so it resolves
    directly — but the flow form writes the whole mapping on the step's own
    `with: {…, ref: …}` line, which IS that column. Re-ask from a copy of that
    line pushed one column deeper: the same question, asked from where the block
    form already asks it, rather than a second copy of the boundary logic that
    could drift from the first.
    """
    bounds = _step_bounds(lines, idx)
    if bounds is not None:
        return bounds
    shifted = list(lines)
    shifted[idx] = " " + shifted[idx]
    return _step_bounds(shifted, idx)


def _skips_on_empty_output(lines, idx, step_id, out):
    """True when the step consuming `steps.<id>.outputs.<out>` skips on empty.

    The empty case moves to the CONSUMER once the ref is resolved a step early,
    and the only thing that can carry it there is a step-level `if:` that is
    EXACTLY the non-empty test on that same output. Matched exactly, and only
    the spellings YAML offers for that one condition — bare or `${{ … }}`-
    wrapped, each optionally written as a double-quoted scalar:

    - **No OR-widening.** `steps.x.outputs.ref != '' || always()` runs the
      checkout precisely when the output is empty, which is the hole inverted.
    - **No other output.** An `if:` on a DIFFERENT output says nothing about
      the one the `ref:` reads.
    - **No job-level `if:`.** It skips the whole job, including the resolver,
      so it can never distinguish an empty ref from a populated one.
    """
    bounds = _consuming_step_bounds(lines, idx)
    if bounds is None:
        return False
    start, end, key_indent = bounds
    wanted = "steps.%s.outputs.%s != ''" % (step_id, out)
    for j in range(start, end):
        line = lines[j]
        if _is_skippable(line):
            continue
        if j == start and line.lstrip().startswith("- "):
            # The list marker occupies the step's key column, so `- if: …`
            # declares the condition there exactly as a later `if:` line does.
            # Same rewrite `_binding_step_id` makes to read an `id:` off it —
            # without it a correctly guarded checkout reads as unguarded.
            line = " " * key_indent + line.lstrip()[2:]
        if _indent(line) != key_indent:
            continue
        if not _STEP_IF_RE.match(line):
            continue
        cond = _strip_comment(line.split(":", 1)[1])
        # `if: "steps.x.outputs.ref != ''"` is the same condition, spelled as a
        # quoted scalar; unwrap ONE matching pair so the quoted form is not
        # failed for being quoted. This cannot widen what follows — the
        # comparison below is still character-exact, so the only string the
        # strip can newly accept is the wanted condition wearing quotes.
        #
        # DOUBLE quotes only. A YAML single-quoted scalar escapes its inner `'`
        # as `''`, so this condition is spelled `'… != '''''` there, and
        # unwrapping that without also undoing the escape would compare the
        # wrong string. Undoing it is a second YAML rule an exact-match test has
        # no need to learn, and the cost of not learning it is LOUD: that
        # spelling fails the lint with the remedy naming the accepted form,
        # rather than passing something unguarded.
        if len(cond) >= 2 and cond[0] == '"' and cond[-1] == '"':
            cond = cond[1:-1].strip()
        if cond.startswith("${{") and cond.endswith("}}"):
            cond = cond[3:-2].strip()
        # Runs of whitespace collapse to one space, which can only ever REJECT
        # a condition that is not the wanted one — it never removes a character
        # and so can never manufacture the `''` this test hinges on. The spacing
        # AROUND `!=` is then pinned to one space either side, because Actions
        # accepts `…outputs.ref!=''` and a lint that failed it would be failing
        # the remedy it just asked for. Normalizing spacing cannot turn a
        # different condition into this one: every other character still has to
        # match exactly, `!=` included.
        if re.sub(r"\s*!=\s*", " != ", re.sub(r"\s+", " ", cond)) == wanted:
            return True
    return False


def _record_steps_output(found, lines, idx, resolved, resolvers):
    """Record the `ref:` at `idx`, which reads `steps.<id>.outputs.<out>`.

    Records NOTHING when `<id>` names no resolver seen earlier in this job. A
    checkout resolved from a step that never touches `workflows_ref` — a
    `git rev-parse`, a release lookup — is not this lint's subject, and
    demanding an empty-ref `if:` of it would fail workflows the lint has no
    claim on. (Fail-open only in the sense that this lint never covered such a
    checkout in the first place; what it must not do is silently drop one that
    IS resolved from the input, which is precisely what `resolvers` catches.)

    Otherwise it IS a ref use, guarded when EITHER the resolving step is itself
    a fail-closed guard (nothing empty ever leaves it), OR the consuming step
    carries the exact non-empty `if:` on that same output. The never-fail idiom
    can only take the second route: its resolver runs `continue-on-error: true`,
    which `is_guard_step` disqualifies on purpose — an `exit 1` that does not
    fail the job leaves the checkout running anyway.

    Recorded as a NON-fallback site: `uses_fallback` exists to compare a
    checkout's expression against the strength of the guard that covered it,
    and there is no expression to compare here — the `if:` tests the resolved
    VALUE, so guard strength is moot on this path.
    """
    step_id, out = resolved
    if step_id not in resolvers:
        return
    guarded = resolvers[step_id] or _skips_on_empty_output(lines, idx, step_id, out)
    found.append((idx + 1, False, guarded, True))


def ref_checkouts(lines):
    """(1-based line, uses_fallback, guarded, via_step_output) for EVERY ref checkout.

    A use is guarded when the empty-ref guard step appears earlier in the SAME
    job — jobs run independently, so a guard in job A does nothing for job B —
    AND that guard validated an expression no weaker than the one the checkout
    consumes.

    Strength matters because the two recognized bindings prove different
    things. A guard on the bare input proves `inputs.workflows_ref` itself is
    non-empty, which covers every checkout in the job. A guard on
    `inputs.workflows_ref || job.workflow_sha` proves only that the OR
    expression is non-empty: with the input omitted it passes on
    `job.workflow_sha`, so it says nothing about a sibling
    `ref: ${{ inputs.workflows_ref }}`, which still receives '' and sends
    checkout to the default branch. So a fallback guard covers fallback
    checkouts only, and a bare checkout needs a bare guard.

    A checkout reaching the ref through an `env:` alias is always judged BARE:
    `ref: ${{ env.NAME }}` says nothing about which binding is in effect there,
    so it needs a bare guard. Fail-closed by design — see `_FALLBACK_RES`.

    A THIRD way to reach the ref does not name the input at all: resolve it in
    an earlier step and check out at that step's OUTPUT (BE-8130). Those sites
    carry `via_step_output`, are judged by `_record_steps_output` — a
    fail-closed resolver, or the exact non-empty `if:` on the consuming step —
    and never earn the fallback-strength exemption, because the `if:` tests the
    resolved value rather than an expression.
    """
    aliases = env_aliases(lines)
    ref_res = _ref_use_res(aliases)
    # Computed ONCE per file, not per line: `env_aliases` is a full-file scan
    # plus a `_block_body` walk per `env:` key, and `check_dir`'s carve-out used
    # to rebuild both inside its own `any(...)` comprehension — quadratic on the
    # files that matter most here (groom.yml is ~3,000 lines with dozens of
    # `env:` blocks, and `check_dir` runs over the real tree in the CLI lint).
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

    found = []
    for start in job_starts:
        guarded_input = False     # a guard proved the INPUT non-empty
        guarded_fallback = False  # …only the `|| job.workflow_sha` expression
        # Steps in THIS job, seen so far, whose `env:` binds the input and that
        # carry an `id:` a later `ref:` can resolve from — `{id: is_guard_step}`.
        # Per job and populated as the walk goes, so a resolver in another job
        # (they run independently) or one declared BELOW its consumer can never
        # be credited; the walk order gives that ordering for free, exactly as
        # it does for the guard flags above.
        resolvers = {}
        # An open `ref:` whose value continues below, as (line index, indent).
        # Continuation lines are the more-indented ones that follow; the first
        # line back at or above the key's indent closes the scalar.
        pending = None
        for i, line in _block_body(lines, start, job_indent):
            if pending is not None:
                if _indent(line) > pending[1]:
                    if mention_re.search(line):
                        # Report the `ref:` KEY line (that is the checkout the
                        # reader must find), but judge the CONTINUATION line —
                        # the key never holds the expression, so asking it
                        # whether this is a fallback always answered no.
                        fallback = _pins_to_job_workflow_sha(line)
                        guarded = guarded_input or (fallback and guarded_fallback)
                        found.append((pending[0] + 1, fallback, guarded, False))
                        pending = None
                        continue
                    resolved = steps_output_ref(line, cont=True)
                    if resolved is not None:
                        # The `ref: >-` / `ref: |` spelling of the resolve-then-
                        # consume shape. Judged from the same place as the block
                        # form — the KEY line is the checkout, and it is the key
                        # line's own step that must carry the skip `if:`.
                        _record_steps_output(found, lines, pending[0], resolved, resolvers)
                        pending = None
                    continue
                # Scalar closed — fall through and judge this line normally.
                pending = None
            binding = _GUARD_BINDING_RE.match(line)
            if binding:
                # NO fallback exception on the guard requirement (BE-8077). The
                # BE-4169 `inputs.workflows_ref || job.workflow_sha` form cannot
                # resolve to a MUTABLE ref — that is what earns it the
                # `default: ''` carve-out in `check_dir` — but it is NOT
                # self-sufficient the way that story assumed: `job.workflow_sha`
                # needs runner v2.334.0+ and expands to '' on anything older,
                # and checkout reads `ref: ''` as the DEFAULT BRANCH. So the
                # fallback answers MUTABILITY and the guard answers EMPTINESS,
                # and this lint requires both. Exempting the fallback from the
                # guard check meant deleting every one of groom.yml's seven
                # guard steps kept this lint green.
                guard = is_guard_step(lines, i)
                # A step that RESOLVES the ref for a later checkout to consume
                # is registered here whether or not it guards, because the two
                # answers differ: a hard guard covers its consumers outright,
                # while the never-fail idiom's resolver (`continue-on-error:
                # true`) does not and hands the empty case to the consumer's
                # own `if:`. Strength (bare vs `|| job.workflow_sha`) is NOT
                # recorded: that distinction exists because a guard proves
                # something about an EXPRESSION, while the consumer's `if:`
                # tests the actual resolved VALUE, where it is moot.
                step_id = _binding_step_id(lines, i)
                if step_id is not None:
                    resolvers[step_id] = guard
                if guard:
                    if binding.group("fallback"):
                        guarded_fallback = True
                    else:
                        guarded_input = True
            elif is_ref_use(line, ref_res):
                fallback = _pins_to_job_workflow_sha(line)
                guarded = guarded_input or (fallback and guarded_fallback)
                # A guard proves the INPUT is non-empty. It says nothing about
                # an expression that never reaches the input: GitHub's `||`
                # returns the FIRST truthy operand, so `ref: ${{ 'main' ||
                # inputs.workflows_ref }}` mentions the input (and is therefore
                # a ref use, and passes the guard) while resolving to a mutable
                # branch on every runner — no second input declaration needed.
                # `check_dir` cannot see it either; it reads only
                # `workflows_ref`'s own `default:`. So the leading operand has
                # to reach the input, or no guard in the job covers this ref.
                if not _leading_operand_reaches_input(line, mention_re):
                    guarded = False
                found.append((i + 1, fallback, guarded, False))
            elif _REF_KEY_OPEN_RE.match(line):
                # (`_REF_KEY_OPEN_RE` needs end-of-line right after the key, so
                # it can never take a `ref: ${{ … }}` off the branch below.)
                pending = (i, _indent(line))
            else:
                resolved = steps_output_ref(line)
                if resolved is not None:
                    _record_steps_output(found, lines, i, resolved, resolvers)
    return found


def unguarded_ref_checkouts(lines):
    """(1-based line, uses_fallback, via_step_output) for every unguarded checkout.

    `via_step_output` separates the two shapes only so `check_dir` can name the
    right remedy: "copy the guard step in ahead of it" is the wrong advice for a
    checkout whose ref is resolved a step earlier — there the missing piece is
    the consuming step's own `if:`.
    """
    return [
        (lineno, fb, via_step)
        for lineno, fb, guarded, via_step in ref_checkouts(lines)
        if not guarded
    ]


def find_unguarded_ref_checkouts(lines):
    """1-based line numbers of ref checkouts with no adequate guard."""
    return [lineno for lineno, _, _ in unguarded_ref_checkouts(lines)]


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
        #
        # Scoped to actual ref CHECKOUTS, not to every line in the file. Asking
        # it of all of them granted the carve-out to any file that merely
        # MENTIONS the expression in code — most sharply, the guard steps' own
        # `WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}`
        # `env:` binding — so a file whose checkouts all read the bare
        # `ref: ${{ inputs.workflows_ref }}` bought an empty default it does not
        # self-pin against. (Stripping comments closed the prose half of that;
        # this closes the code half.)
        #
        # Asked of the PARSER rather than re-derived line by line here. A
        # per-line scan cannot see the block-scalar spelling the parser already
        # handles — for `ref: >-` with the expression on the line below, the key
        # line carries no expression and the continuation line carries no `ref:`
        # key, so no single line satisfies both tests. That file self-pins, but
        # lost the carve-out and got BE-5546's "delete the default" error while
        # its checkouts simultaneously got BE-8077's "the fallback IS
        # recognized" one. Fail-closed, but a false failure with contradictory
        # advice. `ref_checkouts` judges the continuation line the parser
        # matched, and computes the alias scan once instead of once per line.
        self_pins_to_job_workflow_sha = any(fb for _, fb, _, _ in ref_checkouts(lines))

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

        for lineno, uses_fallback, via_step_output in unguarded_ref_checkouts(lines):
            if via_step_output:
                # The resolve-then-consume shape (BE-8130): the ref reaches the
                # checkout through a step output, so the empty case moves to the
                # CONSUMER and the job-wide guard flags say nothing about it.
                errors.append(
                    "::error file=%s,line=%d::%s checks out at a `ref:` resolved "
                    "from a step whose `env:` binds `inputs.%s`, but the "
                    "consuming step carries no `if: steps.<id>.outputs.<name> != "
                    "''` on that same output — and the resolving step is not a "
                    "fail-closed guard either. An unresolvable ref then reaches "
                    "`actions/checkout` as '', which it reads as the default "
                    "branch. Add the exact non-empty `if:` to this step (the "
                    "never-fail idiom, see .github/workflow-pins/README.md), or "
                    "make the resolving step reject the empty value itself. "
                    "See BE-8130." % (path, lineno, name, INPUT_NAME)
                )
                continue
            # `uses_fallback` comes from the line the parser MATCHED, which for a
            # block scalar or continuation value is not the reported `ref:` key
            # line — re-reading that key line here always answered "not a
            # fallback" and emitted the wrong (BE-5546) message for it.
            if uses_fallback:
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
