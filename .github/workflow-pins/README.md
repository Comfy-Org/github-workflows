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

**That resolve-then-consume idiom is LINTED, not exempt (BE-8130).** Its
checkout reads `ref: ${{ steps.<id>.outputs.<name> }}`, which names no input —
so it used to leave the lint entirely, with a hand-written `if:` as the only
thing between an unresolvable ref and a silent default-branch checkout of the
scripts the job executes. The detector now follows a `ref:` through the step
output: when the producing step's `env:` binds `workflows_ref` *in one of the
two literal spellings below*, the checkout is a ref use, and it is covered only
if the consuming step carries exactly
`if: steps.<id>.outputs.<name> != ''` on that same output — bare or
`${{ … }}`-wrapped, either spelling optionally double-quoted. A fail-closed
resolver does not exempt its consumer (BE-8221): a guard proves the step
rejects an empty *input*, not that its *output* is non-empty — a
sanitize-to-`''` branch after the guard, or a dropped or renamed
`$GITHUB_OUTPUT` write, still emits `''`. Nothing wider than the exact `if:`
counts: `... != '' || always()` runs the checkout precisely when the output is
empty, an `if:` on a different output says nothing about this one, and a
job-level `if:` skips the resolver too. A `ref:` resolved from a step that never
touches `workflows_ref` is not this lint's subject and is left alone.

Two preconditions decide whether the detector treats the resolver as one, so
copy the idiom with both intact. The second is now an error in its own right
(below), but the FIRST is still silent — an unrecognized *binding* drops the
consumer out of coverage rather than raising a different error:

- **The binding is matched literally.** The producing step's `env:` must bind
  `WORKFLOWS_REF: ${{ inputs.workflows_ref }}` or
  `WORKFLOWS_REF: ${{ inputs.workflows_ref || job.workflow_sha }}`. A different
  env name, or a third operand such as `|| 'main'`, registers as no resolver.
  (`test_the_ledger_resolver_carries_the_shape_check` pins that binding for
  `cursor-review.yml`, since the lint cannot.)
- **The resolver is an EARLIER step in the SAME job.** Jobs run independently,
  and a resolver declared below its consumer cannot have run first, so neither
  is credited — the same rule the guard steps already follow.

A step written as ONE flow mapping that both binds `WORKFLOWS_REF` in its `env:`
and reads a step output in its `with: {ref: … }` is judged like any other
step-output checkout (BE-9098) — the binding on that line is not what the line
gets judged as, so the `ref:` still earns its own verdict instead of slipping
through unjudged.

**A `ref:` naming a nonexistent or later step is an ERROR in its own right
(BE-8215).** A step-output `ref:` whose `<id>` matches no step declared before
the consuming one — a typo'd id, a step in another job, a resolver declared
below its consumer — used to be silently dropped as "not this lint's subject".
At runtime that expression is `''`, which `actions/checkout` reads as the
default branch, so the checkout runs **unconditionally** at a mutable ref — the
exact hole this lint exists to close, on the one spelling where it was
fail-open. A per-job step-id pre-scan now separates the two cases: a `ref:`
resolved from an EARLIER step that exists but never touches `workflows_ref`
stays out of scope — in **any** operand position, since the lint has no claim
on that step either way — while a dangling id fails with its own message (the
BE-8130 remedies don't apply — adding the `if:` on a nonexistent output guards
nothing that will ever run; the fix is the id itself, or the step order). Only
a `ref:` that is a step's `with:` INPUT is judged this way — a job-level
`outputs:` mapping or a `ref:` line inside a `run:` heredoc is not a checkout
and is left alone, as it always was — including a heredoc emitting a whole
STEP, where the enclosing key is a `with:` the script itself printed: an open
`|`/`>` block scalar is tracked so its body is read as text, not structure.

