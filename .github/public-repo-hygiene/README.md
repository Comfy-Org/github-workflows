# public-repo-hygiene

The checker behind [`public-repo-hygiene.yml`](../workflows/public-repo-hygiene.yml): a
lightweight regression guard that fails CI when a **public** repo's tracked files carry
internal-only references. Setup guide for consumers:
[`docs/callers/public-repo-hygiene.md`](../../docs/callers/public-repo-hygiene.md).

It is **not** a secrets scanner. It looks for three categories of internal-only *reference*, not
for credentials, and it uses small explicit allow/deny lists rather than one clever regex — so a
false positive is a one-line list edit instead of a mystery.

## What it flags

| # | Category | Shape |
|---|---|---|
| 1 | Ticket-style identifiers | `\b[A-Z]{2,6}-\d{2,6}\b` — a generic SHAPE, never a list of real internal team keys, so the check itself discloses no internal naming scheme. Common tech acronyms that fit (`UTF-8`, `SHA-256`, `RFC-3339`, …) are allowlisted; a caller extends that list with the `ticket_allowlist:` input. Well-known **public identifier namespaces** clear by PREFIX rather than as exact tokens — `CVE-`, `CWE-`, `PEP-`, `RFC-`, `ISO-`, `UTF-` — because `CVE-2021-44228` presents to this regex as `CVE-2021` (the `\b` holds against the following hyphen), so a SECURITY.md or a dependency changelog reddened a required check, and an exact carve-out would cost one entry per year prefix and break again each January. None of those is a plausible internal team key. |
| 2 | Internal collaboration-tool links | Notion (`notion.so`/`notion.site`), Slack (`slack.com/archives`, `slack.com/client`, `app.slack.com`), Google Docs/Drive, `app.datadoghq.com`, `posthog.com/project/`, `linear.app`, and `incident-NNN`. Case-insensitive. Public marketing pages on the same hosts (`posthog.com/docs`) are not matched. Each host is anchored on **DNS-label boundaries**, not on `\b`: a preceding dot is a real subdomain edge (`comfy.slack.com/archives/C123`, `www.notion.so/team/page` are those services and are matched — spelled with their paths, because the left anchor is only half of what each pattern requires), while a preceding letter, digit or hyphen makes it a different registrable domain (`fooslack.com`, `evil-posthog.com`, `my-linear.app` are not matched). An explicit **port** is tolerated, so neither `notion.so:443/` nor the empty-port `notion.so:/` (both valid URLs for that host) can walk past a pattern that requires `/` after the host. The **host** patterns compile with `re.ASCII` (`incident-NNN` is not a host and keeps plain `re.IGNORECASE`), so Unicode case-folding no longer reads a different domain — `lınear.app`, `notİon.so` — as the real one. The trade is that a UTS-46-*mapped* spelling of a covered host (`ſlack.com`, `slacK.com` with U+212A) becomes a miss instead; that is grouped with the other obfuscated spellings under "Known limitations". On the right, the required `/` after host+port is what rejects a suffix (`notion.so.evil.com/`); `app.slack.com` is the one host-only pattern and carries a right anchor of its own, `(?!\.?[A-Za-z0-9-]|@|[.:][A-Za-z0-9._~%:-]{0,64}@)`, which rejects a following label (`app.slack.com.evil.com`) and the userinfo family (`app.slack.com@evil.com`, `:@`, `:secret@`, `.@`, `:443.evil.com@evil.com` — where the real host is `evil.com`), while still allowing a sentence-final period, a real port, a colon in prose (`app.slack.com:general`, `app.slack.com:2FA`) and end of line. It does **not** reject a non-numeric “port” such as `app.slack.com:443.evil.com`: `port = *DIGIT`, so that URL does not parse at all and the only host any parser reads on the line is `app.slack.com` — an earlier revision suppressed it as a suffix host, which was a miss rather than a false-positive fix. The userinfo run matches the characters a real credential uses, and is length-bounded: an earlier `[^\s/?#@]*` (“anything but a URL delimiter”) crossed commas, quotes and braces, so an unrelated `@` later on the line silenced the host (`app.slack.com:443,ops@example.com`) — and, being unbounded, made a long single line quadratic to scan. Both bounds have a cost, in the over-flag direction and both listed under "Known limitations": the class still lets a **colon-chained** run cross prose to a later `@`, and userinfo past 64 characters is out of the run's reach. Narrowing a class inside a *negative* lookahead can only ever flag more, never less. It deliberately does **not** consume the port: an optional greedy port in front of a negative lookahead backtracks, and reasoning about every port split is what put a hole and then a regression into two earlier revisions of this anchor. |
| 3 | `Comfy-Org/<repo>` references | **Default-deny** against `PUBLIC_COMFY_ORG_REPOS`. Anything else is flagged so a maintainer either scrubs it or adds it once confirmed public. A leading `@` makes the match a CODEOWNERS **team** handle instead, checked against `PUBLIC_COMFY_ORG_TEAMS` — team handles are inherently public on a public repo (GitHub renders CODEOWNERS owners to anyone who can see it), so an allowlisted one is not a leak. That same `@Comfy-Org/<name>` spelling is also how npm and GitHub Packages write a **scope**, so the `@` path accepts either allowlist; the reverse crossing is not allowed, since a bare `Comfy-Org/<name>` is unambiguously a repo path. A trailing `.git` is stripped, because `Foo.git` in a `repository.url` still references the public repo `Foo`, and a trailing `.` is stripped as sentence punctuation on both paths (a GitHub slug can never end in a dot); a reference left naming nothing (`Comfy-Org/.`, `Comfy-Org/.git`) is not a finding. Case-insensitive throughout — GitHub resolves owner names case-insensitively, so `comfy-org/<repo>` is checked exactly like `Comfy-Org/<repo>`, the `.git` suffix is stripped whatever its casing, and allowlist membership is compared casefolded. The repo-name class itself stays ASCII on purpose (the ignore-case flag is scoped to the org segment): a whole-pattern flag would let Unicode case-folding admit `ſ`/`K` into the name and fold them onto allowlisted spellings. Both ends of the match are bounded: the org segment must **start a token** (`NotComfy-Org/x` is a different org, not a reference to this one), and a name the ASCII class could only **partly read** is never cleared — a Unicode letter or a homoglyph dash immediately after the capture is the *rest of the name*, not a boundary, so `Comfy-Org/comfyui‐internal` written with U+2010 would otherwise test `comfyui` against the allowlist and pass while the full private name sat in the tree. Such a reference is reported whole, with its own remedy (rewrite it in ASCII), because "add it to the allowlist" is not the fix for a homoglyph. The npm/Packages crossing is narrowed to a spelling that could actually **be** an npm coordinate — those are required to be lowercase — so `@Comfy-Org/comfyui`, a team named after the repo it owns, does not clear the team allowlist by borrowing the repo one. |

