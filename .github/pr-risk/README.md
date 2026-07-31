# `.github/pr-risk/` — the PR risk grader

Logic behind [`pr-risk.yml`](../workflows/pr-risk.yml), the **non-blocking**
risk check. It grades a pull request's net diff into a tier — `R0` (trivial) to
`R3` (needs a careful read) — and publishes that grade as a label, a Check Run,
and a sticky comment, so a deep review queue can be sorted by how much attention
each change actually needs:

```text
is:pr is:open label:risk:R0
```

**Nothing is gated.** The Check Run's conclusion is hardcoded `neutral`, the
grader always exits 0, and every publish step is best-effort. This check cannot
fail a PR, cannot block a merge, and no automation consumes the label — it is
for humans to filter by.

Like every other workflow here, these scripts are the single source of truth and
are loaded at run time from a **pinned ref of this repo**, never from the
caller's checkout, so a pull request cannot rewrite the logic grading it.

## Files

| File | Role |
|---|---|
| `grade-risk.sh` | CLI entrypoint. Resolves the three-dot (net) diff and writes `risk-report.json`, `risk-comment.md`, `risk-check.md` into `--out-dir`. Always exits 0 — an unreadable diff produces an **unknown** report, never a default tier. Shared entrypoint for CI and for an offline backfill over merged history (BE-5507). |
| `grade_risk.py` | The grader + renderers. Pure, stdlib-only, no network: parses `git diff --numstat -z`, applies the risk map, and renders the comment and Check Run text. |
| `publish_risk.py` | The publisher. Reconciles the `risk:R*` label, creates the Check Run, upserts the sticky comment, and (`dispute` subcommand) records the "this grade is wrong" checkbox. Runs in the job that holds the write token and checks out no PR code. |
| `tests/` | `python3 -m unittest discover -s .github/pr-risk/tests -p 'test_*.py' -v` |

## How a tier is decided

1. **Every changed file is classified** against `RISK_RULES` in `grade_risk.py`,
   first match wins. The ordering is deliberate: tests first (a test for a
   sensitive surface is still a test), then docs (`docs/auth.md` is prose *about*
   a sensitive surface, not the surface), then sensitive surfaces
   (migrations, CI/CD, IaC, auth, secrets, billing), then `R2` for ordinary
   source.
2. **A very large single file escalates one tier** (`FILE_ESCALATE_LINES`) —
   size is itself an attention signal, independent of what the file is.
3. **The PR's tier is the MAX of its file tiers.** One `R3` file makes the PR
   `R3` no matter how much `R0` surrounds it.
4. **A very large whole diff escalates one more tier** (`SIZE_ESCALATE_LINES`),
   capped at `R3`.
5. The comment then reports the **concentration** — how much of the diff
   actually sits at the top tier — so a reviewer sees that the 6% which made it
   `R3` is two files and 40 lines, and can judge accordingly.

### The risk map is a first cut

`RISK_RULES`, `FILE_ESCALATE_LINES` and `SIZE_ESCALATE_LINES` are a **documented
placeholder**, not derived thresholds. BE-5507 (offline grader + backfill over
merged history) owns deriving and tuning them; when it lands, replace those
three in place. Nothing else in this directory or in `pr-risk.yml` depends on
their values, and nothing anywhere is gated on the result — a wrong threshold
costs a mislabelled PR and nothing else.

The `risk-grade-disputed` label is the feedback channel for exactly this: a
reviewer who thinks a grade is wrong ticks the checkbox on the sticky comment,
which labels the PR, giving a queryable stream of real disagreement to tune the
map against.

## Re-grading is the point

The failure mode this check exists to avoid is a grade that goes **stale** — a
classifier that runs once, marks a PR extra-small, and never re-analyses it
while the PR grows into several thousand lines. A stale grade is worse than no
grade, because it spends trust that does not come back.

So the caller **must** include `synchronize` **and `edited`** in its trigger
types, and the label is recomputed and *replaced* on every run: exactly one
`risk:*` label at any time, stale tiers removed. A PR that grows from `R0` into
`R3` carries `risk:R3` and only `risk:R3`.

