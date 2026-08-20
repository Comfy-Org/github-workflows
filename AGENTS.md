# AGENTS.md — Comfy-Org/github-workflows

Shared, versioned **reusable GitHub Actions workflows** for use across Comfy-Org
repositories. This repo is **public** so any repo — public or private, inside or
outside the org — can call these workflows. Each reusable workflow's logic
(prompts, Python/shell scripts) lives *here* as the single source of truth;
consumer repos carry only a thin caller that pins this repo by full commit SHA.
There is no application to build or run — the deliverable is the workflows
themselves plus the scripts that back them.

## Commands

Python is stdlib-only, with ONE exception — `.github/coderabbit-config/`, which
needs a YAML parser and a real JSON Schema validator and pins both in
`requirements.txt` (exact versions + sha256, installed `--require-hashes`).
CI uses **Python 3.12**. Run from the repo root. Each command mirrors a CI job (see `.github/workflows/test-*.yml`):

```bash
# cursor-review helper-script tests (extract-findings, post-review)
python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py' -v

# agents-md-integrity checker tests
python3 -m unittest discover -s .github/agents-md-integrity/tests -p 'test_*.py' -v

# groom dedup/rejection ledger tests
python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v

# refresh-reviewers generator tests
python3 -m unittest discover -s .github/refresh-reviewers/tests -p 'test_*.py' -v

# bump-callers shell tests + lint (gh is stubbed; no network)
shellcheck -x .github/bump-callers/bump-callers.sh .github/bump-callers/preflight.sh .github/bump-callers/tests/test_bump_callers.sh .github/bump-callers/tests/test_preflight.sh .github/bump-callers/tests/test_paths_contract.sh
bash .github/bump-callers/tests/test_bump_callers.sh
bash .github/bump-callers/tests/test_preflight.sh
bash .github/bump-callers/tests/test_paths_contract.sh

# pr-derisk planner/renderer suite + lint (model stubbed; no network, no API key)
shellcheck -x scripts/pr-derisk/*.sh scripts/pr-derisk/tests/*.sh
bash scripts/pr-derisk/tests/test_plan_derisk.sh

# workflow-pins lint (no reusable workflow may default `workflows_ref`) + its tests
python3 -m unittest discover -s .github/workflow-pins/tests -p 'test_*.py' && python3 .github/workflow-pins/check_workflow_pins.py

# coderabbit-config validator tests (needs the pinned deps below; venv recommended)
python3 -m pip install --require-hashes --only-binary=:all: -r .github/coderabbit-config/requirements.txt
python3 -m unittest discover -s .github/coderabbit-config/tests -p 'test_*.py' -v

# AGENTS.md integrity checker against any repo tree
python3 .github/agents-md-integrity/check_agents_md.py --root .
```

There is no repo-wide formatter/linter config (no ruff/black/pyproject,
no pre-commit). Shell is linted by `shellcheck` in CI; Python is guarded by the
`unittest` suites above. Every test workflow is **path-filtered**, so a change
that touches only the files under a given directory runs only that directory's
tests — run the matching command above for whatever you touched.

## Layout

- `.github/workflows/` — the reusable workflows (`on: workflow_call`) plus this
  repo's own CI callers (`ci-*.yml`) and the `test-*.yml` script tests.
- `.github/cursor-review/` — prompts + scripts behind `cursor-review.yml`
  (the multi-model review panel + judge). Single source of truth; loaded at run
  time, never copied into consumers. Also holds `catalog-drift.py`, the
  comparison logic behind the weekly `cursor-review-catalog-drift.yml` check
  (BE-4819) — it reads the pins *out of* `cursor-review.yml`, so never
  duplicate the model list. Tests in `tests/`.
- `.github/agents-md-integrity/` — `check_agents_md.py`, the checker behind
  `agents-md-integrity.yml` (enforces this AGENTS.md standard). Tests in `tests/`.
