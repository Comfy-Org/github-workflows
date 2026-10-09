# agents-md-integrity

The checker behind the reusable
[`agents-md-integrity.yml`](../workflows/agents-md-integrity.yml) workflow. It
lives here as the single source of truth so consumer repos carry only a thin
caller; the workflow loads this script from a pinned ref of
`Comfy-Org/github-workflows` (never from the caller's checkout, so a PR can't
rewrite the check).

- **`check_agents_md.py`** — the check. Operates on a repo tree and exits
  non-zero (with GitHub annotations) when any hard check fails. Enforces the
  Comfy `AGENTS.md` standard ("AGENTS.md, done right", Comfy Engineering Guide
  §10): a thin top-level `AGENTS.md` source of truth under a hard line ceiling
  and character limits (root and nested), a one-line `@AGENTS.md` `CLAUDE.md` shim, no divergent `.cursorrules`,
  per-subtree shims in monorepos, and a CODEOWNERS DRI. Inputs come from env
  vars (`MAX_LINES`, `WARN_LINES`, `MAX_CHARS`, `WARN_CHARS`,
  `MAX_LINE_CHARS`, `FORBID_CURSORRULES`, `CHECK_NESTED`, `REQUIRE_SHIM`,
  `REQUIRE_CODEOWNERS`, `AGENTS_FILE`) plus the `--exclude` flag; see the
  workflow header for the mapping.
- **`tests/`** — `unittest` suite, run by
  [`test-agents-md-integrity.yml`](../workflows/test-agents-md-integrity.yml).

Run locally against any repo:

```bash
python3 .github/agents-md-integrity/check_agents_md.py --root /path/to/repo
```

## Excluding payload subtrees (`--exclude` / `exclude_paths`)

The nested-shim rule ("every nested `AGENTS.md` needs a sibling `@AGENTS.md`
`CLAUDE.md`") is right for a monorepo subtree and **wrong for a repo whose
product IS agent instructions** — a plugin/skill marketplace ships
`AGENTS.md` + a real multi-line `CLAUDE.md` as distributable payload, and
turning that sibling into a shim would corrupt what gets published. Such a repo
used to have only one escape, `check_nested: false`, which silently drops nested
coverage for the **whole** repo.

`--exclude` (workflow input `exclude_paths`) carves out just those subtrees:

```bash
python3 .github/agents-md-integrity/check_agents_md.py --root . --exclude 'plugins/**'
```

```yaml
with:
  workflows_ref: <sha>
  exclude_paths: |
    plugins/**
```

- Repeatable, and one value may be comma- or newline-separated. Because `,` is
  always a separator, a path containing a literal comma cannot be expressed.
- Globs are repo-root relative; `*`/`?` stay within a path segment, `**`
  crosses **zero or more** segments (so `plugins/**/AGENTS.md` also matches
  `plugins/AGENTS.md`), a leading `**/` means "at any depth", and a glob
  matching a directory excludes everything beneath it — `plugins` and
  `plugins/**` are identical, and both prune once at `plugins` rather than once
  per child.
- **Additive**, never a replacement: the hardcoded `SKIP_DIRS` baseline
  (`node_modules`, `vendor`, `.git`, …) still applies.
- Applied during the **walk**, so an excluded subtree is never opened or
  line-counted — not post-filtered out of the findings.
- Every exclusion is echoed to the log as
  `EXCLUDED: <path> (matched <glob>)` (plus a `::notice::` annotation), and the
  configured globs are printed even when they match nothing. An exclusion that
  leaves no trace is how coverage rots invisibly.
- A glob matching the **root** agents file or `CLAUDE.md` is rejected with exit
  code **2** (`1` = a check failed, `0` = pass). Root compliance is the
  non-negotiable part of the standard and is not excludable. So are the two
  ways of asking for the whole repo without saying so: a glob that normalizes
  to nothing (`/`, `.`, `//`) and a glob made only of wildcard segments (`*`,
  `**`, `*/**`, `*/*`) — the latter would otherwise prune every top-level
  directory while `check_nested` still read `true`. Name the subtree.

## Character limits (`max_chars` / `warn_chars` / `max_line_chars`)

The line ceiling does not bound size: a paragraph is one line, so a file can sit
at 111 lines and still carry 200k+ characters, all of it auto-loaded into every
session through the `@AGENTS.md` shim. Three limits close that, applied to the
root file **and** every nested one the walk finds:

| Env / input | Default | Over it |
|---|---|---|
| `MAX_CHARS` / `max_chars` | `0` (off) | **fails** |
| `WARN_CHARS` / `warn_chars` | `25000` | warns |
| `MAX_LINE_CHARS` / `max_line_chars` | `3000` | warns |

`0` turns a limit off. A value that is not a whole number (`40k`, `40000.5`)
is a config error (exit 2), never a silent fallback — falling back would turn
the default-off hard ceiling off. The hard ceiling defaults off so a caller
bumping its pin never goes red on the bump; `40000` is the suggested value,
matching the size at which Claude Code starts warning about an oversized memory
file. Characters are decoded characters, newlines included (a CRLF counts as
two); lines split on CRLF / LF / CR only, so `L<n>` matches an editor. Every
finding names the file, its size (with a chars/4 token estimate) and its
longest lines, and points at the remedy: move rationale into `docs/agents/` and
leave a one-line pointer — a plain link, not an `@` import, which Claude Code
would still expand at session start.
