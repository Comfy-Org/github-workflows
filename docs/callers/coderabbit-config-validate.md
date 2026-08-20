# `coderabbit-config-validate.yml` — fail a PR that breaks `.coderabbit.yaml`

Read [the shared caller contract](README.md) first.

## What it does

Validates your repo's `.coderabbit.yaml` against CodeRabbit's published config
schema, on the PR that changes it.

**Why that is worth a CI job.** CodeRabbit rejects an invalid `.coderabbit.yaml`
**whole** — it discards the entire file and reviews with org-wide UI defaults
instead. Every reviewer instruction, path filter, path instruction and WIP-skip
rule in it goes silently inert. The file still reads fine in review; only the
validator sees the loss.

And the feedback is displaced by one PR: CodeRabbit validates the config on the
**base** branch, not the PR head. So the PR that breaks the file goes green, and
the breakage first surfaces on the *next* PR, attributed to a change that did not
cause it. The human loop here is not merely slow — it points at the wrong diff.

Severity is split, mirroring what CodeRabbit itself does with each problem:

| Problem | Result | Why |
|---|---|---|
| YAML parse error | **fails** | CodeRabbit can't read the file at all. |
| `maxLength` violation | **fails** | File-rejecting. The schema has 14 per-field caps, from 50 to 20,000 chars. |
| Type / enum error | **fails** | File-rejecting. |
| Unknown / additional property | **warns** | CodeRabbit *strips* an unrecognized key rather than rejecting the file — so the config loads, but everything under that key silently does nothing. Set `strict_unknown_keys: true` to fail instead. |
| No `.coderabbit.yaml` at all | **passes** | Reported in the log, so "no config here" never reads the same as "config validated clean". |
| `.coderabbit.yml` present under the default name | **validated, with a warning** | CodeRabbit honours both spellings, so the file that exists is the config in effect. Validating it is the point; the warning tells you to set `config_file` explicitly. |
| Config path outside the repo, not a regular file, or over 512 KiB | **errors (exit 2)** | "I could not check" must never look like a pass. Symlinks are resolved before the containment test. |

The warning class is not cosmetic. The most common instance is a `tools:` block
written at the document root instead of under `reviews:`; the root is closed, so
the whole block is stripped and every setting in it reverts to the schema default
— which is often the *opposite* of what was written (`golangci-lint.enabled`
defaults to `true`, so a root-level `enabled: false` runs the linter it meant to
disable). The annotation names the offending key path and suggests where it
belongs.

The unknown-key check fires wherever a schema object accepts only the property
names it lists. Upstream says that two ways and both are checked: **explicitly**,
with `additionalProperties: false` (five objects, including the document root),
and **by omission** — a `properties` block and nothing said about anything else
(103 objects, including `reviews`, `chat`, `knowledge_base`, `code_generation` and
every individual tool config). So a key in the wrong PLACE is caught, and so is a
typo *inside* an object (`reviews.profil`, `golangci-lint.enabld`) and a key
upstream has since removed.

The by-omission rule is deliberately conservative: an object counts as closed only
if it declares `properties` and none of `additionalProperties`,
`unevaluatedProperties`, `patternProperties`, `anyOf`, `oneOf`, `allOf`, `$ref`,
`$dynamicRef`, `if`, `dependentSchemas`, `propertyNames` or `not` — each of which
is a way the schema legitimately accepts a name it does not list. Nothing is
reported under `reviews.mutually_exclusive_groups` (whose group names are yours to
choose) or inside `knowledge_base.code_guidelines.filePatterns[]` (an `anyOf`
shape), and a document/schema shape disagreement is left to the type error that
already describes it.

**Rolling this out to a repo that already has a config.** A repo carrying
`reviews.tools.github-checks.timeout_ms` — a key upstream removed, still present
in several org repos — will see a NEW warning it did not see before. That is the
point of the check: the key does nothing today. Default mode still exits 0, so
nothing turns red on enrollment. If you plan to set `strict_unknown_keys: true`,
run the checker locally first and remove the stale keys it names — otherwise the
strict switch turns them into a hard failure on whatever PR happens to be open:

