#!/usr/bin/env python3
"""Table-driven declared-vs-documented input drift check, for ALL 16 reusables.

Two hand-rolled copies of this check used to exist — one for `cursor-review`
(`.github/cursor-review/tests/test_workflow_inputs_docs.py`) and one for
`refresh-reviewers` (`.github/refresh-reviewers/tests/test_inputs_docs.py`).
Both were written for the same failure, BE-4691: `cursor-review.yml`'s
`blocking:` input was deleted in #31 while its documentation lived on in three
places for weeks, and GitHub rejects an unknown `workflow_call` input at startup
with a zero-job `startup_failure` and no logs. So a phantom input in a caller
guide is a broken caller for whoever copies it. The other 14 reusables had no
such pin at all; a third hand-rolled copy per workflow is not the answer.

This file is the generalisation, and now the ONLY parser for this assertion in
the repo: one `ROWS` table, one set of parsers (ported from the refresh-reviewers
copy, CRLF handling and blank/comment tolerance included), one generated
`TestCase` per reusable so `-v` names the workflow that drifted. The two
originals are deleted; everything they asserted is carried by their rows'
`dir_readme` / `readme_mode` / `check_defaults` /
`expect_sentinel_in_examples` columns. It does NOT fix any docs drift.

Some files it reads live outside the `paths:` globs `test-workflow-pins.yml`
already covers (`.github/workflows/**`, `.github/workflow-pins/**`,
`docs/callers/**`): the two directory READMEs, and the repo-root `README.md`
scanned by `SharedDocExampleCallersTest`. Each is listed there as its own
explicit `paths:` entry, so an edit to it has to run this harness — the #31
scenario exactly. `test_files_read_outside_the_globs_are_in_this_suites_ci_path_filters`
derives that set from this file and checks `pull_request` and `push`
separately, so the pairing cannot rot.

Direction-by-direction, what is asserted and why the strictness differs:

* **Phantom** (documented but not declared) — STRICT, no allowlist, because
  every phantom is a copy-paste caller that fails at startup.
* **Undocumented** (declared but not documented) — a knob nobody can discover.
  Real, but quieter, and 13 of them already exist. They are pinned in
  `KNOWN_UNDOCUMENTED` below, modelled on `KNOWN_EXEMPT` in
  `check_workflow_pins.py`: an entry is a KNOWN debt, not a blessing, and a
  STALE entry FAILS, so the list drains itself as the guides get filled in.
* **Example `with:` keys** — the same phantom failure one level in, in the
  copy-paste callers this repo ships: every fence in the guide, the workflow's
  header comment, and the two shared catalogs (`README.md`,
  `docs/callers/README.md`) that belong to no single reusable. Which `with:`
  counts for which reusable is decided by the `uses:` governing it, not by the
  heading above it. Subset, not equality: an example legitimately shows only a
  few inputs.

Parsed WITHOUT PyYAML on purpose: this repo is stdlib-only (`AGENTS.md`) and
`test-workflow-pins.yml` installs no requirements. The workflows are uniformly
2-space indented with every input key alone on its 6-space line, which is all
these scanners need. Every scanner is also guarded against going quiet and
passing vacuously, since each assertion compares two scanner outputs and {} vs
{} is a pass: the two NAME scanners must both find the row's `sentinel` input,
and each example `with:` scanner must find at least one BLOCK with at least one
key in it (non-empty rather than sentinel-bearing — an example caller shows only
the knobs it needs; per block rather than over their union, because a union goes
non-empty as soon as any one block carries keys).

Run: python3 -m unittest discover -s .github/workflow-pins/tests -p 'test_*.py' -v
"""

import collections
import glob
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
WORKFLOWS_DIR = os.path.join(REPO_ROOT, ".github", "workflows")
GUIDES_DIR = os.path.join(REPO_ROOT, "docs", "callers")

# The line that makes a workflow file REUSABLE: `workflow_call:` as a top-level
# key of `on:`, at exactly 2-space indent. ANCHORED, not a substring match, so
# the files that merely MENTION `workflow_call` — in prose, in a heredoc
# fixture, in a comment — are not mistaken for reusables and do not demand a
# ROWS entry.
#
# Tolerant of three spellings that all still declare the trigger, because a
# workflow this regex misses is absent from BOTH `on_disk` and ROWS and so
# ships with no drift coverage at all while
# `test_rows_match_the_reusable_workflows_on_disk` stays green: an empty flow
# mapping (`workflow_call: {}`), trailing whitespace, and a trailing comment.
WORKFLOW_CALL_LINE = "  workflow_call:"
WORKFLOW_CALL_RE = re.compile(r"^  workflow_call:[ \t]*(\{[ \t]*\})?[ \t]*(#.*)?$")

# An input declaration: the key alone on its 6-space line, directly under
# `    inputs:`. Sub-keys of an input (description/type/default) are 8-space,
# and folded description text deeper still, so none of them match.
INPUT_KEY = re.compile(r"^      ([A-Za-z0-9_-]+):\s*$")
# `on:`'s trigger keys sit at 2 spaces, `workflow_call:`'s own keys (`inputs:`,
# `secrets:`, `outputs:`) at 4, an input key at 6 and its sub-keys at 8. Every
# block bound below is expressed against these rather than as "exactly four
# spaces" — see workflow_call_input_lines for why that distinction is
# load-bearing.
TRIGGER_INDENT = 2
INPUTS_LINE = "    inputs:"
INPUT_KEY_INDENT = 6
INPUT_SUBKEY_INDENT = 8
# Section bounds for the Inputs/knob TABLES: `## ` ONLY, never `### `, so a
# `###` sub-section is scanned as part of its parent and a table under one
# contributes names too. Exactly one exists today — `### Choosing
# \`approval-mode\`` under `docs/callers/detect-unreviewed-merge.md`'s
# `## Inputs`. Widening toward MORE documented names can only ever add a
# phantom failure, never hide one, so it fails safe.
#
# The example `with:` scan is NOT section-bounded at all — it anchors on
# `uses:` and reads every fence in the doc; see USES_ANY / reusable_with_blocks.
HEADING = re.compile(r"^#{2}\s")
# An input's `default:` / `required:` sub-keys — 8-space, one level under the
# 6-space input key — used by the opt-in Default-column comparison.
INPUT_DEFAULT = re.compile(r"^        default:\s*(.*?)\s*$")
INPUT_REQUIRED = re.compile(r"^        required:\s*(\S+)\s*$")
# A `with:` mapping in an example caller (at any indent), and a mapping key
# directly under it. A placeholder scalar VALUE (`<same-sha>`, a login) never
# starts a line, so MAPPING_KEY never mistakes one for an input.
WITH_LINE = re.compile(r"^(\s*)with:\s*$")
MAPPING_KEY = re.compile(r"^\s*([A-Za-z0-9_-]+):")
# The `uses:` that GOVERNS a `with:` — the two are siblings in the same mapping.
# Both the reusable-job form (`      uses: …`) and the step form
# (`      - uses: …`) are matched, and group(1)'s length is the effective key
# indent in each, so a step's `with:` is attributed to the step's action rather
# than to the job's reusable.
USES_ANY = re.compile(r"^(\s*(?:-\s+)?)uses:\s*(\S+)")
# ... and the subset of those values that name a reusable OF THIS REPO.
REUSABLE_USES = re.compile(
    r"^Comfy-Org/github-workflows/\.github/workflows/([A-Za-z0-9_-]+)\.yml(?:@|$)"
)
# A `default:` whose value is a block scalar: the VALUE is the indented lines
# that follow, not this indicator.
BLOCK_SCALAR_INDICATOR = re.compile(r"^[|>][+-]?$")
# Markdown link wrapping around a table cell's input name.
CELL_LINK = re.compile(r"^\[(.*)\]\([^)]*\)$")


