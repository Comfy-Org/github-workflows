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
`unapproved: file:line: match` for each hit plus a matching `::error`
annotation. The `unapproved: ` prefix is constant and load-bearing: without it
the line starts with the tracked path, and a tracked file named
`::stop-commands::x.md` would emit a *workflow command* at column zero that
suppresses every annotation after it. The annotation's `file=` value is
percent-escaped for the same reason, and `line=` is *validated* rather than
escaped: a line number is always digits, so a non-numeric field means the
`file:line:match` record did not parse and the run refuses it (exit `2`). The
record's **shape** is checked too — after the boundary drop the match must still
begin with the org prefix — because a numeric line field does not prove the split
landed where it appears to. The way both fire is a tracked path containing a
newline on the `grep -r` **fallback** path, which prints paths raw: one hit
arrives as two records, and the leading fragment would otherwise be reported as a
literal cut out of a filename against a path that does not exist. The **git**
path has no split — `git grep` C-quotes newline, tab, `\` and `"` in a path
whatever `core.quotePath` says (that governs only bytes ≥ 0x80) — so there such a
file is one record, a real finding with a C-quoted location.

The org segment has to **start a token**, so `Not<org>/whatever` — a different
owner whose name happens to end in ours — is not a reference to this org and is
not reported. That is `public-repo-hygiene`'s left-boundary rule verbatim, so
the two checkers agree on what counts as a reference. A token boundary is not an
*owner-name* boundary, though: `-` is legal in a GitHub owner name and is not an
identifier character, so the hyphenated sibling `Not-<org>/whatever` **is** read
as one of ours. That false positive is limitation 8 below, kept rather than fixed
so the boundary stays byte-identical to that checker's.

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

Three shapes of entry are a configuration error (exit `2`) rather than an
allowlist line, because each reads as an ordinary edit while doing something
other than what its author meant.

**No globs.** An entry containing `*`, `?`, `[` or `]` is rejected. Note what
this guard is today: entries are compared with *exact equality*, so a `*` entry
already clears nothing but a literal `*` — nothing reaches the comparison as a
pattern any more. The rejection is **defence-in-depth** against that comparison
going back to a `case` glob, kept so a later reader "simplifying" `=` into one
does not silently re-arm every metacharacter on the list. It is also why an
earlier revision's approach — testing entries against two fixed probe names — was
dropped: two probes cannot decide breadth in general (`[!qz]*` and `[a-p]*` match
neither yet clear nearly everything). Enumerate a test-fixture family rather than
globbing it: a `secret-*` line would pre-approve an unbounded set of names on a
list whose entire value is that each name was reviewed once.

**No `/`.** Entries are bare repo *names* — the comparison runs against the name
alone, so an entry written `<org>/foo` can never match anything. That is exactly
the spelling the tool invites, since a finding quotes the whole literal and the
summary says to add it to the allowlist, so accepting it would yield an inert
line and a rerun still insisting the name is missing from a file that visibly
contains it. Rejected with the spelling that does work.

**No name ending in the org name.** That is the shape of a *merged* literal, not
a repo name: `grep -o` takes non-overlapping matches, so `<org>/public.<org>/private`
scans as one match whose name reads `public.<org>` and the second name is never
examined. The suffix test is complete rather than a sample of separators — the
merged token always ends at the `/` that starts the second name, so `foo.<org>`,
`foo-<org>`, `foo_<org>` (the name class holds `_`) and the bare `<org>` that
`<org>/<org>/private` produces are all one rule. It fails closed
(no merged token is on the list, so the line is always a finding), but the
obvious way to silence so confusing a finding is to allowlist the token it
quotes — and that would clear the private reference with the run green. Making
the merged token unallowlistable removes the foot-gun.

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
- **A left boundary is not an owner-name boundary.** Only the *unhyphenated*
  `Not<org>/x` is excluded; `Not-<org>/x`, an equally real different owner, still
  satisfies the boundary and is reported as one of ours. This is a false
  positive, not a miss, and it is kept rather than fixed so the boundary class
  stays byte-identical to `public-repo-hygiene`'s `REPO_REF_RE` — a name must not
  pass one checker and fail the other. Both spellings are pinned by the smoke
  tests; widening the class is a change to **both** checkers.
