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
# both match — the caller reads whichever pair actually fired. Bracket
# access on `steps`/`outputs` themselves stays out of scope: unlike `<id>` and
# `<out>`, which are workflow-chosen identifiers an author writes either way,
# the literal property names `steps`/`outputs` are never spelled with a
# bracket in practice, and reaching for it here would be scope with no
# observed shape behind it.
#
# The interpolation may CONTINUE past the output name, and open before it
# (BE-8215): anchoring `}}` right after the name meant `ref: ${{
# steps.x.outputs.ref || 'main' }}` matched nothing anywhere and recorded
# nothing — the `||` spelling left the lint entirely, exactly the hole
# `_leading_operand_reaches_input` closes on the input side. The `lead` group
# carries what precedes the step output inside the interpolation so operand
# order can be judged the same way there.
#
# …and the reader is TWO-TIER (BE-8253), because ONE regex per line can only
# give ONE verdict per line, while a `ref:` value can hold several step outputs
# — or a spelling this reader does not understand at all:
#
#   LOOSE  — does the comment-stripped `ref:` VALUE mention
#            `steps.<id>.outputs.<out>` anywhere, any number of times?
#   STRICT — the patterns below, run over EVERY interpolation in that value and
#            every top-level `||` operand inside each one.
#
# When LOOSE counts more mentions than STRICT accounted for, the value plainly
# reaches a step output this reader cannot judge, and the site is recorded
# `'unparsed'` so the lint REFUSES it out loud. Before the split it answered
# "no site" instead: `ref: ${{ steps.x.outputs.ref || format('refs/heads/{0}',
# 'main') }}` matched nothing (the `}` in `{0}` breaks the fallback stretch),
# recorded nothing, and PASSED — fail-open on exactly the runtime behavior this
# lint exists to close, and the identical workflow spelled bare failed. So did
# `(steps.x.outputs.ref) || 'main'` and `steps.x.outputs.ref && 'main'`.
# Teaching the strict reader to EVALUATE `format()` / `&&` / parenthesized
# operands is deliberately not the fix: an unknown spelling asks its author for
# a supported one rather than growing an expression parser in here.
#
# Both tiers read the bracket accessor too (BE-8146): `steps['<id>']` and
# `outputs['<out>']`, each independently of the other's spelling, alongside the
# dot form — a bracket-spelled output must count as a LOOSE mention and be
# readable by STRICT exactly like the dot form, never silently miscounted into
# `'unparsed'` or dropped as no mention at all.
#
# The strict stretches are bounded by `[^}]`, NOT the flow form's `[^,}]`: only
# the flow spelling needs a comma to end its entry, and carrying that bound
# into the block and continuation forms left a fallback containing a comma
# (`|| 'a,b'`) matching nothing at all — the same fail-open. `[^}]` still
# cannot cross a `}}`, so an interpolation match can never span two of them,
# and a body carrying a brace of its own is how shape 1 above reaches
# `'unparsed'` rather than being silently skipped.
_STEPS_OUTPUT_ID_RE = r"""(?:steps\.[A-Za-z0-9_-]+|steps\[\s*['"][A-Za-z0-9_-]+['"]\s*\])"""
_STEPS_OUTPUT_OUT_RE = r"""(?:outputs\.[A-Za-z0-9_-]+|outputs\[\s*['"][A-Za-z0-9_-]+['"]\s*\])"""
_LOOSE_STEPS_OUTPUT_RE = re.compile(
    r"""%s\.%s""" % (_STEPS_OUTPUT_ID_RE, _STEPS_OUTPUT_OUT_RE)
)
# An operand that is NOTHING BUT a step output. `||` falls through an empty
# one, so unlike a truthy literal it does not decide the value for the operands
# behind it — see `_lead_reaches_output`.
_BARE_STEPS_OUTPUT_RE = re.compile(
    r"""^%s\.%s$""" % (_STEPS_OUTPUT_ID_RE, _STEPS_OUTPUT_OUT_RE)
)
# The strict tier's unit: what the old single-match body required of the
# stretch AROUND the output, asked of one `||` operand at a time. `lead` keeps
# its meaning (what precedes the output, judged by `_lead_reaches_output`), and
# the `\s*$` anchor is what refuses the parenthesized and `&&` spellings —
# both carry something AFTER the output name that this reader cannot evaluate.
_STEPS_OUTPUT_OPERAND_RE = re.compile(
    r"""^(?P<lead>.*?)"""
    r"""(?:steps\.(?P<id>[A-Za-z0-9_-]+)|steps\[\s*['"](?P<id_idx>[A-Za-z0-9_-]+)['"]\s*\])"""
    r"""\.(?:outputs\.(?P<out>[A-Za-z0-9_-]+)|outputs\[\s*['"](?P<out_idx>[A-Za-z0-9_-]+)['"]\s*\])"""
    r"""\s*$"""
)


def _steps_interp_re(bound):
    """`${{ … }}`, its body bounded by `bound` — one strict-tier unit."""
    return re.compile(r"""\$\{\{(?P<body>%s*)\}\}""" % bound)


