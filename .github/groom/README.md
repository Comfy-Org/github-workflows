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

A builder patch that touches a **CI-privileged path** — workflow/action
definitions, dependency **lockfiles** (`package-lock.json`, `pnpm-lock.yaml`,
`Cargo.lock`, …), package manifests, or build/test config — is downgraded from a
PR to a filed issue: on a same-repo branch push that code executes in
credentialed CI *before* a human reads the diff (review gates merge, not CI
exec). The deny-list is the tested [`patch_policy.py`](patch_policy.py) (BE-4404)
— a conservative default, not a proof of completeness, so read it before setting
`builder: true` on a repo whose CI runs something else privileged. **Structural
limit:** the policy guards privileged-*config* surfaces, but any patch's source
code still executes when the caller's CI runs its *tests* — a review-gated PR is
untrusted code running pre-merge. Callers enabling the builder should avoid
exposing secrets to test steps and consider `npm ci --ignore-scripts` (or
equivalents) where viable.

### Builder bail-outs, and what `max_findings` does *not* cover (BE-6157)

A build that cannot become a PR **bails**: the builder produced no patch, the
patch exceeds `pr_size_limit`, the patch touches a CI-privileged path
(`.github/workflows|actions/`, build/test config), the patch does not apply, or
the pre-publish secret scan withheld it. By default the bail is filed as a `groom`
issue, so a CONFIRMED finding the builder already spent tokens on is handed to a
human rather than discarded.

Two things follow that are easy to get wrong:

- **`max_findings` does not govern bail issues.** It caps the NEW **findings**
  issues the `file` job opens after dedup — a flood backstop, nothing more. Bail
  issues are opened by the separate `build_pr` job, so `max_findings: 0` silences
  the findings path and a bail issue can still appear. That is deliberate (losing
  paid-for work is worse than one extra issue), and it was surprising enough in
  practice to be worth stating twice.
- **`bail_sink` is the knob for the bail path.** `issue` (default) keeps the
  behavior above; `none` files nothing and instead emits a `::warning::` naming
  the finding, its bail reason and its signature, plus a run-summary line — so the
  bail is visible in the run rather than invisible. Because no issue is filed, no
  signature marker is recorded, so a later run re-proposes the finding; a
  *deterministic* bail (a patch that always exceeds `pr_size_limit`, or always
  touches a CI-privileged path) therefore re-bails on every run and permanently
  holds one of the `max_prs` slots — at `max_prs: 1`, nothing else ever gets
  built. The one bail `none` does **not** suppress is the pre-publish secret-scan
  withhold: that issue is filed regardless, because an expiring `::error::` in the
  run log is not a durable record of a possible key-exfil attempt.

`bail_sink` is an **operational** knob (`vars.GROOM_CONFIG` can set it with no
PR), unlike `sink` / `pr_size_limit` / `builder`, which stay in the reviewed
workflow file — the withhold carve-out above is what keeps that classification
honest: the knob can make groom quieter, never less safe. If bails are frequent because well-scoped patches keep landing
just over the line, the real fix is usually raising `pr_size_limit` **in the
caller** — a reviewed commit, by design — not suppressing the signal.

These two files are the **single source of truth** for the groom prompts, the
same way [`.github/cursor-review/`](../cursor-review) is for the review panel.
The core thesis of the groom initiative is *collaborate on the prompt, not the
code* — so the prompts live here as reviewable artifacts the team PRs against,
rather than buried in a runner script.

## The two-phase contract

| Phase | Brief | Input | Output (JSON) |
|---|---|---|---|
| 1. Find | [`finder.md`](finder.md) | clean `origin/main` checkout + scan scope | `{repo, scope, findings:[{title, dimension, sites, evidence, proposed, value, risk, confidence, steelman}]}` at `{{FINDER_OUT}}` |
| 2. Verify | [`verifier.md`](verifier.md) | the finder's JSON + the code | `{repo, scope, summary, findings:[{title, verdict, security, sites, signature, body}]}` at `{{VERIFIER_OUT}}` |
| 3. Build (opt-in) | [`builder.md`](builder.md) | ONE verified finding `{title, body, signature}` at `{{FINDING_IN}}` + the code | edits in the checkout + a control file `{status: patched\|bail, summary}` at `{{BUILDER_OUT}}` |

