# Security policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report privately via GitHub's [private vulnerability
reporting](https://github.com/Comfy-Org/github-workflows/security/advisories/new)
on this repository, or email **security@comfy.org**.

Please include the workflow file and, where you can, the ref (tag or commit SHA)
you observed the problem on — several of these workflows behave differently
across refs, and the ref narrows the blast radius fast.

We aim to acknowledge within 3 business days.

## What is in scope

This repo contains reusable GitHub Actions workflows that other repositories call
and grant credentials to. In scope:

- **Privilege escalation** — a workflow obtaining more `GITHUB_TOKEN` scope than
  the calling job granted, or using a scope it does not need.
- **Secret exfiltration** — any path by which `ANTHROPIC_API_KEY`,
  `CURSOR_API_KEY`, `SLACK_BOT_TOKEN`, `UNREVIEWED_MERGES_TOKEN`, or a GitHub App
  private key could reach a log, artifact, comment, or third party.
- **Untrusted input reaching a privileged context** — PR titles, branch names,
  commit messages, or fork-authored content flowing into a shell without
  quoting, or into a job that holds write scope.
- **`pull_request_target` / fork-PR issues** — anything that lets a fork PR run
  with the base repo's secrets.
- **Agent-boundary breaks** — where a model step is meant to run with **no write
  credentials**, emitting patches or findings that a *separate* job applies, a way
  to make that step hold a write token — or to influence the privileged job from
  inside the unprivileged one — is a vulnerability. The separation is real for
  groom and for cursor-review's 8 panel cells (`contents: read` only). It is
  **not** absolute for cursor-review's judge: the `Consolidate panel` job runs the
  judge model and posts the review in the same `pull-requests: write` job, so
  treat findings about that boundary as in scope rather than known-and-accepted.
- **Supply chain** — an unpinned or mutable third-party action reference, or a
  way to make a caller resolve assets from a ref other than the one it pinned.

## What is not in scope

- Findings that require an existing org admin or repo write access.
- The AI workflows producing a wrong, low-quality, or duplicate review comment,
  finding, or issue. That is a quality bug — open a normal issue.
- Spend caused by a workflow running more often than intended, absent a security
  boundary being crossed. Also a normal issue.
- Missing branch protection or required status checks on a *consumer* repo. Raise
  that with the consumer repo's owners.

## Notes for consumers

- **Pin by full commit SHA**, not by tag. The `v1` tag is a *moving* pointer that
  we force-push for backwards-compatible changes; a SHA is immutable. See
  [Pinning](README.md#pinning).
- **Keep `workflows_ref` equal to your `uses:` SHA.** Several workflows load
  prompts, briefs, or checker scripts from `workflows_ref` at runtime. If it
  drifts from the pinned workflow — or is left at the default `main` — you are
  executing assets from a ref you did not review.
- **Grant the documented minimum permissions**, not `write-all`. Each workflow's
  required grant is listed in [`docs/callers/`](docs/callers/).
- These workflows are MIT licensed and provided as-is. If you call them from
  outside Comfy-Org, you are trusting this repo's contents at whatever ref you
  pin; review it as you would any dependency.
