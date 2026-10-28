# Review readiness evaluator

`readiness.py` consumes one JSON object on standard input and writes one JSON
result. It does not fetch evidence, publish results, or modify GitHub. The caller
owns the snapshot, policy and evidence collection. `READY` is an advisory result,
not a merge authorization; GitHub rules remain authoritative.

```sh
python3 scripts/review-readiness/readiness.py < evidence.json
python3 -m unittest discover -s scripts/review-readiness/tests -v
```

The snapshot identifies `repo`, `pr`, `base_sha`, `head_sha`, `tested_sha`,
`policy_version`, and the PR `author`. SHAs are lowercase 40-character hashes.
Each evidence scope repeats `repo`, `pr`, `base_sha`, `head_sha`, and
`policy_version`. Checks additionally name their tested SHA. The policy contains at least one required check object with a unique `name` and
`revision` set to `head` or `tested`, plus at least one human approval. Its
version must match the snapshot. The `tested` revision is supplied by the caller
and can be a merge candidate; `head` is the PR head. A GitHub check run attached
to a head SHA does not by itself prove which revision its job actually tested.

Input fields:

- `checks_complete`: whether the producer collected the complete check inventory.
  `checks` is a list of scoped `name`, `result`, `tested_sha`, and `proof` entries.
  Results are `success`, `failure`, `error`, `pending`, `cancelled`, `skipped`, `neutral`, or `unavailable`.
- `reviews`: a scoped object with `complete`, `proof`, and `approvals`. Each
  approval has `actor`, `human`, and `state` (`approved`, `dismissed`, or
  `changes_requested`). Supply the current effective review state for each actor.
- `findings`: a scoped object with `complete`, `proof`, and `items`; each item
  is `open` or `resolved`. The producer must cover every finding source required
  by the repo policy before calling the inventory complete.

A proof is an HTTPS link. Its syntax is checked, but its contents, identity,
permissions, completeness and relationship to the tested revision are **not**
verified here. The caller must establish those facts, including the source and
identity of human approvals, before treating the result as evidence. Missing
inventories, incomplete inventories, absent proof, and stale scoped records cannot
produce `READY`. Current failures, change requests and open findings produce
`BLOCKED` even when other evidence is incomplete. Stale records do not block or
satisfy the current snapshot. Cancelled, skipped, neutral, or unavailable checks wait. Duplicate
conflicting reviews never count as an approval; actor comparisons ignore case.

Malformed input or policy exits 2 without a result. Valid input exits 0 with
`state`, scope, tested SHA, reasons, and current evidence links. This evaluator
cannot detect an ABA revision change during collection, confirm GitHub's current
head at publication, establish the merge candidate, or decide which evidence the
repository requires. A trusted producer, repo-owned policy, publication recheck,
and integration tests are still required before a pilot or enforcement.

## Risk report consumer

`from_risk.py` accepts an object containing `snapshot`, `policy`, and `report`
with the report-only workflow's `summary` and graded `record`. It uses the
actual evaluator with empty check, review, and finding inventories, so its result
is always `WAITING`. The optional risk grade is classified as `observed`,
`stale`, or `unknown` and never substitutes for evidence. The caller must
independently establish that the supplied snapshot is current. This adapter does
not run automatically in a workflow or publish a check.

```sh
python3 scripts/review-readiness/from_risk.py < report-envelope.json
```

## GitHub review inventory

`collect_reviews.py` accepts `snapshot` and `policy` on standard input and uses
`gh api` to read the PR, its paginated reviews and its paginated review threads.
It checks that the PR is open and its base, head and author match before and
again after collection. A changed or unavailable snapshot discards the collected
review and thread evidence. `collection` reports which reads were observed,
partial, unavailable, or discarded.

Only the latest submitted approval, dismissal, or change request per actor can
count, and only when its review records the supplied head SHA. GitHub `User` and
`Bot` identities are distinguished; unknown or malformed identities make review
collection unavailable. A GitHub `User` may be a machine account, so its type and
repository permission do not establish a human identity. The collector checks the
current base permission of current non-author `User` approvals, but never counts
them as verified human approvals: write, maintain, or admin access leaves human
identity incomplete, and lower roles do not qualify. Missing, malformed or
unavailable permission responses also make approval evidence incomplete. It checks
at most 100 approvals and reports `reviewer_permissions` as
observed, partial, not needed, not attempted or discarded. Current change requests
still block even if a permission read fails. Comments and unsubmitted reviews do
not replace a submitted decision. Current unresolved, non-outdated review threads block even
if a later thread page fails. A partial thread read is not a complete inventory.

The collector never marks the finding inventory complete: GitHub review threads
are only one potential source. It also does not verify branch protection or code-owner requirements, prove a
tested revision, publish a report, or
verify that the caller's policy is authorized. Consequently this entry point can
return `BLOCKED` or `WAITING`, but never `READY`. Separate GitHub reads are not
atomic: reviews or permissions can change during collection, and an ABA branch change can
escape the recheck. It is advisory evidence, not a merge gate.

```sh
python3 scripts/review-readiness/collect_reviews.py < snapshot-and-policy.json
```

## Integrated shadow report

`shadow_report.py` runs the real risk grader with `REPORT_ONLY=1` and no
publication options, then collects GitHub reviews for the same supplied snapshot.
It runs the grader in a temporary directory and emits one JSON report to stdout.
A changed or unavailable final snapshot discards the risk grade too. The report
remains advisory: missing checks and incomplete findings prevent `READY`.

The reusable workflow `review-readiness-shadow.yml` retains that report for
30 days. Its caller provides the snapshot and policy; the report records the
provided tested SHA but does not verify it. The target repository must match
the caller repository. The workflow checks out its tools at its own immutable
workflow revision and does not execute PR code. Only use a reviewed, merged
provider revision in a caller. See the caller guide for trust and rollout limits.

## GitHub Actions failure evidence

A policy may add `actions_checks`, with a required check `name` and an exact
`workflow_path` for each source. The collector reads all check-run pages and
links each candidate to its GitHub Actions job and pull-request workflow run.
The GitHub Actions app, run attempt, workflow path, PR number, base and head must
match; the latest unambiguous matching job's reported failure blocks.

`actions_checks_matched` lists the matched names. An unreadable source, partial
inventory, unstarted or ambiguous newer job remains unknown; older failures do
not replace it. The collector rechecks the PR snapshot after collection and
discards these signals when it changes. Reads are not atomic and do not establish
reviewer permissions or a merge candidate.

This is **negative-only evidence**: a successful check run is attached to the PR
head, but that does not prove what revision its job actually tested. It never
satisfies a required passing check or produces `READY`. Caller-supplied policy
must be authorized separately. The pure evaluator accepts scoped
`reported_failures` and includes their proof links, but does not itself verify
the producer or link contents.
