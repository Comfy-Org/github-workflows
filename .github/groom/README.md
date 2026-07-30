# Groom — two-phase code-cleanup briefs + durable rejection ledger

Version-controlled, co-ownable **prompts** for the agent-work *groom* workflow: a
periodic, org-wide sweep that proposes high-value refactors (duplication,
inconsistent patterns, missing abstractions, complexity hotspots, dead code) and
files the survivors as tech-debt tickets.

Groom runs in **two phases**, each a fresh single-shot agent:

1. **Finder** ([`finder.md`](finder.md)) — reads a clean `origin/main` checkout
   and proposes *candidate* findings against a hard precision bar (~6–12, ranked,
   each with a steelman-against). Read-only; its only output is a JSON file.
2. **Verifier** ([`verifier.md`](verifier.md)) — an **independent adversarial
   skeptic** in a fresh session that sees only the finder's JSON and the code. It
   re-checks each candidate and assigns `CONFIRM` / `DOWNGRADE` / `REJECT`, flags
   anything security-adjacent, and emits a stable dedup `signature` per finding.

The finder's JSON file is the **only** handoff between the phases — the verifier
never sees the finder's reasoning, only its claims and the actual code. That
separation is the whole point: the skeptic can't be anchored by the proposer.

**Optional phase 3 — the auto-builder** ([`builder.md`](builder.md), BE-4003).
When the workflow runs with `builder: true`, the top few CONFIRMED, non-security
findings are handed one at a time to a **credential-free** builder agent that
writes the code change into its checkout; a separate no-agent job captures the
diff, opens a **review-gated PR** as the bot (never auto-merged), and the
ledger's PR-state stops that finding from being re-proposed. The builder holds no
credentials — it can only produce a *patch*, never push. Default off: the
finds-only groomer (issues) stays the default.

These two files are the **single source of truth** for the groom prompts, the
same way [`.github/cursor-review/`](../cursor-review) is for the review panel.
The core thesis of the groom initiative is *collaborate on the prompt, not the
code* — so the prompts live here as reviewable artifacts the team PRs against,
rather than buried in a runner script.

## The two-phase contract

| Phase | Brief | Input | Output (JSON) |
|---|---|---|---|
| 1. Find | [`finder.md`](finder.md) | clean `origin/main` checkout + scan scope | `{repo, scope, findings:[{title, dimension, sites, evidence, proposed, value, risk, confidence, steelman}]}` at `{{FINDER_OUT}}` |
| 2. Verify | [`verifier.md`](verifier.md) | the finder's JSON + the code | `{repo, scope, summary, findings:[{title, verdict, security, signature, body}]}` at `{{VERIFIER_OUT}}` |
| 3. Build (opt-in) | [`builder.md`](builder.md) | ONE verified finding `{title, body, signature}` at `{{FINDING_IN}}` + the code | edits in the checkout + a control file `{status: patched\|bail, summary}` at `{{BUILDER_OUT}}` |

- **`verdict`** is `CONFIRM` \| `DOWNGRADE` (real but narrow the scope) \|
  `REJECT` (premature / overstated / not worth it).
- **`security: true`** marks any auth/permission/security-adjacent finding —
  those are filed as investigations, **never** auto-implemented.
- **`signature`** is a stable dedup key, `<repo-basename>:<scope>:<path-slug>`,
  where `<path-slug>` is the finding's **primary file or directory path** —
  lowercased, every run of non-alphanumeric characters collapsed to a single
  hyphen, leading/trailing hyphens trimmed (`src/tools.ts` → `src-tools-ts`,
  `services/ingest/` → `services-ingest`). Multi-file finding: the
  **alphabetically first** of the cited paths — a mechanical rule, because "the
  most representative one" is a judgment the verifier would re-make differently
  next run. Only a repo-wide pattern with no single anchor falls back to a
  normalized subject noun-phrase. A `security: true` finding's slug is prefixed
  `sec_` — underscore, because slugification can never produce one, so the
  security lane for `auth.ts` (`sec_auth-ts`) cannot collide with a routine
  finding about `sec/auth.ts` (`sec-auth-ts`). That lane is what stops a routine
  finding already filed for a file from deduping away a security finding about
  that same file.
  It is anchored to the **path, not the title**, because titles are re-generated
  on every run: a re-worded title yields a new title-slug, the ledger's
  exact-string match sees a "new" finding, and the same finding is filed twice
  (observed in a consumer repo: one `src/tools.ts` finding, two issues opened by
  consecutive runs). Paths survive rewordings, so the signature stays identical
  across re-runs and a consumer never re-files a finding it has already seen.

## How a consumer uses these briefs

