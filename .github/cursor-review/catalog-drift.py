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
raw output of `cursor-agent models`, it reports five kinds of drift:

  * **delisted pin** — a pinned id (panel or judge) is absent from the live
    catalog. Urgent: consumer PRs will start failing preflight.
  * **pin marked NO-ZDR** — a pinned id is still listed, but its catalog line now
    carries a NO-ZDR marker. Also urgent, for the opposite reason: nothing
    breaks, and private review diffs quietly keep flowing to a model that may
    retain them. This is the one marker the script interprets rather than merely
    reproducing (see `_NO_ZDR`).
  * **unpinned same-lab ids** — catalog ids from a lab the panel already pins,
    which the panel does *not* pin. A REVIEW-ME list, never an auto-recommendation:
    picking "newest highest-reasoning ZDR-eligible" needs human judgment, and ZDR
    especially — Cursor only marks NO-ZDR inline (e.g. a `(NO ZDR)` suffix), so any
    such marker on the line is surfaced verbatim rather than interpreted.
  * **unpinned model families** — catalog ids whose family prefix matches no pin
    at all. Quieter than the above (mostly labs the panel will never pin, so it
    renders collapsed and is never `urgent`), but it is the ONLY place a lab the
    panel already pins can surface after rebranding under a new prefix — see
    `lab_of` and the catch-all in `analyze`.
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
workflow it parses. The caller decides separately whether an urgent finding
should also redden the run (it does; see the workflow's final step).
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
# Every model id a lab has shipped to Cursor's catalog carries a version number
# somewhere (`gpt-5.6-sol-max`, `claude-opus-4-8-thinking-max`, `kimi-k2.7-code`,
# `o5-pro`), and a hyphenated PROSE word at the head of a catalog line does not
# ("`gpt-based` models are …"). Requiring a digit is what separates the two — see
# `_is_model_id`.
_HAS_DIGIT = re.compile(r"[0-9]")
_BULLET = re.compile(r"^[-*>•\s]+")
# The ONE catalog marker worth interpreting rather than merely reproducing: a
# PINNED model reclassified non-ZDR means private review diffs are flowing to a
# model that may retain them. Notes are otherwise passed through verbatim (see
# `catalog_entries`) precisely because promotion is a human call — but a
# confidentiality regression on a pin already in service is not a "review me".
_NO_ZDR = re.compile(r"no[\s_-]*zdr", re.IGNORECASE)
# How far above the `Define panel models` heredoc the `last checked` audit
# comment is allowed to sit. Anchoring the search here (rather than scanning the
# whole file) stops an unrelated `last checked` elsewhere in cursor-review.yml
# from shadowing the real pin-adjacent date; the real-workflow test guards the
# window from being too tight.
LAST_CHECKED_WINDOW = 40


class ExtractionError(Exception):
    """The pins could not be read out of the workflow file."""


def _is_model_id(token):
    """True for a bare catalog/model id — see `_ID_TOKEN`.

    Three requirements beyond the character class, each closing a different
    misparse:

      * a `-`/`.` **separator**, which keeps prose lines ("Available models:")
        out of the parsed id list;
      * a **letter**, which keeps a numbered-list marker (`1.` in a hypothetical
        `1. gpt-5.6-sol-max` catalog line) from parsing as an id. That matters
        more than it looks: `present()` trusts this parse, so a catalog of
        ['1.', '2.'] would look perfectly valid while reporting every real pin as
        delisted;
      * a **digit**, which keeps a hyphenated prose word at the head of a catalog
        line ("`gpt-based` models are …") from being admitted as a bogus id — it
        would otherwise add noise to the review-me list and, since BE-4852, mint
        a phantom `gpt-based`-style entry in the unpinned-families section.

    Parsing nothing at all instead routes a garbled catalog to `main`'s
    diagnostic "no ids could be parsed — the format may have changed" hard exit.

    All three bind the PIN validator too (`extract_panel_models` /
    `extract_judge_model` call this): a shape the catalog parser would never emit
    must not be accepted as a pin, or that pin reads as delisted every run. If a
    lab ever ships a separator-less or digit-less id, relax the rule HERE — one
    function, both sides — and `test_pin_and_catalog_id_shape_rules_agree` keeps
    them honest.
    """
    return (
        bool(_ID_TOKEN.match(token))
        and re.search(r"[-.]", token) is not None
        and re.search(r"[a-z]", token) is not None
        and _HAS_DIGIT.search(token) is not None
    )


# --------------------------------------------------------------------------
# Extraction — read the pins out of cursor-review.yml (never duplicate them)
# --------------------------------------------------------------------------


def _heredoc_start(lines):
    """Index of the `cat > /tmp/models.json <<'JSON'` line, or None."""
    for i, line in enumerate(lines):
        if _HEREDOC_START.search(line):
            return i
    return None


def extract_panel_models(workflow_text):
    """Return the panel model ids from the 'Define panel models' heredoc."""
    lines = workflow_text.splitlines()
    start = _heredoc_start(lines)
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
    # EVERY entry must look like a bare model id. A *guessed* pin set is worse
    # than a red run: the quoted-token fallback would otherwise adopt a quoted
    # comment, and even a strictly-valid JSON array can carry a placeholder
    # ("TODO pick a fifth"). Either way the checker would report a bogus pin as
    # delisted every Monday, or silently monitor the wrong set — the exact
    # silent staleness this checker exists to end.
    bad = [m for m in models if not _is_model_id(m)]
    if bad:
        raise ExtractionError(
            f"the /tmp/models.json heredoc yielded entries that are not bare model ids: {bad!r} "
            "(expected a single lowercase token containing a `-` or `.` AND a digit, e.g. "
            "`gpt-5.6-sol-max`). If a lab has shipped a separator-less or digit-less id, relax "
            "`_is_model_id` — it is the ONE rule the pin validator and the catalog parser share, "
            "and they must agree, or the pin will read as delisted every run."
        )
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
    """Return the pins' `last checked YYYY-MM-DD` audit date, or None if absent.

    Searched only in the `LAST_CHECKED_WINDOW` lines immediately above the panel
    heredoc, so an unrelated `last checked` elsewhere in cursor-review.yml can't
    shadow the real pin-adjacent date. None means "report it stale", which is the
    safe direction: a missing audit record is itself a finding.
    """
    lines = workflow_text.splitlines()
    start = _heredoc_start(lines)
    window = (
        lines[max(0, start - LAST_CHECKED_WINDOW) : start + 1] if start is not None else lines
    )
    match = _LAST_CHECKED.search("\n".join(window))
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
        if not _is_model_id(model_id):
            continue
        note = parts[1].strip() if len(parts) > 1 else ""
        if model_id in seen:
            # MERGE rather than keep-first: if the catalog lists an id twice and
            # only the second line carries `(NO ZDR)`, keeping the first note
            # would discard the one marker a human must see.
            if note and note not in _split_notes(seen[model_id]):
                seen[model_id] = f"{seen[model_id]} / {note}" if seen[model_id] else note
            continue
        seen[model_id] = note
        order.append(model_id)
    return [(m, seen[m]) for m in order]


def _split_notes(note):
    """Notes merged by `catalog_entries`, back as a list."""
    return [part.strip() for part in note.split(" / ") if part.strip()]


def present(model_id, catalog_ids):
    """Exact membership in the PARSED catalog ids.

    Deliberately NOT a substring/whole-token scan of the raw catalog text, even
    though the per-PR preflight does that: a delisted id very often still
    *appears* in the text — in its successor's replacement/deprecation note,
    which is precisely when a delist happens — and a text scan would then call
    it present, silently swallowing the one urgent finding this check exists to
    raise while consumer PRs go red at preflight. `unpinned` already compares
    against the parsed ids, so this makes them one source of truth. The
    trade-off is deliberate: a garbled catalog now over-reports (loud, and
    guarded by the "no ids parsed" hard fail) instead of under-reporting.
    """
    return model_id in catalog_ids


def lab_of(model_id):
    """Lab prefix of an id — `gpt-5.6-sol-max` -> `gpt` (preflight's split).

    A *family* prefix, strictly — not a vendor. One lab can ship under several
    (OpenAI's `o<n>` series alongside `gpt-*` is the precedent), and nothing in a
    bare id says the two belong together. Mapping them would need a hand-kept
    alias table, which is exactly the maintenance burden deriving labs from the
    pins avoids; `analyze` handles the rebrand case with the unpinned-families
    catch-all instead (BE-4852).
    """
    return re.split(r"[-.]", model_id, maxsplit=1)[0].lower()


def analyze(panel_models, judge_model, catalog_text, last_checked, today, stale_days):
    """Compare the pins against the catalog and return the drift report."""
    entries = catalog_entries(catalog_text)
    catalog_ids = {m for m, _ in entries}
    notes = dict(entries)
    pinned = list(panel_models)
    if judge_model not in pinned:
        pinned.append(judge_model)

    def roles_of(model_id):
        roles = []
        if model_id in panel_models:
            roles.append("panel")
        if model_id == judge_model:
            roles.append("judge")
        return roles

    delisted = []
    zdr_risk = []
    for model_id in pinned:
        if not present(model_id, catalog_ids):
            lab = lab_of(model_id)
            delisted.append(
                {
                    "id": model_id,
                    "roles": roles_of(model_id),
                    "lab": lab,
                    "same_lab_available": [m for m, _ in entries if lab_of(m) == lab],
                }
            )
            continue
        # Still listed — but is it still ZDR-eligible? A pin reclassified
        # NO-ZDR keeps quietly receiving private review diffs, so it is checked
        # here rather than only being surfaced for promotion candidates.
        note = notes.get(model_id, "")
        if _NO_ZDR.search(note):
            zdr_risk.append({"id": model_id, "roles": roles_of(model_id), "note": note})

    # Labs are derived from the pins themselves (not a hardcoded list) so a pin
    # bump to a newly-branded lab keeps this checker zero-maintenance. With
    # today's pins this resolves to exactly gpt- / claude- / gemini- / kimi-.
    labs = []
    for model_id in pinned:
        lab = lab_of(model_id)
        if lab not in labs:
            labs.append(lab)
    pinned_set = set(pinned)
    pinned_labs = set(labs)
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

    # The catch-all (BE-4852): catalog ids whose family prefix matches NO pin.
    # `lab_of` equates a lab with an id's first token, so an existing lab
    # shipping under a NEW family prefix — OpenAI's `o<n>` series alongside
    # `gpt-*` is the precedent — resolves to a "lab" nobody pins and would drop
    # out of the review-me list above entirely. That is precisely the "a newer
    # model shipped and nobody noticed" case this whole check exists to catch, so
    # it gets its own quieter section rather than a hand-maintained prefix→lab
    # alias table (which is the maintenance burden deriving labs from the pins
    # exists to avoid). Most of what lands here is genuinely uninteresting —
    # labs Comfy will never pin — hence: a finding, but never `urgent`, and
    # rendered collapsed.
    unpinned_labs = []
    for model_id, note in entries:
        lab = lab_of(model_id)
        if lab in pinned_labs:
            continue
        group = next((g for g in unpinned_labs if g["lab"] == lab), None)
        if group is None:
            group = {"lab": lab, "candidates": []}
            unpinned_labs.append(group)
        group["candidates"].append({"id": model_id, "note": note})

    audit = {
        "last_checked": last_checked.isoformat() if last_checked else None,
        "age_days": (today - last_checked).days if last_checked else None,
        "stale_days": stale_days,
    }
    # A NEGATIVE age is a future-dated typo, not a fresh audit — without this it
    # reads as current and suppresses the stale alert until `stale_days` past
    # that future date.
    audit["stale"] = (
        last_checked is None or audit["age_days"] > stale_days or audit["age_days"] < 0
    )
    audit["future_dated"] = audit["age_days"] is not None and audit["age_days"] < 0

    return {
        "pins": {"panel": list(panel_models), "judge": judge_model},
        "catalog_ids": [m for m, _ in entries],
        "delisted": delisted,
        "zdr_risk": zdr_risk,
        "unpinned": unpinned,
        "unpinned_labs": unpinned_labs,
        "audit": audit,
        # `urgent` is what reddens the weekly run: a pin the preflight is about
        # to reject, or a pin that is no longer ZDR-eligible. `unpinned_labs` is
        # deliberately NOT here — it is a standing watchlist, not a breakage.
        "urgent": bool(delisted or zdr_risk),
        # `unpinned_labs` DOES count, or a rebranded family would render into a
        # body nobody ever sees (a clean `has_findings` closes the sticky issue).
        # It does not newly pin the issue open: `unpinned` alone already makes
        # this true on any real catalog, which lists more tiers per pinned lab
        # than the panel pins — the auto-close path was already reserved for a
        # catalog trimmed to exactly the pins.
        "has_findings": bool(delisted or zdr_risk or unpinned or unpinned_labs or audit["stale"]),
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
    zdr = report.get("zdr_risk") or []
    if zdr:
        bits.append(f"{len(zdr)} pin{'s' if len(zdr) != 1 else ''} marked NO-ZDR")
    count = sum(len(g["candidates"]) for g in report["unpinned"])
    if count:
        bits.append(f"{count} unpinned same-lab id{'s' if count != 1 else ''}")
    families = report.get("unpinned_labs") or []
    if families:
        ids = sum(len(g["candidates"]) for g in families)
        bits.append(
            f"{len(families)} unpinned famil{'y' if len(families) == 1 else 'ies'} "
            f"({ids} id{'s' if ids != 1 else ''})"
        )
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


def _inline_code(text):
    """Render `text` as an inline code span that its own backticks can't escape.

    Catalog notes are unconstrained third-party free text reproduced verbatim in
    a bot-authored issue in a PUBLIC repo. A bare single-backtick span would let
    a note carrying a backtick break out and inject markdown — or an `@mention`
    that notifies real people — so the delimiter is sized to the note (per
    CommonMark) and padded when the note starts or ends with a backtick.
    """
    flat = re.sub(r"\s+", " ", text).strip()
    longest = max((len(m) for m in re.findall(r"`+", flat)), default=0)
    delim = "`" * (longest + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{delim}{pad}{flat}{pad}{delim}"


def _candidate_list(candidates):
    """Bullet list of `{id, note}` rows — shared by both unpinned sections."""
    return "\n".join(
        f"- `{c['id']}`" + (f" — {_inline_code(c['note'])}" if c["note"] else "")
        for c in candidates
    )


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
            "No drift: every pinned model id is present in Cursor's live catalog, the catalog "
            "offers no unpinned ids — from a pinned lab or an unpinned family — and the audit "
            "date is current."
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

    if report.get("zdr_risk"):
        out.append(
            "## 🚨 Pinned model marked NO-ZDR — fix first\n\n"
            "These ids are still in the catalog but its line now carries a NO-ZDR marker. "
            "cursor-review sends **private diffs** to every pinned model, so a pin that is no "
            "longer zero-data-retention eligible is a confidentiality regression: confirm against "
            "the catalog and repin to a ZDR-eligible tier."
        )
        for item in report["zdr_risk"]:
            roles = "/".join(item["roles"]) or "pin"
            out.append(f"- `{item['id']}` ({roles}) — catalog note: {_inline_code(item['note'])}")

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
            out.append(_candidate_list(group["candidates"]))

    families = report.get("unpinned_labs") or []
    if families:
        # Collapsed on purpose: most of this is labs the panel will never pin, so
        # it must not crowd out the same-lab review-me list above. It exists for
        # the one row that matters — a lab already on the panel shipping under a
        # new family prefix, which `lab_of` cannot tell from a new vendor.
        detail = [
            "<details>",
            f"<summary>Catalog ids from <b>unpinned model families</b> "
            f"({', '.join('`' + g['lab'] + '`' for g in families)})</summary>",
            "",
            "Families the panel pins **nothing** from. Usually just labs Comfy does not use — but "
            "the lab of an id is its first `-`/`.`-separated token, so a lab the panel DOES pin "
            "shipping under a new family prefix (OpenAI's `o<n>` series alongside `gpt-*` is the "
            "precedent) lands here rather than in the review-me list above. Scan for a familiar lab "
            "wearing an unfamiliar prefix; ignore the rest. Same caveats as above — notes are "
            "verbatim, an unmarked id is **not** thereby confirmed ZDR-eligible.",
        ]
        for group in families:
            detail.append("")
            detail.append(f"**`{group['lab']}`**")
            detail.append("")
            detail.append(_candidate_list(group["candidates"]))
        detail.append("")
        detail.append("</details>")
        out.append("\n".join(detail))

    if audit["stale"]:
        if audit["last_checked"] is None:
            out.append(
                "## Stale audit date\n\n"
                "No `last checked YYYY-MM-DD` comment was found above the panel pins in "
                "`cursor-review.yml`. That comment is the human-audit record — restore it when "
                "you next review the pins."
            )
        elif audit.get("future_dated"):
            out.append(
                f"## Stale audit date — future-dated\n\n"
                f"The `last checked` comment in `cursor-review.yml` reads "
                f"**{audit['last_checked']}**, which is in the future ({-audit['age_days']} days "
                f"from now) — almost certainly a typo. It is treated as stale rather than fresh, "
                f"since a future date would otherwise suppress this alert. Fix the date and "
                f"re-audit the pins."
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


def _quoted_for_log(text):
    """Prefix every line so none can be read as a GitHub workflow command.

    `cursor-agent` output is semi-trusted third-party text, and this runs in a
    PUBLIC repo's Actions log. A line beginning `::` would be executed by the
    runner (`::add-mask::`, `::stop-commands::`, `::error::`), so the catalog is
    never echoed raw.
    """
    return "\n".join("| " + line for line in text.splitlines())


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
            "the format may have changed. Raw output (each line prefixed `| `):"
        )
        print(_quoted_for_log(catalog_text))
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
