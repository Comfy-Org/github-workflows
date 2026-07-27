# `agents-md-integrity.yml` — enforce the Comfy `AGENTS.md` standard

Read [the shared caller contract](README.md) first.

## What it does

Checks that your repo follows the org's agent-instructions standard:

- a top-level `AGENTS.md` **exists** and stays under a hard line ceiling
  (`max_lines`, default 200; warns over `warn_lines`, default 150)
- a root `CLAUDE.md` **exists** and is a thin `@AGENTS.md` shim rather than a
  divergent copy. With the default `require_shim: true`, a repo that has
  `AGENTS.md` and *no* `CLAUDE.md` **fails** — Claude Code reads only `CLAUDE.md`
  and does not fall back, so a missing shim means invisible instructions. Set
  `require_shim: false` to only validate a `CLAUDE.md` that is already there.
- no legacy `.cursorrules` (gated by `forbid_cursorrules`)
- every nested monorepo `AGENTS.md` has a sibling `@AGENTS.md` shim and is under
  the ceiling (gated by `check_nested`)
- `AGENTS.md` has a CODEOWNERS DRI (`require_codeowners`, warn-only by default)

Fails with a non-zero exit and GitHub annotations, so it wires in cleanly as a
required status check. The checker lives in
[`.github/agents-md-integrity/`](../../.github/agents-md-integrity).

## Prerequisites

None. No secrets.

## Caller

`.github/workflows/agents-md-integrity.yml`:

```yaml
name: AGENTS.md Integrity

on:
  pull_request:
  push:
    branches: [main]        # or [master] — your default branch

jobs:
  check:
    permissions:
      contents: read
    uses: Comfy-Org/github-workflows/.github/workflows/agents-md-integrity.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
```

Then add your repo to `vars.AGENTS_MD_CALLERS`.

## Required permissions

```yaml
contents: read
```

## Inputs

| Input | Default | Notes |
|---|---|---|
| `max_lines` | `200` | Hard ceiling. Over this fails. |
| `warn_lines` | `150` | Warns without failing. |
| `forbid_cursorrules` | `true` | Fail on a legacy `.cursorrules`. |
| `check_nested` | `true` | Also check nested monorepo `AGENTS.md` files. |
| `require_shim` | `true` | A root `CLAUDE.md` shim must **exist** (and import `@AGENTS.md`). `false` still rejects a divergent `CLAUDE.md`, but tolerates its absence. |
| `require_codeowners` | `false` | Require a CODEOWNERS DRI for `AGENTS.md`. |
| `agents_file` | `AGENTS.md` | Override the filename. |
| `workflows_ref` | `main` | **Set to your `uses:` SHA** — the checker script loads from this ref. |

## The `CLAUDE.md` shim

Claude Code reads only `CLAUDE.md`, so the standard keeps content in `AGENTS.md`
and makes `CLAUDE.md` a two-line pointer. This repo's own is the reference:

```markdown
<!-- Agent instructions live in AGENTS.md (the cross-agent standard). This is a Claude Code shim: Claude reads only CLAUDE.md, so the import below pulls AGENTS.md in. Don't add content here — edit AGENTS.md. -->
@AGENTS.md
```

## Gotchas

**Adopt on `pull_request` before adding it as a required check.** An existing repo
with a 400-line `AGENTS.md` fails immediately; you want that visible on a PR, not
blocking the queue.

**`check_nested: true` on a large monorepo** can surface a lot at once. Land the
top-level fix first, then switch it on.
