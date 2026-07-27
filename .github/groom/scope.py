#!/usr/bin/env python3
"""Path scoping for a groom run (BE-4757) — validate, derive, contain, filter.

`groom.yml` gained a `path` input so a run (typically a manual dispatch, but any
caller may pin one) can audit ONE subdirectory instead of the whole repo — a
monorepo consumer can groom each subtree as its own unit on its own cadence. This
module is the CI reusable's half of that, and it deliberately mirrors the
invariants `scan-path-scoping-test.sh` paid for on the vulnscan side (BE-4655).

The design rule is **constrain, don't instruct**. `scope_label`/`scope_desc` are
prompt substitutions — asking an agent to "stay in services/api" enforces nothing.
So scoping is layered:

1. **Syntactic validation** (`validate_path`) — runs in the cheap `gate` job,
   before any billed agent. Rejects absolute paths, `..` COMPONENTS, empty
   components and anything outside a conservative charset. A dotted directory
   name (`services/my..svc`) is legitimate and must be ACCEPTED — only a `..`
   path component is dangerous, so this does not over-reject.
2. **Filesystem containment** (`resolve_within`) — runs in the finder job once
   the target is checked out. Normalizes both sides through `os.path.realpath`
   (the Python `cd && pwd -P`) before prefix-comparing, so a symlinked or
   `$TMPDIR`-style trailing-slash path can neither escape nor report a FALSE
   escape. It also proves the directory actually exists, so a typo'd dispatch
   fails loudly instead of auditing an empty file list and reporting "clean".
3. **A concrete file list** handed to the finder (`list_files`) instead of prose.
   An EMPTY list fails the run in `groom.yml` rather than being handed over:
   existing (which step 2 proves) and being auditable are different things — a
   fully-gitignored or empty directory would otherwise buy a billed agent run
   that reviews nothing and reports "clean".
4. **Post-filtering** (`filter_findings`) of any finding whose evidence sites
   all fall outside the scope, with the dropped count LOGGED — a silent drop
   reads as "clean directory", which is the failure this exists to prevent.
5. **The same test again after the VERIFIER** (`filter_verified`), because the
   verifier is allowed to reshape a finding — a `DOWNGRADE` narrows it — so a
   cross-boundary candidate that legitimately passed step 4 can end up narrowed
   onto its out-of-scope half. Plus `canonicalize_signature`, which enforces the
   scope-independent dedup key the verifier brief merely ASKS for.

The checkout stays FULL on purpose (a refactor in `services/api` legitimately
references `common/`); the constraint is on what may be REPORTED, not on what may
be read.

Two things this module deliberately does NOT touch:

* **The dedup signature.** It stays content-derived (see `verifier.md`'s
  `{{SIG_SCOPE}}`, which is wired to `derive_scope`'s `sig_scope` — the caller's
  own `scope_label`, normalized but NEVER path-derived), so a defect filed by a
  scoped run is recognised and suppressed by a later whole-repo run and vice
  versa. A path-derived signature would file the same defect twice under two
  scopes. `canonicalize_signature` ENFORCES that rather than trusting the brief.
* **The cadence clock.** That lives in `interval.py`, and it is per-scope — a
  scoped run must not stamp "done" over the whole-repo audit it did not perform,
  and a permanently scoped caller must still get a working cadence of its own.

CLI (what the workflow steps call):

    python3 scope.py validate --path 'services/api'          # -> normalized path on stdout
    python3 scope.py derive   --path 'services/api' \
        --scope-label whole-repo --scope-desc 'the whole repository'   # -> JSON
    python3 scope.py contain  --root /path/to/clone --path services/api
    python3 scope.py filter   --path services/api --clone /path/to/clone \
        --in /tmp/groom-finder.json --out /tmp/groom-finder.json
    python3 scope.py verify   --path services/api --clone /path/to/clone \
        --sig-scope whole-repo \
        --in /tmp/groom-verifier.json --out /tmp/groom-verifier.json
"""

import argparse
import json
import os
import re
import subprocess
import sys

# The `scope_label` / `scope_desc` input defaults, duplicated from groom.yml. A
# derived value only replaces an input that still holds its DEFAULT — that is the
# only way an Actions reusable can distinguish "not provided" from "provided",
# and it is what makes an explicit caller override win over the path derivation.
DEFAULT_SCOPE_LABEL = "whole-repo"
DEFAULT_SCOPE_DESC = "the whole repository"

