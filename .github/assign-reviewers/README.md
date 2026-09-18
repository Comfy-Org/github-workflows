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
exactly once, in order; missing, duplicated, inverted or out-dented markers —
including an out-dented `:end` — all throw naming the marker. Moving or
renaming one is never a cosmetic edit.

Order alone is not enough for the outer `script` pair, which must also
**bracket the whole `script: |` scalar**: `:begin` directly after the block
header and `:end` as its last line. JavaScript placed above `:begin` or below
`:end` would still ship while being excluded from the bytes the suite runs —
the same silently-wrong span the sentinels replaced, pointing the other way —
so the harness asserts both boundaries at import time. The nested
`glob-matcher` / `config-parser` pairs are sub-spans by design and are exempt
from that check; they instead carry the script's own offset into the workflow
file, so a failure inside them reports a real `assign-reviewers.yml` line
rather than a line number into the extracted string.

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

Three dialect rules the corpus now pins on both sides, because each was a place
the ports read the same bytes differently: a **duplicate top-level key is
last-wins** (the second `default_pool:` replaces the first — the block arm used
to *append* on the JS side — and each port warns rather than rejecting); a **`#`
opens a comment only at column 0 or after a space or a tab**, the two characters
spelled out rather than delegated to `/\s/` and `isspace()`, which disagree about
U+0085, U+001C and U+FEFF; and a **single leading U+FEFF is stripped from the
document**, which `trim()` did for free and `strip()` did not. The warning text
is the one part not corpus-pinned — the channels differ (`core.warning` vs a
`::warning::` line) — so each suite asserts its own.

The [caller guide](../../docs/callers/assign-reviewers.md) documents the ranking,
evidence limits, failure handling, and configuration. Keep behavior there rather
than maintaining another algorithm description here.
