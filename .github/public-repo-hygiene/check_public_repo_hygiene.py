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

Exit codes: 0 pass, 1 one or more internal-only references found, 2 the run
proves nothing (an unusable `--exclude` value, a root that is not a git work
tree, tracked content at the reserved `_public_repo_hygiene/` path, or a scan
that ended up reading zero files). Exit 2 is never "clean" -- a guard that
looked at nothing has to be as loud as one that found something.

Run locally:
    python3 .github/public-repo-hygiene/check_public_repo_hygiene.py --root .
    python3 .github/public-repo-hygiene/check_public_repo_hygiene.py --root . \
        --exclude 'src/generated/' --ticket-allow 'GPU-100'
"""

import argparse
import collections
import itertools
import os
import re
import stat
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
# step). The checkout lands UNTRACKED there, so `git ls-files` never lists it
# and an ordinary run never meets this path at all.
#
# A caller that TRACKS content at that path is therefore not a false-positive
# case to skip past, it is a broken one: the checkout overwrites the tracked
# content before the scan reads it, so whatever the repo actually stores there
# is never examined. Skipping it silently would have made the reserved path a
# parking spot — commit internal references under `_public_repo_hygiene/` and
# the run stays green with a `::notice::`. It is a hard config error (exit 2)
# instead, naming the path so the fix is "rename the directory".
SCRIPT_CHECKOUT_DIR = "_public_repo_hygiene/"

# A tracked file is read up to this cap and no further. `fh.read()` with no
# bound is a runner-memory DoS that a PR author controls by committing (or
# symlinking to) something enormous, and the caller job has a finite budget for
# the whole scan. Truncation is reported as a `::warning::` naming the file, so
# the unread tail is a visible hole rather than a silent one.
MAX_FILE_BYTES = 5 * 1024 * 1024

# The read cap above bounds the bytes read PER FILE; these bound what is DERIVED
# from them, which is where the memory and the log volume actually go. A
# category-2 finding copies the matched LINE, so one 5 MiB single-line file
# matching all nine markers would retain ~45 MiB of strings, and 5 MiB of
# repeated `AA-12` yields ~800k findings -- each materialised twice more in
# `_emit` (plain line plus `::error::`) and flooding a PUBLIC run log. Hitting
# either cap never softens the verdict: the run still exits 1, it just stops
# enumerating. (BE-8654 review.)
MAX_EXCERPT_CHARS = 200
MAX_FINDINGS_PER_FILE = 200
MAX_FINDINGS_TOTAL = 2000

# git-lfs writes a ~130-byte pointer stub in place of the real content, and
# `actions/checkout` leaves `lfs: false` by default. The stub is what is in the
# work tree, so the scan reads IT and would otherwise count the file as covered
# while the real content -- publicly downloadable from the same repo -- was
# never examined. Detected and named, per "everything the scan declines to look
# at leaves a trace". (BE-8654 review.)
LFS_POINTER_PREFIX = "version https://git-lfs.github.com/spec/v1"

# What `run_checks` reports. Named rather than a bare tuple because the two
# coverage fields (`skipped`, `scanned`) are what the exit code turns on, and a
# positional 5-tuple is how a caller ends up reading "scanned" as "findings".
ScanResult = collections.namedtuple(
    "ScanResult", "findings exclusions warnings skipped scanned"
)


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
    except FileNotFoundError as exc:
        # `cwd=root` makes this ambiguous: FileNotFoundError is raised both when
        # `git` is missing from PATH and when `root` does not exist, and a
        # typo'd --root / HYGIENE_CHECK_ROOT reported as "git is not available"
        # sends the operator after the wrong cause entirely (BE-8654 review).
        if not os.path.isdir(root):
            raise ConfigError(
                f"'{root}' is not an existing directory, so there was nothing "
                f"to scan. Check the --root argument / HYGIENE_CHECK_ROOT."
            ) from exc
        raise ConfigError(f"git is not available: {exc}") from exc
    except OSError as exc:
        # The other ways applying `cwd` fails: NotADirectoryError when the root
        # names a FILE, PermissionError when it is unreadable. Uncaught these
        # escape as a traceback, which exits 1 -- the code the workflow reads as
        # "internal-only references found" rather than the exit 2 every other
        # unusable-configuration path returns.
        raise ConfigError(
            f"cannot run git in '{root}': {exc.strerror or exc}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise ConfigError(
            f"'{root}' is not a git work tree (git ls-files failed: "
            f"{stderr or exc.returncode}). This checker scans TRACKED files, "
            f"so it needs the repo checkout, not an exported tree."
        ) from exc
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def _read_text(path):
    """Return (text, warnings, skip_kind). `skip_kind` is None iff text is not.

    Binary files are out of scope. A NUL byte is the crude-but-reliable marker
    (what the JavaScript copy used); an undecodable byte is the other (what the
    Python copy used). Honouring BOTH keeps the merged checker a superset of the
    two it replaces rather than a compromise between them.

    Every route out of here is COUNTED by `run_checks` and reported, because a
    file the scan declined to read is a hole in coverage whichever route it took
    (see README "Everything the scan declines to look at leaves a trace"). The
    two ordinary ones -- binary and non-UTF-8 -- are a per-run count only, so
    they do not bury the ones that name a specific file with a `::warning::`:

    * UNREADABLE -- a permission problem, a vanished path. A tracked path the
      checker cannot open at all is worth naming.
    * NOT A REGULAR FILE -- a FIFO, socket or device node. Reading one would
      block on the device or turn the read below into an OOM, and none of it is
      content this repo stores. `os.lstat` answers this WITHOUT following a
      link, so the decision is made on the entry git actually tracks.

    A SYMLINK is neither read nor skipped outright. `open()` FOLLOWS it, so
    scanning it would read whatever it points at -- a link out of the repo would
    pull arbitrary runner content into a public run log, since category-2
    findings echo the matched line. But the link TARGET STRING is the entry git
    stores in the blob and publishes in the tree, so a tracked link naming a
    private `Comfy-Org/<repo>` or carrying a ticket id is exactly the leak this
    checker exists to catch. `os.readlink` returns that string without opening
    anything, so the target string is scanned IN PLACE OF the file body.

    A regular file larger than `MAX_FILE_BYTES` is scanned up to the cap and the
    truncation is warned about, rather than skipped outright -- most of a large
    file is still worth checking, and the unread tail is named.
    """
    unreadable = "unreadable, so it was NOT scanned: {}"
    try:
        st = os.lstat(path)
    except OSError as exc:
        return None, [unreadable.format(exc.strerror or exc)], "unreadable"

    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            return None, [unreadable.format(exc.strerror or exc)], "unreadable"
        return (
            target,
            [
                "is a symlink: only the link TARGET STRING git tracks was "
                "scanned, never the file it points at -- that content is not "
                "this repo's, and echoing it would be the leak, not the guard"
            ],
            None,
        )

    if not stat.S_ISREG(st.st_mode):
        return (
            None,
            [
                "is not a regular file (FIFO, socket or device node), so it "
                "was NOT scanned: reading it would block on the device rather "
                "than read anything this repo stores"
            ],
            "not a regular file",
        )

    try:
        with open(path, "rb") as fh:
            # One byte past the cap, so "exactly at the cap" is not misreported
            # as truncated.
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        return None, [unreadable.format(exc.strerror or exc)], "unreadable"

    truncated = len(data) > MAX_FILE_BYTES
    if truncated:
        data = data[:MAX_FILE_BYTES]
    if b"\x00" in data:
        return None, [], "binary"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # ONLY the cap landing mid-codepoint is recoverable here, and the test
        # has to be that narrow. `data[:exc.start]` is valid UTF-8 BY
        # CONSTRUCTION -- everything before the first bad byte decoded fine --
        # so an unqualified fallback "succeeds" for a genuinely non-UTF-8 file
        # too: a >5 MiB file whose first invalid byte sits near the TOP would be
        # scanned only that far, still counted in `scanned`, and reported under
        # a warning stating the opposite ("only the first N were scanned").
        # Commit 5 MiB of filler, one `\xff`, then the internal references and
        # that passes clean with a coverage claim that overstates what was read.
        # A UTF-8 sequence is at most 4 bytes, so an error starting further than
        # that from the end of the truncated buffer cannot be the cut. (BE-8654
        # review.)
        if not truncated or len(data) - exc.start >= 4:
            return None, [], "non-UTF-8"
        text = data[: exc.start].decode("utf-8")

    warnings = []
    if truncated:
        warnings.append(
            f"is larger than {MAX_FILE_BYTES} bytes; only the first "
            f"{MAX_FILE_BYTES} were scanned"
        )
    if text.startswith(LFS_POINTER_PREFIX):
        warnings.append(
            "is a git-LFS pointer stub, so only the ~130-byte stub was "
            "scanned, NOT the real content it stands for. The content is "
            "publicly downloadable from this repo but is not in the work tree "
            "(`actions/checkout` does not fetch LFS objects by default), so "
            "this file is a hole in the scan's coverage"
        )
    return text, warnings, None


def _excerpt(line):
    """A bounded, stripped excerpt of a matched line, for the report.

    Category-2 findings echo the matched line rather than just the match, which
    is what makes them actionable -- but the line is attacker-controlled and can
    be the whole file. Bound it (see MAX_EXCERPT_CHARS).
    """
    stripped = line.strip()
    if len(stripped) <= MAX_EXCERPT_CHARS:
        return stripped
    return stripped[:MAX_EXCERPT_CHARS] + "... (line truncated)"


def _bounded(token):
    """Bound a matched TOKEN before interpolating it into a finding.

    `REPO_REF_RE`'s name class is unbounded, so `Comfy-Org/` followed by 5 MiB
    of word characters is ONE match whose text is the whole file: the same
    unbounded-derived-output problem `_excerpt` solves for whole lines, reached
    through a different door. `TICKET_RE` needs no equivalent -- it is bounded
    to 13 characters by its own quantifiers.
    """
    if len(token) <= MAX_EXCERPT_CHARS:
        return token
    return token[:MAX_EXCERPT_CHARS] + "... (truncated)"


def _file_findings(rel, text, ticket_allowlist):
    """Yield one file's findings lazily, in report order.

    A generator rather than a list so `check_file` can stop it at the per-file
    cap: a 5 MiB line of repeated `AA-12` must not be fully enumerated just to
    throw the tail away.
    """
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in TICKET_RE.finditer(line):
            if match.group(0).upper() not in ticket_allowlist:
                yield (
                    f"{rel}:{lineno}: possible internal ticket ID: "
                    f"{match.group(0)!r}"
                )

        for pattern in INTERNAL_MARKER_RES:
            if pattern.search(line):
                yield (
                    f"{rel}:{lineno}: internal collaboration-tool marker: "
                    f"{_excerpt(line)!r}"
                )

        for match in REPO_REF_RE.finditer(line):
            name = match.group(1)
            # A leading `@` makes this a CODEOWNERS team handle, not a repo ref.
            if match.start() > 0 and line[match.start() - 1] == "@":
                if name not in PUBLIC_COMFY_ORG_TEAMS:
                    yield (
                        f"{rel}:{lineno}: reference to "
                        f"@Comfy-Org/{_bounded(name)}, a "
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
                yield (
                    f"{rel}:{lineno}: reference to "
                    f"Comfy-Org/{_bounded(repo)}, which is "
                    "not in the known-public allowlist "
                    "(Comfy-Org/github-workflows "
                    ".github/public-repo-hygiene/check_public_repo_hygiene.py)"
                    " -- confirm it's public and add it, or remove the "
                    "reference"
                )


def check_file(root, rel, ticket_allowlist):
    """Return (findings, warnings, skip_kind) for one file, in report order."""
    text, warnings, skip_kind = _read_text(os.path.join(root, rel))
    if text is None:
        return [], warnings, skip_kind

    # One past the cap, so "capped" is distinguishable from "exactly at it".
    findings = list(
        itertools.islice(
            _file_findings(rel, text, ticket_allowlist),
            MAX_FINDINGS_PER_FILE + 1,
        )
    )
    if len(findings) > MAX_FINDINGS_PER_FILE:
        del findings[MAX_FINDINGS_PER_FILE:]
        warnings.append(
            f"produced more than {MAX_FINDINGS_PER_FILE} findings; only the "
            f"first {MAX_FINDINGS_PER_FILE} are listed. This file needs fixing "
            f"wholesale rather than finding-by-finding -- the run still FAILS"
        )
    return findings, warnings, None


def run_checks(root, excludes=(), extra_ticket_allow=()):
    """Scan `root`, returning a `ScanResult`.

    `exclusions` is [(pattern, files_skipped)] in the caller's order, INCLUDING
    patterns that skipped nothing -- a typo'd exclusion that matches no file has
    to be visible in the log, not silently inert. `skipped` is the same
    accounting for the files the reader itself declined, [(kind, count)]. Both
    exist for one reason: `scanned` is the number of files this run actually
    read, and every file that is tracked but not in it has to be attributable to
    a named reason.
    """
    excludes = [_normalize_exclude(p) for p in excludes]
    # Additive, never a replacement: a caller can name extra acronyms, it can
    # never drop a built-in one.
    ticket_allowlist = TICKET_ALLOWLIST | {a.upper() for a in extra_ticket_allow}

    tracked = tracked_files(root)
    reserved = [r for r in tracked if _is_excluded(r, (SCRIPT_CHECKOUT_DIR,))]
    if reserved:
        raise ConfigError(
            f"the repo tracks {len(reserved)} file(s) under "
            f"'{SCRIPT_CHECKOUT_DIR}' (first: '{reserved[0]}'), which is a "
            f"RESERVED path: the reusable workflow checks the hygiene checker "
            f"out there inside your workspace, so that checkout -- not your "
            f"content -- is what sits at that path by the time the scan runs, "
            f"and nothing tracked beneath it can be examined. Rename the "
            f"directory. Scanning it as if it were yours would fail every run "
            f"on THIS repo's own ticket ids; skipping it would leave a path "
            f"any PR could park an internal reference in and stay green."
        )

    counts = {p: 0 for p in excludes}
    skipped = collections.Counter()
    findings, warnings = [], []
    scanned = 0
    truncated_report = False
    for rel in tracked:
        hit = _is_excluded(rel, excludes)
        if hit is not None:
            counts[hit] += 1
            continue
        found, file_warnings, skip_kind = check_file(root, rel, ticket_allowlist)
        # Per-RUN cap on top of the per-file one, so a tree of many mid-sized
        # offenders cannot flood the log either. Scanning CONTINUES past it --
        # only the enumeration stops -- because `scanned`/`skipped` are the
        # coverage claim and must stay complete.
        room = MAX_FINDINGS_TOTAL - len(findings)
        if len(found) > room:
            truncated_report = True
            found = found[:room]
        findings.extend(found)
        for warning in file_warnings:
            warnings.append(f"{rel}: {warning}")
        if skip_kind is None:
            scanned += 1
        else:
            skipped[skip_kind] += 1

    if truncated_report:
        warnings.append(
            f"more than {MAX_FINDINGS_TOTAL} findings were produced across the "
            f"repo; only the first {MAX_FINDINGS_TOTAL} are listed. The exit "
            f"code is unaffected -- this run still FAILS"
        )

    exclusions = [(p, counts[p]) for p in excludes]
    if scanned == 0:
        # "Nothing to scan" is never the same as "clean". A repo whose every
        # tracked file was excluded, or that has no tracked files at all,
        # produces a green run that proves nothing -- say so out loud, and (in
        # `_emit`) exit 2 rather than 0, exactly as the non-git-root case does.
        warnings.append(
            "no files were scanned at all (nothing tracked, everything "
            "excluded, or nothing readable as text) -- this run proves "
            "nothing about the repo"
        )

    return ScanResult(
        findings, exclusions, warnings, sorted(skipped.items()), scanned
    )


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


def _emit(result):
    """Print human lines plus Actions annotations, and return the exit code."""
    for pattern, count in result.exclusions:
        line = f"EXCLUDED: {count} file(s) matched {_esc_cmd(pattern)!r}"
        print(line)
        print(f"::notice::public-repo-hygiene: {line}")

    # Binary and non-UTF-8 files are ordinary skips, so they are a per-run count
    # rather than a warning each -- but a count they must have. Silent, they hide
    # a whole file behind one stray byte while it still renders as text on
    # GitHub, and `scanned` above would have no way to say so.
    for kind, count in result.skipped:
        line = f"NOT SCANNED: {count} file(s) skipped as {_esc_cmd(kind)}"
        print(line)
        print(f"::notice::public-repo-hygiene: {line}")
    print(f"SCANNED: {result.scanned} file(s) read as text")

    for w in result.warnings:
        w = _esc_cmd(w)
        print(f"WARN: {w}")
        print(f"::warning::public-repo-hygiene: {w}")

    if result.scanned == 0:
        # Green here would make the root-exclusion rejection one spelling away
        # from pointless: a caller that names every top-level directory in
        # `exclude_paths` disables the whole scan without ever naming the root.
        print(
            "\nResult: nothing was scanned, so this run proves nothing about "
            "the repo. Treated as a configuration failure, not a pass."
        )
        return 2

    if not result.findings:
        print("\nResult: no internal-only references found.")
        return 0

    print("\nERROR: possible internal-only references found in this public repo:\n")
    for finding in result.findings:
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
    print(f"\nResult: {len(result.findings)} internal-only reference(s) found.")
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
        result = run_checks(args.root, excludes, ticket_allow)
    except ConfigError as exc:
        msg = _esc_cmd(exc)
        print(f"FAIL: {msg}")
        print(f"::error::public-repo-hygiene: {msg}")
        print("\nResult: invalid configuration.")
        return 2

    return _emit(result)


if __name__ == "__main__":
    sys.exit(main())
