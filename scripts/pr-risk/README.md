# pr-risk — advisory PR risk grading (the shadow check)

The scripts behind [`pr-risk.yml`](../../.github/workflows/pr-risk.yml). Every PR
event is graded into a tier and gets ONE label:

| tier | label (default) | meaning | eventual routing (later phases — nothing routes today) |
|---|---|---|---|
| R0 | `risk:R0` | inert — docs, tests, provably-shaped runbook output | auto-merge candidate |
| R1 | `risk:R1` | contained — bounded, covered, revertable in one click | rubber-stamp |
| R2 | `risk:R2` | standard — ordinary product code | normal review |
| R3 | `risk:R3` | elevated — auth, billing, migrations, IaC, CI, deps, secrets | owner + e2e |
| — | `risk:ungraded` | an input could not be read; deliberately NOT a tier | human review |

**The label is the entire product.** Nothing is gated, blocked, routed, commented
on, or merged. Humans glance at the label and either agree or disagree.
Disagree by adding the `risk-dispute` label (never touched by the grader) plus a
comment saying why — disputes are the pilot's calibration data.

## How a grade is computed

`grade = worst(path_floor, provenance, reversibility)` — three deterministic
axes, worst wins, so no axis can move a PR into a safer lane than another axis
put it. No LLM anywhere; the whole thing is `gh` + `jq` over the PR's API
record.

1. **Path floor** — [`risk-map.v0.json`](risk-map.v0.json): versioned path-glob
   rules. The floor is the worst tier over every rule any changed path matches,
   so a docs file can never cancel a migration in the same PR. Matching covers
   every path the diff touches, **destination and origin** — a renamed file is
   graded under its previous path too, so `git mv auth/x.go misc/x.go` cannot
   walk a file out of the rule that guards it. In globs `**` crosses `/` and `*`
   does not, and matching is whole-path anchored: a rule without a leading `**/`
   matches root-level files ONLY.
2. **Provenance** — what PROCESS produced the diff: `runbook` (a registered
   producer in [`runbook-registry.v0.json`](runbook-registry.v0.json) whose
   identity AND diff shape both assert), `agent-supervised`, `human`, or
   `external` (any fork, or a first-time **human** contributor — R3, no
   exceptions, even when a runbook shape matches). Identity is the
   server-attributed author login, never the forgeable commit author string.
   A **non-fork bot is never `external`**, even though every GitHub App authors
   with `author_association: NONE` — it is a runbook candidate, judged by the
   registry's shape assertion, and falls back to `human` when no entry asserts.
   "Is this a bot?" is answered from GitHub's own actor type (`Bot`) plus the
   `bot_logins` list, so an App needs no list entry and a machine *user* account
   does. The fork half is unconditional, so a bot opening a PR *from a fork* is
   still `external` R3.
3. **Reversibility** — mutates persistent state or deletes data → R3; **removes**
   a file under a sensitive class → R3 (a delete, or a rename out of that class);
   no green check rollup → R2; green but no test file touched → R1; green with
   tests touched → R0. "Green" means at least one check actually CONCLUDED
   success: a rollup of nothing but skipped/neutral answers "did tests covering
   these lines run?" with nothing, so it cannot drop the axis below R2. What
   counts as a test file is `reversibility.test_path_patterns` in the map (omit
   the key and the grader falls back to a built-in regex that only knows the
   Go/TS shapes). The axis also records `files` — which changed paths actually
   supplied the tier — on the two rungs that are attributable to specific files
   (an irreversible class, or a removal under a sensitive class), and `null` on
   the other three: "no green rollup" is a property of the head commit and "no
   test touched" of the whole change set, so neither is removable by dropping
   files, and `null` says exactly that rather than "attributable to nothing".
   When a PR trips *both* attributable rungs the list is their **union**, not
   the first-match one the `reason` names, so peeling every path it lists clears
   *every* attributable rung. Unlike `path_floor.files` (rows), it is a
   flat array of destination path **strings**, directly comparable against
   `path_floor.files[].path`. Beside it, `residual_tier` says **where the axis
   lands once those paths are peeled** — the half `files` cannot answer on its
   own, because the three rungs the remainder falls back to are map-configurable
   and a consumer that sets `no_green_checks_tier: "R3"` puts the remainder
   straight back on R3. It is peel-set-independent: the "no green rollup" rung
   tests the head commit, which no peel can change, so a non-green rollup pins
   the residual at `no_green_checks_tier` exactly, and a green one bounds it by
   the worse of the two rungs below. It is `null` exactly when `files` is.
   Like the per-file path floors, both are **reporting only** — derived from the
   tier decision and read nowhere else, so neither can ever move a tier.

