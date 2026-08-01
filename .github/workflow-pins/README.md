# workflow-pins

An **internal repo lint** — unlike the other directories here, nothing in this
one is loaded by a reusable workflow at run time. It guards a property of this
repo's own workflow files.

- **`check_workflow_pins.py`** — fails if any `on: workflow_call` workflow in
  `.github/workflows/` declares a `default:` for its `workflows_ref` input.
  Text-level parsing (this repo is stdlib-only — no PyYAML), the same
  constraint `bump-callers.sh` works under.
- **`tests/`** — `unittest` suite, run by
  [`test-workflow-pins.yml`](../workflows/test-workflow-pins.yml) along with a
  CLI smoke test that a reintroduced default really exits non-zero.

```bash
python3 .github/workflow-pins/check_workflow_pins.py
```

## Why (BE-5546)

Every reusable workflow that loads its backing scripts at run time takes a
`workflows_ref` input and checks this repo out at that ref. If that input
defaults to a floating branch, a caller can SHA-pin `uses:` and *still* load
**mutable** scripts — into jobs that hold write permissions. The pin then
proves nothing about the code that actually runs.

So `cursor-review.yml`, `groom.yml`, and `agents-md-integrity.yml` declare
`workflows_ref` with `required: true` and **no default**, and each job that
consumes it runs a fail-fast guard before its assets checkout. The guard is not
belt-and-braces: **GitHub does not enforce `required: true` for `workflow_call`
inputs.** An omitted input arrives as `''`, and `actions/checkout` with
`ref: ''` silently checks out the default branch — recreating the hole exactly.
The guard also emits a (non-fatal) `::warning::` when the ref is not a full
40-hex SHA, since branch and tag refs can move between jobs mid-run.

It is copied inline into each consuming job rather than factored into a
composite action **on purpose**: a composite would have to be loaded with
`uses: Comfy-Org/github-workflows/.github/actions/…@<ref>` — the very ref being
validated — and a job cannot `uses: ./…` before its checkout. Twelve copies of a
16-line guard is the cost of not making the check depend on the thing it checks.

Deleting a `default:` is a one-line edit to undo, hence the lint. It covers
**every** `workflow_call` workflow, not an allow-list of today's three, so a
workflow added later is guarded the day it lands.

`KNOWN_EXEMPT` in the script carries workflows with the same debt that are
tracked under their own ticket (today: `pr-size.yml`, whose caller fleet has
not been enumerated yet). The lint fails on a **stale** entry — one whose
workflow no longer has the default — so the list drains itself.
