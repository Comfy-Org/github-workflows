#!/usr/bin/env python3
"""Check a repo's AGENTS.md against the Comfy AGENTS.md standard.

The standard (Comfy Engineering Guide, "AGENTS.md, done right", §10): one thin
top-level `AGENTS.md` is the single source of truth, `CLAUDE.md` is a REQUIRED
one-line `@AGENTS.md` shim (optionally with a few Claude-only lines below —
Claude Code reads only CLAUDE.md and does not fall back), there are no
divergent `.cursorrules`, and the file stays under a hard line ceiling (200,
per Anthropic guidance) with an aspirational target (150). In a monorepo every
nested `AGENTS.md` gets a sibling `CLAUDE.md` shim so Claude Code picks it up in
that subtree, and the file is owned by a DRI via CODEOWNERS.

This script enforces that mechanically so it can wire into CI as a required
status check. It operates on a checked-out repo tree (the CALLER's repo when
run from the reusable workflow) and exits non-zero when any hard check fails.

A repo whose PRODUCT is agent instructions (a plugin/skill marketplace) ships
`AGENTS.md` + `CLAUDE.md` pairs as distributable payload, where the nested-shim
rule is simply wrong — that payload is not this repo's own agent instructions.
`--exclude` carves those subtrees out of the nested walk without disabling the
nested check everywhere else. Exclusions are always echoed to the log, and a
glob that would exclude the ROOT agents file or `CLAUDE.md` is rejected: root
compliance is the non-negotiable part of the standard.

Exit codes: 0 pass, 1 one or more checks failed, 2 bad `--exclude` config.

Run locally:
    python3 .github/agents-md-integrity/check_agents_md.py --root .
    python3 .github/agents-md-integrity/check_agents_md.py --root . \
        --exclude 'plugins/**'
"""

import argparse
import os
import re
import sys

# CODEOWNERS is honored from exactly one location, in this precedence order
# (GitHub uses the first that exists).
CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")

# Directories we never descend into when hunting for nested AGENTS.md files:
# vendored / generated / tooling trees that aren't part of the repo's own
# source and would produce noise (or enormous walks).
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".next",
        ".venv",
        "venv",
        "__pycache__",
        ".claude",
        ".cursor",
        # The reusable workflow checks the caller repo out at the workspace root
        # and this repo's script into a sibling `_agents_md_integrity/` dir; skip
        # that so the checker never scans its own copy of this repo.
        "_agents_md_integrity",
    }
)


class ExcludeConfigError(Exception):
    """An `--exclude` glob is not usable (today: it would exclude the root)."""


def _split_patterns(values):
    """Flatten repeated / comma- / newline-separated `--exclude` values.

    The workflow hands the whole `exclude_paths` input over as ONE argument, so
    a value may itself be a multi-line or comma-separated list. Blank entries
    are dropped, which is what makes an empty input a true no-op.
    """
    patterns = []
    for value in values or ():
        for chunk in re.split(r"[,\n\r]", value):
            chunk = chunk.strip()
            if chunk:
                patterns.append(chunk)
    return patterns


