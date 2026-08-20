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
the paragraph above already leaned on them.

Three things make that enforceable, and each was a hole on its own:

- **The guard-step detector knows both bindings, and keeps them apart.** It
  accepts `WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}` as
  well as the bare `${{ inputs.workflows_ref }}`, because matching only the bare
  form meant none of `groom.yml`'s seven guards was ever consulted. But the two
  are **not** equivalent, and treating either as blanket job-wide coverage is a
  live hole: a guard on the fallback proves only that the *OR expression* is
  non-empty, so with the input omitted it passes on `job.workflow_sha` while a
  sibling `ref: ${{ inputs.workflows_ref }}` in the same job still gets `''`.
  So the lint records each guard's **strength** and requires the checkout's own
  `ref:` to be no weaker: a bare guard covers everything, a fallback guard
  covers fallback checkouts only.
- **The fallback pattern is anchored to the whole YAML value**, not merely to
  the `${{` … `}}` interpolation, and matched against the comment-stripped line.
  Anchoring the interpolation alone still accepted a mutable ref, and the
  runtime guard cannot catch one (a guard proves non-emptiness, not
  immutability). All of these are *not* self-pins:
  `${{ inputs.workflows_ref || job.workflow_sha || 'main' }}` (resolves to the
  default branch in exactly the pre-v2.334.0 case the fallback exists for),
  `${{ inputs.override || inputs.workflows_ref || job.workflow_sha }}`
  (resolves to whatever the leading operand names),
  `refs/heads/${{ … }}` and `${{ inputs.override }}${{ … }}` (buried in a
  concatenation), and a flow mapping whose *sibling* entry — not its `ref:` —
  carries the fallback. A comment merely *naming* the expression buys nothing
  either. Three matchers do this, mirroring the `_REF_USE_*` trio: block, flow
  (bounded at the entry boundary, exactly as `_REF_USE_FLOW_RE` bounds its own
  value), and a continuation line that *is* the expression.
- **The `default: ''` carve-out is scoped to actual ref checkouts**, and it asks
  the *parser* rather than re-reading each line. Asking "does any line
  self-pin?" of the whole file granted it to any file that merely mentions the
  expression in code — most sharply the guard steps' own `env:` binding — so a
  file whose checkouts all read the bare `ref: ${{ inputs.workflows_ref }}`
  bought an empty default it does not self-pin against. Re-deriving it per line
  went too far the other way: no single line satisfies both halves of the
  block-scalar spelling (`ref: >-` with the expression below it), so a file that
  genuinely self-pins that way lost the carve-out and got BE-5546's "delete the
  default" while its checkouts got BE-8077's "the fallback IS recognized". It
  was also quadratic — a full alias scan per line, on a 3,000-line groom.yml.

`env_aliases` and `fallback_env_aliases` are a deliberately mismatched pair,
because they answer questions that fail in opposite directions:

- `env_aliases` — "does this `env:` binding REACH the input?" — matches **any**
  value mentioning `inputs.workflows_ref`. Enumerating blessed spellings made an
  unrecognized one fail *open*: `WORKFLOWS_REF: ${{ inputs.workflows_ref ||
  'main' }}` registered no alias, so `ref: ${{ env.WORKFLOWS_REF }}` read as no
  ref use at all and left the lint entirely, carrying the exact mutable fallback
  the lint exists to catch. Over-approximating here can only ever *demand* a
  guard.
- `fallback_env_aliases` — "is this binding the immutable fallback?" — matches
  only the exact two-operand spelling, because it grants an *exemption*. That
  strength then travels to every `ref: ${{ env.NAME }}` reading the name.
  Without it the alias was registered but scored **bare**, so the hoist below
  was reported as an unguarded bare checkout with BE-5546's message on it.

So hoisting that seven-times duplicated binding to a shared `env:` and checking
out at `ref: ${{ env.WORKFLOWS_REF }}` — the refactor the alias machinery exists
to survive — keeps both its coverage and its strength.

**Keep that `env:` at STEP level.** The `job` context is not available in
`jobs.<job_id>.env`, so a job-level hoist of the *fallback* spelling is not a
refactor the lint mislints — it is an invalid workflow. Verified with
`actionlint` 1.7.12 on `${{ job.status }}` (a `job` property its schema does
know, isolating context availability from the `job.workflow_sha` staleness
noted next): rejected at job level with *"context "job" is not allowed here"*,
accepted at step level. All seven of groom.yml's bindings are step-level today.
A job-level `env:` may still bind the **bare** input — `env_aliases` registers
it and the checkouts below it then need a bare guard, which is the correct
answer. (`actionlint` ≤ 1.7.12 also false-positives on `job.workflow_sha`
itself, whose `job`-context schema predates runner v2.334.0; no CI here runs
it.)

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
