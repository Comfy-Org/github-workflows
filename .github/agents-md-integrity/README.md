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
  `REQUIRE_CODEOWNERS`, `AGENTS_FILE`); see the workflow header for the mapping.
- **`tests/`** — `unittest` suite, run by
  [`test-agents-md-integrity.yml`](../workflows/test-agents-md-integrity.yml).

Run locally against any repo:

```bash
python3 .github/agents-md-integrity/check_agents_md.py --root /path/to/repo
```