def _exclude_pattern_to_regex(pattern):
    """Translate one exclusion glob into an anchored full-match regex.

    Deliberately narrower than the CODEOWNERS translation above: an exclusion
    glob is ALWAYS repo-root-relative (no match-the-basename-at-any-depth
    magic), because a glob that silently matched deeper than intended would
    delete coverage nobody asked to drop. `*`/`?` match within one path
    segment, `**` matches across ZERO or more segments, and a leading `**/`
    means "at any depth". A glob that matches a directory excludes everything
    beneath it (the trailing group) — that is what makes `plugins` and
    `plugins/**` both prune the whole subtree. Redundant separators and a
    leading `/` or `./` are tolerated and stripped.

    Raises ExcludeConfigError for a glob that normalizes to nothing (`/`, `.`,
    `//`) or that is nothing but wildcard segments (`*`, `**`, `*/**`, `*/*`):
    both read as "exclude the whole repo", which is the one thing an exclusion
    must never do quietly. Without this, `*/**` would prune every top-level
    directory while `check_nested` still read `true`.
    """
    segs = [s for s in pattern.strip().split("/") if s not in ("", ".")]
    if not segs:
        raise ExcludeConfigError(
            f"exclusion glob '{pattern}' normalizes to the repo root, which is "
            f"not excludable. Name the subtree instead (e.g. 'plugins/**')."
        )
    if all(s in ("*", "**") for s in segs):
        raise ExcludeConfigError(
            f"exclusion glob '{pattern}' has no literal path segment, so it "
            f"prunes the tree wholesale instead of scoping a subtree — the "
            f"nested scan as a whole is not excludable this way. Name the "
            f"subtree instead (e.g. 'plugins/**')."
        )

    # A trailing `**` is redundant with the trailing subtree group below, and
    # keeping it costs real signal: `plugins/**` would then match only the
    # CHILDREN of `plugins`, so the walk prunes each child separately and emits
    # one EXCLUDED line per plugin instead of one for the subtree. Dropping it
    # is what makes `plugins` and `plugins/**` behave identically, as documented.
    while len(segs) > 1 and segs[-1] == "**":
        segs.pop()

    prefix = r""
    if segs[0] == "**":
        # Leading `**/` — "at any depth", zero leading segments included.
        prefix = r"(?:.*/)?"
        while segs[0] == "**":
            segs.pop(0)

    body = ""
    for seg in segs:
        if seg == "**":
            # An INTERIOR `**` spans zero or more whole segments, so the
            # mandatory separator belongs to the FOLLOWING literal rather than
            # to this group: `plugins/**/AGENTS.md` has to match
            # `plugins/AGENTS.md`, not only `plugins/<something>/AGENTS.md`.
            body += r"(?:/.*)?"
            continue
        esc = re.escape(seg).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        body += esc if not body else "/" + esc

    # Matched with `fullmatch` (never `match` + `$`): Python's `$` also accepts
    # a trailing newline, and POSIX permits a newline inside a path component,
    # so `plugins/demo` would otherwise prune a directory named "plugins/demo\n"
    # and drop coverage outside the configured subtree.
    return re.compile(prefix + body + r"(?:/.*)?", re.DOTALL)


def _compile_excludes(patterns):
    """Return [(glob, regex)] for each non-empty glob, preserving order.

    Blank entries are dropped here as well as in `_split_patterns`, so a config
    dict assembled by hand can't smuggle in an empty glob.
    """
    return [(p, _exclude_pattern_to_regex(p)) for p in patterns if p.strip()]


def _match_exclude(rel_path, excludes):
    """Return the first glob matching `rel_path`, or None."""
    for pattern, regex in excludes:
        if regex.fullmatch(rel_path):
            return pattern
    return None


def _validate_excludes(excludes, agents_file):
    """Reject any glob that would exclude the ROOT agents file or CLAUDE.md.

    Root compliance is the non-negotiable part of the standard, so this is a
    loud config error (exit 2), not one failure among many — a caller that
    writes `**` must be told it asked for something the checker will not do,
    rather than quietly getting a green run over an unchecked repo.
    """
    # Normalized to forward slashes like every other path the globs see (`_rel`,
    # `top_level_rel`) — on Windows a bare normpath of `docs/AGENTS.md` yields
    # `docs\AGENTS.md`, which a forward-slash-only glob can never match, so the
    # guard would silently never fire.
    protected = [os.path.normpath(agents_file).replace(os.sep, "/"), "CLAUDE.md"]
    for pattern, regex in excludes:
        for rel in protected:
            if regex.fullmatch(rel):
                raise ExcludeConfigError(
                    f"exclusion glob '{pattern}' would exclude the root "
                    f"'{rel}', which is not excludable — root AGENTS.md / "
                    f"CLAUDE.md compliance is the non-negotiable part of the "
                    f"standard. Narrow the glob (e.g. 'plugins/**')."
                )


def _count_lines(path):
    """Line count of a text file (a trailing newline doesn't add a phantom line)."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return len(f.read().splitlines())


def _has_import(path, import_token):
    """True if the file contains the `@AGENTS.md`-style import token on some line."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if import_token in line:
                return True
    return False