Anything unreadable grades `unknown` (labeled `risk:ungraded`), never a
confident tier, and never "the axes that did resolve" — a PR whose file list we
could not read is exactly the PR that might touch auth.

Two CI-specific mechanics worth knowing:

- **The grading run excludes itself from the check rollup it reads** (its own
  check is always in-flight at grade time), and the job re-polls until the rest
  of the rollup settles or `wait_for_checks_minutes` runs out — otherwise every
  live grade would floor at R2 as an artifact of the measurement. Exclusion is
  keyed on `github.run_id` (`--self-run-id`), and a **FAILING check is never
  excluded**: self-exclusion may only ever hide our own pending run, never a red
  one. Enroll pr-risk as its **own workflow** rather than a job inside an
  existing CI workflow — a job sharing a run with the rest of CI excludes its
  siblings too, and lands on the honest R2 floor instead of a full rollup.
- **A caller grants the UNION of every job's `permissions:`, including jobs it
  will never run.** GitHub validates each nested job's *declaration* against the
  caller's block at startup, before any job `if:` is evaluated, so `checks:
  write` — declared by the `publish-check` job — is required of every caller
  whatever `check_run` is set to. Leaving `check_run` false skips the job and
  publishes nothing; it does not excuse the grant. **Moving an existing pin onto
  a version that added a job? Add its permission in the same PR** — a bump alone
  fails the caller's next run at startup with an opaque "workflow file issue".
  The copy-paste block lives in [`pr-risk.yml`](../../.github/workflows/pr-risk.yml)'s header.
- **The label is applied with the plain `GITHUB_TOKEN`**, which cannot fire
  `labeled` triggers — the shadow check is structurally unable to start a
  workflow cascade. Later phases that WANT label-triggered routing switch to an
  app token deliberately.

## Grading on demand (`pr_number` / `pr_numbers`)

Every grade above is triggered by a `pull_request` event. Two things need a grade
with no event: a repo that **enrolls mid-stream** and wants the open queue it
already has labeled, and a **manual re-grade** after a `.github/risk.json` change
or on a PR carrying `risk-dispute`. Both are a `workflow_dispatch` on the
consumer's caller, forwarding a number:

```yaml
on:
  workflow_dispatch:
    inputs:
      pr_number: { required: false }    # one PR
      pr_numbers: { required: false }   # 12,15,20 — wins over pr_number