**That pre-scan reads step ITEMS, not lines (BE-8254).** An id counts as
declared only where a step actually declares one — at the item's own key
column, or inside a flow mapping the item opens (`- {id: x, …}`, including the
multi-line spelling). Two shapes used to register as phantom steps and so
SILENCE the dangling verdict on a real finding: an action input literally named
`id:` under an earlier step's `with:` (nested deeper than the step's keys), and
a `run:` block scalar whose heredoc emits a line beginning `- id:` / `"id":`
(fixture YAML this repo's own workflows write). Both are excluded structurally
now — YAML puts a `with:` member and a block scalar's body deeper than the key
column either way. One residue is TOLERATED rather than fixed: a `with:`
written inside a FLOW item (`- {uses: …, with: {id: x}}`) still registers `x`,
because the flow pattern is flat and scoping it to the outermost mapping's
depth is the under-collection direction — get that wrong and a compliant
workflow fails. It is pinned by a test so any change to it is a deliberate
one; the shape occurs 0 times in this tree.

Reading items rather than lines means the walk has to accept every spelling of
an item YAML does, because a missed id is a false FAILURE on a compliant
workflow. It reads the **indentless** sequence (`steps:` and its `- ` items at
the SAME column, which Actions accepts), a marker line whose remainder is only
a comment (`-   # set up`, whose keys land on the lines below), a bare `-`
whose item is a flow mapping on the next line, an item carrying an `&anchor` or
`!tag` node property in front of its mapping (`- &resolver {id: x, …}`), and a
flow mapping spanning physical lines — including one whose continuation dedents
past the dash, which YAML permits inside `{ … }`. Braces are counted **outside
quoted scalars**, so a `{` or `}` that is string content (`run: "echo {"`)
neither wedges flow mode open nor closes it early. An `id:` inside a flow
mapping is gated the same way: the pattern asks only for a preceding `{` or
`,`, and a comma that is string content meets that boundary too, so
`- {run: "build, id: x"}` used to conjure a step nothing declares and silence a
real dangling verdict.

**A quote opens a scalar only where a YAML node can START** — at the start of
the text, or after `{`, `[`, `,`, a `:` key separator, or a `- ` list marker.
YAML forbids a quote only as a plain scalar's FIRST character, so `don't` is a
legal plain scalar and `- {name: don't, id: real}` a legal step; a bare
open-on-any-quote toggle read that apostrophe as opening a string, swallowed
the real entry comma before `id:` (so `real` was never registered — a false
`dangling` FAILURE on a compliant workflow) and left the closing `}` uncounted,
wedging flow mode open so every later block item was lost too. One scan
(`_quote_mask`) answers this for all three readers that need it —
`_outside_quotes`, `_strip_comment` and the flow `id:` boundary — and it is
hoisted per line rather than re-run per match, so a long flow line with many
`id:` candidates costs O(len) rather than O(matches x len).

A step id is refused outright if it still carries a quote once its closing flow
punctuation and one MATCHED quote wrapper are stripped: an Actions step id is
`[A-Za-z0-9_-]`, so a stray `'`/`"` marks the tail of a quoted scalar that
opened on an EARLIER physical line (`- {name: "a` / `  id: phantom", …}`) —
which would otherwise register the exact phantom this pre-scan exists to
exclude. Cross-line quote state itself remains out of scope module-wide (every
reader here is per-line, `_strip_comment` included, and it runs first), so one
residue survives: a continuation line whose id value carries no quote at all
still over-collects. That is the tolerated direction — it silences one site,
exactly as a real earlier out-of-scope step does — and it is pinned by a test;
the shape occurs 0 times in this tree.

**A `steps:` the item walk cannot read makes that job's dangling verdict
fail-open, not fail-loud.** Flow-style `steps: [ … ]` on the key line, or a
`steps:` opening no `- ` item, leaves the pre-scan with no answer at all — so
for that job (only) a site whose id names no tracked resolver is dropped,
exactly as it was before BE-8215. That is every verdict resting on "no step of
that id exists": `dangling`, and the `non-leading` one an absent id also
reaches — the same pair a real earlier out-of-scope step drops today. Under-
collection is the costly direction here: a missed id turns a compliant
workflow into a false FAILURE, while a dropped site merely reproduces the
coverage this lint had before the dangling check existed. Resolver and guard
verdicts are unaffected, in that job and every other.

The fail-open is deliberate but it is not free: reformatting a job's `steps:`
into one of those shapes is an in-band way to switch that job's dangling check
off. So the CLI now SAYS SO (BE-9045). `check_dir` returns a fourth value,
`notices`, alongside `(errors, checked, exempt_ok)` — annotation-ready strings
that never touch the exit status — and `main` prints them ahead of any errors.
One `::warning` per JOB (not per site, and `::warning` rather than `::notice`
so it appears in the PR annotations), naming the job, pointing at its `steps:`
line, and counting the `ref:` SITES that went unjudged — one per `ref:` line,
this lint's unit throughout, so two flow-style checkouts on one physical line
count once. It fires ONLY where the escape actually cost a site its verdict:
a flow-`steps:` job that reads no `steps.<id>.outputs.<name>` never reaches
the drop and stays silent, and a site that still came away with a verdict —
because a SIBLING operand named a tracked resolver — is not counted either,
so one `ref:` line can never carry both an `::error` and a `::warning` saying
it went unjudged. So the warning marks lost coverage rather than a shape.

Lost coverage is all it marks. A counted site is not an accusation: with the
pre-scan defeated, a dangling id and a real earlier step this lint has no
claim on are indistinguishable, and the readable path drops that second one
silently too — so the message says the check *could not run*, not that the
refs are wrong. The OK summary drops its "every ref checkout guarded" claim
for "every JUDGED ref checkout guarded" whenever a warning fired — the run
still exits 0, but it no longer reports coverage it did not have.

**`||` fallbacks are read on this path too, with leading-operand judgment
(BE-8215).** `ref: ${{ steps.<id>.outputs.<name> || 'main' }}` used to match
nothing anywhere and record nothing. Now it is a site like any other, judged
by operand order exactly as the input side is: when the FIRST `||` operand is
the step output, the site is covered the usual way — the exact `if:` on the
consuming step (BE-8221; a fail-closed resolver does not exempt it), under
which the fallback arm is unreachable dead code, so it passes; when the
leading operand is anything else
(`${{ 'main' || steps.<id>.outputs.<name> }}`), it wins on every runner and the
site is unguarded unconditionally — with its own
message, because neither BE-8130 remedy can fix operand order. A leading
operand `||` falls *through* (`false`, `''`, `""`, `0`, `null`) still reaches
the output and is judged normally.

**A spelling the reader cannot parse is REFUSED, not skipped (BE-8253).** The
reader is two-tier: a permissive tier asks whether the comment-stripped `ref:`
value mentions `steps.<id>.outputs.<name>` at all, and the strict tier reads
every interpolation and every top-level `||` operand in it. When the first
counts more mentions than the second could account for, the site is reported —
the lint says it cannot judge this expression and names the spellings it can.
Unrecognized spellings include a fallback containing a brace
(`|| format('refs/heads/{0}', 'main')`), a parenthesized operand
(`(steps.<id>.outputs.<name>) || 'main'`), a TRAILING `&&`
(`${{ steps.<id>.outputs.<name> && 'main' }}`), and — in the flow form only,
whose interpolation body still stops at a comma even though `BE-8253` made its
*entry* boundary interpolation-aware — a fallback whose literal contains one
(`with: {ref: "${{ steps.<id>.outputs.<name> || 'a,b' }}"}`; the block form
reads this spelling fine). Each of those used to match nothing and record no
site, so the lint PASSED a workflow it had never read while failing the
identical one spelled bare; now the site is reported `'unparsed'` and refused.
A *leading* `&&` is the opposite case, not this one: it MATCHES, nothing but an
`||` chain clears the leading-operand check, and a site the lint has a claim on
at all — one naming a resolver, or a dangling id — fails as `non-leading`, red
CI rather than silence. Write the ref as a bare
`${{ steps.<id>.outputs.<name> }}` under the exact `if:`, or as that expression
with a trailing `|| <literal>` fallback, and it is covered. The reader does not
evaluate `format()`, `&&` or parenthesized expressions on purpose: an unknown
spelling asks its author for a supported one rather than growing an expression
parser inside the lint.

**Every operand is judged, and the strongest requirement wins (BE-8253).** A
`ref:` value can read more than one step output, and each one is a ref the
checkout can really resolve to — so each must independently pass (declared as
an earlier step, covered by a fail-closed resolver or the exact non-empty `if:`,
and reachable by operand order). A covered sibling excuses nothing. Two shapes
used to slip through on a single verdict per line:
`ref: "${{ steps.a.outputs.ref }}${{ steps.b.outputs.ref }}"`, where only the
LAST interpolation was judged even though both feed the value, and
`ref: ${{ steps.a.outputs.ref || steps.b.outputs.ref }}`, where the fallback
stretch swallowed `b`. A step output ahead of another operand does not make it
non-leading: an unresolved output is `''`, so `||` really can fall through to
what follows — and that leading output is judged in its own right.

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

**`env:` aliases are recognized as ref USES, never as self-pins.** The two
questions fail in opposite directions, so they get opposite answers:

- *Does this `env:` binding reach the input?* — `env_aliases` matches **any**
  value mentioning `inputs.workflows_ref` (comment-stripped, so prose in an
  unrelated value cannot bind a name). Enumerating blessed spellings made an
  unrecognized one fail *open*: `WORKFLOWS_REF: ${{ inputs.workflows_ref ||
  'main' }}` registered no alias, so `ref: ${{ env.WORKFLOWS_REF }}` read as no
  ref use at all and left the lint entirely, carrying the exact mutable fallback
  the lint exists to catch. Over-approximating here can only ever *demand* a
  guard. Three more spellings joined it in BE-8146, each of which had been
  failing open the same way — and none of them was caught by the `_CONSUMES_*`
  backstop, which only runs when the input *declaration* is unparseable:
  - **the flow-style `env: {NAME: ${{ … }}}`**, walked structurally rather than
    through a single bounded regex: a `,` or `}` only ends an entry (or the
    mapping) when it sits outside a quoted scalar and outside a nested
    `${{ … }}`, so a quoted value with a comma or brace of its own
    (`format('{0}', …)`, two interpolations in one string) still binds, and a
    genuine decoy — a `,` inside a *quoted* scalar planting a fake key — still
    does not. The mapping itself is not required to close on the line that
    opens it either: `env: {` alone, with its entries on the lines below, is
    walked the same way.
  - **index access**, `inputs['workflows_ref']` and `env['NAME']`, tolerant of
    whitespace on either side of the bracket (`inputs ['x']` is the same access
    as the tight spelling) and of the YAML single-quote escape (`''` doubles to
    a literal `'`, so `'${{ inputs[''workflows_ref''] }}'` — the whole value
    forced single-quoted — decodes to the same access too). It is documented
    Actions expression syntax and interchangeable with the property form, so
    both spellings now come from one shared `_INPUT_MENTION_BODY` used by every
    "reaches the input" pattern — including the `_CONSUMES_*` backstop, so a
    bracket-only file with a lost declaration is still loud.
  - **a chain of aliases**: `BASE: ${{ inputs.workflows_ref }}` then
    `REF: ${{ env.BASE }}` binds *both*, via a fixpoint bounded by the number of
    names bound in the file. One hop was all the scan followed, so a `ref: ${{
    env.REF }}` two hops out was invisible.
  - **a hyphenated name** (`WORKFLOWS-REF`), which the bracket form exists to
    read back (`env.WORKFLOWS-REF` is not valid property-access syntax), and a
    value that continues on the line *below* its key (`REF:` / `REF: >-`) —
    the same shape `ref:` itself already followed.

  All of these widen only what counts as a ref **use**, and `_GUARD_BINDING_RE`
  picked up the accessor half of it too (BE-8146): it is also the only thing
  that registers a step as a *resolver* for the resolve-then-consume idiom
  below, and an unrecognized accessor there does not fail loud — it drops the
  step out of `resolvers` entirely, and every checkout resolved from its
  output vanishes from the lint rather than being reported. What stays exact
  is the `fallback` group (`|| job.workflow_sha`, no other spelling): that is
  what proves *immutability*, a question the accessor spelling has nothing to
  do with. An unrecognized *guard* still fails **closed** — its checkout gets
  reported — while an unrecognized *use* fails open and silent, which is why
  the flow *form* (as opposed to the accessor spelling) stays unrecognized for
  the guard binding on purpose.
- *Is this ref the immutable self-pin?* — judged from the **literal expression
  on the checkout line**. An alias never qualifies, so `ref: ${{ env.NAME }}`
  always needs a **bare** guard.

Carrying an alias binding's strength to the checkout was tried and reverted.
`env:` is scoped per step and per job and it *shadows*, while these scans are
file-wide, so a file-wide "names bound to the fallback" set granted the
exemption at checkouts the binding never reaches — in both directions. A
binding in a *guard step's* `env:` is invisible to the sibling checkout step at
run time, so `ref: ${{ env.NAME }}` there expands to `''` and takes the default
branch while scoring as a guarded self-pin; and a step-local
`WORKFLOWS_REF: ${{ inputs.workflows_ref || 'main' }}` inherited fallback
strength from any *other* step binding that name strictly. `cursor-review.yml`
binds `WORKFLOWS_REF` both ways today (line 420 with the fallback, six more
without), so that cross-talk was not hypothetical.

**The fallback cannot be hoisted to a shared `env:` at all**, so there is no
refactor left for strength propagation to serve. The `job` context is not
available in `jobs.<job_id>.env` — verified with `actionlint` 1.7.12 on
`${{ job.status }}`, a `job` property its schema *does* know, which isolates
context availability from the `job.workflow_sha` staleness noted below:
rejected at job level with *"context \"job\" is not allowed here"*, accepted in
a step's `env:`. And a step-level `env:` does not reach a sibling step. So
groom.yml's seven duplicated bindings are duplicated of necessity. A job-level
`env:` may still bind the **bare** input; the checkouts below it are then
required to carry a bare guard, which is the correct answer.
(`actionlint` ≤ 1.7.12 also false-positives on `job.workflow_sha` itself, whose
`job`-context schema predates runner v2.334.0; no CI here runs it.)

**The leading operand has to reach the input.** A guard proves the *input* is
non-empty; it says nothing about an expression that never reaches the input.
GitHub's `||` returns the first **truthy** operand, so
`ref: ${{ 'main' || inputs.workflows_ref }}` mentions the input — making it a
ref use that clears the guard — while resolving to a mutable branch on every
runner, with no second input declaration involved. `check_dir` cannot see it
either; it reads only `workflows_ref`'s own `default:`. So the first `||`
operand of the ref expression must reach the input, or no guard in the job
covers that checkout.

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
The guard's binding is now RECOGNIZED in both block and flow form (`env:
{WORKFLOWS_REF: …}`), so a flow-form resolver still registers (BE-8221) — an
unregistered one left its consumer's step-output checkout unjudged entirely,
a silent pass. But recognition is not credit: only the block form can earn
`guarded_input`/`guarded_fallback` and excuse a checkout that consumes the
INPUT directly — widening what counts as a use may only ever DEMAND a guard,
never widening what counts as a guard to EXCUSE one, so a flow-form guard on
a *direct* checkout still reads as ABSENT and fails loudly.

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
