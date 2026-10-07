**Completeness.** Judge whether this is a complete solution to the stated
problem.

Take the problem and any acceptance criteria from `{{context_file}}` and check
each one against the diff. Look for what is missing as well as what is there:
a call site that still uses the old behavior, a branch or error path left
unhandled, a test that should exist and does not, documentation or
configuration that the change makes stale, a TODO standing in for part of the
fix. A criterion the PR does not meet and does not explain is `red`. Partial
work that the PR body states openly, with the remainder tracked elsewhere, is
at most `yellow`.
