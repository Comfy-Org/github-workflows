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
tree, tracked content at the reserved `_public_repo_hygiene/` path, a
`working-tree-encoding` gitattribute that makes the work tree differ from what
git stores, or a scan that ended up reading zero files). Exit 2 is never
"clean" -- a guard that looked at nothing has to be as loud as one that found
something.

Run locally:
    python3 .github/public-repo-hygiene/check_public_repo_hygiene.py --root .
    python3 .github/public-repo-hygiene/check_public_repo_hygiene.py --root . \
        --exclude 'src/generated/' --ticket-allow 'GPU-100'
"""

import argparse
import codecs
import collections
import itertools
import os
import posixpath
import re
import stat
import subprocess
import sys
import unicodedata

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
# Well-known PUBLIC identifier namespaces, allowlisted by PREFIX rather than as
# exact tokens. `\b[A-Z]{2,6}-\d{2,6}\b` matches `CVE-2021` inside
# `CVE-2021-44228` -- the `\b` holds against the following hyphen -- so a
# SECURITY.md, a dependency changelog or a patch note trips what adopters wire in
# as a REQUIRED check. Clearing that token-by-token would cost one entry per year
# prefix and break again each January; `CWE-89`, `PEP-484` and any
# `RFC-####`/`ISO-####` outside the three hard-coded RFCs above are the same
# shape -- as is `UTF-16`/`UTF-32`, which the exact list above carried only for
# `UTF-8`. None of these is a plausible internal team key, so the namespace is
# the right granularity. (BE-8654 review.)
TICKET_ALLOWED_PREFIXES = frozenset(
    {"CVE", "CWE", "PEP", "RFC", "ISO", "UTF"}
)

# --- Category 2: internal collaboration-tool links/markers -----------------
# Anchored on real DNS-label boundaries, not on `\b` and not on nothing.
#
# A host name is a dot-separated sequence of labels made of letters, digits and
# hyphens, so the only character that can precede a host we care about and still
# leave it THAT host is a dot (a genuine subdomain edge) or something outside the
# label alphabet entirely (`/`, `@`, a space, start of line). A letter, digit or
# hyphen in front means a DIFFERENT registrable name: `comfy.slack.com` and
# `www.notion.so` really are those services, while `fooslack.com` and
# `evil-posthog.com` are unrelated domains that the bare patterns used to flag.
# `\b` did not help -- a hyphen is a non-word character, so `\bposthog\.com`
# matched inside `evil-posthog.com` -- and half these patterns had no left
# anchor at all. Do NOT "tighten" this to `(?<![A-Za-z0-9.-])`: barring a
# preceding dot rejects every subdomain-prefixed positive the fixtures pin.
#
# "A DIFFERENT registrable name" is exact only for the TWO-label patterns
# (`notion.so`, `slack.com`, `posthog.com`, `linear.app`), where the anchored
# host IS the registrable name. For the THREE-label hosts a preceding label
# character just extends the third-level label, so `my-app.slack.com` is still
# a slack.com workspace host and `comfyapp.datadoghq.com` still a Datadog
# customer sub-domain -- both matched before this change, as a substring, and
# both are silent now. That buys nothing in lookalike rejection (no
# `<x>-app.slack.com` can be a different registrable domain) and is the same
# customer-sub-domain shape the README documents for Datadog; the workspace
# case is listed beside it and pinned by
# `test_a_label_character_adjacent_to_the_host_is_a_known_miss`.
#
# The class is ASCII, so an IDN neighbour still clears it and `énotion.so/x` is
# reported as `notion.so`. That is left alone deliberately: the blunt fix, a
# second lookbehind rejecting any non-ASCII character, silences a REAL link
# written after a curly quote, an em dash or CJK prose (`“notion.so/page`), and
# a false negative costs more than a false positive in a leak guard. Documented
# as a limitation in the README instead. The same gap exists for ASCII `_`,
# which is unreserved and which WHATWG accepts in a host: `evil_notion.so/page`
# is a different name and reports as `notion.so`. Adding `_` to the class is
# NOT free -- it would silence a real link written in markdown emphasis
# (`_notion.so/page_`) -- so it is documented with the IDN case rather than
# closed. (BE-8729 review, rounds 1 and 5.)
_HOST_L = r"(?<![A-Za-z0-9-])"
# An explicit port sits between the host and the path, so a pattern that
# requires `/` straight after the host is bypassed by `notion.so:443/`. The
# digits are `*`, not `+`: `port = *DIGIT` in RFC 3986, so `https://notion.so:/x`
# is a valid URL whose host is still `notion.so` (an empty port means the
# default) -- and it was the same one-token bypass `:443` was. (BE-8729 review.)
_PORT = r"(?::\d*)?"
# ASCII on both counts. `re.IGNORECASE` alone folds Unicode, and the fold is
# not one boundary but two, so be precise about which one this buys:
#
#   * U+0131 / U+0130 are DIFFERENT hosts. Python matches both against `i`, so
#     `lınear.app/x` and `notİon.so/page` read as the real hosts -- but UTS-46
#     leaves U+0131 alone and maps U+0130 to `i` + a combining dot, so neither
#     resolves anywhere near `linear.app` or `notion.so`. Flagging them was a
#     false positive, and `re.ASCII` removes it. This is the same widening
#     `REPO_REF_RE` below scopes its flag to avoid.
#   * U+017F / U+212A are the SAME host. UTS-46 *maps* them to `s` and `k`, so
#     `ſlack.com/archives/C123` really does resolve to `slack.com` in any
#     client. `re.ASCII` therefore turns those two into misses. That is a
#     deliberate scope call, not an oversight: they are obfuscated spellings of
#     a covered host, exactly like the punycode (`xn--`), percent-encoded and
#     defanged spellings the README already lists as out of scope for a guard
#     against an accidental paste. Do not read the tests below as claiming
#     `ſlack.com` is somebody else's domain -- it is not.
#
# `re.ASCII` also pins `_PORT`'s `\d` to `[0-9]`, which is all a real port can
# be; a port typed in another script's digits is likewise a spelling no client
# resolves. (BE-8729 review.)
_HOST_FLAGS = re.IGNORECASE | re.ASCII
INTERNAL_MARKER_RES = (
    re.compile(_HOST_L + r"notion\.(so|site)" + _PORT + "/", _HOST_FLAGS),
    re.compile(_HOST_L + r"slack\.com" + _PORT + "/(archives|client)/", _HOST_FLAGS),
    # The one host-only pattern, so it needs a right anchor of its own -- and
    # it does NOT consume `_PORT`. Two review rounds were lost to the fact that
    # an optional, greedy port followed by a negative lookahead BACKTRACKS: the
    # engine hands digits back one at a time until the lookahead is satisfied,
    # so every alternative added to the lookahead had to be reasoned about
    # against every possible port split, and two of the three attempts shipped
    # a hole (`app.slack.com:443.evil.com` matching) or a regression
    # (`app.slack.com:general` and `app.slack.com:2FA` going silent).
    #
    # A `search()` only needs a boolean, so nothing here has to be consumed.
    # One lookahead reads the whole tail, and the port never enters the match:
    # no optional group, no backtracking, each alternative independent.
    #   `\.?[A-Za-z0-9-]`      a following label -- `app.slack.com.evil.com`,
    #                          which is what `\b` used to accept. A dot is
    #                          allowed only when what follows is NOT a label,
    #                          so `app.slack.com./x` still matches. The class
    #                          is ASCII on both sides of the host, so a
    #                          non-ASCII label continuation is NOT rejected and
    #                          `app.slack.com.中国/` reports as the literal
    #                          host -- the right-hand half of the IDN-neighbour
    #                          limitation, README "Known limitations".
    #                          It also admits a hyphen at the FIRST position,
    #                          so a prose hyphen reads as a continuing label
    #                          and `app.slack.com-hosted workspace` is a MISS
    #                          (`\b` held there, since a hyphen is a non-word
    #                          character). Restricting the hyphen to the
    #                          post-dot position would recover it and over-flag
    #                          `app.slack.com-evil.com`, whose registrable name
    #                          really is `com-evil.com`; left as-is to match
    #                          `_HOST_L`, which makes the same trade on the
    #                          left (`my-app.slack.com`). Both are pinned by
    #                          `test_a_label_character_adjacent_to_the_host_is_a_known_miss`.
    #   `@`                    the userinfo delimiter: in
    #                          `https://app.slack.com@evil.com/` the real host
    #                          is `evil.com`, the canonical phishing shape.
    #   `[.:][A-Za-z0-9._~%:-]{0,64}@`
    #                          the same thing with userinfo in between --
    #                          `:@`, `:secret@`, `.@`. Two bounds, and both are
    #                          load-bearing (BE-8729 review, round 4):
    #                            * The CLASS is the userinfo characters a real
    #                              credential uses (unreserved + `%` + the
    #                              `user:pass` colon), NOT "anything but a URL
    #                              delimiter". `[^\s/?#@]*` crossed commas,
    #                              quotes and braces, so any later `@` on the
    #                              line silenced the host:
    #                              `app.slack.com:443,ops@example.com` and
    #                              `{"slack":"app.slack.com:443","owner":"bob@x"}`
    #                              both went quiet -- a MISS, the one direction
    #                              this guard cannot afford. Narrowing a class
    #                              inside a NEGATIVE lookahead can only ever
    #                              flag MORE, so it cannot add a miss of its own;
    #                              the cost is a false positive on a lookalike
    #                              whose userinfo holds a sub-delim (`:p+w@`),
    #                              which is a maintainer glance, not a leak.
    #                              What stops the run is precisely: whitespace,
    #                              `/`, `?`, `#`, a quote, a brace, and the
    #                              sub-delims `! $ & ' ( ) * + , ; =`.
    #                              `:` is IN the class, because `:user:pass@`
    #                              is real userinfo -- so a colon-chained run
    #                              still crosses prose to an unrelated `@` and
    #                              `app.slack.com:2024-01-15:incident@comfy.org`
    #                              is a MISS. Dropping `:` would reopen the
    #                              genuine `:user:pass@evil.com` shape, so the
    #                              miss is kept, documented in the README and
    #                              pinned by
    #                              `test_a_colon_chained_run_still_reaches_a_later_at`.
    #                            * The LENGTH is bounded because `*` here was
    #                              quadratic: nothing bounds a LINE (only
    #                              `MAX_FILE_BYTES` bounds a file), and on
    #                              `('app.slack.com:' * N) + '@'` each of the
    #                              ~L/14 host positions rescanned the whole tail
    #                              -- ~10^12 character steps at the 5 MiB cap,
    #                              i.e. author-controlled content turning a
    #                              required check into a mystery 15-minute
    #                              timeout. Real userinfo is short; 64 is slack.
    #                              Its correctness cost, in the same over-flag
    #                              direction as the CLASS bound: userinfo
    #                              LONGER than 64 characters (a token or JWT
    #                              carried as the basic-auth password) puts the
    #                              `@` out of the run's reach, so
    #                              `https://app.slack.com:<65 chars>@evil.com/`
    #                              is flagged although the real host is
    #                              `evil.com`. Both sides of the boundary are
    #                              pinned by
    #                              `test_the_userinfo_length_bound_over_flags_past_64_characters`,
    #                              so moving the bound cannot silently retrade this.
    # A leading `admin@` is the other direction and is untouched -- `_HOST_L`
    # only bars a label character.
    #
    # There is deliberately NO `:\d+\.[A-Za-z0-9-]` alternative (removed in
    # round 5). It was there to reject `app.slack.com:443.evil.com` as "a port
    # the host continues past", but that shape is not a bypass: `port = *DIGIT`,
    # so WHATWG's port state fails on the `.` and the URL does not parse at all,
    # `urlsplit(...).hostname` returns `app.slack.com`, and curl and Go's
    # `net/url` both error on the port. No mainstream parser resolves that line
    # to `evil.com`, so the only readable host on it is the internal one and
    # suppressing it was a MISS, not a false-positive fix -- paid for with the
    # `:2.5` / `:1.0.1` prose misses it also caused. The genuine phishing form,
    # `app.slack.com:443.evil.com@evil.com`, is rejected by the `[.:]...@`
    # alternative above and needs nothing here. (Removing an alternative from a
    # NEGATIVE lookahead flags MORE, never less -- the class note above states
    # the same rule; an earlier revision of this comment had that direction
    # backwards.) (BE-8729 review, rounds 2, 3, 4 and 5.)
    re.compile(
        _HOST_L
        + r"app\.slack\.com"
        + r"(?!\.?[A-Za-z0-9-]|@|[.:][A-Za-z0-9._~%:-]{0,64}@)",
        _HOST_FLAGS,
    ),
    re.compile(_HOST_L + r"docs\.google\.com" + _PORT + "/", _HOST_FLAGS),
    re.compile(_HOST_L + r"drive\.google\.com" + _PORT + "/", _HOST_FLAGS),
    re.compile(_HOST_L + r"app\.datadoghq\.com" + _PORT + "/", _HOST_FLAGS),
    re.compile(_HOST_L + r"posthog\.com" + _PORT + "/project/", _HOST_FLAGS),
    re.compile(_HOST_L + r"linear\.app" + _PORT + "/", _HOST_FLAGS),
    # Not a host, so it keeps default (Unicode) semantics and its own `\b`.
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
#
# Entries are kept in case-insensitive alphabetical order, and that ordering is
# ASSERTED by `test_repo_allowlist_is_sorted_and_duplicate_free` rather than
# left to review (BE-8855). A `frozenset` has no order of its own, so the only
# place the order exists is this source text -- which is exactly where a human
# reads it to answer "is <name> already allowlisted?" before appending a
# duplicate to the bottom. New names go in their alphabetical slot, each with a
# one-line comment recording where the reference came from and that it was
# verified public.
PUBLIC_COMFY_ORG_REPOS = frozenset(
    {
        "comfy-api-proxy",
        "comfy-cla",
        "comfy-cli",
        "comfy-cloud-mcp-server",
        # Referenced from comfy-cli's refresh-cql-catalogs.yml workflow
        # (comfy-cli#758). Verified public: an unauthenticated
        # raw.githubusercontent.com fetch of its README returns 200.
        "comfy-complete",
        "Comfy-Desktop",
        # Adopted the public-repo-hygiene caller in comfy-mcp#254 and hit a
        # false positive on its own self-references (README, CI comments,
        # issue templates). Verified public via the GitHub API
        # (`private: false`).
        "comfy-mcp",
        "comfy-python-sdk",
        # BE-8855: referenced from comfy-cli (2 findings). Verified public.
        "comfy-skills",
        "comfy-swift-sdk",
        "comfy-typescript-sdk",
        "ComfyUI",
        # BE-8855: referenced from comfy-cli (3 findings). Verified public.
        "ComfyUI-Manager",
        # BE-8855: referenced from comfy-cli (1 finding). Verified public.
        "ComfyUI-test-framework",
        "ComfyUI_frontend",
        # BE-8855: referenced from comfy-cli (1 finding). Verified public.
        "cookiecutter-comfy-extension",
        # BE-8855: referenced from comfy-cli (1 finding). Verified public.
        "CustomNodeComfyMath",
        # BE-8654: this repo. Both SDK copies were missing it, so the caller
        # every one of them needs -- a pin at
        # `Comfy-Org/github-workflows/.github/workflows/...` -- failed the very
        # check it was being added alongside. Verified public.
        "github-workflows",
        # BE-8855: the biggest single false-positive source in comfy-cli, at 21
        # findings -- it is the template pack comfy-cli ships against. Verified
        # public.
        "workflow_templates",
    }
)
# CODEOWNERS team handles (`@Comfy-Org/<team>`) are inherently public on a
# public repo -- GitHub renders CODEOWNERS owners to anyone who can see the
# repo, so listing them here is not a leak. An `@Comfy-Org/<team>` handle NOT
# in this set is still flagged, so a genuinely-internal team reference
# surfaces.
#
# Same editing rule as the repo allowlist above, and asserted the same way by
# `test_team_allowlist_is_sorted_and_duplicate_free`: new team slugs go in
# their case-insensitive alphabetical slot, not at the end, and a duplicate
# (in any casing) fails the build rather than collapsing silently into the set.
PUBLIC_COMFY_ORG_TEAMS = frozenset({"comfy-cloud-team", "core-engine-team"})

# Casefolded views of the two lists above, used for MEMBERSHIP only (BE-8697).
# The lists themselves stay the human-edited source of truth in their canonical
# GitHub spelling, and a finding still quotes the name exactly as it appeared in
# the file, so "add it to the allowlist" stays a copy-paste. Casefolding cannot
# weaken the control: no private name is in either allowlist under any casing,
# so the only references it newly clears are differently-cased spellings of
# names GitHub already resolves to an allowlisted PUBLIC repo or team.
_PUBLIC_REPOS_CF = frozenset(name.casefold() for name in PUBLIC_COMFY_ORG_REPOS)
_PUBLIC_TEAMS_CF = frozenset(name.casefold() for name in PUBLIC_COMFY_ORG_TEAMS)

# Case-INSENSITIVE, because GitHub resolves owner names case-insensitively:
# `comfy-org/<private-repo>` reaches exactly the same repository as
# `Comfy-Org/<private-repo>`, so matching only the canonical spelling left a
# one-keystroke bypass of a default-deny control (BE-8697). The allowlist tests
# below casefold rather than lowercase, so a differently-cased spelling of a
# PUBLIC name is not reported as a leak either.
#
# The `(?i:...)` scope is load-bearing, not style. Whole-pattern `re.IGNORECASE`
# on a `str` pattern ALSO widens the name class: under Unicode case-folding
# `[A-Za-z]` then matches U+017F `ſ`, U+212A `K`, U+0130 `İ` and U+0131 `ı`, so
# the class stops being ASCII and `.casefold()` folds `ſ`/`K` back to `s`/`k`
# at the membership test -- `Comfy-Org/comfy-typeſcript-sdk` would clear a
# default-deny allowlist, and a public reference followed immediately by one of
# those characters would be absorbed into the name and flagged. Scoping the flag
# to the org segment (which contains no `s`, `k` or `i` to widen) keeps the
# capture class ASCII, which is what the allowlists are written in.
#
# BOTH boundaries are explicit, because an unbounded edge is read as a boundary
# that is not there (BE-8654 review). On the LEFT, `(?<![A-Za-z0-9_])` stops
# `NotComfy-Org/x` from being reported as a reference to the org -- the org
# segment has to start a token, and every real spelling is preceded by a
# separator (`/` in a URL, whitespace, a quote, or the `@` of a team handle,
# none of which are in the class). On the RIGHT there is no lookahead at all,
# deliberately: refusing to MATCH a name the class cannot fully read would turn
# a partly-readable name into no finding, which is the bypass upside down. The
# match is made and `_nonascii_tail` below decides what it means.
REPO_REF_RE = re.compile(r"(?<![A-Za-z0-9_])(?i:Comfy-Org)/([A-Za-z0-9_.-]+)")

# ASCII characters the name class accepts -- the source of truth for how far a
# name extends, shared by `REPO_REF_RE` and the tail walk below.
_REPO_NAME_ASCII = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)

# Unicode categories that continue a NAME rather than end it: letters (L*),
# numbers (N*), combining marks (M*), and the dash/connector punctuation that
# supplies the homoglyphs -- U+2010 HYPHEN renders identically to `-` on
# github.com. Quote and bracket categories are deliberately absent, so ordinary
# prose like `Comfy-Org/ComfyUI’s frontend` stays a clean reference.
_NAME_CONTINUING_CATEGORIES = frozenset({"Pd", "Pc"})

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

# The same bound, applied to the OTHER accumulator. `warnings` is per-file and
# embeds the path, and there are now six producers of one (unreadable, symlink,
# submodule gitlink, non-regular file, oversize truncation, LFS stub) -- so a
# tree of tens of thousands of tracked symlinks (a ~20-byte blob apiece) floods
# a PUBLIC run log and retains the strings, reaching the finding caps' problem
# through a door they do not cover. Only the per-FILE warnings are capped: the
# whole-run ones (zero coverage, report truncation) are appended after the scan
# and always survive, and `skipped`/`scanned` are counts, so the coverage claim
# stays complete however many warnings are dropped. (BE-8654 review.)
MAX_WARNINGS_TOTAL = 200

# git-lfs writes a ~130-byte pointer stub in place of the real content, and
# `actions/checkout` leaves `lfs: false` by default. The stub is what is in the
# work tree, so the scan reads IT and would otherwise count the file as covered
# while the real content -- publicly downloadable from the same repo -- was
# never examined. Detected and named, per "everything the scan declines to look
# at leaves a trace". (BE-8654 review.)
LFS_POINTER_PREFIX = "version https://git-lfs.github.com/spec/v1"
# The FULL pointer grammar, not just that first line. A bare `startswith` let
# any ordinary tracked file opt out of the scan by opening with that one line:
# everything below it would go unread while the file still renders as plain text
# on github.com. A genuine stub is a handful of `key value` lines carrying a
# `sha256` oid and a byte size, and it is ~130 bytes -- so the grammar plus a
# size ceiling is what separates "git put a placeholder here" from "someone
# typed the magic line". (BE-8654 review.)
LFS_POINTER_MAX_BYTES = 1024
_LFS_OID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

# A committed UTF-16/UTF-32 blob needs no gitattribute at all -- the NUL bytes
# are in the bytes git STORES, so `_work_tree_encoded` never sees it -- and the
# `binary` skip would then drop the whole file from the scan while GitHub still
# renders it as readable text. Every one of these encodings is required to be
# self-describing via a BOM, so sniffing one and decoding is exact rather than
# guesswork. UTF-32's BOMs are tested FIRST: `\xff\xfe\x00\x00` starts with the
# UTF-16-LE BOM, so the shorter marker would swallow it. A UTF-8 BOM is
# deliberately NOT listed: those bytes already decode as UTF-8 down the ordinary
# path, which carries a recovery for a read cap landing mid-codepoint that this
# table has no equivalent of, and a leading U+FEFF hides nothing (it is not a
# word character, so neither `TICKET_RE`'s `\b` nor the repo pattern's left
# boundary is affected by it). (BE-8654 review.)
_BOM_CODECS = (
    (codecs.BOM_UTF32_LE, "utf-32", 4),
    (codecs.BOM_UTF32_BE, "utf-32", 4),
    (codecs.BOM_UTF16_LE, "utf-16", 2),
    (codecs.BOM_UTF16_BE, "utf-16", 2),
)

# What `run_checks` reports. Named rather than a bare tuple because the two
# coverage fields (`skipped`, `scanned`) are what the exit code turns on, and a
# positional 5-tuple is how a caller ends up reading "scanned" as "findings".
#
# `partial` is the third coverage field and exists because the other two cannot
# express "read, but not all of it": an oversize file truncated at the read cap
# counts as `scanned` with no skipped kind, and a file whose findings were capped
# has no count at all -- so before this the per-file `::warning::` was the ONLY
# record that coverage was partial, and that warning is subject to
# MAX_WARNINGS_TOTAL. 200 cheap tracked symlinks sorting ahead of a large file
# therefore buried the one line saying its tail was never read. Counts are never
# capped, so the coverage arithmetic survives any log truncation. (BE-8654
# review.)
ScanResult = collections.namedtuple(
    "ScanResult", "findings exclusions warnings skipped scanned partial"
)

# `partial` kinds. Named constants because `_emit` prints them and the tests
# assert on them.
PARTIAL_READ = "read only up to the size cap"
PARTIAL_FINDINGS = "reported only up to the per-file findings cap"


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


def _work_tree_encoded(root, paths):
    """Tracked paths whose WORK-TREE bytes are not the bytes git stores.

    `working-tree-encoding=UTF-16` in a tracked `.gitattributes` keeps the
    committed blob UTF-8 while checkout writes NUL-laden UTF-16 to disk. This
    scan reads the work tree, so such a file is skipped as `binary` -- a
    `NOT SCANNED` count, never a failure -- while the internal references sit
    plainly visible in the blob GitHub serves on the web, in the API and in the
    diff a reviewer reads. That is a green run over content the guard never
    looked at, reachable from a two-line commit, so it is a hard configuration
    error like every other route by which this checker would decline to look and
    still report clean. (BE-8654 review.)

    Asked of git rather than re-parsed here: `git check-attr` resolves the
    attribute exactly as checkout does, honouring `.gitattributes` at every
    directory level plus `.git/info/attributes`, which a hand-rolled reader of
    the top-level file would miss.

    `--cached` asks the INDEX rather than the work tree, because the property
    being asserted is about the bytes git STORES. Without it the attributes are
    read from the same on-disk `.gitattributes` a conversion may have mangled:
    a commit that applies `working-tree-encoding=UTF-16` to `.gitattributes`
    ALONG WITH the leaking file leaves an unparseable attributes file on disk,
    every path comes back `unspecified`, and this guard fails open over exactly
    the commit it exists to catch. (BE-8654 review.)
    """
    if not paths:
        return []
    try:
        out = subprocess.run(
            [
                "git",
                "check-attr",
                "--cached",
                "--stdin",
                "-z",
                "working-tree-encoding",
            ],
            cwd=root,
            input="\0".join(paths).encode("utf-8", "surrogateescape"),
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        # Loud, not lenient: if we cannot establish that the work tree holds the
        # committed bytes, we cannot claim to have scanned the committed bytes.
        raise ConfigError(
            f"could not resolve gitattributes via 'git check-attr' in "
            f"'{root}': {exc}. That check is what rules out a work-tree "
            f"encoding conversion hiding tracked content from this scan, so it "
            f"cannot be skipped."
        ) from exc
    # `-z` output is a flat NUL-delimited stream of (path, attribute, value)
    # triples; only the third field of each is of interest.
    fields = out.decode("utf-8", "surrogateescape").split("\0")
    converted = []
    for i in range(0, len(fields) - 2, 3):
        path, value = fields[i], fields[i + 2]
        if value in ("unspecified", "unset"):
            continue
        # UTF-8 is the identity mapping git skips re-encoding for, so it
        # converts nothing and must not cost the caller a hard failure it can
        # only clear by excluding paths (which costs real coverage). Matched
        # case-insensitively like git does, and NARROWLY: the BOM variants
        # (`UTF-8BOM`, `UTF-8-BOM`) do rewrite the bytes and stay fatal.
        # (BE-8654 review.)
        if value.casefold() in ("utf-8", "utf8"):
            continue
        # `git check-attr` answers purely by PATH PATTERN, so a rule like
        # `*.txt working-tree-encoding=UTF-16` also "converts" a tracked symlink
        # or a submodule gitlink named `notes.txt` -- entries checkout writes
        # with no encoding step at all. Only a regular file can actually be
        # re-encoded. A path we cannot stat stays in the list: unresolvable is
        # not the same as harmless.
        try:
            if not stat.S_ISREG(os.lstat(os.path.join(root, path)).st_mode):
                continue
        except OSError:
            pass
        converted.append(path)
    return converted


def _is_lfs_pointer(text, nbytes):
    """True only for text matching the FULL git-lfs pointer grammar.

    A stub is `version <spec-url>` followed by sorted `key value` lines that
    MUST include a `sha256` oid and a byte size, and it is ~130 bytes. Checking
    the first line alone made the skip an opt-out any file could take by opening
    with that line; requiring the grammar (and a size ceiling no real stub
    exceeds) makes it a classification instead. (BE-8654 review.)
    """
    if nbytes > LFS_POINTER_MAX_BYTES:
        return False
    lines = text.splitlines()
    if not lines or lines[0] != LFS_POINTER_PREFIX:
        return False
    fields = {}
    for line in lines[1:]:
        key, sep, value = line.partition(" ")
        if not sep or not key:
            return False
        fields[key] = value
    return bool(_LFS_OID_RE.match(fields.get("oid", ""))) and fields.get(
        "size", ""
    ).isdigit()


def _read_text(path):
    """Return (text, warnings, skip_kind, truncated).

    `text` is None exactly when nothing could be read. `skip_kind` names why a
    file does not count toward coverage -- it is NOT the inverse of `text`: a
    git-LFS pointer stub is returned as text AND as a skip (see below), because
    the stub is readable but is not the file. `truncated` says the read stopped
    at the size cap, which `check_file` turns into a `partial` count.

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
    * SUBMODULE GITLINK -- a directory entry. Its files belong to another
      repository and are out of this scan's scope whichever way it was checked
      out, so it is named as what it is rather than as a device node.
    * NOT A REGULAR FILE -- a FIFO, socket or device node. Reading one would
      block on the device or turn the read below into an OOM, and none of it is
      content this repo stores. `os.lstat` answers this WITHOUT following a
      link, so the decision is made on the entry git actually tracks.
    * GIT-LFS POINTER -- the work tree holds a ~130-byte stub, not the file. The
      stub reads fine as text, which is exactly why it must NOT count as
      scanned.

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
        return None, [unreadable.format(exc.strerror or exc)], "unreadable", False

    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            return (
                None,
                [unreadable.format(exc.strerror or exc)],
                "unreadable",
                False,
            )
        return (
            target,
            [
                "is a symlink: only the link TARGET STRING git tracks was "
                "scanned, never the file it points at -- that content is not "
                "this repo's, and echoing it would be the leak, not the guard"
            ],
            None,
            False,
        )

    if stat.S_ISDIR(st.st_mode):
        # A gitlink. `git ls-files` lists submodule paths like any other entry,
        # and this is by far the commonest way a tracked path is not a regular
        # file -- so it must not be reported as a device node. The submodule's
        # own files are tracked in ITS repo and are never in this scan's scope
        # whether or not they were fetched (the reusable workflow checks the
        # caller out without `submodules:`, so the directory is usually empty).
        # (BE-8654 review.)
        return (
            None,
            [
                "is a submodule gitlink, so it was NOT scanned: its files are "
                "tracked in the submodule's own repository and need their own "
                "hygiene run there (this workflow checks the caller out "
                "without `submodules:`, so the directory is empty here)"
            ],
            "submodule gitlink",
            False,
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
            False,
        )

    try:
        with open(path, "rb") as fh:
            # One byte past the cap, so "exactly at the cap" is not misreported
            # as truncated.
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        # FOUR values, like every other route out of here and like the single
        # caller's unpack. A stray fifth crashed this path with `ValueError:
        # too many values to unpack` on any unreadable tracked file (EACCES on
        # a mode-000 blob, EMFILE, an I/O error), so the intended "unreadable,
        # so NOT scanned" warning became a traceback and exit 1 -- which the
        # workflow renders as "internal-only references found" with no finding
        # listed. Pinned by `test_an_unopenable_file_warns_instead_of_crashing`.
        # (BE-8729 review, round 5.)
        return None, [unreadable.format(exc.strerror or exc)], "unreadable", False

    truncated = len(data) > MAX_FILE_BYTES
    if truncated:
        data = data[:MAX_FILE_BYTES]

    text = _decode_bom(data, truncated)
    if text is _UNDECODABLE:
        return None, [], "non-UTF-8", truncated
    if text is not None:
        return _finish_text(text, len(data), truncated)

    if b"\x00" in data:
        return None, [], "binary", truncated
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
            return None, [], "non-UTF-8", truncated
        text = data[: exc.start].decode("utf-8")

    return _finish_text(text, len(data), truncated)


