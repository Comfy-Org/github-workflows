# `public-repo-hygiene.yml` — stop internal-only references reaching a public repo

Read [the shared caller contract](README.md) first.

## What it does

Scans your repo's **tracked** files for three categories of internal-only reference and fails
CI if it finds any:

1. **Ticket-style identifiers** — `TEAM-1234`-shaped tokens. A generic shape, never a list of
   real internal team keys, so the check itself discloses nothing. Common tech acronyms
   (`UTF-8`, `SHA-256`, `RFC-3339`, …) are allowlisted; add your own with `ticket_allowlist:`.
2. **Internal collaboration-tool links** — Notion, Slack archives/client, Google Docs and Drive,
   Datadog, PostHog project URLs, Linear, and `incident-NNN` strings.
3. **`Comfy-Org/<repo>` references outside a default-deny known-public allowlist** — plus the
   `@Comfy-Org/<team>` CODEOWNERS-handle case, checked against a separate team allowlist.

It is a lightweight regression guard, **not** a secrets scanner. Fails with a non-zero exit and
GitHub annotations, so it wires in cleanly as a required status check. The checker lives in
[`.github/public-repo-hygiene/`](../../.github/public-repo-hygiene).

**The checker and its allowlist are loaded from this repo at the SHA you pin, never from your
checkout** — so a PR in your repo cannot weaken or disable the check that is judging it. That is
the difference from a `scripts/check-hygiene.py` you run yourself, and it is the reason this
workflow exists.

## Prerequisites

None. No secrets. Your repo must be a normal git checkout — the scan reads `git ls-files`, and a
tree with no git metadata is a hard **configuration error** (exit 2), never a silent pass.

## Caller

`.github/workflows/public-repo-hygiene.yml`:

```yaml
name: Public Repo Hygiene

on:
  pull_request:
  push:
    branches: [main]        # or [master] — your default branch

jobs:
  hygiene:
    permissions:
      contents: read
    uses: Comfy-Org/github-workflows/.github/workflows/public-repo-hygiene.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
```

Then ask a maintainer to add your repo to the `PUBLIC_REPO_HYGIENE_CALLERS` roster secret — the
pin does not move on its own, and a stale pin means a stale allowlist.

## Required permissions

```yaml
contents: read
```

## Inputs

| Input | Default | Notes |
|---|---|---|
| `ticket_allowlist` | `''` | Newline- or comma-separated acronyms that look like ticket IDs but are not (`GPU-100`, a SKU, a spec number). **Additive** on the built-in list — you can extend it, never shrink it. Matched case-insensitively. |
| `exclude_paths` | `''` | Newline- or comma-separated tracked paths to skip. `src/generated/` (trailing slash) excludes that subtree; `scripts/thing.py` excludes that exact file. A value naming the repo root (`/`, `.`) is rejected and the run fails. Every entry is reported in the log with its skipped-file count, including one that skipped nothing. |
| `workflows_ref` | — (**required**) | Pin to the SAME full commit SHA as `uses:`. No default on purpose, and the `Require a pinned workflows_ref` step FAILS the run — not warns — when it is empty, is not a full 40-hex SHA, or names a different commit than `uses:` (cross-checked against `job.workflow_sha`). The checker script — and the allowlist — load from this ref, so anything short of that would let a PR pick which checker judges it. |

Note what is **not** an input: the known-public repo and team allowlists. An allowlist a caller
can pass is an allowlist a PR in the caller repo can widen, which is the hole this workflow
closes. To allowlist a repo you have confirmed is public, open a PR against
`Comfy-Org/github-workflows` adding it to `PUBLIC_COMFY_ORG_REPOS` — one reviewed edit, org-wide,
instead of one per consumer.

## Tuning example

```yaml
    with:
      workflows_ref: <same-full-commit-sha>
      exclude_paths: |
        src/generated/
        spec/vendored-openapi.json
      ticket_allowlist: GPU-100, SKU-2024
```

## Gotchas

**Adopt on `pull_request` before making it a required check.** A repo that has never been scanned
will surface genuine findings on its first run — that is the point, but it is a triage ramp, not
a flip. Land the scrub first, then make it required.

**Repo and team references are matched case-insensitively, and sentence punctuation is stripped.**
GitHub resolves owner names case-insensitively, so `comfy-org/<repo>` is checked exactly like
`Comfy-Org/<repo>` — a lowercased reference to a non-public repo is a finding, and a
differently-cased reference to a public one is not. A trailing `.` is treated as sentence
punctuation and stripped before the allowlist lookup (`…built on Comfy-Org/ComfyUI.` is clean), on
both the repo and the `@Comfy-Org/<team>` path, because a GitHub slug can never end in a dot. What
is still not matched is a reference split across two lines — the scan is line-oriented. See
[the checker README](../../.github/public-repo-hygiene/README.md#known-limitations).

**A skip is always visible.** An exclusion is logged with its file count even when it matched
nothing; an unreadable file, a device node, a git-LFS pointer stub or a symlink becomes a
`::warning::` naming it; binary and non-UTF-8 files get a per-run `NOT SCANNED:` count; and every
run prints `SCANNED: <n> file(s) read as text`. A run that read **zero** files exits **2**, not 0 — enumerating every top-level directory
in `exclude_paths` disables the whole scan, and a green "clean" there would prove nothing. If you
see one of those lines, the run did less than it looks like.

**A symlink is scanned as its target *string*, never followed.** `open()` would read whatever the
link points at — content that is not your repo's, and that would land in a public run log. The
target string git actually tracks *is* yours and is published in the tree, so that string is what
gets scanned. A `::warning::` names each one.

**Git LFS content is not scanned.** The workflow checks you out without LFS, so an LFS-tracked file
is present only as its ~130-byte pointer stub. The stub is scanned and the file is named in a
`::warning::` — the real content, though publicly downloadable, is never examined.

**Findings are capped, the verdict is not.** At most 200 findings per file and 2000 per run are
listed, and a matched line is echoed as a 200-character excerpt. Hitting a cap adds a `::warning::`
saying so; the run still **fails**. The caps bound run-log volume and runner memory, which the
scanned repo's own content would otherwise control.

**`_public_repo_hygiene/` is a reserved path.** That is where the workflow checks the checker out
inside your workspace. It normally lands untracked, so you will never meet this — but if your repo
*tracks* anything at that path, the run fails with exit 2 telling you to rename the directory.
Tracked content there is shadowed by the checkout and could never be scanned.

**Findings quote the offending line into the run log.** On a public repo that line is already in
the public diff, so this adds no exposure — but if you enable this on a *private* repo, note that
its run log is where the matched text lands.

**Replacing an in-repo script?** Keep both for one PR and compare their output on the same tree
before deleting yours. Findings should be identical; if they are not, say so in the PR rather
than deleting the evidence.
