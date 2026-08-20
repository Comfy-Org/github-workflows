# workflow-pins

An **internal repo lint** — unlike the other directories here, nothing in this
one is loaded by a reusable workflow at run time. It guards a property of this
repo's own workflow files.

- **`check_workflow_pins.py`** — for every `on: workflow_call` workflow in
  `.github/workflows/`, fails if it (1) declares a `default:` for its
  `workflows_ref` input, or (2) checks out at `ref: ${{ inputs.workflows_ref }}`
  in a job that does not run the empty-ref guard first — recognizing the
  canonical `-z` guard and the length/charset shape `pr-risk.yml` uses, and
  and treating the `job.workflow_sha` self-pin `groom.yml` uses as an exemption
  from the *mutable-default* half only — it still has to carry a guard (see
  below). Text-level parsing (this repo is stdlib-only — no
  PyYAML), the same constraint `bump-callers.sh` works under.
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

So `cursor-review.yml`, `agents-md-integrity.yml`, `pr-size.yml`, and
`refresh-reviewers.yml` declare `workflows_ref` with `required: true` and **no
default**, and each job that consumes it runs a fail-fast guard before its
assets checkout. The guard is not belt-and-braces: **GitHub does not enforce
`required: true` for `workflow_call` inputs.** An omitted input arrives as
`''`, and `actions/checkout` with `ref: ''` silently checks out the default
branch — recreating the hole exactly. The guard also emits a (non-fatal)
`::warning::` when the ref is not a full 40-hex SHA, since branch and tag refs
can move between jobs mid-run. `pr-risk.yml` writes the same guarantee as one
length/charset test (`${#WORKFLOWS_REF} -ne 40`, OR'd with a charset check)
instead of a separate `-z` test — the lint recognizes both shapes: a length
check alone already rejects an empty ref (length 0), and `||` only ever widens
what a condition rejects, so one qualifying branch is enough regardless of
what it is OR'd with.

`groom.yml` is the one workflow whose `workflows_ref` is not required (BE-4169):
it defaults to `''`, and every checkout falls back to
`${{ inputs.workflows_ref || job.workflow_sha }}` — the exact commit THIS
reusable workflow was itself resolved from via the caller's `uses:` pin. That
value is never mutable, so an omitted input self-pins instead of reaching a
floating branch, which is what the lint's exemption is about. The lint
recognizes this LITERAL fallback expression only; anything else OR'd in (a
branch, a tag, another input) is the same hole wearing a different hat and stays
covered by both checks.

**The old `github.job_workflow_sha` spelling is now FLAGGED, not exempt
(BE-8077).** That is an OIDC token claim, not a property of the `github`
context, so Actions expanded it to `''` and `actions/checkout` read `ref: ''` as
this repo's default branch — the exact hole this lint exists to close, blessed
by the lint itself. `job.workflow_sha` is the `job`-context accessor added in
runner v2.334.0 (April 2026), and because it *is* empty on an older runner,
`groom.yml` no longer skips the guard: each of those seven jobs runs a
fail-closed `Require a resolvable workflows_ref` step ahead of its checkout, and
`cursor-review.yml`'s never-fail ledger job resolves the ref in a step that
warns and skips the checkout instead.

**The lint enforces that split, rather than trusting the prose.** The fallback
answers *mutability*; the guard answers *emptiness*; a checkout using the
fallback is exempt from the first check and still held to the second. Until
BE-8077 the fallback was exempt from **both**, so deleting all seven of
`groom.yml`'s guard steps left this lint — and its whole suite — green, while
the paragraph above already leaned on them. The guard-step detector accepts the
`WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}` binding those
seven use as well as the bare `${{ inputs.workflows_ref }}` one; matching only
the bare form meant none of the seven was ever consulted, *and* falsely reported
a checkout that dropped the fallback while keeping its guard. The fallback
pattern is also anchored to the closing `}}` and matched against the
comment-stripped line, so neither
`${{ inputs.workflows_ref || job.workflow_sha || 'main' }}` (mutable in exactly
the pre-v2.334.0 case the fallback exists for) nor a comment merely *naming* the
expression buys the exemption. (`actionlint` ≤ 1.7.12 false-positives on
`job.workflow_sha`; no CI here runs it.)

The guard is copied inline into each consuming job rather than factored into a
composite action **on purpose**: a composite would have to be loaded with
`uses: Comfy-Org/github-workflows/.github/actions/…@<ref>` — the very ref being
validated — and a job cannot `uses: ./…` before its checkout. Many copies of a
16-line guard is the cost of not making the check depend on the thing it checks.

Deleting a `default:` is a one-line edit to undo, hence the lint. It covers
**every** `workflow_call` workflow, not an allow-list of today's three, so a
workflow added later is guarded the day it lands.

The lint checks the guard as well as the default, because the default is only
half the hole. A **new job** — or a whole new reusable workflow — that checks
out at `ref: ${{ inputs.workflows_ref }}` without the guard reopens the `ref: ''`
default-branch fallback, and a default-only lint stays green throughout: there
was never a `default:` to find. So every such checkout must be preceded, *in its
own job*, by the guard step (a guard in job A does nothing for job B). An
exempt workflow is not held to this — it still has its default, so an omitted
input can never arrive as `''` — which puts it back under the check the moment
its own ticket drops the default.

Two shapes the text parser is deliberately strict about: a `default` inside a
flow mapping (`workflows_ref: {type: string, default: main}`) is caught even
though it has no child lines to walk, and a file that *uses* `inputs.workflows_ref`
but whose declaration the parser cannot locate is a hard **error**, not a quiet
skip — "not applicable" and "I could not read this" must never look the same,
or a shape the parser trips on drops out of coverage with CI still green.

The ref-checkout detector recognises all three YAML spellings of the same
checkout — block (`ref: ${{ … }}`), flow (`with: {…, ref: "${{ … }}"}`), and a
value carried on the following line (`ref: >-`, `ref: |`, or a bare `ref:`).
They are one checkout to Actions, so a detector that knows only one of them
reports an unguarded job as clean. Do not "simplify" the extra patterns away.
The guard's own signature is matched in block form **only**, deliberately: a
guard written in flow style reads as ABSENT and fails loudly, which is the right
bias for a check whose whole job is noticing an absence.

`KNOWN_EXEMPT` in the script carries workflows with the same debt that are
tracked under their own ticket. It is **empty today** — `pr-size.yml`, its last
entry, was fixed in BE-5858 once its caller fleet had been audited, so no
reusable workflow here is carved out of either check. The lint fails on a
**stale** entry so the list drains itself rather than rotting — whether the
workflow dropped its default (fixed) or no longer exists under that name at
all (renamed or deleted), the latter being
the case that would otherwise silently pre-exempt whatever later reuses the
filename. The list is only applied to this repo's own `.github/workflows`: run
against an ad-hoc `--workflows-dir` every entry would look stale.