A consumer (the studio groom daemon today; the reusable groom workflow —
**Phase 2, forthcoming** — next) treats each brief as a **template**: fetch the
file at a pinned ref of this repo, substitute the placeholders below, and pass
the result as the phase's prompt. Read the file so the trailing newline is
stripped (e.g. `"$(cat finder.md)"` / `"$(< finder.md)"`); command substitution
drops it, so the prompt matches the intended text exactly.

### Placeholders

Both briefs use `{{DOUBLE_BRACE}}` tokens (chosen so they never collide with the
single-brace JSON in the briefs). A consumer replaces every occurrence:

| Placeholder | Expands to |
|---|---|
| `{{REPO}}` | target repo, `owner/name` (e.g. `Comfy-Org/cloud`) |
| `{{REPO_BASENAME}}` | just the repo name (e.g. `cloud`) — used in the dedup `signature` |
| `{{CLONE}}` | absolute path of the clean `origin/main` checkout |
| `{{SCOPE_DESC}}` | human scan-scope sentence (a package, or "the whole repository") |
| `{{SCOPE_LABEL}}` | short scope label (the package path, or `whole-repo`) |
| `{{FINDER_OUT}}` | path the finder writes its candidate JSON to |
| `{{VERIFIER_OUT}}` | path the verifier writes its verified JSON to |
| `{{FINDING_IN}}` | (builder) path the single finding to build is read from |
| `{{BUILDER_OUT}}` | (builder) path the builder writes its `{status, summary}` control file to |

`{{FINDER_OUT}}` appears in **both** briefs (the finder writes it; the verifier
reads it); `{{VERIFIER_OUT}}` and `{{REPO_BASENAME}}` appear only in the
verifier.

Substituted values are trusted, runner-controlled strings (repo slugs, package
paths, output file paths). They land verbatim inside quoted JSON in the briefs, so
a consumer that could ever pass a value containing a quote, backslash, or newline
must JSON-escape it first (or keep it to a safe charset).

Because the placeholders sit exactly where the runner's inline values used to be,
a template + substitution reproduces the previous inline prompt with no change to
**what groom finds** — which is how the studio daemon can adopt the shared briefs
(see the parity note in the initiating PR). The briefs additionally fold in the
review panel's safety rails — the `security` flag as an explicit placeholder, and a
read-only + untrusted-input boundary on both phases — which harden behavior without
changing the findings themselves.

## `ledger.py` — the durable dedup / rejection ledger (BE-3874)

A **stateless CI run** starts fresh every time — with no durable memory it would
re-file findings that were already filed OR already human-rejected on every
scheduled run. That is the fastest way to make the shared groom capability
annoying and get it disabled. The roundtable was explicit: *dedup must remember
REJECTIONS — don't re-raise a rejected finding next week.*

`ledger.py` uses **GitHub issue state itself** as the durable store — the
GitHub-native option that needs **no net-new secret** (the run's `GITHUB_TOKEN`
already reads issues) and is fully **auditable** (the record is the issues you
can see). No separate database, cache, or committed state file.

Keyed on `(repo, finding_signature) → {filed | rejected | superseded}`:

| Live GitHub state | Ledger status | Re-file / re-propose? |
|---|---|---|
| Open `groom` issue for the signature | `filed` | no |
| Closed as **completed** | `filed` | no (already handled) |
| Closed as **not planned** (GitHub "close as wontfix") | `rejected` | **no — durable** |
| Carries the `groom-rejected` label (open or closed) | `rejected` | **no — durable** |
| Carries the `groom-superseded` label | `superseded` | no |
| Open **builder PR** for the signature (BE-4003) | `pr-open` | no |
| **Builder PR merged** | `merged` | no (shipped) |
| **Builder PR closed unmerged** | `pr-closed` | **no — durable** (human declined) |
| A known, non-`superseded` signature shares the candidate's `<path-slug>`, and the candidate is not `security: true` (BE-4460) | `path-collision` | no |
| No `groom` issue or PR carries the signature | `unknown` | **yes** |

Only an `unknown` signature is filed/proposed. Human rejection — close-as-not-planned,
the `groom-rejected` label, or a **closed-unmerged builder PR** — suppresses that
signature forever. The auto-builder's PRs carry the signature marker in their body
exactly like a filed issue, so the same ledger recognizes them: the `/issues`
listing returns groom-labeled PRs too, and the marker check (a human-opened,
markerless `groom` issue/PR is ignored) is what keeps including PRs safe.

### The filing contract (load-bearing)

This module consumes the verifier's stable dedup `signature` (see above) as an
opaque string on each finding. For the memory to survive, the step that OPENS an
issue for a `to_file` finding **must**:

