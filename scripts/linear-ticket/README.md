# `scripts/linear-ticket/` — the linked-Linear-ticket gate

The scripts behind [`linear-ticket.yml`](../../.github/workflows/linear-ticket.yml). A
consumer repo runs the reusable workflow via a thin two-workflow caller (signal + validate)
and keeps no logic of its own; this directory holds the decision path, loaded from the
`workflows_ref` SHA the caller pins. Standard-library Python only (no third-party deps),
matching this repo's Python convention.

| File | Role |
|---|---|
| `lib.py` | Pure, network-free core — candidate extraction, `team-keys` validation, the attachment **policy gate** (`filter_issues`), Linear error classification, failure-category selection, failure copy, and the batched diagnostic-query builder. Imported by `validate.py` and by the test suite. |
| `validate.py` | The side-effecting orchestration: resolve the PR from the `workflow_run` event, skip unprotected base branches, refetch it, publish the `linear-ticket` commit status, query `attachmentsForURL`, apply the gate, run one batched diagnostic query, and maintain the single marker PR comment. GitHub via the `gh` CLI (`subprocess`), Linear via `urllib`. Imports `lib.py`. |
| `tests/` | Hermetic `unittest` suites over the pure core and protected-branch orchestration — no network. |

## The invariant

The **only** thing that turns the check green is an attachment Linear returns for the PR's
canonical `html_url` (`attachmentsForURL`) whose issue satisfies policy — `filter_issues`.
This invariant applies when the PR targets a protected branch; an unprotected target is outside
the gate and passes without a Linear query. A `TEAM-123`-shaped string an author types is not a
link. So:

- The PR URL is passed to Linear as a **GraphQL variable**, never interpolated into the query.
- The policy reads the resolved issue's **API `team.key`** and **`state.type`**, never a
  prefix or a state *name* the author controls.
- Candidate identifiers extracted from branch/title/body (`extract_candidates`) are
  **diagnostics only** — capped at 20, resolved in one batched query — and can turn a red
  check's *message* from "no candidate found" into "referenced but not linked", never turn it
  green.
- "Any issue remains" is the pass rule: a PR linked to several tickets passes when at least
  one linked issue satisfies policy.

## Security & failure model

- Runs in the **privileged `workflow_run` job**; every PR-derived value is untrusted DATA
  passed through `gh api` argument lists / stdin and GraphQL variables. No PR code is checked
  out or executed.
- The PR is resolved from **GitHub-owned** `workflow_run.pull_requests` (same-repo) or the
  commit→PR association (fork), requiring **exactly one open PR**; ambiguous associations are
  refused.
- The PR's current base branch is read from GitHub and its `protected` property determines
  whether the ticket gate applies. This covers any number of branches protected by classic
  branch protection or rulesets without a caller-maintained branch list. An unreadable
  protection state fails closed.
- Before every terminal status write, the PR head SHA and base branch are **refetched**; if
  either changed, the run exits without publishing so a superseded validation can't overwrite
  a newer result. The base check matters when a retarget keeps the same head commit.
- **Fails closed**: auth, schema, malformed-response, timeout, and rate-limit-exhaustion are
  reported as an *infrastructure* error (red in enforce mode), never as an invalid ticket.
  Linear signals rate limiting as HTTP 400 + GraphQL `RATELIMITED`, which is retried within a
  bounded budget (2s, 4s, 8s, 16s over five attempts).
- Comment I/O is **best-effort**: a comment failure is warned, never changes the verdict.
- **Reporting mode is not the verdict.** `failure_outcome()` maps (category, mode) to the
  status state, comment headline, and exit code; the diagnosis is identical in every mode.
  `enforce: true` → red status + nonzero exit. `enforce: false` (warn-only) never exits
  nonzero *on a verdict* and, with `soft-fail: true`, still publishes a **red** status — loud
  on the PR but blocking nothing while `linear-ticket` is not a required context, with a
  category-aware advisory note on the comment. `soft-fail: false` restores the silent
  always-green warn-only behaviour, and so does an **absent** `SOFT_FAIL` env: the workflow
  always passes it, so absent means a skewed `workflows_ref`/`uses:` pin, and a caller that
  cannot express the input is never silently upgraded to red (`lib.soft_fail_enabled`).
- The advisory note is **hedged on purpose**: the validator never reads the caller's ruleset,
  so it cannot claim "this does not block the merge" — in the documented footgun (warn-only +
  the context already required) that claim would be false and would misdirect the author. It
  names what actually blocks (your ruleset) and the misconfiguration to report.
- Status **descriptions are category-aware**: `infra_error` means Linear could not be queried,
  so the status must not read "no linked Linear issue" — a diagnosis the run never made.
- A failed **terminal status write** is exit 1 even in warn-only: soft-fail's whole point is
  that the red status lands, and a missing write leaves the previous status standing. This is
  one of the run-level exits that warn-only does *not* suppress.

## Running the tests

```bash
python3 -m unittest discover -s scripts/linear-ticket/tests -p 'test_*.py' -v
python3 -m py_compile scripts/linear-ticket/lib.py scripts/linear-ticket/validate.py
```

The hermetic suite covers the design's acceptance cases that live in pure functions
(extraction/dedup/cap, `team-keys` validation, the policy gate incl. multi-link and
state/team handling, error classification, category selection, the diagnostic-query
builder, and the reporting-mode table). The **real-Linear behaviours** — URL canonicalization and attachment timing — are
proven in the pilot, not by mocks (design §11).
