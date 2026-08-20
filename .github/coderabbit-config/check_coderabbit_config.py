#!/usr/bin/env python3
"""Validate a repo's `.coderabbit.yaml` against CodeRabbit's config schema.

Why this exists. CodeRabbit rejects an invalid `.coderabbit.yaml` **whole**: the
entire file is discarded and the review runs on org-wide UI defaults instead, so
every reviewer instruction, path filter, path instruction and WIP-skip rule in it
goes silently inert. Nothing about the PR looks different — the file still reads
fine, only the validator sees the loss.

Worse, the feedback is displaced by one PR. CodeRabbit validates the config on
the BASE branch, not the PR head, so the PR that breaks the file goes green and
the breakage first surfaces on the *next* PR, attributed to a change that did not
cause it. Nobody is looking at the right diff when the signal finally arrives.
That is what makes this a machine check rather than a convention: the human
feedback loop is not merely slow here, it points at the wrong commit.

Severity is split, and the split mirrors what CodeRabbit itself does with each
class of problem rather than what a schema library calls "invalid":

  * a YAML parse error, a `maxLength` violation, a type/enum error  -> FAIL.
    These are what CodeRabbit rejects the whole file for.
  * an unknown / additional property                                -> WARN.
    CodeRabbit STRIPS a key it does not recognize rather than rejecting the
    file, so the config still loads — but everything under that key silently
    does nothing. That is a real defect (a `tools:` block written at the root
    instead of under `reviews:` inverts the settings it was meant to apply),
    just not a file-rejecting one. `--strict-unknown-keys` escalates it to a
    failure for a repo that has cleaned up and wants to stay clean.

An unknown key is reported wherever the schema object it sits in accepts only the
names it lists — whether it says so with `additionalProperties: false` (five
objects, which jsonschema reports) or simply by naming `properties` and nothing
else (103 more, which `_walk_unknown_keys` reports). A node that opts OUT of that
— any of `_OPENER_KEYWORDS` — is left alone in both halves.

A repo with no `.coderabbit.yaml` passes, and says so in the log — not every
consumer of the reusable workflow has one.

Validation is against a VENDORED copy of the schema committed beside this file,
never a live fetch: a network fetch on the validation path makes every consumer's
CI depend on a third-party endpoint, and an upstream tightening would turn CI red
across the fleet with no change on our side. `refresh-coderabbit-schema.yml`
watches upstream for drift and proposes the bump as a reviewable PR instead.

Run locally:
    python3 .github/coderabbit-config/check_coderabbit_config.py --root .
"""

import argparse
import hashlib
import json
import os
import re
import sys
from itertools import islice

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on a bare interpreter
    print(
        "::error::coderabbit-config: PyYAML is not installed. Install the pinned "
        "requirements beside this script "
        "(pip install --require-hashes -r .github/coderabbit-config/requirements.txt)."
    )
    sys.exit(2)

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - exercised only on a bare interpreter
    print(
        "::error::coderabbit-config: jsonschema is not installed. Install the pinned "
        "requirements beside this script "
        "(pip install --require-hashes -r .github/coderabbit-config/requirements.txt)."
    )
    sys.exit(2)

DEFAULT_CONFIG = ".coderabbit.yaml"
DEFAULT_SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.v2.json")

# CodeRabbit honours both spellings of the extension, so a repo on the other one
# must not read as "no config here". See `_sibling_spelling`.
_SPELLING_SWAP = {".yaml": ".yml", ".yml": ".yaml"}

# The config is PR-controlled, so both the input and the output are bounded.
#
# 512 KiB is ~two orders of magnitude above any real `.coderabbit.yaml`; the cap
# exists to stop a runner reading and parsing a junk file, not to police style.
# It is NOT a defence against alias amplification — a billion-laughs document is
# a few hundred bytes, and while PyYAML itself constructs each anchor once,
# `iter_errors` walks the resulting DAG as a tree. That is what MAX_FINDINGS and
# the caller's job timeout bound; the check fails closed either way.
MAX_CONFIG_BYTES = 512 * 1024
MAX_FINDINGS = 100

# What a finding costs the reader if the cap drops it, highest kept first.
#
# The cap cannot simply slice the list: findings come out in document order, the
# root `additionalProperties` error has an EMPTY `absolute_path` and therefore
# sorts first, and it fans out to one finding per unknown key. So ~150 unknown
# root keys plus one `maxLength` violation would slice the file-rejecting error
# away and leave 100 warnings — exit 0 on a config CodeRabbit rejects WHOLE.
# Ranking is what keeps that from happening; the truncation note cannot, because
# a note does not reach the exit code.
_RANK_UNKNOWN = 0     # a stripped key: the document still loads without it
_RANK_INCOMPLETE = 1  # "the scan did not finish" — drop it and a partial run reads as complete
_RANK_REJECTING = 2   # CodeRabbit discards the WHOLE file over this one

# Keywords whose violation CodeRabbit tolerates by STRIPPING the offending key,
# rather than rejecting the document. Everything else is file-rejecting.
STRIPPED_KEYWORDS = frozenset({"additionalProperties", "unevaluatedProperties"})

