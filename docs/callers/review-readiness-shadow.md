# Review readiness shadow report

This reusable workflow evaluates one caller-supplied PR snapshot and policy. It
reads risk inputs, current reviews and review threads, then retains one JSON
artifact for 30 days. It does not write labels, comments, reviews, check runs,
or repository rules. The Actions run and job checks still exist. The token has
read access to contents, pull requests, checks, actions and commit statuses.

The input `snapshot_and_policy_json` is a JSON object containing `snapshot` and
`policy` as documented in [the evaluator](../../scripts/review-readiness/README.md).
The target repo must equal the caller repo. The caller is responsible for obtaining
and verifying the snapshot and for choosing an authorized policy; input JSON is
not itself proof. Use only a reviewed, merged provider commit pinned by full SHA
in the caller's `uses:`. The provider tools load at the workflow's own revision,
never from the PR. This workflow runs only when explicitly called.

The report records the supplied `tested_sha` but cannot prove that tests actually
ran on it. It checks repository base permissions for User approvals, but cannot
establish whether those accounts belong to humans. It does not count them as
verified human approvals, verify code-owner rules, or cover every finding source.
Missing permission data also keeps approval evidence incomplete.
Missing checks and incomplete findings prevent `READY`; known current failures
can still produce `BLOCKED`. Data reads are not atomic, so a review change or an
ABA revision change can race collection. It is not an authorization to merge or
a substitute for GitHub branch protection. No pilot or enforcement follows from
adding the workflow; obtain owner approval and verify the integrated run first.

To recover, remove the caller or revert its pin. Existing artifacts retain their
normal expiry. An unavailable risk grade is reported as unknown, and an invalid
input or failed workflow produces no report artifact rather than a ready result.

## Inputs

| Input | Description |
| --- | --- |
| `snapshot_and_policy_json` | Required JSON object containing the target snapshot and advisory policy. |

## Example caller

Replace the provider ref with a reviewed, merged 40-character commit. Supply the
input explicitly when dispatching; do not treat PR-controlled data as policy.

```yaml
name: Manual review shadow
on:
  workflow_dispatch:
    inputs:
      snapshot_and_policy_json:
        description: Snapshot and advisory policy JSON
        required: true
        type: string
permissions:
  contents: read
  pull-requests: read
  checks: read
  actions: read
  statuses: read
jobs:
  shadow:
    uses: Comfy-Org/github-workflows/.github/workflows/review-readiness-shadow.yml@<reviewed-full-sha>
    with:
      snapshot_and_policy_json: ${{ inputs.snapshot_and_policy_json }}
```
