# public-repo-hygiene

The checker behind [`public-repo-hygiene.yml`](../workflows/public-repo-hygiene.yml): a
lightweight regression guard that fails CI when a **public** repo's tracked files carry
internal-only references. Setup guide for consumers:
[`docs/callers/public-repo-hygiene.md`](../../docs/callers/public-repo-hygiene.md).

It is **not** a secrets scanner. It looks for three categories of internal-only *reference*, not
for credentials, and it uses small explicit allow/deny lists rather than one clever regex — so a
false positive is a one-line list edit instead of a mystery.

## What it flags

| # | Category | Shape |
|---|---|---|
| 1 | Ticket-style identifiers | `\b[A-Z]{2,6}-\d{2,6}\b` — a generic SHAPE, never a list of real internal team keys, so the check itself discloses no internal naming scheme. Common tech acronyms that fit (`UTF-8`, `SHA-256`, `RFC-3339`, …) are allowlisted; a caller extends that list with the `ticket_allowlist:` input. |
| 2 | Internal collaboration-tool links | Notion (`notion.so`/`notion.site`), Slack (`slack.com/archives`, `slack.com/client`, `app.slack.com`), Google Docs/Drive, `app.datadoghq.com`, `posthog.com/project/`, `linear.app`, and `incident-NNN`. Case-insensitive. Public marketing pages on the same hosts (`posthog.com/docs`) are not matched. |
| 3 | `Comfy-Org/<repo>` references | **Default-deny** against `PUBLIC_COMFY_ORG_REPOS`. Anything else is flagged so a maintainer either scrubs it or adds it once confirmed public. A leading `@` makes the match a CODEOWNERS **team** handle instead, checked against `PUBLIC_COMFY_ORG_TEAMS` — team handles are inherently public on a public repo (GitHub renders CODEOWNERS owners to anyone who can see it), so an allowlisted one is not a leak. A trailing `.git` is stripped, because `Foo.git` in a `repository.url` still references the public repo `Foo`. |

Only **tracked** files are scanned (`git ls-files -z`), so build output, `node_modules` and
anything untracked is out of scope by construction. Binary files are skipped (a NUL byte or an
undecodable UTF-8 sequence), and only **regular** files are opened at all: `open()` follows a
symlink, so scanning one would read whatever it points *at* rather than the link target git
actually stores — a link out of the repo pulls arbitrary runner content into a public run log, and
one to `/dev/zero` or a FIFO turns the read into an OOM or a hang. A regular file is read up to
`MAX_FILE_BYTES` (5 MiB) and no further.

Everything the scan declines to look at leaves a trace, because a guard that silently skips
something is worse than no guard — the green run reads as coverage:

- every run prints `SCANNED: <n> file(s) read as text` — the number the rest of this list has to
  account for;
- an exclusion is logged with its skipped-file count, **including one that skipped nothing**;
- a file that cannot be *read* (a permission problem) or that is *not a regular file* (a symlink,
  FIFO, socket or device node) is a `::warning::` naming it, not a silent drop;
- binary and non-UTF-8 files are expected skips, so they are a per-run `NOT SCANNED: <n>` count
  rather than a warning each — a count they must have, because one stray byte otherwise hides a
  whole file that still renders as text on GitHub;
- a file larger than the read cap is scanned up to it and the unread tail is a `::warning::`
  naming the file — truncated loudly, never dropped;
- a run that read **zero** files (nothing tracked, everything excluded, or nothing readable as
  text) **exits 2**, not 0. Rejecting a root-wide exclusion would otherwise be one spelling away
  from pointless: naming each top-level directory in `exclude_paths` disables the whole scan
  without ever naming the root;
- `_public_repo_hygiene/` — where the workflow checks this repo out inside the caller's
  workspace — is a **reserved** path. The checkout lands untracked, so an ordinary run never meets
  it; a caller that *tracks* content there fails with exit 2 and is told to rename the directory,
  because that content is shadowed by the checkout and can never be examined. Skipping it quietly
  is how the reserved path would have become a parking spot that ships green.

## Why the allowlist lives here (BE-8654)

It began as two copies — `scripts/check_public_repo_hygiene.py` in the Python SDK and
`scripts/check-public-repo-hygiene.mjs` in the TypeScript SDK — each run out of the PR's own
checkout. Two problems, both observed rather than predicted:

- **The checker was editable by the PR it guarded.** The job was `actions/checkout` (which
  defaults to the PR merge ref) followed by `python3 scripts/check_public_repo_hygiene.py`. A PR
  could add a private repo name to the in-tree allowlist and then leak it, green. Loading the
  checker from a pinned ref of *this* repo is what closes that, exactly as
  `agents-md-integrity.yml` already does.