1. apply the **`groom`** label (how the next run finds our issues), and
2. append `signature_marker(finding["signature"])` to the issue body — an
   invisible HTML comment (`<!-- groom-signature: … -->`) the next run recovers.

Skip either and the next run cannot recognize the issue and will re-file it.

### The path-token backstop (BE-4460)

Classification treats the signature as an opaque string, with one exception: a
candidate whose exact signature is `unknown` but whose `<path-slug>` segment
(everything after the **last** `:` — `<repo-basename>` and `<path-slug>` are
colon-free by construction, so counting from the right is what keeps a scope
label that itself contains a colon, `pkg:api`, from shearing the token) is
already covered — by a known signature, or by a candidate already routed to
`to_file` in the same batch — is suppressed as **`path-collision`** rather than
filed. That keeps one issue per anchoring path when the leading segments differ
(a re-scoped run, a legacy signature whose slug coincides). Matching is **exact
string equality** on the path segment — no substring or fuzzy matching, which
would silently drop real findings about different files that share a basename
(`src/index.ts` vs `lib/index.ts`).

Because the backstop suppresses a candidate whose *own* signature is new, it is
deliberately narrow — two carve-outs:

- **`security: true` candidates are never suppressed by it.** A path anchors a
  location, not a finding, so without this an already-filed routine finding on
  `src/tools.ts` would bury a later security finding on the same file and break
  the "security findings always surface as investigations" guarantee. (The
  verifier's `sec_` slug prefix separates the two lanes up front; this is the
  code-side guarantee for legacy and cross-scope signatures that predate it, and
  it fails **closed** — `is_security_finding` treats a finding whose flag the
  verifier omitted or mangled as security, so a malformed flag can never be what
  buries one.) Exact-signature dedup still applies, so the exemption costs at
  most one extra issue — the next run sees it as `filed`. It does **not** make
  security findings individually addressable: two distinct vulnerabilities in one
  file share the `sec_<path>` key and collapse, exactly as two routine findings
  on one file do (see the limit below).
- **`superseded` records are left out of the path index.** `groom-superseded` is
  the documented "retire this issue so its finding can be re-filed under the
  current format" signal; keeping it in the index would let the retired issue go
  on suppressing the replacement by path and defeat the label a human applied.

Known, accepted limit — and a **permanent** suppression, not a one-time
transition cost. A path-anchored key identifies a *location*, so every later
finding that maps to an already-covered token is dropped for good: two different
findings about one file, and two genuinely distinct paths that slugify alike
(`src/foo/bar.ts` and `src/foo-bar.ts` both → `src-foo-bar-ts`, or a file and a
same-stem directory). That is the deliberate trade for a key that survives
re-wording — one issue per anchoring path, chosen over the duplicate-per-run
spam the title-derived key produced. Widening it needs a per-finding
discriminator that is *stable across runs*, which is exactly what the LLM cannot
supply today; the `security` flag is one bit that is, which is why the security
lane is carved out of this and nothing else is.

Consequence for the format transition: a legacy *title*-derived slug that merely
*embeds* the path (`split-tools-ts-into-focused-modules`) is **not** matched, so
such a finding can be filed once more under its new path-anchored signature —
then it is stable forever. Label the superseded legacy issue `groom-superseded`
(or close it as not planned) to retire it. A security finding already filed under
an unprefixed slug re-files once for the same reason when it picks up its `sec_`
prefix — same one-per-finding, one-time transition cost, same fix.

The dedup decision is a point-in-time snapshot of GitHub issue state read
*before* filing, and issue creation happens in a later step. Two overlapping
groom runs could therefore both classify the same signature as `unknown` and
file duplicates (a TOCTOU race). The caller workflow (not yet written — epic
BE-3870) **must serialize groom runs with a `concurrency:` group** so at most
one run reads-then-files at a time.

### CLI (called right before the groomer files)

```bash
python3 .github/groom/ledger.py \
    --repo owner/name --candidates findings.json --out decision.json
```

`findings.json` is a JSON array of findings, each with a `signature`.
`decision.json` receives `{to_file, suppressed, invalid, ledger_size}` — open
issues only for `to_file`. `invalid` = findings with no usable signature; they
are **not** filed (filing an un-dedupable finding would risk the exact
duplicate-spam this ledger prevents) and should be surfaced as a producer error.

Single-signature probe (exit 0 = should file, 1 = suppressed):

```bash
python3 .github/groom/ledger.py --repo owner/name --check "<signature>"
# ...as a security finding, which the path backstop never suppresses:
python3 .github/groom/ledger.py --repo owner/name --check "<signature>" --check-security
```

