**Business.** Judge whether the provided context — the PR title, the PR body,
and the linked issue text in `{{context_file}}` — shows that the business wants
this change.

Look for a stated problem, request, or decision that this PR answers, and check
that the change is the one that context asks for. A PR with no context beyond
its own title, or whose context describes a different change, is not evidence
of intent: that is `yellow`, not `green`. Context that rules the change out —
an issue closed as won't-do, a request for the opposite behavior — is `red`.
Judge intent only; whether the code is good is another reviewer's axis.