`edited` is the less obvious half. **Retargeting** a PR changes its base — and
therefore its three-dot diff and its grade — without moving the head SHA and
without firing `synchronize`, so an author could otherwise hold a low tier by
rebasing the base away. The publisher refuses to write a label or a comment
computed against a base the PR no longer targets (`--base-ref`, compared with
the PR's live base), so a caller missing `edited` leaves that PR carrying its
pre-retarget tier until the next push rather than silently re-asserting it.

## Unknown is published as unknown

A PR the grader could not read (bad refs, a git failure, a malformed diff) gets
**no tier**. It is published as `unknown` in the Check Run and the comment, and
carries no `risk:*` label — never defaulted to `risk:R0`, never silently
skipped. Silently defaulting to the safest tier is the same trust-spending
failure as a stale grade.

## What a PR cannot do to its own grade

The grader reads PR-authored content (paths, `.gitattributes`) and writes into a
bot-authored comment, so a few things are deliberately not taken on trust:

- **The sticky marker is public**, so `find_sticky` needs three things to agree
  before it adopts a comment: a `user.type == 'Bot'` author, an author *login*
  that is `github-actions[bot]` or the publishing app's `<slug>[bot]`, and the
  marker on the **first line** of the body. Bot *type* alone is not identity —
  every other GitHub App installed on the repo is a Bot too, and whichever one
  sorted first would be adopted permanently, with every re-grade PATCHing over
  its body (or 403ing forever) and the dispute checkbox read back out of a
  foreign comment. Login alone is not enough either: every other
  `GITHUB_TOKEN` workflow in the repo also posts as `github-actions[bot]`, and
  one that *quotes* our comment carries the marker — nested in its own prose,
  which is what the first-line test rules out. Without all three, a PR author
  could pre-post a comment carrying the marker and have the publisher overwrite
  it, inheriting control of the preserved dispute checkbox.
  The app login comes from the minted token's `app-slug`, so on the degraded
  path where the mint itself failed the publisher falls back to
  `github-actions[bot]` and would post a second comment rather than adopt the
  app's. That is the deliberate trade: a duplicate comment in an already-
  degraded run, instead of a foreign bot's comment being adopted permanently.
- **Paths and reasons are escaped before rendering.** Git permits `|`,
  backticks and newlines in a filename; unescaped, such a path breaks out of
  its table cell and can forge a ticked "this grade is wrong" line in the bot's
  own comment, or inject a remote image that logs reviewer IPs. An UNKNOWN
  report's reason gets the same treatment — it carries git's stderr, which
  quotes PR-authored path names. The body is length-bounded too, so a diff of
  very long paths cannot 422 the upsert and freeze the comment.
- **The fallback renderer runs `python3 -I`.** Reading the program from stdin
  would otherwise put the process CWD — the PR's checkout — at the front of
  `sys.path`, so a PR shipping a top-level `json.py` would execute its own code
  in the job that authors the report.
- **The dispute checkbox is matched by comment id**, not by the marker alone,
  so another bot quoting our comment cannot set — or clear — the
  `risk-grade-disputed` label.
- **The label the publisher applies is re-validated** against the same
  `risk:R<n>` pattern reconciliation uses, so a malformed report cannot make
  the privileged job create an arbitrary label that nothing later cleans up.
- **`.gitattributes` is read from the BASE ref** (`git --attr-source`), so a PR
  that adds `* -diff` cannot make numstat report `-` for every file and zero out
  its own size escalation. Same guard `pr-size.yml` applies to
  `linguist-generated`. Needs git >= 2.42; support is probed explicitly (rather
  than inferred from a failed diff, which would silently restore the bypass on
  any unrelated error), and an older runner sets `attr_source_degraded` in the
  report, which both renders surface.
- **The publisher re-checks the PR head SHA** before touching the label or the
  comment, so a delayed, superseded run cannot republish a stale tier over a
  newer one. Its Check Run still publishes — that one is per-commit.

## Adoption

See the caller pattern in the header comment of
[`pr-risk.yml`](../workflows/pr-risk.yml). Two things to know before enrolling:

- **`mode` defaults to `shadow`** — Check Run only, no label and no comment. A
  repo can accumulate grades on its commits and check them against reviewer
  intuition before any reviewer-facing surface changes. Flip to `mode: publish`
  when the grades look right.
- **The calling job must grant `checks: write` + `pull-requests: write`** (plus
  `contents: read`). GitHub rejects a caller that grants less than a called job
  requests, at startup, even in shadow mode. Supply `bot_app_id` +
  `BOT_APP_PRIVATE_KEY` to publish as your app — **required for fork PRs**,
  whose `GITHUB_TOKEN` is read-only.
