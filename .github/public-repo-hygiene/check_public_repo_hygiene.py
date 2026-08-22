#!/usr/bin/env python3
"""Fail CI if a PUBLIC repo's tracked files carry internal-only references.

This is a lightweight regression guard, not a secrets scanner: it looks for
categories of internal-only references — ticket-style IDs, internal
collaboration-tool links, and Comfy-Org repo names outside a known-public
allowlist — not credentials. It uses small, explicit allow/deny lists instead
of one clever regex, so a false positive is a one-line list edit instead of a
mystery.

WHY IT LIVES HERE (BE-8654). It started as two copies — a Python one in the
Python SDK and a JavaScript one in the TypeScript SDK — each run from the PR's
OWN checkout. That had two defects. (1) A PR could weaken or delete the checker
that was judging it: the job checked the caller out and ran `python3
scripts/check_public_repo_hygiene.py`, so adding a private repo name to the
in-tree allowlist and then leaking it passed green. (2) "Which Comfy-Org repos
are public" is ORG-WIDE knowledge, and copying it per repo let the copies go
stale independently — both were missing `github-workflows` itself, which is
what surfaced this. Hosting the list here leaks nothing, because the list is
public repo names BY DESIGN: no private repo name is ever listed, which is the
whole point of default-deny.

The reusable workflow (`.github/workflows/public-repo-hygiene.yml`) loads this
file from a pinned ref of THIS repo, never from the caller's checkout, so a PR
in the caller repo cannot reach the checker or the allowlist below.

Per-repo tuning is a workflow INPUT, never a fork, and every knob is ADDITIVE:
`--ticket-allow` adds acronyms to the built-in ticket allowlist and
`--exclude` drops paths from the scan. Neither can remove a built-in entry, and
neither reaches the known-public repo/team allowlist at all — that one is not
caller-tunable in any form. Every exclusion is echoed to the run log, including
one that matched nothing, so coverage cannot rot invisibly.

Exit codes: 0 pass, 1 one or more internal-only references found, 2 bad config
(an unusable `--exclude` value, or a root that is not a git work tree).

Run locally:
    python3 .github/public-repo-hygiene/check_public_repo_hygiene.py --root .
    python3 .github/public-repo-hygiene/check_public_repo_hygiene.py --root . \
        --exclude 'src/generated/' --ticket-allow 'GPU-100'
"""

import argparse
import os
import re
import subprocess
import sys

# --- Category 1: ticket-shaped identifiers (TEAM-1234) ---------------------
# A generic SHAPE rather than a guessed list of real internal team keys, so we
# never have to encode (and thus disclose) an internal naming scheme here.
# Common tech acronyms that fit the shape are carved out below -- extend the
# allowlist, not the regex, when a legitimate term trips it.
TICKET_RE = re.compile(r"\b[A-Z]{2,6}-\d{2,6}\b")
TICKET_ALLOWLIST = frozenset(
    {
        "UTF-8",
        "ISO-8601",
        "SHA-256",
        "SHA-384",
        "SHA-512",
        "AES-128",
        "AES-256",
        "RFC-2119",
        "RFC-7231",
        "RFC-3339",
        "OAUTH-2",
        "IPV-4",
        "IPV-6",
        "X-25519",
        "WIN-32",
        "WIN-64",
    }
)

# --- Category 2: internal collaboration-tool links/markers -----------------
INTERNAL_MARKER_RES = (
    re.compile(r"notion\.(so|site)/", re.IGNORECASE),
    re.compile(r"slack\.com/(archives|client)/", re.IGNORECASE),
    re.compile(r"\bapp\.slack\.com\b", re.IGNORECASE),
    re.compile(r"docs\.google\.com/", re.IGNORECASE),
    re.compile(r"drive\.google\.com/", re.IGNORECASE),
    re.compile(r"app\.datadoghq\.com/", re.IGNORECASE),
    re.compile(r"\bposthog\.com/project/", re.IGNORECASE),
    re.compile(r"\blinear\.app/", re.IGNORECASE),
    re.compile(r"\bincident-\d+\b", re.IGNORECASE),
)

