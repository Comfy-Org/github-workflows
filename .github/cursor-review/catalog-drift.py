#!/usr/bin/env python3
"""Detect drift between the cursor-review panel pins and Cursor's live catalog.

The per-PR preflight in `cursor-review.yml` already fails loud when a *pinned*
model id disappears — but only at review time, on somebody's PR, after the pin
has already gone bad. And by design it never notices a *newer* model shipping.
Until BE-4819 the only guard against that second case was a `last checked
<date>` comment above the pins, which went stale inside 13 days: both catalog
swaps found by the BE-4817 audit (Opus 5 on 2026-07-24, Kimi K3 on ~2026-07-26)
landed in that window and were caught by hand, not by CI.

This script is the machine half of that audit. Given the workflow file and the
raw output of `cursor-agent models`, it reports three kinds of drift:

  * **delisted pin** — a pinned id (panel or judge) is absent from the live
    catalog. Urgent: consumer PRs will start failing preflight.
  * **unpinned same-lab ids** — catalog ids from a lab the panel already pins,
    which the panel does *not* pin. A REVIEW-ME list, never an auto-recommendation:
    picking "newest highest-reasoning ZDR-eligible" needs human judgment, and ZDR
    especially — Cursor only marks NO-ZDR inline (e.g. a `(NO ZDR)` suffix), so any
    such marker on the line is surfaced verbatim rather than interpreted.
  * **stale audit date** — the `last checked YYYY-MM-DD` comment is older than
    `--stale-days` (or missing entirely).

Pins are read out of `cursor-review.yml` itself rather than duplicated here, so
this checker stays zero-maintenance when the pins change.

Usage (see cursor-review-catalog-drift.yml):

    python3 catalog-drift.py \
      --workflow .github/workflows/cursor-review.yml \
      --catalog /tmp/catalog.txt \
      --title-out /tmp/drift-title.txt \
      --body-out /tmp/drift-body.md \
      --json-out /tmp/drift-report.json \
      --run-url "$RUN_URL"

Exit code is 0 for both "drift" and "no drift" — findings are reported through
the sticky issue, not the run status. A non-zero exit means the checker itself
could not run (pins unreadable), which is a real defect in the checker or in the
workflow it parses. The caller decides separately whether a delisted pin should
also redden the run (it does; see the workflow's final step).
"""

import argparse
import datetime
import json
import os
import re
import sys

STICKY_TITLE_PREFIX = "[cursor-review catalog drift]"
DEFAULT_STALE_DAYS = 30
# GitHub caps an issue body at 65536 chars; leave room for the report above the
# raw-catalog fold rather than losing the whole body to a 422.
MAX_CATALOG_CHARS = 40000
MAX_BODY_CHARS = 60000