Only **tracked** files are scanned (`git ls-files -z`), so build output, `node_modules` and
anything untracked is out of scope by construction. Binary files are skipped (a NUL byte or an
undecodable UTF-8 sequence), and only **regular** files are opened at all: `open()` follows a
symlink, so scanning one would read whatever it points *at* — a link out of the repo pulls
arbitrary runner content into a public run log, and one to `/dev/zero` or a FIFO turns the read
into an OOM or a hang. A symlink is not skipped outright, though: `os.readlink()` returns the
target **string**, which is the entry git actually tracks and publishes in the tree, so that
string is scanned in place of the file body. A regular file is read up to `MAX_FILE_BYTES` (5 MiB)
and no further, and what is *derived* from those bytes is capped too — `MAX_FINDINGS_PER_FILE`
(200), `MAX_FINDINGS_TOTAL` (2000) and a `MAX_EXCERPT_CHARS` (200) bound on the echoed line, since
a category-2 finding copies the matched line and the scanned repo controls how long that is.
Hitting a cap adds a `::warning::` and never softens the verdict: the run still fails.

Everything the scan declines to look at leaves a trace, because a guard that silently skips
something is worse than no guard — the green run reads as coverage:

- every run prints `SCANNED: <n> file(s) read as text` — the number the rest of this list has to
  account for;
- an exclusion is logged with its skipped-file count, **including one that skipped nothing**;
- a file that cannot be *read* (a permission problem) or that is *not a regular file* (a FIFO,
  socket or device node) is a `::warning::` naming it, not a silent drop;