_STEPS_INTERP_RE = _steps_interp_re(r"""[^}]""")
_STEPS_INTERP_FLOW_RE = _steps_interp_re(r"""[^,}]""")
# Where a `ref:` VALUE begins, per spelling. The block form takes the rest of
# the line; a flow entry is delimited by `_flow_entry_value`; the continuation
# line under `ref: >-` / `ref: |` IS the value and carries no key at all.
_REF_KEY_VALUE_RE = re.compile(r"""^\s*(['"]?)ref\1\s*:(?P<value>.*)$""")
_REF_FLOW_KEY_RE = re.compile(r"""[{,]\s*(['"]?)ref\1\s*:""")
# The producing step's `id:`, read at the step's own key column. Quoted or bare
# — both are valid Actions YAML — and `\S+` stops before a trailing comment on
# its own, because YAML needs whitespace ahead of a `#` for it to open one.
_STEP_ID_RE = re.compile(r"""^\s*(['"]?)id\1\s*:\s*(\S+)""")
# The same key inside a flow mapping (`- {id: x, run: …}`), for the pre-scan
# `_job_step_ids` runs (BE-8215). An id the pre-scan misses turns a compliant
# workflow into a false FAILURE under the dangling check, so under-collection
# is the costly direction — over-collection merely reproduces the old
# fail-open drop for that one site.
_STEP_ID_FLOW_RE = re.compile(r"""[{,]\s*(['"]?)id\1\s*:\s*([^,}\s]+)""")
# How the guard RECEIVES the ref: through `env:` (never interpolated into the
# script body) under this one name. Half the signature — `is_guard_step` below
# checks the other half, that the step actually rejects an empty value.
# The block form; the flow spelling has its own regex just below — reading it
# as absent stopped being loud once registration became load-bearing (BE-8221).
# A trailing comment IS tolerated — a guard carrying one is a real guard doing
# its job, so rejecting it would fail a compliant workflow, not catch one.
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
# The same binding inside a flow mapping: `env: {WORKFLOWS_REF: ${{ … }}}`.
# While a fail-closed resolver could stand in for the consumer's `if:`, reading
# the flow form as ABSENT failed loudly — the checkout was reported and the
# author pushed toward the block form. With that exemption gone (BE-8221),
# absence on the resolver path means the step is never REGISTERED, so its
# consumer's `ref: ${{ steps.<id>.outputs.ref }}` is never judged at all — a
# silent pass, which is why the binding scan now has to see this spelling too.
_GUARD_BINDING_FLOW_RE = re.compile(
    r"""[{,]\s*(['"]?)WORKFLOWS_REF\1\s*:\s*(['"]?)\$\{\{\s*inputs\.workflows_ref\s*"""
    r"""(?P<fallback>\|\|\s*job\.workflow_sha\s*)?\}\}\2\s*[,}]"""
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
# checkout to the guard and mutability checks — absence must fail LOUDLY or not
# at all, the posture the module states everywhere else.
# It answers ONLY "does this name reach the input" — never "is
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


# Characters after which a YAML node — and so a QUOTED scalar — can begin.
# `-` is handled separately: it opens a node only as a standalone list marker.
_NODE_OPENERS = "{[,:"


def _opens_quoted_scalar(text, i):
    """True when the quote at `i` STARTS a quoted scalar, not sits inside a plain one.

    YAML forbids a quote only as a plain scalar's FIRST character, so `don't`
    is a perfectly legal plain scalar and `- {name: don't, id: real}` a legal
    step. Reading that apostrophe as an opener swallows the rest of the line:
    the real entry-separating comma before `id:` becomes string CONTENT, so
    `real` is never registered — the false-`dangling` direction this reader
    calls costly — and the closing `}` goes uncounted, wedging `flow_depth`
    open so every later block item is lost too.

    A quoted scalar can only begin where a node can: at the start of the text,
    after a flow indicator (`{`, `[`, `,`), after a `:` key separator, or after
    a `- ` block-sequence marker. Anywhere else the quote is content.

    NARROWING only — it can never open a scalar the bare toggle left closed —
    so every caller moves in the same direction: toward reading real YAML
    punctuation as punctuation.
    """
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0:
        return True
    if text[j] in _NODE_OPENERS:
        return True
    # A `-` opens a node only as a standalone list marker (`- 'x'`); inside a
    # plain scalar (`utf-8'`) it is an ordinary character.
    return (
        text[j] == "-"
        and (j == 0 or text[j - 1] in " \t")
        and j + 1 < len(text)
        and text[j + 1] in " \t"
    )


def _quote_mask(text, node_start_only=True):
    """Per-index `[bool]`: True where that index sits OUTSIDE any quoted scalar.

    `len(text) + 1` entries, so the position just past the end is answerable
    too. Each entry is the state BEFORE that index is consumed, which puts an
    OPENING quote outside its own scalar and a CLOSING one inside it.

    YAML-aware, not a bare toggle, in both directions: a quote only opens where
    a node can start (`_opens_quoted_scalar`), a single-quoted scalar escapes
    an inner `'` by doubling it (`''`), and a double-quoted one escapes a `"`
    with a backslash. Get any of the three wrong and the scanner reads whatever
    follows as string content (or vice versa) until an unrelated quote happens
    to toggle it back.

    ONE left-to-right pass, so a caller testing many positions on one line —
    `_collect_flow_step_ids` runs a `finditer` over every flow `id:` candidate —
    pays O(len) once instead of O(len) per test.

    `node_start_only=False` drops the scalar-start rule and opens on ANY quote.
    That is the weaker reading, and it exists for `_strip_comment` alone: that
    one is asked about EVERY physical line, block-scalar bodies included, where
    a `run: |` script's `echo "PR #${n}"` is literal text and not a YAML node at
    all. Under the strict rule that `"` follows the plain word `echo`, so it
    opens nothing, and the ` #` after it reads as a comment opener — truncating
    a line whose tail may carry the very `ref:` this lint is looking for. The
    structural readers are not asked about script text and take the strict rule.

    Per LINE, like every reader here: a quote opened on one physical line and
    closed on the next is out of scope, as it is for `_strip_comment`.
    """
    n = len(text)
    mask = [True] * (n + 1)
    quote = None
    i = 0
    while i < n:
        mask[i] = quote is None
        ch = text[i]
        if quote:
            if quote == "'" and ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    mask[i + 1] = False
                    i += 2
                    continue
                quote = None
            elif quote == '"' and ch == "\\":
                if i + 1 < n:
                    mask[i + 1] = False
                i += 2
                continue
            elif quote == '"' and ch == '"':
                quote = None
        elif ch in "'\"" and (not node_start_only or _opens_quoted_scalar(text, i)):
            quote = ch
        i += 1
    mask[n] = quote is None
    return mask


def _outside_quotes(line, pos):
    """True when `pos` sits outside any quoted scalar on `line`."""
    return _quote_mask(line)[min(pos, len(line))]


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
        # `- env:` — the step's first key written on its list marker — opens
        # the same block a plain `env:` line does, so it must bind aliases
        # too (`_ENV_KEY_RE` already tolerates the marker). The block's
        # boundary is measured at the KEY's column, not the marker's physical
        # one: off the marker, the step's OTHER keys (one level shallower
        # than the env members) would read as block members, and a `ref:`
        # among them would bind `ref` as a false alias.
        if _ENV_KEY_RE.match(line):
            children = list(_block_body(lines, i, _indent(line) + _marker_width(line)))
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


def _flow_entry_value(code, start):
    """The flow-mapping entry value beginning at `start`, up to its boundary.

    `${{ … }}` is ATOMIC here: a `,` or `}` INSIDE an interpolation belongs to
    the expression (`|| 'a,b'`, `format('refs/heads/{0}', 'main')`), not to the
    mapping, so the naive `[^,}]` bound cuts the value mid-expression and hides
    the rest of it from BOTH tiers — which would read as "nothing to see" on
    exactly the shapes BE-8253 exists to refuse. Outside an interpolation the
    first `,` or `}` still ends the entry, so a SIBLING entry carrying a step
    output cannot be misread as the ref.
    """
    depth = 0
    i = start
    while i < len(code):
        if code.startswith("${{", i):
            depth += 1
            i += 3
            continue
        if depth and code.startswith("}}", i):
            depth -= 1
            i += 2
            continue
        if not depth and code[i] in ",}":
            break
        i += 1
    return code[start:i]


def _ref_values(code, cont=False):
    """[(value, flow)] for every `ref:` value on the comment-stripped `code`.

    Empty when the line carries no `ref:` key at all — the answer for most
    lines, since the walk asks this of every line in the job.

    The flow form yields one entry per `ref:` sitting OUTSIDE any quoted
    scalar. A comma inside a quoted sibling meets the `[{,]` entry boundary, so
    a decoy `ref:` can be planted in string content; those are skipped and the
    scan keeps LOOKING past them, the same discipline `_pins_to_job_workflow_sha`
    applies — stopping at a decoy drops the real site out of coverage entirely
    rather than merely scoring it weaker.
    """
    if cont:
        return [(code, False)]
    match = _REF_KEY_VALUE_RE.match(code)
    if match:
        return [(match.group("value"), False)]
    return [
        (_flow_entry_value(code, entry.end()), True)
        for entry in _REF_FLOW_KEY_RE.finditer(code)
        if _outside_quotes(code, entry.start())
    ]


# Operands `||` falls THROUGH, so a leading one of these still lets the step
# output be the value (BE-8215). `||` returns the first TRUTHY operand, not the
# first operand, so reading `leading` as "nothing precedes the output" reported
# `${{ false || steps.x.outputs.ref }}` as unguarded unconditionally, with a
# message no edit can satisfy, even though it resolves to exactly the output a
# guard covers. GitHub's expression language treats these five as false.
_FALSEY_OPERANDS = frozenset(("false", "''", '""', "0", "null"))


def _falls_through(operand):
    """True when `||` cannot stop at `operand` — so what follows may be the ref.

    The five falsey literals, plus a bare `steps.<id>.outputs.<out>`: an
    unresolved output is `''`, which is falsey, so `${{ steps.a.outputs.ref ||
    steps.b.outputs.ref }}` really can resolve to `b`. That second operand only
    became VISIBLE with the exhaustive reader (BE-8253) — the single-match one
    swallowed it into the fallback stretch and judged `a` alone — and it is
    judged in its own right, so nothing is excused by falling through here.
    """
    operand = operand.strip()
    return operand in _FALSEY_OPERANDS or bool(_BARE_STEPS_OUTPUT_RE.match(operand))


def _lead_reaches_output(lead):
    """True when `lead` cannot stop the step output being the ref's value.

    `lead` is what precedes `steps.<id>.outputs.<out>` inside the `${{ … }}`,
    operands ahead of it included. Empty is the common case. Anything else has
    to be a chain of `||` operands that all fall through — a truthy leading
    operand (`'main' ||`) wins on every runner, and nothing the consuming step
    or the resolver can do will change that, so the site is unguarded
    unconditionally.
    """
    lead = lead.strip()
    if not lead:
        return True
    if not lead.endswith("||"):
        # Not an `||` chain at all — a `(`, a `format(`, an `&&`. Unrecognized
        # rather than proven harmless, so judge it the fail-closed way.
        return False
    return all(_falls_through(operand) for operand in lead[:-2].split("||"))


def _strict_steps_output_sites(value, flow):
    """[(id, out, leading)] for every step output the strict tier can read.

    EXHAUSTIVE, where the old reader stopped at its first match: every
    interpolation in `value`, and every top-level `||` operand inside each one.
    Two whole shapes hid behind that first match, and in both the operand
    nobody judged is a live checkout ref (BE-8253):

    - `ref: "${{ steps.a.outputs.ref }}${{ steps.b.outputs.ref || 'main' }}"` —
      the block pattern's greedy `.*` landed on the LAST interpolation, so `a`
      was never judged. Both interpolations feed the value, so both must be.
    - `ref: ${{ steps.a.outputs.ref || steps.b.outputs.ref }}` — the fallback
      stretch swallowed `b`, so a covered `a` passed the whole site while `b`
      reached an uncovered output.

    `leading` is per operand and reads the operands AHEAD of it, so the
    single-operand answers are unchanged: `${{ 'main' || <output> }}` is still
    non-leading and `${{ false || <output> }}` still reaches the output.
    """
    interp_re = _STEPS_INTERP_FLOW_RE if flow else _STEPS_INTERP_RE
    sites = []
    for interp in interp_re.finditer(value):
        operands = interp.group("body").split("||")
        for pos, operand in enumerate(operands):
            match = _STEPS_OUTPUT_OPERAND_RE.match(operand)
            if match is None:
                continue
            lead = match.group("lead")
            if pos:
                lead = "||".join(operands[:pos]) + "||" + lead
            step_id = match.group("id") or match.group("id_idx")
            out = match.group("out") or match.group("out_idx")
            sites.append((step_id, out, _lead_reaches_output(lead)))
    return sites


# Recorded in place of a site list when the LOOSE tier sees a step output the
# STRICT tier could not account for. A module constant so the walk, the
# recorder and `check_dir` all name the same state.
UNPARSED = "unparsed"


def _steps_output_sites(line, cont=False):
    """Every step output `line`'s `ref:` reads, `UNPARSED`, or None (BE-8253).

    None when the value names no step output at all — the common case, and the
    answer for prose that merely mentions one, since comments are stripped
    first for the same reason `_pins_to_job_workflow_sha` strips them:
    `ref: main  # was ${{ steps.x.outputs.ref }}` is not a checkout resolved
    from a step output. The loose tier runs only on the comment-stripped VALUE,
    so it cannot fire on that line either.

    `UNPARSED` when the loose tier counts more mentions than the strict tier
    accounted for. The value plainly reaches a step output and this reader
    cannot say WHICH, or under what condition — so it must not answer "no
    site", which is the fail-open this state closes: the lint passed
    `${{ steps.x.outputs.ref || format('refs/heads/{0}', 'main') }}` while
    failing the identical workflow spelled bare.

    Otherwise the list, one tuple per operand, for `_record_steps_output` to
    judge INDEPENDENTLY. `cont=True` judges a CONTINUATION line instead: the
    value under a `ref: >-` / `ref: |` key, which carries no `ref:` of its own.
    """
    # Asked of nearly every line of every job (the `else` arm of the walk), and
    # a `steps.`/`steps[` substring is a precondition of every pattern below —
    # so answer the common case before paying for the comment strip and the
    # scan.
    if "steps." not in line and "steps[" not in line:
        return None
    code = _strip_comment(line)
    for value, flow in _ref_values(code, cont):
        mentions = len(_LOOSE_STEPS_OUTPUT_RE.findall(value))
        if not mentions:
            continue
        sites = _strict_steps_output_sites(value, flow)
        # A strictly-read operand contains exactly one mention and operands do
        # not overlap, so the strict tier can never account for MORE than the
        # loose tier saw; fewer means at least one mention went unread.
        return sites if len(sites) >= mentions else UNPARSED
    return None


def steps_output_ref(line, cont=False):
    """(step id, output name) for the FIRST step output `line`'s `ref:` reads.

    Answers only "which step produced this ref" — never whether the checkout is
    covered. `ref_checkouts` decides that, because the answer depends on which
    steps the job has already walked past. None when the value reads none, and
    None for an `UNPARSED` value too: there is no id to name where the reader
    could not read one.
    """
    sites = _steps_output_sites(line, cont)
    if not isinstance(sites, list) or not sites:
        return None
    step_id, out, _ = sites[0]
    return step_id, out


def _reported_step_output(line, state):
    """The `steps.<id>.outputs.<out>` an error message for `state` should NAME.

    A `ref:` can read SEVERAL step outputs (BE-8253) and only one of them need
    be the offending operand, so naming the first would point the author at a
    sibling that is fine. `'non-leading'` is answerable from the line alone —
    it is the first operand something precedes — while `'dangling'` is a
    property of the JOB, not the line, so a multi-output value falls back to
    the placeholders rather than naming the wrong step. Same fallback the
    `ref: >-` continuation spelling already takes, which reports the KEY line
    and keeps its value below it.
    """
    sites = _steps_output_sites(line)
    if not isinstance(sites, list) or not sites:
        return "<id>", "<out>"
    if state == "non-leading":
        for step_id, out, leading in sites:
            if not leading:
                return step_id, out
    if len(sites) == 1:
        return sites[0][0], sites[0][1]
    return "<id>", "<out>"


def _consumes_input(text):
    """True when `text` uses the input as a mapping value in any YAML style."""
    return bool(
        _CONSUMES_BLOCK_RE.search(text)
        or _CONSUMES_FLOW_RE.search(text)
        or _CONSUMES_SCALAR_RE.search(text)
    )


def _strip_comment(value):
    """Drop a trailing `# …` comment from a scalar value.

    Quote-aware, on the shared `_quote_mask` scan (`''` inside a single-quoted
    scalar, `\\"` inside a double-quoted one): a `#` that sits inside a quoted
    entry's own value (`MSG: "a # b"`) is string content, not a comment opener,
    and stripping from the FIRST `#` on the line — as a plain regex does —
    truncates every sibling entry after it too. Falls back to the whole value
    when no unquoted `#` is found.

    Deliberately WITHOUT the scalar-start rule the structural readers use. This
    one is asked about every physical line, `run: |` script bodies included,
    where `echo "PR #${n}"` is literal text: strictly, that `"` follows a plain
    word and opens nothing, so the ` #` would read as a comment and truncate a
    line whose tail may carry the `ref:` this lint exists to find. Opening on
    any quote over-protects such a line instead, which is the safe direction
    for a reader that cannot see whether it is looking at YAML or at shell.
    """
    mask = _quote_mask(value, node_start_only=False)
    for i, ch in enumerate(value):
        if ch == "#" and mask[i] and (i == 0 or value[i - 1].isspace()):
            return value[:i].strip()
    return value.strip()


def _flow_brace_delta(text):
    """`{` minus `}` in `text`, counting only braces OUTSIDE quoted scalars.

    `_job_step_ids` tracks a flow mapping that spans physical lines by the
    braces it has left unbalanced, and a raw `text.count("{")` counts braces
    that are string CONTENT: `- {run: "echo {"}` wedges flow mode open for the
    rest of the job, `- {name: "}"}` closes it a character early. Neither
    miscount is harmless — while flow mode is open every later line is read
    column-agnostically, which is exactly how a `run:` heredoc emitting
    `- id: x` registers the phantom step that reader exists to exclude, while
    a real block item (`- id: resolve_ref`, matching neither flow nor
    block-at-column pattern there) stops being read at all and its consumer
    becomes a false `dangling` failure.

    On the shared `_quote_mask` scan, in its STRICT reading — a quote opens a
    scalar only where a YAML node can start — so the apostrophe of a plain
    `- {name: don't, id: real}` leaves the closing `}` counted instead of
    wedging the mapping open. (`_strip_comment` takes the weaker reading of the
    same scan, deliberately; see `_quote_mask`.) Per LINE, like every reader
    here: a quote opened on one physical line and closed on the next is out of
    scope, as it is for `_strip_comment`.
    """
    mask = _quote_mask(text)
    return sum(
        1 if ch == "{" else -1
        for i, ch in enumerate(text)
        if ch in "{}" and mask[i]
    )


def _step_id_value(raw):
    """The step id `raw` names, or None when it cannot be one.

    Drops the closing punctuation a flow CONTINUATION line leaves on the block
    pattern's `\\S+` (`- {uses: x,` / `id: real}` captures `real}`, and no
    legitimate id ends in `,` or `}`), then ONE matched quote wrapper. What is
    left may contain no quote at all: an Actions step id is `[A-Za-z0-9_-]`, so
    a stray `'`/`"` means this is not an id but the tail of a quoted scalar
    that OPENED on an earlier physical line — `- {name: "a` / `  id: phantom",
    …}` — which would otherwise register the exact phantom this reader exists
    to exclude, silencing a genuine `dangling` verdict.

    NARROWING only: no legal step id carries a quote, so this cannot refuse a
    real one and manufacture the false failure under-collection costs here.
    """
    value = raw.strip().rstrip(",}")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    if not value or "'" in value or '"' in value:
        return None
    return value


def _collect_flow_step_ids(ids, text, i):
    """Register every flow-mapping `id:` on `text`, keyed to its line `i`.

    `_STEP_ID_FLOW_RE` asks only for a preceding `[{,]`, and a comma that is
    string CONTENT meets that boundary as readily as a real entry separator:
    in `- {run: "build, id: phantom", uses: org/act@sha}` the comma sits
    inside the quoted scalar, so a step nothing declares registers and
    SILENCES the dangling verdict on a genuine finding — the same phantom
    this reader exists to exclude, arriving through another door. So the
    boundary is held to the shared `_quote_mask` scan — the same test
    `_pins_to_job_workflow_sha` and `_ref_values` already apply before
    trusting a flow match of their own, hoisted to ONE pass so many candidates
    on one long flow line cost O(len) rather than O(matches x len).

    Narrowing, never widening: a real `id:` is only ever reached across an
    unquoted `{` or `,`, so the guard cannot drop one and manufacture the
    false `dangling` failure under-collection costs here.

    A quote only opens a scalar where a node can START, so the apostrophe of a
    plain `- {name: don't, id: real}` no longer hides the real entry comma.

    Per LINE, like every reader here: a quote opened on one physical line and
    closed on the next is out of scope, as it is for `_flow_brace_delta`.
    """
    mask = _quote_mask(text)
    for flow in _STEP_ID_FLOW_RE.finditer(text):
        if mask[flow.start()]:
            value = _step_id_value(flow.group(2))
            if value is not None:
                ids.setdefault(value, i)


# `&anchor` / `!tag` node properties, which may precede a node's own content.
_NODE_PROPERTIES_RE = re.compile(r"""^(?:[&!]\S*[^\S\n]+)+""")


def _after_node_properties(value):
    """`value` past any leading `&anchor` / `!tag` node properties.

    A step item may carry either in front of its mapping — `- &resolver {id:
    resolve_ref, run: echo hi}` is valid YAML that Actions accepts — and the
    flow-mapping gate in `_job_step_ids` tests for a `{` where the item's
    content begins. Left unskipped the property defeats that gate: the item
    falls through to the block pattern, which cannot match a line beginning
    `&resolver {`, its real id is never collected, and a later
    `steps.resolve_ref` consumer becomes a false `dangling` FAILURE — the
    costly direction.

    Only ever consulted to ask "does the content begin `{`", so skipping a
    token that is not really a property cannot widen what is collected.
    """
    return _NODE_PROPERTIES_RE.sub("", value)


def _is_skippable(line):
    """Blank lines and whole-line comments never open or close a YAML block."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _marker_width(line):
    """Columns the leading `- ` list marker and its separation spaces occupy.

    0 when `line` is not a list item. YAML allows ANY run of separation spaces
    after the `-`, so this is measured rather than assumed to be 2: a step
    written `-  id: resolve` (siblings aligned at dash+3) is as valid as
    `- id: resolve`, and reading it as a fixed two-column marker leaves the key
    one column deeper than the step's real key column.
    """
    stripped = line.rstrip().lstrip(" ")
    if not stripped.startswith("- "):
        # Includes the bare `-` that puts every key on the lines BELOW it —
        # with or without trailing whitespace, which is why the rstrip comes
        # first: `-` followed by nothing but spaces declares no key either,
        # so there is no column to recover from it.
        return 0
    return len(stripped) - len(stripped[1:].lstrip(" "))


def _opens_list_item(line):
    """True when `line` opens a YAML sequence entry — `- key: v` or a bare `-`."""
    stripped = line.lstrip(" ")
    return stripped == "-" or stripped.startswith("- ")


def _unmarked(line, key_indent):
    """`line` rewritten so a key written after its `- ` marker reads at `key_indent`.

    A step's first key may be written on the marker line itself (`- id: resolve`,
    `- continue-on-error: true`), where it declares that key at the step's key
    column exactly as a later line of its own does — but `_indent` reads the
    marker's physical column, so every `_indent(line) != key_indent` scan skips
    it. Rewriting normalizes it back into view. Lines that are not list items
    pass through untouched.
    """
    if not _marker_width(line):
        return line
    return " " * key_indent + line.lstrip(" ")[1:].lstrip(" ")


def _dedash(line):
    """`line` with a leading `- ` list marker rewritten as two spaces.

    The marker occupies the step's key column, so `- with:` declares that key
    exactly where a later `with:` line does. `_binding_step_id` and
    `_skips_on_empty_output` make the same normalization inline, against a
    `key_indent` they already hold; this is the form for readers that must
    DERIVE the column, so they cannot drift apart. Column-preserving: the key
    lands where YAML puts it, which is what indentation comparisons need.
    """
    stripped = line.lstrip()
    if not stripped.startswith("- "):
        return line
    return line[: _indent(line)] + "  " + stripped[2:]


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


_STEPS_KEY_RE = re.compile(r"""^\s*(['"]?)steps\1\s*:[^\S\n]*(?:#.*)?$""")


def _in_steps_sequence(lines, marker):
    """True when the list item opened at `marker` is an entry of a `steps:` sequence.

    A real step's `- ` marker is a direct child of `steps:`, so the nearest
    shallower non-skippable line above it is the `steps:` key itself (sibling
    steps sit at the SAME indent and their bodies deeper, so neither can be
    the first strictly-shallower line). Anything else — an `include:` item
    under `strategy.matrix`, a `- …`-shaped line of shell text inside a
    `run: |` block scalar — is a list item this lint must never read as a
    step: crediting one as a guard turns arbitrary text into a job-wide
    verdict, fail-open.
    """
    ind = _indent(lines[marker])
    for j in range(marker - 1, -1, -1):
        if _is_skippable(lines[j]):
            continue
        if _indent(lines[j]) < ind:
            return bool(_STEPS_KEY_RE.match(lines[j]))
    return False


def _step_bounds(lines, idx):
    """(start, end, key_indent) of the STEP holding the binding at `idx`.

    None when the binding is not inside a step at all — a job-level `env:`
    hoists the value out of every step, which is a binding but not a guard —
    and, fail-closed, for any list item that is not an entry of a `steps:`
    sequence (see `_in_steps_sequence`).
    """
    marker = _marker_width(lines[idx])
    if marker:
        # The asked line ITSELF opens the step, its first key holding the
        # binding in flow form (`- env: {WORKFLOWS_REF: …}`,
        # `- with: {…, ref: …}`). The step is this line: resolving it here —
        # rather than letting the parent scan below walk past it — is also
        # what keeps `_consuming_step_bounds`'s shifted retry from stopping
        # at the PRECEDING sibling step and crediting this checkout with its
        # neighbour's `if:`.
        key_indent = _indent(lines[idx]) + marker
        if not _in_steps_sequence(lines, idx):
            return None
        return idx, _step_end(lines, idx, key_indent), key_indent

    ind = _indent(lines[idx])
    key_indent = None  # the step's own key column, i.e. where `env:`/`run:` sit
    parent = None     # the line that opened the block the binding sits in
    for j in range(idx - 1, -1, -1):
        if _is_skippable(lines[j]):
            continue
        if _indent(lines[j]) < ind:
            parent = j
            key_indent = _indent(lines[j])
            break
    if key_indent is None:
        return None

    marker = _marker_width(lines[parent])
    if marker:
        # The block holding the binding hangs off the step's FIRST key and that
        # key is written on the marker line (`- env:` with `id:`/`run:` below).
        # The marker's physical column is NOT the step's key column — the key
        # sits after the marker — so read the column from where the key
        # actually starts, and take this line as the step's start. Reading the
        # physical column instead left the scan below looking for a `- ` line
        # shallower than the marker, finding none, and answering None:
        # `is_guard_step` then failed closed (harmless), but `_binding_step_id`
        # never registered the resolver and a `ref: ${{ steps.<id>.outputs.ref }}`
        # reading it passed the lint unreported.
        #
        # Two gates keep this branch fail-closed. The item must be a real step
        # (`_in_steps_sequence`), and the asked line must actually SIT inside
        # the item's bounds — the nearest-shallower scan above can land on the
        # marker of the PREVIOUS step when `idx` is a shifted copy of a
        # marker-line consumer, and answering with that step's bounds credits
        # this checkout with its neighbour's `if:`.
        key_indent += marker
        end = _step_end(lines, parent, key_indent)
        if idx >= end or not _in_steps_sequence(lines, parent):
            return None
        return parent, end, key_indent

    start = None  # the step's `- …` list-item line
    for j in range(idx, -1, -1):
        if _is_skippable(lines[j]) or _indent(lines[j]) >= key_indent:
            continue
        if _opens_list_item(lines[j]):
            # A bare `-` opens the step just as `- name: …` does, with every
            # key on the lines below it. Requiring `- ` here read it as "not a
            # step" and answered None, which registers no resolver — so the
            # consumer of its output passed the lint unreported.
            start = j
        break  # first shallower line decides it: a step, or not one at all
    if start is None or not _in_steps_sequence(lines, start):
        return None

    return start, _step_end(lines, start, key_indent), key_indent


def _step_end(lines, start, key_indent):
    """First line past the step opened at `start`: the next line above `key_indent`."""
    for j in range(start + 1, len(lines)):
        if _is_skippable(lines[j]):
            continue
        if _indent(lines[j]) < key_indent:
            return j
    return len(lines)


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
    if body:
        # The list marker occupies the step's key column, so a first key
        # written on it (`- continue-on-error: true`, `- if: …`,
        # `- run: [ -z "$WORKFLOWS_REF" ] && exit 1`) declares that key there
        # exactly as a later line of its own does — the same rewrite
        # `_binding_step_id` makes to read an `id:` off it. Normalized ONCE,
        # because BOTH scans below read the line: without it the disqualifier
        # scan lets a never-fail step written marker-first score as a hard
        # guard, and the guard-detection scan misses a one-line guard written
        # marker-first — reporting the checkout behind a guard the author
        # already has.
        body[0] = _unmarked(body[0], key_indent)

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

    The whole-step FLOW spelling is read too (BE-9099). `_GUARD_BINDING_FLOW_RE`
    already recognizes `- {id: r, env: {WORKFLOWS_REF: ${{ inputs.workflows_ref
    }}}, run: …}` as a binding, but the block `id:` pattern cannot match a line
    opening `{`, so the step registered no id — and an unregistered resolver is
    not a loud failure, it is a SILENT one: `_record_steps_output` reads a later
    `ref: ${{ steps.r.outputs.ref }}` as "a real earlier step this lint has no
    claim on" and drops it with no verdict at all. Spelling the resolver
    flow-style therefore switched the BE-8130/BE-8221 requirement off for its
    consumer — the same fail-open this reader's consumer-side twin closes.

    Gated exactly as `_job_step_ids` gates its own flow reading: ONLY where a
    flow mapping actually OPENS, on a key-column line whose content begins `{`.
    Applied to a block key line the flow pattern also matches `with: {id: x}`,
    where `id` is an action INPUT and no step is declared at all — the phantom
    step that would register a non-resolver and demand an `if:` of a consumer
    this lint has no claim on. Ids are read through the same `_collect_flow_step_ids`
    quote-mask scan, so a `,` inside a quoted scalar cannot smuggle one in.
    """
    bounds = _step_bounds(lines, idx)
    if bounds is None:
        return None
    start, end, key_indent = bounds
    for j in range(start, end):
        line = lines[j]
        if _is_skippable(line):
            continue
        if j == start:
            # The list marker occupies the step's key column, so `- id: resolve`
            # declares the id there exactly as a later `id:` line does.
            line = _unmarked(line, key_indent)
        if _indent(line) != key_indent:
            continue
        text = _strip_comment(line)
        if _after_node_properties(text.lstrip()).startswith("{"):
            # The step written as one flow mapping — on the marker line
            # (`- {id: r, …}`) or on the first member line of a bare `-`.
            flow_ids = {}
            _collect_flow_step_ids(flow_ids, text, j)
            for value in flow_ids:
                return value
            continue
        match = _STEP_ID_RE.match(line)
        if match:
            # Read through the same narrowing `_job_step_ids` applies, so the
            # two readers cannot disagree about WHICH id a step declares. They
            # did: `_STEP_ID_RE`'s `\S+` keeps the `}` a flow CONTINUATION line
            # leaves on it (`- {env: {…},` / `id: r}` → `r}`), while the
            # pre-scan reads `r` — and a resolver registered under an id no
            # consumer can name is not a loud failure but the same SILENT drop
            # BE-9099 closes one spelling over. `_step_id_value` only ever
            # narrows, so it cannot manufacture a registration either.
            value = _step_id_value(match.group(2))
            if value is not None:
                return value
    return None


def _job_step_ids(lines, start, job_indent):
    """{step id: first line index} over the STEP ITEMS of the job opened at `start`.

    A pre-scan ahead of the walk (BE-8215), because the question it answers is
    different from `resolvers`': not "which resolver covers this ref" but "does
    the id exist AT ALL in this job, and where". `_record_steps_output` reads
    it to tell a real earlier step this lint has no claim on (dropped) from an
    id no step declares (reported `'dangling'`), so an id collected from a line
    that declares NO step silences a real finding — the shape BE-8251 verified
    empirically, in two spellings: an action input literally named `id:` under
    an earlier step's `with:`, and a `run:` block scalar whose heredoc emits a
    line beginning `- id:` / `"id":` (fixture YAML this repo's own workflows
    write). A flat line scan reads both as declarations.

    So the scan walks the job's `steps:` ITEMS and reads each id exactly where
    `_binding_step_id` does — at the item's own key column, with a key written
    on the `- ` marker line normalized back into that column. That is what
    excludes both shapes structurally rather than by pattern: a `with:` member
    is nested DEEPER than the step's keys, and YAML requires a block scalar's
    content to be deeper than the key that opens it, so neither can ever land
    on the key column. Block scalars are additionally skipped outright, as
    belt-and-braces for odd indentation-indicator edges.

    Flow spellings are read too, but only where a flow mapping actually opens:
    on a marker line whose value begins `{` (`- {id: x, run: …}`), on the first
    member line of a bare `-` when THAT begins `{`, and on the continuation
    lines of one that spans several physical lines (`- {uses: x,` / `id: y}`)
    — tracked by braces counted OUTSIDE quoted scalars (`_flow_brace_delta`;
    a raw count reads `run: "echo {"` as opening one). Reading the flow
    pattern on every line is how `with: {id: x}` registered a phantom step.

    Reading items rather than lines means accepting every spelling of an item
    YAML has, because a missed id manufactures a false FAILURE on a compliant
    workflow. So the walk also reads: the INDENTLESS sequence, where `steps:`
    and its `- ` items share a column (valid YAML, and Actions accepts it), so
    the sequence is looked for at or DEEPER than the key's own indent and
    closed by the first non-item line back at the marker column; a marker line whose remainder is
    only a comment (`-   # set up`), which declares no key and leaves the
    column to the members below, hence the width measured on the
    comment-STRIPPED line; and a flow continuation dedented past the dash,
    which `{ … }` permits — the sequence-closing break is suppressed while a
    mapping is open, but never past the JOB, which bounds a `flow_depth` a
    miscount left stuck.

    Returns None — "unknown", not "empty" — when the job HAS a `steps:` this
    walk cannot stand behind: written flow-style on the key line
    (`steps: [ … ]`), or opening no `- ` item. The caller then keeps the
    pre-BE-8215 fail-open drop for that job's sites — every verdict that rests
    on "no step of that id exists", so `'dangling'` AND the `'non-leading'`
    one an absent id also reaches, exactly as a real earlier out-of-scope step
    drops both today. Resolver and guard verdicts are untouched, here and
    everywhere. UNDER-collection is the costly direction here:
    a missed id turns a compliant workflow into a false FAILURE, while a
    dropped site merely reproduces the coverage this lint had before the
    dangling check existed. Over-collection stays tolerated for the same
    reason — a residue of it survives inside a flow mapping, where the flat
    pattern cannot see nesting.

    NO `steps:` key is not that case: it is the empty map, and it is the
    ordinary shape of a reusable-workflow CALLER job (`uses:` + `with:`), which
    declares no step and where `steps.<id>.outputs.<out>` is therefore `''` at
    runtime just as a typo'd id is. Answering "unknown" there would drop a real
    finding on the one job shape that cannot possibly have the step.
    """
    job_child = _first_child_indent(lines, start, job_indent)
    if job_child is None:
        return {}
    steps_line = _find_key(lines, "steps", start, job_child, job_indent)
    if steps_line is None:
        return {}
    if _strip_comment(lines[steps_line].split(":", 1)[1]):
        # A value on the key line — `steps: [ … ]`, an anchor, anything. There
        # are no item lines to walk, so answer "unknown" rather than "none".
        return None

    steps_indent = _indent(lines[steps_line])
    marker_column = None
    for i in range(steps_line + 1, len(lines)):
        line = lines[i]
        if _is_skippable(line):
            continue
        # The FIRST line under `steps:` opens the sequence; anything else is a
        # shape this walk has no reading of. At or DEEPER than the key's own
        # column, because YAML lets a block sequence sit at its key's column —
        # `steps:` and `- uses: …` both at 4, the "indentless" style Actions
        # accepts and plenty of real workflows write. Reading only the
        # strictly-deeper body finds no item there and escapes a job this walk
        # reads perfectly well, silently dropping its dangling verdicts.
        if _indent(line) >= steps_indent and _opens_list_item(line):
            marker_column = _indent(line)
        break
    if marker_column is None:
        return None

    ids = {}
    key_column = None     # the current item's key column, None until a bare `-` names it
    scalar_indent = None  # indent of the key holding an open `|`/`>` block scalar
    flow_depth = 0        # unbalanced `{` from a flow mapping opened on a marker line
    for i in range(steps_line + 1, len(lines)):
        line = lines[i]
        if _is_skippable(line):
            continue
        indent = _indent(line)
        if indent < marker_column and (not flow_depth or indent <= job_indent):
            # Back out at the sequence's own column: the list is closed and
            # this line belongs to the job (or to `jobs:`). A flow mapping
            # spanning several physical lines is exempt — YAML constrains no
            # indentation inside `{ … }`, so a dedented continuation closes
            # nothing and breaking there loses every id below it — but never
            # past the JOB, which bounds a `flow_depth` this reader left open.
            break
        if scalar_indent is not None:
            if indent > scalar_indent:
                # Literal TEXT — a shell script, a heredoc emitting fixture
                # YAML. Never workflow structure. Same discipline `ref_checkouts`
                # applies to its `pending` scalar.
                continue
            scalar_indent = None
        if flow_depth:
            text = _strip_comment(line)
            _collect_flow_step_ids(ids, text, i)
            match = _STEP_ID_RE.match(line)
            if match:
                # `_step_id_value` both trims the closing punctuation this
                # continuation-line spelling leaves on the block pattern's
                # `\S+` (`- {uses: x,` / `id: y}`) and refuses a value
                # carrying a stray quote — the tail of a scalar that opened on
                # an earlier physical line, which is no id at all.
                value = _step_id_value(match.group(2))
                if value is not None:
                    ids.setdefault(value, i)
            flow_depth = max(0, flow_depth + _flow_brace_delta(text))
            continue
        if indent == marker_column and not _opens_list_item(line):
            # A line at the sequence's OWN column that opens no item ends the
            # sequence — the shape that exists only in the indentless style,
            # where `steps:`'s sibling keys share the marker column. Every
            # member of an item sits at `key_column`, which is strictly deeper
            # than the marker in both styles, so no line inside the sequence
            # can land here.
            break
        if indent == marker_column:
            text = _strip_comment(line)
            # Measured on the COMMENT-STRIPPED line: `-   # set up` declares no
            # key at all — YAML puts that item's keys on the lines below at the
            # usual marker width — so measuring the raw line locks `key_column`
            # to the comment's column and the item's real `id:` is then never
            # read, the under-collection direction this reader calls costly.
            marker = _marker_width(text)
            if not marker:
                # A bare `-` puts every key on the lines BELOW it, so the
                # column is not recoverable from the marker — the first member
                # line names it.
                key_column = None
                continue
            key_column = marker_column + marker
            if _after_node_properties(text[1:].lstrip()).startswith("{"):
                # The whole step written as a flow mapping. ONLY this shape is
                # read with the flow pattern: applied to any marker line it
                # also matches `- with: {id: x}`, where `id` is an action
                # INPUT and no step is declared at all.
                _collect_flow_step_ids(ids, text, i)
                flow_depth = max(0, _flow_brace_delta(text))
                continue
            # A key written on the marker line declares it at the step's key
            # column exactly as a line of its own does — see `_binding_step_id`.
            line = _unmarked(line, key_column)
        elif key_column is None:
            text = _strip_comment(line)
            if _after_node_properties(text).startswith("{"):
                # A bare `-` whose item is written flow-style on the line
                # BELOW it — the same mapping as `- {id: x, …}`, one line
                # down. Read with the block pattern alone a line opening `{`
                # matches nothing and the item's real id is lost.
                _collect_flow_step_ids(ids, text, i)
                flow_depth = max(0, _flow_brace_delta(text))
                continue
            # First member line after a bare `-` — its indent IS the item's
            # key column.
            key_column = indent
        indent = _indent(line)  # re-read: the marker rewrite above moves it
        if indent >= key_column and _BLOCK_SCALAR_OPEN_RE.match(line):
            # `run: |`, `script: >-` — at the item's key column or nested
            # under it. YAML already puts the content deeper than this key, so
            # the key-column gate below would drop it anyway; tracking the
            # scalar outright is the belt-and-braces the indentation-indicator
            # spellings (`|2`, `>-3`) earn.
            scalar_indent = indent
        if indent != key_column:
            continue
        match = _STEP_ID_RE.match(line)
        if match:
            value = _step_id_value(match.group(2))
            if value is not None:
                ids.setdefault(value, i)
    return ids


# A step's `with:` key, block form and flow form (`- {uses: …, with: {…}}`).
_WITH_KEY_RE = re.compile(r"""^\s*(['"]?)with\1\s*:""")
_WITH_FLOW_RE = re.compile(r"""(?:^|[{,\s])(['"]?)with\1\s*:\s*\{""")
# A key whose value is a BLOCK SCALAR (`run: |`, `script: >-`). Everything
# indented past it is literal TEXT — a shell script, a heredoc emitting fixture
# YAML — and must never be read as workflow structure. `\d*` is the explicit
# indentation indicator, `[+-]?` the chomping indicator; both are optional and
# may appear in either order, but Actions workflows in the wild write at most
# one, so accepting `|`/`>` plus an optional suffix is enough.
_BLOCK_SCALAR_OPEN_RE = re.compile(
    r"""^\s*(['"]?)[A-Za-z0-9_.-]+\1\s*:[^\S\n]*[|>][+-]?\d*[^\S\n]*(?:#.*)?$"""
)


def _in_block_scalar(lines, idx):
    """True when `lines[idx]` is TEXT inside an open `|`/`>` block scalar.

    `_is_ref_input` answers "which key encloses this `ref:`" from indentation,
    and indentation cannot by itself tell a step's real `with:` from the word
    `with:` printed by a heredoc: a `run: |` emitting a whole STEP —

        run: |
          cat <<'EOF' > f.yml
          with:
            ref: ${{ steps.x.outputs.ref }}
          EOF

    — puts a `with:` line at exactly the indent the backward walk stops on, so
    script output was judged a checkout and hard-failed a compliant workflow
    with the dangling error. Tracking the open scalar answers it directly:
    inside one, there is no enclosing key to find, because there is no YAML.

    Scanned forward, since a block scalar is opened by its key and closed by
    the first non-skippable line back at or above that key's column. `idx`
    itself is never consulted as an opener — the `ref: >-` continuation
    spelling is reported at its own KEY line, which OPENS a scalar rather than
    sitting inside one.
    """
    open_indent = None
    for j in range(idx):
        if _is_skippable(lines[j]):
            continue
        line = _dedash(lines[j])
        ind = _indent(line)
        if open_indent is not None:
            if ind > open_indent:
                continue
            open_indent = None
        if _BLOCK_SCALAR_OPEN_RE.match(line):
            open_indent = ind
    return open_indent is not None and _indent(lines[idx]) > open_indent


def _is_ref_input(lines, idx):
    """True when the `ref:` at `idx` is a step's `with:` INPUT — a checkout.

    The walk asks EVERY line of the job about step-output refs, so `ref:` keys
    that are not action inputs reach `_record_steps_output` too: a job-level
    `outputs:` block (`ref: ${{ steps.pick.outputs.ref }}`, which conventionally
    sits ABOVE `steps:` and therefore above every step id) and a `run:` heredoc
    emitting fixture YAML, a shape this repo itself uses. Those used to be
    dropped harmlessly by the resolver early return; under the dangling check
    they would instead HARD-FAIL a compliant workflow with an error naming a
    checkout at a mutable ref where there is no checkout at all. So the gate is
    the one thing every real site has and neither of those does: a checkout's
    `ref:` is an entry of its step's `with:` mapping.

    An open `|`/`>` block scalar is tracked first, because indentation alone
    cannot tell a step's real `with:` from one a `run:` script PRINTS — see
    `_in_block_scalar`.

    Answered from the enclosing key rather than from `_consuming_step_bounds`,
    which resolves the `- ` list item and so cannot see the difference between
    `with:` and `run:` — a heredoc line is inside a step exactly as an input
    is. The flow spelling writes `with: {…, ref: …}` on one line, so that line
    carries its own answer; the continuation spelling (`ref: >-`) is judged from
    its KEY line, which is where the walk reports it.

    Fail-open when the enclosing key is not `with:` (return False → the site is
    dropped), because that is the behavior every one of these shapes had before
    the dangling check existed. Narrowing a false FAILURE back to the old miss
    is the safe direction; the reverse is not.
    """
    if _in_block_scalar(lines, idx):
        # Script text, not YAML — asked FIRST, so it also closes the flow
        # spelling's copy of the same hole (a heredoc line reading
        # `with: {ref: "${{ … }}"}` carries its own false answer).
        return False
    if _WITH_FLOW_RE.search(_strip_comment(lines[idx])):
        return True
    ind = _indent(lines[idx])
    for j in range(idx - 1, -1, -1):
        if _is_skippable(lines[j]):
            continue
        if _indent(lines[j]) < ind:
            # `_dedash` because `with:` may ride the list-item line: mapping
            # keys are unordered, so `- with:` / `ref: …` / `uses: checkout`
            # is valid Actions YAML. Without it the marker holds the key
            # column, the match fails, and the site is DROPPED — turning the
            # fail-closed report this gate protects into a silent fail-open on
            # exactly the shape the lint exists to close, one an author could
            # pick deliberately to evade it.
            return bool(_WITH_KEY_RE.match(_dedash(lines[j])))
    return False


def _consuming_step_bounds(lines, idx):
    """`_step_bounds` for a `ref:` line, tolerating the flow form.

    `_step_bounds` now resolves every consuming spelling directly: a block
    `ref:` sits under `with:`, one level deeper, and a flow `with: {…, ref: …}`
    resolves off the marker line above it — or, written marker-first
    (`- with: {…}`), off the asked line itself. The shifted retry below is a
    belt-and-braces backstop for a spelling the direct paths miss, kept only
    because `_step_bounds`'s bounds gates make it safe: it can no longer stop
    at the PRECEDING step's marker and answer with bounds that exclude the
    asked line, which is what used to credit a marker-first flow consumer with
    its neighbour's `if:`.
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
        if j == start:
            # `- if: steps.x.outputs.ref != ''` as the consuming step's first
            # key is the remedy, written marker-first. Normalized into the key
            # column like `is_guard_step` and `_binding_step_id` do, so the one
            # accepted remedy is not refused for its spelling — with the
            # resolver exemption gone (BE-8221) this `if:` is the ONLY coverage
            # route, so missing it reports a checkout that is in fact guarded.
            line = _unmarked(line, key_indent)
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


def _record_steps_output(found, lines, idx, sites, resolvers, step_ids, drop=None):
    """Record the `ref:` at `idx`, which reads `steps.<id>.outputs.<out>`.

    `sites` is EVERY step output the value reads (BE-8253), and every one of
    them is judged INDEPENDENTLY: an operand reaching an uncovered step output
    is not excused by a covered sibling, and for a value concatenating two
    interpolations both feed the ref, so both must be covered. The strongest
    requirement wins — the site is guarded only when all of them are.

    `sites` is `UNPARSED` when the value plainly reads a step output the strict
    reader could not account for. There is nothing to resolve then, and the one
    honest answer is to REFUSE the expression rather than pass it unexamined,
    so the site is recorded `'unparsed'` for `check_dir` to name a supported
    spelling. Gated by `_is_ref_input` like every other state below it: a
    job-level `outputs:` mapping or a `run:` heredoc holding an unparseable
    expression is not a checkout, and hard-failing one is the false-CI-failure
    channel the dangling check already had to close once.

    Records NOTHING only when `<id>` names a real step declared AHEAD of the
    consuming one that never touches `workflows_ref` — a `git rev-parse`, a
    release lookup — which is not this lint's subject, and demanding an
    empty-ref `if:` of it would fail workflows the lint has no claim on.

    That scope question is asked FIRST, ahead of operand order, because it
    holds in every operand position — the lint has no claim on such a step
    whether or not the ref's expression starts with its output.

    `step_ids` is None when the pre-scan could not read this job's `steps:`
    at all (BE-8254). The scope question then has no answer, so every id the
    walk has not already registered as a resolver is DROPPED — the pre-BE-8215
    behavior. That reaches the `'non-leading'` verdict as well as `'dangling'`
    (both rest on "no step of that id exists"), which is also what a real
    earlier out-of-scope step does to both today. Sites whose id names a
    tracked resolver are judged exactly as always.

    `drop`, when a caller passes one, is `(job_start, job_indent, out_list)`
    and `out_list` collects `(job_start, job_indent, idx)` for every SITE that
    escape swallows (BE-9045). The drop itself is right — a fail-CLOSED escape
    manufactures false CI failures out of a pre-scan that could not run — but
    it is INVISIBLE, so reformatting a job's `steps:` into one of those shapes
    is an in-band way to switch that job's dangling check off with nothing in
    the output to say so. The collector is the observability channel and
    nothing more: it never changes a verdict, and `check_dir` turns it into a
    non-fatal `::warning` that leaves the exit status alone.

    ONE parameter, not three, because the list is USELESS without the job
    coordinates: the annotation names the job and points at its `steps:` line.
    Three independently-defaulting parameters let a caller pass the documented
    out-parameter alone and collect `(None, None, idx)`, whose `lines[None]`
    is an exit-1 `TypeError` traceback out of the one path whose entire
    purpose is to be non-fatal. Bundled, that call cannot be written.

    Recorded once per SITE this call leaves with NO `found` entry at all, not
    once per swallowed OPERAND. A site is a line — the unit this function
    itself works in, appending at most one `found` entry per call — so the
    per-operand append counted a two-operand value twice. And a site whose
    SIBLING operand names a tracked resolver still lands in `found`: it WAS
    judged, by the operand this reader could read, so recording it would let
    one `ref:` line carry a BE-8130/BE-8215 `::error` AND be counted in a
    `::warning` saying it went unjudged. Only a site that came away with no
    verdict at all actually lost coverage to the escape.

    That second case has no end-to-end fixture because it cannot be reached
    today: `_step_bounds` refuses the same `steps:` shapes `_job_step_ids`
    does, so no resolver ever registers inside an escaped job and `resolvers`
    is empty there. Written as an invariant rather than as a live fix — the
    two are separate walks over the same key, and the count should stay
    honest if they diverge.

    When `<id>` matches NO step declared before the consuming one (a typo'd
    id, a step in another job, a resolver declared below its consumer), the
    site is recorded as a DANGLING ref use (BE-8215): at runtime the
    expression is `''`, `actions/checkout` reads `''` as the default branch,
    and the checkout runs unconditionally. That used to be silently dropped —
    fail-open on exactly the shape whose runtime behavior is the hole this
    lint exists to close. `via_step_output` carries `'dangling'` (truthy, so
    `unguarded_ref_checkouts` needs no change) for `check_dir`'s message.

    Otherwise it IS a ref use, guarded ONLY when the consuming step carries the
    exact non-empty `if:` on that same output. A fail-closed resolver does NOT
    exempt its consumer (BE-8221): `is_guard_step` proves the step rejects an
    empty INPUT, nothing about the value written to `$GITHUB_OUTPUT` — a
    resolver that guards the input and then sanitizes a malformed ref to `''`,
    or whose output write was dropped or renamed, or a consumer naming an
    output the step never sets, all still hand checkout an empty ref. That
    `if:` route also does not cover an output that is not the expression's
    FIRST `||` operand: `${{ 'main' || steps.<id>.outputs.<out> }}` resolves to
    the literal on every runner, so that spelling is unguarded unconditionally
    — while a TRAILING fallback (`${{ steps.<id>.outputs.<out> || 'main' }}`)
    under a covering `if:` is unreachable dead code and passes.

    Recorded as a NON-fallback site: `uses_fallback` exists to compare a
    checkout's expression against the strength of the guard that covered it,
    and there is no expression to compare here — the `if:` tests the resolved
    VALUE, so guard strength is moot on this path.
    """
    if not _is_ref_input(lines, idx):
        # Not an action input, so not a checkout — a job-level `outputs:`
        # mapping, a `run:` heredoc. Nothing to judge and nothing to fail.
        return
    if sites == UNPARSED:
        found.append((idx + 1, False, False, UNPARSED))
        return
    verdicts = []
    # Did the BE-8254 escape swallow an operand here? Noted, not recorded:
    # the drop is only worth reporting if NO operand of this site reached a
    # verdict — see the `drop` paragraph above.
    escaped = False
    for step_id, out, leading in sites:
        if step_id not in resolvers:
            if step_ids is None:
                escaped = True
                # The job's `steps:` is a shape `_job_step_ids` will not stand
                # behind, so "no such step" and "a real earlier step this lint
                # has no claim on" are indistinguishable here. Drop the site —
                # exactly what this reader did before the dangling check
                # existed — rather than manufacture a false failure out of a
                # pre-scan that could not run. Ahead of the `leading` test on
                # purpose: judging it first would print `'non-leading'` for an
                # id that may well name a real earlier step, and the readable
                # path drops THAT site too (the out-of-scope return below is
                # likewise reached before `leading`). So the escape costs the
                # `'non-leading'` verdict as well, and says so rather than
                # trading a dropped site for a false failure.
                continue
            declared = step_ids.get(step_id)
            bounds = _consuming_step_bounds(lines, idx)
            consumer_start = bounds[0] if bounds is not None else idx
            # A step's OWN id must not excuse a ref consuming it — during the
            # step's `with:` evaluation its output does not exist yet — so the
            # boundary is the consuming STEP's first line, not the `ref:` line.
            if declared is not None and declared < consumer_start:
                # OUT OF SCOPE, and asked ahead of operand order on purpose: a
                # `ref:` resolved from a real earlier step that never touches
                # `workflows_ref` is not this lint's subject in ANY operand
                # position. Judging `leading` first reported
                # `ref: ${{ inputs.pr_sha || steps.detect.outputs.sha }}` — a
                # perfectly ordinary checkout — as 'non-leading', printing an
                # error that demands an operand reorder which CHANGES the
                # workflow's runtime semantics. An operand this lint has no
                # claim on neither fails the site nor vouches for its siblings.
                continue
            if leading:
                # No step of that id runs ahead of the checkout, and the
                # output IS what this operand resolves to: `''` at runtime,
                # which `actions/checkout` reads as the default branch.
                verdicts.append("dangling")
            else:
                # A dangling id whose operand does not lead falls to the
                # non-leading verdict: the leading operand wins, so the output
                # is never consulted, "no such step" is a red herring, and
                # fixing the id changes nothing. Operand order is the only fix.
                verdicts.append("non-leading")
            continue
        # `step_id` names a real resolver-tracked step, so the one question
        # left is operand order — judged before guard strength for the same
        # reason as above: when the leading operand wins, the output is never
        # consulted, so a covered resolver is a red herring and fixing the
        # guard changes nothing.
        if not leading:
            verdicts.append("non-leading")
            continue
        # Coverage is ONLY the consuming step's own exact non-empty `if:` on
        # this output (BE-8221) — `resolvers` proves membership now, nothing
        # about guard strength: a fail-closed resolver proves it rejects an
        # empty INPUT, nothing about the value it writes to `$GITHUB_OUTPUT`
        # (a sanitize-to-`''` branch, or a dropped/renamed output write, still
        # hands the checkout an empty ref).
        verdicts.append(bool(_skips_on_empty_output(lines, idx, step_id, out)))
    if not verdicts:
        # Every operand was out of scope — the same drop the single-operand
        # reader made, for the same reason.
        if escaped and drop is not None:
            # This site produced no `found` entry and at least one operand was
            # swallowed by the escape, so the escape is what cost it its
            # verdict. That is exactly the site `check_dir` warns about.
            job_start, job_indent, out_list = drop
            out_list.append((job_start, job_indent, idx))
        return
    # A failing operand decides the site, and its state decides the REMEDY the
    # message prints, so the two states with their own message come first.
    # `'non-leading'` outranks `'dangling'` for the reason it is judged first
    # above: no change to the id or the step order can fix operand order.
    for state in ("non-leading", "dangling"):
        if state in verdicts:
            found.append((idx + 1, False, False, state))
            return
    found.append((idx + 1, False, all(verdicts), True))


def ref_checkouts(lines, dropped=None):
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
    carry `via_step_output` and are judged by `_record_steps_output` — covered
    ONLY by the exact non-empty `if:` on the consuming step, full stop. A
    fail-closed resolver does not exempt its consumer (BE-8221): `is_guard_step`
    proves input rejection, not output non-emptiness. These sites never earn
    the fallback-strength exemption either, because the `if:` tests the
    resolved value rather than an expression. `via_step_output` is five-state
    (BE-8215, BE-8253), every non-`False` value truthy so the unguarded
    projection needs no change: `False` (the ref names the input), `True` (a
    step output, every operand of it covered), `'dangling'` — a step output
    whose id matches no step declared before the consuming one, so the
    expression is `''` at runtime and the checkout takes the default branch
    unconditionally — `'non-leading'`, a step output the expression does not
    start with, so something ahead of it decides the value — or `'unparsed'`,
    a value that plainly reads a step output in a spelling this reader cannot
    judge at all, which used to record no site and pass.

    `dropped` is an OPTIONAL out-parameter, not a return-shape change
    (BE-9045): the returned tuple is what 49 call sites read, so the
    observability channel for the BE-8254 fail-open is a list the caller
    passes in and `_record_steps_output` appends `(job_start, job_indent,
    idx)` to — once per SITE it leaves unjudged, never per operand. Passing
    one changes no verdict and no return value — only `check_dir` does, and
    only to emit a non-fatal `::warning`.
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
        # Ids of steps in THIS job, seen so far, whose `env:` binds the input —
        # membership alone: registration marks the step as a resolver of the
        # input, and coverage always comes from the consumer's own `if:`
        # (BE-8221). Per job and populated as the walk goes, so a resolver in
        # another job (they run independently) or one declared BELOW its
        # consumer can never be credited; the walk order gives that ordering
        # for free, exactly as it does for the guard flags above.
        resolvers = set()
        # EVERY step id in this job, pre-scanned (BE-8215): the walk-order
        # `resolvers` set cannot tell "an earlier step this lint has no claim
        # on" from "no such step at all", and the two demand opposite answers —
        # the first is out of scope, the second is a ref that is `''` at
        # runtime and must be reported. None when the job's `steps:` defeats
        # the walk (BE-8254) — `_record_steps_output` then drops this job's
        # sites whose id names no tracked resolver, which is every verdict
        # resting on "no such step" (`dangling`, and the `non-leading` one an
        # absent id also reaches). Resolver and guard verdicts are unchanged.
        step_ids = _job_step_ids(lines, start, job_indent)
        # The BE-9045 collector, bound to THIS job's coordinates once so the
        # list can never travel without them (`_record_steps_output`'s `drop`).
        drop = None if dropped is None else (start, job_indent, dropped)
        # An open `ref:` whose value continues below, as (line index, indent).
        # Continuation lines are the more-indented ones that follow; the first
        # line back at or above the key's indent closes the scalar.
        pending = None
        # Those continuation lines, stripped, in order. A block scalar may
        # split the ref's `${{ … }}` across PHYSICAL lines and still fold to
        # one expression at runtime, so matching each line on its own left a
        # single newline enough to hide the very spelling BE-8215 closed:
        # `ref: >-` / `${{ steps.r.outputs.ref ||` / `'main' }}` matched
        # neither continuation arm and recorded no site.
        pending_parts = []
        for i, line in _block_body(lines, start, job_indent):
            if pending is not None:
                if _indent(line) > pending[1]:
                    # Stripped for the same reason the arm selection below
                    # is — a trailing `# … inputs.workflows_ref` on the
                    # continuation is prose, not the value the runtime folds.
                    if mention_re.search(_strip_comment(line)):
                        # Report the `ref:` KEY line (that is the checkout the
                        # reader must find), but judge the CONTINUATION line —
                        # the key never holds the expression, so asking it
                        # whether this is a fallback always answered no.
                        fallback = _pins_to_job_workflow_sha(line)
                        guarded = guarded_input or (fallback and guarded_fallback)
                        found.append((pending[0] + 1, fallback, guarded, False))
                        pending = None
                        continue
                    pending_parts.append(_strip_comment(line))
                    resolved = _steps_output_sites(line, cont=True)
                    if not isinstance(resolved, list) and len(pending_parts) > 1:
                        # Ask the FOLDED value — what the runtime actually
                        # sees — only once more than one physical line has
                        # been seen, so the common one-line spelling keeps its
                        # exact behavior and the join can only ever ADD a
                        # site or complete a parse the lone line could not.
                        joined_resolved = _steps_output_sites(
                            " ".join(pending_parts), cont=True
                        )
                        if joined_resolved is not None:
                            resolved = joined_resolved
                    if isinstance(resolved, list):
                        # A complete parse. The `ref: >-` / `ref: |` spelling
                        # of the resolve-then-consume shape, judged from the
                        # same place as the block form — the KEY line is the
                        # checkout, and it is the key line's own step that
                        # must carry the skip `if:`.
                        _record_steps_output(
                            found, lines, pending[0], resolved, resolvers, step_ids, drop
                        )
                        pending = None
                    elif resolved == UNPARSED:
                        joined = " ".join(pending_parts)
                        if joined.count("${{") <= joined.count("}}"):
                            # The interpolation is already CLOSED — not still
                            # folding across a further line — so no later
                            # line can complete a parse this reader cannot
                            # make. Refuse it now rather than risk the site
                            # being dropped if the scalar has no further line
                            # to trigger the "scalar closed" fall-through
                            # below (the last field of the last step).
                            found.append((pending[0] + 1, False, False, UNPARSED))
                            pending = None
                        # Otherwise the `${{` this line opened is still
                        # unclosed, so keep the scalar pending: a later line
                        # may still complete the strict-tier parse the way
                        # the plain `|| <literal>` fold does.
                    continue
                # Scalar closed — fall through and judge this line normally.
                pending = None
            # Arm selection reads the COMMENT-STRIPPED text, not the raw
            # line: `_REF_USE_BLOCK_RE`'s `.*` otherwise spans a trailing
            # comment, so `ref: ${{ steps.r.outputs.ref }}  # from
            # inputs.workflows_ref` takes the input-use arm and a correctly
            # `if:`-guarded checkout is reported unguarded — and the same `.*`
            # the other way lets a comment carrying `{WORKFLOWS_REF: "${{
            # inputs.workflows_ref }}"}` register a phantom resolver. Same
            # precedent as `_steps_output_sites`, which strips before either
            # tier, and the `_STEP_IF_RE` consumers that read `_unmarked`.
            # ONLY these three classifiers see `code`; every other reader in
            # this loop keeps the RAW line, because `_strip_comment` also drops
            # the leading indentation they judge by column.
            code = _strip_comment(line)
            binding = _GUARD_BINDING_RE.match(code)
            flow_binding = False
            ref_use = binding is None and is_ref_use(code, ref_res)
            if binding is None and not ref_use:
                # The flow binding is only taken for a line that is not ALSO a
                # ref use — a whole step written as one flow mapping can carry
                # both, and the binding branch would swallow the checkout
                # unreported. Reporting wins: fail-closed, as everywhere else.
                binding = _GUARD_BINDING_FLOW_RE.search(code)
                flow_binding = binding is not None
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
                # Site-recording runs BEFORE registration, because the two
                # answer different questions about this ONE physical line: a
                # step written as a single flow mapping can bind
                # `WORKFLOWS_REF` in its `env:` AND read a step output in its
                # `with: {ref: …}`, and the binding arm is not terminal for
                # the second question, or the checkout is never judged at all
                # — no verdict, no drop, no notice (BE-9098). The block `env:`
                # binding can never share a line with `ref:`
                # (`_GUARD_BINDING_RE` is line-anchored), so only the flow
                # spelling reaches here with a site.
                #
                # Recording FIRST is what keeps a SELF-referencing flow step
                # dangling. `_record_steps_output`'s `resolvers` arm carries
                # no `declared < consumer_start` ordering check of its own —
                # only the `step_ids` arm does — so registering this step
                # first would route its own `ref: ${{ steps.<self>.outputs.…
                # }}` down the resolver arm and judge it on an `if:`, when
                # during its OWN `with:` evaluation that output does not exist
                # yet and the expression is '' at runtime. Order, not the
                # narrowness of `_binding_step_id`, is what makes that verdict
                # right — so it survives the reader widening below.
                resolved = _steps_output_sites(line)
                if resolved is not None:
                    _record_steps_output(
                        found, lines, i, resolved, resolvers, step_ids, drop
                    )
                # A step that RESOLVES the ref for a later checkout to consume
                # is registered here, whether or not it guards. Registration
                # only marks the step as a resolver of the input — coverage
                # always comes from the consumer's own `if:` (BE-8221), because
                # a hard guard proves the step rejects an empty INPUT, nothing
                # about what it writes to `$GITHUB_OUTPUT`. Guard verdict and
                # strength (bare vs `|| job.workflow_sha`) are therefore not
                # recorded at all: they answer questions about an EXPRESSION,
                # while the consumer's `if:` tests the actual resolved VALUE.
                step_id = _binding_step_id(lines, i)
                if step_id is not None:
                    resolvers.add(step_id)
                # Registration (above) has to see the flow spelling — an
                # unregistered resolver leaves its `steps.<id>.outputs.ref`
                # consumer unjudged entirely. But excusing a checkout that
                # consumes the INPUT directly is the unsafe direction to
                # widen (ref-use recognition may only ever DEMAND a guard,
                # never the reverse — see `is_ref_use`'s callers) — so a
                # flow-form binding registers as a resolver without ALSO
                # earning `guarded_input`/`guarded_fallback` credit.
                if guard and not flow_binding:
                    if binding.group("fallback"):
                        guarded_fallback = True
                    else:
                        guarded_input = True
            elif ref_use:
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
                pending_parts = []
            else:
                resolved = _steps_output_sites(line)
                if resolved is not None:
                    _record_steps_output(
                        found, lines, i, resolved, resolvers, step_ids, drop
                    )
    return found


def unguarded_ref_checkouts(lines):
    """(1-based line, uses_fallback, via_step_output) for every unguarded checkout.

    `via_step_output` separates the shapes only so `check_dir` can name the
    right remedy: "copy the guard step in ahead of it" is the wrong advice for a
    checkout whose ref is resolved a step earlier — there the missing piece is
    the consuming step's own `if:` — and BOTH are the wrong advice for a
    `'dangling'` site, where no step with that id precedes the checkout at all
    and the fix is the id itself, or a `'non-leading'` one, where the fix is
    operand order and no guard anywhere can substitute for it (BE-8215), or an
    `'unparsed'` one, where the fix is to rewrite the expression in a spelling
    the lint can judge (BE-8253).
    """
    return [
        (lineno, fb, via_step)
        for lineno, fb, guarded, via_step in ref_checkouts(lines)
        if not guarded
    ]


def find_unguarded_ref_checkouts(lines):
    """1-based line numbers of ref checkouts with no adequate guard."""
    return [lineno for lineno, _, _ in unguarded_ref_checkouts(lines)]


def _ann_msg(value):
    """`value`, safe to interpolate into a workflow command's MESSAGE.

    Every name this file annotates comes from a directory listing or from
    file CONTENT, and neither is trusted text: git permits newlines in a
    filename, and a `.yml` whose NAME carries one puts whatever follows at the
    start of a log line — where `::stop-commands::` is a real workflow command
    that suppresses every annotation printed after it. Since BE-9045 prints
    notices AHEAD of the errors, the annotations it would suppress are the
    ERROR ones (the exit status is unaffected — the run still fails — but a
    reader scanning the PR's annotations would see nothing to explain it).
    A `::` in the MIDDLE of a message opens nothing, so escaping the newline
    is what closes this; `%` and `\\r` come along per GitHub's documented set.

    A no-op for every name a workflow file actually has.
    """
    return str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _ann_prop(value):
    """`value`, safe to interpolate into a workflow command PROPERTY (`file=…`).

    A property list is `,`-separated and `:`-terminated, so those two escape
    on top of `_ann_msg`'s set — otherwise a filename containing either walks
    out of `file=` and forges the rest of the annotation's properties.
    """
    return _ann_msg(value).replace(":", "%3A").replace(",", "%2C")


def _escaped_steps_warning(path, name, lines, job_start, job_indent, count):
    """The non-fatal `::warning` for one job whose `steps:` escaped the pre-scan.

    `path` and `name` arrive ALREADY annotation-escaped (`_ann_prop` /
    `_ann_msg`), as they do for every error `check_dir` builds; `job_name`
    comes from file content and is escaped here, at the one place that reads
    it.

    Points at the `steps:` line rather than the job key, because that line IS
    the shape to rewrite — recomputed with the SAME lookup `_job_step_ids`
    uses, so the two can never disagree about which key they mean, and falling
    back to the job line only where the lookup finds none at all.

    The job name is read off the key line through the module's quote-aware
    `_strip_comment`, not by trimming the raw line: `  build:  # the mapper`
    is legal YAML and a raw trim prints the comment as part of the job's name,
    in the one annotation whose whole job is to name the job.

    …and it is cut at the key's OWN mapping colon, not merely stripped of a
    trailing one. A job key line may carry a VALUE — `  build: &common`, an
    anchor on the key line, which is one of the three escape shapes this very
    message enumerates and therefore a routine way to reach it — and trimming
    only a trailing `:` printed "job `build: &common`" in the one annotation
    whose whole job is to name the job. The colon is found on the shared
    `_quote_mask` scan so a quoted key that CONTAINS one (`"deploy: prod":`)
    is cut at its own delimiter rather than inside its name.
    """
    job_name = _strip_comment(lines[job_start]).strip()
    mask = _quote_mask(job_name)
    for pos, ch in enumerate(job_name):
        if ch == ":" and mask[pos]:
            job_name = job_name[:pos]
            break
    job_name = _ann_msg(job_name.strip().strip("'\""))
    steps_line = None
    job_child = _first_child_indent(lines, job_start, job_indent)
    if job_child is not None:
        steps_line = _find_key(lines, "steps", job_start, job_child, job_indent)
    if steps_line is None:
        steps_line = job_start
    return (
        "::warning file=%s,line=%d::%s job `%s`: its `steps:` could not be read "
        "as a step sequence (`steps: [ … ]` / an anchor on the key line, or no "
        "`- ` item below it), so the dangling-ref check could NOT run for %d "
        "`ref:` site(s) in this job — one per `ref:` LINE, this lint's unit "
        "throughout — and they were passed WITHOUT it. This is lost coverage, "
        "not a finding: each of those refs may well be fine, and a real "
        "earlier step this lint has no claim on would have been dropped "
        "anyway; with the pre-scan defeated, a dangling id and that step are "
        "indistinguishable here. Rewrite `steps:` as a block sequence "
        "(`steps:` then `- uses: … ` items) to restore it. See BE-9045."
        % (path, steps_line + 1, name, job_name, count)
    )


def check_dir(workflows_dir, exempt=KNOWN_EXEMPT):
    """Returns (errors, checked, exempt_ok, notices) — errors and notices are annotation-ready strings; only errors fail the run.

    `notices` carries the BE-9045 observability channel: one `::warning` per
    job whose `steps:` defeated the BE-8254 pre-scan AND actually cost a site
    its dangling verdict. It NEVER affects the exit status — `main` prints it
    and still returns 0 — because the drop it reports is a deliberate
    fail-open (a fail-CLOSED escape manufactures false CI failures out of a
    pre-scan that could not run); what was wrong was that the drop was silent.
    """
    errors = []
    checked = []
    exempt_ok = []
    notices = []
    seen_exempt = set()

    names = sorted(
        n
        for n in os.listdir(workflows_dir)
        if n.endswith((".yml", ".yaml")) and os.path.isfile(os.path.join(workflows_dir, n))
    )
    for name in names:
        path = os.path.join(workflows_dir, name)
        # Annotation-safe forms of the two values every message below
        # interpolates. They come from `os.listdir`, which is not trusted
        # text — see `_ann_msg`. A no-op for every real workflow filename.
        ann_path = _ann_prop(path)
        ann_name = _ann_msg(name)
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
                    % (ann_path, ann_name, INPUT_NAME)
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
                    % (ann_path, ann_name, INPUT_NAME)
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
        #
        # `dropped` rides along on this SAME walk (BE-9045) rather than adding
        # a third one: the sites the BE-8254 fail-open swallows are collected
        # where they are dropped, and turned into non-fatal warnings below.
        dropped = []
        self_pins_to_job_workflow_sha = any(
            fb for _, fb, _, _ in ref_checkouts(lines, dropped=dropped)
        )

        # One warning per JOB, first-seen order, and only for a job that
        # actually lost a site: a flow-`steps:` job with no
        # `steps.<id>.outputs.<out>` consumer never reaches the drop branch
        # and stays silent, and neither does a site whose sibling operand
        # still earned it a verdict. The collector already appends once per
        # SITE — a line, which is this lint's unit everywhere — so two
        # flow-style checkouts written on ONE physical line are one site here,
        # exactly as they are one entry in `found`. Kept a set anyway so the
        # count stays a count of LINES if a line is ever walked twice; the
        # message says "`ref:` site(s)" rather than "checkouts" so the number
        # cannot be read as a claim the reader can check against the job's
        # step count.
        by_job = {}
        for job_start, job_indent, idx in dropped:
            job = by_job.setdefault((job_start, job_indent), set())
            job.add(idx)
        for (job_start, job_indent), sites in by_job.items():
            notices.append(
                _escaped_steps_warning(
                    ann_path, ann_name, lines, job_start, job_indent, len(sites)
                )
            )

        for lineno in defaults:
            if self_pins_to_job_workflow_sha and _default_value(lines[lineno - 1]) in ("''", '""'):
                continue
            errors.append(
                "::error file=%s,line=%d::%s declares a `default:` for the `%s` "
                "input. Delete it (and keep `required: true` + the runtime "
                "empty-ref guard): a default lets a caller SHA-pin `uses:` while "
                "loading scripts from a mutable ref. See BE-5546."
                % (ann_path, lineno, ann_name, INPUT_NAME)
            )

        for lineno, uses_fallback, via_step_output in unguarded_ref_checkouts(lines):
            if via_step_output == UNPARSED:
                # The value reads a step output in a spelling this lint cannot
                # judge (BE-8253). Every other remedy below presumes a parse —
                # naming an id, an operand order, an `if:` on a known output —
                # so the only honest instruction is "write one of the two
                # spellings that ARE judged". Deliberately no expression parser:
                # an unknown spelling asks its author for a supported one.
                errors.append(
                    "::error file=%s,line=%d::%s checks out at a `ref:` that "
                    "reads a `steps.<id>.outputs.<name>` this lint cannot "
                    "parse, so it cannot tell whether the checkout is guarded "
                    "— and an unparsed ref used to pass silently while the "
                    "same workflow spelled bare failed. Unrecognized "
                    "spellings include a fallback containing a brace "
                    "(`|| format('refs/heads/{0}', 'main')`), a parenthesized "
                    "operand (`(steps.<id>.outputs.<name>) || 'main'`), and "
                    "`&&` in place of `||`. Rewrite the ref as a bare "
                    "`${{ steps.<id>.outputs.<name> }}` with the exact "
                    "non-empty `if:` on this step, or as that expression with "
                    "a trailing `|| <literal>` fallback — the supported "
                    "spellings in .github/workflow-pins/README.md. "
                    "See BE-8253." % (ann_path, lineno, ann_name)
                )
                continue
            if via_step_output in ("dangling", "non-leading"):
                # Name the ACTUAL id and output where the reported line still
                # holds the expression, so a typo — the first cause the
                # dangling message lists — is visible in the annotation itself.
                # The `ref: >-` continuation spelling reports the KEY line and
                # keeps its value below it, so fall back to the placeholders
                # there rather than naming the wrong step.
                step_id, out = _reported_step_output(lines[lineno - 1], via_step_output)
            if via_step_output == "dangling":
                # The `ref:` reads a step output whose id matches NO step
                # declared before the consuming one (BE-8215). The BE-8130
                # remedies below do not apply — adding the `if:` on a
                # nonexistent output guards nothing that will ever run, and
                # there is no resolving step to make fail-closed.
                errors.append(
                    "::error file=%s,line=%d::%s checks out at a `ref:` that "
                    "reads `steps.%s.outputs.%s`, but no step `%s` "
                    "precedes this checkout in its job — a typo'd id, a step "
                    "in another job, or a resolver declared below its "
                    "consumer. At runtime that expression is '' and "
                    "`actions/checkout` reads '' as the DEFAULT BRANCH, so "
                    "the checkout runs unconditionally at a mutable ref. Fix "
                    "the step id, or move the resolving step above this "
                    "checkout. See BE-8215."
                    % (ann_path, lineno, ann_name, step_id, out, step_id)
                )
                continue
            if via_step_output == "non-leading":
                # The output IS read, but something precedes it inside the
                # `${{ … }}` (BE-8215). The BE-8130 remedies are worse than
                # useless here: both harden a value this ref never reliably
                # resolves to, so applying the printed advice leaves CI red
                # with the identical error and never names operand order — the
                # one thing that can fix it.
                errors.append(
                    "::error file=%s,line=%d::%s checks out at a `ref:` that "
                    "reads `steps.%s.outputs.%s`, but the expression does not "
                    "START with it. GitHub's `||` returns the first TRUTHY "
                    "operand, so a leading `'main' || …` wins on every runner "
                    "and the output is never consulted; any other leading "
                    "operator can likewise resolve this ref to a value no "
                    "guard proved. No `if:` on this step and no change to the "
                    "resolving step can alter that. Make "
                    "`steps.%s.outputs.%s` the expression's FIRST operand (a "
                    "trailing `|| <literal>` fallback is fine), or drop what "
                    "precedes it. See BE-8215."
                    % (ann_path, lineno, ann_name, step_id, out, step_id, out)
                )
                continue
            if via_step_output:
                # The resolve-then-consume shape (BE-8130): the ref reaches the
                # checkout through a step output, so the empty case moves to the
                # CONSUMER and the job-wide guard flags say nothing about it.
                errors.append(
                    "::error file=%s,line=%d::%s checks out at a `ref:` resolved "
                    "from a step whose `env:` binds `inputs.%s`, but the "
                    "consuming step carries no `if: steps.<id>.outputs.<name> != "
                    "''` on that same output. An unresolvable ref then reaches "
                    "`actions/checkout` as '', which it reads as the default "
                    "branch. Add the exact non-empty `if:` to this step (the "
                    "never-fail idiom, see .github/workflow-pins/README.md). "
                    "An `if:` riding INSIDE a flow mapping "
                    "(`- {if: …, with: {ref: …}}`) is not read as coverage — "
                    "promote it to the step's own `if:` key, leaving `env:` "
                    "and `with:` as flow maps. See BE-8130."
                    % (ann_path, lineno, ann_name, INPUT_NAME)
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
                    % (ann_path, lineno, ann_name, INPUT_NAME)
                )
                continue
            errors.append(
                "::error file=%s,line=%d::%s checks out at `ref: ${{ inputs.%s }}` "
                "with no empty-ref guard earlier in the same job. Copy the "
                "`Require a pinned workflows_ref` step in ahead of it: `required: "
                "true` is unenforced for workflow_call, so an omitted input "
                "arrives as '' and checkout silently takes the default branch. "
                "See BE-5546." % (ann_path, lineno, ann_name, INPUT_NAME)
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
            % (_ann_msg(name), _ann_msg(workflows_dir), INPUT_NAME)
        )

    return errors, checked, exempt_ok, notices


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workflows-dir",
        default=DEFAULT_WORKFLOWS_DIR,
        help="Directory of workflow files to check (default: %s)." % DEFAULT_WORKFLOWS_DIR,
    )
    args = parser.parse_args(argv)

    if not os.path.isdir(args.workflows_dir):
        print("::error::no such directory: %s" % _ann_msg(args.workflows_dir))
        return 2

    # KNOWN_EXEMPT names files in THIS repo's workflows dir, and the staleness
    # check reads "an entry with no matching workflow is dead". Applied to an
    # ad-hoc --workflows-dir (a fixture, another repo) every entry looks stale,
    # so the list only applies where it means something.
    exempt = KNOWN_EXEMPT if args.workflows_dir == DEFAULT_WORKFLOWS_DIR else frozenset()
    errors, checked, exempt_ok, notices = check_dir(args.workflows_dir, exempt=exempt)

    for name in checked:
        note = " (KNOWN_EXEMPT — tracked separately)" if name in exempt_ok else ""
        print("checked %s%s" % (name, note))
    if not checked:
        print("no reusable workflow declares a `%s` input" % INPUT_NAME)

    # Ahead of the errors and OUTSIDE the exit-status logic (BE-9045): a
    # warning names coverage this run did not have, never a problem with the
    # workflow it names.
    for notice in notices:
        print(notice)

    for err in errors:
        print(err)
    if errors:
        print(
            "\n%d problem(s): a reusable workflow's `%s` input must have NO "
            "default, and every job checking out at it must guard against an "
            "empty value first." % (len(errors), INPUT_NAME)
        )
        return 1

    if notices:
        # Do NOT claim full coverage over a run that skipped jobs: the
        # difference between "every checkout is guarded" and "every checkout I
        # could JUDGE is guarded" is the whole point of the warnings above.
        print(
            "\nOK — %d workflow(s) declare `%s`, none with a default, every "
            "JUDGED ref checkout guarded (%d exempt); %d job(s) skipped the "
            "dangling-ref check — see the warning(s) above."
            % (len(checked), INPUT_NAME, len(exempt_ok), len(notices))
        )
        return 0

    print(
        "\nOK — %d workflow(s) declare `%s`, none with a default, every ref "
        "checkout guarded (%d exempt)."
        % (len(checked), INPUT_NAME, len(exempt_ok))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
