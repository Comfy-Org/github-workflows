# `scripts/area-label/` — the agentic area-label classifier

The scripts behind [`pr-area-label.yml`](../../.github/workflows/pr-area-label.yml). A
consumer repo runs the reusable workflow via a thin caller and keeps only its own taxonomy
(`.github/area-labels.yml` by default); this directory holds the logic, loaded from the
`workflows_ref` SHA the caller pins.

| File | Role |
|---|---|
| `lib.sh` | Pure, network-free core — taxonomy parsing, the two validation gates, request-building, reply-parsing. Sourced by `classify-and-apply.sh` and by the test suite. |
| `sync-labels.sh` | `gh label create --force` loop that reconciles the consumer's `area:*` labels to the taxonomy. Runs on push to the consumer's default branch. No secret. |
| `classify-and-apply.sh` | The side-effecting orchestration: fetch the taxonomy from the PR base ref, call the Anthropic Messages API, apply the label with targeted `area:*` ops. Sources `lib.sh`. |
| `tests/test_lib.sh` | Hermetic suite over `lib.sh` — no network. |

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
```

## Running the tests

```bash
cd scripts/area-label
shellcheck -x lib.sh sync-labels.sh classify-and-apply.sh tests/test_lib.sh
bash tests/test_lib.sh
```