- a **submodule gitlink** is named as one — `git ls-files` lists it, and the workflow checks the
  caller out without `submodules:`, so it is an empty directory here. Its files belong to another
  repository and need their own hygiene run there;
- a **symlink** is a `::warning::` naming it too, saying that only the target string was read —
  it counts as scanned, because that string *is* the blob git stores at that path and publishes in
  the tree; the file it points at is not this repo's content and echoing it into a public run log
  would be the leak, not the guard;
- a **git-LFS pointer stub** is a `::warning::` naming it *and* a `NOT SCANNED:` count:
  `actions/checkout` does not fetch LFS objects, so the work tree holds the ~130-byte stub and the
  real content — publicly downloadable from the same repo — is never examined. It deliberately does
  **not** count as scanned: otherwise `git lfs track '*.md'` plus a commit carrying internal
  references would hold the zero-scan net below open and exit 0 on a required check. Coverage and
  detection are separate questions here, and this is the one file kind where they diverge — the
  stub is **still checked for references**, and the classification requires the *whole* pointer
  grammar (a `sha256` oid, a byte size, stub-sized). A first line alone would have made the skip an
  opt-out any file could take by typing the magic line;
- binary and non-UTF-8 files are expected skips, so they are a per-run `NOT SCANNED: <n>` count
  rather than a warning each — a count they must have, because one stray byte otherwise hides a
  whole file that still renders as text on GitHub. **UTF-16/UTF-32 is decoded, not skipped**: those
  encodings are self-describing via a BOM, and a blob committed in one carries its NUL bytes in
  what git *stores*, so no gitattribute is involved and the rule below would never see it;
- a file larger than the read cap is scanned up to it and the unread tail is a `::warning::`
  naming the file — truncated loudly, never dropped. It also gets a `PARTIAL: <n>` count, as does a
  file whose findings hit the per-file cap: both are "read, but not all of it", which `SCANNED:`
  and `NOT SCANNED:` cannot express, and unlike the per-file warning a **count is never capped**;
- a run that read **zero** files (nothing tracked, everything excluded, or nothing readable as
  text) **exits 2**, not 0. Rejecting a root-wide exclusion would otherwise be one spelling away
  from pointless: naming each top-level directory in `exclude_paths` disables the whole scan
  without ever naming the root;
- `_public_repo_hygiene/` — where the workflow checks this repo out inside the caller's
  workspace — is a **reserved** path. The checkout lands untracked, so an ordinary run never meets
  it; a caller that *tracks* content there fails with exit 2 and is told to rename the directory,
  because that content is shadowed by the checkout and can never be examined. Skipping it quietly
  is how the reserved path would have become a parking spot that ships green. The reusable workflow
  refuses *before* its own checkout too, so nothing at that path is ever replaced;
- a `working-tree-encoding` gitattribute on any path this run would read **exits 2**. It makes the
  bytes on disk differ from the bytes git stores, so a `UTF-16` conversion reads as binary here
  while the committed blob GitHub serves stays plainly readable — a green run over content the
  guard never looked at. The attributes are resolved from the **index** (`git check-attr --cached`),
  because the property is about the bytes git stores — and because a commit that converts
  `.gitattributes` itself would otherwise leave an unparseable attributes file on disk and the guard
  would fail open over exactly that commit. `UTF-8` is exempt (it is the identity mapping, and the
  BOM variants stay fatal), as are symlinks and gitlinks matching an encoding rule — `check-attr`
  answers by path pattern, and only a regular file can be re-encoded. An excluded path may carry
  one: that hole is already named by its exclusion count;
- per-file warnings are capped at `MAX_WARNINGS_TOTAL` (200) with a `+N more` tail, for the same
  reason the findings are. The **counts** are never capped, so the coverage claim above stays
  complete however many warnings are dropped.

## Why the allowlist lives here (BE-8654)

It began as two copies — `scripts/check_public_repo_hygiene.py` in the Python SDK and
`scripts/check-public-repo-hygiene.mjs` in the TypeScript SDK — each run out of the PR's own
checkout. Two problems, both observed rather than predicted:

