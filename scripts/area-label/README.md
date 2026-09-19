# `scripts/area-label/` — the agentic area-label classifier

The scripts behind [`pr-area-label.yml`](../../.github/workflows/pr-area-label.yml). A
consumer repo runs the reusable workflow via a thin caller and keeps only its own taxonomy
(`.github/area-labels.yml` by default); this directory holds the logic, loaded from the
`workflows_ref` SHA the caller pins.

| File | Role |
|---|---|
| `lib.sh` | Pure, network-free core — taxonomy parsing, the validation gates, path-glob matching for sub-labels, request-building, reply-parsing. Sourced by both scripts and by the test suites. |
| `sync-labels.sh` | `gh label create --force` loop that reconciles the consumer's `area:*` labels — `labels[]` and `sub_labels[]` alike — to the taxonomy. Runs on push to the consumer's default branch. No secret. |
| `classify-and-apply.sh` | The side-effecting orchestration: fetch the taxonomy from the PR base ref, call the Anthropic Messages API, apply the label with targeted `area:*` ops. Sources `lib.sh`. |
| `tests/test_lib.sh` | Hermetic suite over `lib.sh` — no network. |
| `tests/test_apply.sh` | Hermetic suite over `classify-and-apply.sh` with `gh` and `curl` stubbed: the label-set arithmetic, idempotence, cleanup, and the fail-soft paths. |

## Security model

- The model gets **no tools and no token**. PR title/body/paths/labels are passed as *data*
  inside `<pr_data>` tags; the diff is excluded.
- The reply is **enum-constrained by a JSON schema** to the taxonomy's own `area:*` names,
  so injection in PR text can't produce anything but a valid label.
- The taxonomy is read from the PR's **base ref**, never its head — a PR cannot rewrite the
  rules that classify it — and validated (`validate_names` / `validate_vocab`) before it is
  trusted to drive a label write.
- The label is applied with **targeted `area:*` add/remove ops**, never a full-set PUT, so
  concurrent non-area label edits are preserved.
- Sub-label names are constrained to the same `area:*` shape and must be **disjoint** from
  the classified names, so every write this tool makes stays inside the `area:*` namespace
  and no label is ever both "the classification" and "an exempt sub-label".
- Everything **fails soft** (skip-with-warning) rather than failing the PR check.

## Taxonomy shape

```yaml
# .github/area-labels.yml in the CONSUMER repo
repo_context: |            # optional — repo/domain framing injected into the system prompt
  One or two sentences on what this repo is and the domain-vs-path judgement calls.
labels:
  - name: "area:gcp"       # must match ^area:[a-z0-9-]+$, unique across the file
    color: "0e8a16"
    description: "GCP infrastructure"        # what GitHub stores (≤100 chars)
    guidance: "Longer routing guidance…"     # optional; the classifier reads this,
                                             # falling back to description when absent

sub_labels:                # optional — deterministic, additive, NOT seen by the model
  - name: "area:router"    # same ^area:[a-z0-9-]+$ shape; must NOT appear in labels[]
    color: "7057ff"
    description: "Router: the model-routing layer inside the API service"
    paths:                 # at least one; matched against the PR's changed files
      - "services/api/router*/**"
      - "**/*router*"
```

## Path sub-labels

`labels[]` answers "which area owns this PR?" — a judgement call, so a model makes it, and
exactly one wins. `sub_labels[]` answers "does this PR touch X?" — a path question, so no
model is involved and the answer rides *alongside* the classified area rather than competing
with it. The motivating case: a subsystem that lives inside another area's service (the
a set of router packages, inside a larger API service) and would otherwise be
unfilterable without splitting the service's own area in two.

A PR's end state is **one classified area plus every sub-label whose `paths:` matched**. The
matched names are exempt from the one-area cleanup; a sub-label that stops matching is
retired by that same cleanup on the next run. Glob semantics match the ones a workflow
`paths:` filter uses, so a glob can be copied between the two:

| Pattern | Matches |
|---|---|
| `*` | anything within one path segment |
| `?` | one character within one segment |
| `**` | anything, `/` included |
| `**/` | zero or more leading directories — `**/*router*` also matches a root-level file |

## Running the tests

```bash
cd scripts/area-label
shellcheck -x ./*.sh tests/*.sh
# LOOP — `bash a.sh b.sh` runs only a.sh
for t in tests/*.sh; do bash "$t" || { echo "FAILED: $t"; break; }; done
```