# Keywords that leave a schema object OPEN to keys its own `properties` block does
# not name. Upstream's schema closes only five objects with an explicit
# `additionalProperties: false` (the document root, the `htmlhint` and `stylelint`
# tool objects, `knowledge_base.mcp`, and `knowledge_base.linked_repositories[]`),
# while 103 more declare `properties` and say nothing at all about the rest. A
# typo inside one of those — `reviews.profil`, `golangci-lint.enabld` — is stripped
# by CodeRabbit exactly like a root-level one, but jsonschema has nothing to
# complain about, so `iter_errors` alone reports it nowhere. `_walk_unknown_keys`
# closes that gap by treating "declares `properties`, declares none of these" as
# closed BY OMISSION.
#
# The list is deliberately wider than this schema needs (it currently carries one
# `anyOf` and one `propertyNames`, and none of the rest). Every entry names a way
# a node can legitimately accept a key its `properties` does not list — through a
# combinator branch, a `$ref`, a pattern, a conditional. Reading any of them as
# "closed" would invent a finding, so a future vendored-schema bump that starts
# using one produces silence here rather than a false positive.
_OPENER_KEYWORDS = (
    "additionalProperties",
    "unevaluatedProperties",
    "patternProperties",
    "anyOf",
    "oneOf",
    "allOf",
    "$ref",
    "$dynamicRef",
    "if",
    "dependentSchemas",
    "propertyNames",
    "not",
)

# The walk's own bounds, for the same reason `iter_errors` is collected through
# `islice`: the document is PR-controlled, and while PyYAML constructs an anchored
# mapping once, an aliased document is walkable as a much larger tree — the same
# billion-laughs shape in a few hundred bytes. The budget is charged BOTH per key
# inspected and per node visited, and it needs both halves: per-key is what keeps
# the bound linear in real work (one visit to a mapping with 100k keys costs
# 100k, not 1), while per-visit is what covers list traversal, which names no key
# at all — an aliased nested list charges 1 for the outer entry and would
# otherwise descend through thousands of elements for free.
#
# The depth cap is belt-and-braces rather than the working bound: descent follows
# the SCHEMA (matched properties and `items`), whose nesting is finite and shallow,
# and `$ref` is an opener this walk never resolves — so a self-referential future
# schema cannot make it recurse forever. 64 is far past any real config and far
# short of the interpreter's recursion limit.
MAX_WALK_KEYS = 200_000
MAX_WALK_DEPTH = 64

# Guards the "did you mean" suggestion below: an unknown key is only worth
# proposing a home for when the name is specific enough that a same-named
# property elsewhere in the schema is likely the intended one.
_SUGGESTABLE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,}$")

# `_format_path` spells an array element `[0]`; `_index_schema_paths` spells the
# same position `[]`. `_suggest_home` compares the two vocabularies, so it
# normalizes with this first — otherwise no schema candidate can ever share the
# parent of a key under a list element, and the parent-preference half of the
# hint silently never applies there.
_ARRAY_INDEX_RE = re.compile(r"\[\d+\]")

# Stands in for NaN in `_index_key`, which needs a key equal to itself.
_NAN_KEY = object()

# `_index_key`'s "this key cannot be indexed at all" answer. A distinct sentinel
# rather than None, because None is itself a perfectly ordinary YAML key (`null:`)
# and must index like any other.
_UNINDEXABLE = object()

# How many homes may be named before the hint stops being a hint. A name with
# more plausible homes than this has no single likely one, so listing the
# alphabetically-first few is guessing out loud — see `_suggest_home`.
_MAX_SUGGESTIONS = 3


class ConfigError(Exception):
    """A problem with how the checker was invoked, not with the config."""


def _esc_cmd(text):
    """Escape a value before it is interpolated into a workflow-command line.

    Every string here — key names, YAML error text, the offending value's length
    — is derived from a file the PR author controls, and YAML permits a newline
    inside a quoted key. Unescaped, a key named "x\\n::stop-commands::tok" would
    close this line and emit a SECOND, attacker-chosen workflow command,
    suppressing the annotations printed after it or forging notices in a public
    run log. Applied to the plain line too, since that line would equally start
    a `::` command. Same helper, same reasoning, as check_agents_md.py.
    """
    return str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _esc_prop(text):
    """Escape a value used as an annotation PROPERTY (`file=`, `title=`).

    Properties are comma-separated and colon-terminated, so they need `,` and `:`
    escaped on top of the message escaping — an unescaped comma in a path would
    be read as the start of the next property.
    """
    return _esc_cmd(text).replace(":", "%3A").replace(",", "%2C")


def _format_path(path_parts):
    """Render a JSON-pointer-ish path as `reviews.path_instructions[0].path`."""
    if not path_parts:
        return "(document root)"
    out = ""
    for part in path_parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _index_schema_paths(schema):
    """Map every property NAME in the schema to the dotted paths it appears at.

    Used only for the "did you mean" hint on an unknown key: a `tools:` block at
    the document root is almost always `reviews.tools` written one level too
    high, and naming the right home is the difference between a warning someone
    acts on and one they scroll past.
    """
    index = {}

    def walk(node, path):
        if not isinstance(node, dict):
            return
        for name, sub in (node.get("properties") or {}).items():
            child = f"{path}.{name}" if path else name
            index.setdefault(name, []).append(child)
            walk(sub, child)
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, f"{path}[]")

    walk(schema, "")
    return index