- `.github/groom/` — briefs + building blocks behind the reusable **groom**
  code-cleanup workflow (`groom.yml`, epic BE-3870): `finder.md` / `verifier.md`
  / `builder.md` (the phase-1/2/3 prompts, single source of truth, loaded at run
  time), `ledger.py`, the durable dedup/rejection memory that stops the
  stateless groom CI run from re-filing already-filed or human-rejected findings
  — and (BE-4003) recognizes auto-builder PR state (open/merged/closed) so a
  built finding is never re-proposed — and `interval.py`, the runtime cadence
  gate (`GROOM_INTERVAL_DAYS`) that early-exits a daily tick unless the interval
  has elapsed since the last real run (derived from Actions run history — no new
  secret) — and `scope.py` (BE-4757), the `path` input's enforcement: validate +
  contain the path, hand the finder the in-scope file list, post-filter
  out-of-scope findings. The cadence clock is PER SCOPE (a scoped run never
  stamps "done" over the whole-repo audit, and a permanently scoped caller still
  gets its own cadence); dedup signatures ignore `path`. Also `package.json`
  (BE-5373) — not a project, no lockfile, nothing is installed from it: it is the
  one Dependabot-visible home of the `@anthropic-ai/claude-code` pin, which
  `groom.yml`'s gate reads once and feeds to all three agent jobs. Keep it exact;
  never re-hardcode a version in a `run:` step. Tests in `tests/`.
- `.github/coderabbit-config/` — `check_coderabbit_config.py` + the VENDORED
  `schema.v2.json` behind `coderabbit-config-validate.yml`, plus `schema_drift.py`
  (the comparison behind the weekly `refresh-coderabbit-schema.yml`). The schema is
  vendored, never fetched at validation time: a live fetch makes every consumer's
  CI depend on a third party, and an upstream tightening would redden the fleet with
  no change here. `requirements.txt` is the repo's only Python dependency set — see
  Commands. Tests in `tests/`.
- `.github/refresh-reviewers/` — `generate.py`, the engine behind
  `refresh-reviewers.yml`: recomputes a caller's reviewers.yml from git history
  and rewrites just the reviewer lists for a drift PR. Tests in `tests/`.
- `.github/workflow-pins/` — `check_workflow_pins.py` + `tests/`: the lint
  forbidding a `default:` on `workflows_ref` and requiring the empty-ref guard
  at every checkout. The `job.workflow_sha` self-pin is exempt from the
  `default:` half only — it stops the ref being MUTABLE, not being EMPTY, so it
  still has to carry a guard (BE-4169, corrected by BE-8077; see below).
- `.github/bump-callers/` — `bump-callers.sh`, the ONE fleet-agnostic script
  that opens SHA-bump PRs in consumer repos when a reusable workflow changes,
  plus `preflight.sh` (BE-6475), the ONE staleness/decommission guard that runs
  ahead of it — `proceed` / `new_sha` step outputs, `WATCHED` +
  `WATCHED_ASSETS` inputs, plus `WATCHED_PATHSPECS` (BE-6676; the excluding
  fleets: pr-risk, and pr-size/cursor-review since BE-7084) + `WATCHED_EXEC`
  (the per-executed-file fleets: pr-risk, pr-derisk). Watched inputs MUST
  mirror that fleet's `paths:` filter, exclusions included. Tests in `tests/`.
- `scripts/pr-risk/` + `scripts/pr-derisk/` — the two rungs of the PR risk ladder.
  v0 grades (`grade-pr-risk.sh`, deterministic — **no LLM in the grading path; keep it
  that way**); v1 plans a split on `/derisk` (`plan-derisk.sh`), the ONLY place a model
  runs, and every floor it shows is computed by v0's grader over a synthetic record,
  never claimed by the model. A pr-derisk caller executes BOTH trees at its pinned ref
  — hence its bump fleet watching `scripts/pr-risk/**`. Tests in each `tests/`.
- `README.md` — the public workflow catalog: per-workflow purpose, the SHA-pin
  usage pattern, and the versioning policy. Keep its table in sync when you add
  a workflow.
