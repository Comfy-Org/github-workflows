#!/usr/bin/env python3
"""Tests for the single-source-of-truth @anthropic-ai/claude-code pin (BE-5373).

The pin the groom agent jobs install used to be three hardcoded literals inside
`run:` steps, which meant (a) nothing watched it — Dependabot's github-actions
ecosystem only parses `uses:` refs, and an inline `npm install -g pkg@ver` is
invisible to every ecosystem without an npm manifest — and (b) a hand bump could
update two of the three and leave a split state.

The fix moves the version into `.github/groom/package.json`, resolved once by
groom.yml's `gate` job. Nothing in CI actually runs those workflow steps, so
these are the guards that keep the arrangement from quietly regressing:

- the manifest exists, parses, and pins an EXACT version (a range would defeat
  the pin, and groom.yml's gate rejects one at run time);
- groom.yml contains no hardcoded `@anthropic-ai/claude-code@<version>` literal
  anywhere — re-introducing one is the regression;
- every "Install Claude Code" step reads the gate's output instead;
- .github/dependabot.yml still carries the npm entry for `/.github/groom` —
  without it the manifest is just an unwatched second place to rot.

Run: python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
"""

import json
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_MANIFEST = os.path.join(_REPO_ROOT, ".github", "groom", "package.json")
_GROOM_YML = os.path.join(_REPO_ROOT, ".github", "workflows", "groom.yml")
_DEPENDABOT = os.path.join(_REPO_ROOT, ".github", "dependabot.yml")

_PACKAGE = "@anthropic-ai/claude-code"
# Mirrors the gate step's shell guard in groom.yml, and must stay strict for the
# same reason it is strict there. Three ways a sloppy pattern admits a spec npm
# then resolves as a mutable DIST-TAG instead of an exact version — the precise
# un-pinning the guard exists to reject:
#   - SemVer prerelease/build parts are dot-separated NON-EMPTY identifiers, at
#     most one of each, in that order; a looser `([-+][0-9A-Za-z.-]+)*` accepts
#     junk like `1.2.3-a..b`;
#   - a bare `[0-9]+` accepts leading zeros (`01.02.03`), which node-semver
#     rejects;
#   - a bare `[0-9]+` is unbounded (`9007199254740992.0.0`), and node-semver
#     rejects any component past 2^53-1.
# Hence `_NUM` (no leading zeros, at most 15 digits — the widest that cannot
# exceed 2^53-1) and `_PRE_ID` (the SemVer prerelease-identifier grammar, which
# also forbids leading zeros on a NUMERIC identifier). Build identifiers stay
# `[0-9A-Za-z-]+`: SemVer allows leading zeros there, since a build part is
# never compared numerically.
#
# Always matched with `re.fullmatch`, never `assertRegex`/`re.search`, which
# use `re.search` semantics where `$` also matches just before a trailing
# newline. That would silently accept a manifest value of `"2.1.217\n"`.
#
# Measured, because the direction is counter-intuitive: the shell ACCEPTS that
# value (command substitution strips the trailing newline before the guard sees
# it), so `fullmatch` is strictly TIGHTER than the run-time check, not a mirror
# of it. That is the direction to err in — this guard runs on the PR that edits
# the manifest, so a malformed pin gets rejected at review time instead of
# being quietly normalized on every groom run forever. An EMBEDDED newline is
# rejected in both places.
_NUM = r"(0|[1-9][0-9]{0,14})"
_PRE_ID = r"(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_EXACT_VERSION = re.compile(
    rf"{_NUM}\.{_NUM}\.{_NUM}"
    rf"(-{_PRE_ID}(\.{_PRE_ID})*)?"
    r"(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"
)
# Any `<pkg>@<spec>` occurrence in the workflow. The point is that NO version
# literal survives, so this is deliberately loose about what follows the `@` —
# see `_hardcoded_specs` for how a literal is told apart from an expansion.
_PKG_AT = re.compile(re.escape(_PACKAGE) + r"@(\S+)")