# Conservative per-component charset. Deliberately excludes shell/markdown
# metacharacters, whitespace, `:` and `\` — the derived label is interpolated
# into an issue body inside backticks and into agent prompts, and the path itself
# reaches `git ls-files`, so keeping the charset boring removes that whole family
# of concerns in ONE place instead of at each use site. It still admits every
# real-world source directory name, including a DOTTED one (`my..svc`).
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Trailing `:12`, `:12-40` or `:12:5` on an evidence site — the finder brief asks
# for `file:line`, so the location suffix is stripped before the containment test.
_SITE_LOCATION_RE = re.compile(r":\d+(?:[:-]\d+)?$")

# How many in-scope files to inline in the finder prompt. A hard cap keeps a
# monorepo subtree from blowing the prompt budget; truncation is ANNOUNCED in the
# prompt rather than silently swallowed (a silently short list reads to the agent
# as "that is the whole directory").
_MAX_LISTED_FILES = 1500

# The verdicts that actually reach the filing job (`groom.yml`'s validate step
# keeps exactly these). A REJECT is discarded downstream regardless, so the
# verifier-side scope filter leaves it alone.
_FILED_VERDICTS = ("CONFIRM", "DOWNGRADE")


class UnsafePathError(ValueError):
    """A `path` input that must never reach `git ls-files` or a prompt."""


