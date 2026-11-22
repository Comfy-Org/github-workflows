# `scripts/telemetry-cost-contract/` — the cost-impact-explanation gate

The scripts behind
[`telemetry-cost-contract.yml`](../../.github/workflows/telemetry-cost-contract.yml). A
consumer repo runs the reusable workflow via a thin two-workflow caller (signal + validate)
and keeps no logic of its own beyond its `patterns` input; this directory holds the decision
path, loaded from the `workflows_ref` SHA the caller pins. Standard-library Python only,
matching this repo's convention.

| File | Role |
|---|---|
| `lib.py` | Pure, network-free core — `patterns` parsing, diff-added-line extraction, the detection match (`first_match`), the `## Cost impact` section check, failure-category selection and copy. Imported by `validate.py` and the test suite. |
| `validate.py` | The side-effecting orchestration: resolve the PR from the `workflow_run` event, skip unprotected base branches, read changed files with diff patches via `GET .../pulls/{n}/files` (GitHub-owned metadata, no checkout), apply the gate, publish the `Telemetry cost contract` commit status, and maintain the single marker PR comment. GitHub via the `gh` CLI. Imports `lib.py`. |
| `tests/` | Hermetic `unittest` suite over the pure core — no network. |

## The invariant

A PR turns red for a cost-impact verdict when an **added** diff line matches a
caller-configured pattern AND the PR body has no non-empty `## Cost impact` section AND the PR
carries no exemption label. Detection reads only the `patch` fragments GitHub's changed-files
API already returns — nothing here checks out the PR branch or executes PR content, which is
what makes it safe to run in the privileged `workflow_run` validator job on a fork or
Dependabot PR.

Separately, the validator also fails closed — publishing red with no cost-impact verdict at
all — on infrastructure failure: the changed-file lookup erroring, or a changed file with
added content but no renderable `patch` (`changed_files_unavailable`). This can block a
required check in enforce mode even though no matching line was ever found; it is a
"couldn't verify" signal, not a cost-impact finding. See
[`docs/callers/telemetry-cost-contract.md`](../../docs/callers/telemetry-cost-contract.md) for
the caller setup and rollout.
