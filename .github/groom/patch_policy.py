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
    "makefile",  # GNU Make searches GNUmakefile, makefile, Makefile — and PREFERS
    #              lowercase `makefile` over `Makefile`, so it must be denied too.
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
    "rakefile",  # `rake` accepts the lowercase form too.
    "CMakeLists.txt",
    "WORKSPACE",
    "WORKSPACE.bazel",
    "BUILD",
    "BUILD.bazel",
    "Jenkinsfile",
    "Taskfile.yml",
    "Taskfile.yaml",
    "taskfile.yml",  # go-task also loads the lowercase spellings.
    "taskfile.yaml",
    "justfile",
    "Justfile",
    # Gradle wrapper scripts: Gradle CI invokes `./gradlew` (or `gradlew.bat` on
    # Windows) directly, so an edited wrapper runs arbitrary code. These are at the
    # repo root with no extension, so the `*.gradle` glob and `gradle/wrapper/`
    # segment (which cover the jar/properties) do NOT reach them — deny by name.
    "gradlew",
    "gradlew.bat",
    # Go: `go build`/`go test` in CI re-fetch modules from these; a `replace`
    # directive in go.mod can redirect a dependency to attacker-controlled code.
    "go.mod",
    "go.sum",
    "go.work",
    "go.work.sum",
    "Package.resolved",  # SwiftPM pins resolved from this on `swift build`.
    ".pnpmfile.cjs",     # pnpm executes this hook during install.
)

# Path suffixes anchored on a full segment boundary (basenames won't do — these
# carry a directory component). `.cargo/config[.toml]` can set a build `runner` or
# linker that executes during `cargo build`/`cargo test` in pre-review CI.
_PATH_SUFFIXES = (
    r"\.cargo/config\.toml",
    r"\.cargo/config",
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
    # Multi-Dockerfile conventions CI builds via `docker build -f`: the
    # `Dockerfile.<tag>` suffix form (Dockerfile.prod) and the `<tag>.Dockerfile`
    # extension form (prod.Dockerfile). `Dockerfile*` also over-catches
    # `Dockerfile.md`-style docs — a safe over-block (downgrades to an issue).
    "Dockerfile*",
    "*.Dockerfile",
)


def _glob_to_regex(glob: str) -> str:
    """Translate a basename glob to a regex fragment (`*` never crosses `/`)."""
    return re.escape(glob).replace(r"\*", r"[^/]*")


# IGNORECASE: macOS and Windows CI runners use case-INSENSITIVE filesystems, so a
# builder can commit `PACKAGE.JSON` / `MAKEFILE` / `DOCKERFILE` which a
# case-sensitive Linux checker would miss but which resolves to the real,
# CI-executed file once checked out on the runner. The policy explicitly targets
# Swift/Xcode (macOS) callers, so match case-insensitively. This also over-blocks
# some lowercase near-collisions (e.g. a file literally named `build`) — the safe
# direction per the module contract (over-block only downgrades a PR to an issue).
_PATTERN = re.compile(
    "|".join(
        [rf"^{p}" for p in _ROOT_PREFIXES]
        + [rf"(?:^|/){p}" for p in _SEGMENT_PREFIXES]
        + [rf"(?:^|/){re.escape(b)}$" for b in _BASENAMES]
        + [rf"(?:^|/){_glob_to_regex(g)}$" for g in _BASENAME_GLOBS]
        + [rf"(?:^|/){s}$" for s in _PATH_SUFFIXES]
    ),
    re.IGNORECASE,
)


def _is_denied(path: str) -> bool:
    """True if `path` (or any of its newline-split lines) is CI-privileged."""
    # Split on newlines and test every line: git -z can emit a path containing a
    # raw newline, and the old grep matched line-by-line. Testing every line
    # over-blocks such a path, never under-blocks (the safe direction).
    return any(_PATTERN.search(line) for line in path.split("\n"))


def denied_paths(paths: Iterable[str]) -> list[str]:
    """Return the subset of `paths` that touch a CI-privileged surface."""
    # A bare `str` is iterable; without this guard a caller that passes one path as
    # a string would iterate its characters and silently under-block. Fail loud.
    if isinstance(paths, (str, bytes)):
        raise TypeError("denied_paths expects an iterable of paths, not a single str/bytes")
    return [p for p in paths if _is_denied(p)]


def parse_nul_delimited(data: bytes) -> list[str]:
    """Decode `git ... -z` output (NUL-delimited raw path bytes) to str paths.

    `-z` emits paths verbatim with NO C-quoting, so bytes are decoded with
    `surrogateescape` (which never raises) to preserve exotic/non-UTF-8 paths for
    matching. Empty fields (e.g. the trailing one after the final NUL) are dropped.
    """
    return [chunk.decode("utf-8", "surrogateescape") for chunk in data.split(b"\x00") if chunk]


def main() -> int:
    # Write raw bytes, not text: `parse_nul_delimited` decodes with `surrogateescape`,
    # so a denied path carrying non-UTF-8 bytes holds lone surrogates that the default
    # strict `sys.stdout` (text) would reject with UnicodeEncodeError — crashing on the
    # very path we must report. Round-trip through the same codec so a non-UTF-8
    # CI-privileged path is still emitted (and thus still blocked), never dropped.
    out = sys.stdout.buffer
    for path in denied_paths(parse_nul_delimited(sys.stdin.buffer.read())):
        out.write(path.encode("utf-8", "surrogateescape") + b"\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
