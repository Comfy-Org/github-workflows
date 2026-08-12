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
    such marker on the line is surfaced verbatim rather than interpreted. Rendered
    one row per model *family* rather than per id (BE-6911): the panel pins one
    reasoning/speed tier per family, so every other tier of every pinned family is
    an "unpinned candidate" forever — 178 rows on the 2026-08-10 catalog, in which
    the two genuinely new families were below the truncation cut. See
    `TIER_SUFFIXES`.
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
# GitHub caps an issue body at 65536 chars. MAX_CATALOG_CHARS is only the raw
# fold's CEILING — `render_body` grants the fold whatever MAX_BODY_CHARS the
# report sections have not used, so the two budgets cannot overlap into a 422.
MAX_CATALOG_CHARS = 40000
MAX_BODY_CHARS = 60000
# Budgets for the report's candidate lists, so `MAX_BODY_CHARS` (a blunt
# `body[:N]`) can never cut into their markup. MAX_FAMILY_IDS caps the ROWS of
# every per-lab list — but a row is not always one id: the same-lab review-me
# list collapses a family's reasoning/speed tiers into a single row (see
# `TIER_SUFFIXES`), while a delisted pin's alternatives and the families fold
# still list raw ids. MAX_FAMILY_LABS plus the CHARS budget bound the families
# fold, the one list whose group count the catalog controls — row caps alone
# bound its rows, not its chars (40 labs × 25 capped-note rows is still ~4× the
# body cap).
#
# Sized against a real catalog rather than a guess: the 2026-08-10 one carried
# 178 unpinned ids across the four pinned labs, which collapse to ~40 family
# rows — so with tier collapse the cap is not reached on today's catalog at all.
# It is still a cap, so ordering is what makes it safe: the same-lab list is
# sorted newest-family-version-first (`_family_version`), so what MAX_FAMILY_IDS
# drops is the oldest families rather than an arbitrary tail of Cursor's print
# order. The families fold is a watchlist scanned whole, so it keeps catalog
# order.
MAX_FAMILY_LABS = 40
MAX_FAMILY_IDS = 25
MAX_FAMILY_FOLD_CHARS = 15000
# Tier tokens listed on one collapsed row before it says `+N more`. The tier
# vocabulary is fixed (`len(TIER_SUFFIXES)`, doubled by the `-fast` twins), so a
# row is bounded either way — but "bounded" is not "small". At this value a
# collapsed row costs about what the single id row it replaces used to, so the
# section's worst-case char budget (every pinned lab at MAX_FAMILY_IDS rows,
# every note at MAX_NOTE_CHARS) is unchanged by the collapse; listing all 17
# would inflate it ~15% and eat into what is left for the raw catalog fold.
# Tiers are listed strongest-first and the count is always stated in full ahead
# of the list, so a truncated row still says how many tiers the family offers
# and drops the weakest — the part a promotion decision does not use.
MAX_TIERS_PER_ROW = 8
# Cap on a rendered note. Notes are third-party free text repeated across
# dozens of rows; unbounded, a single note could eat the whole body budget.
MAX_NOTE_CHARS = 200
# Scaffolding reserve when computing the raw fold's remaining budget: the
# <details> wrapper, the code-fence lines, and a truncation notice.
_FOLD_OVERHEAD = 400