- **Contents only.** The pattern is applied to what a tracked file *contains*,
  never to the tracked **path** and never to a **symlink's target string** — so
  a name published as a directory component (`docs/<org>/<name>/placeholder`) or
  as a link target is not a finding here. `public-repo-hygiene` does scan a
  symlink's target string; neither checker scans the path itself.
- **Two adjacent literals scan as one.** `<org>/public.<org>/private` is a single
  `grep -o` match whose name reads `public.<org>`; the second name is never
  compared and the scan resumes past it. Any separator in the name class does it
  — `.`, `-`, `_`, or none at all for `<org>/<org>/private`. It fails closed — that token is on no
  list, so the line is always a finding — and the remedy that *would* be
  dangerous (allowlisting the quoted token) is rejected by the loader, above.
- **The per-hit loop is linear in the hit count**, and the 200-finding print cap
  does not bound it: past the cap a hit stops printing but is still normalized
  and compared, because the reported count and the exit status have to stay the
  true ones. Measured at ~5.4 ms per hit past the cap, so the caller's
  `timeout-minutes: 10` is reached at roughly **110,000** hits in one scan.
  Documented rather than capped — this tree's largest tracked file is under 600
  lines, and capping the scan would trade an unreachable timeout for a truncated
  count, the one number a red run's summary turns on.

## Running it

```bash
bash .github/lint/check-org-repo-literals.sh          # scan the tracked tree
bash .github/lint/check-org-repo-literals.sh --root DIR
shellcheck -x .github/lint/check-org-repo-literals.sh
```

Exit `0` clean, `1` findings, `2` usage/setup error.

Two bounds keep one hostile tracked line from turning the lint into an
inconclusive run rather than a verdict. A name longer than GitHub's
**100-character** repo-name limit *after* normalization is reported without
being compared (it cannot be a real repo, so it can never be allowlisted — the
bound fails *closed*). The strips run first on purpose: peeling is the only thing
that can bring an over-long name back under the limit, and measuring first turned
`<org>/<98-char-name>.git` into a finding whose stated remedy — the allowlist — is
skipped for exactly that class. Their input is bounded to 8 bytes of headroom
(`.git`, a sentence period, an ellipsis) because the peel is quadratic in the run
it removes: unbounded, a single line of `<org>/<name>` followed by 400 KB of
periods ran for over two minutes.
And past **200** findings the per-hit lines stop printing — the count and the
exit status stay the true ones — so a badly-seeded allowlist floods neither the
public run log nor the annotation list.

`--root` is resolved robustly rather than trustingly: `CDPATH`, `GIT_DIR`,
`GIT_WORK_TREE` and `GIT_INDEX_FILE` are unset (each can silently move the scan
off the directory the run then reports as its scope), and so are
`GIT_CONFIG_COUNT`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM` and
`GIT_CONFIG_PARAMETERS` — those inject arbitrary config, and
`core.attributesFile` pointing at one `*.md binary` line marks a subset of the
tree binary, which `git grep -I` then silently skips. That one fails *open*: the
scannability probe still clears, the scan reads less than the tree, and the OK
line still claims the whole scope. (`GIT_CONFIG_KEY_n`/`_VALUE_n` are inert once
the count is gone. `GIT_CONFIG_PARAMETERS` is the non-obvious one: it is how `-c`
propagates to child git processes and is read unconditionally, so it delivers the
same fail-open with the count already unset.)

What that does *not* do: unsetting `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM`
restores git's **default** config search rather than disabling it, so a
runner-level `core.attributesFile` still applies. Pointing both at `/dev/null`
plus `GIT_CONFIG_NOSYSTEM=1` would close that and is deliberately not done — it
would also drop a runner's legitimate `safe.directory`, which makes `rev-parse`
fail and silently takes the `grep -r` fallback. The environment is hardened; the
runner's own config is trusted, the same way the tamper boundary trusts the
checkout.

The git/`grep -r` branch is chosen from `rev-parse --is-inside-work-tree`'s
**output** rather than its exit status (it prints `false` and exits `0` for a
bare repo and for a path under `.git`), and `git grep`'s output-shaping config
(`grep.column`, `grep.fullName`, `color.grep`, `core.quotePath`) is pinned on the
command line. `grep.fullName` is pinned **`true`**, not `false`: with `false` git
prints paths relative to the cwd, which `git -C "$root"` has set to `$root`, so
`--root docs` would report `docs/x.md` as `x.md` and emit an annotation GitHub
resolves against the repo root and drops.
