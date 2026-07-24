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
- **`signature`** is a stable dedup key (`<repo-basename>:<scope>:<slug>`) whose
  `<slug>` is derived **deterministically** from the finding's core subject, so it
  stays identical across re-runs of the same finding and a consumer never re-files
  a finding it has already seen.

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
```

- **`tests/`** — `unittest` suite, run by
  [`test-groom-scripts.yml`](../workflows/test-groom-scripts.yml).

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
      [--ro-file <path> ...] [--env KEY=VALUE ...] -- <command...>
  ```

- **[`broker.mjs`](broker.mjs)** — a ~50-line node-stdlib reverse proxy
  (`node broker.mjs <port>`) that holds the real key on the host and forwards the
  jail's requests to it.

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
| network | shared (so the agent can reach the broker on loopback) |

The `rw-git-ro` worktree write is exactly how the builder's patch is captured: the
agent edits tracked files, the wrapper's caller reads them back on the host
afterward, but the agent can never rewrite git history or `.git/config`.

**The real API key never enters the jail.** The broker reads
`ANTHROPIC_API_KEY` from *its own* (host) environment, **deletes** any inbound
`x-api-key` / `authorization` header, injects the real key, and forwards only
`/v1/*` paths to `api.anthropic.com` — streaming the response through unbuffered
so SSE works. `GET /healthz` answers locally; anything not under `/v1/` is `404`.
It listens on `127.0.0.1` only, refuses to start with an empty key, and logs one
line per request — method + path + status, never headers or body.

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
to prove key injection/stripping, the `/healthz` + non-`/v1` behavior, and SSE
pass-through. No `claude`, no API key, no spend.

```bash
shellcheck -x .github/groom/agent-sandbox.sh .github/groom/tests/sandbox-tests.sh
bash .github/groom/tests/sandbox-tests.sh   # Linux + unprivileged userns only
```