A bare signature carries no `security` flag, so the probe answers for a routine
finding by default; pass `--check-security` to mirror `partition`'s decision for
a `security: true` candidate.

## `interval.py` — the runtime cadence gate (BE-4004)

GitHub Actions `schedule:` cron is **static in the workflow file** — there is no
native "every N days" input. So a caller fires on a **frequent (daily) base
cron**, and this gate turns that into an **effective every-`GROOM_INTERVAL_DAYS`
run**: at run start it early-exits unless the interval has elapsed since the last
real groom, so a skipped tick costs ~nothing (it never reaches the finder).

- **The knob is a repo Actions variable, `GROOM_INTERVAL_DAYS`** (default `7` =
  weekly, matching the original cron). The caller wires it to the reusable's
  `interval_days` input (`interval_days: ${{ vars.GROOM_INTERVAL_DAYS || '7' }}`)
  and re-evaluates it each run, so changing the variable retunes cadence — weekly
  → every-3-days → daily — with **no workflow-file edit**, the same "live knob"
  ergonomics as the per-repo caps. Both cadence inputs (`interval_days`,
  `cadence`) are declared **`type: string`** deliberately: they carry a free-text
  Actions variable, and a `number` input would make GitHub reject a typo'd value
  (`weekly`, `7d`) at workflow-call time — failing the run *closed* before the
  degradation below could ever run. As strings, `interval.py` is the single
  normalization authority.
- **A tick clears the bar a half-tick early.** GitHub's cron fires late by an
  unpredictable amount, so demanding a full `interval_days` on a daily tick would
  skip at 6.99 days elapsed, push the run to tomorrow, and — because the clock
  re-anchors on that later run — ratchet the cadence a day later every cycle. The
  gate compares against `interval_days` less `0.5` (capped at half the interval),
  which absorbs the jitter without letting two real runs land on consecutive
  daily ticks (those are a full ~1.0 day apart).
- **Last-run state is derived from GitHub Actions run history**, not a writable
  store: the GitHub-native option that needs **no net-new secret** and only
  `actions: read`. A prior run "counts" only if it actually reached the finder
  (its `Audit — finder` job ran, not `skipped` by this gate), so the
  interval-skip ticks in between never reset the clock. (A repo variable would
  need a `Variables: write` credential the run doesn't carry, and a missing grant
  would fail *silently* into a daily over-spend — run history has no such trap.)
- **`workflow_dispatch` bypasses THIS gate** — a manual dispatch is never
  interval-throttled. It is not a blanket "always runs": the volume gate is a
  second, independent throttle, so a live dispatch into a quiescent repo can
  still skip. Turn `volume_gate` off (the reference caller does exactly this for
  `dry_run:true` dispatches) if a manual run must always reach the finder.
- **Fail-open**, like the volume gate: any error reading history (API hiccup, no
  history, unparseable timestamp) RUNS the audit rather than skip a due groom.
- **One normalization for both gates.** The caller wires the same variable to
  `cadence` (the volume gate's merge-activity window), so the volume gate routes
  it through this module too — `interval.py --normalize-cadence "$CADENCE"` —
  rather than feeding the raw value to `date -d`. Same degradation
  (blank/garbage/negative → `7`), then floored at **1 whole day**. Without it the
  gates drift on reachable values: `-3` becomes a *future* `date -d` cutoff that
  matches no merged PR (skipping every run — groom silently off) while the
  interval gate had safely degraded to weekly, and `0` (a legitimate "no
  throttle") shrinks the merge window to today-only.

The caller **must** grant `actions: read`. The `gate` job declares that scope, and
a nested reusable job can never hold more than the calling job grants — GitHub
checks the subset at **startup**, so a caller that omits it has the whole run
rejected (`requesting 'actions: read', but is only allowed 'actions: none'`,
surfaced as an opaque "workflow file issue" with zero jobs) rather than degrading
to a fail-open daily run. Fail-open covers the *other* failure: the grant is
present but the history read errors (fresh repo with no runs, API hiccup) — then
the gate runs rather than skips. As with `ledger.py`, the pure decision logic is
split from the thin `gh` I/O so it is fully unit-testable with no network.

```bash
python3 .github/groom/interval.py \
    --repo owner/name --workflow-file ci-groom.yml \
    --current-run-id 123 --interval-days 7 --event-name schedule

# Second mode — normalize the shared knob into the volume gate's window:
python3 .github/groom/interval.py --normalize-cadence "$GROOM_INTERVAL_DAYS"
```

- **`tests/`** — `unittest` suite, run by
  [`test-groom-scripts.yml`](../workflows/test-groom-scripts.yml).

```bash
python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
```