- **The checker was editable by the PR it guarded.** The job was `actions/checkout` (which
  defaults to the PR merge ref) followed by `python3 scripts/check_public_repo_hygiene.py`. A PR
  could add a private repo name to the in-tree allowlist and then leak it, green. Loading the
  checker from a pinned ref of *this* repo is what closes that, exactly as
  `agents-md-integrity.yml` already does.
- **"Which Comfy-Org repos are public" is ORG-WIDE knowledge**, and copying it per repo let the
  copies rot independently. Both were missing `github-workflows` itself, so the caller every one
  of them needs failed the very check it was being added alongside — and fixing it meant the same
  edit twice, in two languages, in two repos.

Hosting the list in a public repo leaks nothing, and that is a property of the design, not luck:
the list contains **public repo names only**. No private repo name is ever written down — which
is the entire point of default-deny, and what makes this shareable at all.

## Tamper resistance — what is and is not caller-tunable

| Knob | Where it lives | Can a PR in the caller repo change it? |
|---|---|---|
| `PUBLIC_COMFY_ORG_REPOS` / `PUBLIC_COMFY_ORG_TEAMS` | this file, pinned by `workflows_ref` | **No.** Not an input in any form. An allowlist a caller can pass is an allowlist a PR in the caller repo can widen. |
| Which commit `workflows_ref` loads | the caller's workflow file | **No, in practice.** A `pull_request` caller runs its workflow file from the PR head, so a PR *can* edit `workflows_ref:` — which is why the workflow fails the run unless it is a full 40-hex SHA **equal to `job.workflow_sha`**, the commit the `uses:` pin resolved to. Pointing it at a `refs/pull/*` ref of this repo would otherwise run the PR author's own checker and report green. |
| Detection regexes | this file, pinned by `workflows_ref` | **No.** |
| `ticket_allowlist:` | the caller's workflow file | Yes — but it is **additive** (it can add an acronym, never drop a built-in one) and it reaches category 1 only. |
| `exclude_paths:` | the caller's workflow file | Yes — it drops files from the scan. It cannot widen a category, a value naming the repo root is rejected outright, and every entry is echoed to the run log with its skipped-file count *including one that skipped nothing*. |

The two tunable knobs are visible in the caller's workflow diff and in the run log, which is the
honest boundary: this design makes weakening the *rule* impossible from a caller repo and makes
narrowing the *scope* loud. Adding a repo to the allowlist is now one reviewed PR here rather
than one edit per consumer.

**Where the guarantee stops — the `uses:` line itself.** Everything above is about what a caller
can pass *in*. What no reusable workflow can enforce from the inside is *which* reusable runs: a
`pull_request` caller executes its workflow FILE from the PR head, so a PR that rewrites the
`uses:` line **and** `workflows_ref` together — to another commit of this repo, or to a fork of it
that never contained these checks — runs a different workflow, and the equality check inside *that*
one says nothing about this one. This is a property of GitHub Actions, not of this workflow, and it
applies equally to `agents-md-integrity.yml` and every other entry in the catalog. The control is
out of band: an adopter making this a **required** status check should also protect
`.github/workflows/` with a branch-protection rule or repository ruleset that **requires an
approving review from someone other than the author**, so the `uses:` line cannot move unreviewed.
CODEOWNERS on its own is **not** that control — it only *requests* reviewers, and blocks nothing
until a rule requires Code Owner approval. Stated precisely, what this design buys is that a
PR cannot reach the checker or the allowlist *through this workflow's inputs* — the failure the two
per-repo copies had, where an in-tree allowlist edit was all it took.

`tests/test_check_public_repo_hygiene.py::TamperResistanceTest` asserts the checker half (an
in-tree copy of the checker, a config file, and environment variables named after the constants
are all inert). [`test-public-repo-hygiene.yml`](../workflows/test-public-repo-hygiene.yml)
asserts the same end-to-end.

## Known limitations

What this checker still does not see. The two limitations the per-repo scripts had here — a
sentence-final period swallowed by the repo-name class, and an org segment matched
case-sensitively — were fixed in BE-8697 and are pinned by unit tests; that is where detection
parity with those scripts deliberately ended. BE-8729 took the category-2 host patterns off `\b`
and onto DNS-label boundaries, taught them about ports and pinned the host patterns to ASCII, which
removed the lookalike-host false positives, the `:443` and empty-port bypasses, and the
Unicode-case-folding lookalikes listed above.

