# Owner assignment tests

The runtime remains self-contained in `../workflows/assign-reviewers.yml`, so
live-only callers need only a SHA bump. Caching is opt-in through a scheduled
job calling the same reusable with `generate_history: true`, plus the
`history_workflow` filename on the assignment job. No caller code or mutable scripts execute.

Run `node --test .github/assign-reviewers/tests/*.test.cjs` from the repo root.
Tests extract and execute the actual inline JavaScript with mocked GitHub APIs.
They also generate and read real ZIP manifests using Python stdlib, exercising
the same bounded archive parser used in Actions, and verify that cache hits
avoid historical API calls while retaining live routing checks.
CI runs the same suite whenever the workflow or tests change.

The [caller guide](../../docs/callers/assign-reviewers.md) documents the ranking,
evidence limits, failure handling, and configuration. Keep behavior there rather
than maintaining another algorithm description here.
