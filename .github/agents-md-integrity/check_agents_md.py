#!/usr/bin/env python3
"""Check a repo's AGENTS.md against the Comfy AGENTS.md standard.

The standard (Comfy Engineering Guide, "AGENTS.md, done right", §10): one thin
top-level `AGENTS.md` is the single source of truth, `CLAUDE.md` is a one-line
`@AGENTS.md` shim (optionally with a few Claude-only lines below), there are no
divergent `.cursorrules`, and the file stays under a hard line ceiling (200,
per Anthropic guidance) with an aspirational target (150). In a monorepo every
nested `AGENTS.md` gets a sibling `CLAUDE.md` shim so Claude Code picks it up in
that subtree, and the file is owned by a DRI via CODEOWNERS.

This script enforces that mechanically so it can wire into CI as a required
status check. It operates on a checked-out repo tree (the CALLER's repo when
run from the reusable workflow) and exits non-zero when any hard check fails.

Run locally:
    python3 .github/agents-md-integrity/check_agents_md.py --root .
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


def _iter_nested_agents(root, agents_basename, top_level_rel):
    """Yield repo-relative paths of every nested AGENTS.md (not the top-level one).

    `top_level_rel` is the configured agents_file path (normalized) so a pathful
    value like `docs/AGENTS.md` isn't also re-checked here as a "nested" file.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if agents_basename in filenames:
            rel = os.path.relpath(os.path.join(dirpath, agents_basename), root)
            if os.path.normpath(rel) != top_level_rel:  # skip the top-level file
                yield rel


def run_checks(root, config):
    """Run every integrity check against `root`.

    Returns (failures, warnings): two lists of human-readable strings. An empty
    `failures` list means the repo passes; warnings never fail the check.
    """
    failures = []
    warnings = []

    agents_file = config["agents_file"]
    agents_basename = os.path.basename(agents_file)
    import_token = "@" + agents_basename
    max_lines = config["max_lines"]
    warn_lines = config["warn_lines"]

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

    # 3. CLAUDE.md shim.
    claude_path = os.path.join(root, "CLAUDE.md")
    if os.path.isfile(claude_path):
        if not _has_import(claude_path, import_token):
            failures.append(
                f"'CLAUDE.md' exists but has no '{import_token}' import line — "
                f"it is a divergent copy. Make it a thin shim whose first line "
                f"is '{import_token}' (Claude-only notes may follow)."
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
        top_level_rel = os.path.normpath(agents_file)
        for rel in sorted(_iter_nested_agents(root, agents_basename, top_level_rel)):
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

    return failures, warnings


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


def _emit(failures, warnings):
    """Print human lines plus GitHub Actions annotations, and return exit code."""
    for w in warnings:
        print(f"WARN: {w}")
        print(f"::warning::AGENTS.md integrity: {w}")
    for f in failures:
        print(f"FAIL: {f}")
        print(f"::error::AGENTS.md integrity: {f}")

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
    args = parser.parse_args(argv)

    config = {
        "agents_file": os.environ.get("AGENTS_FILE", "AGENTS.md") or "AGENTS.md",
        "max_lines": _env_int("MAX_LINES", 200),
        "warn_lines": _env_int("WARN_LINES", 150),
        "forbid_cursorrules": _env_bool("FORBID_CURSORRULES", True),
        "check_nested": _env_bool("CHECK_NESTED", True),
        "require_codeowners": _env_bool("REQUIRE_CODEOWNERS", False),
    }

    print(f"Checking AGENTS.md integrity in '{args.root}'...\n")
    failures, warnings = run_checks(args.root, config)
    return _emit(failures, warnings)


if __name__ == "__main__":
    sys.exit(main())
