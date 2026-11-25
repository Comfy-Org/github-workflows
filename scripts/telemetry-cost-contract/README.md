# `scripts/telemetry-cost-contract/` — the cost-impact-explanation gate

The scripts behind
[`telemetry-cost-contract.yml`](../../.github/workflows/telemetry-cost-contract.yml). A
consumer repo runs the reusable workflow via a thin two-workflow caller (signal + validate)
and keeps no logic of its own beyond its `patterns` input; this directory holds the decision
path, loaded from the `workflows_ref` SHA the caller pins. Standard-library Python only,
matching this repo's convention.

| File | Role |
|---|---|
| `lib.py` | Pure, network-free core — `patterns` parsing, diff-added-line extraction, whole-PR-diff splitting (`split_unified_diff`), the detection match (`first_match`), the `## Cost impact` section check, failure-category selection, per-category status state and copy. Imported by `validate.py` and the test suite. |
| `validate.py` | The side-effecting orchestration: resolve the PR from the `workflow_run` event, skip unprotected base branches, read changed files with diff patches via `GET .../pulls/{n}/files` (GitHub-owned metadata, no checkout), recover any omitted `patch` from the whole-PR unified diff, apply the gate, publish the `Telemetry cost contract` commit status, and maintain the single marker PR comment. GitHub via the `gh` CLI. Imports `lib.py`. |
| `tests/` | Hermetic `unittest` suite over the pure core — no network. |

## The invariant

A PR turns red for a cost-impact verdict when an **added** diff line matches a
caller-configured pattern AND the PR body has no non-empty `## Cost impact` section AND the PR
carries no exemption label. Detection reads only GitHub-rendered diff text — the `patch`
fragments the changed-files API returns, plus the whole-PR unified diff (`GET .../pulls/{n}`
under `application/vnd.github.diff`) for any file whose `patch` GitHub omitted. Nothing here
checks out the PR branch or executes PR content, which is what makes it safe to run in the
privileged `workflow_run` validator job on a fork or Dependabot PR.

### Why there are two reads, not one

`GET .../pulls/{n}/files` silently drops a file's `patch` past a per-file render ceiling, and
that ceiling is low enough that one large generated client or JSON fixture in an otherwise
ordinary PR used to take the whole gate out. The whole-PR diff endpoint's ceiling is far
higher, so `lib.split_unified_diff` recovers those files' hunks and `validate.py` splices
them into the changed-file entries before detection runs. The recovered fragments are shaped
exactly like the API's `patch` field — hunks only, no `diff --git`/`---`/`+++` headers — so
`first_match` consumes them unchanged, and a `+++ b/<path>` header can never be mistaken for
an added line. Recovery is logged as a `::warning::` and recorded in the job summary in both
directions; a gate that quietly stops checking is worse than no gate, because it reads as
coverage.

### Two non-success states, and the difference matters

Separately, the validator fails closed — no cost-impact verdict at all — when it cannot read
a changed file's content from **either** source, when a recovered fragment does not account
for all of that file's reported `additions`, or when the changed-file lookup itself errors
(`changed_files_unavailable`). In the two reporting modes that publish a red status that is
the commit-status state **`error`**, never `failure`, and the same now goes for the
pre-verdict infrastructure failures (PR fetch, ambiguous PR resolution, unreadable branch
protection, a non-`pull_request` trigger).

`failure` is this gate's verdict that the contract was broken; `error` is this gate reporting
it could not reach a verdict. Both are non-success, so a required context still blocks in
enforce mode and the exit code is unchanged — distinguishable is not the same as permissive —
but "I could not look" no longer emits the same signal as "you broke it", for a human or for
a tool reading the status. See
[`docs/callers/telemetry-cost-contract.md`](../../docs/callers/telemetry-cost-contract.md) for
the caller setup and rollout.
