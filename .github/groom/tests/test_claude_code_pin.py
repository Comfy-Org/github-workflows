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

The last class is a different kind of guard: it queries the registry to check
that the PINNED VERSION'S OWN dependency tree still has the shape that makes a
top-level-only pin a complete pin. See `TestPinnedDependencyShape` and the
"Scope: why the top-level pin is the whole pin" section of ../README.md.

Run: python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
"""

import json
import os
import re
import shutil
import subprocess
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

# The registry round-trip for the dependency-shape guard below. Generous, because
# a cold CI runner's first `npm view` pays a metadata fetch, and the failure mode
# of a too-tight timeout here is a red suite on a PR that changed nothing.
_NPM_TIMEOUT_SEC = 60
# One process per (spec, field) for the whole run, so splitting the shape guard
# into readable per-assertion test methods costs one registry hit, not four.
_NPM_CACHE = {}

# Spellings of "not actually CI". GitHub Actions sets `CI=true` on every runner,
# so a registry that errors or times out hard-fails there rather than skipping; a
# developer who exports `CI=false` gets the offline-friendly skip they clearly
# meant.
_NOT_CI = {"", "0", "false", "no", "off"}


def _in_ci():
    return os.environ.get("CI", "").strip().lower() not in _NOT_CI


def _npm_field(spec, field):
    """`npm view <spec> <field> --json`, as `(value, error, unavailable)`.

    ONE field per invocation, deliberately. Asking for two in a single call is
    the obvious optimization and it is unsafe: npm keys the output object by
    field name only when BOTH fields are present. When just one exists it prints
    that field's map BARE and unlabelled, so `{"is-number": "^6.0.0"}` is
    indistinguishable from a two-field reply in which `dependencies` happens to
    be absent. Read the keyed way, a future release that declares real
    `dependencies` and no `optionalDependencies` would parse as "both empty" and
    PASS — the precise regression this guard exists to fail on. One field per
    call has no such shape ambiguity: the reply is that field's map, `{}` if the
    field is present but empty, or empty output if the field is absent at all.

    Returns `(value, error, unavailable)`. `error` is set for anything that
    stopped us from learning the shape — non-zero exit, timeout, unreadable JSON —
    and is fatal in CI, where an unverified guard is an absent guard. `unavailable`
    marks the narrower "there is no npm here at all" case, which is ALWAYS a skip:
    it is a precise, non-flaky property of the machine rather than a transient
    registry failure, and an `unittest discover` on a node-less dev box must not go
    red over a change it has nothing to do with. (CI runners ship npm, so this
    branch does not quietly disarm the guard there.)
    """
    key = (spec, field)
    if key in _NPM_CACHE:
        return _NPM_CACHE[key]

    npm = shutil.which("npm")
    if npm is None:
        result = (None, "no `npm` on PATH", True)
    else:
        try:
            proc = subprocess.run(
                [npm, "view", spec, field, "--json"],
                capture_output=True,
                text=True,
                timeout=_NPM_TIMEOUT_SEC,
                # Inherited stdin would let a credential prompt hang the run out
                # to the timeout instead of failing immediately.
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            result = (None, f"`npm view` timed out after {_NPM_TIMEOUT_SEC}s", False)
        except OSError as exc:  # npm on PATH but unexecutable
            result = (None, f"could not run `npm view`: {exc}", False)
        else:
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                result = (
                    None,
                    f"`npm view {spec} {field}` exited {proc.returncode}"
                    + (f": {detail[0]}" if detail else ""),
                    False,
                )
            else:
                raw = proc.stdout.strip()
                # Absent field -> npm prints nothing. That is a real answer ("no
                # such field"), not a failure, and `{}` is its faithful reading.
                if not raw:
                    result = ({}, None, False)
                else:
                    try:
                        result = (json.loads(raw), None, False)
                    except ValueError as exc:
                        result = (
                            None,
                            f"`npm view` returned unreadable JSON: {exc}",
                            False,
                        )

    _NPM_CACHE[key] = result
    return result


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


class TestPinnedDependencyShape(unittest.TestCase):
    """The pinned version's own dependency tree must stay CLOSED (BE-5580).

    Everything above guards the pin's *mechanism*. This guards its *scope*.

    `npm install -g` has no lockfile, so a top-level pin does not mechanically
    pin transitive deps. The reason that is nonetheless the whole pin today is a
    property of the package, not of the install command: as of 2.1.x
    `@anthropic-ai/claude-code` declares NO regular dependencies, and its only
    optionalDependencies are same-scope `@anthropic-ai/claude-code-<platform>`
    binaries exact-pinned to the identical version. So the resolved install is
    fully determined by the pinned version. The accepted boundary and the risks
    accepted with it are written up in ../README.md, "Scope: why the top-level
    pin is the whole pin".

    That property is not guaranteed to hold forever — versions 1.x through 2.0.0
    DID declare floating third-party `@img/sharp-*: ^0.33.5` ranges, under which
    two installs of the same pinned CLI version can resolve different bytes. So
    the residual risk gets a guard rather than a promise: this class fails the
    PR that changes the pin — Dependabot's bump PR edits
    `.github/groom/package.json`, which is exactly what triggers this suite — if
    the new version's tree has stopped being closed. A red here is not "fix the
    test"; it is the signal to re-open the transitive-pinning decision.

    Network-dependent — the only class here that is — so it degrades by AUDIENCE
    rather than failing everyone: a registry that errors or times out is a hard
    failure in CI, where an unverifiable guard is a silently missing guard, and a
    skip on a dev machine, where an offline `unittest discover` must not go red
    over a change it has nothing to do with. A machine with no `npm` at all skips
    in both places; CI runners ship npm, so that branch cannot disarm the guard
    where it matters.
    """

    @classmethod
    def setUpClass(cls):
        deps = json.loads(_read(_MANIFEST)).get("dependencies", {})
        cls.version = deps.get(_PACKAGE)
        cls.spec = f"{_PACKAGE}@{cls.version}"

    def setUp(self):
        # ManifestTest owns asserting these; here they are preconditions, and
        # querying the registry for `@anthropic-ai/claude-code@None` or for a
        # dist-tag would just add a confusing second failure to that one.
        if self.version is None or _EXACT_VERSION.fullmatch(self.version) is None:
            self.skipTest(
                f"{_MANIFEST} does not pin {_PACKAGE} to an exact version "
                f"(got {self.version!r}) — see ManifestTest for that failure"
            )

    def _field(self, field):
        """The pinned version's `field` map, or skip/fail if we cannot learn it."""
        value, error, unavailable = _npm_field(self.spec, field)
        if error is not None:
            message = (
                f"could not verify the dependency shape of {self.spec}: {error}. "
                "The npm registry could not be consulted, so this guard could not "
                "check that the pinned CLI's dependency tree is still closed under "
                "the exact top-level pin (see .github/groom/README.md, 'Scope: why "
                "the top-level pin is the whole pin')."
            )
            # No npm at all is a property of the MACHINE, not a failed check, so it
            # skips everywhere — CI runners ship npm, so this cannot quietly disarm
            # the guard there. A registry that IS reachable-in-principle but errored
            # or timed out is different: in CI an unverified guard is an ABSENT
            # guard, and this suite runs on the bump PR precisely to be the check
            # nobody has to remember.
            if _in_ci() and not unavailable:
                self.fail(message)
            self.skipTest(message)
        self.assertIsInstance(value, dict, f"unexpected `npm view` shape: {value!r}")
        for name, spec in value.items():
            # Guards the single-field reading above: every value in a dependency
            # map is a version SPEC string. A nested object would mean npm
            # answered in some other shape (keyed by field, or by version) and
            # this whole comparison is meaningless rather than passing.
            self.assertIsInstance(
                spec, str, f"unexpected `npm view` {field} entry: {name}={spec!r}"
            )
        return value

    def _regression(self, name, spec):
        return (
            f"{self.spec} declares {name}@{spec}: the dependency tree is no "
            "longer closed under the exact top-level pin, so the accepted "
            "boundary documented in .github/groom/README.md no longer holds. "
            "Re-open the transitive-pinning decision (spike BE-5580) before "
            "bumping."
        )

    def test_pinned_version_declares_no_regular_dependencies(self):
        # ANY regular dependency is a shape regression: unlike the platform
        # binaries below, a `dependencies` entry is installed unconditionally and
        # is not covered by the same-publisher argument.
        deps = self._field("dependencies")
        for name, spec in sorted(deps.items()):
            self.fail(self._regression(name, spec))

    def test_optional_dependencies_are_all_same_scope(self):
        # The boundary rests on the platform binaries sharing a publisher with
        # the top-level package: hijacking one is not a cheaper attack than
        # hijacking the thing we pinned. A dep from any other scope breaks that
        # argument even if it happens to be exact-pinned — 1.x-2.0.0's
        # `@img/sharp-*` is the historical instance.
        optional = self._field("optionalDependencies")
        for name, spec in sorted(optional.items()):
            if not name.startswith("@anthropic-ai/"):
                self.fail(self._regression(name, spec))

    def test_optional_dependencies_are_pinned_to_the_same_exact_version(self):
        # String equality, NOT semver satisfaction: `^2.1.217` SATISFIES 2.1.217
        # while still floating to 2.9.x tomorrow, and a floating range is
        # precisely the regression being guarded.
        optional = self._field("optionalDependencies")
        for name, spec in sorted(optional.items()):
            if spec != self.version:
                self.fail(self._regression(name, spec))


if __name__ == "__main__":
    unittest.main()
