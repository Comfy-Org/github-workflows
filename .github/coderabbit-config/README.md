# coderabbit-config

The checker behind [`coderabbit-config-validate.yml`](../workflows/coderabbit-config-validate.yml),
plus the vendored schema it validates against. Consumer repos carry only a thin
caller; everything here is loaded at run time from the SHA a caller pins, so a PR
can never rewrite the check that judges it — nor swap the schema it is graded
against.

## What it catches, and why CI has to

CodeRabbit rejects an invalid `.coderabbit.yaml` **whole**. Not the offending
field — the entire file. The review then runs on org-wide UI defaults, so every
reviewer instruction, path filter, path instruction and WIP-skip rule in the file
goes silently inert. The file still reads fine; only the validator sees the loss.

And the feedback arrives one PR late, on the wrong diff: CodeRabbit validates the
config on the **base** branch, not the PR head. The PR that breaks the file goes
green; the breakage surfaces on the *next* PR, attributed to a change that did not
cause it. That displacement is what makes this a machine check rather than a
convention — the same field on the same repo was fixed twice in six days by two
independent tickets, which is the signature of a failure nothing detects.

## Severity, and why it is split

The split mirrors what CodeRabbit itself does with each class of problem, not what
a schema library calls "invalid":

| Class | Severity | Because |
|---|---|---|
| YAML parse error | error | CodeRabbit cannot read the file at all. |
| `maxLength` violation | error | File-rejecting. The schema carries **14** per-field caps, 50 → 20,000 chars. |
| type / enum error | error | File-rejecting. |
| unknown / additional property | **warning** | CodeRabbit *strips* an unrecognized key rather than rejecting the file. Reported wherever the schema object accepts only the names it lists — see below. |
| config path that is not a regular file, or > 512 KiB | exit 2 | "I could not check" must never look like a pass. A path resolving outside the repo root (symlinks resolved) is refused the same way. |

`strict_unknown_keys: true` escalates the last row to an error for a repo that has
cleaned up and wants to stay clean.

**How far the unknown-key row reaches.** It fires wherever a schema object accepts
only the property names it lists — which upstream expresses two different ways,
and both are checked:

* **explicitly**, with `additionalProperties: false`. Five objects: the document
  root, `knowledge_base.mcp`, `knowledge_base.linked_repositories[]`, and the
  `htmlhint` / `stylelint` tool configs. jsonschema reports these.
* **by omission** — a `properties` block and no statement at all about anything
  else. **103** objects, including `reviews`, `chat`, `knowledge_base`,
  `code_generation` and every individual tool config. jsonschema has no keyword to
  fire on here, so a schema-walk in `_walk_unknown_keys` reports them. This is
  what catches a typo *inside* an object — `reviews.profil`,
  `golangci-lint.enabld` — and a key upstream has since REMOVED, such as
  `reviews.tools.github-checks.timeout_ms`.

The rule the walk applies is conservative: a node is closed by omission only when
it declares a `properties` block and NONE of `additionalProperties`,
`unevaluatedProperties`, `patternProperties`, `anyOf`, `oneOf`, `allOf`, `$ref`,
`$dynamicRef`, `if`, `dependentSchemas`, `propertyNames` or `not`. Each of those
is a way the object can legitimately accept a name its `properties` does not list,
so reading any of them as "closed" would invent a finding — `additionalProperties`
given a SCHEMA rather than `false` (`reviews.mutually_exclusive_groups`, where the
group names are the user's to choose) is the live example. The walk descends only
through matched properties and through `items`, never into a combinator branch or
under a key it just reported, and where the document and the schema disagree about
shape it stays silent, because that is a type error and jsonschema owns it. The
two halves are disjoint by construction: one needs the keyword present, the other
needs it absent.

A stripped key is not a cosmetic problem. The recurring instance is a `tools:`
block written at the document root instead of under `reviews:` — the schema root
is `additionalProperties: false`, so the whole block is dropped and every setting
in it reverts to the schema default, which is often the *opposite* of what was
written. `reviews.tools.golangci-lint.enabled` defaults to `true`, so a root-level
`enabled: false` runs the linter it was written to disable. Three org repos
carried exactly that shape when this landed.

## Why file size is the wrong invariant

The obvious cheap check — "fail if `.coderabbit.yaml` gets too big" — would gate
nothing. There is no whole-document size cap in the schema, only the 14 per-field
ones, and size is uncorrelated with validity: the invalid config that started this
was 2,198 bytes while a valid one next to it was 4,826. Across the org sweep there
were **zero** `maxLength` violations and three misplaced-key repos, so a size check
would have found none of the real problems and none of the historical one.

## Why the schema is vendored

`schema.v2.json` is a committed copy of
`https://coderabbit.ai/integrations/schema.v2.json`. Validation never fetches it.

- A live fetch makes every consumer's CI depend on a third-party endpoint.
- An upstream tightening — a `maxLength` lowered, a property removed — would turn
  CI red across every enrolled repo with no change on our side and no PR to point
  at.

[`refresh-coderabbit-schema.yml`](../workflows/refresh-coderabbit-schema.yml)
covers the cost of vendoring: weekly it fetches upstream, compares semantically
(so a whitespace-only reserialization is not "drift"), and opens a reviewable PR
with a summary that leads on tightened caps — the one drift class that can
retroactively invalidate a config nobody touched. Merging it trips
`bump-coderabbit-config-callers.yml`, which rolls the pinned caller fleet forward.

**Any fetch of that URL must use `curl -fsSL`.** It 301-redirects, and a bare
`curl` writes a ~167-byte HTML redirect stub. Both `check_coderabbit_config.py`
and `schema_drift.py` refuse a file that is not a JSON Schema object rather than
treating it as "no drift" or "everything valid" — a check that cannot fail is
worse than no check.

## The dependency

This repo is otherwise stdlib-only, deliberately. This directory is the one
exception and neither half has a stdlib answer: there is no YAML parser in the
standard library, and the point of the check is to reproduce the verdict
CodeRabbit's own Draft 2020-12 validator reaches — a hand-written subset validator
would agree with the real one until it didn't, and the day it diverged the check
would either gate nothing or fail a valid config.

So the dependency is pinned hard instead: exact versions with sha256 hashes in
[`requirements.txt`](requirements.txt), installed with `--require-hashes`, and
watched by Dependabot (`pip` ecosystem in `.github/dependabot.yml`) so the pins do
not rot.

## Files

| File | What it is |
|---|---|
| `check_coderabbit_config.py` | The validator. `--root`, `--config`, `--schema`, `--strict-unknown-keys`. Exit 0 pass / 1 invalid / 2 could-not-run. |
| `schema.v2.json` | The vendored schema. Never edit by hand — let the refresh job propose the bump. |
| `schema_drift.py` | Semantic comparison + Markdown drift summary for the refresh PR. Exit 0 no-drift / 1 drifted / 2 unusable input. |
| `requirements.txt` | The hash-pinned PyYAML + jsonschema set. |
| `tests/` | Unit tests, run by `test-coderabbit-config-validate.yml`. |

## Run it locally

```bash
python3 -m venv /tmp/crvenv
/tmp/crvenv/bin/pip install --require-hashes --only-binary=:all: \
  -r .github/coderabbit-config/requirements.txt      # linux/cp312 wheels
/tmp/crvenv/bin/python .github/coderabbit-config/check_coderabbit_config.py --root .
/tmp/crvenv/bin/python -m unittest discover -s .github/coderabbit-config/tests -p 'test_*.py' -v
```

The hashes pin the cp312 / manylinux x86_64 wheels CI installs, so on another
platform install the same two versions without `--require-hashes` instead.