Row = collections.namedtuple(
    "Row",
    (
        "workflow",  # basename without .yml, e.g. "cursor-review"
        "sentinel",  # input that MUST appear on both sides (anti-vacuity guard)
        "guide",  # None -> docs/callers/<workflow>.md
        "inputs_heading",  # the guide heading whose table lists the inputs
        "expect_guide_with",  # a `with:` block is expected in the guide's fences
        "expect_header_with",  # ... and in the workflow's header comment
        "dir_readme",  # None -> no directory-README knob table to police
        "readme_heading",
        "readme_mode",  # "one-way" (phantom only) | "two-way" (set equality)
        "check_defaults",  # compare the guide's Default column to the workflow
        # Require `sentinel` in EVERY example `with:` block, not merely a
        # non-empty one. OFF by default: most examples legitimately show only
        # the knobs they need (groom's header example passes no ref at all,
        # `stale`'s passes only `dry_run`), so demanding it everywhere would be
        # permanently red.
        "expect_sentinel_in_examples",
    ),
    defaults=(
        None,  # guide
        "## Inputs",  # inputs_heading
        True,  # expect_guide_with
        True,  # expect_header_with
        None,  # dir_readme
        None,  # readme_heading
        "one-way",  # readme_mode
        False,  # check_defaults
        False,  # expect_sentinel_in_examples
    ),
)

# One row per reusable. `sentinel` is the anti-vacuity guard: an input the
# workflow really declares AND the guide's Inputs table really names, so an
# empty-vs-empty comparison can never pass silently. `workflows_ref` is the
# natural choice for the 11 that declare it; the other five name a knob of
# their own.
#
# `dir_readme` / `readme_heading` / `readme_mode` / `check_defaults` are OFF on
# every row but two: only `cursor-review` and `refresh-reviewers` ship a
# directory README with a knob table, and theirs are the pins the two deleted
# hand-rolled suites used to own. The two rows are NOT symmetric, and each
# asymmetry is load-bearing — see the comments on them below.
ROWS = (
    Row(workflow="agents-md-integrity", sentinel="workflows_ref"),
    # The only reusable here that declares no `workflows_ref` (it loads no
    # script from this repo), hence the `skip-bots` sentinel. Neither example
    # carries a `with:` at all — both show the bare `uses:` — so demanding a
    # non-empty one would be permanently red.
    Row(
        workflow="assign-prs-to-author",
        sentinel="skip-bots",
        expect_guide_with=False,
        expect_header_with=False,
    ),
    Row(workflow="assign-reviewers", sentinel="reviewer_config_path"),
    Row(workflow="coderabbit-config-validate", sentinel="workflows_ref"),
    Row(workflow="cursor-review-auto-label", sentinel="review_label"),
    # `## Configuration knobs` in the panel README is checked TWO-WAY, which is
    # what the deleted hand-rolled suite asserted: set equality across all three
    # sources (workflow, guide, panel README). `check_defaults` stays OFF —
    # `extra_generated_globs`' default is a folded `>-` scalar
    # (cursor-review.yml) rendered with `<br>` in the guide's Default cell, and
    # the canonicaliser does not equate the two; turning it on would redden a
    # correct doc.
    Row(
        workflow="cursor-review",
        sentinel="workflows_ref",
        dir_readme=".github/cursor-review/README.md",
        readme_heading="## Configuration knobs",
        readme_mode="two-way",
    ),
    Row(workflow="detect-unreviewed-merge", sentinel="approval-mode"),
    Row(workflow="groom", sentinel="workflows_ref"),
    # linear-ticket's header comment carries no `with:` example — it documents
    # the two caller FILES in prose and leaves the fences to the guide.
    Row(
        workflow="linear-ticket",
        sentinel="workflows_ref",
        expect_header_with=False,
    ),
    Row(
        workflow="pr-area-label",
        sentinel="workflows_ref",
        expect_header_with=False,
    ),
    Row(
        workflow="pr-derisk",
        sentinel="workflows_ref",
        inputs_heading="## Inputs worth setting",
    ),
    Row(workflow="pr-risk", sentinel="workflows_ref"),
    Row(workflow="pr-size", sentinel="workflows_ref"),
    Row(workflow="public-repo-hygiene", sentinel="workflows_ref"),
    # Mirror image of the cursor-review row. `## Knob defaults (and why)` is
    # intentionally PARTIAL — it omits `reviewer_config_path`, `map_exclude`,
    # `extra_exclude_paths` and `workflows_ref` — so it is one-way (phantom
    # only); set equality there would be permanently red. `check_defaults` is ON:
    # this guide's Default column is the only one that currently matches the
    # workflow under the canonicaliser, and the deleted suite pinned it.
    Row(
        workflow="refresh-reviewers",
        sentinel="workflows_ref",
        dir_readme=".github/refresh-reviewers/README.md",
        readme_heading="## Knob defaults (and why)",
        readme_mode="one-way",
        check_defaults=True,
        # Restores the deleted suite's `assertIn("workflows_ref", keys)` on both
        # shipped example callers. The generalized harness only requires a
        # non-empty `with:`, under which a later edit dropping `workflows_ref:`
        # from either copy-paste caller would land green — and whoever copied it
        # would run with `workflows_ref` as `''`, the BE-5546 hole.
        expect_sentinel_in_examples=True,
    ),
    Row(workflow="stale", sentinel="slack_channel"),
)

# Inputs a workflow declares that its caller guide's Inputs table does not name
# — the 13 that exist as of this file's first green run. Modelled on
# `KNOWN_EXEMPT` in `check_workflow_pins.py`: an entry is a KNOWN debt, not a
# blessing. `test_known_undocumented_is_not_stale` FAILS on a stale entry — one
# whose input the workflow no longer declares (renamed/deleted) or whose guide
# now DOES document it (fixed) — so the list drains itself instead of rotting,
# and a name left here after the fix would silently pre-exempt the next drift
# under that name. Documenting one of these is a two-line change: add the table
# row, delete the name here.
#
# There is deliberately NO allowlist for the phantom direction.
KNOWN_UNDOCUMENTED = {
    "agents-md-integrity": {"exclude_paths"},
    "groom": {"bail_sink", "config", "path", "sink"},
    "pr-derisk": {"bot_logins", "fleet_logins"},
    "pr-risk": {
        "check_name",
        "check_run",
        "enabled",
        "pr_number",
        "pr_numbers",
        "sticky_comment",
    },
}


def workflow_path(row):
    return os.path.join(WORKFLOWS_DIR, row.workflow + ".yml")


def guide_path(row):
    """The row's caller guide, defaulting to docs/callers/<workflow>.md.

    A row-supplied `guide` is resolved against the repo root, not the cwd, so it
    matches `dir_readme` and the suite still works from any directory (CI runs
    `unittest discover` from the repo root; a developer may not).
    """
    if row.guide:
        return os.path.join(REPO_ROOT, row.guide)
    return os.path.join(GUIDES_DIR, row.workflow + ".md")


