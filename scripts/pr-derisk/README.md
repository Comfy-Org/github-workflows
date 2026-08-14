# pr-derisk — the `/derisk` split planner (beta)

The scripts behind [`pr-derisk.yml`](../../.github/workflows/pr-derisk.yml), rung **v1** of the
PR risk ladder and the first rung with a model in it.

[pr-risk](../pr-risk/README.md) (v0) answers *how risky is this PR, and which files hold the
grade up*. What it cannot answer is the part authors skip: the concrete, ordered partition of the
diff into a chain where **one small PR concentrates the risk** and everything else lands in a
cheaper lane. That is a semantic judgement, so it is the one place a model is used — and it is
used for that only.

```
/derisk  →  re-grade the PR  →  ONE model call: which files go together?
                                      ↓
                     validate the partition (exact coverage, or re-prompt once)
                                      ↓
                     grade-pr-risk.sh --stdin computes EVERY step's path floor
                                      ↓
                            one sticky advisory comment
```

## The division of labour, and why it is the whole design

The model proposes a **partition**: which files go in which step, in what order, and why each
step is inert. **It never states a tier.** Every floor rendered in the comment is computed by
`grade-pr-risk.sh --stdin` over a synthetic scorecard record built from that step's files — the
same deterministic judge, the same map, the same rules that graded the PR. A model that
hallucinates "this split lands R0" cannot put that number in front of a reviewer: the plan object
is rebuilt field by field from a fixed list, so a model-claimed `tier` key does not survive.

That is also why this lives *outside* the grader rather than inside it. pr-risk's grading path
stays LLM-free and auditable; this reads its output and adds a suggestion. Nothing here can
change a grade, a label, or a check.

## What a computed floor is, and is not

`grade = worst(path_floor, provenance, reversibility)`, and only the **path** axis is a function
of which files a PR contains. So a split's path floor is computable today and the other two axes
are not: provenance follows the author into the split PR (a narrower path set can newly assert —
or newly fail — a runbook shape), and reversibility keys on a check rollup that has not run yet.
The comment therefore shows a **floor with its assumptions named, never a promised grade** — the
same wording discipline the v0 reducibility readout landed with — and it is deliberately **not
clamped** to the graded PR's other two axes, because both are re-derived per split PR and can move
either way. A split can land *worse* than its floor; it can never land better.

## Honesty rules, and where they are enforced

A single-class monolith — every file already at the headline tier — has no lane win available. The
verdict line above the fold is computed from the **floors**, not from the model's prose, so in
that case it reads "N smaller single-concern R3s, same lane" and there is no wording available to
it that claims a reduction. A prompt can *ask* for that; only the renderer can guarantee it.

Two rules the plan text carries, because they are how a split plan goes wrong:

- **Chain linkage.** Every split PR links the whole chain, and the risk-carrying PR's stated
  review scope is **the chain**, not its own diff. A split that lets a risky change be reviewed as
  a small one is risk laundering.
- **Sequential, never stacked.** The steps are sequential PRs to the default branch in dependency
  order. A stacked branch whose base is deleted on merge is a known footgun and no plan here
  proposes one.

## Failure modes are outcomes, not silence

Someone typed a command and is waiting, so every path ends in a comment:

| what happened | what the reader gets |
|---|---|
| diff over `max_diff_bytes` | deterministic fallback — "too large to plan", pointing back at the v0 reducibility readout |
| partition invalid twice | fallback naming the specific missing / duplicated / invented paths |
| the PR is `unknown`, or changes <2 files | fallback — there is nothing to partition |
| the grader could not compute the floors | fallback — a plan with an un-computed floor is not shown at all |
| model or API failure | an explicit failure comment |

## Prompt injection is contained by shape

The diff, the filenames and the model's own output are all attacker-influenceable, and none of
them executes. They are interpolated into a prompt (by `jq --rawfile`, never by string
concatenation) and into markdown, where every PR-controlled string is escaped exactly as
`publish-risk-surfaces.sh` escapes one — newlines flattened first, every CommonMark-escapable
punctuation character backslashed, a path containing a backtick or backslash demoted to plain
text. Nothing shells out with model output, nothing is filed, and the only write in the whole run
is one PR comment. The worst a crafted diff can do is talk the planner into a silly partition,
which is then rejected unless it covers the changed-file set exactly.

## Files

- `collect-pr-inputs.sh` — re-grades the PR with the pr-risk grader and fetches the capped diff.
  It **sources** `grade-targets.sh` for `resolve_base_ref` / `fetch_override` rather than
  reimplementing them, so the rules that judge a split are resolved by the one implementation that
  resolved the rules that judged the PR.
- `plan-derisk.sh` — the single model call, the partition validation + one retry, and the
  grader-computed floors. Emits one plan JSON object. `MODEL_RESPONSE_FILE` is the hermetic test
  surface: it reads the reply off disk (one line per attempt) and makes no network call.
- `publish-derisk-comment.sh` — renders the plan as markdown and upserts the sticky comment
  (`<!-- ci-pr-derisk -->`, its own first line, matched by the same backwards page scan pr-risk
  uses). `DRY_RUN=1` renders to stdout and writes nothing.
- `resolve-enabled.sh` — the `enabled` input / `vars.DERISK_CONFIG` switch, degrading toward the
  reviewed value and never toward off.
- `tests/` — one hermetic suite, run by
  [`test-pr-derisk.yml`](../../.github/workflows/test-pr-derisk.yml). It also pins the
  `workflows_ref` guard in `pr-derisk.yml` byte-identical to `pr-risk.yml`'s, which is what
  extends that guard's own 40-odd assertions to this workflow without forking them.

## What is deliberately NOT here

No auto-running on every R3 (on-demand only in beta), no ticket filing from a plan (its own rung,
behind its own command), no routing, no check, no label, no auto-merge. The offer threshold and
which repos see it at all are post-merge flips of a repo variable, not code.