_HEREDOC_START = re.compile(r"cat\s*>\s*/tmp/models\.json\s*<<\s*'?JSON'?")
_HEREDOC_END = re.compile(r"^\s*JSON\s*$")
_QUOTED = re.compile(r'"([^"]+)"')
_JUDGE_KEY = re.compile(r"^(\s*)judge_model\s*:\s*$")
_DEFAULT_KEY = re.compile(r"^\s*default\s*:\s*(.+?)\s*$")
_LAST_CHECKED = re.compile(r"last checked\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
# A catalog id is lowercase alphanumeric with `.`/`-`/`_` separators, and always
# carries a `-`/`.` or a digit — that requirement is what keeps prose lines
# ("models available:") out of the parsed id list while still admitting a bare
# id like `o3`.
_ID_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
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

# The reasoning/speed suffixes Cursor appends to a model family, and the ONE
# place they are enumerated. Cursor ships a family at every reasoning tier and
# most of them again as `-fast`, but the panel pins exactly ONE tier per family
# — so without collapsing, every other tier of every family the panel ALREADY
# pins is reported as an unpinned "candidate" forever. That is what buried the
# two genuinely new families under 178 rows on 2026-08-10 (BE-6911).
#
# `extra-high` precedes `high` because the family/tier split takes the LONGEST
# tier that matches (see `_TIER_SUFFIX_RE`) — otherwise `gpt-5.5-extra-high`
# would read as family `gpt-5.5-extra`. A bare `-fast` with no tier in front of
# it (`gpt-5.3-codex-fast`) is a tier too: it is the default tier, run fast.
#
# NOT included, deliberately: `-thinking`. It reads like a reasoning knob but
# the panel pins ON it — `claude-opus-5-thinking-max` and `claude-opus-5-max`
# are different products for this purpose — so collapsing the two together
# would hide exactly the distinction a promotion decision turns on.
TIER_SUFFIXES = ("extra-high", "minimal", "none", "low", "medium", "high", "xhigh", "max")
SPEED_SUFFIX = "fast"
_TIER_SUFFIX_RE = re.compile(
    r"^(?P<family>.+?)-(?P<tier>(?:(?:{tiers})(?:-{fast})?|{fast}))$".format(
        tiers="|".join(re.escape(tier) for tier in TIER_SUFFIXES),
        fast=re.escape(SPEED_SUFFIX),
    )
)
# Rough capability order of the tiers, weakest first. Used for ONE thing: which
# member of a collapsed family lends the row its catalog note (the tier a human
# would most plausibly promote). It is not a recommendation and never reaches
# the report — the note itself is still reproduced verbatim.
_TIER_STRENGTH = ("", "none", "minimal", "low", "medium", "high", "extra-high", "xhigh", "max")
_VERSION_TOKEN = re.compile(r"\d+")


class ExtractionError(Exception):
    """The pins could not be read out of the workflow file."""


def _is_model_id(token):
    """True for a bare catalog/model id — see `_ID_TOKEN`.

    Two requirements beyond the character class, each closing a different
    misparse:

      * a `-`/`.` separator **or a digit**, which keeps bare prose words
        ("models", "available") out of the parsed id list;
      * a **letter**, which keeps a numbered-list marker (`1.` in a hypothetical
        `1. gpt-5.6-sol-max` catalog line — or a bare year in prose) from
        parsing as an id. That matters more than it looks: `present()` trusts
        this parse, so a catalog of ['1.', '2.'] would look perfectly valid
        while reporting every real pin as delisted.

    Parsing nothing at all instead routes a garbled catalog to `main`'s
    diagnostic "no ids could be parsed — the format may have changed" hard exit.

    Deliberately NOT requiring a digit (Cursor ships digit-less ids for real —
    `code-supernova`) and NOT requiring a separator either (OpenAI ships bare
    o-series ids — `o3`). Each rule was tried and dropped for the same reason:
    an id it rejects is dropped by `catalog_entries` BEFORE parsing, so a newly
    shipped family of that shape never reaches the BE-4852 unpinned-families
    catch-all and the run reports clean — the exact "a new model shipped and
    nobody noticed" silence this whole check exists to end, and for the
    separator rule that silence hit exactly the bare o-series rebrand the
    catch-all was built for. What each rule bought was keeping some prose at
    the head of a catalog line out of the id list ("`gpt-based` models …", or
    "`v2` models …" for the separator); what it cost was dropping real ids.
    That trade is the wrong way round, and in the same direction `present`
    already argues for explicitly: over-report (one bogus row in a collapsed,
    never-urgent section) rather than under-report (a real family silently
    missing).

    Both rules bind the PIN validator too — `extract_panel_models` and
    `extract_judge_model` call this: a shape the catalog parser would never emit
    must not be accepted as a pin, or that pin reads as delisted every run. If a
    lab ever ships an id this rule rejects (a bare digit-less word), relax it
    HERE — one function, both sides — and
    `test_pin_and_catalog_id_shape_rules_agree` keeps them honest.
    """
    return (
        bool(_ID_TOKEN.match(token))
        and re.search(r"[-.0-9]", token) is not None
        and re.search(r"[a-z]", token) is not None
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
            "(expected a single lowercase token carrying a `-`, `.`, or digit, e.g. "
            "`gpt-5.6-sol-max` or `o3`). If a lab has shipped an id of a new shape, relax "
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
                # …and then the SAME shape rule the catalog parser applies. The
                # scalar/whitespace checks above are strictly weaker than
                # `_is_model_id`, so without this the judge pin was the one hole
                # in the "both sides agree" contract: a default the catalog
                # parser would never emit (`sonnet` — a bare digit-less word)
                # extracted cleanly here and then failed `present()`, reporting
                # a phantom URGENT "delisted pin" every Monday against a model
                # that is sitting right there in the catalog.
                if value and value[0] not in ">|" and _is_model_id(value):
                    return value
                raise ExtractionError(
                    f"the `judge_model` input's default is not a bare model id: {value!r} "
                    "(expected a single lowercase token carrying a `-`, `.`, or digit, e.g. "
                    "`claude-opus-4-8-thinking-max`). Relax `_is_model_id` if a lab ships an id "
                    "of a new shape — it gates the catalog parser too, and the two must agree."
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


def family_of(model_id):
    """Split an id into `(family, tier)` at its reasoning/speed suffix.

    `gpt-5.6-terra-max-fast` -> `('gpt-5.6-terra', 'max-fast')`. An id carrying
    no suffix from `TIER_SUFFIXES` is its own family at the empty (default)
    tier — `gpt-5.3-codex` -> `('gpt-5.3-codex', '')` — which is what merges it
    with its `-low`/`-fast`/… siblings instead of stranding it as a family of
    one.

    Deliberately a *suffix* rule and not a table: like `lab_of`, it stays
    zero-maintenance when a lab ships a new family name. The cost is that a
    family whose name happens to end in a tier word would be split — no such id
    exists in Cursor's catalog, and the failure mode is a cosmetic extra row in
    a review-me list, not a missed finding.
    """
    match = _TIER_SUFFIX_RE.match(model_id)
    if not match:
        return model_id, ""
    return match.group("family"), match.group("tier")


def _family_version(family):
    """Sort key for a family — every digit run in it, in order.

    `gpt-5.6-terra` -> `(5, 6)`; `claude-opus-4-8-thinking` -> `(4, 8)`;
    `kimi-k3` -> `(3,)`; `code-supernova` -> `()`, which sorts oldest. Purely
    positional on purpose: nothing in a bare id says which number is the version,
    and the labs write it at least three ways (`5.6`, `4-8`, `k3`), so anything
    smarter would be a per-lab table — the maintenance burden this checker
    avoids everywhere else. Sorting on it descending is what makes the
    MAX_FAMILY_IDS cut drop the OLDEST families rather than whatever Cursor
    happened to print last.
    """
    return tuple(int(token) for token in _VERSION_TOKEN.findall(family))


def _tier_strength(tier):
    """Rank a tier for picking a collapsed row's representative — see `_TIER_STRENGTH`."""
    base, fast = tier, False
    # Split the SPEED half off the END, not at the first `-`: a tier can carry a
    # hyphen itself (`extra-high`), and splitting at the first one reads its base
    # as `extra`, which is in no ranking and would demote the family's top tier.
    if base == SPEED_SUFFIX:  # a bare `-fast` id is the default tier, run fast
        base, fast = "", True
    elif base.endswith("-" + SPEED_SUFFIX):
        base, fast = base[: -len(SPEED_SUFFIX) - 1], True
    strength = _TIER_STRENGTH.index(base) if base in _TIER_STRENGTH else 0
    # Prefer the non-`fast` twin, so the note reads as the family's plain top tier.
    return (strength, 0 if fast else 1)


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
    # Grouped through a dict keyed by lab (not a linear scan per entry) so this
    # stays O(N) on a catalog whose ids all have distinct prefixes.
    unpinned_labs = []
    by_lab = {}
    for model_id, note in entries:
        lab = lab_of(model_id)
        if lab in pinned_labs:
            continue
        group = by_lab.get(lab)
        if group is None:
            group = by_lab[lab] = {"lab": lab, "candidates": []}
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
    CommonMark) and padded when the note starts or ends with a backtick. The
    note is also capped at MAX_NOTE_CHARS — it is repeated across dozens of
    rows, and unbounded it could eat the whole body budget by itself — with the
    cap applied BEFORE the delimiter is sized, so it sizes what is emitted.
    """
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) > MAX_NOTE_CHARS:
        flat = flat[:MAX_NOTE_CHARS].rstrip("`") + " …"
    longest = max((len(m) for m in re.findall(r"`+", flat)), default=0)
    delim = "`" * (longest + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{delim}{pad}{flat}{pad}{delim}"


def _candidate_list(candidates, limit=None):
    """Bullet list of `{id, note}` rows, one per id — the unpinned-families fold.

    `limit` caps the rows and names the remainder instead of dropping it
    silently; the caller passes one because the ids per group come from the
    catalog. The same-lab review-me list uses `_collapsed_candidate_list`
    instead — the fold is a short watchlist scanned whole, so it keeps one row
    per id in catalog order.
    """
    shown = candidates if limit is None else candidates[:limit]
    rows = [
        f"- `{c['id']}`" + (f" — {_inline_code(c['note'])}" if c["note"] else "") for c in shown
    ]
    hidden = len(candidates) - len(shown)
    if hidden:
        rows.append(f"- _… and {hidden} more — see the raw catalog fold below._")
    return "\n".join(rows)


def collapse_tiers(candidates):
    """Group `{id, note}` rows into one group per family, newest family first.

    Returns `[{family, members, _order}]` where `members` are the candidate rows
    of that family in catalog order, each carrying its `tier`, and `_order` is
    the family's first appearance in the catalog. Ordering is `_family_version`
    descending, ties broken by that catalog order — the "highest-signal rows
    first" the row caps assume.
    """
    groups = []
    by_family = {}
    for index, candidate in enumerate(candidates):
        family, tier = family_of(candidate["id"])
        group = by_family.get(family)
        if group is None:
            group = by_family[family] = {"family": family, "members": [], "_order": index}
            groups.append(group)
        group["members"].append(dict(candidate, tier=tier))
    # Explicit `-_order` rather than leaning on sort stability under `reverse`:
    # the tie-break IS the contract here (catalog order), so it is written down.
    groups.sort(key=lambda g: (_family_version(g["family"]), -g["_order"]), reverse=True)
    return groups


def _collapsed_candidate_list(candidates, pinned=(), limit=None):
    """`_candidate_list`, but one row per FAMILY — the same-lab review-me list.

    The panel pins one tier per family, so a per-id list reports every other
    tier of every pinned family as a candidate, forever (BE-6911: 178 rows, with
    both new families below the cut). One row per family names the family, how
    many tiers the catalog offers and which, and reproduces ONE member's catalog
    note verbatim — the strongest tier's, since that is the plausible promotion
    target. Per-id notes are not interpreted or merged: a family whose members
    carry differing notes (a NO-ZDR marker on some tiers only) shows the
    representative's, and the raw catalog fold below remains the full record.

    `pinned` are the lab's pinned ids, used only to mark a family the panel
    already pins — the rows that are noise by construction.
    """
    groups = collapse_tiers(candidates)
    shown = groups if limit is None else groups[:limit]
    pinned_by_family = {}
    for pin in pinned:
        pinned_by_family.setdefault(family_of(pin)[0], []).append(pin)

    rows = []
    for group in shown:
        members = group["members"]
        # `max` keeps the FIRST maximal member, i.e. catalog order breaks ties.
        top = max(members, key=lambda m: _tier_strength(m["tier"]))
        note = f" — {_inline_code(top['note'])}" if top["note"] else ""
        already = pinned_by_family.get(group["family"]) or []
        pin_note = (
            " · _panel already pins " + ", ".join(f"`{p}`" for p in already) + "_" if already else ""
        )
        if len(members) == 1:
            rows.append(f"- `{top['id']}`{note}{pin_note}")
            continue
        # Strongest tier first, so MAX_TIERS_PER_ROW drops the weakest rather
        # than whichever ones Cursor happened to print last.
        ranked = sorted(members, key=lambda m: _tier_strength(m["tier"]), reverse=True)
        tiers = [f"`{m['tier']}`" if m["tier"] else "_default_" for m in ranked]
        if len(tiers) > MAX_TIERS_PER_ROW:
            extra = len(tiers) - MAX_TIERS_PER_ROW
            tiers = tiers[:MAX_TIERS_PER_ROW] + [f"+{extra} more"]
        rows.append(
            f"- `{group['family']}-*` — {len(members)} tiers ({', '.join(tiers)}); "
            f"top `{top['id']}`{note}{pin_note}"
        )

    hidden = len(groups) - len(shown)
    if hidden:
        hidden_ids = sum(len(g["members"]) for g in groups[len(shown) :])
        rows.append(
            f"- _… and {hidden} older famil{'y' if hidden == 1 else 'ies'} "
            f"({hidden_ids} id{'s' if hidden_ids != 1 else ''}) — see the raw catalog fold below._"
        )
    return "\n".join(rows)


def _footer(report):
    """The trailing note: what THIS issue asks of the reader, in its own words.

    Three arms, because the report already sorts itself into three states and a
    single sentence cannot describe all of them honestly (BE-6912):

      * `urgent` — a delisted pin or a pin marked NO-ZDR. Someone must edit
        `cursor-review.yml` now; the run is red.
      * findings, not urgent — the standing review-me list (and/or an audit-date
        reminder). Advisory. This is the steady state, so the footer has to say
        out loud that a permanently-open issue here is not neglect: `unpinned`
        counts toward `has_findings`, and any real catalog lists more reasoning
        tiers per pinned lab than the panel pins one of, so the close arm in
        `cursor-review-catalog-drift.yml` is effectively unreachable. Measured
        on the 2026-08-10 catalog (the `tests/fixtures` capture, from run
        31363137595 / issue #144): 192 catalog ids, 178 of them unpinned
        same-lab, 3 unpinned families — no delisted pin, no NO-ZDR pin, run
        green, issue open ever since.
      * no findings — the only state that closes the issue, and the ONLY arm
        that may promise a close. The footer used to promise it unconditionally,
        which told every reader of a permanently-open advisory issue that it
        should have closed weeks ago — i.e. that it was being ignored — on the
        one issue that is also where a genuinely urgent pin failure is reported.
    """
    lead = "_Filed by the weekly `cursor-review-catalog-drift` check."
    # Whether an advisory list is what is holding the issue open. On any real
    # catalog it is (192 ids, 178 of them unpinned same-lab, on 2026-08-10), but
    # a report whose only finding is a stale audit date closes as soon as the
    # date is refreshed — claiming "open indefinitely" there would be this
    # footer's own bug in miniature.
    standing = bool(report["unpinned"] or report.get("unpinned_labs"))
    if report["urgent"]:
        closing = (
            "and it will **not** close itself once you repin — the standing review-me list below "
            "holds it open, as it does on any real catalog. The next run simply rewrites this "
            "issue without the 🚨 section."
            if standing
            else "and it closes itself on the first run that finds nothing at all to report."
        )
        return (
            f"{lead} **Act on this now:** a pinned model is delisted or marked NO-ZDR — see the 🚨 "
            "section(s) above and repin in `cursor-review.yml`. That is also why this check's run "
            f"is red. The issue is sticky: each run rewrites it in place, {closing}_"
        )
    if report["has_findings"]:
        closing = (
            "and it stays open indefinitely, because Cursor's catalog always lists more reasoning "
            "tiers than the panel pins one of. **An open issue here is not a sign anyone is "
            "ignoring it**"
            if standing
            else "and it closes itself on the first run that finds nothing at all to report"
        )
        return (
            f"{lead} **Nothing here is urgent:** no pinned model is delisted or marked NO-ZDR, so "
            "the review panel is working and this check's run is green. What is above is advisory — "
            "catalog ids for a human to review, and/or an audit-date reminder — to skim when "
            f"convenient. The issue is sticky: each run rewrites it in place, {closing} — when a "
            "pin actually breaks, the run goes red and this footer says **act on this now** "
            "instead._"
        )
    return (
        f"{lead} No drift at all — nothing to act on, and this is the one outcome that closes the "
        "issue. A later run files a fresh one if drift returns._"
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
            alts = item["same_lab_available"]
            if alts:
                same_lab = ", ".join(f"`{m}`" for m in alts[:MAX_FAMILY_IDS])
                if len(alts) > MAX_FAMILY_IDS:
                    same_lab += f", +{len(alts) - MAX_FAMILY_IDS} more"
            else:
                same_lab = "_(no same-lab id in the catalog)_"
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
            "Catalog ids from labs the panel already pins that are **not** pinned today, **one row "
            "per model family**, newest family first — ids that differ only by a reasoning/speed "
            "tier (`-none`/`-low`/`-medium`/`-high`/`-xhigh`/`-max`, each optionally `-fast`) share "
            "a row, since the panel pins one tier per family and the rest are not separate "
            "candidates. This is a **review-me list, not a recommendation**: picking the newest "
            "highest-reasoning *ZDR-eligible* model is a human call. Cursor only marks NO-ZDR "
            "models inline, so any marker on the catalog line is reproduced verbatim below — an id "
            "with no marker is **not** thereby confirmed ZDR-eligible; check the catalog before "
            "promoting one. A collapsed row shows the note of the tier named after `top` only, so "
            "consult the raw fold for the other tiers' lines before promoting."
        )
        for group in report["unpinned"]:
            pinned_now = ", ".join(f"`{m}`" for m in group["pinned"]) or "_none_"
            out.append(f"**`{group['lab']}`** (pinned: {pinned_now})")
            out.append(
                _collapsed_candidate_list(
                    group["candidates"], pinned=group["pinned"], limit=MAX_FAMILY_IDS
                )
            )

    families = report.get("unpinned_labs") or []
    if families:
        # Collapsed on purpose: most of this is labs the panel will never pin, so
        # it must not crowd out the same-lab review-me list above. It exists for
        # the one row that matters — a lab already on the panel shipping under a
        # new family prefix, which `lab_of` cannot tell from a new vendor.
        # Budgeted HERE rather than left to `MAX_BODY_CHARS` below. That clamp is
        # a blunt `body[:N]`: on a catalog with thousands of distinct prefixes it
        # would cut INTO this block, leaving the `<details>`/inline-code markup
        # unterminated (which swallows the rest of the issue in GitHub's
        # renderer) and dropping the stale-audit and raw-catalog sections that
        # follow. Truncating with an explicit count keeps the body well-formed
        # and says out loud what was dropped. Budgeted in CHARS as well as rows:
        # the row caps bound how many rows render, but 40 labs × 25 capped-note
        # rows still overruns the whole body budget, so groups stop when the
        # fold's char budget is spent (the first group always renders).
        shown = []
        used = 0
        for group in families:
            if len(shown) >= MAX_FAMILY_LABS:
                break
            block = "\n**`{lab}`**\n\n{rows}".format(
                lab=group["lab"],
                rows=_candidate_list(group["candidates"], limit=MAX_FAMILY_IDS),
            )
            if shown and used + len(block) > MAX_FAMILY_FOLD_CHARS:
                break
            shown.append(block)
            used += len(block)
            if used > MAX_FAMILY_FOLD_CHARS:
                break
        hidden = len(families) - len(shown)
        labs_listed = ", ".join("`" + g["lab"] + "`" for g in families[: len(shown)])
        if hidden:
            labs_listed += f", +{hidden} more"
        detail = [
            "<details>",
            f"<summary>Catalog ids from <b>unpinned model families</b> "
            f"({labs_listed})</summary>",
            "",
            "Families the panel pins **nothing** from. Usually just labs Comfy does not use — but "
            "the lab of an id is its first `-`/`.`-separated token, so a lab the panel DOES pin "
            "shipping under a new family prefix (OpenAI's `o<n>` series alongside `gpt-*` is the "
            "precedent) lands here rather than in the review-me list above. Scan for a familiar lab "
            "wearing an unfamiliar prefix; ignore the rest. Same caveats as above — notes are "
            "verbatim, an unmarked id is **not** thereby confirmed ZDR-eligible.",
        ]
        detail.extend(shown)
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

    footer = _footer(report)
    # The raw fold gets whatever body budget the report has NOT used, up to the
    # MAX_CATALOG_CHARS ceiling. A fixed 40K assumed the report stayed inside
    # ~20K; a large catalog can exceed that even with every list capped, and the
    # blunt `body[:N]` clamp below would then slice mid-fold — unterminated
    # markup that swallows the footer in GitHub's renderer.
    catalog = catalog_text.rstrip("\n")
    remaining = MAX_BODY_CHARS - sum(len(section) + 2 for section in out) - len(footer)
    budget = min(MAX_CATALOG_CHARS, remaining - _FOLD_OVERHEAD)
    if budget < 500:
        # Not enough room left for a useful excerpt — say so instead of folding
        # a fragment (or overshooting into the clamp).
        out.append(
            "_Raw `cursor-agent models` output omitted — the report above used the body "
            "budget; see the workflow run log for the full catalog._"
        )
    else:
        if len(catalog) > budget:
            catalog = catalog[:budget] + "\n… truncated — see the workflow run log for the full catalog."
        out.append(
            "<details>\n<summary>Raw <code>cursor-agent models</code> output</summary>\n\n"
            + _fenced(catalog)
            + "\n</details>"
        )
    out.append(footer)
    body = "\n\n".join(out) + "\n"
    if len(body) > MAX_BODY_CHARS:
        # Last-resort clamp — every section above is bounded, so reaching this
        # takes pathological ids, but GitHub rejects an oversized body outright
        # (422) and a failed issue write would lose the whole report. A
        # truncated report still names the delisted pins, which lead the body.
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
