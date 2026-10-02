const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')

const workflowPath = path.resolve(
  __dirname,
  '../../workflows/detect-unreviewed-merge.yml'
)

function workflowScript() {
  const workflow = fs.readFileSync(workflowPath, 'utf8')
  const marker = '          script: |\n'
  const start = workflow.indexOf(marker)
  assert.notEqual(start, -1, 'workflow github-script block must exist')

  return workflow
    .slice(start + marker.length)
    .split('\n')
    .filter((line) => line === '' || line.startsWith('            '))
    .map((line) => line.slice(12))
    .join('\n')
}

async function runDetector({ approvalMode, reviews, justification = null }) {
  const createdIssues = []
  const logs = []
  const pr = {
    number: 19867,
    title: 'Reproduce bot-only approval blind spot',
    merged_at: '2026-10-02T13:05:19Z',
    base: { ref: 'main' },
    user: { login: 'author', type: 'User' },
    body: justification ? `Justification: ${justification}` : null,
  }

  class MockOctokit {
    constructor(options = {}) {
      if (options.auth) {
        this.rest = {
          search: {
            issuesAndPullRequests: async () => ({ data: { total_count: 0 } }),
          },
          issues: {
            create: async (params) => {
              createdIssues.push(params)
              return { data: { html_url: 'https://example.test/tracker/1' } }
            },
          },
        }
        return
      }

      this.rest = {
        repos: {
          listPullRequestsAssociatedWithCommit: async () => ({ data: [pr] }),
          getCommit: async () => assert.fail('commit fallback should not run'),
        },
        pulls: {
          get: async () => ({ data: { ...pr, merged_by: { login: 'merger' } } }),
          listReviews: async () => ({ data: reviews }),
        },
        issues: {
          listComments: async () => ({ data: [] }),
        },
      }
    }

    async paginate(method, params) {
      return (await method(params)).data
    }
  }

  const github = new MockOctokit()
  const core = {
    info: (message) => logs.push(message),
    warning: (message) => logs.push(message),
    setFailed: (message) => assert.fail(message),
  }
  const context = {
    sha: '99e51fe0b15754d55616afb65dea68033a289133',
    ref: 'refs/heads/main',
    repo: { owner: 'Comfy-Org', repo: 'ComfyUI_frontend' },
  }

  const previousMode = process.env.APPROVAL_MODE
  const previousToken = process.env.UNREVIEWED_MERGES_TOKEN
  process.env.APPROVAL_MODE = approvalMode
  process.env.UNREVIEWED_MERGES_TOKEN = 'test-token'
  try {
    const execute = new Function(
      'context',
      'github',
      'core',
      `return (async () => {\n${workflowScript()}\n})()`
    )
    await execute(context, github, core)
  } finally {
    if (previousMode === undefined) delete process.env.APPROVAL_MODE
    else process.env.APPROVAL_MODE = previousMode
    if (previousToken === undefined) delete process.env.UNREVIEWED_MERGES_TOKEN
    else process.env.UNREVIEWED_MERGES_TOKEN = previousToken
  }

  return { createdIssues, logs }
}

const humanApproval = {
  id: 1,
  state: 'APPROVED',
  submitted_at: '2026-10-02T11:45:00Z',
  user: { login: 'human-reviewer', type: 'User' },
}
const botApproval = {
  id: 2,
  state: 'APPROVED',
  submitted_at: '2026-10-02T11:45:10Z',
  user: { login: 'coderabbitai[bot]', type: 'Bot' },
}

for (const approvalMode of ['latest-per-reviewer', 'any-approval']) {
  test(`${approvalMode}: a human approval satisfies the control`, async () => {
    const result = await runDetector({ approvalMode, reviews: [humanApproval] })
    assert.equal(result.createdIssues.length, 0)
    assert.ok(result.logs.includes('PR has an approving review — no action needed.'))
  })

  test(`${approvalMode}: a bot-only approval creates a tracker issue`, async () => {
    const result = await runDetector({
      approvalMode,
      reviews: [botApproval],
      justification: 'Human reviewer gave a comment-form verdict on the exact head.',
    })
    assert.equal(result.createdIssues.length, 1)
    assert.match(result.createdIssues[0].title, /PR #19867/)
    assert.deepEqual(result.createdIssues[0].labels, [
      'unreviewed-merge',
      'needs-review',
      'repo:comfyui_frontend',
    ])
  })
}
