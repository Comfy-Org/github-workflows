# Owner assignment tests

The runtime remains self-contained in `../workflows/assign-reviewers.yml`, so
existing callers need only a SHA bump. No caller code or mutable scripts execute.

Run `node --test .github/assign-reviewers/tests/*.test.cjs` from the repo root.
Tests extract and execute the actual inline JavaScript with mocked GitHub APIs.
CI runs the same suite whenever the workflow or tests change.

The [caller guide](../../docs/callers/assign-reviewers.md) documents the ranking,
evidence limits, failure handling, and configuration. Keep behavior there rather
than maintaining another algorithm description here.