- **`verdict`** is `CONFIRM` \| `DOWNGRADE` (real but narrow the scope) \|
  `REJECT` (premature / overstated / not worth it).
- **`security: true`** marks any auth/permission/security-adjacent finding —
  those are filed as investigations, **never** auto-implemented.
- **`sites`** is the `file:line` evidence the verdict actually rests on — the
  NARROWED set on a `DOWNGRADE`. On a path-scoped run `scope.py verify` re-applies
  the directory filter to it, because a downgrade may narrow a cross-boundary
  finding onto its out-of-scope half.
- **`signature`** is a stable dedup key, `<repo-basename>:<scope>:<path-slug>`.
  The `<scope>` component is the caller's own `scope_label`, never the audited
  directory — and `scope.py verify` rewrites it back to that value, so
  scope-independence does not depend on the model obeying the brief (one defect
  found by a directory-scoped run and by a whole-repo run yields ONE signature
  and is filed ONCE). `<path-slug>` is the finding's **primary file or directory
  path** — lowercased, every run of non-alphanumeric characters collapsed to a
  single hyphen, leading/trailing hyphens trimmed (`src/tools.ts` →
  `src-tools-ts`, `services/ingest/` → `services-ingest`). Multi-file finding:
  the **alphabetically first** of the cited paths — a mechanical rule, because
  "the most representative one" is a judgment the verifier would re-make
  differently next run. Only a repo-wide pattern with no single anchor falls
  back to a normalized subject noun-phrase. A `security: true` finding's slug is
  prefixed `sec_` — underscore, because slugification can never produce one, so
  the security lane for `auth.ts` (`sec_auth-ts`) cannot collide with a routine
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
| `{{REPO}}` | target repo, `owner/name` (e.g. `Comfy-Org/example`) |
| `{{REPO_BASENAME}}` | just the repo name (e.g. `example`) — used in the dedup `signature` |
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

## `key-broker.mjs` — the localhost API-key proxy (BE-4419)

A tiny, dependency-free localhost HTTP proxy that **holds the real Anthropic API
key and injects it into forwarded requests**, so the groom agent steps (BE-4311)
can run the Claude CLI with only a **dummy** key and `ANTHROPIC_BASE_URL` pointed
at the broker — the real key never enters the agent step's environment.

**Why not just put the key in the agent step's env?** The groom agent has an
unscoped `Read` tool and `Bash(cat:*)`, and everything on the runner is the same
user, so `/proc/<agent-pid>/environ` — and, crucially, `/proc/<broker-pid>/environ`
and `/proc/<broker-pid>/cmdline` — are all agent-readable. The broker's design
follows from that:

1. **The real key arrives on stdin (first line), never via env or argv.** A key in
   the process environment or command line would be recoverable straight out of
   `/proc`. Stdin is not. The broker reads exactly the first line at startup and
   exits non-zero if stdin closes without one.
2. **It never logs request/response headers or bodies** — the log file is
   agent-readable too. At most it logs `method path -> status`.
3. **It listens on `127.0.0.1` only.**

What it does per request:

- `HEAD` / `GET` on `/` → answered locally with `200` (the CLI's connectivity
  probe; never forwarded).
- Path starting with `/v1/` → forwarded to the upstream preserving method and
  body (streamed both ways, so SSE works), **stripping** any inbound `x-api-key`,
  `authorization`, and `host`, then **injecting** the real `x-api-key` and the
  upstream `host`. Upstream status + response headers are copied back verbatim.
- Anything else → `404` locally. Upstream connection error → `502` with a static
  body (no detail echoed).

Env knobs (config only — **never** the key):