# Sentinel: a BOM was recognised but the bytes behind it would not decode.
_UNDECODABLE = object()


def _decode_bom(data, truncated):
    """Decode BOM-marked UTF-16/UTF-32; None if there is no such BOM."""
    for bom, codec, unit in _BOM_CODECS:
        if not data.startswith(bom):
            continue
        for drop in (0, unit):
            # The read cap can cut a code unit (or a surrogate pair) in half.
            # Only a TRUNCATED buffer earns that retry: on a complete file a
            # decode failure means the BOM was a lie, not that the tail is short.
            if drop and not truncated:
                break
            try:
                return data[: len(data) - drop].decode(codec)
            except UnicodeDecodeError:
                continue
        return _UNDECODABLE
    return None


def _finish_text(text, nbytes, truncated):
    """Shared tail: the truncation warning and the git-LFS classification."""
    warnings = []
    if truncated:
        warnings.append(
            f"is larger than {MAX_FILE_BYTES} bytes; only the first "
            f"{MAX_FILE_BYTES} were scanned"
        )
    if _is_lfs_pointer(text, nbytes):
        # Skipped for COVERAGE, still scanned for FINDINGS -- the two are
        # separate questions and this is the one file kind where they diverge.
        # It cannot count as `scanned`: the stub carries none of the file's
        # actual bytes, so `git lfs track '*.md'` plus a commit carrying
        # internal references would otherwise hold the zero-scan net open and
        # exit 0 on a required check. But it must still be READ, because the
        # only thing standing between "this is a stub" and "this is a file
        # pretending to be one" is the grammar above -- and a genuine stub is
        # three lines of hex and digits that yield no findings anyway, so
        # scanning it costs nothing and closes the classification off as a
        # bypass. (BE-8654 review.)
        warnings.append(
            "is a git-LFS pointer stub, so it does NOT count as scanned: the "
            "real content it stands for is publicly downloadable from this "
            "repo but is not in the work tree (`actions/checkout` does not "
            "fetch LFS objects by default), and the ~130-byte stub is not the "
            "file's text (the stub itself was still checked for references)"
        )
        return text, warnings, "git-LFS pointer", truncated
    return text, warnings, None, truncated


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