- **The scan is line-oriented.** A reference split across two lines is not matched.
- **A colon-chained run before an unrelated `@` silences `app.slack.com`.** The right anchor's
  userinfo alternative keeps `:` in its character class, because `:user:pass@evil.com` is real
  userinfo and the real host there *is* `evil.com`. The same colon lets the run chain across prose,
  so `app.slack.com:443:ops@example.com` and a log line like
  `app.slack.com:2024-01-15:incident@comfy.org` go silent. What *does* stop the run is everything
  outside the credential class: whitespace, `/?#`, quotes, commas, braces and the sub-delims — the
  round-4 narrowing that recovered `app.slack.com:443,ops@example.com`. Dropping `:` would reopen
  the genuine `:user:pass@` phishing shape, so the miss is kept and pinned by
  `test_a_colon_chained_run_still_reaches_a_later_at`.
- **Userinfo longer than 64 characters over-flags.** The same alternative is length-bounded at 64
  because an unbounded run made a long single line quadratic to scan (`MAX_FILE_BYTES` bounds a
  *file*; nothing bounds a *line*). Past that bound the `@` is out of reach, so
  `https://app.slack.com:<65-char token>@evil.com/` is reported as `app.slack.com` although the
  real host is `evil.com` — realistic when a token or JWT rides as the basic-auth password. Over-flag,
  not a leak, and both sides of the boundary are pinned by
  `test_the_userinfo_length_bound_over_flags_past_64_characters` so moving the bound cannot
  silently retrade it.
- **A host written with a trailing root label is not matched by the `/`-requiring patterns.**
  `https://notion.so./page` is the same host to a resolver, but those patterns want `/` (or a port
  then `/`) directly after the host, so the extra dot walks past them. The limitation is scoped:
  `app.slack.com` is the one host-only pattern, and its right anchor deliberately tolerates a dot
  followed by a non-label character, so `https://app.slack.com./x` IS matched. This predates the
  DNS-label anchoring and is unchanged by it; obfuscated spellings of the same hosts —
  percent-encoding (`%2E`), punycode (`xn--`), defanging (`notion[.]so`), a backslash separator
  (browsers resolve `https://notion.so\page` to `notion.so/page` for special schemes, but the
  patterns want a literal `/`), and UTS-46-*mapped* characters (`ſlack.com`, `slacK.com` with
  U+212A, both of which really do resolve to `slack.com`) — are likewise not matched. This is a
  hygiene guard against an accidental paste, not an adversary who is actively hiding a link.
- **An internationalized neighbour of a category-2 host reports as that host.** The left anchor is
  ASCII, so `https://énotion.so/page` — a different registrable domain — is flagged as
  `notion.so`. Rejecting *any* non-ASCII character in front would fix it and cost more than it
  saves: it would also silence a real link written straight after a curly quote, an em dash or CJK
  prose, and a missed leak is worse here than an extra finding. **The right-hand side has the same
  gap**, since the `app.slack.com` anchor's classes are ASCII too: `https://app.slack.com.中国/`
  (IDNA-mapped to `app.slack.com.xn--fiqs8s`) and `https://app.slack.comévil.com/` are both
  reported as `app.slack.com`, pinned by
  `test_a_non_ascii_label_continuation_reports_as_the_literal_host`. **ASCII `_` is the same shape
  in the same direction** — `_` is unreserved and WHATWG accepts it in a host, so
  `https://evil_notion.so/page` reports as `notion.so`, and on the right
  `https://app.slack.com_user@evil.example/` (userinfo `app.slack.com_user`, host `evil.example`)
  reports as Slack. Adding `_` to the classes is not free either: it would silence a real link
  written in markdown emphasis (`_notion.so/page_`). The reverse direction is narrowed, not closed —
  the category-2 **host** patterns compile with `re.ASCII`, so Unicode case-folding no longer
  reads `lınear.app/x` or `notİon.so/page` as the real hosts. Those two are genuinely different
  hosts under UTS-46; `ſlack.com` and `slacK.com` are not, so they move from over-flag to miss and
  are listed with the other obfuscated spellings above.