| Env var | Default | Meaning |
|---|---|---|
| `GROOM_BROKER_PORT` | `8199` | port to listen on (`127.0.0.1:<port>`) |
| `GROOM_BROKER_UPSTREAM` | `https://api.anthropic.com` | where `/v1/*` is forwarded (overridable so tests can point it at a local fake; `http://` is accepted **only** for loopback hosts, so a plaintext non-loopback upstream can't leak the injected key) |

On listen it prints one readiness line (`groom-key-broker listening on
127.0.0.1:<port>`); a consumer's wait-loop should key off **that line** (proof
the broker itself bound the port), not merely the port being connectable — a
foreign process already holding the port would pass a bare connect check while
the broker exits with `EADDRINUSE`, and the consumer would then stream prompts
and repo data (plus the dummy key) to an unrelated listener.

> **groom.yml wiring lands in the sibling ticket (BE-4311)** — this file adds the
> broker script + its unit tests only; nothing in `groom.yml` calls it yet.

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
- **A NON-SUCCESS finder job counts only if it actually spent the audit**
  (BE-4814). A failure that reached the agent and died later (the JSON assert, an
  upload) still counts — otherwise a run that cost money re-spends on tomorrow's
  tick. But the job can also die *before* the agent (checkout, asset load, prompt
  build), and those bill nothing, so counting them would advance the clock and
  suppress every tick for a whole `GROOM_INTERVAL_DAYS` — hiding a typo'd input
  or a broken caller for a week rather than letting it recur daily until someone
  notices. The gate therefore requires **positive evidence**: the jobs API's
  per-job `steps[]` must show the agent step (`Run finder`, pinned as
  `interval.agent_step_name()`) actually started. Every ambiguity — no `steps[]`,
  an empty one, the step absent, still `queued`, or `skipped` — reads as **not
  audited**, i.e. re-run. A duplicated audit costs one run; a suppressed one
  hides a broken caller for a full interval. A `success` needs no such check (the
  agent step is upstream of everything that could still fail, and no `if:`
  guards it).
  - Which endings take the evidence path is a **denylist**, not an enumeration:
    only `skipped` (this gate's own interval-skip) and an unfinished run are
    excluded outright. Everything else — `failure`, `timed_out`, `cancelled`,
    and the rarer `neutral`/`stale`/`action_required` — is decided by the step
    evidence, because if the agent ran, the audit was spent however the job was
    finally stamped. An allowlist would silently forget any ending it missed.
  - `timed_out`/`cancelled` are the expensive members: the finder job runs under
    `timeout-minutes: 40`, so a **hung agent bills the whole window** and only
    then trips the timeout. GitHub stamps that in-flight step `cancelled` —
    indistinguishable *by conclusion* from a step that was never reached, so the
    gate reads the step's **timestamps**: a `started_at` strictly before its
    `completed_at` proves it ran; no span is no evidence (fail-open).
  - Evidence is looked for across a run's **earlier attempts**, not just the
    latest. The jobs endpoint reports only the newest attempt, so a manual
    re-run that dies in checkout would otherwise erase the record of an earlier
    attempt that did reach the agent — and re-spend that audit. The walk is
    newest-first and bounded, and when an earlier attempt supplies the evidence
    the clock anchors on **that attempt's** finder-job start, not the run's
    `run_started_at` (which tracks the re-run and would date a week-old audit to
    today).
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

## `package.json` — the agent CLI pin (BE-5373)

`.github/groom/package.json` is **not a project**. Nothing is ever installed from
this directory, there is no lockfile, and no CI job runs `npm install` here. Its
only job is to be the single, machine-visible source of truth for the
`@anthropic-ai/claude-code` version that `groom.yml` installs in its three agent
jobs (finder, verifier, builder):

```json
{ "dependencies": { "@anthropic-ai/claude-code": "X.Y.Z" } }
```

(The live value is deliberately not repeated here — [`package.json`](package.json)
is the only place it appears, which is the whole point.)

Why a manifest instead of an `env:` constant at the top of the workflow:
Dependabot's `github-actions` ecosystem only parses `uses:` refs, so an inline
`npm install -g <pkg>@<ver>` inside a `run:` step is invisible to **every**
ecosystem when the repo has no npm manifest. The version therefore had nothing
watching it and simply rotted — while being duplicated across three call sites,
so a hand bump could update two and leave a split state. A real manifest makes
the npm ecosystem see it; the `/.github/groom` entry in
[`.github/dependabot.yml`](../dependabot.yml) opens the bump PR.

`groom.yml`'s `gate` job reads the pin **once**, validates it, and exports it as
the `claude_code_version` job output; all three install steps consume that output
via `needs.gate.outputs`. So there is no version literal left in the workflow, and
a merged bump PR moves every call site at once.

**Which ref the pin is read from — `job.workflow_sha`, not `workflows_ref`.**
Unlike the briefs and `ledger.py`/`interval.py`, the manifest is *not* loaded
from `$GROOM_ASSETS`. It gets its own sparse checkout at the commit this
`groom.yml` itself was read from, i.e. the ref the caller pinned `uses:` to.
That matters twice over, because this is executable supply chain rather than a
prompt:

- `workflows_ref` is `required: false`, and a caller may legitimately point it at
  a mutable BRANCH (the documented brief-testing override). A caller that SHA-pins
  `uses:` and does that would have *what executes* inside the three agent jobs
  tracking a branch tip, while the sandbox flags those jobs pass stay frozen at
  the pinned SHA. Reading from `job.workflow_sha` keeps the CLI version and the
  flags that depend on it on the same commit, so SHA-pinning `uses:` alone fully
  pins the CLI. (Since BE-8077 the asset checkouts fall back to
  `job.workflow_sha` too when the input is *omitted*, so on that path the two
  agree by construction — this step simply never depended on them agreeing.)
- The resolve step fails **closed** on a missing manifest. Read from
  `workflows_ref`, this repo's documented split-pin state (Dependabot moves
  `uses:` and leaves `workflows_ref:` behind — see
  [`.github/dependabot.yml`](../dependabot.yml)) would become a total groom
  outage. Read from `job.workflow_sha` the case cannot arise: any commit whose
  `groom.yml` reads the manifest also ships it.