def _nonascii_tail(line, end):
    """The name characters after `end` that `REPO_REF_RE`'s ASCII class dropped.

    `REPO_REF_RE` captures ASCII only, so the match stops at the first character
    outside that class -- but a Unicode letter or a homoglyph dash is not the END
    of the name, it is the REST of it. Without this, the allowlist is tested
    against a PREFIX of what the file actually says: `Comfy-Org/comfyui<U+2010>internal`
    captures `comfyui`, casefolds into the known-public set and passes clean
    while the full private name sits in the tree. (BE-8654 review.)

    Returns "" for an ordinary match. A non-empty tail means the reference was
    only partly read, and `_file_findings` never CLEARS such a name -- casefold
    membership cannot be trusted over it either way round, since `.casefold()`
    folds U+017F back to `s` and would admit `comfy-type<U+017F>cript-sdk`.

    The rule is deliberately every non-ASCII name character, not just the ones
    that fold onto ASCII. Narrowing it to those would still clear
    `Comfy-Org/comfyui<U+0430>internal` (Cyrillic a: a homoglyph that folds to
    nothing ASCII, so the capture stops and `comfyui` clears on its own). The
    cost is that an allowlisted name butted straight against non-Latin PROSE
    (`Comfy-Org/ComfyUI<CJK>`) is a finding rather than a pass; the message says
    how to clear it, and a separator is all it takes.
    """
    if end >= len(line) or line[end].isascii():
        return ""
    out = []
    for ch in line[end:]:
        if ch.isascii():
            if ch not in _REPO_NAME_ASCII:
                break
        elif not (
            unicodedata.category(ch)[0] in "LNM"
            or unicodedata.category(ch) in _NAME_CONTINUING_CATEGORIES
        ):
            break
        out.append(ch)
    return "".join(out)


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