def _codeowners_pattern_to_regex(pattern):
    """Translate one CODEOWNERS glob into an anchored full-match regex.

    Follows the gitignore-ish semantics GitHub uses: a leading `/` anchors to
    the repo root; a pattern with no internal slash matches at any depth; a
    trailing `/` matches everything beneath the directory; `*` matches within a
    path segment, `**` across segments.
    """
    anchored = pattern.startswith("/")
    p = pattern[1:] if anchored else pattern
    p = p.rstrip("/")

    # Build the body segment-safely so `*` and `**` get distinct meanings.
    body = re.escape(p)
    body = body.replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")

    # An unanchored pattern with no internal slash matches the basename at any
    # depth; everything else anchors to the repo root. The trailing group lets
    # a directory pattern also match files beneath it.
    prefix = r"(?:.*/)?" if (not anchored and "/" not in p) else r""
    return re.compile(r"^" + prefix + body + r"(?:/.*)?$")


def _parse_codeowners(text):
    """Yield (regex, has_owner) for each rule line, in file order."""
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern = parts[0]
        owners = parts[1:]
        try:
            rules.append((_codeowners_pattern_to_regex(pattern), bool(owners)))
        except re.error:
            # A pattern we can't compile shouldn't crash the whole check.
            continue
    return rules


def _codeowners_owns(root, rel_path):
    """Return (checked, owned): whether a CODEOWNERS file exists and, if so,
    whether the last rule matching `rel_path` assigns an owner.

    Last-match-wins mirrors GitHub; a matching rule with no owners explicitly
    unassigns, so it counts as *not* owned.
    """
    for loc in CODEOWNERS_LOCATIONS:
        full = os.path.join(root, loc)
        if os.path.isfile(full):
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                rules = _parse_codeowners(f.read())
            owned = False
            for regex, has_owner in rules:
                if regex.match(rel_path):
                    owned = has_owner  # last match wins
            return True, owned
    return False, False


def _rel(root, path):
    """Repo-relative, normalized, forward-slash path — the form globs match."""
    return os.path.normpath(os.path.relpath(path, root)).replace(os.sep, "/")


def _scan_nested_agents(root, agents_basename, top_level_rel, excludes):
    """Find every nested AGENTS.md (not the top-level one), honoring exclusions.

    Returns (nested, excluded): repo-relative paths to check, and the
    (path, glob) pairs the exclusion globs pruned. `top_level_rel` is the
    configured agents_file path (normalized) so a pathful value like
    `docs/AGENTS.md` isn't also re-checked here as a "nested" file.

    Exclusions are applied DURING the walk, not as a post-filter on findings:
    an excluded directory is never descended into, so nothing inside it is ever
    opened or line-counted. `SKIP_DIRS` remains the always-on baseline;
    `excludes` is purely additive on top of it.
    """
    nested = []
    excluded = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if excludes:
            kept = []
            for d in dirnames:
                rel_dir = _rel(root, os.path.join(dirpath, d))
                match = _match_exclude(rel_dir, excludes)
                if match:
                    excluded.append((rel_dir, match))
                else:
                    kept.append(d)
            dirnames[:] = kept
        if agents_basename in filenames:
            rel = _rel(root, os.path.join(dirpath, agents_basename))
            if rel == top_level_rel:  # skip the top-level file
                continue
            match = _match_exclude(rel, excludes)
            if match:
                excluded.append((rel, match))
            else:
                nested.append(rel)
    return nested, excluded


