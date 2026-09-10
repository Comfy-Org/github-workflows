#!/usr/bin/env python3
r"""CI-privileged patch-path policy for the groom auto-builder (BE-4404).

The groom `build` job hands a prompt-injectable, credential-free builder agent a
finding and lets it edit a checkout; a later job opens a **review-gated** PR from
that patch. Review gates the *merge*, not CI *execution* — a same-repo branch
push runs the caller's CI (with repository secrets + a writable token) BEFORE any
human reads the diff. So a patch that touches a path the caller's pre-review CI
*executes* is a code-execution primitive, not a doc change. This module is the
deny-list that downgrades such a patch from an auto-PR to a filed issue (a human
authors the privileged change instead). It also covers owner-gated
**dataset-of-record** paths (BE-9609): graded eval cases whose merge publishes
immutable versions and whose changes are reserved for the dataset owner — not a
CI-execution risk, but the same downgrade-to-issue treatment.

`denied_paths(paths)` returns the subset of changed paths in either class;
`denied_entries(entries)` additionally denies, by MODE, what path shape cannot
see — a symlink-typed change in a `suites` tree, the indirection that would
point a dataset importer's glob at an undenied tree. `main()` reads raw-diff
records from stdin (the `git diff --cached --no-renames --raw -z` producer in
groom.yml's `Capture patch` step — `--raw`, not `--name-only`, precisely so the
mode bits reach the policy) and prints the denied paths, exit 0 always — the
caller tests non-emptiness, not the exit code.

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
   (`-z` does not escape it), so each path is tested WHOLE **and** split on
   newlines with EVERY line tested — the split mirrors the old
   `tr '\0' '\n' | grep` line semantics, and the whole-path test catches a match
   that straddles the newline (which the split alone would miss for any
   `$`-anchored branch). Both directions only ADD denials — over-block, never
   under-block (the safe direction).

   The producer must also pass `--no-renames` (see groom.yml): with git's default
   rename detection a rename collapses into ONE record naming only the
   destination-side pairing, so moving a denied path to an undenied one hides the
   source from the policy. `--no-renames` emits both sides as delete+add. (A
   rename record also carries TWO path fields, which `parse_raw_z` would reject
   as misaligned — failing closed, not open.)

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

# Dataset-of-record paths (BE-9609). NOT CI-privileged — these are graded eval
# cases whose merge publishes IMMUTABLE, owner-owned versions downstream, a change
# class a caller reserves for the dataset owner. A builder patch here is downgraded
# to an issue so a human authors it. Deliberately WIDE per invariant 1 (an
# over-block only downgrades a PR to an issue; an under-block is the hole):
#   * segment-anchored (`(?:^|/)`), so a nested `suites/` tree is covered;
#   * `(?:[^/]+/)*` — ZERO or more segments between `suites/` and `cases/`, so
#     grouped or versioned layouts (`suites/eval/agent/cases/`,
#     `suites/agent/v2/cases/`) are not a bypass, and a flat `suites/cases/`
#     layout is denied too — every description of this class (README, groom.yml)
#     says `suites/**/cases/`, and `**` spans zero segments;
#   * `(?s:.*)` tail — any depth under `cases/`, an empty stem (`cases/.yaml`),
#     and a raw newline in the name (see `_is_denied`: a glob-driven importer
#     still picks such a file up, so the gate must too);
#   * second alternative: a change whose FINAL segment is `cases` under a suite.
#     git tracks no directories, so that path shape is a file or a SYMLINK — one
#     of the indirections that would let a builder point `suites/<x>/cases` at an
#     undenied directory. Path shape alone cannot catch a symlink at any OTHER
#     component of the importer's glob (`suites` itself, a suite dir, a
#     non-YAML name inside `cases/`) — those are caught by MODE, not path:
#     see `denied_entries`, which denies any symlink-typed change carrying a
#     `suites` segment.
_DATASET_OF_RECORD_REGEXES = (
    r"suites/(?:[^/]+/)*cases/(?s:.*)\.ya?ml",
    r"suites/(?:[^/]+/)*cases",
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
        + [rf"(?:^|/){r}$" for r in _DATASET_OF_RECORD_REGEXES]
    ),
    re.IGNORECASE,
)


def _is_denied(path: str) -> bool:
    """True if `path` — whole, or any of its newline-split lines — is denied."""
    # Split on newlines and test every line: git -z can emit a path containing a
    # raw newline, and the old grep matched line-by-line. Testing every line
    # over-blocks such a path, never under-blocks (the safe direction).
    #
    # ALSO test the unsplit path. Splitting alone under-blocks any `$`-anchored
    # branch whose match would STRADDLE the newline: `suites/s/cases/a\nb.yaml`
    # splits into `suites/s/cases/a` + `b.yaml`, and neither line matches — yet a
    # `suites/*/cases/*.yaml` glob importer happily reads that file. The
    # dataset-of-record tail uses `(?s:.*)` so it matches across the newline here.
    # Strictly widening: this only ever adds denials, per invariant 1.
    return any(_PATTERN.search(s) for s in (path, *path.split("\n")))


def denied_paths(paths: Iterable[str]) -> list[str]:
    """Return the subset of `paths` a human must author, not the builder.

    Two distinct classes, deliberately returned UNDIFFERENTIATED because the sole
    consumer (groom.yml's `Capture patch` gate) only tests non-emptiness: paths
    the caller's pre-review CI EXECUTES, and owner-gated dataset-of-record paths.
    Both get the same downgrade-to-issue treatment. A future caller that needs to
    ask "would this path execute in pre-review CI?" must return the class too —
    do not infer it from membership in this list.
    """
    # A bare `str` is iterable; without this guard a caller that passes one path as
    # a string would iterate its characters and silently under-block. Fail loud.
    if isinstance(paths, (str, bytes)):
        raise TypeError("denied_paths expects an iterable of paths, not a single str/bytes")
    return [p for p in paths if _is_denied(p)]


# `git diff --raw` meta record: `:<oldmode> <newmode> <oldsha> <newsha> <status>`.
# Modes are 6 octal digits (`100644`, `120000`, `000000`); shas may be
# abbreviated (with trailing dots), and with `--no-renames` the status is a
# single letter with no score — only the two modes are consumed, so match just
# the record's leading shape.
_RAW_META = re.compile(r"\A:([0-7]{6}) ([0-7]{6}) ")

_SYMLINK_MODE = "120000"


def _has_suites_segment(path: str) -> bool:
    """True if any `/`-separated segment of `path` (or of any of its
    newline-split lines — mirroring `_is_denied`'s over-blocking split) is
    `suites`, case-insensitively (case-insensitive runners; see `_PATTERN`)."""
    return any(
        seg.lower() == "suites"
        for line in (path, *path.split("\n"))
        for seg in line.split("/")
    )


def parse_raw_z(data: bytes) -> list[tuple[str, str, str]]:
    """Parse `git diff --raw --no-renames -z` output to (old_mode, new_mode, path).

    In `-z` mode each entry is `:<oldmode> <newmode> <oldsha> <newsha> <status>`
    NUL `<path>` NUL — meta and path strictly alternate (with `--no-renames`
    there are no two-path R/C entries). `-z` emits path bytes verbatim with NO
    C-quoting; they are decoded with `surrogateescape` (which never raises) to
    preserve exotic/non-UTF-8 paths for matching. A field where a meta
    record must sit but doesn't parse raises ValueError: under the caller's
    `set -euo pipefail` that aborts the gate, which fails CLOSED (no PR opens) —
    never silently skip an entry, which would fail open.
    """
    fields = data.split(b"\x00")
    # git terminates the final path with NUL, leaving one trailing empty field.
    if fields and not fields[-1]:
        fields.pop()
    if len(fields) % 2:
        raise ValueError("truncated --raw -z input: unpaired meta/path fields")
    entries = []
    for meta_bytes, path_bytes in zip(fields[::2], fields[1::2]):
        meta = _RAW_META.match(meta_bytes.decode("utf-8", "surrogateescape"))
        if not meta:
            raise ValueError(f"malformed --raw -z meta record: {meta_bytes[:80]!r}")
        entries.append(
            (meta.group(1), meta.group(2), path_bytes.decode("utf-8", "surrogateescape"))
        )
    return entries


def denied_entries(entries: Iterable[tuple[str, str, str]]) -> list[str]:
    """Return the denied paths among (old_mode, new_mode, path) raw-diff entries.

    Everything `denied_paths` denies by PATH, plus the mode-visible class path
    shape cannot express: a SYMLINK-typed change anywhere in a `suites` tree.
    A symlink at any component the dataset importer's `suites/**/cases/*.y*ml`
    glob traverses (`suites` itself, a suite dir, a link inside `cases/` with no
    YAML tail) redirects resolution to an undenied tree holding builder-authored
    cases — the path regexes only see the shapes ending in `cases`/`.ya?ml`.
    Either mode being `120000` denies: a change FROM a symlink is a resolution
    change too, and over-blocking is the safe direction (invariant 1).

    RESIDUAL (recorded, not shut): this sees only the BUILDER'S diff. If the
    repo already carries a human-authored symlink pointing a suites component at
    an outside tree, a builder file added under that TARGET tree is importable
    without any denied path or mode appearing in the diff. That is invariant 1's
    territory: the deny-list is a conservative default, and a caller whose
    dataset surface extends beyond literal `suites/` paths must extend it.
    """
    return [
        path
        for old_mode, new_mode, path in entries
        if _is_denied(path)
        or (_SYMLINK_MODE in (old_mode, new_mode) and _has_suites_segment(path))
    ]


def main() -> int:
    # Write raw bytes, not text: the parsers decode with `surrogateescape`, so a
    # denied path carrying non-UTF-8 bytes holds lone surrogates that the default
    # strict `sys.stdout` (text) would reject with UnicodeEncodeError — crashing on
    # the very path we must report. Round-trip through the same codec so a non-UTF-8
    # CI-privileged path is still emitted (and thus still blocked), never dropped.
    out = sys.stdout.buffer
    for path in denied_entries(parse_raw_z(sys.stdin.buffer.read())):
        out.write(path.encode("utf-8", "surrogateescape") + b"\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
