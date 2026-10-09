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

Two cross-checks against what a cell's record claims, both because a `review`
cell runs its agent `--trust` with shell access over PR-authored text and can
upload an artifact under ANOTHER cell's name before that cell does (the honest
leg's own upload then 409s and goes red):

- name vs record: a cell's identity is its artifact NAME,
  `findings-<review_type>-<model>` / `findings-direct-<review_type>-<model>`,
  which the workflow chose. A record whose own `model` / `review_type` disagree
  with it (or sits under a name that does not parse, or names no cell the
  matrix runs) gets `status: "mismatch"` — not `ok`, and not the `error`
  auto-approve may tolerate — so it counts as a failed reviewer. Its
  `model` / `review_type` are rewritten to the name's (the claimed pair kept
  as `claimed_model` / `claimed_review_type`), so it cannot occupy, or count
  twice in, a slot it was not uploaded for.
- leg result vs records (`--review-result`, `--direct-result`, the
  `needs.<job>.result` of each matrix): `Fail the leg when the cell did not
  submit` reds every leg whose cell is not `ok`, so a matrix that did not
  succeed while EVERY counted cell of it reads `ok` is inconsistent — some leg
  failed after its agent ran, by an upload conflict (the forgery signature) or
  a post-review infrastructure failure. Either way the count cannot be
  trusted. The direct matrix is checked only when its cells count; advisory,
  its legs absorb their own failures.

Either cross-check failing on a counted cell writes `panel_inconsistent=true`
to GITHUB_OUTPUT for auto-approve and `Panel integrity` to withhold on — a
step output, not a field in panel.json: the judge runs `--trust` in the same
job afterwards and can rewrite the file, but not an earlier step's outputs.
No finding is dropped either way, so the judge still reads every one.