# Everything before the first UNESCAPED `#`, i.e. the non-comment body of a
# CODEOWNERS line. Both of these deliberately use DISJOINT alternation branches
# (`[^\\#]` / `[^\s\\]` cannot match the `\` that starts the escape branch), so
# each quantifier is linear. The ambiguous spellings (`(?:\\.|.)*`, `(?:\\.|\S)+`)
# backtrack exponentially over a long backslash run that nothing follows -- and
# these run over every line of a file that may be `MAX_FILE_BYTES` on one line.
_CODEOWNERS_BODY_RE = re.compile(r"(?:\\.|[^\\#])*")
# The leading PATH PATTERN plus the whitespace that ends it; owners follow.
_CODEOWNERS_PATTERN_RE = re.compile(r"\s*(?:\\.|[^\s\\])+\s+")


# A CODEOWNERS line is `<pattern> <owner>...`, with `#` starting a comment --
# the syntax the BE-8857 gate needs in order to key on the OWNER FIELDS rather
# than on the whole file. Classifying the file wholesale read every
# `@Comfy-Org/<name>` token in it as an owner handle, but two other things
# legally carry that spelling and are NOT handles: a `#` comment naming a
# package, and a scoped monorepo PATH PATTERN such as
# `/packages/@comfy-org/comfy-cli/**`. Both would have become hard "team not in
# the known-public allowlist" findings on a required check. (BE-8857 review.)
def _codeowners_owner_span(line):
    """Return the `(start, end)` offsets of LINE's owner fields, or `None`.

    `None` means "this line has no owner fields" -- a blank line, a comment, or
    a pattern with no owners after it -- and leaves the npm/Packages crossing
    exactly as it behaves in every other file. That fallback is the same
    accepted ambiguity documented for prose, not a new hole: the crossing still
    requires membership in the PUBLIC repo allowlist.

    Text after an unescaped `#` is treated as a comment. GitHub documents only
    whole-line comments, so a TRAILING `#` may in truth be owner text; excluding
    it can only fall back to the crossing (never widen the deny), and `#` is not
    a character a real owner handle contains.
    """
    body = _CODEOWNERS_BODY_RE.match(line).group(0)
    match = _CODEOWNERS_PATTERN_RE.match(body)
    if match is None:
        return None
    end = len(body.rstrip())
    if match.end() >= end:
        return None
    return (match.end(), end)