def _suggest_home(key, schema_index, offending_path):
    """A one-line 'did you mean' for an unknown key, or '' when there is none.

    Written for the root-level `tools:` case: a name specific enough that the one
    other place the schema uses it is almost certainly where it belonged. Keys
    found by the closed-by-omission walk break that assumption, because nested
    unknown keys are dominated by GENERIC names — `enabled` sits at ~72 places in
    this schema, plus `scope`, `path`, `instructions` — and for one of those the
    three alphabetically-first paths are three homes that are all probably wrong
    (`reviews.tools.enabled` used to be answered with `issue_enrichment.*`).

    So the shortlist prefers candidates under the offending key's OWN parent —
    that is the "written one level too high" shape the hint exists for — and a
    name with more homes than `_MAX_SUGGESTIONS` to choose between gets no hint
    at all. No hint is strictly better than a confident wrong one.
    """
    if not isinstance(key, str) or not _SUGGESTABLE_KEY_RE.match(key):
        return ""
    # Both comparisons below are against `_index_schema_paths` spellings, which
    # write an array position `[]` where `_format_path` writes `[0]`.
    normalized = _ARRAY_INDEX_RE.sub("[]", offending_path)
    candidates = [p for p in schema_index.get(key, []) if p != normalized]
    if not candidates:
        return ""
    parent = normalized.rsplit(".", 1)[0] if "." in normalized else ""
    if parent:
        nearer = [c for c in candidates if c.startswith(f"{parent}.")]
        if nearer:
            candidates = nearer
    if len(candidates) > _MAX_SUGGESTIONS:
        return ""
    shown = ", ".join(f"`{c}`" for c in sorted(candidates))
    return f" Did you mean {shown}?"


def _apply_cap(tagged):
    """Trim `[(rank, finding)]` to MAX_FINDINGS. Returns (findings, was_capped).

    Drops the lowest-ranked entries first and, within a rank, the ones latest in
    the document — so what survives is still in emission order, and a
    file-rejecting error is never crowded out by unknown-key warnings that
    happened to sort ahead of it.
    """
    if len(tagged) <= MAX_FINDINGS:
        return [finding for _rank, finding in tagged], False
    doomed = set(
        sorted(range(len(tagged)), key=lambda i: (tagged[i][0], -i))[
            : len(tagged) - MAX_FINDINGS
        ]
    )
    return [f for i, (_rank, f) in enumerate(tagged) if i not in doomed], True


def _extra_keys(error):
    """The property names an `additionalProperties: false` error actually rejected.

    jsonschema puts them in the human message only, so recompute them from the
    instance and the subschema. That is what lets each unknown key be reported on
    its own line, at its own path, with its own YAML line number — rather than
    one blob naming the parent object.
    """
    subschema = error.schema if isinstance(error.schema, dict) else {}
    known = set(subschema.get("properties") or {})
    patterns = []
    for raw in subschema.get("patternProperties") or {}:
        try:
            patterns.append(re.compile(raw))
        except re.error:
            continue
    if not isinstance(error.instance, dict):
        return []
    return [
        k
        for k in error.instance
        if k not in known and not any(p.search(str(k)) for p in patterns)
    ]


def _line_for_path(node, path_parts):
    """1-based YAML line of the value at `path_parts`, or None.

    Walks the composed node tree (`yaml.compose`), which carries source markers
    PyYAML's plain `safe_load` throws away. A path the tree cannot follow — a
    merge key, an alias, a shape the schema and the document disagree about —
    returns None and the caller falls back to a file-level annotation, which is
    strictly better than pointing at a confidently wrong line.
    """
    current = node
    for part in path_parts:
        if current is None:
            return None
        current = _descend(current, part)
    if current is None:
        return None
    return current.start_mark.line + 1


def _resolved_key(key_node):
    """The Python value a composed KEY node loads to — tag-aware, like `safe_load`.

    A raw text compare is not enough: `safe_load` resolves plain `true`/`on`/`yes`
    to True, `null`/`~` to None and `0x10` to 16, so the key sitting in the loaded
    dict and the key node's source text are routinely different objects. Going the
    other way (compare `str(loaded)` to the text) is what dropped the line number
    for exactly those keys. Constructing the node the way `safe_load` constructed
    it makes the two comparable again, and gets the mirror case right too: a
    quoted `"true"` keeps its string tag and must NOT match the boolean.
    """
    try:
        return yaml.constructor.SafeConstructor().construct_object(key_node, deep=True)
    except Exception:  # pragma: no cover - a key safe_load accepted constructs here
        return getattr(key_node, "value", None)


def _index_key(value):
    """A hashable, REFLEXIVE stand-in for a constructed key, or `_UNINDEXABLE`.

    Two keys `safe_load` produces are not usable as dict lookups on their own.
    NaN is not equal to itself, so a `.nan:` key would miss its own entry and
    fall back to `str()` — reproducing the very "path naming a key that is not in
    the file" defect this lookup exists to prevent. An unhashable key (a list,
    from a complex `? [a, b]` key) cannot be indexed at all and comes back as
    `_UNINDEXABLE` — NOT as None, which is an ordinary YAML key (`null:`).
    """
    if isinstance(value, float) and value != value:
        return _NAN_KEY
    try:
        hash(value)
    except TypeError:
        return _UNINDEXABLE
    return value