`job.workflow_sha`, **not** `github.job_workflow_sha` — the latter is the
spelling everyone reaches for (BE-4169's asset checkouts reached for it, and
BE-8077 moved all seven onto this one) and it expands to an empty string inside a
reusable-workflow job, which Actions does not treat as an error. The populated
accessor is the `job`-context one added in runner v2.334.0 (April 2026). All
groom jobs are `ubuntu-latest`, so it is always available; the resolve step
still re-checks it and emits a `::warning::` if it is ever empty, because the
failure mode is otherwise invisible. (`actionlint` ≤ 1.7.12 flags
`job.workflow_sha` as an undefined property — its `job`-context schema predates
that runner release. It is a false positive, and nothing in this repo's CI runs
actionlint, so nothing gates on it.)

The seven asset checkouts differ from this step in one way: they pair the
fallback with a fail-**closed** `Require a resolvable workflows_ref` guard step
that `::error::`s and exits non-zero when both `inputs.workflows_ref` and
`job.workflow_sha` come back empty. This step warns instead, because a degraded
CLI pin is not worth a groom outage and the pin is still validated; a
default-branch *brief* checkout inside a job holding `ANTHROPIC_API_KEY` is.

That resolve step is the **last** step in `gate` and runs only when
`should_run == 'true'` (as does the checkout that feeds it). It is the one
fail-**closed** step in a job whose every other step fails open, and a scheduled
caller skips ~6 of 7 daily ticks — running it eagerly would let a broken manifest
red out ticks that were never going to install anything. So on a skipped tick the
`claude_code_version` output is empty; that is expected, and unobservable, since
every consumer job is itself gated on `should_run`. A bad pin still cannot reach
`main`, because `tests/test_claude_code_pin.py` runs on any PR touching
`.github/groom/**`.