```bash
python3 -m pip install --require-hashes --only-binary=:all: \
  -r .github/coderabbit-config/requirements.txt        # from a checkout of Comfy-Org/github-workflows
python3 .github/coderabbit-config/check_coderabbit_config.py --root /path/to/your/repo
```

**The by-omission half has a staleness window — mind it before turning strict on.**
The 103 by-omission objects are read as closed against the schema *vendored at
your pinned `workflows_ref`*, so a property upstream ADDS — a new linter under
`reviews.tools`, a new knob on an existing one — is an unknown key to this
checker until two things land: the weekly `refresh-coderabbit-schema.yml` PR in
`Comfy-Org/github-workflows`, **and** your repo's SHA bump on top of it. In
default warn-only mode that window costs you a warning on a config CodeRabbit is
perfectly happy with. Under `strict_unknown_keys: true` it is a hard CI failure,
on whatever unrelated PR happens to be open, over a key you were right to add.
The five *explicitly* closed objects (the document root among them) have no such
window — jsonschema reports those straight from the keyword.

That is the trade `strict_unknown_keys` makes, not a bug: the same freshness that
lets the checker catch a key upstream REMOVED is what makes it briefly wrong about
a key upstream ADDED. If you hit it, the escape hatch is to flip
`strict_unknown_keys` back to `false` for the one PR and merge the schema refresh
— not to delete the key.

The checker and the schema it validates against live in
[`.github/coderabbit-config/`](../../.github/coderabbit-config) and are loaded
from **this** repo at your pinned `workflows_ref` — never from your checkout — so
a PR cannot rewrite the check that judges it, nor swap the schema it is graded
against.

## Prerequisites

None. No secrets.

## Caller

`.github/workflows/coderabbit-config-validate.yml`:

```yaml
name: CodeRabbit config

on:
  pull_request:
  push:
    branches: [main]        # or [master] — your default branch

jobs:
  coderabbit-config:
    permissions:
      contents: read
    uses: Comfy-Org/github-workflows/.github/workflows/coderabbit-config-validate.yml@<full-commit-sha>
    with:
      workflows_ref: <same-full-commit-sha>
```

Then ask a maintainer to add your repo to the `CODERABBIT_CONFIG_CALLERS` roster
secret. Skipping that second step is the most repeated enrollment mistake: the pin
never moves, and the caller quietly drifts behind the reusable.

## Required permissions

```yaml
contents: read
```

## Inputs

| Input | Default | Notes |
|---|---|---|
| `config_file` | `.coderabbit.yaml` | Point this at `.coderabbit.yml` if that's your spelling — if you don't, the checker finds it anyway and warns rather than reporting "absent". Must resolve inside the repo root (symlinks resolved). |
| `strict_unknown_keys` | `false` | Fail on an unknown/additional property instead of warning. Opt in once your config is clean, to keep it that way. |
| `workflows_ref` | *(required)* | **Set to your `uses:` SHA** — the checker and the vendored schema load from this ref. |

## Gotchas

**Do not add a `paths:` filter.** It is tempting — the check only concerns one
file — but a path-filtered check reports **skipped**, not **success**, on every
unrelated PR, which makes it useless as a required status check. The run is a
YAML parse and a schema walk over a few kilobytes; it is cheap enough to run
always.

**Adopt on `pull_request` before making it required.** Three org repos were
invalid the day this landed. You want that visible on a PR first.

**`strict_unknown_keys: true` is a one-way door for a repo that isn't clean yet.**
Fix the unknown keys first, then turn it on.

**The schema is vendored, not fetched.** Validation never touches the network, so
an upstream schema change cannot turn your CI red without a reviewed PR here
first. `refresh-coderabbit-schema.yml` opens that PR weekly when upstream drifts;
merging it rolls the caller fleet forward automatically.