```

With neither supplied the workflow reads the event exactly as before. The grading
logic is unchanged either way — it was always an API read keyed on a PR number,
never a read of the event payload. What the number-supplied path changes is only
which PR is read, plus three consequences worth knowing:

- **Bot-authored and fork PRs are graded here.** The `github.actor != 'dependabot[bot]'`
  and `head.repo.full_name == github.repository` clauses in the caller's `if:`
  are **token** guards — a bot's or a fork's `pull_request` run gets a read-only
  `GITHUB_TOKEN`, so the label write would 403 and the check would go red on
  every dependency bump. On a dispatch the token is writable and the actor is a
  human, so neither applies. Fork **risk** is untouched: `external` comes from
  the API's own fork flag, never from the actor, and still grades R3.
- **Both clauses must be scoped to the event, or the dispatch is a silent
  no-op.** There is no `github.event.pull_request` on a `workflow_dispatch`, so
  an unscoped fork clause is false and the job skips with no run, no error and no
  annotation. The same applies to the concurrency group: an event-only key
  collapses to one constant group, and with `cancel-in-progress` a batch has each
  dispatch cancel the one before it. The workflow header carries a copy-paste
  block that is correct on both event shapes — use it rather than reconstructing
  one, and note that a caller-side skip is undetectable from inside the reusable.
- **A low `wait_for_checks_minutes` is right for a backfill, but `0` is not.**
  The wait exists to outlast the rest of the rollup while the grading run's own
  check sits in it. A dispatched run's check attaches to the dispatched ref, not
  to the PR's head commit, so it is not in that rollup at all and a settled PR
  reads its true state on the first poll. `0` still breaks out after a single
  read, ahead of the "require a settled reading to repeat" confirmation, so a
  target someone pushed to minutes ago lands the honest R2 floor. `1` costs one
  15s backoff per PR and keeps the confirmation.

A batch grades one target at a time and **one unreadable PR is reported without
abandoning the rest** — the whole list is attempted, then the run reports whether
any target failed. The list is explicit and capped (50) rather than an
`all_open: true` that would be unbounded, and a target the job's time budget
cannot reach is reported by number as *not attempted* rather than started and cut
off. The base ref is re-read **per target** and an unresolvable one fails that
target: the ref selects which branch's `.github/risk.json` judges the PR, live
PRs are commonly stacked on feature branches rather than the default branch, and
an empty `?ref=` is not an error to the contents API — it silently resolves to
the default branch. That is also why the ref is percent-encoded into the request
rather than interpolated raw: `#`, `&` and `+` are all legal in a branch name,
and a raw `#` truncates the URL into exactly that empty-ref read. A 404 for the
**ref** ("no commit found for the ref" — a deleted or renamed base branch) is
distinguished from a 404 for the **file** and fails the target instead of
falling back to the generic map.

Operational caveats for a backfill:

- **A batch cannot serialize per-PR.** One run covers N pull requests and a run
  belongs to exactly one concurrency group, so a batch overlapping a
  `pull_request` run for one of its own numbers will interleave with it. The
  label sync is a single atomic `PUT` of the PR's whole label set, so that
  interleaving cannot leave the PR carrying two `risk:*` labels: the two writers
  end last-writer-wins with exactly one — possibly the staler tier, which gates
  nothing meanwhile and which the next grade re-syncs, with the caveat that on
  the *last* push to a PR there is no next grade (a superseded run's PUT can
  still land after the winner's, since cancellation is not instantaneous), so
  re-dispatch on `pr_number` if a final grade looks wrong. The residual it costs
  instead is narrower, but it cuts both ways: the PUT is built from a snapshot
  read, so a **non-owned** label added in the read→PUT window is dropped
  (`risk-dispute` included — re-add a dispute that lands in that instant) and one
  **removed** in that window is resurrected. The window opens only on a run that
  actually changes the grade, and is about one API round-trip — three on the
  first grade in a repo, where the label pre-create sits inside it. A drop is not
  invisible: GitHub records it on the PR timeline as an `unlabeled` event by the
  grader token. Dispatch when the queue is quiet, and use `pr_number` when you
  want the per-PR group to serialize a re-grade against event runs.
- **Remapping `label_map` orphans the old names.** Ownership is defined by the
  *current* map, so labels applied under a previous one are no longer owned:
  they ride through every future PUT beside the new target and no re-grade will
  clear them. Delete the retired label names repo-side once, as part of the
  remap.
- **The pre-grader reads retry.** Rate limits are global, not per-PR, so the
  base-ref and override reads — the first hop for every target — retry a
  transient failure with backoff, as the grader already does. Without it one
  secondary-rate-limit burst mid-backfill failed every remaining target at once.
  A definitive answer (404, 401, 422) is never retried.

## Per-repo overrides (read from the base ref)

The shipped map and registry are deliberately generic. A consumer repo sharpens
them by committing:

- `.github/risk.json` — the repo's own path→tier map (same schema as
  [`risk-map.v0.json`](risk-map.v0.json))