def rel(path):
    """Repo-relative path, for readable assertion messages."""
    return os.path.relpath(path, REPO_ROOT)


def read_lines(path):
    # Tolerate CRLF: split on \n and drop a trailing \r so indent-anchored
    # matches (dedent break, INPUT_KEY) and `lines.index("jobs:")` are not
    # thrown off by a stray carriage return.
    with open(path, encoding="utf-8") as f:
        return [line.rstrip("\r") for line in f.read().split("\n")]


def workflow_head(path):
    """Lines before the top-level `jobs:` key.

    Bounds every workflow-side scan, so a 6-space key inside some job's step
    mapping can never register as an input and a `#` line inside a job's `run:`
    can never register as header-comment text.
    """
    lines = read_lines(path)
    if "jobs:" not in lines:
        raise AssertionError(
            "no top-level `jobs:` line in %s — file moved or its structure "
            "changed; the input scan cannot be bounded" % rel(path)
        )
    return lines[: lines.index("jobs:")]


def section_lines(lines, heading):
    """Lines strictly under `heading` (a `## …` line), up to the next `## `.

    `###` sub-headings are part of the section, not a bound (see HEADING).

    Fence-aware: a `## …` line INSIDE a ``` block is example content, not a
    heading, so it must not end the section. No guide writes one today, but the
    example fences are full of YAML `#` comments and one gaining a second `#`
    would otherwise truncate the section silently — turning a real check into a
    vacuous one rather than into a failure. The fence delimiters are kept in the
    output, so `fenced_blocks` still sees every block.
    """
    out, in_section, in_fence = [], False, False
    for line in lines:
        if not in_fence and line.strip() == heading:
            in_section = True
            continue
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif in_section and not in_fence and HEADING.match(line):
            break
        if in_section:
            out.append(line)
    return out


def split_cells(line):
    """Split a markdown table row into trimmed cells, honoring GFM's escaped
    `\\|` (a literal pipe inside a cell) so a Default value that contains a pipe
    — plausible for a regex-valued input — doesn't shift every later column and
    make the Default comparison run against the wrong cell."""
    body = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", body)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def workflow_call_input_lines(path):
    """Lines of the `on.workflow_call.inputs` mapping, in file order.

    Two bounds, both expressed as "indented less than X" rather than as "exactly
    X spaces":

    * `workflow_call:` scoping. A reusable may ALSO declare
      `workflow_dispatch:` — a sibling trigger at the same 2-space indent, with
      an `inputs:` mapping of its own at the same 4-space indent. Keying off a
      bare `    inputs:` line would merge that second trigger's 6-space keys
      into the declared set, inflating exactly the set the STRICT phantom check
      subtracts and masking the BE-4691 drift this file targets. Whichever
      trigger is declared first, only `workflow_call:`'s keys are collected.
    * The `inputs:` bound. An input key sits at 6 spaces and its sub-keys
      deeper, so ANY content line shallower than that closes the mapping.
      Matching `^    \\S` (exactly four spaces) — the obvious spelling, and what
      this scanner used to do — catches only the 4-space siblings (`secrets:`,
      `outputs:`) and lets a 2-space `  workflow_dispatch:` or a column-0
      `permissions:` through with the scan still latched open.

    Blank and comment lines are KEPT rather than filtered: a `#` line indented
    inside a folded `default:` is literal text, not a comment, and only
    workflow_input_defaults has the context to tell those apart. Neither ever
    ends a block here — neither carries a meaningful indentation signal.
    """
    out, in_call, in_inputs = [], False, False
    for line in workflow_head(path):
        if not line.strip() or line.lstrip().startswith("#"):
            if in_inputs:
                out.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if line == WORKFLOW_CALL_LINE or WORKFLOW_CALL_RE.match(line):
            in_call, in_inputs = True, False
            continue
        if in_call and indent <= TRIGGER_INDENT:
            # A sibling trigger (`  workflow_dispatch:`) or a top-level key
            # (`permissions:`). Not a `break`: `workflow_call:` may be declared
            # AFTER the trigger we are leaving.
            in_call = in_inputs = False
            continue
        if not in_call:
            continue
        if line == INPUTS_LINE:
            in_inputs = True
            continue
        if in_inputs and indent < INPUT_KEY_INDENT:  # secrets:, outputs:, ...
            in_inputs = False
            continue
        if in_inputs:
            out.append(line)
    return out


def workflow_inputs(path):
    """Input names declared under on.workflow_call.inputs."""
    names = set()
    for line in workflow_call_input_lines(path):
        match = INPUT_KEY.match(line)
        if match:
            names.add(match.group(1))
    return names


def strip_cell_markup(fragment):
    """Drop markdown emphasis and link wrapping from a table-cell fragment.

    A cell written ``**`foo`**``, ``__`foo`__`` or ``[`foo`](#foo)`` documents a
    copyable input every bit as plainly as ``` `foo` ```, but a bare backtick
    fullmatch sees none of them — the BE-4691 `blocking:` row would have escaped
    the direction this file calls STRICT simply by being bold. Unwrapping first
    still keeps PROSE cells out: `**Note**` unwraps to `Note`, which carries no
    backticks and so still contributes no name.
    """
    text = fragment.strip()
    for _ in range(4):  # `**[`foo`](#x)**` — emphasis wrapped around a link
        before = text
        link = CELL_LINK.match(text)
        if link:
            text = link.group(1).strip()
        for marker in ("**", "__", "*", "_"):
            if len(text) > 2 * len(marker) and text.startswith(marker) and text.endswith(marker):
                text = text[len(marker) : -len(marker)].strip()
                break
        if text == before:
            break
    return text


def cell_input_names(cell):
    """The input names a table row's FIRST cell documents, or None for a cell
    that documents none (the header row, the `---` separator, a prose row).

    A cell counts only when EVERY `/`-separated fragment is a lone backticked
    name once emphasis/link markup is stripped. Combined cells like
    `` `scope_label` / `scope_desc` `` are NOT a README-only shape — groom's
    guide documents `scope_label`/`scope_desc` on one row and pr-derisk's
    documents `repo_map_path`/`repo_runbooks_path` on one row — so both names
    are yielded and a lone-name regex would report four real, documented inputs
    as undocumented.

    Shared by BOTH table scanners on purpose. Reading one table with two
    different cell parsers made them disagree about what it documents: the
    Default scanner used a lone-name regex, so those same four names counted as
    documented yet contributed no Default entry and would be reported as
    missing the moment `check_defaults` is turned on.
    """
    parts = [strip_cell_markup(part) for part in cell.split("/")]
    if not parts or not all(parts):
        return None
    matched = [re.fullmatch(r"`([A-Za-z0-9_-]+)`", part) for part in parts]
    if not all(matched):
        return None
    return [match.group(1) for match in matched]


def documented_knob_names(path, heading):
    """Every input name documented by a table row under `heading`."""
    names = set()
    for line in section_lines(read_lines(path), heading):
        if not line.lstrip().startswith("|"):
            continue
        cells = split_cells(line)
        if not cells or not cells[0]:
            continue
        found = cell_input_names(cells[0])
        if found:
            names.update(found)
    return names


