# groom

Building blocks for the reusable **groom** workflow (epic BE-3870 —
productionize studio groom as an org-wide code-cleanup capability). This
directory currently holds the one genuinely hard piece that can be built ahead
of the workflow itself: the **durable dedup / rejection ledger** (BE-3874).

## The problem it solves

Studio groom keeps its dedup + rejection state on the local filesystem
(`.groom-state/`). A **stateless CI run** starts fresh every time — with no
durable memory it would re-file findings that were already filed OR already
human-rejected on every scheduled run. That is the fastest way to make the
shared capability annoying and get it disabled. The roundtable was explicit:
*dedup must remember REJECTIONS — don't re-raise a rejected finding next week.*

## `ledger.py` — GitHub issue state **is** the store

The ledger uses **GitHub issue state itself** as the durable store — the
GitHub-native option that needs **no net-new secret** (the run's `GITHUB_TOKEN`
already reads issues) and is fully **auditable** (the record is the issues you
can see). No separate database, cache, or committed state file.

Keyed on `(repo, finding_signature) → {filed | rejected | superseded}`:

| Live GitHub state | Ledger status | Re-file? |
|---|---|---|
| Open `groom` issue for the signature | `filed` | no |
| Closed as **completed** | `filed` | no (already handled) |
| Closed as **not planned** (GitHub "close as wontfix") | `rejected` | **no — durable** |
| Carries the `groom-rejected` label (open or closed) | `rejected` | **no — durable** |
| Carries the `groom-superseded` label | `superseded` | no |
| No `groom` issue carries the signature | `unknown` | **yes** |

Only an `unknown` signature is filed. Human rejection — close-as-not-planned or
the `groom-rejected` label — suppresses that signature forever.

### The filing contract (load-bearing)

The signature is owned by the **verifier** ("keyed on the verifier's stable
dedup signature"); this module consumes whatever opaque string it emits on each
finding's `signature` field. For the memory to survive, the step that OPENS an
issue for a `to_file` finding **must**:

1. apply the **`groom`** label (how the next run finds our issues), and
2. append `signature_marker(finding["signature"])` to the issue body — an
   invisible HTML comment (`<!-- groom-signature: … -->`) the next run recovers.

Skip either and the next run cannot recognize the issue and will re-file it.

### CLI (called right before the groomer files)

```bash
python3 .github/groom/ledger.py \
    --repo owner/name --candidates findings.json --out decision.json
```

`findings.json` is a JSON array of findings, each with a `signature`.
`decision.json` receives `{to_file, suppressed, invalid, ledger_size}` — open
issues only for `to_file`. `invalid` = findings with no usable signature; they
are **not** filed (filing an un-dedupable finding would risk the exact
duplicate-spam this ledger prevents) and should be surfaced as a producer error.

Single-signature probe (exit 0 = should file, 1 = suppressed):

```bash
python3 .github/groom/ledger.py --repo owner/name --check "<signature>"
```

- **`tests/`** — `unittest` suite, run by
  [`test-groom-scripts.yml`](../workflows/test-groom-scripts.yml).

```bash
python3 -m unittest discover -s .github/groom/tests -p 'test_*.py' -v
```
