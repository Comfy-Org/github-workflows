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
   `external` (fork / first-time contributor — R3, no exceptions, even when a
   runbook shape matches). Identity is the server-attributed author login,
   never the forgeable commit author string.
3. **Reversibility** — mutates persistent state or deletes data → R3; **removes**
   a file under a sensitive class → R3 (a delete, or a rename out of that class);
   no green check rollup → R2; green but no test file touched → R1; green with
   tests touched → R0. "Green" means at least one check actually CONCLUDED
   success: a rollup of nothing but skipped/neutral answers "did tests covering
   these lines run?" with nothing, so it cannot drop the axis below R2. What
   counts as a test file is `reversibility.test_path_patterns` in the map (omit
   the key and the grader falls back to a built-in regex that only knows the
   Go/TS shapes).

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
- **The label is applied with the plain `GITHUB_TOKEN`**, which cannot fire
  `labeled` triggers — the shadow check is structurally unable to start a
  workflow cascade. Later phases that WANT label-triggered routing switch to an
  app token deliberately.

## Per-repo overrides (read from the base ref)

The shipped map and registry are deliberately generic. A consumer repo sharpens
them by committing:

- `.github/risk.json` — the repo's own path→tier map (same schema as
  [`risk-map.v0.json`](risk-map.v0.json))
- `.github/risk-runbooks.json` — the repo's own producer registry (same schema
  as [`runbook-registry.v0.json`](runbook-registry.v0.json))

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
- `apply-risk-label.sh` — the one write. Owns exactly the five mapped labels:
  removes stale ones, applies the computed one, touches nothing else.
- `risk-map.v0.json` / `runbook-registry.v0.json` — the generic defaults.
- `tests/` — hermetic suites (synthetic records + a stubbed `gh`); run via
  [`test-pr-risk.yml`](../../.github/workflows/test-pr-risk.yml).

## What is deliberately NOT here

No auto-merge, no routing, no required check, no PR comment, no LLM judgement,
no linked-ticket requirement. Those are later rungs of the ladder and each one
is its own explicit switch — this workflow exists to accumulate the
agree/disagree evidence that decides whether any of them turn on.