# --- Category 3: Comfy-Org repo references outside the known-public set ----
# Default-deny: only these are known to be public. Anything else under
# `Comfy-Org/<repo>` is flagged so a maintainer either scrubs it or adds it
# here once confirmed public (`gh repo view Comfy-Org/<name> --json visibility`).
# No private repo name is listed here on purpose -- the point of default-deny
# is that we never need to, which is also what makes this file safe to host in
# a public repo.
#
# This is the ORG-WIDE single source of truth. It is deliberately NOT a
# workflow input: an allowlist a caller could pass in would be an allowlist a
# PR in the caller repo could widen, which is the hole this file closes.
PUBLIC_COMFY_ORG_REPOS = frozenset(
    {
        "comfy-api-proxy",
        "comfy-cla",
        "comfy-cli",
        "comfy-cloud-mcp-server",
        "Comfy-Desktop",
        "comfy-python-sdk",
        "comfy-swift-sdk",
        "comfy-typescript-sdk",
        "ComfyUI_frontend",
        "ComfyUI",
        # BE-8654: this repo. Both SDK copies were missing it, so the caller
        # every one of them needs -- a pin at
        # `Comfy-Org/github-workflows/.github/workflows/...` -- failed the very
        # check it was being added alongside. Verified public.
        "github-workflows",
    }
)
# CODEOWNERS team handles (`@Comfy-Org/<team>`) are inherently public on a
# public repo -- GitHub renders CODEOWNERS owners to anyone who can see the
# repo, so listing them here is not a leak. An `@Comfy-Org/<team>` handle NOT
# in this set is still flagged, so a genuinely-internal team reference
# surfaces.
PUBLIC_COMFY_ORG_TEAMS = frozenset({"comfy-cloud-team", "core-engine-team"})

# Case-sensitive on the org segment, matching both scripts this replaces.
# GitHub resolves owner names case-insensitively, so a lowercased
# `comfy-org/<repo>` reference is a known blind spot -- see README.md
# "Known limitations". Widening it is a detection change, deliberately not
# bundled into the centralization.
REPO_REF_RE = re.compile(r"Comfy-Org/([A-Za-z0-9_.-]+)")

# Where the reusable workflow checks THIS repo out inside the caller's
# workspace (`path:` in public-repo-hygiene.yml — keep the two spellings in
# step). Normally it is UNTRACKED there, so `git ls-files` never lists it. It is
# skipped unconditionally anyway, for the one case where that is not true: a
# caller that happens to track a directory of that name would otherwise have
# THIS repo's own files scanned as if they were its own, and this repo is full
# of ticket ids and Comfy-Org references by design — a guaranteed false failure
# nobody could act on. The skip is reported like any other exclusion when it
# actually skips something, so it can never hide real coverage silently.
SCRIPT_CHECKOUT_DIR = "_public_repo_hygiene/"


class ConfigError(Exception):
    """A value passed by the caller is unusable (exit 2, not a finding)."""


def _split_values(values):
    """Flatten repeated / comma- / newline-separated CLI values into a list.

    The workflow hands each multi-value input over as ONE argument, so a single
    value may itself be a multi-line or comma-separated list. Blank entries are
    dropped, which is what makes an empty input a true no-op.
    """
    out = []
    for value in values or ():
        for chunk in re.split(r"[,\n\r]", value):
            chunk = chunk.strip()
            if chunk:
                out.append(chunk)
    return out


def _normalize_exclude(pattern):
    """Normalize one exclusion entry, or raise if it excludes the whole tree.

    Semantics are deliberately literal rather than glob -- they are exactly
    what the two scripts this replaces used (an exact-path set plus a
    directory-prefix tuple), so a migrating repo transcribes its lists instead
    of translating them:

      `path/to/file.py`  -> that exact tracked path
      `path/to/dir/`     -> that directory and everything beneath it

    A value that normalizes to the repo root (``/``, ``.``, ``./``, ``''``) is
    rejected: excluding everything is the one thing an exclusion must never do
    quietly, and a green run over an unscanned repo is worse than no check.
    """
    cleaned = pattern.strip()
    is_dir = cleaned.endswith("/")
    segs = [s for s in cleaned.split("/") if s not in ("", ".")]
    if not segs:
        raise ConfigError(
            f"exclusion '{pattern}' normalizes to the repo root, which is not "
            f"excludable. Name the file or subtree instead "
            f"(e.g. 'src/generated/')."
        )
    if ".." in segs:
        raise ConfigError(
            f"exclusion '{pattern}' contains '..'; exclusions are "
            f"repo-root-relative and never traverse upward."
        )
    norm = "/".join(segs)
    return norm + "/" if is_dir else norm


def _is_excluded(rel, excludes):
    """Return the first exclusion matching `rel`, or None.

    A directory entry matches the directory itself and everything under it; a
    file entry matches that exact path. Nothing here matches by basename at
    arbitrary depth -- an exclusion that silently reached deeper than intended
    would drop coverage nobody asked to drop.
    """
    for pattern in excludes:
        if pattern.endswith("/"):
            if rel == pattern[:-1] or rel.startswith(pattern):
                return pattern
        elif rel == pattern:
            return pattern
    return None