Two rules the tooling enforces, both because the agent CLI is executable supply
chain for steps that read untrusted repo content:

- **Keep the version exact** — no `^`, `~`, wildcard or dist-tag. The gate step
  fails the run on anything that is not strict SemVer (no leading zeros, no
  component past 2^53-1, non-empty prerelease/build identifiers — anything
  node-semver rejects, npm resolves as a *mutable dist-tag*), and the Dependabot
  entry sets `versioning-strategy: "increase"` so a bump stays exact.
- **Bump deliberately** — a CLI release can rename a flag or shift the default
  permission mode, and groom's sandbox is built out of those flags. Re-validate a
  real groom run (`workflow_dispatch` on `ci-groom.yml`) before merging a bump.

`tests/test_claude_code_pin.py` guards the arrangement: exact pin, no hardcoded
literal anywhere in `groom.yml`, every install step wired to the gate output, and
the Dependabot entry still present.

### Scope: why the top-level pin is the whole pin

Mechanically the pin covers only the **top-level** version — `npm install -g`
writes no lockfile, so nothing in the install command constrains what the package
itself depends on. The reason that is nonetheless the complete boundary is a
property of *this package*, not of the command: as of 2.1.x
`@anthropic-ai/claude-code` declares **no regular, peer or bundled dependencies at
all**, and its only `optionalDependencies` are the eight same-scope
`@anthropic-ai/claude-code-<platform>` binaries, each **exact-pinned to the
identical version** and each a leaf — no dependency fields and no install
lifecycle scripts of its own. Verify with:

```bash
for f in dependencies optionalDependencies peerDependencies bundleDependencies; do
  npm view "@anthropic-ai/claude-code@<pinned>" "$f"
done
```

(One field per call, and read the output by eye rather than with a script.
`npm view` labels fields only when **more than one** is present; when just one is,
it prints that field's map bare and unlabelled, so a reply of
`{"is-number": "^6.0.0"}` is ambiguous about which field answered. The guard below
queries one field per call for the same reason — see `_npm_field`.)

`peerDependencies` and `bundleDependencies` are in that list on purpose, not for
completeness: npm 7+ **auto-installs** peer dependencies, so a floating peer range
floats exactly like a regular one, and `bundleDependencies` ships third-party code
*inside the tarball*, where no registry version spec constrains it at all. A check
that looked only at `dependencies` would leave both doors open.

So the resolved install is fully determined by the pinned version: across the
pinned package and its eight declared platform binaries there is no third-party
code in the tree and no range left to float. Hijacking a "transitive dep" here
would mean compromising the *same publisher* as the top-level package — not a
cheaper attack than compromising the thing we already pinned, which is what makes
the extra machinery a lockfile would buy not worth its cost.

(The top-level package does run a `postinstall`, so this is "the resolved bytes
are pinned", not "nothing executes". Those bytes are covered by the pin like the
rest of the package; that install step is why the CLI is treated as executable
supply chain throughout this section.)

Two residual risks are **accepted**:

- **An npm-registry-level compromise** serving different bytes for an
  already-published, immutable version. A lockfile `integrity` hash would close
  this, and it was still rejected: adding one would turn this deliberately inert
  pin carrier into a real project (see the section above — nothing is ever
  installed from this directory, and a lockfile invites exactly the `npm install`
  that must not happen here), in exchange for a defense against an event that
  compromises effectively all CI everywhere, not just groom.