def _merge_entries(value_node, seen=None):
    """The (key_node, value_node) pairs a `<<:` merge brings into a mapping.

    `safe_load` FLATTENS `<<: *anchor` into the mapping it sits in, but the
    composed node keeps only the `<<` key — so without this every merged-in key
    is a key the loaded document has and the node tree does not, which is the
    `str()` fallback and a lost line number again. A merge value is a mapping or
    a sequence of them; `seen` guards the self-referential alias (`a: &x [*x]`)
    that composes into a cyclic node graph.

    TRANSITIVE, because `flatten_mapping` is: it flattens a merged mapping before
    splicing it in, so `a: &a {x: 1}` / `b: &b {<<: *a}` / `reviews: {<<: *b}`
    puts `x` under `reviews` in the loaded document. Stopping at one level would
    index a literal `"<<"` instead and lose `x`'s line — the same defect this
    function exists to fix, one level deeper. A merged mapping's OWN keys are
    returned ahead of what it merged in, because `node.value = merge + node.value`
    plus dict-assignment order is what gives them precedence, and this list is
    consumed first-wins.
    """
    if seen is None:
        seen = set()
    if id(value_node) in seen:
        return []
    seen.add(id(value_node))
    if isinstance(value_node, yaml.MappingNode):
        own, inherited = [], []
        for key_node, child in value_node.value:
            if getattr(key_node, "tag", None) == "tag:yaml.org,2002:merge":
                inherited.extend(_merge_entries(child, seen))
            else:
                own.append((key_node, child))
        return own + inherited
    if isinstance(value_node, yaml.SequenceNode):
        out = []
        for item in value_node.value:
            out.extend(_merge_entries(item, seen))
        return out
    return []


def _resolved_key_index(parent):
    """`{resolved key -> (key node, value node)}` for one mapping, built once and cached.

    Built once because the alternative is quadratic: `_extra_keys` can hand back
    every key in the mapping, and rescanning `parent.value` per finding —
    constructing each candidate key on the way — is ~1e9 constructions for a 50k
    key mapping that still fits inside `MAX_CONFIG_BYTES`. The cache lives on the
    composed node, which is rebuilt per `validate` call, so it cannot go stale.

    Explicit keys win over merged ones, which is YAML's own merge precedence and
    the same precedence `safe_load` applied when it built the document. Merge keys
    themselves never reach the index: `_merge_entries` drops them at the single
    point it expands them, so `merged` holds only real keys the loaded document
    actually has.

    Carries the VALUE node too, so `_descend` can route through a merge as well —
    an ancestor that arrives via `<<:` is in the loaded document and not in the
    composed mapping's own pairs, which is the same lost line number one level up.
    """
    index = getattr(parent, "_coderabbit_key_index", None)
    if index is not None:
        return index
    index = {}
    merged = []
    for key_node, value_node in parent.value:
        if getattr(key_node, "tag", None) == "tag:yaml.org,2002:merge":
            merged.extend(_merge_entries(value_node))
            continue
        indexed = _index_key(_resolved_key(key_node))
        if indexed is not _UNINDEXABLE:
            index.setdefault(indexed, (key_node, value_node))
    for key_node, value_node in merged:
        indexed = _index_key(_resolved_key(key_node))
        if indexed is not _UNINDEXABLE:
            index.setdefault(indexed, (key_node, value_node))
    try:
        parent._coderabbit_key_index = index
    except AttributeError:  # pragma: no cover - a node type that forbids the attribute
        pass
    return index


def _index_entry(node, key):
    """The `(key node, value node)` pair `key` resolves to in a composed mapping."""
    if not isinstance(node, yaml.MappingNode):
        return None
    indexed = _index_key(key)
    if indexed is _UNINDEXABLE:
        return None
    return _resolved_key_index(node).get(indexed)


def _key_node(node, parent_parts, key):
    """The composed KEY node for `key` inside the mapping at `parent_parts`, or None.

    An unknown key has no value the schema knows about, so both the useful
    annotation line and the name worth printing come from the key itself.
    """
    parent = node
    for part in parent_parts:
        if parent is None:
            return None
        parent = _descend(parent, part)
    entry = _index_entry(parent, key)
    return entry[0] if entry else None


def _descend(node, part):
    """The composed value node one path step below `node`, or None.

    Mapping steps go through `_resolved_key_index` rather than a raw text compare
    on `key_node.value`, for the two reasons that index exists: a path part is a
    key `safe_load` RESOLVED (`on:` arrives here as True, never as `"on"`), and a
    key merged in through `<<:` lives in the loaded document but not in this
    mapping's own pairs. Either mismatch ends the descent early, and every caller
    reads that as "no such node" — an invented `str()` path with no line number.
    """
    # `not isinstance(part, bool)`: a bool IS an int in Python, and `on:` /
    # `true:` are ordinary YAML KEYS — reading one as a list index would send the
    # descent down the sequence branch and end it there.
    if isinstance(part, int) and not isinstance(part, bool):
        if not isinstance(node, yaml.SequenceNode) or part >= len(node.value):
            return None
        return node.value[part]
    entry = _index_entry(node, part)
    return entry[1] if entry else None


def _unknown_key_finding(severity, node, parent_parts, key, schema_index):
    """One finding for `key` not being recognized inside the mapping at `parent_parts`.

    Shared by BOTH unknown-key paths — the `additionalProperties: false` errors
    jsonschema raises, and the closed-by-omission walk below — so the two are
    identical in severity, wording, path, line resolution and "did you mean" by
    construction rather than by two copies staying in step.

    YAML keys need not be strings (`1:`, `true:`, `2026-01-01:` all parse to
    non-strings), and the display name must be the SOURCE SPELLING, taken from
    the composed key node. `str()` on the loaded value is not that spelling and
    not even always a key present in the file: `true:`/`on:`/`yes:` all load as
    True and rendered `reviews.True`, `0x10:` rendered `reviews.16`, `null:`
    rendered `reviews.None` — each naming a key that appears nowhere, with no line
    number to correct the impression, because the lookup then compared "True"
    against the node's "true" and found nothing. Reading name and line off the one
    node fixes both together.

    The `str()` coercion survives as the fallback for a key whose node cannot be
    found (an aliased or merge-keyed mapping the composed tree will not follow),
    where it is still needed: without it `_format_path` would render an integer
    key as a LIST INDEX (`reviews[1]` for `reviews: {1: x}`). `_suggest_home`
    needs no coercion guard of its own — `_SUGGESTABLE_KEY_RE` already rejects
    anything that is not a plausible property name.
    """
    key_node = _key_node(node, parent_parts, key)
    text = getattr(key_node, "value", None)
    if isinstance(text, str):
        name = text
        line = key_node.start_mark.line + 1
    else:
        name = key if isinstance(key, str) else str(key)
        line = None
    full = _format_path(parent_parts + [name])
    where = (
        "at the document root"
        if not parent_parts
        else f"under `{_format_path(parent_parts)}`"
    )
    return (
        severity,
        full,
        line,
        f"unknown key `{name}` {where}. CodeRabbit STRIPS keys it "
        f"does not recognize, so the config still loads but "
        f"everything under this key silently does nothing."
        + _suggest_home(name, schema_index, full),
    )


