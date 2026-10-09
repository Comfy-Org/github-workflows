#!/usr/bin/env python3
"""Combine the panel cells' findings artifacts into one panel.json.

Run by `consolidate`'s `Aggregate panel findings` step, after
`Download panel findings` has unpacked every `findings-*` artifact under one
directory. Each artifact directory holds one findings.json. The output keeps the
cell metadata the judge and `Build consolidated findings file` read, and the
`ok_count` / `total` pair is what `Panel integrity` and auto-approve gate on.

The opt-in direct-API cells (`findings-direct-<review_type>-<model>`) take one
of two roles, chosen by `--direct-counts`:

- counted (`openai_direct_replaces_cursor_openai: true`): the direct cells ARE
  the panel's OpenAI lane, so they join the expected set and ok_count/total
  exactly like a Cursor cell — a missing or errored one is a failed reviewer.
- advisory (replace false, the side-by-side comparison): their findings still
  reach the judge, but they stay out of `present` (so one cannot mask a missing
  Cursor cell with the same id) and out of ok_count/total.

Two markers are set HERE and only here — a cell's own record is model-adjacent
output and must never be able to excuse itself from the count: `direct_api`
(this cell came through the direct backend) and `advisory` (keep it out of the
panel metadata auto-approve counts failures from).

Stdlib only. Usage:
  aggregate-panel.py --panel-dir /tmp/panel --models '<json list>' \
      [--direct-model <id> --direct-counts true|false] \
      --out /tmp/panel.json [--github-output "$GITHUB_OUTPUT"]
"""

import argparse
import glob
import json
import os
import re
import sys

REVIEW_TYPES = ("adversarial", "edge-case")
DIRECT_PREFIX = "findings-direct-"
MARKERS = ("direct_api", "advisory")


def load_cells(panel_dir):
    """(cursor_cells, direct_cells) read from `<panel_dir>/findings-*/findings.json`."""
    cursor, direct = [], []
    for path in sorted(glob.glob(os.path.join(panel_dir, "findings-*", "findings.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                cell = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(cell, dict):
            continue
        for marker in MARKERS:
            cell.pop(marker, None)
        if os.path.basename(os.path.dirname(path)).startswith(DIRECT_PREFIX):
            direct.append(cell)
        else:
            cursor.append(cell)
    return cursor, direct


def fill_missing(cells, models, error):
    """Append a status=error record for every expected (model, type) with no cell."""
    present = {(c.get("model"), c.get("review_type")) for c in cells}
    expected = {(m, t) for m in models for t in REVIEW_TYPES}
    for model, review_type in sorted(expected - present):
        cells.append({
            "model": model,
            "review_type": review_type,
            "status": "error",
            "error": error,
            "findings": [],
        })


def aggregate(panel_dir, models, direct_model="", direct_counts=False):
    """Return (cells, ok_count, total). `cells` is everything the judge reads."""
    panel, direct = load_cells(panel_dir)
    fill_missing(panel, models, "Cell findings artifact was not uploaded.")
    counts = bool(direct_counts and direct_model)
    if counts:
        # Same contract as a Cursor cell: absent means failed, never "not run".
        fill_missing(direct, [direct_model], "Direct-API cell findings artifact was not uploaded.")
    for c in direct:
        c["direct_api"] = True
        if not counts:
            c["advisory"] = True
    counted = panel + direct if counts else panel
    ok = sum(1 for c in counted if c.get("status") == "ok")
    return panel + direct, ok, len(counted)


def _log_safe(value):
    """A cell-written value made safe for a ::workflow command::-parsing log line."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))[:32] or "?"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--panel-dir", required=True)
    p.add_argument("--models", required=True, help="JSON list of Cursor panel model ids")
    p.add_argument("--direct-model", default="")
    p.add_argument("--direct-counts", default="false")
    p.add_argument("--out", required=True)
    p.add_argument("--github-output", default="")
    args = p.parse_args(argv)

    counts = args.direct_counts.strip().lower() == "true"
    cells, ok, total = aggregate(
        args.panel_dir, json.loads(args.models), args.direct_model.strip(), counts
    )
    print(f"Panel: {ok}/{total} cells contributed findings.")
    for c in cells:
        if c.get("direct_api"):
            role = "advisory, not counted" if c.get("advisory") else "counted"
            print(
                f"Direct-API cell {_log_safe(c.get('review_type'))} ({role}): "
                f"status={_log_safe(c.get('status'))}."
            )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cells, f, indent=2)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as g:
            g.write(f"ok_count={ok}\n")
            g.write(f"total={total}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