def validate_path(raw) -> str:
    """Normalize the `path` input, or raise `UnsafePathError`.

    Returns "" for an unset/blank path — the whole-repo default, which must
    reproduce today's behavior byte-for-byte. Otherwise returns the path with
    any `./` prefix and trailing slashes removed.

    Rejected: absolute paths, `~`-relative paths, backslashes, control
    characters, an empty component (`a//b`), a `.` component, a `..` COMPONENT,
    and any component outside `_COMPONENT_RE`. NOT rejected: a component that
    merely CONTAINS dots (`services/my..svc`) — over-rejecting that is the
    documented trap from the vulnscan suite.
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    if text == "":
        return ""
    if any(ch in text for ch in ("\\", "\x00")) or any(ord(ch) < 0x20 for ch in text):
        raise UnsafePathError(f"path {raw!r} contains a backslash or control character")
    if text.startswith("/"):
        raise UnsafePathError(f"path {raw!r} is absolute — pass a path relative to the repo root")
    if text.startswith("~"):
        raise UnsafePathError(f"path {raw!r} is home-relative — pass a path relative to the repo root")
    # Strip a leading `./` and any trailing slashes BEFORE splitting, so the
    # ergonomic `./services/api/` normalizes instead of failing on empty/dot
    # components a caller could not reasonably be expected to avoid.
    while text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/")
    if text == "":
        # `.` or `./` or `/`-only after stripping — the whole repo, spelled oddly.
        raise UnsafePathError(f"path {raw!r} resolves to the repo root — leave `path` empty for a whole-repo run")
    parts = text.split("/")
    for part in parts:
        if part == "":
            raise UnsafePathError(f"path {raw!r} has an empty component (doubled separator)")
        if part == ".":
            raise UnsafePathError(f"path {raw!r} has a '.' component")
        if part == "..":
            raise UnsafePathError(f"path {raw!r} has a '..' component — it could escape the repo root")
        if not _COMPONENT_RE.match(part):
            raise UnsafePathError(
                f"path {raw!r} has an unsupported component {part!r} — allowed: letters, digits, '.', '_', '-'"
            )
    return "/".join(parts)


def derive_scope(path: str, scope_label, scope_desc) -> dict:
    """Drive the cosmetic scope inputs from `path`, without clobbering overrides.

    A filed issue must say WHERE the finding came from, so a scoped run should
    read `services/api`, not `whole-repo`. But an explicit caller override has to
    win — the studio fleet already labels its cloud subtrees itself. An Actions
    reusable cannot see whether an input was supplied, so "still equal to the
    documented default" is the (only, and stated) proxy for "not overridden".
    """
    # Collapse whitespace runs (including newlines) to single spaces: these values
    # are written to $GITHUB_OUTPUT as `key=value`, where an embedded newline
    # would truncate the value and inject a bogus output key. The YAML `>-` inputs
    # already fold to one line, so this is byte-identical for every real caller —
    # it just removes the injection shape rather than trusting the folding.
    label = " ".join((scope_label or "").split()) or DEFAULT_SCOPE_LABEL
    desc = " ".join((scope_desc or "").split()) or DEFAULT_SCOPE_DESC
    # The DEDUP-signature scope forks from the presentation label here, BEFORE the
    # path derivation. It gets the same normalization (whitespace collapse, blank
    # -> the documented default) — a blank `scope_label:` would otherwise reach
    # verifier.md's `{{SIG_SCOPE}}` empty and yield a malformed `repo::slug` — but
    # it deliberately never absorbs `path`, which is what lets a scoped run and a
    # whole-repo run recognise each other's findings. A caller that sets an
    # explicit `scope_label` is choosing its own dedup namespace, and that choice
    # is honoured verbatim (changing it would re-file every finding once).
    sig_scope = label
    if path:
        if label == DEFAULT_SCOPE_LABEL:
            label = path
        if desc == DEFAULT_SCOPE_DESC:
            desc = f"the `{path}` directory"
    return {"path": path, "scope_label": label, "scope_desc": desc, "sig_scope": sig_scope}


def resolve_within(root: str, path: str) -> str:
    """Absolute path of `path` inside `root`, proving it cannot escape.

    Both sides go through `os.path.realpath` — the Python equivalent of
    `cd && pwd -P` — BEFORE the prefix compare, which is what makes the check
    correct in the two ways a naive `startswith` is not: a symlinked component
    that points outside the tree is resolved (so it cannot escape), and a root
    that carries a trailing slash (macOS `$TMPDIR` is the classic) is normalized
    (so it does not report a FALSE escape). `realpath` strips trailing
    separators, hence the explicit `+ os.sep` on the prefix.
    """
    root_real = os.path.realpath(root)
    if not path:
        return root_real
    lexical = os.path.join(root_real, path)
    target = os.path.realpath(lexical)
    if target != root_real and not target.startswith(root_real + os.sep):
        raise UnsafePathError(f"path {path!r} resolves outside the checkout ({target} not under {root_real})")
    if target == root_real:
        raise UnsafePathError(f"path {path!r} resolves to the checkout root — leave `path` empty for a whole-repo run")
    if not os.path.isdir(target):
        raise UnsafePathError(f"path {path!r} is not a directory in this checkout ({target})")
    if target != lexical:
        # The scope traverses a symlink that stays INSIDE the checkout, so the
        # containment test above passed. Reject it anyway: `git ls-files -- <link>`
        # lists the LINK (one entry), not the target directory's files, so the
        # non-empty guard in groom.yml would be satisfied while the finder audits
        # nothing and reports the directory clean — the silent-clean failure this
        # module exists to prevent. Downstream steps also keep using the lexical
        # `path` (it has to stay repo-relative for `git ls-files` and for the site
        # filter), so resolving it here and auditing something else would be a lie.
        # Naming the real directory instead is a one-word fix for the caller.
        raise UnsafePathError(
            f"path {path!r} is (or traverses) a symlink to {target!r} — pass the real directory, "
            "since a symlinked scope enumerates the link rather than the files under it"
        )
    return target


def normalize_site(site, clone: str = "") -> str:
    """Repo-relative file part of an evidence site (`file:line` -> `file`).

    Defensive about the shapes an LLM actually emits: a bare path, a `file:line`,
    a `file:line-line` range, a `./`-prefixed path, or an absolute path inside the
    runner's clone. Anything unparseable returns "" and is treated as out of
    scope by the caller — a finding we cannot LOCATE is a finding we cannot
    attribute to the audited directory.

    Traversal is COLLAPSED before the caller's lexical prefix test, and a site
    that climbs out of the repo root returns "". Without this,
    `services/api/../../common/x` passes a naive `startswith('services/api/')`
    while actually resolving outside the scope — the same `..` hole `validate_path`
    already closes on the input side, on the side the AGENT controls.
    """
    if not isinstance(site, str):
        return ""
    text = site.strip()
    if not text:
        return ""
    text = _SITE_LOCATION_RE.sub("", text).strip()
    if clone:
        clone_prefix = clone.rstrip("/") + "/"
        if text.startswith(clone_prefix):
            text = text[len(clone_prefix):]
        elif text.startswith("/"):
            # Absolute, and NOT under the runner's checkout — so it names a file
            # outside the repository entirely. Relativizing it (dropping the
            # leading slash) would silently REINTERPRET it as repo-relative:
            # `/services/api/x.go` would then satisfy a `services/api` scope, and
            # `/etc/passwd` would satisfy an `etc` one. We know where the clone is,
            # so an absolute path outside it is unlocatable, not repo-relative.
            # (Without a `clone` we cannot tell the two apart, so the lenient
            # leading-slash strip below still applies in that case.)
            return ""
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    text = text.rstrip("/")
    if not text:
        return ""
    # `normpath` also folds `a//b` and `a/./b`; it is purely lexical (no
    # filesystem access), which is what we want — the site may name a file the
    # finder hallucinated, and touching the disk here would be a different check.
    text = os.path.normpath(text)
    if text == "." or text == ".." or text.startswith("../"):
        return ""
    return text


def site_in_scope(site, path: str, clone: str = "") -> bool:
    """Is one evidence site inside the audited directory?"""
    if not path:
        return True
    norm = normalize_site(site, clone)
    if not norm:
        return False
    return norm == path or norm.startswith(path + "/")


def finding_in_scope(finding, path: str, clone: str = "") -> bool:
    """Keep a finding if ANY of its evidence sites is inside the audited directory.

    ANY, not ALL, and this is a deliberate call. The checkout is kept FULL
    precisely because a refactor in `services/api` legitimately references
    `common/` — a duplication finding that spans the two IS a finding about the
    audited directory, and requiring every site to be in scope would suppress
    exactly the cross-cutting findings the full checkout exists to enable. What
    gets dropped is a finding whose evidence lies ENTIRELY outside the scope
    (including one with no locatable sites at all, which cannot be attributed).
    """
    if not path:
        return True
    if not isinstance(finding, dict):
        return False
    sites = finding.get("sites")
    if not isinstance(sites, list):
        return False
    return any(site_in_scope(s, path, clone) for s in sites)


def filter_findings(findings, path: str, clone: str = ""):
    """Partition findings into (kept, dropped) against the audited directory."""
    if not path:
        return list(findings or []), []
    kept, dropped = [], []
    for finding in findings or []:
        (kept if finding_in_scope(finding, path, clone) else dropped).append(finding)
    return kept, dropped


def filter_verified(findings, path: str, clone: str = ""):
    """Partition VERIFIED findings into (kept, dropped, unlocatable_count).

    The finder-side `filter_findings` is not enough on its own. Between it and
    filing sits the verifier — a model-driven step reading untrusted repository
    content — and it is allowed to RESHAPE a finding: a `DOWNGRADE` explicitly
    means "real but narrower". A cross-boundary candidate that legitimately
    passed the finder filter (one site in `services/api`, one in `common/`) can
    therefore be narrowed onto its OUT-of-scope half, by honest adjudication or
    by injected repo content, and be filed under a scope it no longer belongs to.
    So the same ANY-site rule is re-applied to what the verifier confirmed.

    One deliberate difference: a finding with no LOCATABLE site is KEPT here,
    where the finder-side filter drops it. `sites` is advisory on the verifier's
    schema (the validate step hard-checks `verdict`/`title`/`body`, not `sites`),
    so "no locatable sites" most often means the verifier omitted or garbled the
    field, not that the finding left the directory — and dropping on that would
    discard every survivor and render as an honest "nothing survived
    verification", the silent-clean failure this module exists to prevent. The
    count is returned so the caller can say so out loud.

    `REJECT` entries pass through untouched: they are discarded downstream
    anyway, and scope-filtering them would only inflate the dropped count.
    """
    if not path:
        return list(findings or []), [], 0
    kept, dropped, unlocatable = [], [], 0
    for finding in findings or []:
        if not isinstance(finding, dict) or finding.get("verdict") not in _FILED_VERDICTS:
            kept.append(finding)
            continue
        sites = finding.get("sites")
        located = [s for s in (sites if isinstance(sites, list) else []) if normalize_site(s, clone)]
        if not located:
            unlocatable += 1
            kept.append(finding)
        elif any(site_in_scope(s, path, clone) for s in located):
            kept.append(finding)
        else:
            dropped.append(finding)
    return kept, dropped, unlocatable


def canonicalize_signature(signature, sig_scope: str):
    """Force a dedup signature's SCOPE component back to the one we handed out.

    A signature is `<repo-basename>:<scope>:<slug>`, and its scope component must
    be the caller's own `scope_label` — never the audited directory. That is the
    single property that makes one defect file ONCE whether a scoped run or the
    scheduled whole-repo sweep found it (see `derive_scope`'s `sig_scope`).

    Until this, that property rested entirely on the verifier brief's instruction
    to copy `{{SIG_SCOPE}}` verbatim — an instruction given to a model reading
    untrusted repository content. This module's rule is constrain, don't instruct,
    and it applies here too: model variation or an injected "use the directory
    name" would file the same defect once per scope, the exact double-filing the
    scope-independent signature exists to prevent.

    Only a signature with the full three-part shape is rewritten. Anything else is
    returned UNTOUCHED: the ledger already routes a malformed signature to
    `invalid` with a warning, and inventing a shape here would turn a visible
    producer error into a silently mis-keyed issue. A slug cannot contain `:`
    (the brief derives it from a normalized title), so everything after the
    second separator is rejoined as the slug rather than re-split.

    `scope_label` is free-form caller text, so `sig_scope` may ITSELF contain a
    colon (`monorepo:api`) and then the component boundaries are ambiguous. An
    already-correct signature is recognised by prefix (which works for any number
    of colons) and left alone; a DEVIATING one under such a label is left alone
    too, because guessing where a multi-part scope ends would corrupt a working
    dedup key — a worse outcome than the double-filing this guards against.
    """
    if not isinstance(signature, str) or not sig_scope:
        return signature
    head, sep, rest = signature.partition(":")
    if not sep:
        return signature
    if rest.startswith(sig_scope + ":"):
        return signature
    if ":" in sig_scope:
        return signature
    parts = signature.split(":")
    if len(parts) < 3:
        return signature
    return f"{head}:{sig_scope}:{':'.join(parts[2:])}"


def printable_path(name: str) -> bool:
    """Is this tracked filename safe to inline in an agent prompt verbatim?

    Rejects control characters (a newline forges extra `- {f}` bullets in the
    finder's file list) and lone surrogates, which is how `list_files` carries
    the non-UTF-8 filename bytes Git permits. A surrogate cannot be encoded back
    out, so inlining one would crash the prompt write instead of the file merely
    going unlisted.
    """
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF for ch in name)


def _as_text(raw) -> str:
    """Decode `git` output, tolerating the non-UTF-8 bytes Git allows in paths.

    `subprocess.run` is injectable for tests, so a stub may hand back `str`
    already; both shapes are accepted rather than making the double's return type
    load-bearing.
    """
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "surrogateescape")
    return raw or ""


def list_files(root: str, path: str, run=subprocess.run):
    """Tracked files under `path`, via `git ls-files` in the checkout.

    Tracked-only on purpose: it is what the finder can actually read and review,
    and it excludes build output a full checkout may carry.

    Names carrying a newline or other control character are DROPPED. Git permits
    them, and every one of these names is interpolated verbatim into the finder
    prompt as a `- {f}` bullet the agent treats as authoritative — so a planted
    filename containing a newline could forge extra list entries or inject
    instructions. Dropping is safe (the file is simply not enumerated; the brief
    already says the scope is the whole directory, not just the listed files) but
    it is never SILENT: a shortened list presented as complete reads as "that is
    the whole directory", so the dropped count is warned about here, next to the
    only place that knows it.

    Output is read as BYTES and decoded with `surrogateescape`, not as text. Git
    permits arbitrary non-UTF-8 bytes in a filename, and a strict decode against
    the runner locale would raise `UnicodeDecodeError` on the first such tracked
    file — aborting the whole scoped audit before the finder runs, and before
    `printable_path` ever gets the chance to drop just that one name.
    """
    result = run(
        ["git", "-C", root, "ls-files", "-z", "--", path or "."],
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {_as_text(result.stderr).strip()}")
    names = [f for f in _as_text(result.stdout).split("\0") if f]
    listed = [f for f in names if printable_path(f)]
    if len(listed) != len(names):
        print(
            f"::warning::scope: omitted {len(names) - len(listed)} of {len(names)} tracked filename(s) "
            f"under `{path or '.'}` from the finder's file list (control characters or non-UTF-8 bytes). "
            "The audited scope is still the WHOLE directory — only the enumeration is short."
        )
    return listed


def scope_note(path: str) -> str:
    """The HARD SCOPE paragraph appended to the finder AND verifier briefs.

    States the post-filter explicitly, so an out-of-scope finding is a wasted
    finding rather than a surprise drop — and states just as explicitly that
    READING outside the directory is fine, because the checkout is full and the
    context outside is often what makes a finding correct.

    The rule it states is `finding_in_scope`'s ANY-in-scope rule, verbatim. Asking
    for EVERY site to be in scope would read as a stricter contract than the
    filter enforces and would suppress exactly the cross-boundary findings
    (a duplication spanning `services/api` and `common/`) that keeping the
    checkout FULL exists to enable.
    """
    return (
        f"HARD SCOPE — this run audits ONLY the `{path}` directory of the repository. "
        f"AT LEAST ONE entry in a finding's `sites` list MUST be a file under `{path}/`, and it must be "
        "the site the finding is actually ABOUT. A finding whose evidence lies ENTIRELY outside the "
        "directory is DROPPED after you finish, so reporting one is wasted work. A finding that spans "
        f"the boundary — e.g. logic duplicated between `{path}/` and code elsewhere — is IN scope and "
        f"wanted: list every relevant site, inside `{path}/` and out. "
        "You MAY read any file in the repository for CONTEXT (a refactor inside this directory "
        "legitimately references code outside it) — the restriction is on what you REPORT, not on "
        "what you read."
    )


def file_list_block(files, path: str) -> str:
    """`scope_note` plus the concrete in-scope file list, for the finder brief.

    This is the "constrain, don't instruct" half that a prompt CAN carry: a
    concrete enumeration beats prose.
    """
    total = len(files)
    shown = files[:_MAX_LISTED_FILES]
    lines = [
        "",
        "",
        scope_note(path),
        "",
        f"In-scope tracked files ({total}):",
    ]
    lines += [f"- {f}" for f in shown]
    if total > len(shown):
        lines.append(
            f"- …and {total - len(shown)} more (list truncated at {_MAX_LISTED_FILES}; "
            f"the scope is the WHOLE `{path}` directory, not just the files listed)."
        )
    lines.append("")
    return "\n".join(lines)


def _cmd_validate(args) -> int:
    print(validate_path(args.path))
    return 0


def _cmd_derive(args) -> int:
    path = validate_path(args.path)
    print(json.dumps(derive_scope(path, args.scope_label, args.scope_desc)))
    return 0


def _cmd_contain(args) -> int:
    path = validate_path(args.path)
    print(resolve_within(args.root, path))
    return 0


def _cmd_filter(args) -> int:
    path = validate_path(args.path)
    with open(args.infile, encoding="utf-8") as f:
        document = json.load(f)
    findings = document.get("findings") if isinstance(document, dict) else None
    if not isinstance(findings, list):
        # FAIL, don't shrug. There is no downstream check that catches this: the
        # workflow's only assertion is `jq '.findings | length'`, and jq scores a
        # MISSING field as 0 — identical to a genuinely clean directory. A finder
        # that emitted `{}` structurally failed, and letting that render as "the
        # scoped directory is clean, run green" is precisely the silent-clean
        # failure the rest of this module exists to prevent. (An empty but
        # PRESENT `"findings": []` is the real clean case and passes below.)
        print(
            "::error::scope filter: finder output has no `findings` array — "
            "the finder produced structurally invalid output, not a clean directory.",
            file=sys.stderr,
        )
        return 1
    kept, dropped = filter_findings(findings, path, args.clone or "")
    if dropped:
        # LOUD, and itemized. A silent drop is indistinguishable from a clean
        # directory, which is the whole reason this is counted.
        print(
            f"::warning::scope filter: dropped {len(dropped)} finding(s) whose evidence lies "
            f"entirely outside `{path}` (kept {len(kept)})."
        )
        for finding in dropped:
            title = finding.get("title") if isinstance(finding, dict) else None
            sites = finding.get("sites") if isinstance(finding, dict) else None
            print(f"::warning::  dropped (out of scope `{path}`): {title!r} sites={sites!r}")
    print(f"scope filter: kept {len(kept)}, dropped {len(dropped)} (scope `{path or 'whole-repo'}`)")
    document["findings"] = kept
    document["scope_dropped"] = len(dropped)
    with open(args.outfile, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2)
    return 0


def _cmd_verify(args) -> int:
    path = validate_path(args.path)
    with open(args.infile, encoding="utf-8") as f:
        document = json.load(f)
    findings = document.get("findings") if isinstance(document, dict) else None
    if not isinstance(findings, list):
        # Same reasoning as `_cmd_filter`: a missing array is a structural
        # producer failure, and letting it read as "nothing survived
        # verification" is the silent-clean failure this module prevents.
        print(
            "::error::scope verify: verifier output has no `findings` array — "
            "the verifier produced structurally invalid output, not an empty verdict.",
            file=sys.stderr,
        )
        return 1
    rewritten = 0
    if args.sig_scope:
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            canonical = canonicalize_signature(finding.get("signature"), args.sig_scope)
            if canonical != finding.get("signature"):
                finding["signature"] = canonical
                rewritten += 1
        if rewritten:
            print(
                f"::warning::scope verify: rewrote the scope component of {rewritten} signature(s) to "
                f"`{args.sig_scope}` — the verifier did not copy the scope literal it was given. Left as "
                "emitted, the same defect would be filed once per scope instead of once."
            )
    kept, dropped, unlocatable = filter_verified(findings, path, args.clone or "")
    if dropped:
        print(
            f"::warning::scope verify: dropped {len(dropped)} verified finding(s) the verifier narrowed "
            f"onto evidence entirely outside `{path}` (kept {len(kept)})."
        )
        for finding in dropped:
            title = finding.get("title") if isinstance(finding, dict) else None
            sites = finding.get("sites") if isinstance(finding, dict) else None
            print(f"::warning::  dropped (out of scope `{path}`): {title!r} sites={sites!r}")
    if unlocatable:
        print(
            f"::warning::scope verify: {unlocatable} verified finding(s) carry no locatable `sites` and were "
            f"KEPT unchecked against `{path}` — the verifier omitted or garbled the field, so scope could not "
            "be re-confirmed for them."
        )
    print(
        f"scope verify: kept {len(kept)}, dropped {len(dropped)}, unlocatable {unlocatable}, "
        f"signatures rewritten {rewritten} (scope `{path or 'whole-repo'}`)"
    )
    document["findings"] = kept
    document["scope_dropped_verified"] = len(dropped)
    with open(args.outfile, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Groom path scoping (BE-4757).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="normalize + syntactically validate the path input")
    p_validate.add_argument("--path", default="")
    p_validate.set_defaults(func=_cmd_validate)

    p_derive = sub.add_parser("derive", help="derive scope_label/scope_desc from the path")
    p_derive.add_argument("--path", default="")
    p_derive.add_argument("--scope-label", default=DEFAULT_SCOPE_LABEL)
    p_derive.add_argument("--scope-desc", default=DEFAULT_SCOPE_DESC)
    p_derive.set_defaults(func=_cmd_derive)

    p_contain = sub.add_parser("contain", help="prove the path resolves inside the checkout")
    p_contain.add_argument("--root", required=True)
    p_contain.add_argument("--path", default="")
    p_contain.set_defaults(func=_cmd_contain)

    p_filter = sub.add_parser("filter", help="drop findings whose evidence is outside the scope")
    p_filter.add_argument("--path", default="")
    p_filter.add_argument("--clone", default="")
    p_filter.add_argument("--in", dest="infile", required=True)
    p_filter.add_argument("--out", dest="outfile", required=True)
    p_filter.set_defaults(func=_cmd_filter)

    p_verify = sub.add_parser(
        "verify", help="re-apply the scope to VERIFIED findings + canonicalize their signature scope"
    )
    p_verify.add_argument("--path", default="")
    p_verify.add_argument("--clone", default="")
    p_verify.add_argument("--sig-scope", default="")
    p_verify.add_argument("--in", dest="infile", required=True)
    p_verify.add_argument("--out", dest="outfile", required=True)
    p_verify.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except UnsafePathError as exc:
        # A rejected path FAILS the run — unlike the cadence/volume gates, there is
        # no safe "fail open" reading of "audit a directory I could not validate".
        print(f"::error::Invalid groom `path` input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
