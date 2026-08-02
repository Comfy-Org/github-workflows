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
  §10): a thin top-level `AGENTS.md` source of truth under a hard line ceiling,
  a one-line `@AGENTS.md` `CLAUDE.md` shim, no divergent `.cursorrules`,
  per-subtree shims in monorepos, and a CODEOWNERS DRI. Inputs come from env
  vars (`MAX_LINES`, `WARN_LINES`, `FORBID_CURSORRULES`, `CHECK_NESTED`,
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

- Repeatable, and one value may be comma- or newline-separated. Globs are
  repo-root relative; `*`/`?` stay within a path segment, `**` crosses
  segments, a leading `**/` means "at any depth", and a glob matching a
  directory excludes everything beneath it.
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
  non-negotiable part of the standard and is not excludable.