def _file_findings(rel, text, ticket_allowlist):
    """Yield one file's findings lazily, in report order.

    A generator rather than a list so `check_file` can stop it at the per-file
    cap: a 5 MiB line of repeated `AA-12` must not be fully enumerated just to
    throw the tail away.
    """
    # Per FILE, not per line or per match: the BE-8857 gate below needs it on
    # every owner line, and the answer cannot change within one file.
    is_codeowners = posixpath.basename(rel).casefold() == "codeowners"
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in TICKET_RE.finditer(line):
            token = match.group(0).upper()
            if token in ticket_allowlist:
                continue
            # A PUBLIC identifier namespace clears by prefix, not by exact
            # token (see TICKET_ALLOWED_PREFIXES): `CVE-2021-44228` presents
            # here as `CVE-2021`, and the year makes an exact carve-out expire.
            if token.split("-", 1)[0] in TICKET_ALLOWED_PREFIXES:
                continue
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

        # Per LINE, not per match: `_codeowners_owner_span` walks the line, so
        # recomputing it inside the match loop is quadratic on a single long
        # line -- the same shape `_excerpt` and `_bounded` guard against.
        owner_span = _codeowners_owner_span(line) if is_codeowners else None
        for match in REPO_REF_RE.finditer(line):
            name = match.group(1)
            # Characters the ASCII name class could not read are the REST of the
            # name, not a boundary. Carry them into what is reported, and (below)
            # never clear a name they appear in. (BE-8654 review.)
            tail = _nonascii_tail(line, match.end())
            name += tail
            # Strip a sentence-final period BEFORE the team/repo fork: a GitHub
            # repo or team slug can never end in `.`, so a trailing one is
            # always prose punctuation the `.`-permitting name class swallowed
            # (BE-8697). It has to happen here rather than in the repo branch
            # below, because the team branch needs it too -- `@Comfy-Org/
            # Comfy-Cloud-Team.` at the end of a sentence is the confirmed
            # false positive that motivated this. `rstrip` also handles the
            # ellipsis case, and a reference that is nothing BUT the period
            # (`Comfy-Org/.`) names no repo at all, so it is not a finding.
            name = name.rstrip(".")
            if not name:
                continue
            at_prefixed = match.start() > 0 and line[match.start() - 1] == "@"
            if tail:
                # Never cleared, and never SILENTLY cleared either: casefold
                # membership is untrustworthy in both directions over a name
                # like this -- `comfy-type<U+017F>cript-sdk` folds ONTO an
                # allowlisted name, and `comfyui<U+2010>internal` folds off the
                # end of one. Reported with its own remedy, because "add it to
                # the allowlist" is not the fix for a homoglyph. (BE-8654
                # review.)
                yield (
                    f"{rel}:{lineno}: reference to "
                    f"{'@' if at_prefixed else ''}Comfy-Org/{_bounded(name)}, "
                    "whose name carries non-ASCII characters that can render "
                    "identically to ASCII ones on github.com (a homoglyph), so "
                    "it is NOT cleared against the known-public allowlist -- "
                    "rewrite the name in ASCII, remove the reference, or (if "
                    "the non-ASCII text is adjacent PROSE rather than part of "
                    "the name) put a separator between them"
                )
                continue
            # A leading `@` makes this a CODEOWNERS team handle, not a repo ref
            # -- OR an npm / GitHub Packages scope, which is spelled exactly the
            # same way and is required to be lowercase. Before BE-8697 made the
            # org segment case-insensitive, the canonical `@comfy-org/<pkg>`
            # spelling in a `package.json` or lockfile did not match at all;
            # now it does, so this branch has to admit BOTH readings or a
            # dependency on a known-PUBLIC repo becomes "a team not in the
            # known-public allowlist" with no caller-side escape (the repo
            # allowlist is deliberately not a workflow input).
            #
            # EXCEPT on the OWNER FIELDS of a CODEOWNERS line, where only ONE
            # of the two readings is possible: an `@`-prefixed owner is a
            # handle and npm coordinates never appear there, so the repo
            # crossing is denied and team-allowlist membership is required.
            # The FILE is matched by BASENAME with `posixpath` -- `rel` is a
            # git-tracked path, `/`-separated whatever the host OS is. GitHub
            # only honors CODEOWNERS at `CODEOWNERS`, `.github/CODEOWNERS` and
            # `docs/CODEOWNERS`, so a basename match also covers files GitHub
            # ignores; that is the SAFE direction (it re-enables default-deny
            # on a file where no npm coordinate can live). Within the file the
            # deny is scoped by `_codeowners_owner_span`, because a `#` comment
            # and a scoped path pattern carry the same spelling without being
            # handles. (BE-8857, + its review.)
            #
            # The reverse crossing stays forbidden on purpose -- a bare
            # `Comfy-Org/<name>` is unambiguously a repo path, since there is no
            # syntax that writes a team without the `@`, so admitting team names
            # there would weaken default-deny with no false positive to justify
            # it. See test_team_allowlist_does_not_leak_into_repo_allowlist.
            if at_prefixed:
                folded = name.casefold()
                # The crossing is NARROWED to a spelling that could actually BE
                # an npm coordinate: those are required to be lowercase, so
                # `@comfy-org/comfy-cli` crosses and the canonical GitHub team
                # spelling `@Comfy-Org/comfy-cli` does not. Naming a team after
                # the repo it owns is the commonest CODEOWNERS convention there
                # is, so an unconditional crossing cleared exactly the likely
                # collision -- a team handle that is not in the team allowlist,
                # waved through because a public repo happens to share its name.
                # (BE-8654 review.) That narrowing is not enough on its own:
                # team slugs are lowercase BY CONSTRUCTION and GitHub resolves
                # the org segment case-insensitively, so `@comfy-org/comfy-cli`
                # in a CODEOWNERS file is a real, functional team handle that
                # the lowercase test alone waved through. Hence the CODEOWNERS
                # gate. Elsewhere the lowercase narrowing stands, because a
                # lowercase mention in a README, a Dockerfile `npm i` line or a
                # CI shell script genuinely could be an npm coordinate -- that
                # residual ambiguity is ACCEPTED: in prose the two readings are
                # indistinguishable, and re-denying there would re-open the
                # false-positive class the crossing exists to fix (see
                # test_npm_scope_of_a_public_repo_is_not_a_team_finding).
                # (BE-8857.)
                # Keyed on the OWNER FIELDS of a CODEOWNERS line, not on the
                # file as a whole: a `#` comment and a scoped path pattern
                # (`/packages/@comfy-org/comfy-cli/**`) legally carry the same
                # spelling without being handles. (BE-8857 review.) The
                # basename that produced `owner_span` compares CASEFOLDED --
                # git records the name the author typed, and this was the only
                # exact-case identity test left on a path where the org
                # segment, the `.git` strip and both allowlists are all
                # case-insensitive, so a `codeowners` spelling walked the gate.
                in_owner_field = owner_span is not None and (
                    owner_span[0] <= match.start() < owner_span[1]
                )
                npm_scope = (
                    not in_owner_field
                    and line[match.start() : match.end()].islower()
                )
                if folded not in _PUBLIC_TEAMS_CF and not (
                    npm_scope and folded in _PUBLIC_REPOS_CF
                ):
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
            # `Foo.git` is still a reference to the public repo `Foo`. Matched
            # case-insensitively like everything else on this path (BE-8697) --
            # a case-SENSITIVE strip here would leave `ComfyUI.GIT` carrying its
            # suffix into a membership test that then misses `comfyui`.
            repo = re.sub(r"\.git$", "", name, flags=re.IGNORECASE)
            # `Comfy-Org/.git` reaches here with the whole name consumed: the
            # period strip above left `.git` alone (no TRAILING dot) and the
            # suffix strip took the rest. Like `Comfy-Org/.`, it names no repo,
            # so there is nothing to report -- and reporting it would print the
            # repo-less "reference to Comfy-Org/" the guard above exists to
            # prevent.
            if not repo:
                continue
            if repo.casefold() not in _PUBLIC_REPOS_CF:
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
    """Return (findings, warnings, skip_kind, partial) for one file.

    `partial` is the list of PARTIAL_* kinds this file earned -- the coverage
    counterpart of the `::warning::` lines, which the per-run warning cap can
    drop.
    """
    text, warnings, skip_kind, truncated = _read_text(os.path.join(root, rel))
    partial = [PARTIAL_READ] if truncated else []
    if text is None:
        return [], warnings, skip_kind, partial

    # One past the cap, so "capped" is distinguishable from "exactly at it".
    findings = list(
        itertools.islice(
            _file_findings(rel, text, ticket_allowlist),
            MAX_FINDINGS_PER_FILE + 1,
        )
    )
    if len(findings) > MAX_FINDINGS_PER_FILE:
        del findings[MAX_FINDINGS_PER_FILE:]
        partial.append(PARTIAL_FINDINGS)
        warnings.append(
            f"produced more than {MAX_FINDINGS_PER_FILE} findings; only the "
            f"first {MAX_FINDINGS_PER_FILE} are listed. This file needs fixing "
            f"wholesale rather than finding-by-finding -- the run still FAILS"
        )
    return findings, warnings, skip_kind, partial