# A job key inside the top-level `jobs:` mapping. GitHub Actions job IDs allow
# `-` and uppercase, so this deliberately does NOT narrow to `[a-z_]`: a future
# job named `build-cli` must still register as a block boundary, or its steps
# fold into the PRECEDING job's block and the `needs: gate` assertion below
# passes vacuously — the precise regression these guards exist to catch.
_JOB_KEY = re.compile(r"^  ([A-Za-z_][A-Za-z0-9_-]*):$", re.MULTILINE)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _hardcoded_specs(text):
    """`<pkg>@<spec>` occurrences whose spec is a LITERAL, not an expansion.

    Keyed on the absence of a `$` expansion rather than on a prefix allowlist:
    an allowlist of `${` and `"` gets it wrong in both directions — it flags a
    perfectly valid unbraced `@$CLAUDE_CODE_VERSION`, and it lets a quoted
    hardcoded `@"2.1.217"` through, which is the regression itself.
    """
    return [m.group(1) for m in _PKG_AT.finditer(text) if "$" not in m.group(1)]


def _strip_comments(block):
    """Drop YAML comments, whole-line and inline.

    The `gate` job's prose explains the very things these tests grep for — it
    contains both `npm install -g ...` and the words `needs: gate` — so a raw
    substring scan sees a phantom install step in a job that has none and then
    "proves" it depends on itself. Only executable lines count.

    Inline too, per the YAML rule that a `#` opens a comment only when preceded
    by whitespace: a correct `needs: gate  # after the interval gate` would
    otherwise parse the comment text INTO the needs set and red the suite.
    """
    out = []
    for line in block.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)


def _jobs(text):
    """Yield (job_name, block) for each job in groom.yml's `jobs:` mapping."""
    # Slice from `jobs:` first: `on:` has 2-space keys too (`workflow_call:`),
    # and those are not jobs.
    start = re.search(r"^jobs:$", text, re.MULTILINE)
    assert start, "groom.yml has no top-level `jobs:` mapping"
    body = text[start.end() :]
    starts = [(m.group(1), m.start()) for m in _JOB_KEY.finditer(body)]
    for i, (name, at) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(body)
        yield name, body[at:end]


def _dependabot_entries(text):
    """Split dependabot.yml's `updates:` list into one dict of scalars per entry.

    Deliberately shallow — it reads only the top-level scalar keys of each
    `- package-ecosystem:` item, which is all this guard asserts on. Enough to
    tie ecosystem/directory/strategy to the SAME entry without a YAML parser
    (this repo's Python is stdlib-only).

    Surrounding quotes are dropped: `npm` and `"npm"` are the same YAML scalar,
    so asserting on the quoted spelling would red this guard on a purely
    cosmetic, semantically identical edit to dependabot.yml.
    """

    def _scalar(raw):
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            return raw[1:-1]
        return raw

    entries = []
    current = None
    for line in _strip_comments(text).splitlines():
        head = re.match(r"^  - ([a-z-]+):[ \t]*(.*)$", line)
        if head:
            current = {head.group(1): _scalar(head.group(2))}
            entries.append(current)
            continue
        key = re.match(r"^    ([a-z-]+):[ \t]*(.*)$", line)
        if key and current is not None:
            current[key.group(1)] = _scalar(key.group(2))
    return entries


