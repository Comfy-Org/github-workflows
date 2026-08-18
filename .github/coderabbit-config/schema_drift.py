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


def property_paths(schema):
    """Every dotted property path in the schema, e.g. `reviews.tools.eslint.enabled`."""
    paths = set()

    def walk(node, path):
        if not isinstance(node, dict):
            return
        for name, sub in (node.get("properties") or {}).items():
            child = f"{path}.{name}" if path else name
            paths.add(child)
            walk(sub, child)
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, f"{path}[]")

    walk(schema, "")
    return paths


def length_caps(schema):
    """`{dotted path: maxLength}` for every capped string in the schema.

    Called out separately from the raw property diff because a tightened cap is
    the one drift class that can turn a config that has been valid for months
    into one CodeRabbit rejects whole, with no change on the config's side.
    """
    caps = {}

    def walk(node, path):
        if not isinstance(node, dict):
            return
        cap = node.get("maxLength")
        if isinstance(cap, int):
            caps[path or "(document root)"] = cap
        for name, sub in (node.get("properties") or {}).items():
            walk(sub, f"{path}.{name}" if path else name)
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, f"{path}[]")

    walk(schema, "")
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
    for path, cap in sorted(new_caps.items()):
        was = old_caps.get(path)
        if was is None or was == cap:
            continue
        (tightened if cap < was else loosened).append(f"{path}: {was} → {cap}")

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
    except DriftInputError as exc:
        print(f"::error::coderabbit-schema-refresh: {exc}")
        return 2

    if canonical(vendored) == canonical(fetched):
        print("No drift: the vendored schema matches upstream.")
        return 0

    summary = summarize(vendored, fetched)
    print(summary)
    if args.summary_out:
        with open(args.summary_out, "w", encoding="utf-8") as f:
            f.write(summary)
    return 1


if __name__ == "__main__":
    sys.exit(main())