def run_checks(root, config):
    """Run every integrity check against `root`.

    Returns (failures, warnings, exclusions): two lists of human-readable
    strings plus the (path, glob) pairs the nested walk excluded. An empty
    `failures` list means the repo passes; warnings never fail the check.

    Raises ExcludeConfigError when `config["exclude"]` contains a glob that
    would exclude the root agents file or CLAUDE.md.
    """
    failures = []
    warnings = []
    exclusions = []

    agents_file = config["agents_file"]
    agents_basename = os.path.basename(agents_file)
    import_token = "@" + agents_basename
    max_lines = config["max_lines"]
    warn_lines = config["warn_lines"]

    # Validated unconditionally — a root-excluding glob is a config error even
    # when `check_nested` is off and the globs would never have been consulted.
    excludes = _compile_excludes(config.get("exclude") or [])
    _validate_excludes(excludes, agents_file)
    if excludes and not config["check_nested"]:
        # Both knobs set means the caller narrowed an exclusion they think is
        # scoping coverage while nested checking is off for the WHOLE repo —
        # exactly the invisible coverage loss exclusions exist to replace.
        warnings.append(
            "exclusion globs are configured but `check_nested` is false, so "
            "they exclude nothing — nested checking is already off for the "
            "entire repo. Re-enable `check_nested` to use the exclusions."
        )

    agents_path = os.path.join(root, agents_file)

    # 1. Exists.
    if not os.path.isfile(agents_path):
        failures.append(
            f"'{agents_file}' not found at the repo root. It is the required "
            f"source of truth for agent instructions."
        )
        # Without the file, the line/shim/nested checks below have nothing to
        # anchor on, but CODEOWNERS/cursorrules are still worth reporting, so
        # keep going rather than returning early.
    else:
        # 2. Line ceiling (+ aspirational warn).
        n = _count_lines(agents_path)
        if n > max_lines:
            failures.append(
                f"'{agents_file}' is {n} lines, over the hard ceiling of "
                f"{max_lines}. Trim it — AGENTS.md must stay thin."
            )
        elif n > warn_lines:
            warnings.append(
                f"'{agents_file}' is {n} lines, over the aspirational target "
                f"of {warn_lines} (hard ceiling {max_lines})."
            )

    # 3. CLAUDE.md shim. Claude Code reads only CLAUDE.md and does NOT fall
    # back to AGENTS.md, so a missing root shim means the repo's instructions
    # are invisible to it — the most common gap in the org audit. Only checked
    # when the agents file itself exists (check 1 already fired otherwise;
    # don't double-report).
    claude_path = os.path.join(root, "CLAUDE.md")
    if os.path.isfile(claude_path):
        if not _has_import(claude_path, import_token):
            failures.append(
                f"'CLAUDE.md' exists but has no '{import_token}' import line — "
                f"it is a divergent copy. Make it a thin shim whose first line "
                f"is '{import_token}' (Claude-only notes may follow)."
            )
    elif config["require_shim"] and os.path.isfile(agents_path):
        failures.append(
            f"no root 'CLAUDE.md' shim. Claude Code reads only 'CLAUDE.md' "
            f"and does not fall back to '{agents_basename}', so this repo's "
            f"agent instructions are invisible to it. Add a one-line shim "
            f"containing '{import_token}'."
        )

    # 4. No legacy .cursorrules.
    if config["forbid_cursorrules"]:
        cursorrules_path = os.path.join(root, ".cursorrules")
        if os.path.isfile(cursorrules_path):
            failures.append(
                "legacy '.cursorrules' file found at the repo root. Delete it — "
                f"'{agents_file}' is the single source of truth."
            )

    # 5. Nested AGENTS.md (monorepo).
    if config["check_nested"]:
        top_level_rel = os.path.normpath(agents_file).replace(os.sep, "/")
        nested, excluded = _scan_nested_agents(
            root, agents_basename, top_level_rel, excludes
        )
        exclusions = sorted(set(excluded))
        for rel in sorted(nested):
            nested_path = os.path.join(root, rel)
            sibling_claude = os.path.join(os.path.dirname(nested_path), "CLAUDE.md")
            if not (
                os.path.isfile(sibling_claude)
                and _has_import(sibling_claude, import_token)
            ):
                failures.append(
                    f"nested '{rel}' has no sibling 'CLAUDE.md' containing "
                    f"'{import_token}', so Claude Code won't pick it up in that "
                    f"subtree. Add a one-line shim next to it."
                )
            n = _count_lines(nested_path)
            if n > max_lines:
                failures.append(
                    f"nested '{rel}' is {n} lines, over the hard ceiling of "
                    f"{max_lines}."
                )

    # 6. CODEOWNERS / DRI (warn unless require_codeowners).
    checked, owned = _codeowners_owns(root, agents_file)
    if not owned:
        if not checked:
            msg = (
                f"no CODEOWNERS file found, so '{agents_file}' has no DRI. Add a "
                f"CODEOWNERS rule assigning an owner."
            )
        else:
            msg = (
                f"'{agents_file}' is not matched by any CODEOWNERS rule (no "
                f"owner/DRI). Add a rule so it has a single owner."
            )
        if config["require_codeowners"]:
            failures.append(msg)
        else:
            warnings.append(msg)

    return failures, warnings, exclusions


