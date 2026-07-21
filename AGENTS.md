# AGENTS.md — Comfy-Org/github-workflows

Shared, versioned **reusable GitHub Actions workflows** for use across Comfy-Org
repositories. This repo is **public** so any repo — public or private, inside or
outside the org — can call these workflows. Each reusable workflow's logic
(prompts, Python/shell scripts) lives *here* as the single source of truth;
consumer repos carry only a thin caller that pins this repo by full commit SHA.
There is no application to build or run — the deliverable is the workflows
themselves plus the scripts that back them.

## Commands

Python is stdlib-only (no requirements file); CI uses **Python 3.12**. Run from
the repo root. Each command mirrors a CI job (see `.github/workflows/test-*.yml`):

```bash
# cursor-review helper-script tests (extract-findings, post-review)
python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py' -v

# agents-md-integrity checker tests
python3 -m unittest discover -s .github/agents-md-integrity/tests -p 'test_*.py' -v

# groom dedup/rejection ledger tests
python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v

# bump-callers shell tests + lint (gh is stubbed; no network)
shellcheck -x .github/bump-callers/bump-callers.sh .github/bump-callers/tests/test_bump_callers.sh
bash .github/bump-callers/tests/test_bump_callers.sh

# run the AGENTS.md integrity checker against any repo tree
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
  time, never copied into consumers. Tests in `tests/`.
- `.github/agents-md-integrity/` — `check_agents_md.py`, the checker behind
  `agents-md-integrity.yml` (enforces this AGENTS.md standard). Tests in `tests/`.
- `.github/groom/` — briefs + building blocks behind the reusable **groom**
  code-cleanup workflow (`groom.yml`, epic BE-3870): `finder.md` / `verifier.md`
  (the two-phase prompts, single source of truth, loaded at run time), and
  `ledger.py`, the durable dedup/rejection memory that stops the stateless groom
  CI run from re-filing already-filed or human-rejected findings (it uses GitHub
  issue state as the store — no new secret). Tests in `tests/`.
- `.github/bump-callers/` — `bump-callers.sh`, the ONE fleet-agnostic script
  that opens SHA-bump PRs in consumer repos when a reusable workflow changes.
  Tests in `tests/`.
- `README.md` — the public workflow catalog: per-workflow purpose, the SHA-pin
  usage pattern, and the versioning policy. Keep its table in sync when you add
  a workflow.

## Reusable workflow catalog (what each does)

- `cursor-review.yml` — label-triggered multi-model PR review (4-lab × 2-type
  panel → judge → one PR review with severity badges). Advisory by default;
  `blocking: true` gates on unresolved findings.
- `cursor-review-auto-label.yml` — translates PR assignment/open into the review
  label (via an app token, since a `GITHUB_TOKEN`-applied label won't fire runs).
- `groom.yml` — scheduled/dispatch org-wide code-cleanup sweep (finds only, no
  PRs): read-only finder → independent verifier on a clean whole-repo checkout →
  dedup vs the ledger → file survivors as `groom` GitHub issues as a bot. Agent
  step holds no write creds; briefs live in `.github/groom/`.
- `agents-md-integrity.yml` — enforces the AGENTS.md standard on the caller repo.
- `assign-reviewers.yml` — expertise-aware, load-balanced reviewer requests.
- `assign-prs-to-author.yml` — assigns unassigned open PRs to their author.
- `detect-unreviewed-merge.yml` — SOC 2: flags PRs merged without approval.
- `bump-cursor-review-callers.yml` / `bump-agents-md-callers.yml` /
  `bump-pr-size-callers.yml` — thin entrypoints over `bump-callers.sh` that fan
  SHA bumps out to consumers.

## Conventions & gotchas

- **Public repo — never leak private caller names.** Consumer repo lists live in
  repo **variables** (`CURSOR_REVIEW_CALLERS`, `AGENTS_MD_CALLERS`), never
  hardcoded in a workflow file or printed to run logs (logs are public). The
  bumper masks names it processes. Keep private repo paths/detail out of
  workflow files, commit messages, and PR text.
- **Pin everything by full commit SHA**, with a trailing `# v1` comment — both
  the `uses:` in callers and every third-party action here. Bare `@v1` fails the
  pin-validation (`pinact`, `zizmor`) that consumer CI runs. See README "Usage".
- **Scripts are the single source of truth**, loaded at run time from a pinned
  ref of THIS repo — never from the caller's checkout. That's what makes the
  reviewer/checker tamper-proof: a PR can't rewrite the logic judging it. The
  self-enrollment callers (`ci-cursor-review.yml`, `ci-detect-unreviewed-merge.yml`)
  deliberately pin a merged-main SHA instead of a local `./` path for the same
  reason — do not "simplify" them to a local path.
- **One bumper, not several.** `bump-callers.sh` backs every fleet; the
  `bump-*-callers.yml` files are thin per-fleet wrappers (they stay separate so a
  `cursor-review.yml` change doesn't spuriously bump agents-md or pr-size
  callers). Do not fork the script — a forked copy is how other shared org
  machinery has drifted.
- **New reusable workflow?** `on: workflow_call` + a header comment documenting
  inputs/secrets/triggers + a caller-pattern example, then update the README
  table (README "Adding a new reusable workflow"). Move the floating major tag
  after merge.
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
- [`.github/cursor-review/README.md`](.github/cursor-review/README.md) — review panel internals + adoption.
- [`.github/agents-md-integrity/README.md`](.github/agents-md-integrity/README.md) — the checker + its knobs.
- [`.github/bump-callers/README.md`](.github/bump-callers/README.md) — the shared bumper + its fleets.