def _needs(block):
    """The job's `needs:` as a set, across all three YAML spellings.

    `needs: gate`, `needs: [audit_find, gate]`, and the block-sequence form are
    all valid and all equivalent. Matching only a bare scalar or the FIRST flow
    element would red the suite on a correct reordering, which trains people to
    edit the test instead of reading it.
    """
    m = re.search(r"^    needs:[ \t]*(.*)$", _strip_comments(block), re.MULTILINE)
    if not m:
        return set()
    inline = m.group(1).strip()
    if inline:
        return {n.strip().strip("\"'") for n in inline.strip("[]").split(",") if n.strip()}
    # Block sequence: `needs:` on its own line, then `- name` items. YAML lets
    # the sequence sit at the KEY's own indentation (`    - gate`) as readily as
    # indented under it (`      - gate`), and both are what a formatter might
    # emit — pinning one spelling would red this guard on a correct file.
    tail = _strip_comments(block)[m.end() :]
    out = set()
    for line in tail.splitlines()[1:]:
        item = re.match(r"^ {4,8}- (.+)$", line)
        if not item:
            break
        out.add(item.group(1).strip().strip("\"'"))
    return out


class ManifestTest(unittest.TestCase):
    def test_manifest_pins_the_cli_at_an_exact_version(self):
        manifest = json.loads(_read(_MANIFEST))
        deps = manifest.get("dependencies", {})
        self.assertIn(
            _PACKAGE,
            deps,
            f"{_MANIFEST} is the single source of truth for the groom agent CLI pin",
        )
        version = deps[_PACKAGE]
        self.assertIsNotNone(
            _EXACT_VERSION.fullmatch(version),
            f"{_PACKAGE} must be pinned to an exact version, got {version!r}. "
            "A range or dist-tag would un-pin executable supply chain, and "
            "groom.yml's gate rejects it at run time.",
        )

    def test_exact_version_pattern_matches_the_shell_guard(self):
        # The pattern above is a MIRROR of groom.yml's gate regex; nothing
        # mechanically keeps the two in step, so pin the shared contract here.
        # These cases are the ones where a sloppy mirror and the shell disagree.
        for good in (
            "2.1.217",
            "1.0.0",
            "0.0.0",
            "2.1.218-beta.1",
            "1.0.0+b5",
            "1.0.0-rc.1+b5",
            "1.2.3-0a",  # alphanumeric identifier, so a leading 0 is legal
            "1.2.3-alpha.0",
            "1.2.3+001",  # leading zeros ARE legal in a build identifier
            "999999999999999.0.0",  # 15 digits — the widest component allowed
        ):
            self.assertIsNotNone(_EXACT_VERSION.fullmatch(good), good)
        for bad in (
            "^2.1.217",
            "~2.1.0",
            "*",
            "latest",
            "2.1.x",
            ">=2.0.0",
            "",
            "2.1.217\n",  # `re.search`-with-`$` would accept this; fullmatch must not
            "1.2.3-a..b",  # empty prerelease identifier -> npm dist-tag, not exact
            "1.2.3-",
            "1.2.3+",
            "01.02.03",  # leading zeros -> node-semver rejects -> npm dist-tag
            "1.02.3",
            "1.2.3-01",  # numeric prerelease identifier, same rule
            "9007199254740992.0.0",  # 16 digits, past 2^53-1 -> node-semver rejects
        ):
            self.assertIsNone(_EXACT_VERSION.fullmatch(bad), bad)

    def test_manifest_is_private_and_declares_no_other_dependencies(self):
        # It is a pin carrier, not a project. Keeping it to one dependency is
        # what makes a Dependabot PR against this directory unambiguous, and
        # `private` keeps it from ever being publishable by accident.
        manifest = json.loads(_read(_MANIFEST))
        self.assertIs(manifest.get("private"), True)
        self.assertEqual(list(manifest.get("dependencies", {})), [_PACKAGE])
        self.assertNotIn("devDependencies", manifest)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.groom = _read(_GROOM_YML)

    def test_no_hardcoded_version_literal_remains(self):
        # Scanned WITHOUT `_strip_comments`, unlike the count below: a version
        # literal sitting in prose is stale documentation the moment Dependabot
        # bumps the manifest, which is the same rot this PR removed from the
        # `run:` steps. Reference the pin, never restate it.
        found = _hardcoded_specs(self.groom)
        self.assertEqual(
            [],
            found,
            "groom.yml must not hardcode the CLI version — it comes from "
            f".github/groom/package.json via the gate job. Found: {found}",
        )

    def test_hardcoded_scan_keys_on_expansion_not_on_a_prefix(self):
        # The scan must key on whether the spec EXPANDS, not on how it is
        # spelled: both directions of a prefix allowlist are regressions.
        self.assertEqual([], _hardcoded_specs(f'npm i -g {_PACKAGE}@$CLAUDE_CODE_VERSION'))
        self.assertEqual(
            [], _hardcoded_specs(f'npm i -g "{_PACKAGE}@${{CLAUDE_CODE_VERSION}}"')
        )
        self.assertEqual(['"2.1.217"'], _hardcoded_specs(f'npm i -g {_PACKAGE}@"2.1.217"'))
        self.assertEqual(["2.1.217"], _hardcoded_specs(f"npm i -g {_PACKAGE}@2.1.217"))

    def test_every_install_step_uses_the_gate_output(self):
        # Comment-stripped: a YAML comment that happens to spell out
        # `npm install -g <the package>` is not an install site, and counting it
        # would red this guard even though no executable step changed.
        installs = [
            line
            for line in _strip_comments(self.groom).splitlines()
            if "npm install -g" in line and _PACKAGE in line
        ]
        self.assertEqual(
            3,
            len(installs),
            f"expected the three known install sites, found {len(installs)}: {installs}",
        )
        for line in installs:
            self.assertIn("CLAUDE_CODE_VERSION", line, line)

        # Each of those steps must actually be handed the gate's output; an
        # `env:` block that never got added would leave the variable unset and
        # the `:?` guard would fail the run rather than install a wrong version,
        # but catching it here is cheaper than catching it in a groom run.
        self.assertEqual(
            3,
            self.groom.count(
                "CLAUDE_CODE_VERSION: ${{ needs.gate.outputs.claude_code_version }}"
            ),
        )

    def test_gate_exports_the_resolved_version(self):
        self.assertIn(
            "claude_code_version: ${{ steps.claude_version.outputs.version }}",
            self.groom,
        )

    def test_every_install_job_depends_on_the_gate(self):
        # The gate output is only readable by jobs that `needs: gate`; a future
        # agent job that installs the CLI without that edge would silently
        # resolve to an empty string.
        installing = [
            name
            for name, block in _jobs(self.groom)
            if _PACKAGE in _strip_comments(block)
            and "npm install -g" in _strip_comments(block)
        ]
        self.assertEqual(
            ["audit_find", "audit_verify", "build"],
            installing,
            "the set of CLI-installing jobs changed; keep this list honest so "
            "the `needs: gate` assertion below cannot pass vacuously",
        )
        for name, block in _jobs(self.groom):
            if name in installing:
                self.assertIn(
                    "gate",
                    _needs(block),
                    f"job {name!r} installs the agent CLI but does not `needs: gate`",
                )


class DependabotTest(unittest.TestCase):
    def test_npm_entry_watches_the_groom_manifest(self):
        # Text scanning rather than a YAML parse: this repo's Python is
        # stdlib-only (no PyYAML in CI). But the three keys are asserted against
        # ONE entry, not against the whole file — three independent substring
        # checks would still pass if the npm block were moved off the manifest
        # while some other ecosystem entry happened to point at
        # `/.github/groom`, masking exactly the regression this guards.
        entries = _dependabot_entries(_read(_DEPENDABOT))
        npm = [e for e in entries if e.get("package-ecosystem") == "npm"]
        self.assertEqual(
            1, len(npm), f"expected exactly one npm entry, found {len(npm)}"
        )
        self.assertEqual("/.github/groom", npm[0].get("directory"))
        # The workflow guard rejects a range at run time, so the updater must be
        # told to keep an exact requirement exact rather than widening it.
        self.assertEqual("increase", npm[0].get("versioning-strategy"))


if __name__ == "__main__":
    unittest.main()
