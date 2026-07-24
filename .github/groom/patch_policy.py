#!/usr/bin/env python3
r"""CI-privileged patch-path policy for the groom auto-builder (BE-4404).

The groom `build` job hands a prompt-injectable, credential-free builder agent a
finding and lets it edit a checkout; a later job opens a **review-gated** PR from
that patch. Review gates the *merge*, not CI *execution* — a same-repo branch
push runs the caller's CI (with repository secrets + a writable token) BEFORE any
human reads the diff. So a patch that touches a path the caller's pre-review CI
*executes* is a code-execution primitive, not a doc change. This module is the
deny-list that downgrades such a patch from an auto-PR to a filed issue (a human
authors the privileged change instead).

`denied_paths(paths)` returns the subset of changed paths that are CI-privileged;
`main()` reads NUL-delimited paths from stdin (the `git diff --cached
--name-only -z` producer in groom.yml's `Capture patch` step) and prints the
matches, exit 0 always — the caller tests non-emptiness, not the exit code.

Two load-bearing invariants (see also the trimmed comment in groom.yml):

1. **Conservative DEFAULT, not proof of completeness.** The list covers the paths
   a *typical* repo's CI executes (workflow/action defs, package manifests,
   lockfiles, build/test config across ecosystems). It is target-repo-specific:
   a repo whose CI runs something else privileged (a checked-in `scripts/`
   entrypoint) must add it here. Erring WIDE is the safe direction — a false
   positive only downgrades a legit refactor from a PR to an issue, and the
   finding still lands in the ledger + a `groom` issue; nothing is dropped. Never
   under-block.

2. **NUL-delimited comparison is mandatory.** git C-quotes and double-quote-wraps
   any path containing a quote, backslash or control byte in its DEFAULT output,
   so `.github/workflows/ev"il.yml` is emitted as `".github/workflows/ev\"il.yml"`
   whose leading quote slips past a `^\.github/` anchor. `-z` emits raw bytes with
   no quoting; this module reads those bytes. A path may itself contain a newline
   (`-z` does not escape it), so each path is split on newlines and EVERY line is
   tested — mirroring the old `tr '\0' '\n' | grep` line semantics; that
   over-blocks a newline-bearing path, never under-blocks (the safe direction).

Run/test: python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
"""

import re
import sys
from collections.abc import Iterable

# --- Patterns ---------------------------------------------------------------
# Ported verbatim from the inline `grep -E` the `Capture patch` step used before
# BE-4404 (root-anchored `.github/` prefixes + the basename set), then extended.

# Path prefixes matched ROOT-anchored only (verbatim port — `^\.github/...`).
_ROOT_PREFIXES = (
    r"\.github/workflows/",
    r"\.github/actions/",
)

# Directory segments privileged wherever they appear in the path
# (`.husky/pre-commit`, `frontend/.husky/...`, `android/gradle/wrapper/...`).
_SEGMENT_PREFIXES = (
    r"\.husky/",
    r"gradle/wrapper/",
)

# Exact basenames, privileged at ANY depth (`package.json`, `sub/package.json`).
_BASENAMES = (
    # --- ported verbatim (pre-BE-4404) ---
    "package.json",
    "Makefile",
    "GNUmakefile",
    "conftest.py",
    "noxfile.py",
    "tox.ini",
    "pytest.ini",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "Dockerfile",
    ".pre-commit-config.yaml",
    # --- added by BE-4404: lockfiles ---
    # `npm ci` / `yarn install` / `pip install -r` re-resolve dependency tarballs
    # from these on every CI run, so a rewritten `resolved` URL + `integrity` pair
    # executes an attacker tarball's postinstall in credentialed CI before review
    # (the BE-4012 headline gap — the pilot caller runs `npm ci` on every push).
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    # --- added by BE-4404: other build/test config that runs in pre-review CI ---
    "action.yml",
    "action.yaml",
    ".gitmodules",
    "Pipfile",
    "Cargo.toml",
    "build.rs",
    "Package.swift",
    "settings.gradle",  # also covered by the *.gradle glob; explicit for clarity
    "Gemfile",
    "Rakefile",
    "CMakeLists.txt",
    "WORKSPACE",
    "WORKSPACE.bazel",
    "BUILD",
    "BUILD.bazel",
    "Jenkinsfile",
    "Taskfile.yml",
    "justfile",
    "Justfile",
)

# Basename globs (`*` = any run of non-`/` chars), privileged at ANY depth.
_BASENAME_GLOBS = (
    "requirements*.txt",  # requirements.txt, requirements-dev.txt, ...
    "*.pbxproj",
    "*.gradle",
    "*.gradle.kts",
    "*.gemspec",
    "*.cmake",
    "*.bzl",
)


def _glob_to_regex(glob: str) -> str:
    """Translate a basename glob to a regex fragment (`*` never crosses `/`)."""
    return re.escape(glob).replace(r"\*", r"[^/]*")


_PATTERN = re.compile(
    "|".join(
        [rf"^{p}" for p in _ROOT_PREFIXES]
        + [rf"(?:^|/){p}" for p in _SEGMENT_PREFIXES]
        + [rf"(?:^|/){re.escape(b)}$" for b in _BASENAMES]
        + [rf"(?:^|/){_glob_to_regex(g)}$" for g in _BASENAME_GLOBS]
    )
)


def _is_denied(path: str) -> bool:
    """True if `path` (or any of its newline-split lines) is CI-privileged."""
    # Split on newlines and test every line: git -z can emit a path containing a
    # raw newline, and the old grep matched line-by-line. Testing every line
    # over-blocks such a path, never under-blocks (the safe direction).
    return any(_PATTERN.search(line) for line in path.split("\n"))


def denied_paths(paths: Iterable[str]) -> list[str]:
    """Return the subset of `paths` that touch a CI-privileged surface."""
    return [p for p in paths if _is_denied(p)]


def parse_nul_delimited(data: bytes) -> list[str]:
    """Decode `git ... -z` output (NUL-delimited raw path bytes) to str paths.

    `-z` emits paths verbatim with NO C-quoting, so bytes are decoded with
    `surrogateescape` (which never raises) to preserve exotic/non-UTF-8 paths for
    matching. Empty fields (e.g. the trailing one after the final NUL) are dropped.
    """
    return [chunk.decode("utf-8", "surrogateescape") for chunk in data.split(b"\x00") if chunk]


def main() -> int:
    for path in denied_paths(parse_nul_delimited(sys.stdin.buffer.read())):
        sys.stdout.write(path + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
