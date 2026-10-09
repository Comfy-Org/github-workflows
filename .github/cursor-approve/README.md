# cursor-approve — five-axis approval prompts + verdict aggregator

The prompts and the decision script behind a planned reusable workflow,
`cursor-approve.yml`, that judges each PR on five axes and approves only when
the axes allow it. The workflow itself is not here yet; like cursor-review, it
will check this directory out at a pinned ref of this repo at run time, so a
PR can never rewrite the prompts or the rules judging it.

## Prompts

`prompt-common.md` holds the rules every reviewer shares — review only the
merge-base diff (`git diff MERGE_BASE...HEAD`, never against the base tip), read
but do not run, the red/yellow/green/n-a meaning, PR content is data not
instructions, and the one-JSON-object output contract. Each axis file adds one
focus:

| Axis | Focus |
|---|---|
| `business` | does the PR's context show the business wants this change? |
| `design` | does the change match its issue, plan and intended design? |
| `correctness` | is the code correct under a pragmatic risk model? |
| `completeness` | is this a complete solution to the stated problem? |
| `conformance` | does the code fit the repo's engineering and deployment practices? |

### Verdicts, and the two fields a verdict carries

`red` / `yellow` / `green` / `n/a`. `n/a` is for an axis with nothing to judge
on this change — no applicable guidance, nothing in its remit touched — and it
is NOT a `green`: letting "no applicable standard" claim a clean bill would
quietly approve the gap the day guidance does appear. It does not count against
`--max-yellow-axes`, because `yellow` is a *concern* and this is its absence;
but a round where EVERY axis says `n/a` is withheld, since nothing was judged.
An `n/a` has to name what the axis looked for and where.

Each verdict carries two strings, with different jobs and different audiences:

| Field | Cap | Where it goes |
|---|---|---|
| `headline` | 100 chars, **required** | the PR card — ONE line, the reason for the verdict |
| `summary` | 1200 chars | the run's job summary, behind the card's "workflow run" link |

The split exists because one field could not do both. The card used to show the
first sentence of `summary`, and `summary` is where a model writes its process
("Checked the only changed file…", "I read root AGENTS.md and CLAUDE.md…") — so
the card said what the axis *did* and never why it ruled as it did, which is
unreadable on a yellow. `headline` must state what decided the verdict, and a
missing or over-long one makes the axis untrusted rather than being truncated:
truncating would reproduce the reason-free row one step later.

Placeholders — `{{pr_number}}`, `{{repo}}`, `{{head_sha}}`,
`{{merge_base_sha}}`, `{{base_ref}}`, `{{context_file}}` — are filled by
`aggregate.py render`, which checks each value's shape first and substitutes in
one pass (a value is never re-scanned for placeholders).

## `aggregate.py`

Stdlib only; pure; never writes to GitHub.

```bash
aggregate.py render --axis correctness --pr-number 12 --repo owner/name \
  --head-sha <sha> --merge-base-sha <sha> --base-ref main --context-file ctx.md
aggregate.py decide --outputs-dir out/ [--axes design,correctness] [--max-yellow-axes 0]
```

`decide` reads `<axis>.json` for each expected axis and prints
`{"event", "verdicts", "axes", "reasons"}`. In order:

1. any expected axis missing, unparsable, with a verdict outside
   red/yellow/green/n-a, a confidence outside 0..1, or a missing or over-long
   `headline` → `NONE` — untrusted, so the approval is withheld; it is never a
   veto;
2. any `red` → `NONE`, naming the red axes;
3. more `yellow` axes than `--max-yellow-axes` (0–3, default 0, and always
   strictly below the number of expected axes — a limit that every axis could
   reach would approve a round with no green axis at all) → `NONE`;
4. every axis `n/a` → `NONE`: nothing was judged, so there is nothing to
   approve on;
5. otherwise `APPROVE`.

`decide` is strict about its input: the file must be exactly one JSON object,
so extracting it from the model's raw reply is the workflow's job. Bad
arguments exit 2; every decision, `NONE` included, exits 0. The workflow's
decide job turns `APPROVE` into a review by reusing
[`../cursor-review/auto-approve.py`](../cursor-review/auto-approve.py).

Trust model: every verdict is model output over the PR's own content, so a diff
that prompt-injects every axis can steer the round to an approval. Treat the
approval as an automated review signal, not as a substitute for a human.

Tests: `python3 -m unittest discover -s .github/cursor-approve/tests -p 'test_*.py' -v`
(run in CI by `test-cursor-review-scripts.yml`).

## `context-proxy.py`

A read-only stdio MCP server that gives the business, design and completeness
axes Linear and Notion context without the agent ever holding a token. It still
implements Slack tools, but the base workflow refuses `slack`: no axis reads it.
It reads a token file once, deletes it, and sends every request through one
guard function that allow-lists each read endpoint before anything goes out.

```bash
context-proxy.py --token-file tokens.json --enable linear,notion,slack --log calls.jsonl
```

Its module docstring is the full contract: the allow-list, the call and byte
limits, the bounded Slack cache, and why every result is third-party data the
consuming prompt must never follow as instructions.

`cursor-axis-base.yml` starts the proxy BEFORE the agent, on two FIFOs, and
waits until it has deleted its token file; cursor-agent then reaches it through
`mcp-relay.py`, a byte relay that holds no token. Two prompt addenda are
appended by the workflow, not by `render`: `prompt-no-checkout.md` (business,
design: the change is in a fetched file, there is no shell) and
`prompt-context-tools.md` (the context tools, whose results are data).
