# Contributing

Thanks for helping out. One thing to understand before you start:

> **Every change here is live-fire.** Other repositories call these workflows at
> pinned SHAs, and moving the `v1` tag pushes your change to every caller pinned
> to the tag on their next run. There is no staging environment. A workflow that
> is merely *wrong* fails someone else's CI; one that is wrong about
> **permissions** fails it at startup with no logs to explain why.

## Before you open a PR

Run the tests for whatever you touched. They are the same commands CI runs:

```bash
# groom (ledger + dedup)
python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v

# cursor-review (finding extraction, bot identity)
python3 -m unittest discover -s .github/cursor-review/tests -p 'test_*.py' -v

# AGENTS.md standard checker
python3 -m unittest discover -s .github/agents-md-integrity/tests -p 'test_*.py' -v

# public-repo leak guard (allowlist + the caller-can't-edit-it assertions)
python3 -m unittest discover -s .github/public-repo-hygiene/tests -p 'test_*.py' -v

# caller-bump machinery
shellcheck -x .github/bump-callers/bump-callers.sh .github/bump-callers/tests/test_bump_callers.sh
bash .github/bump-callers/tests/test_bump_callers.sh

# org repo literal allowlist lint (runs on EVERY change — no path filter)
shellcheck -x .github/lint/check-org-repo-literals.sh
bash .github/lint/check-org-repo-literals.sh

# PR-size classifier
cd scripts/check-pr-size && go vet ./... && go test ./...
```

Do **not** commit build outputs. `scripts/check-pr-size/` is compiled from source
by `pr-size.yml` at run time; a committed binary is dead weight (and the wrong
architecture for the `ubuntu-latest` runners).

## Changing a reusable workflow

Ask two questions in order:

**1. Does this change what a caller must send or grant?** Adding a required
input, adding a required secret, or making a nested job request a *new*
permission are all **breaking**, even though nothing in the caller's YAML
changed. GitHub validates the permission grant at **startup**, so a caller that
was fine yesterday fails with an opaque "workflow file issue" and zero jobs. If
the answer is yes, bump the major tag (`v1` → `v2`) and let callers opt in.

**2. Does it change assets loaded at run time?** Several workflows fetch prompts,
briefs, or checker scripts from `workflows_ref` while running. That input is
required with no default (`groom.yml` excepted — it defaults to `''` and every
asset checkout falls back to `job.workflow_sha`, the commit the caller's `uses:`
resolved to, so leaving it unset there is safe; see the README), so a caller that
bumps `uses:` and leaves an explicitly-set `workflows_ref:` behind mixes your new
workflow with its old assets. When you
change assets and workflow together, say so in the PR body so callers bump both.

## Adding a new reusable workflow

1. Add `.github/workflows/<descriptive-name>.yml` with `on: workflow_call:`.
   Document every input and secret inline.
2. Declare **minimum permissions per job**, not at the workflow level. Callers
   must grant the union of what your nested jobs request — keep that union small
   and state it in the header comment.
3. Add a setup guide at `docs/callers/<descriptive-name>.md` following the shape
   of the existing ones: a **complete, copy-pasteable** caller (including `on:`),
   the exact permission grant, required vs optional secrets and `vars`, and any
   footguns. A guide that omits `on:` or the permission grant is not a guide.
4. Add a one-line row to the [README](README.md#workflows) table linking to it.
5. If the workflow should be adopted broadly, add a `<NAME>_CALLERS` roster secret
   and a `bump-<name>-callers.yml` job so pins get bumped automatically. See
   [.github/bump-callers/](.github/bump-callers/).
6. Add a `test-<name>.yml` if it ships scripts.

## Enrolling a repository as a caller

Two steps. Missing the second is the common mistake:

1. Add the caller workflow to the consumer repo (see
   [`docs/callers/`](docs/callers/)).
2. **Add the repo to the matching `<NAME>_CALLERS` roster secret** on this repo
   (`jq -c . callers.json | gh secret set <NAME>_CALLERS --repo Comfy-Org/github-workflows`,
   from the canonical `callers.json` in the private infra/ops repo — a secret,
   not a variable, so caller names stay out of the public run logs, and there is
   no read-back).
   That roster is what `bump-*-callers.yml` reads to keep pins current. A repo
   absent from it keeps its original SHA forever, drifts behind the reusable, and
   eventually breaks when the two stop being compatible.

## Security-sensitive areas

The AI workflows (`cursor-review.yml`, `groom.yml`) aim to split model execution
from credential use: the agent jobs run with `contents: read` and mint no GitHub
token, emitting a patch or findings that a **separate** job applies as a GitHub
App. Preserving that boundary matters more than convenience — do not give an agent
job a write token to save a step.

The split is clean for `groom.yml` and for `cursor-review.yml`'s 8 panel cells.
It is **not** clean for cursor-review's judge: `Consolidate panel` runs the judge
model and posts the review in one `pull-requests: write` job. Treat that as a known
rough edge to work *toward* the boundary, not as licence to widen it elsewhere. See
[SECURITY.md](SECURITY.md).

## Review

Reviewers and assignees are routed automatically from
[`.github/reviewers.yml`](.github/reviewers.yml) by path. If you are changing a
bucket's globs, read the comments at the top of that file first — it explains why
the buckets are the durable part and the names are not.
