# AGENTS.md — Comfy-Org/github-workflows

Shared, versioned **reusable GitHub Actions workflows** used across Comfy-Org
repositories. This repo is **public**, so any repo — public or private, in the org
or out — can call them. Each workflow's logic (prompts, Python/shell scripts) lives
*here* as the single source of truth; consumers carry only a thin caller pinning
this repo by full commit SHA. Nothing is built or run — the workflows ARE the deliverable.

## Commands

Python is stdlib-only, with ONE exception: `.github/coderabbit-config/` needs a
YAML parser and a JSON Schema validator, pinned in `requirements.txt` (exact
versions + sha256, installed `--require-hashes`). CI uses **Python 3.12**; there
is no repo-wide formatter config. Run from the repo root. Every suite is mirrored
by a path-filtered `.github/workflows/test-*.yml`, and that workflow — not the
list below — is the authority on the exact command; read the matching one for
whatever you touched.

```bash
# Python suites — agents-md-integrity, coderabbit-config, cursor-review, groom,
# linear-ticket, public-repo-hygiene, refresh-reviewers, workflow-pins:
python3 -m unittest discover -s <dir>/tests -p 'test_*.py' -v
# coderabbit-config first: pip install --require-hashes --only-binary=:all: -r <its requirements.txt>

# Shell suites — area-label, bump-callers, groom, pr-derisk, pr-risk (gh/model
# stubbed; no network). Glob `tests/*.sh`, not `test_*.sh` — groom's confinement
# suite is `sandbox-tests.sh` — and LOOP: `bash a.sh b.sh` runs only `a.sh`.
shellcheck -x <dir>/*.sh <dir>/tests/*.sh
for t in <dir>/tests/*.sh; do bash "$t" || { echo "FAILED: $t"; break; }; done
# shellcheck-only, no suite: .github/cursor-review/{install-cursor-cli,slack-notify}.sh

# Go — scripts/check-pr-size; its tests sit beside the source, not in tests/:
(cd scripts/check-pr-size && [ -z "$(gofmt -l .)" ] && go vet ./... && go test ./...)

# Repo-wide lints that take a target rather than a suite:
python3 .github/workflow-pins/check_workflow_pins.py   # no reusable may default `workflows_ref`
python3 .github/agents-md-integrity/check_agents_md.py --root .
```

## Layout

Most directories own a README with their design rationale — read it before changing
anything there; `.github/workflows/` and `scripts/check-pr-size/` are the exceptions.

- `.github/workflows/` — the reusable workflows (`on: workflow_call`), this
  repo's own CI callers (`ci-*.yml`), and the `test-*.yml` script tests.
- `.github/cursor-review/` — prompts + scripts behind `cursor-review.yml` (the
  multi-model panel + judge). `catalog-drift.py` reads the model pins *out of*
  `cursor-review.yml` — never duplicate that model list.
- `.github/agents-md-integrity/` + `.github/workflow-pins/` — the two self-checks:
  this AGENTS.md standard, and the lint forbidding a `default:` on
  `workflows_ref` / requiring the empty-ref guard at every checkout.
- `.github/public-repo-hygiene/` — the leak checker + the org-wide known-public
  allowlist it default-denies against. Never make that allowlist a workflow input:
  one a caller can pass is one a PR in that repo can widen.
- `.github/groom/` — the finder/verifier/builder briefs behind `groom.yml`, plus
  `ledger.py` (dedup), `interval.py` (cadence), `scope.py` (path containment) and
  `agent-sandbox.sh` (the credential boundary). `package.json` installs nothing: it
  is the one Dependabot-visible home of the `@anthropic-ai/claude-code` pin — keep
  it exact, and never re-hardcode a version in a `run:` step.
- `.github/coderabbit-config/` — the validator + the **vendored** `schema.v2.json`;
  never fetch it at validation time, which would put a third party in consumer CI.
- `.github/refresh-reviewers/` — `generate.py`, the reviewers.yml drift engine.
- `.github/bump-callers/` — `bump-callers.sh`, the ONE fleet-agnostic SHA-bump
  script, plus `preflight.sh`, the ONE staleness/decommission guard ahead of it. Its
  `WATCHED*` inputs MUST mirror that fleet's `paths:` filter, exclusions included.
- `scripts/pr-risk/` + `scripts/pr-derisk/` — the two rungs of the PR risk ladder.
  v0 grades deterministically — **no LLM in the grading path; keep it that way**.
  v1 plans a split on `/derisk`, the ONLY place a model runs, and every floor it
  shows comes from v0's grader. A pr-derisk caller executes BOTH trees.
- `scripts/{area-label,linear-ticket,check-pr-size}/` — logic behind those reusables.
- `README.md` (public catalog) + `docs/callers/` (per-reusable setup guides) —
  keep both in sync when you add a workflow.

**The workflow catalog lives in [`README.md`](README.md).** Do not restate it here;
a second catalog drifts, and this one already had. Three facts it cannot tell you:

- **Not self-enrolled in `detect-unreviewed-merge.yml`**, deliberately: nothing
  merged here reaches a consumer until that consumer approves its own SHA-bump
  PR, and that repo's own detector audits it. Do not re-add a caller.
- **Not self-enrolled in `public-repo-hygiene.yml`**, deliberately: this repo's
  fixtures and its `(BE-####)` commit convention are internal-reference-shaped.
- **A groom, pr-risk or pr-derisk caller pins TWICE** (`uses:` + `workflows_ref:`);
  the shared rewrite moves both, so never hand-bump one alone.

## Conventions & gotchas

- **Public repo — never leak private caller names.** Consumer rosters live in repo
  **secrets**, one per fleet — except pr-risk and pr-derisk, which SHARE
  `PR_RISK_CALLERS`; there is no `PR_DERISK_CALLERS`, and creating one leaves those
  callers unbumped (the bump-callers README table is canonical). Never hardcode a
  roster in a workflow file or print one to run logs, which are public. Secrets,
  not variables (BE-6472): a variable passed via a step's `env:` prints unmasked in
  the env dump Actions emits *before* the step, too early for the bumper's masking.
  Keep private repo paths and detail out of workflow files, commits, and PR text.
- **Pin everything by full commit SHA**, with a trailing `# v1` comment — both the
  `uses:` in callers and every third-party action here. Bare `@v1` fails the
  pin-validation (`pinact`, `zizmor`) that consumer CI runs.
- **`workflows_ref` is REQUIRED, never given a `default:`** (BE-5546) — a default
  lets a caller SHA-pin `uses:` yet load mutable scripts, and `required:` is
  unenforced for `workflow_call` (omitted → `''` → checkout takes the default
  branch). Hence the empty-ref guard, in the checkout's OWN job. `groom.yml`'s
  carve-out (BE-4169/BE-8077) buys the `default:` half ONLY, and only for a default
  of exactly `''`: its checkouts fall back to `inputs.workflows_ref ||
  job.workflow_sha` — **not** `github.job_workflow_sha`, an OIDC claim expanding to
  `''`, now flagged. That proves the ref IMMUTABLE, not non-empty (`job.workflow_sha`
  is `''` below runner v2.334.0), so groom keeps the empty-ref guard too;
  `check_workflow_pins.py` enforces both halves. See [`docs/callers/groom.md`](docs/callers/groom.md).
- **Scripts are the single source of truth**, loaded at run time from a pinned ref
  of THIS repo — never from the caller's checkout. That is what makes the
  reviewer/checker tamper-proof: a PR cannot rewrite the logic judging it. The
  self-enrollment callers (`ci-cursor-review.yml`, `ci-assign-reviewers.yml`,
  `ci-groom.yml`, `ci-agents-md-integrity.yml`) pin a merged-main SHA rather than
  a local `./` path for the same reason — do not "simplify" them to a path.
- **One bumper, not several.** `bump-callers.sh` backs every fleet; the thin
  `bump-*-callers.yml` wrappers stay separate only so one reusable's change does not
  bump another fleet. Never fork it — forking is how other shared org machinery drifted.
- **Comment/docs-only edit inside a watched surface? Add `Skip-caller-bump: true`**
  as the LAST commit's trailer — only the squashed message's trailing trailer
  block counts, and it declares the WHOLE PR bump-irrelevant for EVERY fleet.
  Reviewers must reject it on any PR with behavioral changes; when in doubt,
  leave it off and let the fleet bump. `workflow_dispatch` overrides a mistake.
- **Enrolling a caller is TWO steps.** Merge the caller, *and* add the repo to its
  `*_CALLERS` roster secret. Skipping the second is the most repeated mistake here
  — the pin never moves, and it fails at startup much later with no obvious cause.
  This repo did it to its own `ci-groom.yml`. Rosters are write-only, so audit the
  canonical `callers.json` against reality **in both directions**. Un-bumpable
  cases: [`.github/bump-callers/README.md`](.github/bump-callers/README.md).
- **New reusable workflow?** `on: workflow_call` + a header comment documenting
  inputs/secrets/triggers + a caller example, then a `docs/callers/<name>.md` guide
  and a README table row (CONTRIBUTING.md). Move the major tag after merge.
- **Document only inputs that exist.** GitHub rejects an unknown input at startup,
  so a phantom input in the docs is a broken caller for whoever copies it. Check
  `on.workflow_call.inputs` first. **Deleting an input is a docs change too** —
  grep the repo for its name in the same commit. (`cursor-review`'s `blocking:` is
  the worked example: deleted in #31, its docs outlived it in three places.)
- **Versioning:** semver-style major tags (`v1`, `v2`). Breaking changes bump the
  major; compatible changes move the tag in place — `git tag -f v1 <sha> && git
  push -f origin v1`. Without the push the public tag never moves. That force-move
  is the one sanctioned force-push — NOT license to force-push branches.
- **This AGENTS.md is itself gated** by the standard `agents-md-integrity.yml`
  enforces — under 200 lines (aim ≤150), `CLAUDE.md` a bare `@AGENTS.md` shim, no
  `.cursorrules` — via `ci-agents-md-integrity.yml`, so every PR and push to main
  runs the check. Locally: `check_agents_md.py --root .`.

## Deeper docs

- [`README.md`](README.md) — public catalog, SHA-pin usage, versioning.
- [`docs/callers/`](docs/callers/) — per-workflow setup guides (copy-pasteable callers).
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — tests to run, breaking-change rules, enrollment.
- [`SECURITY.md`](SECURITY.md) — disclosure process + the agent credential boundary.
- [`.github/cursor-review/README.md`](.github/cursor-review/README.md) — review panel internals.
- [`.github/groom/README.md`](.github/groom/README.md) — briefs, ledger, cadence, the CLI pin.
- [`.github/public-repo-hygiene/README.md`](.github/public-repo-hygiene/README.md) — the leak guard + its limits.
- [`.github/bump-callers/README.md`](.github/bump-callers/README.md) — the shared bumper + its fleets.