def _walk_unknown_keys(data, schema, node, schema_index, severity, limit):
    """Unknown keys inside objects the schema closes BY OMISSION.

    jsonschema reports an unknown key only where the schema says `additionalProperties`
    / `unevaluatedProperties` — five objects in upstream's schema. This walks the
    parsed document alongside the schema and reports the other 103: a node that
    declares a dict-valued `properties` and NONE of `_OPENER_KEYWORDS` accepts
    exactly the names it lists, so any other key in the document is stripped.

    Conservative on purpose, in three ways:

      * it descends ONLY through matched `properties` values and through `items`
        when the document has a list and the schema node's `items` is a dict.
        Never into an `additionalProperties` schema, a combinator branch, or the
        value under a key it just reported — a key the schema does not name has no
        subschema, so anything below it is unjudgeable, not more findings.
      * where the document and the schema disagree about shape (a mapping where
        the schema wants a scalar, and so on) it stays SILENT. That is a type
        error, and reporting it is `iter_errors`' job — saying it twice, in two
        different vocabularies, is worse than saying it once.
      * against THIS vendored schema it never fires where jsonschema does, which
        is what makes the combined output duplicate-free by construction rather
        than by a de-duplication pass that could drift. Note that is a fact about
        the schema, not a property of the two conditions: they are complements at
        a SINGLE node — jsonschema needs the keyword present, this needs it absent
        — but jsonschema evaluates a document path against every applicable
        subschema, so a node reached through `properties` with no opener of its
        own (which this walk judges closed) whose `$ref` target or a combinator
        branch carried `additionalProperties: false` would be reported by both
        halves. It holds today because the schema has no `$ref`, and because a
        node that declares `properties` alongside a combinator counts as OPEN
        here — both asserted in `OpennessTest`.

    `limit` caps the findings (the caller sizes it from what is left of
    MAX_FINDINGS). Returns (findings, bounded) — the flag says the walk hit one of
    its OWN limits (`MAX_WALK_KEYS` or `MAX_WALK_DEPTH`) and therefore did NOT see
    the whole document, which the caller reports rather than passing off as a
    clean bill of health. It is set at the sites that actually bail, not derived
    from the leftover budget afterwards: a document whose keys land exactly on
    `MAX_WALK_KEYS` was walked in full and must not be reported as partial, and a
    depth cutoff is just as partial as an exhausted budget. Stopping on `limit` is
    NOT bounded — that is truncation, which the caller reports separately.
    """
    findings = []
    remaining_keys = [MAX_WALK_KEYS]
    bounded = [False]

    def walk(value, subschema, path, depth):
        if len(findings) >= limit:
            return
        # A scalar leaf or an empty container holds nothing to inspect, so
        # arriving at one with the budget already gone leaves nothing unchecked:
        # spending the last unit on a known key whose value is a scalar
        # (`reviews.profile: chill`) must not flag the trailing visit as a partial
        # walk — reporting a fully walked document as stopped early, which is a
        # hard error under `strict_unknown_keys`. Only the BAIL-OUT FLAG is gated
        # on there being something to inspect; the visit itself is still CHARGED.
        # Making leaf visits free would take the walk's call count out of
        # `MAX_WALK_KEYS`' hands altogether — the list branch below recurses once
        # per element with no budget check of its own, so a single aliased list of
        # scalars would cost one unit and buy unboundedly many free calls, each
        # allocating a fresh `path`.
        inspectable = isinstance(value, (dict, list)) and bool(value)
        if remaining_keys[0] <= 0 or depth > MAX_WALK_DEPTH:
            if inspectable:
                bounded[0] = True
            return
        remaining_keys[0] -= 1
        if not inspectable:
            return
        if isinstance(value, list):
            items = subschema.get("items")
            if isinstance(items, dict):
                for index, element in enumerate(value):
                    walk(element, items, path + [index], depth + 1)
            return
        if not isinstance(value, dict):
            return
        properties = subschema.get("properties")
        if not isinstance(properties, dict):
            return
        closed = not any(keyword in subschema for keyword in _OPENER_KEYWORDS)
        for key, child in value.items():
            if len(findings) >= limit:
                return
            if remaining_keys[0] <= 0:
                bounded[0] = True
                return
            remaining_keys[0] -= 1
            if key in properties:
                child_schema = properties[key]
                if isinstance(child_schema, dict):
                    walk(child, child_schema, path + [key], depth + 1)
            elif closed:
                findings.append(
                    _unknown_key_finding(severity, node, path, key, schema_index)
                )

    if isinstance(schema, dict):
        walk(data, schema, [], 0)
    return findings, bounded[0]