- **"Which Comfy-Org repos are public" is ORG-WIDE knowledge**, and copying it per repo let the
  copies rot independently. Both were missing `github-workflows` itself, so the caller every one
  of them needs failed the very check it was being added alongside — and fixing it meant the same
  edit twice, in two languages, in two repos.

Hosting the list in a public repo leaks nothing, and that is a property of the design, not luck:
the list contains **public repo names only**. No private repo name is ever written down — which
is the entire point of default-deny, and what makes this shareable at all.

## Tamper resistance — what is and is not caller-tunable

| Knob | Where it lives | Can a PR in the caller repo change it? |
|---|---|---|
| `PUBLIC_COMFY_ORG_REPOS` / `PUBLIC_COMFY_ORG_TEAMS` | this file, pinned by `workflows_ref` | **No.** Not an input in any form. An allowlist a caller can pass is an allowlist a PR in the caller repo can widen. |
| Which commit `workflows_ref` loads | the caller's workflow file | **No, in practice.** A `pull_request` caller runs its workflow file from the PR head, so a PR *can* edit `workflows_ref:` — which is why the workflow fails the run unless it is a full 40-hex SHA **equal to `job.workflow_sha`**, the commit the `uses:` pin resolved to. Pointing it at a `refs/pull/*` ref of this repo would otherwise run the PR author's own checker and report green. |
| Detection regexes | this file, pinned by `workflows_ref` | **No.** |
| `ticket_allowlist:` | the caller's workflow file | Yes — but it is **additive** (it can add an acronym, never drop a built-in one) and it reaches category 1 only. |
| `exclude_paths:` | the caller's workflow file | Yes — it drops files from the scan. It cannot widen a category, a value naming the repo root is rejected outright, and every entry is echoed to the run log with its skipped-file count *including one that skipped nothing*. |

The two tunable knobs are visible in the caller's workflow diff and in the run log, which is the
honest boundary: this design makes weakening the *rule* impossible from a caller repo and makes
narrowing the *scope* loud. Adding a repo to the allowlist is now one reviewed PR here rather
than one edit per consumer.

`tests/test_check_public_repo_hygiene.py::TamperResistanceTest` asserts the checker half (an
in-tree copy of the checker, a config file, and environment variables named after the constants
are all inert). [`test-public-repo-hygiene.yml`](../workflows/test-public-repo-hygiene.yml)
asserts the same end-to-end.

## Known limitations

Carried over deliberately from the two scripts this replaces. The migration was proven by running
old and new over the same trees and diffing findings, which is only worth something if the
migration changed nothing — so each of these is its own change, not a rider on the centralization.

- **A sentence-final period is swallowed by the repo-name class.** Prose ending
  `…built on Comfy-Org/ComfyUI.` is flagged as `ComfyUI.`, which is not in the allowlist. Both
  original copies did this. It is pinned by a unit test so it stays a decision rather than an
  accident. This is the first thing to fix before rolling out beyond a handful of repos — it fires
  on ordinary English.
- **The org segment is matched case-sensitively.** GitHub resolves owner names
  case-insensitively, so a lowercased `comfy-org/<private-repo>` reference is not flagged. Both
  original copies had this blind spot too.
- **The scan is line-oriented.** A reference split across two lines is not matched.

## Running it

```bash
python3 .github/public-repo-hygiene/check_public_repo_hygiene.py --root .
python3 .github/public-repo-hygiene/check_public_repo_hygiene.py --root . \
    --exclude 'src/generated/' --ticket-allow 'GPU-100'

python3 -m unittest discover -s .github/public-repo-hygiene/tests -p 'test_*.py' -v
```

Exit codes: `0` clean, `1` findings, `2` the run proves nothing — an unusable `--exclude`, a root
that is not a git work tree, tracked content at the reserved `_public_repo_hygiene/` path, or a
scan that read zero files. "Nothing to scan" must never read as "clean".

## Adding a repo to the allowlist

Confirm it is genuinely public first — `gh repo view Comfy-Org/<name> --json visibility` — then
add the name to `PUBLIC_COMFY_ORG_REPOS` with a one-line comment, and merge. The
`bump-public-repo-hygiene-callers.yml` fleet rolls the new SHA out to every enrolled caller; a
caller that is not on the roster keeps scanning against the old list, which is the usual
two-step-enrollment footgun this repo documents in `AGENTS.md`.
