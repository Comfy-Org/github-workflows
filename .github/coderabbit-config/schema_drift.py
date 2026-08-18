#!/usr/bin/env python3
"""Compare the vendored CodeRabbit schema against a freshly fetched one.

`check_coderabbit_config.py` validates against a VENDORED schema on purpose: a
live fetch would make every consumer's CI depend on a third-party endpoint, and
an upstream tightening would turn CI red across the fleet with no change on our
side. The cost of vendoring is that the copy rots silently — so
`refresh-coderabbit-schema.yml` fetches upstream on a schedule and runs this to
decide whether the drift is worth a PR, and to write the summary that makes that
PR reviewable in thirty seconds instead of by reading a 75 KB JSON diff.

Two deliberate behaviours:

  * The comparison is SEMANTIC (canonical JSON), not byte-for-byte. Upstream
    re-serializing the same schema with different whitespace or key order is not
    drift, and opening a PR for it trains everyone to ignore these PRs.
  * A fetched file that is not a JSON Schema object is a hard ERROR, never
    "no drift". The schema URL 301-redirects, and a `curl` without `-L` writes a
    167-byte HTML redirect stub; treating that as "nothing changed" would freeze
    the vendored copy forever with a green run every week saying so.

Run locally:
    python3 .github/coderabbit-config/schema_drift.py \\
        --vendored .github/coderabbit-config/schema.v2.json --fetched /tmp/new.json

Exit codes: 0 = no drift, 1 = drifted (summary on stdout), 2 = unusable input.
"""

import argparse
import json
import sys

# Cap the per-section lists so one sweeping upstream restructure cannot produce a
# PR body GitHub truncates. The counts are always reported in full — only the
# enumeration is capped, and the cap says so when it bites.
MAX_LISTED = 25


class DriftInputError(Exception):
    """A file that cannot serve as a schema — not a drift verdict."""


def load(path, label):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as exc:
        raise DriftInputError(f"cannot read the {label} schema at {path}: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DriftInputError(
            f"the {label} schema at {path} is not valid JSON: {exc}. If this is "
            f"the fetched copy, the most likely cause is a redirect stub — the "
            f"schema URL 301-redirects, so the fetch must use `curl -fsSL`."
        )
    if not isinstance(data, dict) or not isinstance(data.get("properties"), dict):
        raise DriftInputError(
            f"the {label} schema at {path} is not a JSON Schema object with a "
            f"`properties` map — refusing to treat it as a schema."
        )
    return data


def canonical(schema):
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


# Keywords whose value is a single subschema, and whose contents are therefore
# as capable of holding a cap or a property as `properties` itself.
_SUBSCHEMA_KEYWORDS = (
    "additionalProperties",
    "unevaluatedProperties",
    "propertyNames",
    "contains",
    "not",
    "if",
    "then",
    "else",
)

# Keywords whose value is a LIST of subschemas.
_BRANCH_KEYWORDS = ("anyOf", "oneOf", "allOf")


def _subschemas(schema):
    """Yield `(dotted path, node, kind)` for every subschema in the document.

    `kind` is `"property"` when the node was reached as a named property (what
    `property_paths` reports), and `""` otherwise.

    Descending only through `properties` and a dict-valued `items` — the obvious
    walk — is not enough, and the vendored copy already proves it: it uses
    `knowledge_base.code_guidelines.filePatterns.items.anyOf` and
    `reviews.mutually_exclusive_groups.additionalProperties` today. A `maxLength`
    tightened inside one of those invalidates a config exactly as hard as one on a
    plain property, but the narrow walk could not see it — and because the
    property path itself is unchanged, `added`/`removed` would be empty too, so
    the summary would claim "the drift is elsewhere".

    Paths are stable and distinct across versions, which is the whole basis of
    the diff: a combinator branch carries its index (`x<anyOf[0]>`) so two
    branches constraining the same location cannot collide into one entry.
    """
    # Cycle backstop. `json.load` never shares objects, so nothing is skipped
    # today; this only stops a self-referential schema from recursing forever.
    seen = set()

    def walk(node, path, kind):
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen.add(id(node))
        yield path, node, kind

        for name, sub in (node.get("properties") or {}).items():
            yield from walk(sub, f"{path}.{name}" if path else name, "property")
        for pattern, sub in (node.get("patternProperties") or {}).items():
            child = f"{{{pattern}}}"
            yield from walk(sub, f"{path}.{child}" if path else child, "property")
        for name, sub in (node.get("$defs") or node.get("definitions") or {}).items():
            yield from walk(sub, f"{path}<$defs.{name}>", "")

        items = node.get("items")
        if isinstance(items, dict):
            yield from walk(items, f"{path}[]", "")
        elif isinstance(items, list):
            # Tuple validation: each entry constrains one position.
            for index, sub in enumerate(items):
                yield from walk(sub, f"{path}[{index}]", "")

        for keyword in _SUBSCHEMA_KEYWORDS:
            sub = node.get(keyword)
            if isinstance(sub, dict):
                yield from walk(sub, f"{path}<{keyword}>", "")
        for keyword in _BRANCH_KEYWORDS:
            branches = node.get(keyword)
            if isinstance(branches, list):
                for index, sub in enumerate(branches):
                    yield from walk(sub, f"{path}<{keyword}[{index}]>", "")

    yield from walk(schema, "", "")