def _describe(error, path_str):
    """A human sentence for one file-rejecting schema error."""
    if error.validator == "maxLength":
        actual = len(error.instance) if isinstance(error.instance, str) else "?"
        return (
            f"`{path_str}` is {actual} characters; the schema caps it at "
            f"{error.validator_value}. CodeRabbit rejects the WHOLE config when a "
            f"field is over its cap, falling back to org-wide UI defaults."
        )
    if error.validator == "type":
        got = type(error.instance).__name__
        want = error.validator_value
        if isinstance(want, list):
            want = " or ".join(str(w) for w in want)
        return f"`{path_str}` must be {want}, got {got} ({error.instance!r})."
    if error.validator == "enum":
        return (
            f"`{path_str}`: {error.instance!r} is not one of the permitted values "
            f"({', '.join(repr(v) for v in error.validator_value)})."
        )
    if error.validator == "required":
        return f"`{path_str}`: {error.message}"
    return f"`{path_str}`: {error.message}"


def validate(text, schema, strict_unknown_keys=False):
    """Validate one config document.

    Returns (findings, notes). A finding is
    (severity, path, line_or_None, message) with severity in {"error","warning"};
    notes are informational log lines. Raises nothing for a bad config — a YAML
    parse error comes back as a finding, because the caller wants an annotation,
    not a traceback.
    """
    findings = []
    notes = []

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = None
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            line = mark.line + 1
        # `str(exc)` on a MarkedYAMLError already embeds the marks, so this is
        # the whole diagnosis in one line.
        return (
            [("error", "(document)", line, f"not valid YAML: {exc}")],
            notes,
        )

    if data is None:
        notes.append(
            "the file parsed to an empty document (blank, or comments only) — "
            "nothing to validate"
        )
        return findings, notes

    # Composed separately from safe_load: compose() keeps the source markers that
    # turn a schema path into a clickable line, and a document safe_load accepted
    # cannot fail here.
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:  # pragma: no cover - unreachable after a clean load
        node = None

    schema_index = _index_schema_paths(schema)
    validator = Draft202012Validator(schema)
    unknown_severity = "error" if strict_unknown_keys else "warning"

    # Sorted so annotations come out in document order. The key TAGS each part
    # by kind rather than comparing raw values: `absolute_path` mixes string keys
    # with integer indices, and comparing two such paths directly raises
    # TypeError the moment an int meets a str at the same depth.
    def _order(error):
        return tuple(
            (0, part, "") if isinstance(part, int) else (1, 0, str(part))
            for part in error.absolute_path
        )

    # Collected through `islice` rather than materialized whole: the document is
    # PR-controlled, and a deeply aliased one can yield findings faster than any
    # reviewer will read them (and flood a public run log doing it). One past the
    # cap so the truncation can say so honestly.
    raw_errors = list(islice(validator.iter_errors(data), MAX_FINDINGS + 1))
    truncated = len(raw_errors) > MAX_FINDINGS
    del raw_errors[MAX_FINDINGS:]

    # `islice` above bounds the ERROR count, not the finding count: one
    # `additionalProperties: false` error reports a whole mapping and `_extra_keys`
    # splits it into one finding per key, so a single error can fan out past the
    # cap on its own. Findings are therefore collected TAGGED with what they cost
    # the reader if dropped, and the cap is applied once, at the end, over the
    # whole list — see `_apply_cap`.
    tagged = []

    for error in sorted(raw_errors, key=_order):
        parent_parts = list(error.absolute_path)
        if error.validator in STRIPPED_KEYWORDS:
            extras = _extra_keys(error)
            if not extras:
                # The subschema shape defeated the recomputation; report the
                # library's own message rather than dropping the finding.
                tagged.append(
                    (
                        _RANK_UNKNOWN,
                        (
                            unknown_severity,
                            _format_path(parent_parts),
                            _line_for_path(node, parent_parts),
                            error.message,
                        ),
                    )
                )
                continue
            for key in extras:
                if len(tagged) >= MAX_FINDINGS:
                    # Stop DOING the work, not merely stop printing it. `extras`
                    # is every extra key in the mapping, each finding costs a
                    # key-node lookup, and `k00000: 1` is ten bytes — so a root
                    # mapping of ~50k unknown keys fits inside MAX_CONFIG_BYTES
                    # and would burn the job to its timeout building annotations
                    # that the cap below then throws away.
                    truncated = True
                    break
                tagged.append(
                    (
                        _RANK_UNKNOWN,
                        _unknown_key_finding(
                            unknown_severity, node, parent_parts, key, schema_index
                        ),
                    )
                )
            continue

        path_str = _format_path(parent_parts)
        tagged.append(
            (
                _RANK_REJECTING,
                ("error", path_str, _line_for_path(node, parent_parts), _describe(error, path_str)),
            )
        )

    # The other 103 objects: closed by OMISSION, so jsonschema said nothing about
    # them. Appended after the jsonschema findings rather than merged into their
    # sort, which keeps both halves in document order within themselves and the
    # whole list stable run to run. One past `remaining` so an overflow is
    # detectable; `remaining` can be 0, in which case a single walk finding is
    # enough to prove the combined total passed the cap.
    remaining = max(0, MAX_FINDINGS - len(tagged))
    walked, walk_bounded = _walk_unknown_keys(
        data, schema, node, schema_index, unknown_severity, remaining + 1
    )
    tagged.extend((_RANK_UNKNOWN, f) for f in walked)

    if walk_bounded:
        # Say so rather than let a partial walk read as a clean one — the same
        # reason an absent config is reported instead of passing silently.
        #
        # A FINDING, not a note, and carrying `unknown_severity`: a note does not
        # reach the exit code, so a walk that gave up early still exited 0 even
        # under `strict_unknown_keys: true` — a repo that asked for unknown keys
        # to FAIL got a green check over keys nothing ever looked at. That is the
        # "I could not check" reading as a pass that an oversized config exits 2
        # for. Warn-only mode is unchanged: it stays a warning, exactly like the
        # unknown keys it could not go looking for.
        tagged.append(
            (
                _RANK_INCOMPLETE,
                (
                    unknown_severity,
                    "(document root)",
                    None,
                    f"the unknown-key scan stopped early — after inspecting "
                    f"{MAX_WALK_KEYS} keys and nodes, or at depth {MAX_WALK_DEPTH} "
                    f"(a very large or heavily aliased document) — so nested keys "
                    f"past that point were NOT checked. This run cannot vouch for "
                    f"them.",
                ),
            )
        )

    findings, capped = _apply_cap(tagged)
    truncated = truncated or capped

    if truncated:
        # Not "the first N": `_apply_cap` drops by RANK, so what survives is no
        # longer a prefix of the findings — for 150 unknown root keys plus one
        # `maxLength` violation it is unknown keys 1-99 plus a violation that came
        # after all of them. Describing the survivors as a prefix would misname
        # what the reader is looking at.
        notes.append(
            f"more than {MAX_FINDINGS} schema violations in this file — {MAX_FINDINGS} "
            f"are reported and the least consequential were dropped, so file-rejecting "
            f"errors survive ahead of unknown keys. Fix these and re-run."
        )

    return findings, notes


