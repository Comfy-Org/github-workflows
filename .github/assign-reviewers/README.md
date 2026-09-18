# Owner assignment tests

The runtime remains self-contained in `../workflows/assign-reviewers.yml`, so
live-only callers need only a SHA bump. Caching is opt-in through a scheduled
job calling the same reusable with `generate_history: true`, plus the
`history_workflow` filename on the assignment job. No caller code or mutable scripts execute.

Run `node --test .github/assign-reviewers/tests/*.test.cjs` from the repo root.
Tests extract and execute the actual inline JavaScript with mocked GitHub APIs.
They also generate and read real ZIP manifests using Python stdlib, exercising
the same bounded archive parser used in Actions, and verify that cache hits
avoid historical API calls while retaining live routing checks.
CI runs the same suite whenever the workflow or tests change.

## Extraction sentinels

The suite recovers the shipped script from `assign-reviewers.yml` by splitting
on `// PARITY-HARNESS:<name>:{begin,end}` comment lines emitted inside the
script itself, and by splitting the recovered script again on the two nested
regions `glob-matcher` and `config-parser`. It used to split on a 10-space
`script: |` plus the *name of an unrelated downstream step*, which captured the
wrong span — silently, still green — the moment a second `script: |` appeared,
a step was renamed, or the indentation changed. Every sentinel must occur
exactly once, in order; missing, duplicated, inverted or out-dented markers all
throw naming the marker. Moving or renaming one is never a cosmetic edit.

## Shared parser corpus

`parser-corpus.json` is the language-neutral fixture behind the parser tests:
config documents mapped to their expected parsed shape, and glob/path pairs
mapped to match verdicts. `refresh-reviewers` **writes** the `reviewers.yml`
this workflow **reads**, and each side ships its own hand-rolled parser and
glob translator, so the same file is also driven through the Python port in
[`../refresh-reviewers/generate.py`](../refresh-reviewers/generate.py) by
`../refresh-reviewers/tests/test_generate.py`. Both CI path filters watch the
corpus, so an edit to it runs both suites.

Add parser cases **to the corpus**, not as an inline literal in either suite —
a case only one implementation ever sees proves nothing about the other, which
is how the two live divergences reached `main`.

The [caller guide](../../docs/callers/assign-reviewers.md) documents the ranking,
evidence limits, failure handling, and configuration. Keep behavior there rather
than maintaining another algorithm description here.