def property_paths(schema):
    """Every dotted property path in the schema, e.g. `reviews.tools.eslint.enabled`."""
    return {path for path, _node, kind in _subschemas(schema) if kind == "property"}


def length_caps(schema):
    """`{dotted path: maxLength}` for every capped string in the schema.

    Called out separately from the raw property diff because a tightened cap is
    the one drift class that can turn a config that has been valid for months
    into one CodeRabbit rejects whole, with no change on the config's side.
    """
    caps = {}
    for path, node, _kind in _subschemas(schema):
        cap = node.get("maxLength")
        if isinstance(cap, int):
            caps[path or "(document root)"] = cap
    return caps


def _bullets(title, items):
    if not items:
        return []
    out = [f"**{title}** ({len(items)}):", ""]
    for entry in sorted(items)[:MAX_LISTED]:
        out.append(f"- `{entry}`")
    if len(items) > MAX_LISTED:
        out.append(f"- …and {len(items) - MAX_LISTED} more (see the diff).")
    out.append("")
    return out


def summarize(vendored, fetched):
    """A Markdown summary of what changed, for the refresh PR body."""
    old_paths, new_paths = property_paths(vendored), property_paths(fetched)
    old_caps, new_caps = length_caps(vendored), length_caps(fetched)

    added = new_paths - old_paths
    removed = old_paths - new_paths
    tightened = []
    loosened = []
    # The UNION of both sides, not just the fetched caps. Two drift classes hide
    # in the difference, and the first is the one this whole section exists to
    # lead with:
    #   * upstream ADDS a cap to a property that was uncapped
    #     (`reviews.high_level_summary_instructions`, `reviews.auto_title_instructions`
    #     and `pre_merge_checks.title.requirements` are all uncapped today) — the
    #     single most config-invalidating change upstream can make;
    #   * upstream REMOVES a cap, which never appears in `new_caps` at all.
    # In both cases the property path is unchanged, so `added`/`removed` are
    # empty too — skipping them would print "No property or `maxLength` changes"
    # over exactly the drift a reviewer must not miss.
    for path in sorted(set(old_caps) | set(new_caps)):
        was, now = old_caps.get(path), new_caps.get(path)
        if was == now:
            continue
        if was is None:
            tightened.append(f"{path}: uncapped → {now}")
        elif now is None:
            loosened.append(f"{path}: {was} → uncapped")
        else:
            (tightened if now < was else loosened).append(f"{path}: {was} → {now}")

    lines = []
    # Tightened caps first: this is the only class that can retroactively
    # invalidate a config nobody touched, so it must not sit below a hundred
    # cosmetic additions.
    if tightened:
        lines += _bullets("⚠️ Tightened `maxLength` caps — may invalidate existing configs", tightened)
    lines += _bullets("Loosened `maxLength` caps", loosened)
    lines += _bullets("Removed properties", removed)
    lines += _bullets("Added properties", added)

    if not lines:
        lines = [
            "No property or `maxLength` changes — the drift is elsewhere in the "
            "schema (a description, default, enum or type). Read the diff.",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Detect CodeRabbit schema drift.")
    parser.add_argument("--vendored", required=True, help="Path to the committed schema.")
    parser.add_argument("--fetched", required=True, help="Path to the freshly fetched schema.")
    parser.add_argument(
        "--summary-out",
        help="Write the Markdown drift summary here as well as to stdout.",
    )
    args = parser.parse_args(argv)

    try:
        vendored = load(args.vendored, "vendored")
        fetched = load(args.fetched, "fetched")

        if canonical(vendored) == canonical(fetched):
            print("No drift: the vendored schema matches upstream.")
            return 0

        summary = summarize(vendored, fetched)
        # Written BEFORE exit 1 is returned, and inside this guard: exit 1 tells
        # the caller "drifted", and the caller acts on it by vendoring the fetch
        # and force-resetting a shared branch. Reporting drift while the summary
        # that explains it does not exist strands that branch behind a PR step
        # that then fails trying to read it.
        if args.summary_out:
            with open(args.summary_out, "w", encoding="utf-8") as f:
                f.write(summary)
    except DriftInputError as exc:
        print(f"::error::coderabbit-schema-refresh: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 — deliberate, see below
        # Anything unexpected — a non-UTF-8 fetch decoded here, a RecursionError
        # on a degenerate schema, an unwritable --summary-out — must NOT reach the
        # caller as Python's default exit 1, because 1 is the drift verdict and
        # the caller force-resets a branch on it. A failure to compare is not a
        # comparison: it comes back as 2, like every other unusable input.
        print(
            f"::error::coderabbit-schema-refresh: unexpected failure comparing the "
            f"schemas ({type(exc).__name__}: {exc}) — treating this as "
            f"'could not compare', not as drift."
        )
        return 2

    print(summary)
    return 1


if __name__ == "__main__":
    sys.exit(main())