- `.github/risk-runbooks.json` — the repo's own producer registry (same schema
  as [`runbook-registry.v0.json`](runbook-registry.v0.json)). **Name a GitHub App
  by its suffixed login** — `"my-bot[bot]"`, the form REST and the web UI show
  you. The grader reads the PR over GraphQL, which reports a Bot login
  **unsuffixed**, and restores the suffix before matching — one way only, and
  only for an author GitHub's own actor type calls a `Bot`. A bare slug
  (`"my-bot"`) still matches literally, because a machine **user** account has no
  suffix and needs it; but a bare slug is equally matchable by a same-named
  *human*, who would then inherit that runbook — so never name an App with one.

Both are read from the PR's **base ref**, so a PR cannot edit the rules that
judge it (editing them — or the grader — at all is R3 by the map's own first
rule). A genuine 404 falls back to the shipped defaults; a present-but-invalid
file fails the run loudly rather than silently grading generic, and so does any
non-404 read failure (a 403 rate-limit or 5xx must not quietly demote the PR to
the generic map, which would be a lower tier computed from an input nobody read).

A map must MAP every provenance class (`runbook`, `agent-supervised`, `human`,
`external`). Omitting one is refused at load time rather than filled in with a
tier nobody chose — that is how a map that forgot `external` used to grade a fork
the same as a teammate.

Every graded record carries `map_version` + `registry_version`, so grades made
under different maps stay comparable and a map revision can be replayed against
accumulated records.

## Relabeling (R0–R3 vs R1–R4 and friends)

Tier SEMANTICS are fixed (R0 safest .. R3 riskiest, `unknown` separate)
everywhere records are stored. The label TEXT is the caller's, via `label_map`:

```yaml
with:
  label_map: "R0=risk:R1,R1=risk:R2,R2=risk:R3,R3=risk:R4,unknown=risk:ungraded"
```

Labels are created on first use, color-coded green → red (gray for ungraded).

## Files

- `grade-pr-risk.sh` — the grader. `--pr N --repo o/r` grades a live PR;
  `--stdin` grades synthetic records (the no-network test surface). Extracted
  from the fleet's offline corpus grader (BE-5507); the identity jq is inlined
  from its collector (BE-5030) — keep the two in sync when either changes. The
  changed-file list comes from REST `pulls/{n}/files`, not the GraphQL `files`
  connection: GraphQL has no previous-path field (so renames are invisible) and
  its connection capped the list at 100, which put exactly the PRs a risk grade
  helps most in the ungraded lane. A read that comes back short of `changedFiles`
  is still `unknown`.