def tracked_files(root):
    """Tracked paths under `root`, NUL-delimited so odd names survive.

    `-z` is not a nicety: without it git C-quotes any path containing a
    newline, a quote or a non-ASCII byte, and the checker would then scan a
    path that does not exist (silently skipping the real file) -- a hole an
    author could park a leak in.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
    except FileNotFoundError as exc:  # pragma: no cover - git absent
        raise ConfigError(f"git is not available: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise ConfigError(
            f"'{root}' is not a git work tree (git ls-files failed: "
            f"{stderr or exc.returncode}). This checker scans TRACKED files, "
            f"so it needs the repo checkout, not an exported tree."
        ) from exc
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def _read_text(path):
    """Return (text, skip_reason). Exactly one of the two is None.

    Binary files are out of scope. A NUL byte is the crude-but-reliable marker
    (what the JavaScript copy used); an undecodable byte is the other (what the
    Python copy used). Honouring BOTH keeps the merged checker a superset of the
    two it replaces rather than a compromise between them.

    An UNREADABLE file is reported rather than dropped. Binary and non-UTF-8 are
    ordinary, expected skips; a tracked path the checker cannot open at all
    (a dangling symlink, a permission problem) is a hole in coverage, and a hole
    nobody is told about is where a leak sits unnoticed. `_emit` turns the
    reason into a `::warning::`.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return None, f"unreadable, so it was NOT scanned: {exc.strerror or exc}"
    if b"\x00" in data:
        return None, None  # binary
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, None  # not UTF-8 text


def check_file(root, rel, ticket_allowlist):
    """Return (findings, skip_reason) for one tracked file, in report order."""
    findings = []
    text, skipped = _read_text(os.path.join(root, rel))
    if text is None:
        return findings, skipped

    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in TICKET_RE.finditer(line):
            if match.group(0).upper() not in ticket_allowlist:
                findings.append(
                    f"{rel}:{lineno}: possible internal ticket ID: "
                    f"{match.group(0)!r}"
                )

        for pattern in INTERNAL_MARKER_RES:
            if pattern.search(line):
                findings.append(
                    f"{rel}:{lineno}: internal collaboration-tool marker: "
                    f"{line.strip()!r}"
                )

        for match in REPO_REF_RE.finditer(line):
            name = match.group(1)
            # A leading `@` makes this a CODEOWNERS team handle, not a repo ref.
            if match.start() > 0 and line[match.start() - 1] == "@":
                if name not in PUBLIC_COMFY_ORG_TEAMS:
                    findings.append(
                        f"{rel}:{lineno}: reference to @Comfy-Org/{name}, a "
                        "team not in the known-public allowlist "
                        "(Comfy-Org/github-workflows "
                        ".github/public-repo-hygiene/"
                        "check_public_repo_hygiene.py) -- confirm it's public "
                        "and add it, or remove the reference"
                    )
                continue
            # Strip a trailing `.git`: repository URLs (package.json
            # `repository.url`, git remotes) conventionally end in `.git`, and
            # `Foo.git` is still a reference to the public repo `Foo`.
            repo = re.sub(r"\.git$", "", name)
            if repo not in PUBLIC_COMFY_ORG_REPOS:
                findings.append(
                    f"{rel}:{lineno}: reference to Comfy-Org/{repo}, which is "
                    "not in the known-public allowlist "
                    "(Comfy-Org/github-workflows "
                    ".github/public-repo-hygiene/check_public_repo_hygiene.py)"
                    " -- confirm it's public and add it, or remove the "
                    "reference"
                )

    return findings, None


def run_checks(root, excludes=(), extra_ticket_allow=()):
    """Scan `root`, returning (findings, exclusion_counts, warnings).

    `exclusion_counts` is [(pattern, files_skipped)] in the caller's order,
    INCLUDING patterns that skipped nothing -- a typo'd exclusion that matches
    no file has to be visible in the log, not silently inert. The built-in
    SCRIPT_CHECKOUT_DIR skip is appended only when it actually skipped
    something, so it costs a log line only when it matters.
    """
    excludes = [_normalize_exclude(p) for p in excludes]
    # Additive, never a replacement: a caller can name extra acronyms, it can
    # never drop a built-in one.
    ticket_allowlist = TICKET_ALLOWLIST | {a.upper() for a in extra_ticket_allow}

    counts = {p: 0 for p in excludes}
    builtin_skipped = 0
    findings, warnings = [], []
    scanned = 0
    for rel in tracked_files(root):
        if _is_excluded(rel, (SCRIPT_CHECKOUT_DIR,)) is not None:
            builtin_skipped += 1
            continue
        hit = _is_excluded(rel, excludes)
        if hit is not None:
            counts[hit] += 1
            continue
        scanned += 1
        found, skipped = check_file(root, rel, ticket_allowlist)
        findings.extend(found)
        if skipped:
            warnings.append(f"{rel}: {skipped}")

    exclusions = [(p, counts[p]) for p in excludes]
    if builtin_skipped:
        exclusions.append((SCRIPT_CHECKOUT_DIR, builtin_skipped))
    if scanned == 0:
        # "Nothing to scan" is never the same as "clean". A repo whose every
        # tracked file was excluded, or that has no tracked files at all,
        # produces a green run that proves nothing -- say so out loud.
        warnings.append(
            "no files were scanned at all (nothing tracked, or everything "
            "excluded) -- this run proves nothing about the repo"
        )

    return findings, exclusions, warnings


