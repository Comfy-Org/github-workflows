#!/usr/bin/env python3
"""Table-driven declared-vs-documented input drift check, for ALL 16 reusables.

Two hand-rolled copies of this check already exist — one for `cursor-review`
(`.github/cursor-review/tests/test_workflow_inputs_docs.py`) and one for
`refresh-reviewers` (`.github/refresh-reviewers/tests/test_inputs_docs.py`).
Both were written for the same failure, BE-4691: `cursor-review.yml`'s
`blocking:` input was deleted in #31 while its documentation lived on in three
places for weeks, and GitHub rejects an unknown `workflow_call` input at startup
with a zero-job `startup_failure` and no logs. So a phantom input in a caller
guide is a broken caller for whoever copies it. The other 14 reusables had no
such pin at all; a third hand-rolled copy per workflow is not the answer.

This file is the generalisation: one `ROWS` table, one set of parsers (ported
from the refresh-reviewers copy, CRLF handling and blank/comment tolerance
included), one generated `TestCase` per reusable so `-v` names the workflow that
drifted. It deliberately does NOT delete the two originals (that is a separate
fold-in, which also flips the `dir_readme` / `check_defaults` columns on for the
two rows that have those extra pins today) and it does NOT fix any docs drift.

Direction-by-direction, what is asserted and why the strictness differs:

* **Phantom** (documented but not declared) — STRICT, no allowlist, because
  every phantom is a copy-paste caller that fails at startup.
* **Undocumented** (declared but not documented) — a knob nobody can discover.
  Real, but quieter, and 13 of them already exist. They are pinned in
  `KNOWN_UNDOCUMENTED` below, modelled on `KNOWN_EXEMPT` in
  `check_workflow_pins.py`: an entry is a KNOWN debt, not a blessing, and a
  STALE entry FAILS, so the list drains itself as the guides get filled in.
* **Example `with:` keys** — the same phantom failure one level in, in the two
  copy-paste callers this repo ships (the guide's caller fence and the workflow
  header comment). Subset, not equality: an example legitimately shows only a
  few inputs.

Parsed WITHOUT PyYAML on purpose: this repo is stdlib-only (`AGENTS.md`) and
`test-workflow-pins.yml` installs no requirements. The workflows are uniformly
2-space indented with every input key alone on its 6-space line, which is all
these scanners need. Every scanner is also guarded against going quiet and
passing vacuously, since each assertion compares two scanner outputs and {} vs
{} is a pass: the two NAME scanners must both find the row's `sentinel` input,
and each example `with:` scanner must find at least one key (non-empty rather
than sentinel-bearing — an example caller shows only the knobs it needs).

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
# key of `on:`, at exactly 2-space indent and alone on its line. Matched
# exactly, not by substring, so the files that merely MENTION `workflow_call`
# — in prose, in a heredoc fixture, in a comment — are not mistaken for
# reusables and do not demand a ROWS entry.
WORKFLOW_CALL_LINE = "  workflow_call:"

# An input declaration: the key alone on its 6-space line, directly under
# `    inputs:`. Sub-keys of an input (description/type/default) are 8-space,
# and folded description text deeper still, so none of them match.
INPUT_KEY = re.compile(r"^      ([A-Za-z0-9_-]+):\s*$")
# Section bounds: `## ` ONLY, never `### `. `linear-ticket.md` puts the two
# caller fences under `### 1.` / `### 2.` sub-headings inside
# `## Caller — two files`, so a `^#{2,}` bound (what the refresh-reviewers copy
# uses, correctly, for a guide with no sub-headings) would end that section
# before either fence and make the `with:` scan vacuous.
#
# The trade-off is real and deliberate: a `###` sub-section is now scanned as
# part of its parent, so a table under one contributes names too. Exactly one
# exists today — `### Choosing \`approval-mode\`` under
# `docs/callers/detect-unreviewed-merge.md`'s `## Inputs` — and it yields no
# phantom. Widening toward MORE documented names can only ever add a phantom
# failure, never hide one, so it fails safe.
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


Row = collections.namedtuple(
    "Row",
    (
        "workflow",  # basename without .yml, e.g. "cursor-review"
        "sentinel",  # input that MUST appear on both sides (anti-vacuity guard)
        "guide",  # None -> docs/callers/<workflow>.md
        "inputs_heading",  # the guide heading whose table lists the inputs
        "caller_heading",  # the guide heading whose fences hold the example
        "expect_guide_with",  # a `with:` block is expected in the guide fence
        "expect_header_with",  # ... and in the workflow's header comment
        "dir_readme",  # None -> no directory-README knob table to police
        "readme_heading",
        "readme_mode",  # "one-way" (phantom only) | "two-way" (set equality)
        "check_defaults",  # compare the guide's Default column to the workflow
    ),
    defaults=(
        None,  # guide
        "## Inputs",  # inputs_heading
        "## Caller",  # caller_heading
        True,  # expect_guide_with
        True,  # expect_header_with
        None,  # dir_readme
        None,  # readme_heading
        "one-way",  # readme_mode
        False,  # check_defaults
    ),
)

# One row per reusable. `sentinel` is the anti-vacuity guard: an input the
# workflow really declares AND the guide's Inputs table really names, so an
# empty-vs-empty comparison can never pass silently. `workflows_ref` is the
# natural choice for the 11 that declare it; the other five name a knob of
# their own.
#
# `dir_readme` / `readme_heading` / `readme_mode` / `check_defaults` are OFF on
# every row here by design: the two hand-rolled originals still own those extra
# pins (cursor-review's `## Configuration knobs` README table, refresh-reviewers'
# `## Knob defaults (and why)` table and its Default-column comparison), and
# turning them on here is the fold-in ticket's job, not this one's.
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
    Row(workflow="cursor-review", sentinel="workflows_ref"),
    Row(workflow="detect-unreviewed-merge", sentinel="approval-mode"),
    # groom's guide leads with the finds-only caller rather than a `## Caller`.
    Row(
        workflow="groom",
        sentinel="workflows_ref",
        caller_heading="## Minimal caller — finds-only",
    ),
    # linear-ticket ships TWO caller files; only the second one calls this
    # reusable, and the header comment carries no `with:` example.
    Row(
        workflow="linear-ticket",
        sentinel="workflows_ref",
        caller_heading="## Caller — two files",
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
    Row(workflow="refresh-reviewers", sentinel="workflows_ref"),
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
    caller fences are full of YAML `#` comments and one gaining a second `#`
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


def workflow_inputs(path):
    """Input names declared under on.workflow_call.inputs."""
    names, in_inputs = set(), False
    for line in workflow_head(path):
        if line == "    inputs:":
            in_inputs = True
            continue
        if in_inputs and (not line.strip() or line.lstrip().startswith("#")):
            # Blank or comment lines carry no indentation signal: a 4-space
            # comment is legal YAML inside `inputs:` and must not trip the
            # dedent break and silently truncate the scan.
            continue
        if in_inputs and re.match(r"^    \S", line):  # dedent: secrets:, etc.
            break
        if in_inputs:
            match = INPUT_KEY.match(line)
            if match:
                names.add(match.group(1))
    return names


def documented_knob_names(path, heading):
    """Every backticked name in the FIRST cell of each table row under
    `heading`, INCLUDING combined-cell rows like `| `scope_label` / `scope_desc` |`
    that a lone-name regex would skip. A cell counts only when every
    `/`-separated part is itself a lone backticked name, so the header row, the
    `---` separator, and prose rows contribute nothing.

    Combined cells are NOT a README-only shape, which is why this — not the
    lone-name scanner — is what the Inputs tables are read with: groom's guide
    documents `scope_label`/`scope_desc` on one row and pr-derisk's documents
    `repo_map_path`/`repo_runbooks_path` on one row. Reading those with a
    lone-name regex would report four real, documented inputs as undocumented.
    """
    names = set()
    for line in section_lines(read_lines(path), heading):
        if not line.lstrip().startswith("|"):
            continue
        cells = split_cells(line)
        if not cells or not cells[0]:
            continue
        parts = [part.strip() for part in cells[0].split("/")]
        matched = [re.fullmatch(r"`([A-Za-z0-9_-]+)`", part) for part in parts]
        if all(matched):
            names.update(match.group(1) for match in matched)
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


def with_keys(lines):
    """Mapping keys directly under each `with:` in `lines`.

    Relative-indentation only, no YAML parser: the first non-blank line after a
    `with:` fixes the child indent, and keys at exactly that indent are
    collected until the block dedents back to (or past) the `with:` line. So
    `uses:` / `secrets:`, which sit at the `with:` indent, end the block rather
    than count as inputs.
    """
    keys = set()
    i, n = 0, len(lines)
    while i < n:
        opener = WITH_LINE.match(lines[i])
        if not opener:
            i += 1
            continue
        base = len(opener.group(1))
        child = None
        j = i + 1
        while j < n:
            line = lines[j]
            if not line.strip() or line.lstrip().startswith("#"):
                j += 1
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
            j += 1
        i = j
    return keys


def example_with_keys(row):
    """`with:` keys from the two example callers this repo ships, as
    {source_label: (set_of_keys, expected_non_empty)}.

    Both sources are scoped the way their label claims, and each fenced block is
    scanned on its own:

    * Guide side — ONLY the fences under the row's caller heading, not every
      fence in the file, so the anti-vacuity guard can't be satisfied by an
      unrelated snippet while the real caller example silently loses its
      `with:`, and so a stray step-level `with:` elsewhere (an
      `actions/checkout`) can't register here as a phantom input.
    * Workflow side — ONLY the header comment above `jobs:`, bounded exactly the
      way the input scan is bounded, not every column-0 `#` line in the file.

    Scanning per block (rather than one flattened list) keeps a `with:` that
    ends one block from absorbing the next block's more-indented lines.
    """
    guide = guide_path(row)
    guide_keys = set()
    for block in fenced_blocks(section_lines(read_lines(guide), row.caller_heading)):
        guide_keys |= with_keys(block)

    header_comment = [
        strip_comment_prefix(line)
        for line in workflow_head(workflow_path(row))
        if line.startswith("#")
    ]
    return {
        "%s %s" % (rel(guide), row.caller_heading): (
            guide_keys,
            row.expect_guide_with,
        ),
        "%s header comment" % rel(workflow_path(row)): (
            with_keys(header_comment),
            row.expect_header_with,
        ),
    }


def workflow_input_defaults(path):
    """(`{name: raw default:}`, `{names marked required: true}`) from the
    workflow. Required inputs carry no default (`workflows_ref`)."""
    defaults, required, current, in_inputs = {}, set(), None, False
    for line in workflow_head(path):
        if line == "    inputs:":
            in_inputs = True
            continue
        if not in_inputs:
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^    \S", line):  # dedent out of inputs: (secrets:, etc.)
            break
        key = INPUT_KEY.match(line)
        if key:
            current = key.group(1)
            continue
        if current is None:
            continue
        default = INPUT_DEFAULT.match(line)
        if default:
            defaults[current] = default.group(1)
            continue
        req = INPUT_REQUIRED.match(line)
        if req and req.group(1) == "true":
            required.add(current)
    return defaults, required


def documented_input_defaults(path, heading):
    """`{input key: raw Default-column cell}` from the table under `heading`."""
    result = {}
    for line in section_lines(read_lines(path), heading):
        if not line.lstrip().startswith("|"):
            continue
        cells = split_cells(line)
        if len(cells) < 2:
            continue
        key = re.fullmatch(r"`([A-Za-z0-9_-]+)`", cells[0])
        if key:
            name = key.group(1)
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
    if text[:1] in "\"'":
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


def reusable_workflow_names():
    """Basenames of every `.github/workflows/*.yml` that is itself reusable.

    `*.yml` only, matching the repo's uniform extension — there is no `.yaml`
    workflow here and `test_rows_match_the_reusable_workflows_on_disk` would not
    notice one. Actions accepts both, so widen this glob if that ever changes.
    """
    names = set()
    for path in glob.glob(os.path.join(WORKFLOWS_DIR, "*.yml")):
        if WORKFLOW_CALL_LINE in read_lines(path):
            names.add(os.path.basename(path)[: -len(".yml")])
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
            for label, (keys, expect_non_empty) in sorted(
                example_with_keys(row).items()
            ):
                with self.subTest(example=label):
                    if expect_non_empty:
                        # Guard: this example really does pass inputs, so a
                        # scanner gone quiet fails loudly instead of asserting
                        # {} ⊆ declared. NON-EMPTY, not "names the sentinel":
                        # an example caller shows only the knobs it needs, and
                        # several here legitimately omit theirs — groom's
                        # header example leans on `workflows_ref`'s `default:
                        # ''` carve-out and passes no ref at all, `stale`'s
                        # passes only `dry_run`. Where the row says no `with:`
                        # is expected at all, demanding one is permanently red.
                        self.assertTrue(
                            keys,
                            "the %s `with:` scanner found no keys at all — the "
                            "example block moved or the parser broke; this "
                            "subset check is now vacuous" % label,
                        )
                    undeclared = keys - self.declared
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
            for name in sorted(self.declared):
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


def _install_cases():
    """Publish one generated TestCase per row under its own module-level name.

    Wrapped in a function on purpose: unittest's loader walks `dir(module)` and
    collects EVERY `TestCase` subclass it finds, with no `_`-prefix exemption.
    A bare module-level `for` loop leaves its loop variables bound afterwards,
    so the last row's class stayed reachable under a second name and that one
    workflow was silently checked twice — 17 runs of each method across 16 rows.
    """
    for row in ROWS:
        case = _make_case(row)
        globals()[case.__name__] = case


_install_cases()


if __name__ == "__main__":
    unittest.main(verbosity=2)