def strip_comment_prefix(line):
    """Drop the leading `# ` from a full-line YAML comment while preserving the
    example's own indentation, so relative-indent parsing still works. A bare
    `#` becomes empty."""
    if line.startswith("# "):
        return line[2:]
    if line == "#":
        return ""
    return line


def fenced_blocks(lines):
    """Content lines of every ``` fenced code block, one list per block."""
    blocks, current, inside = [], [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            if inside:
                blocks.append(current)
                current = []
            inside = not inside
            continue
        if inside:
            current.append(line)
    return blocks


def keys_under_with(lines, index):
    """Mapping keys directly under the `with:` at `lines[index]`.

    Relative-indentation only, no YAML parser: the first non-blank line after
    the `with:` fixes the child indent, and keys at exactly that indent are
    collected until the block dedents back to (or past) the `with:` line. So
    `uses:` / `secrets:`, which sit at the `with:` indent, end the block rather
    than count as inputs.
    """
    base = len(WITH_LINE.match(lines[index]).group(1))
    keys, child = set(), None
    for line in lines[index + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base:  # dedent to a sibling (secrets:, next job key)
            break
        if child is None:
            child = indent
        if indent == child:
            key = MAPPING_KEY.match(line)
            if key:
                keys.add(key.group(1))
    return keys


def governing_uses(lines, index):
    """The `uses:` value that owns the `with:` at `lines[index]`, or None.

    Walks BACKWARDS over the mapping the `with:` belongs to — its siblings at
    the same indent and their deeper values — and stops at the first line
    shallower than that indent, which is where the mapping began. None means
    the snippet shows a bare `with:` with no `uses:` above it.
    """
    base = len(WITH_LINE.match(lines[index]).group(1))
    for line in reversed(lines[:index]):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        owner = USES_ANY.match(line)
        if owner and len(owner.group(1)) == base:
            return owner.group(2)
        # Tested AFTER the `uses:` match, not before: in the step form
        # (`  - uses: actions/checkout@…`) the dash is part of the key's
        # indentation, so the line MEASURES two columns shallower than the
        # `with:` it governs. Breaking on the raw measurement first would call
        # every step-level `with:` an unowned fragment — and an unowned fragment
        # is attributed to the reusable, which is exactly the misattribution
        # this function exists to prevent.
        if len(line) - len(line.lstrip()) < base:
            return None
    return None


def reusable_with_blocks(lines, workflow, allow_orphan=True):
    """Key sets of the `with:` blocks that pass `workflow`'s inputs, one set per
    block, in file order.

    A `with:` counts when the `uses:` governing it names THIS repo's
    `workflow`. A `with:` governed by some OTHER `uses:` — an
    `actions/checkout`'s `ref:`/`fetch-depth:`, a sibling reusable's knobs in a
    multi-workflow doc — never counts: reading it here would register those keys
    as phantom inputs AND would satisfy the anti-vacuity guard while this
    reusable's own example silently lost its `with:`.

    `allow_orphan` covers a `with:` with no `uses:` above it at all. These docs
    really do ship such fragments — `cursor-review-auto-label.yml`'s header
    shows a bare `with:`/`secrets:` pair under "mint from a narrower App
    instead", and it is unambiguously that reusable's — so it is True when the
    lines come from a SINGLE-subject document (a row's own guide, a workflow's
    own header comment) and False for a shared doc, where an unattributable
    fragment would otherwise be charged against every reusable at once.

    Anchoring on `uses:` rather than on a `## Caller` heading is what lets this
    read EVERY fence in a doc. Heading-scoping missed every other
    copy-pasteable snippet a guide ships — `public-repo-hygiene.md`'s
    `## Tuning example`, groom's environment/builder/deny-list examples — and a
    phantom key in one of those is the same zero-job `startup_failure` as one in
    the `## Caller` fence.
    """
    blocks = []
    for index, line in enumerate(lines):
        if not WITH_LINE.match(line):
            continue
        owner = governing_uses(lines, index)
        if owner is None:
            if not allow_orphan:
                continue
        else:
            reusable = REUSABLE_USES.match(owner)
            if not reusable or reusable.group(1) != workflow:
                continue
        blocks.append(keys_under_with(lines, index))
    return blocks


def header_comment_lines(path):
    """The workflow's CONTIGUOUS leading `#` block, comment markers stripped.

    Bounded at the first non-comment line rather than filtered for `#` across
    the whole head. `cursor-review.yml`, `groom.yml` and `pr-size.yml` each
    carry a SECOND column-0 comment block further down (the preamble to `env:`
    or `concurrency:`), and collecting every `#` line splices those onto the
    header example with the YAML between them discarded — so a `with:` still
    open at the end of the header would absorb the spliced block's
    more-indented lines and report them as phantom inputs.
    """
    head = workflow_head(path)
    start = next((i for i, line in enumerate(head) if line.startswith("#")), None)
    if start is None:
        return []
    end = start
    while end < len(head) and head[end].startswith("#"):
        end += 1
    return [strip_comment_prefix(line) for line in head[start:end]]


def example_with_keys(row):
    """`with:` key sets from the two example callers a ROW owns, as
    {source_label: (list_of_per_block_key_sets, expected_non_empty)}.

    * Guide side — EVERY fenced block in the row's guide, anchored on `uses:`.
    * Workflow side — ONLY the contiguous header comment above `jobs:`.

    Each fenced block is scanned on its own, so a `with:` that ends one block
    cannot absorb the next block's more-indented lines. The per-BLOCK key sets
    are returned rather than their union so the anti-vacuity guard can be
    applied per block: a union goes non-empty as soon as ANY block carries keys,
    which is how a guard meant for the real caller example ends up satisfied by
    a different fence entirely.
    """
    guide_blocks = []
    for block in fenced_blocks(read_lines(guide_path(row))):
        guide_blocks.extend(reusable_with_blocks(block, row.workflow))
    return {
        "%s example callers" % rel(guide_path(row)): (
            guide_blocks,
            row.expect_guide_with,
        ),
        "%s header comment" % rel(workflow_path(row)): (
            reusable_with_blocks(
                header_comment_lines(workflow_path(row)), row.workflow
            ),
            row.expect_header_with,
        ),
    }


def _block_scalar_value(lines, start, indicator):
    """(folded value, index after the block) for the block scalar whose
    indicator sits on the preceding `default:` line.

    The body is every line indented deeper than the `default:` key; blank lines
    inside it belong to the scalar, and a `#` line inside it is literal text,
    not a comment. `>` folds to spaces, `|` keeps newlines; the chomping
    indicator only affects trailing newlines, which the caller strips anyway.
    """
    body, index = [], start
    while index < len(lines):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) <= INPUT_SUBKEY_INDENT:
            break
        body.append(line.strip())
        index += 1
    while body and not body[-1]:
        body.pop()
    joiner = " " if indicator.startswith(">") else "\n"
    return joiner.join(body).strip(), index


def workflow_input_defaults(path):
    """(`{name: raw default:}`, `{names marked required: true}`) from the
    workflow. Required inputs carry no default (`workflows_ref`)."""
    lines = workflow_call_input_lines(path)
    defaults, required, current = {}, set(), None
    index, total = 0, len(lines)
    while index < total:
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key = INPUT_KEY.match(line)
        if key:
            current = key.group(1)
            continue
        if current is None:
            continue
        default = INPUT_DEFAULT.match(line)
        if default:
            raw = default.group(1)
            if BLOCK_SCALAR_INDICATOR.match(raw):
                # The VALUE of a folded/literal default is the indented lines
                # BELOW it, not the `>-` on this line. Recording the indicator
                # would pit `'>-'` against the guide's correct prose the moment
                # check_defaults is turned on — and `groom.yml`'s `themes`,
                # `cursor-review.yml`'s `diff_excludes` and both of
                # `stale.yml`'s messages all use this shape.
                defaults[current], index = _block_scalar_value(lines, index, raw)
            else:
                defaults[current] = raw
            continue
        req = INPUT_REQUIRED.match(line)
        if req and req.group(1) == "true":
            required.add(current)
    return defaults, required


def documented_input_defaults(path, heading):
    """`{input name: raw Default-column cell}` from the table under `heading`.

    Reads cells with `cell_input_names`, the SAME parser the name scanner uses,
    so the two cannot disagree about what one table documents; a combined cell
    maps every name it carries to that row's one Default cell.
    """
    result = {}
    for line in section_lines(read_lines(path), heading):
        if not line.lstrip().startswith("|"):
            continue
        cells = split_cells(line)
        if len(cells) < 2:
            continue
        for name in cell_input_names(cells[0]) or ():
            # A second row for the same input would silently overwrite the
            # first, so two contradictory Default cells would pass as long as
            # the last one is right. Surface it — that's exactly the drift this
            # file exists to catch.
            if name in result:
                raise AssertionError(
                    "`%s` appears twice in the `%s` table of %s — a duplicate "
                    "row hides a contradictory Default cell from this "
                    "comparison" % (name, heading, rel(path))
                )
            result[name] = cells[1]
    return result


# Canonical form for the required-with-no-default input, shared by both sides
# of the Default-column comparison so `workflows_ref`'s `— (**required**)` cell
# and the workflow's `required: true` compare equal.
_REQUIRED = "\x00required-no-default"


def _clean_workflow_default(raw):
    """Canonicalize a raw `default:` value so cosmetic YAML — a trailing inline
    comment, surrounding quotes — doesn't turn this suite red against correct
    docs. Backticks are stripped from the guide side, so the two sides would
    otherwise compare a value against its raw YAML source."""
    text = raw.strip()
    # A quoted scalar ends at its closing quote; anything after that (including
    # a ` #` that would otherwise look like a comment marker) is a trailing
    # inline comment, not part of the value. Keep the `''` spelling both the
    # guide and this file use for the empty string.
    if text[:1] in ("'", '"'):
        # Tuple membership, NOT `text[:1] in "\"'"`: that is a SUBSTRING test and
        # `""` is a substring of every string, so a bare `default:` (legal YAML
        # null, captured as `''`) would enter this branch and `text[0]` would
        # raise IndexError instead of failing an assertion.
        quote = text[0]
        end = 1
        while end < len(text):
            if quote == '"' and text[end] == "\\":
                end += 2
                continue
            if quote == "'" and text[end : end + 2] == "''":
                end += 2
                continue
            if text[end] == quote:
                inner = text[1:end]
                return inner if inner else quote * 2
            end += 1
    # An unquoted inline comment (` # …`) is not part of the value.
    hash_at = text.find(" #")
    if hash_at != -1:
        text = text[:hash_at].rstrip()
    return text


def canonical_guide_default(cell):
    text = cell.strip()
    # Anchor on the required-cell SHAPE (`— (**required**)`), not a loose
    # `**required**` substring: a cell like `` `main` (**required**) `` advertises
    # a default the workflow deliberately lacks and must NOT canonicalize to
    # _REQUIRED (that drift is the whole point of this check), while a real
    # default annotated in prose must not false-match either.
    if text.startswith("—") and "**required**" in text:
        return _REQUIRED
    return text.strip("`").strip()


def canonical_workflow_default(name, defaults, required):
    if name in defaults:
        return _clean_workflow_default(defaults[name])
    if name in required:
        return _REQUIRED
    # Optional with no `default:` — a legal `workflow_call` shape that arrives at
    # runtime as `''`. Canonicalize to the empty string it actually delivers
    # rather than to None (which the guide side can never produce, permanently
    # reddening the subTest).
    return ""


# The `paths:`-filter globs in test-workflow-pins.yml that already cover whole
# trees. A file this suite READS which falls outside all of them needs its own
# literal entry, or an edit to it runs no job at all.
CI_PATH_GLOB_PREFIXES = (
    ".github/workflows/",
    ".github/workflow-pins/",
    "docs/callers/",
)
# Events whose `paths:` list must carry every such file. Both, so adding an
# entry to `pull_request` while forgetting `push` — or vice versa — still fails.
CI_FILTER_EVENTS = ("pull_request", "push")


def files_needing_their_own_ci_path_entry():
    """Repo-relative files this suite reads from outside CI_PATH_GLOB_PREFIXES.

    Derived from the suite itself — the rows' `dir_readme`s plus the shared
    catalogs `SharedDocExampleCallersTest` scans — rather than hard-coded, so
    adding a row that reads a new README extends the guard automatically instead
    of leaving it pinned to yesterday's list.
    """
    read = {row.dir_readme for row in ROWS if row.dir_readme}
    read |= {doc.replace(os.sep, "/") for doc in SharedDocExampleCallersTest.DOCS}
    return sorted(
        path
        for path in read
        if not path.startswith(CI_PATH_GLOB_PREFIXES)
    )


def event_paths_entries(path, event):
    """The literal `paths:` list entries under `on.<event>` in a workflow file.

    Indent-anchored rather than YAML-parsed (stdlib-only repo). The event's
    block is every line indented deeper than its own `  <event>:` key, and
    within it `paths:` owns the `- ` items indented deeper than itself — so a
    sibling key's list (`branches:`, or another event's `paths:`) is never
    counted as this event's. A trailing ` # comment` is stripped, and the
    surrounding quotes normalised away, so a correctly-configured filter whose
    entry carries an inline note still matches.
    """
    lines = read_lines(path)
    key = "  %s:" % event
    try:
        start = lines.index(key)
    except ValueError:
        return None
    entries, in_paths, paths_indent = [], False, None
    for line in lines[start + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= 2:
            break  # left the event's block
        stripped = line.strip()
        if in_paths:
            if indent <= paths_indent:
                in_paths = False
            elif stripped.startswith("- "):
                entries.append(_unquote_filter_entry(stripped[2:]))
                continue
        if stripped == "paths:":
            in_paths, paths_indent = True, indent
    return entries


def _unquote_filter_entry(value):
    """A `paths:` item's bare glob: inline comment stripped, quotes removed."""
    value = value.strip()
    if value[:1] in ("'", '"'):
        closing = value.find(value[0], 1)
        if closing != -1:
            return value[1:closing]
        return value[1:]
    # Unquoted: YAML ends the scalar at ` #`.
    comment = value.find(" #")
    if comment != -1:
        value = value[:comment]
    return value.strip()


def reusable_workflow_names():
    """Basenames of every workflow file that is itself reusable.

    Both extensions Actions accepts, and `WORKFLOW_CALL_RE` rather than exact
    membership of `WORKFLOW_CALL_LINE`: a workflow this scan misses is absent
    from BOTH `on_disk` and ROWS, so it ships with no drift coverage at all
    while `test_rows_match_the_reusable_workflows_on_disk` stays green — the
    `assertTrue(on_disk)` guard there proves some file matched, never that none
    were missed. Every reusable here is `.yml` today, and a `.yaml` one would
    fail `test_files_exist_for_every_row` (which resolves rows through
    `workflow_path`, `.yml`-only) with a clear message rather than go unnoticed.
    """
    names = set()
    for pattern in ("*.yml", "*.yaml"):
        for path in glob.glob(os.path.join(WORKFLOWS_DIR, pattern)):
            if any(WORKFLOW_CALL_RE.match(line) for line in read_lines(path)):
                names.add(os.path.splitext(os.path.basename(path))[0])
    return names


def _make_case(row):
    """One TestCase class per reusable, so `-v` names the workflow that drifted
    rather than burying it in a subTest label nobody reads until it fails."""

    class InputsDocsDriftCase(unittest.TestCase):
        maxDiff = None

        def setUp(self):
            self.row = row
            self.workflow = workflow_path(row)
            self.guide = guide_path(row)
            self.declared = workflow_inputs(self.workflow)
            self.documented = documented_knob_names(self.guide, row.inputs_heading)
            self.allowed = KNOWN_UNDOCUMENTED.get(row.workflow, frozenset())

        def test_scanners_are_not_vacuous(self):
            # Every set comparison below is between two scanner outputs, so a
            # scanner that silently stopped matching would compare {} with {}
            # and pass. The sentinel is an input both sides really carry.
            self.assertIn(
                row.sentinel,
                self.declared,
                "the %s input scanner lost `%s` — parser or file structure "
                "changed, every assertion for this workflow is now vacuous"
                % (rel(self.workflow), row.sentinel),
            )
            self.assertIn(
                row.sentinel,
                self.documented,
                "the `%s` table in %s lost `%s` — parser or table shape "
                "changed, every assertion for this workflow is now vacuous"
                % (row.inputs_heading, rel(self.guide), row.sentinel),
            )

        def test_every_documented_input_is_declared(self):
            # The #31 failure mode: docs outliving a deleted input. A caller who
            # copies a phantom input gets a zero-job startup_failure with no
            # logs. No allowlist here, ever.
            phantom = self.documented - self.declared
            self.assertFalse(
                phantom,
                "the `%s` table in %s documents inputs %s does not declare: %s "
                "— deleting an input is a docs change too"
                % (
                    row.inputs_heading,
                    rel(self.guide),
                    rel(self.workflow),
                    sorted(phantom),
                ),
            )

        def test_every_declared_input_is_documented_or_allowlisted(self):
            missing = self.declared - self.documented - self.allowed
            self.assertFalse(
                missing,
                "%s declares inputs missing from the `%s` table in %s: %s — "
                "document them, or add them to KNOWN_UNDOCUMENTED in %s with a "
                "reason"
                % (
                    rel(self.workflow),
                    row.inputs_heading,
                    rel(self.guide),
                    sorted(missing),
                    rel(os.path.abspath(__file__)),
                ),
            )

        def test_known_undocumented_is_not_stale(self):
            # Self-draining, same contract as KNOWN_EXEMPT in
            # check_workflow_pins.py: an entry survives only while it is still
            # TRUE. Either half going false means the debt is settled (or the
            # input is gone), and a name left behind would pre-exempt the next
            # drift that reuses it.
            for name in sorted(self.allowed):
                with self.subTest(allowlisted=name):
                    self.assertIn(
                        name,
                        self.declared,
                        "KNOWN_UNDOCUMENTED lists `%s` for %s but that workflow "
                        "no longer declares it (renamed, deleted, or moved) — "
                        "delete it from KNOWN_UNDOCUMENTED"
                        % (name, rel(self.workflow)),
                    )
                    self.assertNotIn(
                        name,
                        self.documented,
                        "KNOWN_UNDOCUMENTED lists `%s` for %s but the `%s` table "
                        "in %s now documents it — delete it from "
                        "KNOWN_UNDOCUMENTED"
                        % (
                            name,
                            rel(self.workflow),
                            row.inputs_heading,
                            rel(self.guide),
                        ),
                    )

        def test_example_with_blocks_are_declared_inputs(self):
            # The copy-paste callers pass inputs via `with:`. A phantom key
            # there is a zero-job startup_failure for whoever copies it — the
            # #31 failure, one level in from the Inputs table. Subset, not
            # equality: an example legitimately shows only a few inputs.
            for label, (blocks, expect_non_empty) in sorted(
                example_with_keys(row).items()
            ):
                with self.subTest(example=label):
                    if expect_non_empty:
                        # Guard: this example really does pass inputs, so a
                        # scanner gone quiet fails loudly instead of asserting
                        # {} ⊆ declared. Applied PER BLOCK, not to the union:
                        # a union goes non-empty as soon as any one block
                        # carries keys, so the guard would be satisfied by some
                        # other snippet while the real caller example silently
                        # lost its `with:`. NON-EMPTY, not "names the
                        # sentinel": an example shows only the knobs it needs,
                        # and several here legitimately omit theirs — groom's
                        # header example leans on `workflows_ref`'s `default:
                        # ''` carve-out and passes no ref at all, `stale`'s
                        # passes only `dry_run`. Where the row says no `with:`
                        # is expected at all, demanding one is permanently red.
                        self.assertTrue(
                            blocks,
                            "the %s scan found no `with:` block for `%s` at all "
                            "— the example moved, lost its `with:`, or the "
                            "`uses:` anchoring it was renamed; this subset "
                            "check is now vacuous" % (label, row.workflow),
                        )
                        for position, keys in enumerate(blocks, start=1):
                            self.assertTrue(
                                keys,
                                "`with:` block #%d of %d in %s carries no keys "
                                "— an empty example block makes this subset "
                                "check vacuous"
                                % (position, len(blocks), label),
                            )
                            if row.expect_sentinel_in_examples:
                                # Stronger than non-empty, for the rows whose
                                # deleted hand-rolled suite pinned the sentinel
                                # itself: a shipped caller that quietly loses
                                # `workflows_ref:` is a consumer running off a
                                # mutable ref, not just a thinner example.
                                self.assertIn(
                                    row.sentinel,
                                    keys,
                                    "`with:` block #%d of %d in %s does not "
                                    "pass `%s` — this example is pinned to show "
                                    "it, and a caller copied without it runs "
                                    "with `%s` as `''`"
                                    % (
                                        position,
                                        len(blocks),
                                        label,
                                        row.sentinel,
                                        row.sentinel,
                                    ),
                                )
                    undeclared = set().union(*blocks) - self.declared
                    self.assertFalse(
                        undeclared,
                        "the %s example passes `with:` inputs %s does not "
                        "declare: %s — a phantom input there is a zero-job "
                        "startup_failure for whoever copies it"
                        % (label, rel(self.workflow), sorted(undeclared)),
                    )

        def test_directory_readme_knob_table(self):
            if not row.dir_readme:
                self.skipTest(
                    "no directory README knob table for %s" % row.workflow
                )
            readme = os.path.join(REPO_ROOT, row.dir_readme)
            documented = documented_knob_names(readme, row.readme_heading)
            self.assertTrue(
                documented,
                "the `%s` scanner in %s found no knob rows — the heading was "
                "renamed or the table reshaped"
                % (row.readme_heading, rel(readme)),
            )
            phantom = documented - self.declared
            self.assertFalse(
                phantom,
                "the `%s` table in %s names knobs %s does not declare: %s"
                % (
                    row.readme_heading,
                    rel(readme),
                    rel(self.workflow),
                    sorted(phantom),
                ),
            )
            if row.readme_mode == "two-way":
                missing = self.declared - documented
                self.assertFalse(
                    missing,
                    "%s declares inputs missing from the `%s` table in %s: %s"
                    % (
                        rel(self.workflow),
                        row.readme_heading,
                        rel(readme),
                        sorted(missing),
                    ),
                )

        def test_guide_default_column_matches_workflow(self):
            if not row.check_defaults:
                self.skipTest(
                    "Default-column comparison is off for %s" % row.workflow
                )
            # Every check above is name-only, so the guide's Default column can
            # silently drift from the workflow's real `default:`. Pin it.
            defaults, required = workflow_input_defaults(self.workflow)
            guide = documented_input_defaults(self.guide, row.inputs_heading)
            self.assertTrue(
                defaults,
                "workflow_input_defaults found no `default:` lines in %s — "
                "parser or file structure changed; this check is now vacuous"
                % rel(self.workflow),
            )
            self.assertIn(
                row.sentinel,
                guide,
                "the `%s` Default scanner in %s lost `%s` — parser or table "
                "shape changed; this check is now vacuous"
                % (row.inputs_heading, rel(self.guide), row.sentinel),
            )
            # KNOWN_UNDOCUMENTED subtracted for the same reason
            # test_every_declared_input_is_documented_or_allowlisted subtracts
            # it: without that, the two directions of this file disagree — an
            # input added to the allowlist would pass the documented-or-
            # allowlisted test and still redden here, on a guide that is
            # correct by this file's own definition.
            for name in sorted(self.declared - self.allowed):
                with self.subTest(input=name):
                    self.assertIn(
                        name,
                        guide,
                        "`%s` is declared but absent from the `%s` Default "
                        "column in %s"
                        % (name, row.inputs_heading, rel(self.guide)),
                    )
                    want = canonical_workflow_default(name, defaults, required)
                    got = canonical_guide_default(guide[name])
                    if name in required:
                        have = "required: true (no default)"
                    elif name in defaults:
                        have = "default: %r" % defaults[name]
                    else:
                        have = "no default and not required (arrives as '')"
                    self.assertEqual(
                        got,
                        want,
                        "the %s Default column for `%s` is %r but %s has %s"
                        % (rel(self.guide), name, guide[name], rel(self.workflow), have),
                    )

    name = "InputsDocsDrift_" + row.workflow.replace("-", "_")
    InputsDocsDriftCase.__name__ = name
    InputsDocsDriftCase.__qualname__ = name
    return InputsDocsDriftCase


class RowsCoverTheReusablesTest(unittest.TestCase):
    """ROWS must cover every reusable, or a 17th one ships unpoliced."""

    maxDiff = None

    def test_files_exist_for_every_row(self):
        for row in ROWS:
            with self.subTest(workflow=row.workflow):
                self.assertTrue(
                    os.path.isfile(workflow_path(row)),
                    "ROWS names %s but %s does not exist"
                    % (row.workflow, rel(workflow_path(row))),
                )
                self.assertTrue(
                    os.path.isfile(guide_path(row)),
                    "ROWS points %s at %s, which does not exist"
                    % (row.workflow, rel(guide_path(row))),
                )
                if row.dir_readme:
                    readme = os.path.join(REPO_ROOT, row.dir_readme)
                    self.assertTrue(
                        os.path.isfile(readme),
                        "ROWS points %s's dir_readme at %s, which does not "
                        "exist — a moved README would otherwise surface as a "
                        "FileNotFoundError traceback rather than as drift, and "
                        "its path also has to stay in test-workflow-pins.yml's "
                        "`paths:` filters" % (row.workflow, row.dir_readme),
                    )

    def test_readme_columns_are_internally_consistent(self):
        """A half-filled README column is a check that silently does nothing.

        `test_directory_readme_knob_table` keys entirely off `dir_readme`, so a
        row carrying `readme_heading`/`readme_mode` WITHOUT it skips — no error,
        no coverage. And `readme_mode` is compared by string equality against
        `"two-way"`, so a typo (`"twoway"`, `"two way"`) silently downgrades a
        set-equality pin to the phantom direction alone: the exact
        quietly-vacuous failure every guard in this file exists to prevent.
        """
        for row in ROWS:
            with self.subTest(workflow=row.workflow):
                self.assertIn(
                    row.readme_mode,
                    ("one-way", "two-way"),
                    "ROWS gives %s readme_mode=%r; only 'one-way' and 'two-way' "
                    "are understood, and anything else reads as 'one-way'"
                    % (row.workflow, row.readme_mode),
                )
                if not row.dir_readme:
                    self.assertIsNone(
                        row.readme_heading,
                        "ROWS gives %s a readme_heading (%r) but no dir_readme, "
                        "so the README knob check skips and that heading is "
                        "never read" % (row.workflow, row.readme_heading),
                    )
                    # Same half-filled config, one column over: a row carrying
                    # readme_mode="two-way" with no dir_readme reads as a
                    # set-equality pin while test_directory_readme_knob_table
                    # skips outright, so the strictest-looking column in the
                    # table checks nothing at all.
                    self.assertEqual(
                        row.readme_mode,
                        Row._field_defaults["readme_mode"],
                        "ROWS gives %s readme_mode=%r but no dir_readme, so the "
                        "README knob check skips and that mode is never applied"
                        % (row.workflow, row.readme_mode),
                    )
                else:
                    self.assertTrue(
                        row.readme_heading
                        and HEADING.match(row.readme_heading),
                        "ROWS gives %s dir_readme=%s but readme_heading=%r; "
                        "section_lines matches a literal `## ` line, so a "
                        "missing or wrongly-levelled heading finds no rows"
                        % (row.workflow, row.dir_readme, row.readme_heading),
                    )

    def test_files_read_outside_the_globs_are_in_this_suites_ci_path_filters(self):
        """Every file this suite reads from outside the `paths:` globs must
        appear in BOTH of test-workflow-pins.yml's `paths:` lists.

        That is the two rows' directory READMEs plus the repo-root `README.md`
        — the public catalog `SharedDocExampleCallersTest` scans for
        `uses:`-anchored `with:` fences. None of them matches
        `.github/workflows/**`, `.github/workflow-pins/**` or `docs/callers/**`,
        the three globs the filter already covers. Drop an entry and an edit to
        that file matches no filter, runs no job, and lands green: the #31
        scenario, on files this suite is the only checker of. Nothing else in
        the repo checks a `paths:` filter, so the pairing is asserted here, next
        to the rows that depend on it.

        Each event's own `paths:` block is scanned SEPARATELY and required to
        carry the entry, so two copies in one list cannot stand in for the
        other event's missing one — exactly the mispairing this test exists to
        catch. The needed-files set is derived from the suite, so a row that
        starts reading a new README extends this guard on its own.
        """
        filters = os.path.join(WORKFLOWS_DIR, "test-workflow-pins.yml")
        needed = files_needing_their_own_ci_path_entry()
        self.assertTrue(
            needed,
            "no file outside %s is read by this suite — the rows or "
            "SharedDocExampleCallersTest.DOCS were reshaped; this check is now "
            "vacuous" % (CI_PATH_GLOB_PREFIXES,),
        )
        by_event = {}
        for event in CI_FILTER_EVENTS:
            entries = event_paths_entries(filters, event)
            self.assertIsNotNone(
                entries,
                "no `on.%s:` key in %s — the file moved or was reshaped; this "
                "check is now vacuous" % (event, rel(filters)),
            )
            self.assertTrue(
                entries,
                "`on.%s:` in %s has no `paths:` entries — the filter was "
                "removed or reshaped; this check is now vacuous"
                % (event, rel(filters)),
            )
            by_event[event] = entries
        for path in needed:
            for event in CI_FILTER_EVENTS:
                with self.subTest(path=path, event=event):
                    self.assertIn(
                        path,
                        by_event[event],
                        "%s is read by this suite but is missing from the "
                        "`on.%s.paths:` list in %s — an edit to it would run "
                        "no job at all"
                        % (path, event, rel(filters)),
                    )

    def test_sentinel_pin_has_an_example_to_pin(self):
        """`expect_sentinel_in_examples` needs both examples actually scanned.

        It is enforced inside the `expect_*_with` branch, so a row that turns it
        on while turning either example off gets no sentinel check on that side
        — no error, no coverage: the quietly-vacuous half-filled config this
        file exists to prevent, on the column added to close exactly that gap.
        """
        for row in ROWS:
            if not row.expect_sentinel_in_examples:
                continue
            with self.subTest(workflow=row.workflow):
                self.assertTrue(
                    row.expect_guide_with and row.expect_header_with,
                    "ROWS gives %s expect_sentinel_in_examples=True but "
                    "expect_guide_with=%r / expect_header_with=%r; the sentinel "
                    "is only asserted where a `with:` block is expected, so it "
                    "would silently skip that example"
                    % (
                        row.workflow,
                        row.expect_guide_with,
                        row.expect_header_with,
                    ),
                )

    def test_rows_have_no_duplicate_workflows(self):
        names = [row.workflow for row in ROWS]
        dupes = sorted({name for name in names if names.count(name) > 1})
        self.assertFalse(
            dupes, "ROWS names the same workflow more than once: %s" % dupes
        )

    def test_rows_match_the_reusable_workflows_on_disk(self):
        # The whole point of a table: a 17th reusable fails here until someone
        # gives it a row. Matched on the exact `  workflow_call:` line, so the
        # files that only MENTION it (prose, a heredoc fixture) are not counted.
        on_disk = reusable_workflow_names()
        in_table = {row.workflow for row in ROWS}
        self.assertTrue(
            on_disk,
            "no `%s` line found in any %s/*.yml — the scanner broke or the "
            "directory moved; this coverage check is now vacuous"
            % (WORKFLOW_CALL_LINE.strip(), rel(WORKFLOWS_DIR)),
        )
        self.assertEqual(
            in_table,
            on_disk,
            "ROWS is out of sync with the reusable workflows on disk. Missing a "
            "row: %s. Row with no such reusable: %s"
            % (sorted(on_disk - in_table), sorted(in_table - on_disk)),
        )

    def test_known_undocumented_names_a_workflow_in_rows(self):
        # The other half of the allowlist's staleness contract: the per-row test
        # can only police entries whose workflow still HAS a row, so an entry
        # for a renamed or deleted workflow would go unchecked forever.
        orphans = sorted(set(KNOWN_UNDOCUMENTED) - {row.workflow for row in ROWS})
        self.assertFalse(
            orphans,
            "KNOWN_UNDOCUMENTED has entries for workflows with no ROWS entry: "
            "%s — delete them from KNOWN_UNDOCUMENTED" % orphans,
        )


class SharedDocExampleCallersTest(unittest.TestCase):
    """The copy-pasteable callers that live outside any row's guide.

    `README.md` (the public catalog) and `docs/callers/README.md` both ship
    `with:` fences that a new caller copies, and neither has — or wants — a ROWS
    entry of its own, so nothing above looks at them. A phantom key there is the
    same zero-job `startup_failure` as one in a guide, so they are scanned the
    same way: every `with:` anchored to a `uses:` of one of THIS repo's
    reusables, checked against that reusable's declared inputs.

    Orphan `with:` fragments are SKIPPED here (unlike in a single-subject
    guide): these docs cover many reusables, so an unattributable block would be
    charged against every one of them at once.
    """

    maxDiff = None

    DOCS = ("README.md", os.path.join("docs", "callers", "README.md"))

    def test_every_example_with_key_is_a_declared_input(self):
        rowed = {guide_path(row) for row in ROWS}
        scanned = 0
        for relative in self.DOCS:
            path = os.path.join(REPO_ROOT, relative)
            self.assertTrue(os.path.isfile(path), "%s does not exist" % relative)
            self.assertNotIn(
                path,
                rowed,
                "%s is a row's guide and is already scanned per-row" % relative,
            )
            blocks = fenced_blocks(read_lines(path))
            for workflow in sorted(reusable_workflow_names()):
                declared = workflow_inputs(
                    os.path.join(WORKFLOWS_DIR, workflow + ".yml")
                )
                for block in blocks:
                    for keys in reusable_with_blocks(
                        block, workflow, allow_orphan=False
                    ):
                        scanned += 1
                        with self.subTest(doc=relative, workflow=workflow):
                            undeclared = keys - declared
                            self.assertFalse(
                                undeclared,
                                "the %s example for %s passes `with:` inputs it "
                                "does not declare: %s — a phantom input there "
                                "is a zero-job startup_failure for whoever "
                                "copies it"
                                % (relative, workflow, sorted(undeclared)),
                            )
        self.assertTrue(
            scanned,
            "no `uses:`-anchored `with:` block found in any of %s — the fences "
            "moved or the anchoring broke; this check is now vacuous"
            % (sorted(self.DOCS),),
        )


def _install_cases():
    """Publish one generated TestCase per row under its own module-level name.

    Wrapped in a function on purpose: unittest's loader walks `dir(module)` and
    collects EVERY `TestCase` subclass it finds, with no `_`-prefix exemption.
    A bare module-level `for` loop leaves its loop variables bound afterwards,
    so the last row's class stayed reachable under a second name and that one
    workflow was silently checked twice — 17 runs of each method across 16 rows.
    """
    seen = {}
    for row in ROWS:
        case = _make_case(row)
        # `-` -> `_` is not injective: rows for `foo-bar` and `foo_bar` both
        # derive `InputsDocsDrift_foo_bar`, and the second assignment would
        # silently drop the first workflow's ENTIRE case while
        # test_rows_have_no_duplicate_workflows and the on-disk coverage check
        # both stayed green.
        if case.__name__ in seen:
            raise AssertionError(
                "ROWS entries %r and %r both derive the TestCase name %s — one "
                "would silently replace the other, dropping a whole workflow's "
                "coverage" % (seen[case.__name__], row.workflow, case.__name__)
            )
        seen[case.__name__] = row.workflow
        globals()[case.__name__] = case


_install_cases()


if __name__ == "__main__":
    unittest.main(verbosity=2)
