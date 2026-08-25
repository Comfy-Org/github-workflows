# lint — org repo literal allowlist

`check-org-repo-literals.sh` + `org-repo-allowlist.txt` are the lint behind
[`test-org-repo-literals.yml`](../workflows/test-org-repo-literals.yml). They
make AGENTS.md's first convention — *never leak private caller names* — a CI
gate instead of something a reviewer has to remember, the same move
[`test-workflow-pins.yml`](../workflows/test-workflow-pins.yml) made for the
`workflows_ref` default rule.

## The rule

Every org-prefixed repo literal in this repo's **tracked** tree must have its
repo-name part on `org-repo-allowlist.txt`. Anything else fails the run, with
`file:line:match` for each hit.

The org segment has to **start a token**, so `Not<org>/whatever` — a different
owner whose name happens to end in ours — is not a reference to this org and is
not reported. That is `public-repo-hygiene`'s left-boundary rule verbatim, so
the two checkers agree on what counts as a reference.

**Allowlist, never denylist.** A denylist grep would have to commit the private
names into this public repo in order to match them — the lint would *be* the
leak. Default-deny inverts that: the committed list holds publishable names
only, and publishing a new one is an allowlist edit that shows up in review.

## Adding a name

Add one line, lowest-friction spelling first, with a trailing `#` comment saying
why it is safe:

```
comfy-cli                   # public (also on the org-wide PUBLIC_COMFY_ORG_REPOS list)
```

Entries are literal names, compared case-insensitively; a trailing `.git` suffix
and trailing periods are stripped from the literal before comparison. A trailing
`-` or `_` is **not** stripped — both are legal at the end of a real slug, so
stripping them would let `<allowlisted>-` clear on `<allowlisted>`. For a
**real** repo, "why it is safe" means you confirmed it is public. If it is
private, the fix is to remove the reference, not to add the line.

**Team handles are a separate namespace.** An entry written with a leading `@`
clears only an `@`-prefixed literal — a CODEOWNERS team handle, or an npm /
GitHub Packages scope. A plain entry clears either spelling, because
`@<org>/<name>` is also how a package scope for a repo of that name is written.
Scoping team slugs this way is what stops a CODEOWNERS fixture's team name from
also clearing a literal reference to a *private repo* of the same name; the
org-wide checker splits the two lists for the same reason.

**No globs.** An entry containing `*`, `?`, `[` or `]` is a configuration error
(exit `2`), not an allowlist line: `*`, `**`, `?*`, `[a-z]*`, `[!qz]*` and
`secret-*` all read as ordinary edits while clearing most or all of the
namespace. Rejecting the metacharacters is what makes that deterministic — an
earlier revision tested entries against two fixed probe names instead, and two
probes cannot decide breadth in general (`[!qz]*` and `[a-p]*` match neither yet
clear nearly everything). Enumerate a test-fixture family rather than globbing
it: a `secret-*` line would pre-approve an unbounded set of names on a list
whose entire value is that each name was reviewed once.

## Why not `public-repo-hygiene`?

[`.github/public-repo-hygiene/`](../public-repo-hygiene/) is the rigorous,
org-wide implementation of this same default-deny idea, and it is what every
*other* repo should adopt. This repo cannot adopt it as a caller: it is that
checker's own **home**, so its tests and docs deliberately commit fake-private
fixture names, quote internal collaboration-tool hosts, and reference ticket ids
by convention throughout — all three of that checker's categories, all
intentional. This lint is the one category this repo *can* enforce on itself
today. It is deliberately small; it is not a second copy of that checker, and
new detection belongs there rather than here.

## Known limitations

Run `bash check-org-repo-literals.sh --help` for the authoritative list — the
script's numbered KNOWN LIMITATIONS block. Every entry there has a bullet below,
in the same order, except limitation 5 (*this lint is one category of
`public-repo-hygiene`*), which is the section above. In short:

- Only **org-prefixed** literals are caught. A bare repo name cannot be linted
  without a denylist that is itself the leak — that half stays with review and
  AGENTS.md.
- The name class is **ASCII**, so a name whose tail is non-ASCII (a U+2010
  homoglyph dash, a U+017F long s, CJK text) is read only as far as its ASCII
  prefix. The class cannot simply be widened to high bytes: this tree
  legitimately writes an ellipsis, an em dash and CJK text flush against a real
  name in prose, so widening turns those into findings. The scan runs under
  `LC_ALL=C` so this limit is the same on every machine rather than a function
  of the runner's locale. Closing it properly needs the offset-aware scan
  [`public-repo-hygiene`](../public-repo-hygiene/) already implements.
- **Binary blobs are never read.** Both scan paths pass `-I`, so a UTF-16 file,
  a file carrying a stray NUL, or one `.gitattributes` line (`*.md binary`)
  takes whole file types out of the scan with no signal in the run output. Text
  is the only surface this lint claims.
- **Only a literal written whole is caught.** An org literal assembled at run
  time — `printf '%s/%s' '<org>' '<name>'`, `"${ORG}/<name>"`, a name split
  across a concatenation — never matches, so it needs no allowlist edit while
  the run still reports its scope clean. That is not hypothetical here: this
  lint's own smoke tests use exactly that spelling on purpose, which makes it
  the house style for org literals in this repo's workflow files. It stays
  fully human-readable in the source, so review is what catches it.
- **A team `@` is judged by position, not by grammar.** An `@` glued to the tail
  of an identifier (`user@<org>/<name>`, email-shaped) still reads as a scope, so
  such a literal can draw on the `@`-scoped team entries. Reaching a private name
  through that needs the name spelled exactly like an allowlisted team slug.
  `public-repo-hygiene` reads the `@` from the same position and shares the
  residual.
- **`_` is identifier-continuation in the left boundary**, so a literal written
  directly after an underscore is not read as a reference at all — the
  markdown-italic spelling `_<org>/<name>_` matches nothing. The same accepted
  trade `public-repo-hygiene` makes, kept identical on purpose so a name cannot
  pass one checker and fail the other.
- **Contents only.** The pattern is applied to what a tracked file *contains*,
  never to the tracked **path** and never to a **symlink's target string** — so
  a name published as a directory component (`docs/<org>/<name>/placeholder`) or
  as a link target is not a finding here. `public-repo-hygiene` does scan a
  symlink's target string; neither checker scans the path itself.

## Running it

```bash
bash .github/lint/check-org-repo-literals.sh          # scan the tracked tree
bash .github/lint/check-org-repo-literals.sh --root DIR
shellcheck -x .github/lint/check-org-repo-literals.sh
```

Exit `0` clean, `1` findings, `2` usage/setup error.