def _env_bool(name, default):
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _esc_cmd(text):
    """Escape a value before it is interpolated into a workflow-command line.

    Every path here comes from the scanned repo tree, which a PR author
    controls, and POSIX/git permit a newline inside a path component. Unescaped,
    a directory named "x\\n::stop-commands::tok" would close this line and emit a
    SECOND, attacker-chosen workflow command — suppressing the `::error::`
    annotations printed just below, or forging notices in a public log. Applied
    to the plain line too, since that line would equally start a `::` command.
    """
    return str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _emit(failures, warnings, exclusions=()):
    """Print human lines plus GitHub Actions annotations, and return exit code.

    Exclusions are printed FIRST and annotated as notices: an exclusion that
    leaves no trace in the log is how coverage rots invisibly, so every subtree
    the walk skipped is named alongside the glob that skipped it.
    """
    for path, pattern in exclusions:
        line = f"EXCLUDED: {_esc_cmd(path)} (matched {_esc_cmd(pattern)})"
        print(line)
        print(f"::notice::AGENTS.md integrity: {line}")
    for w in warnings:
        w = _esc_cmd(w)
        print(f"WARN: {w}")
        print(f"::warning::AGENTS.md integrity: {w}")
    for f in failures:
        f = _esc_cmd(f)
        print(f"FAIL: {f}")
        print(f"::error::AGENTS.md integrity: {f}")

    if exclusions:
        print(
            f"\n{len(exclusions)} path(s) excluded from the nested scan "
            f"by --exclude."
        )

    if failures:
        print(f"\nResult: {len(failures)} check(s) failed.")
        return 1
    if warnings:
        print(f"\nResult: passed with {len(warnings)} warning(s).")
    else:
        print("\nResult: AGENTS.md integrity OK.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check AGENTS.md integrity.")
    parser.add_argument(
        "--root",
        default=os.environ.get("AGENTS_CHECK_ROOT", "."),
        help="Repo root to check (default: current directory).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help=(
            "Path glob to exclude from the NESTED AGENTS.md scan, relative to "
            "the repo root (e.g. 'plugins/**'). Repeatable; a single value may "
            "also be comma- or newline-separated. Additive on top of the "
            "always-on SKIP_DIRS baseline. A glob matching the root agents "
            "file or CLAUDE.md is rejected."
        ),
    )
    args = parser.parse_args(argv)

    config = {
        "agents_file": os.environ.get("AGENTS_FILE", "AGENTS.md") or "AGENTS.md",
        "max_lines": _env_int("MAX_LINES", 200),
        "warn_lines": _env_int("WARN_LINES", 150),
        "forbid_cursorrules": _env_bool("FORBID_CURSORRULES", True),
        "check_nested": _env_bool("CHECK_NESTED", True),
        "require_shim": _env_bool("REQUIRE_SHIM", True),
        "require_codeowners": _env_bool("REQUIRE_CODEOWNERS", False),
        "exclude": _split_patterns(args.exclude),
    }

    print(f"Checking AGENTS.md integrity in '{args.root}'...")
    # Echo the CONFIGURED globs, not just the paths they hit: a typo'd glob
    # that matches nothing must still be visible in the log.
    if config["exclude"]:
        print("Exclusion globs: " + ", ".join(_esc_cmd(g) for g in config["exclude"]))
    print()

    try:
        failures, warnings, exclusions = run_checks(args.root, config)
    except ExcludeConfigError as exc:
        # The glob is echoed back in the message and comes from the caller's
        # workflow file, which a `pull_request` run reads from the merge ref —
        # so it gets the same workflow-command escaping as scanned paths.
        msg = _esc_cmd(exc)
        print(f"FAIL: {msg}")
        print(f"::error::AGENTS.md integrity: {msg}")
        print("\nResult: invalid --exclude configuration.")
        return 2

    return _emit(failures, warnings, exclusions)


if __name__ == "__main__":
    sys.exit(main())