def load_schema(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as exc:
        raise ConfigError(f"cannot read the vendored schema at {path}: {exc}")
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"the vendored schema at {path} is not valid JSON: {exc}")
    if not isinstance(schema, dict) or "properties" not in schema:
        # The failure mode this catches is concrete: the schema URL 301-redirects,
        # and a `curl` without `-L` writes a 167-byte HTML redirect stub that is
        # neither JSON nor a schema. A stub that somehow parsed would validate
        # everything, i.e. gate nothing.
        raise ConfigError(
            f"the vendored schema at {path} is not a JSON Schema object with "
            f"`properties` — refusing to validate against it (a check that "
            f"cannot fail is worse than no check)."
        )
    return schema, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _emit(findings, notes, config_rel):
    """Print human lines plus GitHub annotations; return the exit code."""
    for note in notes:
        note = _esc_cmd(note)
        print(f"NOTE: {note}")
        print(f"::notice::coderabbit-config: {note}")

    errors = [f for f in findings if f[0] == "error"]
    warnings = [f for f in findings if f[0] == "warning"]

    for severity, _path, line, message in findings:
        message = _esc_cmd(message)
        label = "FAIL" if severity == "error" else "WARN"
        where = f"{config_rel}:{line}" if line else config_rel
        print(f"{label}: {_esc_cmd(where)} {message}")
        props = f"file={_esc_prop(config_rel)}"
        if line:
            props += f",line={line}"
        print(f"::{severity} {props}::coderabbit-config: {message}")

    if errors:
        print(
            f"\nResult: {len(errors)} error(s), {len(warnings)} warning(s) — "
            f"{_esc_cmd(config_rel)} is invalid."
        )
        return 1
    if warnings:
        print(f"\nResult: passed with {len(warnings)} warning(s).")
    else:
        print("\nResult: .coderabbit.yaml OK.")
    return 0


def _contained(root_abs, path):
    """Is `path` inside `root_abs` once symlinks on BOTH sides are resolved?

    `os.path.abspath` normalizes `..` textually but does NOT resolve symlinks,
    while `os.path.isfile` and `open` below both follow them — so a
    `.coderabbit.yaml` committed as a symlink to a path outside the checkout
    would clear a purely textual guard and be read anyway. Its content can then
    reach a PUBLIC run log: a `MarkedYAMLError` embeds the offending source line,
    and `_describe`'s type branch prints the instance in full. Resolving the root
    too is what keeps a legitimately symlinked checkout path (`/var` → `/private/var`
    on macOS, and how CI temp dirs are handed out) from failing every config for
    sitting "outside" a root that is itself a symlink.
    """
    try:
        return os.path.commonpath([root_abs, os.path.realpath(path)]) == root_abs
    except ValueError:
        # Different drives, or a mix of absolute and relative — not comparable,
        # and therefore not containable either.
        return False