- `docs/callers/` — one setup guide per reusable workflow: a complete,
  copy-pasteable caller (including `on:` and the exact permission grant),
  required secrets/vars, and per-workflow footguns. Add a page when you add a
  workflow; the README table links to it.

## Reusable workflow catalog (what each does)

- `cursor-review.yml` — label-triggered multi-model PR review (4-lab × 2-type
  panel → judge → one PR review with severity badges). Advisory: it posts a
  review, it does not gate.
- `cursor-review-auto-label.yml` — translates PR assignment/open into the review
  label (via an app token, since a `GITHUB_TOKEN`-applied label won't fire runs).
- `groom.yml` — scheduled/dispatch org-wide code-cleanup sweep: read-only finder
  → independent verifier on a clean whole-repo checkout → dedup vs the ledger →
  file survivors as `groom` GitHub issues as a bot. Agent step holds no write
  creds; briefs live in `.github/groom/`. Opt-in auto-builder (`builder: true`,
  BE-4003) turns the top CONFIRMED findings into review-gated PRs (never
  auto-merged) via a credential-free patch job + a separate bot PR job.
- `pr-derisk.yml` — on-demand `/derisk` comment command (beta, off by default): re-grade
  the PR, ONE model call for a split partition, floors computed by `grade-pr-risk.sh
  --stdin`, one sticky advisory comment. Nothing gated, routed or filed; safe as a comment
  command because `issue_comment` runs from the default branch, the commenter is
  association-gated, and no PR code is checked out.
- `agents-md-integrity.yml` — enforces the AGENTS.md standard on the caller repo.
- `coderabbit-config-validate.yml` — validates the caller's `.coderabbit.yaml`
  against the vendored CodeRabbit schema, on the PR that breaks it. CodeRabbit
  rejects an invalid config WHOLE (everything in it goes inert) and validates the
  BASE branch, so the breakage otherwise surfaces one PR late on the wrong diff.
  Parse/`maxLength`/type errors FAIL; an unknown key WARNS wherever its object is
  closed (`additionalProperties: false`, 5; or by omission, 103) unless `strict_unknown_keys: true`; an absent file passes,
  though the other extension spelling is validated, not called absent. A SIBLING of
  agents-md-integrity, deliberately not an extension of it — that workflow is
  scoped to agent-instruction files, and folding this in would push a Python
  dependency and a new failure mode onto every one of its callers.
- `refresh-coderabbit-schema.yml` — weekly PR moving the vendored
  `schema.v2.json` when upstream drifts (fetch is `curl -fsSL`; the URL
  301-redirects and a bare curl writes an HTML stub). Merging it trips the
  coderabbit-config fleet, which rolls the callers.
- `assign-reviewers.yml` — expertise-aware, load-balanced reviewer requests.
- `refresh-reviewers.yml` — scheduled drift-detector: recomputes the caller's
  reviewers.yml from git history and opens one idempotent drift PR (never a
  live mutator).
- `assign-prs-to-author.yml` — assigns unassigned open PRs to their author.
- `detect-unreviewed-merge.yml` — SOC 2: flags PRs merged without approval.
  THIS repo is deliberately NOT self-enrolled: nothing merged here reaches a
  consumer until that consumer approves its own SHA-bump PR, and that repo's own
  detector audits it. Do not re-add a `ci-detect-unreviewed-merge.yml` caller.
- `bump-cursor-review-callers.yml` / `bump-auto-label-callers.yml` /
  `bump-agents-md-callers.yml` / `bump-pr-size-callers.yml` /
  `bump-pr-risk-callers.yml` / `bump-pr-derisk-callers.yml` /
  `bump-assign-reviewers-callers.yml` /
  `bump-groom-callers.yml` / `bump-detect-unreviewed-merge-callers.yml` /
  `bump-coderabbit-config-callers.yml` — thin
  entrypoints over `bump-callers.sh` that fan SHA bumps out to consumers. A
  groom, pr-risk or pr-derisk caller pins TWICE (`uses:` + `workflows_ref:`); the shared rewrite
  moves both, so never hand-bump one alone. `stale.yml` and
  `assign-prs-to-author.yml` have no fleet because they have no callers;
  `detect-unreviewed-merge.yml`'s fleet is
  `bump-detect-unreviewed-merge-callers.yml` — live, its
  `DETECT_UNREVIEWED_MERGE_CALLERS` roster seeded and stored as a repo secret
  (BE-6473); this repo is not in it, being un-self-enrolled (above).
- `bump-cursor-cli-pin.yml` — weekly PR moving `CURSOR_CLI_VERSION` /
  `CURSOR_CLI_SHA256` in `cursor-review.yml` (BE-5870). Not a caller bumper:
  merging it trips `bump-cursor-review-callers.yml`'s path filter, which rolls
  the fleet. Cursor ships no checksums, so the PR reviewer is the trust anchor
  for the digest; the nixpkgs cross-check corroborates when it can and is fatal
  only on a same-version hash MISMATCH (never a gate — nixpkgs lags releases).

## Conventions & gotchas

- **Public repo — never leak private caller names.** Consumer repo lists live in
  repo **secrets** — one per fleet (`CURSOR_REVIEW_CALLERS`,
  `AUTO_LABEL_CALLERS`, `AGENTS_MD_CALLERS`, `PR_SIZE_CALLERS`,
  `PR_RISK_CALLERS` (SHARED by the pr-risk + pr-derisk fleets via `FILE_FILTER` — one entry per caller file, never a second roster), `ASSIGN_REVIEWERS_CALLERS`, `GROOM_CALLERS`,
  `DETECT_UNREVIEWED_MERGE_CALLERS`, `CODERABBIT_CONFIG_CALLERS`; the bump-callers README table is canonical)
  — never hardcoded in a workflow file or printed to run logs (logs are public).
  Secrets, not variables (BE-6472): a variable passed via a step's `env:` prints
  unmasked in the env dump Actions emits *before* the step, so the bumper's own
  masking can never run early enough; a secret is masked there too. The bumper
  still masks each name. Keep private repo paths/detail out of workflow files,
  commit messages, and PR text.
- **Pin everything by full commit SHA**, with a trailing `# v1` comment — both
  the `uses:` in callers and every third-party action here. Bare `@v1` fails the
  pin-validation (`pinact`, `zizmor`) that consumer CI runs. See README "Pinning".
- **`workflows_ref` is REQUIRED, never given a `default:`** (BE-5546) — a default
  lets a caller SHA-pin `uses:` yet load mutable scripts, and `required:` is
  unenforced for `workflow_call` (omitted → `''` → `actions/checkout` takes the
  default branch) — hence the empty-ref guard, in the checkout's OWN job.
  `groom.yml` is the sanctioned exception (BE-4169, fixed in BE-8077): it
  defaults to `''` and its checkouts fall back to `job.workflow_sha` — the exact,
  immutable commit the reusable resolved from — so an omitted input can't reach a
  mutable ref. It is `job.workflow_sha`, NOT `github.job_workflow_sha`: that one
  is an OIDC claim, not a `github` context property, so it expands to `''` and
  checkout takes the default branch — which is why every one of those jobs also
  runs a fail-closed empty-ref guard (`job.workflow_sha` needs runner v2.334.0+),
  and why the pin lint now FLAGS the old spelling. `cursor-review.yml`'s
  never-fail ledger job warns and skips its checkout instead. (actionlint
  ≤ 1.7.12 false-positives on `job.workflow_sha`; nothing here runs actionlint.)
- **Scripts are the single source of truth**, loaded at run time from a pinned
  ref of THIS repo — never from the caller's checkout. That's what makes the
  reviewer/checker tamper-proof: a PR can't rewrite the logic judging it. The
  self-enrollment callers (`ci-cursor-review.yml`, `ci-assign-reviewers.yml`,
  `ci-groom.yml`) deliberately pin a merged-main SHA instead of a local `./`
  path for the same reason — do not "simplify" them to a path.
- **One bumper, not several.** `bump-callers.sh` backs every fleet; the
  `bump-*-callers.yml` files are thin per-fleet wrappers (they stay separate so a
  `cursor-review.yml` change doesn't spuriously bump agents-md or pr-size
  callers). Do not fork the script — a forked copy is how other shared org
  machinery has drifted.
- **Enrolling a caller is TWO steps.** Merge the caller, *and* add the repo to
  its `*_CALLERS` roster secret. Skipping the second is the most repeated mistake
  here: the pin then never moves, the caller drifts behind the reusable, and it
  fails at startup much later with no obvious cause. This repo did it to its own
  `ci-groom.yml`. Rosters are write-only secrets — audit the canonical
  `callers.json` (the run log prints a matching count + sha256) against
  reality both ways. Being listed is necessary, not sufficient: a caller's pin
  shape (placeholder, tag, branch, short SHA) no longer matters — the rewrite
  is anchored to the pin token, not to 40-hex-ness, so it self-heals any of
  those on the next bump (BE-4662), and a shape it truly cannot move (e.g. a
  `workflows_ref` fed by a `${{ … }}` expression) fails the run rather than
  shipping a half-bumped caller. What the bumper *can't* fix is a roster entry
  pointing at a file with no pin of ours to move — absent on the caller's
  default branch, naming some *other* github-workflows reusable but never
  ours, or naming no github-workflows reusable at all in a spelling the
  bumper can parse — that is the roster entry being wrong, not a pin to move,
  and it fails the run naming it (BE-6471). When auditing, compare the roster
  against reality in both directions — a roster entry whose caller file does
  not exist is equally broken.
- **New reusable workflow?** `on: workflow_call` + a header comment documenting
  inputs/secrets/triggers + a caller-pattern example, then a
  `docs/callers/<name>.md` setup guide and a row in the README table (see
  CONTRIBUTING.md "Adding a new reusable workflow"). Move the floating major tag
  after merge.
- **Document only inputs that exist.** GitHub rejects an unknown input at startup,
  so a phantom input in the docs is a broken caller for whoever copies it. Check
  `on.workflow_call.inputs` before documenting a knob. The `cursor-review`
  `blocking:` input is the worked example: it shipped in #16, was deleted from the
  workflow by #31 (a judge-extraction fix) while its docs and
  `gate-unresolved.py` were left behind, and the stale docs outlived it here, in
  the README, and in `.github/cursor-review/README.md`. **Deleting an input is a
  docs change too** — grep the repo for its name in the same commit.
- **Versioning:** semver-style major tags (`v1`, `v2`). Breaking changes bump the
  major; backwards-compatible changes move the existing tag in place
  (`git tag -f v1 <sha> && git push -f origin v1`). This tag force-move is the
  one sanctioned force-push — it is NOT license to force-push branches.
- **Commit style:** Conventional Commits with a scope, e.g.
  `fix(cursor-review): …`, `ci(bump-callers): …`, `feat(assign-reviewers): …`.
  Append a `(BE-####)` Linear suffix when a ticket drives the change. Land via
  squash-merged PR.
- **This AGENTS.md is itself gated** by the standard `agents-md-integrity.yml`
  enforces: keep it under 200 lines (aim ≤150), keep `CLAUDE.md` a bare
  `@AGENTS.md` shim (never a divergent copy), and never add a `.cursorrules`.

## Deeper docs

- [`README.md`](README.md) — public catalog, SHA-pin usage, versioning.
- [`docs/callers/`](docs/callers/) — per-workflow setup guides (copy-pasteable callers).
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — tests to run, breaking-change rules, enrollment.
- [`SECURITY.md`](SECURITY.md) — disclosure process + the agent credential boundary.
- [`.github/cursor-review/README.md`](.github/cursor-review/README.md) — review panel internals + adoption.
- [`.github/agents-md-integrity/README.md`](.github/agents-md-integrity/README.md) — the checker + its knobs.
- [`.github/bump-callers/README.md`](.github/bump-callers/README.md) — the shared bumper + its fleets.