def _esc_cmd(text):
    """Escape a value before interpolating it into a workflow-command line.

    Every path and excerpt here comes from the scanned repo tree, which a PR
    author controls, and POSIX/git permit a newline inside a path component.
    Unescaped, a file named "x\\n::stop-commands::tok" would close this line and
    emit a SECOND, attacker-chosen workflow command -- suppressing the
    `::error::` annotations printed just below, or forging notices in a public
    log. Applied to the plain line too, since that line would equally start a
    `::` command.
    """
    # `tracked_files` decodes with `surrogateescape`, so a path holding bytes
    # that are not valid UTF-8 arrives carrying lone surrogates. Printing one to
    # a strict-UTF-8 stdout raises UnicodeEncodeError, which would turn a
    # perfectly good finding into a traceback -- failing closed, but reporting
    # nothing a maintainer could act on. Round-trip it to a lossy but printable
    # form first.
    out = str(text).encode("utf-8", "backslashreplace").decode("utf-8")
    return out.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _emit(findings, exclusions=(), warnings=()):
    """Print human lines plus Actions annotations, and return the exit code."""
    for pattern, count in exclusions:
        line = f"EXCLUDED: {count} file(s) matched {_esc_cmd(pattern)!r}"
        print(line)
        print(f"::notice::public-repo-hygiene: {line}")

    for w in warnings:
        w = _esc_cmd(w)
        print(f"WARN: {w}")
        print(f"::warning::public-repo-hygiene: {w}")

    if not findings:
        print("\nResult: no internal-only references found.")
        return 0

    print("\nERROR: possible internal-only references found in this public repo:\n")
    for finding in findings:
        escaped = _esc_cmd(finding)
        print(f"  {escaped}")
        print(f"::error::public-repo-hygiene: {escaped}")
    print(
        "\nIf this is a genuine false positive, either add the acronym via the "
        "workflow's `ticket_allowlist:` input, or -- for a Comfy-Org repo you "
        "have CONFIRMED is public -- open a PR against "
        "Comfy-Org/github-workflows adding it to PUBLIC_COMFY_ORG_REPOS in "
        ".github/public-repo-hygiene/check_public_repo_hygiene.py. The repo "
        "allowlist is org-wide and deliberately not editable from a caller "
        "repo."
    )
    print(f"\nResult: {len(findings)} internal-only reference(s) found.")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scan a public repo's tracked files for internal-only references."
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("HYGIENE_CHECK_ROOT", "."),
        help="Repo root to scan (default: current directory).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Tracked path to skip: 'dir/' excludes that subtree, 'a/b.py' that "
            "exact file. Repeatable; a single value may also be comma- or "
            "newline-separated. A value naming the repo root is rejected."
        ),
    )
    parser.add_argument(
        "--ticket-allow",
        action="append",
        default=[],
        metavar="TOKEN",
        help=(
            "Extra acronym to treat as NOT a ticket ID (e.g. 'GPU-100'). "
            "Additive on top of the built-in allowlist; repeatable, and a "
            "single value may be comma- or newline-separated."
        ),
    )
    args = parser.parse_args(argv)

    excludes = _split_values(args.exclude)
    ticket_allow = _split_values(args.ticket_allow)

    print(f"Scanning tracked files in '{_esc_cmd(args.root)}' for internal-only references...")
    # Echo the CONFIGURED knobs, not only their effect: a caller-side tuning
    # value has to be visible in the run log of the check it tunes.
    if excludes:
        print("Exclusions: " + ", ".join(_esc_cmd(p) for p in excludes))
    if ticket_allow:
        print("Extra ticket allowlist: " + ", ".join(_esc_cmd(t) for t in ticket_allow))
    print()

    try:
        findings, exclusions, warnings = run_checks(args.root, excludes, ticket_allow)
    except ConfigError as exc:
        msg = _esc_cmd(exc)
        print(f"FAIL: {msg}")
        print(f"::error::public-repo-hygiene: {msg}")
        print("\nResult: invalid configuration.")
        return 2

    return _emit(findings, exclusions, warnings)


if __name__ == "__main__":
    sys.exit(main())
