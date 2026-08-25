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

Entries are shell globs, matched case-insensitively; a trailing `.git` and
trailing sentence punctuation are stripped from the literal before comparison.
For a **real** repo, "why it is safe" means you confirmed it is public. If it is
private, the fix is to remove the reference, not to add the line.

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

Run `bash check-org-repo-literals.sh --help` for the authoritative list. In
short: only **org-prefixed** literals are caught (a bare repo name cannot be
linted without a denylist that is itself the leak — that half stays with review
and AGENTS.md), and the name class is ASCII, so a name whose tail is non-ASCII
is read only as far as its ASCII prefix. The scan runs under `LC_ALL=C` so that
second limit is the same on every machine rather than a function of the runner's
locale.

## Running it

```bash
bash .github/lint/check-org-repo-literals.sh          # scan the tracked tree
bash .github/lint/check-org-repo-literals.sh --root DIR
shellcheck -x .github/lint/check-org-repo-literals.sh
```

Exit `0` clean, `1` findings, `2` usage/setup error.