def run_checks(root, excludes=(), extra_ticket_allow=()):
    """Scan `root`, returning a `ScanResult`.

    `exclusions` is [(pattern, files_skipped)] in the caller's order, INCLUDING
    patterns that skipped nothing -- a typo'd exclusion that matches no file has
    to be visible in the log, not silently inert. `skipped` is the same
    accounting for the files the reader itself declined, [(kind, count)]. Both
    exist for one reason: `scanned` is the number of files this run actually
    read, and every file that is tracked but not in it has to be attributable to
    a named reason.

    Raises `ConfigError` (exit 2) before reading anything when the tree cannot
    be scanned honestly at all: tracked content at the reserved checkout path,
    or a `working-tree-encoding` gitattribute that makes the bytes on disk
    differ from the bytes git stores.
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

    # Only over what this run would actually READ: an encoding conversion on a
    # path the caller excluded hides nothing this scan was going to look at, and
    # failing the run over it would be a false alarm the caller cannot clear.
    converted = _work_tree_encoded(
        root, [r for r in tracked if _is_excluded(r, excludes) is None]
    )
    if converted:
        raise ConfigError(
            f"{len(converted)} tracked file(s) carry a `working-tree-encoding` "
            f"gitattribute (first: '{converted[0]}'), so what checkout writes "
            f"to disk is NOT the bytes git stores. This checker reads the work "
            f"tree, so a UTF-16 conversion turns the file into NUL-laden bytes "
            f"it skips as binary -- while the committed blob GitHub serves on "
            f"the web, in the API and in the diff stays plainly readable. That "
            f"would be a green run over content this guard never looked at. "
            f"Drop the attribute, or exclude those paths explicitly via "
            f"`exclude_paths:` so the hole is named in the log instead of "
            f"hidden."
        )

    counts = {p: 0 for p in excludes}
    skipped = collections.Counter()
    partial = collections.Counter()
    findings, warnings = [], []
    scanned = 0
    truncated_report = False
    suppressed_warnings = 0
    for rel in tracked:
        hit = _is_excluded(rel, excludes)
        if hit is not None:
            counts[hit] += 1
            continue
        found, file_warnings, skip_kind, file_partial = check_file(
            root, rel, ticket_allowlist
        )
        for kind in file_partial:
            partial[kind] += 1
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
            # Per-run cap, for the same reason the findings have one. Coverage
            # accounting is deliberately NOT capped: every file dropped here is
            # still counted in `skipped`, `partial` or `scanned` below.
            if len(warnings) < MAX_WARNINGS_TOTAL:
                warnings.append(f"{rel}: {warning}")
            else:
                suppressed_warnings += 1
        if skip_kind is None:
            scanned += 1
        else:
            skipped[skip_kind] += 1

    if suppressed_warnings:
        warnings.append(
            f"+{suppressed_warnings} more per-file warning(s) were produced "
            f"and are NOT listed; only the first {MAX_WARNINGS_TOTAL} are. The "
            f"exit code is unaffected, and so is coverage accounting -- every "
            f"file behind a suppressed warning is still counted in the "
            f"NOT SCANNED / PARTIAL totals or in SCANNED above"
        )

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
        findings,
        exclusions,
        warnings,
        sorted(skipped.items()),
        scanned,
        sorted(partial.items()),
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
    # Never capped, unlike the per-file warnings that name each one: a file
    # read only as far as the size cap still counts in `scanned`, so without
    # this line a buried warning would leave the report claiming full coverage
    # over a file whose tail was never read. (BE-8654 review.)
    for kind, count in result.partial:
        line = f"PARTIAL: {count} file(s) {_esc_cmd(kind)}"
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