- **A future version reintroducing floating third-party dependencies.** This one
  is *not* accepted silently — it is guarded.
  `tests/test_claude_code_pin.py`'s `TestPinnedDependencyShape` queries the
  registry for the pinned version and fails if:
  - the top-level `dependencies`, `peerDependencies` or `bundleDependencies` is
    non-empty;
  - any `optionalDependencies` key leaves the `@anthropic-ai/` scope, or any of
    their values is not the exact pinned version string (string equality, not
    range-satisfaction — `^2.1.217` satisfies 2.1.217 today and floats tomorrow);
  - any declared platform binary is no longer a leaf — it declares its own
    `dependencies`, `optionalDependencies` or `peerDependencies`, or runs a
    `preinstall`/`install`/`postinstall` script. (`prepare` is excluded: npm runs
    it for git and local installs, not when unpacking a published tarball, and the
    top-level package already carries one as a publish guard.)

  That second level matters: a depth-1-only guard would rest the whole closure
  claim on an unchecked assumption about the binaries, and a release whose
  platform binary picked up a floating third-party dep would reopen the resolved
  tree with every top-level assertion still green. What the guard does **not**
  reach is anything below those binaries — which is sound only because they are
  verified to be leaves; if that ever stops holding, the guard says so rather than
  quietly narrowing.

  Because a Dependabot bump PR edits `.github/groom/package.json`, it triggers
  this suite — so the guard fires on the one event that can change the pin. A red
  there is not a test to fix: it means the tree stopped being closed, and the
  transitive-pinning decision (spike BE-5580) has to be re-opened before bumping.

  The lookups pin the registry explicitly (`--registry` *and*
  `--@anthropic-ai:registry`, since npm's scoped setting outranks the global one),
  so an `.npmrc` added to the checkout cannot redirect the guard at a registry
  that answers "closed tree"; they retry once so a single blip does not red a PR
  that only touched `ledger.py`; and they open with a positive-control lookup of
  `version`, because empty `npm view` output legitimately means "field absent" and
  would otherwise let an npm that answers *nothing* pass every assertion
  vacuously. `test-groom-scripts.yml` installs Node explicitly so npm is a
  declared dependency of the job rather than an incidental property of the runner
  image; missing npm is therefore a hard failure in CI, and a skip only on a dev
  machine that is simply offline.

The guard is not hypothetical. Versions **1.x through 2.0.0** of this same package
declared floating `@img/sharp-*: ^0.33.5` ranges — third-party, cross-scope, and
range-pinned — under which two installs of the same pinned CLI version could
resolve different bytes. The closed tree is a recent property, so it is checked
rather than assumed.

- **`tests/`** — `unittest` suite, run by
  [`test-groom-scripts.yml`](../workflows/test-groom-scripts.yml). The key-broker
  tests boot the real script under `node` (skipped when `node` is absent).

```bash
python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
```

## The agent sandbox — `agent-sandbox.sh` + `broker.mjs` (BE-4302)

The auto-builder (phase 3) runs an untrusted agent that writes code. These two
trusted assets confine that agent so a prompt-injected or misbehaving run cannot
read the runner's secrets, touch anything outside its clone, or exfiltrate the
API key — while still letting it edit its worktree and reach Anthropic.

- **[`agent-sandbox.sh`](agent-sandbox.sh)** — a [bubblewrap](https://github.com/containers/bubblewrap)
  (`bwrap`) wrapper that runs an arbitrary command inside an unprivileged jail:

  ```bash
  agent-sandbox.sh --clone <path> --clone-mode ro|rw-git-ro --out-dir <path> \
      [--ro-file <path> ...] [--env KEY=VALUE ...] [--uds <host-socket-path>] \
      -- <command...>
  ```

- **[`broker.mjs`](broker.mjs)** — a ~50-line node-stdlib reverse proxy
  (`node broker.mjs <port|socket-path>`) that holds the real key on the host and
  forwards the jail's requests to it. In socket mode it listens on a unix-domain
  socket (bind-mounted into the jail via `--uds`); the legacy TCP port mode is
  retained for the test plumbing and back-compat.
- **[`jail-shim.mjs`](jail-shim.mjs)** — a ~20-line node-stdlib TCP→UDS forwarder
  (`node jail-shim.mjs <port> /run/broker.sock`) that runs **inside** the jail so
  agent tooling speaking HTTP to a `127.0.0.1:<port>` base URL reaches the broker's
  bind-mounted socket (the isolated netns has no way to dial a host TCP port).

### The sandbox contract (what the agent can and cannot see)

| Surface | Inside the jail |
|---|---|
| `/usr`, `/etc` | read-only |
| `/tmp`, `/home/agent` (`HOME`) | fresh tmpfs — host `/tmp` is **shadowed**, not shared |
| the clone (`--clone`) | bound **at its real path**; `ro` = read-only, `rw-git-ro` = worktree writable but `.git` read-only |
| explicit `--ro-file`s | read-only, at their real paths |
| the out-dir (`--out-dir`) | the **only** writable host location (created on the host first) |
| host `$HOME` / `$RUNNER_TEMP` / `$GITHUB_WORKSPACE` / other repos | **invisible** |
| host process table | **invisible** (own pid namespace) |
| environment | **cleared** — only `HOME`, `PATH`, `TERM`, and each `--env KEY=VALUE`; nothing inherited from the host |
| network | **isolated network namespace** (loopback only) — host network, host loopback, and cloud metadata are all **unreachable**; the broker is reached via a unix socket bind-mounted at `/run/broker.sock` plus the in-jail `jail-shim.mjs` TCP forwarder |

The `rw-git-ro` worktree write is exactly how the builder's patch is captured: the
agent edits tracked files, the wrapper's caller reads them back on the host
afterward, but the agent can never rewrite git history or `.git/config`.

**The real API key never enters the jail.** The broker reads
`ANTHROPIC_API_KEY` from *its own* (host) environment, **deletes** any inbound
`x-api-key` / `authorization` header, injects the real key, and forwards only
`/v1/*` paths to `api.anthropic.com` — streaming the response through unbuffered
so SSE works. `GET /healthz` answers locally; anything not under `/v1/` is `404`.
It listens on a unix-domain socket (`--uds`, the phase-2 default) or `127.0.0.1`
(legacy TCP mode), refuses to start with an empty key or a relative socket path,
and logs one line per request — method + path + status, never headers or body. The
request-handling contract is identical on both transports.

**No network egress (BE-4421).** The jail runs in an isolated network namespace
with only loopback up, so the broker — reached over the unix socket bind-mounted
at `/run/broker.sock` via the in-jail `jail-shim.mjs` TCP→UDS forwarder — is the
*only* thing the agent can talk to. Host network, host loopback services, and
cloud metadata (`169.254.169.254` / `168.63.129.16`) are all unreachable. Two
consequences for callers: set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` in the
agent env so the agent doesn't stall on telemetry endpoints that can never be
reached; and because there is no egress, in-jail `git fetch` / `npm install`
cannot work — anything the agent needs must already be in the clone before it is
sandboxed.

### The loud-preflight guarantee

Before it runs anything, `agent-sandbox.sh` **proves the sandbox works or exits
non-zero** — it will **never** fall back to running the command unsandboxed. The
preflight installs `bubblewrap` if missing, installs an unconfined AppArmor
profile for `bwrap` when the runner sets
`kernel.apparmor_restrict_unprivileged_userns=1` (mirroring the runner image's own
podman workaround), and self-tests a real `bwrap` invocation. If that still fails
it drops the userns restriction and retests; if it *still* fails it emits
`::error::bwrap sandbox unavailable …` and exits non-zero. A broken sandbox stops
the run — it never silently degrades to no sandbox.

### Tests — deterministic, no API spend

[`tests/sandbox-tests.sh`](tests/sandbox-tests.sh) (run by the `sandbox-tests` job
in [`test-groom-scripts.yml`](../workflows/test-groom-scripts.yml)) asserts every
row of the contract above with `bash -c` as the sandboxed command — env scrub, FS
confinement + tmpfs shadowing, both clone modes, pid isolation — and points the
broker at a local fake upstream ([`tests/fake-upstream.mjs`](tests/fake-upstream.mjs))
*over the bind-mounted unix socket + in-jail `jail-shim.mjs`* to prove key
injection/stripping, the `/healthz` + non-`/v1` behavior, and SSE pass-through. It
also proves the BE-4369 egress isolation: host loopback, cloud metadata, and an
arbitrary external IP are all unreachable from the jail. No `claude`, no API key,
no spend.

```bash
shellcheck -x .github/groom/agent-sandbox.sh .github/groom/tests/sandbox-tests.sh
bash .github/groom/tests/sandbox-tests.sh   # Linux + unprivileged userns only
```

## `patch_policy.py` — the CI-privileged patch deny-list (BE-4404)

The auto-builder's `Capture patch` step must never open an auto-PR whose patch
touches a path the caller's CI *executes* before a human reviews the merge —
that would run builder-authored (untrusted) code with repository secrets. The
patterns that decide this were an untestable inline `grep -E`; `patch_policy.py`
extracts them so they carry a unit-test suite, and closes the biggest live gap
(dependency **lockfiles** — `npm ci` re-resolves and runs their tarballs' install
scripts) plus `.husky/`, composite-action manifests, `.gitmodules`, and the
common build files across the JS/Python/Rust/Ruby/Swift/Go/Gradle/Bazel/CMake
ecosystems. Matching is **case-insensitive** — macOS/Windows CI runners resolve
`PACKAGE.JSON` to the real file, so the Linux checker must too.

- `denied_paths(paths)` returns the subset of changed paths a human must author
  — CI-privileged **and** dataset-of-record, undifferentiated (the gate only
  tests non-emptiness). Do not read membership as "executes in pre-review CI".
  `denied_entries(entries)` wraps it for `(old_mode, new_mode, path)` raw-diff
  entries, adding the symlink-mode deny described below.
- `main()` reads raw diff records from stdin (matching `git diff --cached
  --no-renames --raw -z`) and prints the denied paths, **exit 0 always** — the
  caller tests non-emptiness. Each producer flag is load-bearing. `-z`: git
  C-quotes exotic paths in its default output, slipping them past the anchors,
  while `-z` emits raw bytes. `--no-renames`: with rename detection on, a rename
  reports only its DESTINATION pairing, so a patch MOVING a denied path out to
  an undenied one would show the policy nothing. `--raw` (not `--name-only`):
  the raw records carry file MODE bits, which is how `denied_entries` sees
  symlinks — path shape alone cannot.
- The list is a conservative **default, not a proof of completeness** — over-block
  is safe (a false positive only downgrades a PR to an issue), under-block is the
  hole. A repo whose CI runs something else privileged must add it here first.
- It also denies **owner-gated dataset-of-record paths** (BE-9609) — `.yml`/`.yaml`
  files at any depth under a `suites/**/cases/` tree (`**` spanning zero or more
  segments, so a flat `suites/cases/` layout is inside the surface), plus any
  change whose final path segment is `cases` under a suite (git tracks no
  directories, so that shape is a file or a symlink), plus — by MODE, via the
  `--raw` producer — any symlink-typed change carrying a `suites` segment: a
  link at any other component the importer's glob traverses (`suites` itself, a
  suite dir, a non-YAML name inside `cases/`) would silently redirect resolution
  to an undenied tree. One residual stays open by construction: the policy sees
  only the builder's diff, so a **pre-existing, human-authored** symlink into an
  outside tree already extends the importable surface, and a builder file added
  under that target tree matches nothing — a caller whose dataset surface
  extends beyond literal `suites/` paths must extend the list (the conservative
  default rule below). Their merge publishes immutable case versions reserved for the
  dataset owner. This is the one entry with **no CI-execution justification**, and
  it is currently hardcoded rather than caller-gated: a caller with an unrelated
  `…/suites/<x>/cases/*.yaml` fixture tree inherits the deny with no opt-out, and
  because a path bail is deterministic it recurs every run and re-spends a
  `max_prs` slot. Over-block is still the safe direction here (the finding is
  filed as an issue, never dropped) — but if a second consumer needs its own path
  family, make the class a caller input instead of extending this tuple.

```bash
python3 -m unittest discover -s .github/groom/tests -p test_patch_policy.py -v
```
