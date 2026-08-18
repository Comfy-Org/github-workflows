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

# Keywords whose violation CodeRabbit tolerates by STRIPPING the offending key,
# rather than rejecting the document. Everything else is file-rejecting.
STRIPPED_KEYWORDS = frozenset({"additionalProperties", "unevaluatedProperties"})

# Guards the "did you mean" suggestion below: an unknown key is only worth
# proposing a home for when the name is specific enough that a same-named
# property elsewhere in the schema is likely the intended one.
_SUGGESTABLE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,}$")


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
    """A one-line 'did you mean' for an unknown key, or '' when there is none."""
    if not isinstance(key, str) or not _SUGGESTABLE_KEY_RE.match(key):
        return ""
    candidates = [p for p in schema_index.get(key, []) if p != offending_path]
    if not candidates:
        return ""
    shown = ", ".join(f"`{c}`" for c in sorted(candidates)[:3])
    return f" Did you mean {shown}?"


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


def _key_line(node, parent_parts, key):
    """1-based YAML line of the KEY `key` inside the mapping at `parent_parts`.

    An unknown key has no value the schema knows about, so the useful annotation
    points at the key itself.
    """
    parent = node
    for part in parent_parts:
        parent = _descend(parent, part)
        if parent is None:
            return None
    if not isinstance(parent, yaml.MappingNode):
        return None
    for key_node, _ in parent.value:
        if getattr(key_node, "value", None) == key:
            return key_node.start_mark.line + 1
    return None


def _descend(node, part):
    if isinstance(part, int):
        if not isinstance(node, yaml.SequenceNode) or part >= len(node.value):
            return None
        return node.value[part]
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if getattr(key_node, "value", None) == part:
            return value_node
    return None


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

    for error in sorted(raw_errors, key=_order):
        parent_parts = list(error.absolute_path)
        if error.validator in STRIPPED_KEYWORDS:
            extras = _extra_keys(error)
            if not extras:
                # The subschema shape defeated the recomputation; report the
                # library's own message rather than dropping the finding.
                findings.append(
                    (
                        unknown_severity,
                        _format_path(parent_parts),
                        _line_for_path(node, parent_parts),
                        error.message,
                    )
                )
                continue
            for key in extras:
                full = _format_path(parent_parts + [key])
                where = (
                    "at the document root"
                    if not parent_parts
                    else f"under `{_format_path(parent_parts)}`"
                )
                findings.append(
                    (
                        unknown_severity,
                        full,
                        _key_line(node, parent_parts, key),
                        f"unknown key `{key}` {where}. CodeRabbit STRIPS keys it "
                        f"does not recognize, so the config still loads but "
                        f"everything under this key silently does nothing."
                        + _suggest_home(key, schema_index, full),
                    )
                )
            continue

        path_str = _format_path(parent_parts)
        findings.append(
            ("error", path_str, _line_for_path(node, parent_parts), _describe(error, path_str))
        )

    if truncated:
        notes.append(
            f"more than {MAX_FINDINGS} schema violations in this file — only the "
            f"first {MAX_FINDINGS} are reported. Fix these and re-run."
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