The leg-result check is per matrix, so ANY non-ok cell in it explains every
red leg away: a forged `ok` slips through whenever another cell of the same
matrix errored (a step-cap timeout, or the forger's own), and a forger able to
delete the honest upload and re-upload under its name leaves no red
leg at all. Telling which leg failed needs per-job conclusions, which this
job has no `actions: read` to see.

Stdlib only. Usage:
  aggregate-panel.py --panel-dir /tmp/panel --models '<json list>' \
      [--direct-model <id> --direct-counts true|false] \
      --review-result <result> [--direct-result <result>] \
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
# The status a record gets when it disagrees with the artifact name it sits
# under. Deliberately not auto-approve's tolerable `error`.
MISMATCH_STATUS = "mismatch"
# The review_type a mismatched record gets when its artifact name does not parse.
UNKNOWN_TYPE = "unknown"


def identity_from_name(artifact):
    """(review_type, model) the workflow named this artifact for, or (None, None)."""
    for prefix in (DIRECT_PREFIX, "findings-"):
        if artifact.startswith(prefix):
            rest = artifact[len(prefix):]
            break
    else:
        return None, None
    for review_type in REVIEW_TYPES:
        if rest.startswith(review_type + "-") and len(rest) > len(review_type) + 1:
            return review_type, rest[len(review_type) + 1:]
    return None, None


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
        artifact = os.path.basename(os.path.dirname(path))
        review_type, model = identity_from_name(artifact)
        if (cell.get("review_type"), cell.get("model")) != (review_type, model) or review_type is None:
            print(
                f"::warning::Findings artifact {_log_safe(artifact, 100)} holds a record for "
                f"{_log_safe(cell.get('review_type'))}/{_log_safe(cell.get('model'))}, not the cell "
                f"it is named for; counted as status={MISMATCH_STATUS}."
            )
            _mark_mismatch(cell, review_type or UNKNOWN_TYPE, model or artifact)
        if artifact.startswith(DIRECT_PREFIX):
            direct.append(cell)
        else:
            cursor.append(cell)
    return cursor, direct


def _mark_mismatch(cell, review_type, model):
    """Key `cell` to the slot its artifact name gives it, keeping its claim aside."""
    cell["claimed_review_type"] = cell.get("review_type")
    cell["claimed_model"] = cell.get("model")
    cell["review_type"] = review_type
    cell["model"] = model
    cell["status"] = MISMATCH_STATUS


def flag_unexpected(cells, models):
    """Mismatch every cell keyed to no (model, type) the matrix runs.

    A cell can upload under a made-up name as well as a victim's: a phantom
    `error` would otherwise explain a red leg away (and be tolerated), and a
    phantom `ok` keep a review type "completed".
    """
    expected = {(m, t) for m in models for t in REVIEW_TYPES}
    for c in cells:
        if (c.get("model"), c.get("review_type")) not in expected and c.get("status") != MISMATCH_STATUS:
            print(
                f"::warning::Findings artifact for {_log_safe(c.get('review_type'))}/"
                f"{_log_safe(c.get('model'))} is not a cell this panel runs; "
                f"counted as status={MISMATCH_STATUS}."
            )
            c["status"] = MISMATCH_STATUS


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


def _all_ok(cells):
    return all(c.get("status") == "ok" for c in cells)


def aggregate(panel_dir, models, direct_model="", direct_counts=False,
              review_result="success", direct_result="skipped"):
    """Return (cells, ok_count, total, inconsistent).

    `cells` is everything the judge reads; `inconsistent` is either
    cross-check in the module docstring failing on a counted cell.
    """
    panel, direct = load_cells(panel_dir)
    flag_unexpected(panel, models)
    fill_missing(panel, models, "Cell findings artifact was not uploaded.")
    counts = bool(direct_counts and direct_model)
    if counts:
        flag_unexpected(direct, [direct_model])
        # Same contract as a Cursor cell: absent means failed, never "not run".
        fill_missing(direct, [direct_model], "Direct-API cell findings artifact was not uploaded.")
    for c in direct:
        c["direct_api"] = True
        if not counts:
            c["advisory"] = True
    counted = panel + direct if counts else panel
    ok = sum(1 for c in counted if c.get("status") == "ok")
    inconsistent = (
        (review_result != "success" and _all_ok(panel))
        or (counts and direct_result not in ("success", "skipped") and _all_ok(direct))
        or any(c.get("status") == MISMATCH_STATUS for c in counted)
    )
    return panel + direct, ok, len(counted), inconsistent


def _log_safe(value, limit=32):
    """A cell-written value made safe for a ::workflow command::-parsing log line."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))[:limit] or "?"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--panel-dir", required=True)
    p.add_argument("--models", required=True, help="JSON list of Cursor panel model ids")
    p.add_argument("--direct-model", default="")
    p.add_argument("--direct-counts", default="false")
    # Required, so a dropped flag fails the step rather than reading as success.
    p.add_argument("--review-result", required=True, help="needs.review.result")
    p.add_argument("--direct-result", default="skipped", help="needs.review-openai-direct.result")
    p.add_argument("--out", required=True)
    p.add_argument("--github-output", default="")
    args = p.parse_args(argv)

    counts = args.direct_counts.strip().lower() == "true"
    review_result = args.review_result.strip()
    direct_result = args.direct_result.strip()
    cells, ok, total, inconsistent = aggregate(
        args.panel_dir, json.loads(args.models), args.direct_model.strip(), counts,
        review_result, direct_result,
    )
    print(f"Panel: {ok}/{total} cells contributed findings.")
    if inconsistent:
        print(
            f"::warning::A counted findings artifact may not be its own cell's: a reviewer matrix "
            f"did not succeed (review={_log_safe(review_result)}, direct={_log_safe(direct_result)}) "
            f"although every counted cell artifact of it reads ok, or a record is "
            f"status={MISMATCH_STATUS}. The panel count is untrusted (panel_inconsistent=true)."
        )
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
            g.write(f"panel_inconsistent={'true' if inconsistent else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