def _sibling_spelling(root, config_rel):
    """The other extension CodeRabbit also honours, when THAT file is the present one.

    CodeRabbit reads `.coderabbit.yaml` and `.coderabbit.yml` alike. A repo on the
    `.yml` spelling that leaves `config_file` at its default would otherwise get a
    permanently green "absent — pass" over a config that is real, in effect, and
    possibly invalid — the same "checked nothing" failure the empty-path guard
    returns 2 for, only quieter, because it looks exactly like a pass.
    """
    stem, ext = os.path.splitext(config_rel)
    other_ext = _SPELLING_SWAP.get(ext.lower())
    if not other_ext:
        return None
    other_rel = stem + other_ext
    if os.path.isfile(os.path.join(root, other_rel)):
        return other_rel
    return None


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate .coderabbit.yaml against CodeRabbit's config schema."
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("CODERABBIT_CHECK_ROOT", "."),
        help="Repo root to check (default: current directory).",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CODERABBIT_CONFIG_FILE") or DEFAULT_CONFIG,
        help=f"Config path relative to --root (default: {DEFAULT_CONFIG}).",
    )
    parser.add_argument(
        "--schema",
        default=os.environ.get("CODERABBIT_SCHEMA_FILE") or DEFAULT_SCHEMA,
        help="Vendored JSON Schema to validate against (default: beside this script).",
    )
    parser.add_argument(
        "--strict-unknown-keys",
        action="store_true",
        default=_env_bool("STRICT_UNKNOWN_KEYS", False),
        help=(
            "Escalate an unknown/additional property from a warning to a "
            "failure. Off by default because CodeRabbit strips such keys rather "
            "than rejecting the file."
        ),
    )
    args = parser.parse_args(argv)

    config_rel = args.config
    if not config_rel.strip():
        # An empty `config_file:` input would otherwise join to the repo root,
        # fail the isfile() test, and report a clean pass over a repo whose
        # config was never looked at — a green check that checked nothing.
        msg = "config path is empty — pass a path relative to the repo root"
        print(f"FAIL: {msg}")
        print(f"::error::coderabbit-config: {msg}")
        return 2
    config_path = os.path.join(args.root, config_rel)

    # Keep the validated file inside the tree we were pointed at. `config_file`
    # reaches here from a workflow input, and `os.path.join` silently DISCARDS
    # --root when handed an absolute path — so without this, `config_file: /etc/…`
    # or `../../x` would read outside the caller's checkout and report on it. A
    # symlink counts as outside too — see `_contained`.
    root_abs = os.path.realpath(args.root)
    if not _contained(root_abs, config_path):
        msg = (
            f"config path '{config_rel}' resolves outside the checked-out repo "
            f"root — refusing to read it. Give a path relative to the repo root "
            f"(a symlink whose target leaves the tree resolves outside it too)."
        )
        print(f"FAIL: {_esc_cmd(msg)}")
        print(f"::error::coderabbit-config: {_esc_cmd(msg)}")
        return 2

    # Which file are we actually about to read? Three different states hide
    # behind a false `isfile()`, and only one of them is "this repo has no
    # config". Settle that BEFORE the banner below, so the banner names the file
    # that really gets validated rather than the one that was asked for.
    pending = []
    if os.path.lexists(config_path) and not os.path.isfile(config_path):
        # A directory, or a symlink with no target. We were pointed at something
        # unusable: that is a could-not-run, not a pass.
        msg = (
            f"'{config_rel}' exists but is not a regular file (a directory, or a "
            f"symlink with no target) — refusing to report a pass over it."
        )
        print(f"FAIL: {_esc_cmd(msg)}")
        print(f"::error::coderabbit-config: {_esc_cmd(msg)}")
        return 2

    absent = not os.path.isfile(config_path)
    if absent:
        sibling = _sibling_spelling(args.root, config_rel)
        if sibling and _contained(root_abs, os.path.join(args.root, sibling)):
            pending.append(
                (
                    "warning",
                    f"no {config_rel} in this repo, but {sibling} is — CodeRabbit "
                    f"honours both spellings, so {sibling} is the config in "
                    f"effect and is what was validated. Set `config_file: "
                    f"{sibling}` to make that explicit.",
                )
            )
            config_rel = sibling
            config_path = os.path.join(args.root, sibling)
            absent = False

    try:
        schema, schema_digest = load_schema(args.schema)
    except ConfigError as exc:
        msg = _esc_cmd(exc)
        print(f"FAIL: {msg}")
        print(f"::error::coderabbit-config: {msg}")
        return 2

    # Escaped like every other print in this file: `config_file` and `root` both
    # arrive from `workflow_call` inputs a PR to the caller repo can edit, and a
    # newline in either would emit an attacker-chosen second workflow command —
    # `::stop-commands::` here suppresses every `::error::` annotation printed
    # afterwards, so a failing check would silently lose its annotations.
    print(
        f"Validating '{_esc_cmd(config_rel)}' in '{_esc_cmd(args.root)}' against "
        f"vendored schema {_esc_cmd(os.path.basename(args.schema))} "
        f"(sha256 {schema_digest[:12]})..."
    )
    print(
        "Unknown keys: "
        + ("FAIL (strict_unknown_keys)" if args.strict_unknown_keys else "warn only")
    )
    print()

    for severity, line in pending:
        label = "WARN" if severity == "warning" else "NOTE"
        print(f"{label}: {_esc_cmd(line)}")
        print(f"::{severity}::coderabbit-config: {_esc_cmd(line)}")

    if absent:
        # Not every consumer repo has one, and a missing file is not a defect —
        # but it IS reported, so "no config here" never looks the same in a log
        # as "config validated clean".
        line = f"no {config_rel} in this repo — nothing to validate"
        print(f"NOTE: {_esc_cmd(line)}")
        print(f"::notice::coderabbit-config: {_esc_cmd(line)}")
        print("\nResult: .coderabbit.yaml absent — pass.")
        return 0

    size = os.path.getsize(config_path)
    if size > MAX_CONFIG_BYTES:
        msg = (
            f"'{config_rel}' is {size} bytes, past the {MAX_CONFIG_BYTES}-byte "
            f"limit this checker will parse — refusing to read it. A real "
            f"CodeRabbit config is kilobytes; this is not one."
        )
        print(f"FAIL: {_esc_cmd(msg)}")
        print(f"::error::coderabbit-config: {_esc_cmd(msg)}")
        return 2

    with open(config_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    findings, notes = validate(text, schema, strict_unknown_keys=args.strict_unknown_keys)
    return _emit(findings, notes, config_rel)


if __name__ == "__main__":
    sys.exit(main())