- **A `Comfy-Org/<name>` reference butted straight against non-Latin prose is a finding.** The
  name class is ASCII, so anything outside it immediately after the capture is treated as the rest
  of the *name* rather than as a boundary — that is what stops a U+2010 homoglyph from clearing as
  `comfyui`, and the price is that `Comfy-Org/ComfyUIを使う` (no separator) reports. A space
  clears it, and the finding message says so. Narrowing the rule to characters that casefold onto
  ASCII would reopen the bypass for every alphabet that does not, so it is deliberately broad.
- **Only the literal `app.datadoghq.com` host is covered.** Datadog hands an organization its own
  `<name>.datadoghq.com` sub-domain, so unlike `*.google.com` that namespace is not vendor-only —
  and a dashboard on a custom sub-domain (`comfyapp.datadoghq.com/dashboard/1`) is a different
  host to the DNS-label anchor and is not matched. Before BE-8729 the pattern carried no left
  anchor and caught such a host by accident, as a substring; that was never a rule the pattern
  set stated. Add a second pattern if an org sub-domain is ever in use.
- **A label character adjacent to a three-label host silences it.** Same cause as the Datadog
  bullet, seen from both sides. "A letter, digit or hyphen in front means a different registrable
  name" is exact only for the *two-label* patterns (`notion.so`, `slack.com`, `posthog.com`,
  `linear.app`); for a three-label host it just extends the third-level label, so
  `https://my-app.slack.com/ssb/redirect` — a real slack.com workspace host — is not matched, and
  neither is a prose hyphen on the right (`app.slack.com-hosted workspace`, where `\b` used to hold
  because a hyphen is a non-word character). Recovering either costs an over-flag on a shape that
  really *is* a different registrable name (`app.slack.com-evil.com` → `com-evil.com`), so both are
  kept and pinned by `test_a_label_character_adjacent_to_the_host_is_a_known_miss`.
- **Only file *contents* are scanned, never file *paths*.** A `docs/<TICKET>-migration.md` or a
  `notion-exports/` directory passes clean.
- **Git-LFS content, submodule contents and the far side of a symlink are not scanned** — each is
  counted under `NOT SCANNED:` (or, for a symlink, scanned as the link target *string* git actually
  stores) rather than silently treated as covered. See the coverage rules below.
- **A `working-tree-encoding` gitattribute is refused, not worked around.** It would make the bytes
  on disk differ from the bytes git stores — `UTF-16` on disk reads as binary here while the
  committed blob GitHub serves stays plainly readable — so a tree carrying one on any path this run
  would read is a hard configuration error (exit 2), naming the file.

## Running it

```bash
python3 .github/public-repo-hygiene/check_public_repo_hygiene.py --root .
python3 .github/public-repo-hygiene/check_public_repo_hygiene.py --root . \
    --exclude 'src/generated/' --ticket-allow 'GPU-100'

python3 -m unittest discover -s .github/public-repo-hygiene/tests -p 'test_*.py' -v
```

Exit codes: `0` clean, `1` findings, `2` the run proves nothing — an unusable `--exclude`, a root
that is not a git work tree, tracked content at the reserved `_public_repo_hygiene/` path, a
`working-tree-encoding` gitattribute on a path this run would read, or a scan that read zero files.
"Nothing to scan" must never read as "clean".

## Adding a repo to the allowlist

Confirm it is genuinely public first — `gh repo view Comfy-Org/<name> --json visibility` — then
add the name to `PUBLIC_COMFY_ORG_REPOS` with a one-line comment, in its **case-insensitive
alphabetical slot** — not appended at the bottom. A `frozenset` has no order at run time, so the
source text is the only place the order lives, and it is the view a human reads to check whether a
name is already there; `AllowlistSourceOrderTest` fails the build on an out-of-order or duplicated
entry rather than leaving it to review. Then merge. The
`bump-public-repo-hygiene-callers.yml` fleet rolls the new SHA out to every enrolled caller; a
caller that is not on the roster keeps scanning against the old list, which is the usual
two-step-enrollment footgun this repo documents in `AGENTS.md`.
