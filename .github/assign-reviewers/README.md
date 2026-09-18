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

Four dialect rules the corpus now pins on both sides, because each was a place
the ports read the same bytes differently: a **duplicate top-level
`default_pool:` is last-wins** (the second replaces the first — the block arm
used to *append* on the JS side — and each port warns rather than rejecting;
a repeated `rules:` is *not* covered, it still appends on both sides); a **`#`
opens a comment only at column 0 or after a space or a tab**, the two characters
spelled out rather than delegated to `/\s/` and `isspace()`, which disagree about
U+0085, U+001C and U+FEFF; a **single leading U+FEFF is stripped from the
document**, which `trim()` did for free and `strip()` did not; and **a token is
trimmed of a space or a tab and nothing else** — YAML s-white, spelled out as
`_trim` in the Python port and `trimSWhite` in the JS one, because `str.strip()`
also drops U+0085 and U+001C-U+001F, `String.prototype.trim()` also drops U+FEFF,
and *both* drop U+00A0. Every other invisible character now survives into the
login verbatim on both sides, so a padded login fails to route identically in the
runtime and in the drift generator instead of routing in one and not the other.
The warning text is the one part not corpus-pinned — the channels differ
(`core.warning` vs a `::warning::` line) — so each suite asserts its own.

Three consequences of that narrowing, each pinned rather than left to be rediscovered:
the runtime **warns** when a *configured* login fails the login-shape gate, because the
padding that now survives is invisible and the token would otherwise just never be
assigned (keyed on the shape test only — being excluded as the PR author is normal and
must not warn); a line whose only content is non-s-white whitespace is **no longer
blank**, and since indentation counts spaces it reads as column 0 and terminates the
block above it, which inside `rules:` drops every later rule — so a column-0 line that
**actually breaks something** is now reported by a warning on both ports, naming its line
number and rendering its content codepoint-escaped (`line 3 is not a recognised top-level
key (\u00a0)`), since nothing about that truncation is visible in an editor and a
misspelled key truncates identically. Two arms, and the *silence* between them is as
pinned as the text: a line that ENDED an open `default_pool:`/`rules:` block, and a
near miss that names a supported key without opening it (`\u0085rules:`, whose stray
character is not indentation; `rules:v2:`, a key YAML reads as `rules:v2`; and
`default_pool:[alice]`, which YAML reads as a plain scalar — the last two used to be
silently HONOURED as the supported key, which is why both ports now require s-white or
a line end after a key's colon). A `---`/`...` document marker, a `version:` key before
the first block and a stray key between two complete blocks break nothing and stay
silent, as does every INDENTED fallthrough line — one annotation per thing actually
broken, so a tab-indented or zero-indented config cannot flood GitHub's ~10-annotation
budget and bury the one that matters. Finally, the `setKey` regex
spells its class out as `[^\n]*` rather than `.`, because Python's `.` excludes only LF
while JS's also excludes CR, U+2028 and U+2029 — with `.` the Python port matched a rule
line ending in a bare CR and the JS port did not, so the runtime dropped the key while
the generator modelled those reviewers as routing.

The generator reads the committed config as **bytes**, never with `text=True`: universal-
newline translation rewrites CRLF and a bare CR to LF before the parser sees them, which
would hand the parser different bytes than the runtime reads from the base64 blob — the
same divergence class, one layer above the parser and invisible to a test that feeds the
parser text directly.

The [caller guide](../../docs/callers/assign-reviewers.md) documents the ranking,
evidence limits, failure handling, and configuration. Keep behavior there rather
than maintaining another algorithm description here.