_HEREDOC_START = re.compile(r"cat\s*>\s*/tmp/models\.json\s*<<\s*'?JSON'?")
_HEREDOC_END = re.compile(r"^\s*JSON\s*$")
_QUOTED = re.compile(r'"([^"]+)"')
_JUDGE_KEY = re.compile(r"^(\s*)judge_model\s*:\s*$")
_DEFAULT_KEY = re.compile(r"^\s*default\s*:\s*(.+?)\s*$")
_LAST_CHECKED = re.compile(r"last checked\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
# A catalog id is lowercase alphanumeric with `.`/`-`/`_` separators, and always
# carries at least one `-` or `.` — that separator requirement is what keeps
# prose lines ("Available models:") out of the parsed id list.
_ID_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_BULLET = re.compile(r"^[-*>•\s]+")


class ExtractionError(Exception):
    """The pins could not be read out of the workflow file."""


# --------------------------------------------------------------------------
# Extraction — read the pins out of cursor-review.yml (never duplicate them)
# --------------------------------------------------------------------------


def extract_panel_models(workflow_text):
    """Return the panel model ids from the 'Define panel models' heredoc."""
    lines = workflow_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _HEREDOC_START.search(line):
            start = i
            break
    if start is None:
        raise ExtractionError(
            "could not find the `cat > /tmp/models.json <<'JSON'` heredoc in the "
            "preflight 'Define panel models' step"
        )

    body = []
    terminated = False
    for line in lines[start + 1 :]:
        if _HEREDOC_END.match(line):
            terminated = True
            break
        body.append(line)
    if not terminated:
        raise ExtractionError("the /tmp/models.json heredoc is not terminated by a `JSON` line")

    raw = "\n".join(body)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Tolerate a heredoc that stops being strict JSON (a trailing comma, an
        # inline comment): fall back to the quoted tokens, same as the ticket's
        # `grep -oE '"[^"]+"'` sketch.
        parsed = _QUOTED.findall(raw)
    if not isinstance(parsed, list) or not all(isinstance(m, str) for m in parsed):
        raise ExtractionError("the /tmp/models.json heredoc is not a JSON array of strings")
    models = [m.strip() for m in parsed if m.strip()]
    if not models:
        raise ExtractionError("the /tmp/models.json heredoc contains no model ids")
    return models


def extract_judge_model(workflow_text):
    """Return the `judge_model` workflow input's default value."""
    lines = workflow_text.splitlines()
    for i, line in enumerate(lines):
        key = _JUDGE_KEY.match(line)
        if not key:
            continue
        indent = len(key.group(1))
        for follow in lines[i + 1 :]:
            if not follow.strip():
                continue
            if len(follow) - len(follow.lstrip()) <= indent:
                break  # left the judge_model block without seeing a default
            default = _DEFAULT_KEY.match(follow)
            if default:
                # Trim an inline `# comment`, then quotes. A folded/literal
                # scalar (`default: >-`) or any value with embedded whitespace
                # is not a bare model id — raise rather than "check" a pin like
                # `>-`, which would report as delisted on every single run.
                value = re.split(r"\s+#", default.group(1).strip(), maxsplit=1)[0].strip()
                value = value.strip("\"'")
                if value and value[0] not in ">|" and not re.search(r"\s", value):
                    return value
                raise ExtractionError(
                    f"the `judge_model` input's default is not a bare model id: {value!r}"
                )
        break
    raise ExtractionError("could not find the `judge_model` input's `default:` value")


def extract_last_checked(workflow_text):
    """Return the `last checked YYYY-MM-DD` audit date, or None if absent."""
    match = _LAST_CHECKED.search(workflow_text)
    if not match:
        return None
    try:
        return datetime.date.fromisoformat(match.group(1))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Catalog parsing + comparison
# --------------------------------------------------------------------------


def catalog_entries(catalog_text):
    """Parse `cursor-agent models` output into ordered (id, note) pairs.

    `note` is whatever else the line carries — Cursor marks non-ZDR models
    inline (e.g. a `(NO ZDR)` suffix), and that marker is exactly what a human
    needs to see before promoting a model into a panel that reviews private
    diffs, so it is passed through verbatim rather than parsed.
    """
    seen = {}
    order = []
    for raw in catalog_text.splitlines():
        line = _BULLET.sub("", raw.strip())
        if not line:
            continue
        parts = line.split(None, 1)
        model_id = parts[0]
        if not _ID_TOKEN.match(model_id) or not re.search(r"[-.]", model_id):
            continue
        note = parts[1].strip() if len(parts) > 1 else ""
        if model_id in seen:
            if note and not seen[model_id]:
                seen[model_id] = note
            continue
        seen[model_id] = note
        order.append(model_id)
    return [(m, seen[m]) for m in order]


def present(model_id, catalog_text):
    """Whole-token containment check — the same regex the preflight uses."""
    pattern = r"(?<![\w.-])" + re.escape(model_id) + r"(?![\w.-])"
    return re.search(pattern, catalog_text) is not None


def lab_of(model_id):
    """Lab prefix of an id — `gpt-5.6-sol-max` -> `gpt` (preflight's split)."""
    return re.split(r"[-.]", model_id, maxsplit=1)[0].lower()


def analyze(panel_models, judge_model, catalog_text, last_checked, today, stale_days):
    """Compare the pins against the catalog and return the drift report."""
    entries = catalog_entries(catalog_text)
    pinned = list(panel_models)
    if judge_model not in pinned:
        pinned.append(judge_model)

    delisted = []
    for model_id in pinned:
        if present(model_id, catalog_text):
            continue
        lab = lab_of(model_id)
        roles = []
        if model_id in panel_models:
            roles.append("panel")
        if model_id == judge_model:
            roles.append("judge")
        delisted.append(
            {
                "id": model_id,
                "roles": roles,
                "lab": lab,
                "same_lab_available": [m for m, _ in entries if lab_of(m) == lab],
            }
        )

    # Labs are derived from the pins themselves (not a hardcoded list) so a pin
    # bump to a newly-branded lab keeps this checker zero-maintenance. With
    # today's pins this resolves to exactly gpt- / claude- / gemini- / kimi-.
    labs = []
    for model_id in pinned:
        lab = lab_of(model_id)
        if lab not in labs:
            labs.append(lab)
    pinned_set = set(pinned)
    unpinned = []
    for lab in labs:
        candidates = [
            {"id": m, "note": note} for m, note in entries if lab_of(m) == lab and m not in pinned_set
        ]
        if candidates:
            unpinned.append(
                {
                    "lab": lab,
                    "pinned": [m for m in pinned if lab_of(m) == lab],
                    "candidates": candidates,
                }
            )

    audit = {
        "last_checked": last_checked.isoformat() if last_checked else None,
        "age_days": (today - last_checked).days if last_checked else None,
        "stale_days": stale_days,
    }
    audit["stale"] = last_checked is None or audit["age_days"] > stale_days

    return {
        "pins": {"panel": list(panel_models), "judge": judge_model},
        "catalog_ids": [m for m, _ in entries],
        "delisted": delisted,
        "unpinned": unpinned,
        "audit": audit,
        "urgent": bool(delisted),
        "has_findings": bool(delisted or unpinned or audit["stale"]),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def summary_line(report):
    """One-line description of the findings — used as the sticky issue title."""
    bits = []
    delisted = report["delisted"]
    if delisted:
        bits.append(f"{len(delisted)} delisted pin{'s' if len(delisted) != 1 else ''}")
    count = sum(len(g["candidates"]) for g in report["unpinned"])
    if count:
        bits.append(f"{count} unpinned same-lab id{'s' if count != 1 else ''}")
    audit = report["audit"]
    if audit["stale"]:
        if audit["last_checked"] is None:
            bits.append("no audit date")
        else:
            bits.append(f"audit date {audit['age_days']}d old")
    return ", ".join(bits) if bits else "no drift"


def issue_title(report):
    return f"{STICKY_TITLE_PREFIX} {summary_line(report)}"


def _fenced(text):
    """Fence `text` with enough backticks to survive fences inside it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def render_body(report, catalog_text, run_url=None, checked_at=None):
    """Render the sticky issue body (also used as the run's step summary)."""
    pins = report["pins"]
    audit = report["audit"]
    out = []

    if report["has_findings"]:
        out.append(
            "Drift detected between the cursor-review panel pins and Cursor's live model "
            "catalog. Pins are read from `.github/workflows/cursor-review.yml` (the preflight "
            "`Define panel models` step + the `judge_model` input default) — update them there, "
            "not here."
        )
    else:
        out.append(
            "No drift: every pinned model id is present in Cursor's live catalog, no unpinned "
            "same-lab ids are available, and the audit date is current."
        )

    meta = []
    if checked_at:
        meta.append(f"Checked **{checked_at}**")
    if run_url:
        meta.append(f"[run]({run_url})")
    meta.append("panel pins: " + ", ".join(f"`{m}`" for m in pins["panel"]))
    meta.append(f"judge pin: `{pins['judge']}`")
    out.append(" · ".join(meta))

    if report["delisted"]:
        out.append(
            f"## 🚨 Delisted pin{'s' if len(report['delisted']) != 1 else ''} — fix first\n\n"
            "These pinned ids are **no longer in the catalog**. Every consumer PR that triggers "
            "cursor-review will fail the preflight job until the pin is updated."
        )
        for item in report["delisted"]:
            roles = "/".join(item["roles"]) or "pin"
            same_lab = (
                ", ".join(f"`{m}`" for m in item["same_lab_available"])
                if item["same_lab_available"]
                else "_(no same-lab id in the catalog)_"
            )
            out.append(f"- `{item['id']}` ({roles}) — available for lab `{item['lab']}`: {same_lab}")

    if report["unpinned"]:
        out.append(
            "## Unpinned same-lab catalog ids — review me\n\n"
            "Catalog ids from labs the panel already pins that are **not** pinned today. This is a "
            "**review-me list, not a recommendation**: picking the newest highest-reasoning "
            "*ZDR-eligible* model is a human call. Cursor only marks NO-ZDR models inline, so any "
            "marker on the catalog line is reproduced verbatim below — an id with no marker is "
            "**not** thereby confirmed ZDR-eligible; check the catalog before promoting one."
        )
        for group in report["unpinned"]:
            pinned_now = ", ".join(f"`{m}`" for m in group["pinned"]) or "_none_"
            out.append(f"**`{group['lab']}`** (pinned: {pinned_now})")
            out.append(
                "\n".join(
                    f"- `{c['id']}`" + (f" — `{c['note']}`" if c["note"] else "")
                    for c in group["candidates"]
                )
            )

    if audit["stale"]:
        if audit["last_checked"] is None:
            out.append(
                "## Stale audit date\n\n"
                "No `last checked YYYY-MM-DD` comment was found above the panel pins in "
                "`cursor-review.yml`. That comment is the human-audit record — restore it when "
                "you next review the pins."
            )
        else:
            out.append(
                f"## Stale audit date\n\n"
                f"The pins were last human-audited on **{audit['last_checked']}** "
                f"({audit['age_days']} days ago, threshold {audit['stale_days']}). Re-audit the "
                f"pins and refresh the `last checked` comment in `cursor-review.yml`."
            )

    catalog = catalog_text.rstrip("\n")
    if len(catalog) > MAX_CATALOG_CHARS:
        catalog = catalog[:MAX_CATALOG_CHARS] + "\n… truncated — see the workflow run log for the full catalog."
    out.append(
        "<details>\n<summary>Raw <code>cursor-agent models</code> output</summary>\n\n"
        + _fenced(catalog)
        + "\n</details>"
    )
    out.append(
        "_Filed by the weekly `cursor-review-catalog-drift` check. This issue is sticky — it is "
        "updated in place each run and closed automatically once a run finds no drift._"
    )
    body = "\n\n".join(out) + "\n"
    if len(body) > MAX_BODY_CHARS:
        # Last-resort clamp: GitHub rejects an oversized body outright (422), and
        # a failed issue write would lose the whole report. A truncated report
        # still names the delisted pins, which lead the body.
        body = body[:MAX_BODY_CHARS] + "\n\n_… report truncated — see the workflow run log._\n"
    return body


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write(path, text):
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflow", required=True, help="Path to cursor-review.yml.")
    parser.add_argument("--catalog", required=True, help="Path to raw `cursor-agent models` output.")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    parser.add_argument("--now", default=None, help="ISO-8601 timestamp to treat as now (tests).")
    parser.add_argument("--run-url", default=None)
    parser.add_argument("--title-out", default=None)
    parser.add_argument("--body-out", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    with open(args.workflow, encoding="utf-8") as handle:
        workflow_text = handle.read()
    with open(args.catalog, encoding="utf-8") as handle:
        catalog_text = handle.read()

    if args.now:
        now = datetime.datetime.fromisoformat(args.now)
    else:
        now = datetime.datetime.now(datetime.timezone.utc)

    try:
        panel_models = extract_panel_models(workflow_text)
        judge_model = extract_judge_model(workflow_text)
    except ExtractionError as exc:
        # Better red than silent: if the pins can't be read, the checker cannot
        # say anything true about drift, and a green run would be a lie.
        print(f"::error::Could not read the cursor-review pins from {args.workflow}: {exc}")
        return 1

    if not catalog_text.strip():
        print("::error::The Cursor catalog output is empty — nothing to compare the pins against.")
        return 1
    if not catalog_entries(catalog_text):
        # Non-empty but no id parsed = a garbled catalog (an error page, a
        # format change), not a catalog where every pin happens to be delisted.
        # Fail loudly instead of crying wolf about four delisted pins.
        print(
            "::error::No model ids could be parsed out of the Cursor catalog output — "
            "the format may have changed. Raw output:"
        )
        print(catalog_text)
        return 1

    report = analyze(
        panel_models,
        judge_model,
        catalog_text,
        extract_last_checked(workflow_text),
        now.date(),
        args.stale_days,
    )
    body = render_body(report, catalog_text, args.run_url, now.strftime("%Y-%m-%d %H:%M UTC"))

    _write(args.title_out, issue_title(report) + "\n")
    _write(args.body_out, body)
    _write(args.json_out, json.dumps(report, indent=2) + "\n")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"has_findings={str(report['has_findings']).lower()}\n")
            handle.write(f"urgent={str(report['urgent']).lower()}\n")

    print(f"Panel pins: {', '.join(panel_models)}")
    print(f"Judge pin: {judge_model}")
    print(f"Catalog ids parsed: {len(report['catalog_ids'])}")
    print(f"Drift: {summary_line(report)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