- `publish-risk-surfaces.sh` — the two **opt-in** publish surfaces (`sticky_comment:` /
  `check_run:`, both `false` by default): the sticky PR comment and the Check Run render. The
  comment is deliberately **one line above the fold** — tier, the axis that decided it, its
  reason, plus a concentration fragment when the path axis is what decided — with the formula,
  the per-axis table, the per-file breakdown and the caveats inside a collapsed `<details>` and
  the dispute checkbox below it. It is created once and updated in place. The full concentration
  sentence also carries a **reducibility readout**: when the path axis is what decided the tier and
  some of the diff sits below the floor, it names what the below-floor remainder would path-floor
  at on its own — "…peeled into their own PR, the remaining 3 file(s) would path-floor at **R2**
  (final grade still depends on the provenance and reversibility axes at PR time)". That complement
  floor is the worst per-file floor over exactly the files the share sentence already counts as
  below-floor, computed from the grader's own per-file floors rather than estimated, and it is what
  tells a split worth doing (the remainder lands in a cheap lane) from one that is not (it is still
  a normal review). It is deliberately a **floor with its assumptions named, never a promised
  grade**, and deliberately **not clamped to the other two axes**: both are re-derived for the
  split PR and can move either way — a narrower path set may newly assert a runbook (provenance
  improves) or newly fail one, and reversibility keys on the remainder's own classes and on checks
  that have not run yet — so clamping would print a number that is not a floor either. It is
  silent where a split cannot help: an irreducible diff (every line already at the floor), any
  grade the path axis did not decide, and a remainder that rounds to **0%** of the diff, where the
  sentence would otherwise pitch relocating a line it just called nothing. A *tie* — another axis
  proposing the same tier as the path floor — is read **per axis**, because the two ties do not
  mean the same thing. A **provenance** tie always suppresses: provenance is a property of the
  author, so it follows the remainder into the split PR and peeling the top files changes nothing.
  A **reversibility** tie is a property of specific *files*, and those may be exactly the files the
  clause proposes peeling — so it suppresses **unless** two things hold at once.
  `axes.reversibility.files` (the paths that supplied the reversibility tier) must be a non-empty
  array **every** entry of which is already in the peeled set, *and* `axes.reversibility
  .residual_tier` (where the axis lands after that peel) must rank strictly below the headline.
  Only then does one split provably remove both reasons, and the clause speaks.
  Both halves are load-bearing rather than formalities, and a consumer map override defeats each
  one separately. Against the subset test: remap `migrations` to R1 while leaving it in
  `irreversible_classes` and the irreversible-class file sits *below* the path floor, so peeling
  the top files leaves the reversibility reason exactly where it was. Against the residual test:
  set `no_green_checks_tier: "R3"` and peeling every attributed path drops the remainder onto a
  rung that is R3 all over again — the attributed reason went with the peel, but the tier did not
  move, so the reduction the clause would promise cannot happen.
  Either field absent or `null` — the R2/R1 rungs, where the reason is a property of the
  head commit or of the whole change set, and every record graded before those fields existed —
  reads as *not* removable, failing safe back to suppression. When a removable reversibility tie
  does let the clause speak, the above-the-fold headline names the driver **`path and
  reversibility`** and carries reversibility's reason, so the split pitch inside the `<details>` is
  never sitting under a headline crediting reversibility alone — and it is bound to the *same*
  predicate the pitch itself is, so a record that pitches no split (an irreducible diff, a
  remainder that rounds to 0%) keeps the plain single-axis headline it always had.
  The caveat stays attached and stays honest in that case:
  the remainder re-derives reversibility from its own classes and its own checks.
  The long form lives in
  the Check Run instead, because this comment lands on PRs that already carry CodeRabbit and an
  8-cell review panel, and an advisory grade nothing routes on has the weakest claim on the
  reader's scroll. Every PR-controlled string it renders is escaped here — a filename may
  legally contain `|`, a backtick or a newline, and raw it could break out of its table row and
  forge a pre-ticked dispute checkbox that the next re-grade reads back as a real reviewer
  disagreement. The body is bounded under GitHub's 65536-char comment limit by construction, and
  no failure in it can redden a PR. `RENDER_ONLY=1` emits the surfaces and writes nothing, which
  is how the Check Run is rendered in the grading job and POSTed from the job that holds
  `checks: write`.
- `grade-targets.sh` — the orchestration layer, extracted from `pr-risk.yml`'s
  inline job body so the event path and the by-number path cannot drift into two
  copies of it. Per target: resolve the base ref, fetch that ref's override
  files, poll the grader until the rest of the rollup settles, sync the one
  label. Grades one target or a list through the same code, records each
  outcome, and never lets one bad target abandon the rest. It computes nothing
  about risk.
- `apply-risk-label.sh` — the one write, and it is literally one request: a
  single atomic `PUT` of the PR's full label set — every label the script does
  not own, carried through verbatim from the snapshot read, plus the computed
  one. Owns exactly the five mapped labels (matched case-insensitively, as GitHub
  label identity is), so a PR it has written to carries exactly one of them even
  when two grading runs race. An already-in-sync PR writes nothing at all.
- `risk-map.v0.json` / `runbook-registry.v0.json` — the generic defaults.
- `tests/` — hermetic suites (synthetic records + a stubbed `gh`); run via
  [`test-pr-risk.yml`](../../.github/workflows/test-pr-risk.yml).

## What is deliberately NOT here

No auto-merge, no routing, no required check, no PR comment, no LLM judgement,
no linked-ticket requirement. Those are later rungs of the ladder and each one
is its own explicit switch — this workflow exists to accumulate the
agree/disagree evidence that decides whether any of them turn on.
