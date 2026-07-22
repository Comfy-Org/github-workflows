# refresh-reviewers — recompute the reviewer expertise map from git history

The engine behind [`refresh-reviewers.yml`](../workflows/refresh-reviewers.yml):
a scheduled drift-detector that recomputes the caller repo's
`.github/reviewers.yml` (the config [`assign-reviewers.yml`](../workflows/assign-reviewers.yml)
consumes at PR time) from git history and opens **one reviewable single-file
PR** when the committed map has fallen behind reality. It never assigns anyone
and never merges anything — the drift PR is the whole deliverable, and a human
accepts or edits it.

## How it scores

Per commit on the default branch within the window, per rule bucket touched by
≥1 surviving changed file:

```
score[rule][login]   += 0.5 ** (age_days / half_life_days)
touches[rule][login] += 1
```

- **Buckets are the committed rules.** Each `rules:` entry's path globs are the
  bucket definition, matched with the *same* glob semantics as
  assign-reviewers.yml's `globToRegExp` (`*` within a segment, `**` across,
  `?` one non-slash char) — the map is only correct if it is scored with the
  matcher the runtime assigns with.
- **Line counts are intentionally unused.** Recency-decayed commit *touches*
  are the signal; numstat is read only for the changed-file list.
- **Excluded:** bot authors (`[bot]@` emails, `noreply@argoproj.io`),
  generated/churn paths (ent codegen outside `ent/schema/`, `*.gen.go`,
  `*.pb.go`, `vendor/`, Go sum files, JS/generic lockfiles, dynamicconfig
  version bumps, `frontend-version.json`, plus caller `extra_exclude_paths`
  regexes), and rename noise (numstat's `{old => new}` resolves to the new
  path).
- **Emails → logins:** `login@users.noreply.github.com` (and the
  `digits+login@` form) decode directly; every other distinct email costs one
  `GET /repos/{owner}/{repo}/commits/{sha}` to read `.author.login`. Commits
  whose email can't be resolved are dropped and the count is reported in the
  PR body.
- **Eligibility = repo collaborators** (paginated `GET .../collaborators`) —
  *not* org members, because `addAssignees` silently drops non-collaborators,
  so the collaborator set is the exact test the runtime applies.

## Selection

Per rule: contributors with `touches >= min_touches` and `score >= min_score`,
ranked by score, capped at `top_k`. Below `floor` qualifiers, the ranked
remainder backfills (needing only `touches >= floor_min_touches`); a rule
still under the floor is **left unchanged** and noted in the PR body — the
runtime already falls back to `default_pool` when a rule can't match, so a
cold bucket keeps its hand-set owners. `default_pool` becomes the top-5
whole-repo scorers minus anyone already anchoring ≥2 rules (the anti-pile-on
rationale from ComfyUI_frontend#5448) minus `map_exclude`.

The rewrite is **surgical**: only the `reviewers: [...]` / `default_pool:
[...]` sequences (flow or block) are replaced; every comment and all other
bytes are preserved. The config's comments are its documentation — a
YAML-dump rewrite would be a regression.

The PR body carries the per-rule before/after table with scores/touches, the
unresolved-email count, the knob values, and a report-only **taxonomy gap**
section: the hottest top-two-level directories matched by *no* rule glob,
with their top contributors — candidates for new rules, never auto-added.

## Knob defaults (and why)

| knob | default | rationale |
|---|---|---|
| `window_months` | 12 | Long enough to cover slow-moving subsystems; validated in BE-4114 against cloud history. |
| `half_life_days` | 90 | The decay is what correctly aged out stale expertise (e.g. inference contributors who had moved on); 90d matched observed team reality where flat counts did not. |
| `top_k` / `floor` | 4 / 2 | Enough experts to load-balance across without piling every rule onto the same two people. |
| `min_touches` / `min_score` | 5 / 1.5 | Filters drive-by contributors: one huge recent commit is not sustained expertise. |
| `floor_min_touches` | 2 | Relaxed bar used only to reach the floor. |
| `pr_branch` | `bot/refresh-reviewers` | Stable branch, force-reset each run — re-runs update the one open drift PR instead of stacking duplicates. |

## `map_exclude` guidance

`map_exclude` controls who may appear in the **committed map**; it is distinct
from the runtime `vars.REVIEWER_EXCLUDE` (who assign-reviewers skips at PR
time). Seed it with operator logins whose commit volume is largely
**agent-authored** — their history signal is machine throughput, not personal
review expertise, and without the exclusion they would anchor every bucket.

## Files

- `generate.py` — the whole engine (stdlib-only; parsing, scoring, selection,
  surgical rewrite, report/PR-body emission). Loaded at run time from a pinned
  ref of this repo, never from the caller's checkout.
- `tests/test_generate.py` — pure-python tests: glob parity, decay math,
  threshold/floor/backfill selection, bot/path/rename filtering, noreply
  decoding, byte-preserving rewrite. Run:

```bash
python3 -m unittest discover -s .github/refresh-reviewers/tests -p 'test_*.py' -v
```
