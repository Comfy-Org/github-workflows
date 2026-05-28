# Comfy-Org reusable GitHub Actions workflows

Shared, versioned [reusable workflows](https://docs.github.com/en/actions/using-workflows/reusing-workflows) for use across Comfy-Org repositories.

This repo is **public** so any repo — public or private, inside or outside the org — can call these workflows with no extra GitHub Actions settings.

## Workflows

| Workflow | Purpose |
|---|---|
| [`detect-unreviewed-merge.yml`](.github/workflows/detect-unreviewed-merge.yml) | SOC 2 compliance — detects PRs merged without prior approval and opens a tracking issue in [`Comfy-Org/unreviewed-merges`](https://github.com/Comfy-Org/unreviewed-merges). |

## Usage

Reference a workflow by full path and pin to a **full commit SHA** (with the version as a trailing comment). Also set explicit minimum permissions on the calling job so the default permissive token scope isn't granted:

```yaml
permissions:
  contents: read
  pull-requests: read

jobs:
  my-job:
    uses: Comfy-Org/github-workflows/.github/workflows/<workflow-name>.yml@<sha>  # v1
    with:
      <input>: <value>
    secrets:
      <SECRET>: ${{ secrets.<SECRET> }}
```

The SHA-pin format satisfies pin-validation tooling (`pinact`, `zizmor`, etc.) and gives auditors immutable supply-chain evidence. Dependabot/Renovate can auto-bump the SHA when the upstream tag moves.

A bare `@v1` tag is technically allowed but **will fail** in repos that run pin-validation in CI (e.g. `cloud`, `ComfyUI_frontend`).

Per-workflow inputs, required secrets, and triggers are documented in each workflow file's header comment.

## Versioning

Workflows in this repo use **semver-style major-version tags** (`v1`, `v2`, …).

- Breaking changes bump the major (`v1` → `v2`); callers opt in.
- Backwards-compatible changes update the existing major tag in place (`git tag -f v1 <sha> && git push -f origin v1`) — callers pinned to the tag pick up the update on the next run; callers pinned to a SHA opt in by bumping the SHA.

## Adding a new reusable workflow

1. Add the workflow file under `.github/workflows/<descriptive-name>.yml` with `on: workflow_call:` and a header comment documenting inputs/secrets.
2. Update the table in this README.
3. Move the floating `v1` tag (or cut a new major) once the change is reviewed and merged.
