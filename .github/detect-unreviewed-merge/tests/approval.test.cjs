// Runs the SHIPPED inline github-script of detect-unreviewed-merge.yml against API fixtures,
// never a copied implementation. Extraction anchors on the `// HARNESS:script:{begin,end}`
// sentinels, which must bracket the whole `script: |` scalar so no line ships untested.
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const assert = require('node:assert/strict');

const workflow = readFileSync(resolve(__dirname, '../../workflows/detect-unreviewed-merge.yml'), 'utf8');
const lines = workflow.split('\n');
const only = (marker) => {
  const hits = lines.flatMap((line, index) => line.trim() === marker ? [index] : []);
  if (hits.length !== 1) throw Error(`expected exactly one \`${marker}\` line, found ${hits.length}`);
  return hits[0];
};
const begin = only('// HARNESS:script:begin');
const end = only('// HARNESS:script:end');
if (!/^\s*script:\s*\|\s*$/.test(lines[begin - 1])) throw Error('`// HARNESS:script:begin` must be the first line of the `script: |` scalar');
const indent = lines[begin].length - lines[begin].trimStart().length;
const after = lines.slice(end + 1).find((line) => line.trim());
if (after !== undefined && after.startsWith(' '.repeat(indent))) throw Error('`// HARNESS:script:end` must be the last line of the `script: |` scalar');
const script = lines.slice(begin + 1, end).map((line) => line.slice(indent)).join('\n');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const runScript = new AsyncFunction('github', 'context', 'core', 'process', script);

const review = (login, state = 'APPROVED', at = '2026-10-01T00:00:00Z', type = 'User') =>
  ({user: {login, type}, state, submitted_at: at});

async function run({reviews, approvalMode = 'latest-per-reviewer', ignore = '', warnings = []}) {
  const pr = {number: 7, title: 't', body: '', merged_at: '2026-10-02T00:00:00Z', base: {ref: 'main'}, user: {login: 'author'}};
  const created = [];
  const failed = [];
  const rest = {
    repos: {listPullRequestsAssociatedWithCommit: async () => ({data: [pr]})},
    pulls: {get: async () => ({data: {...pr, merged_by: {login: 'merger'}}}), listReviews: () => reviews},
    issues: {listComments: () => []},
  };
  class Octokit {
    constructor() {
      this.rest = {
        search: {issuesAndPullRequests: async () => ({data: {total_count: 0, items: []}})},
        issues: {create: async (params) => { created.push(params); return {data: {html_url: 'u'}}; }},
      };
    }
  }
  const github = Object.assign(new Octokit(), {rest, paginate: async (fn, params) => fn(params)});
  const context = {sha: 'a'.repeat(40), ref: 'refs/heads/main', repo: {owner: 'o', repo: 'r'}};
  const core = {info: () => {}, warning: (m) => warnings.push(m), setFailed: (m) => failed.push(m)};
  const env = {APPROVAL_MODE: approvalMode, IGNORE_APPROVERS: ignore, UNREVIEWED_MERGES_TOKEN: 'x'};
  await runScript(github, context, core, {env});
  assert.deepEqual(failed, []);
  return created.length; // 1 → tracking issue filed (unreviewed), 0 → counted as approved
}

for (const approvalMode of ['any-approval', 'latest-per-reviewer']) {
  test(`${approvalMode}: a human approval counts`, async () => {
    assert.equal(await run({approvalMode, reviews: [review('human')]}), 0);
  });

  test(`${approvalMode}: the bot approval counts when not ignored (back-compat default)`, async () => {
    assert.equal(await run({approvalMode, reviews: [review('review-app[bot]', 'APPROVED', undefined, 'Bot')]}), 0);
  });

  test(`${approvalMode}: an ignored approver's approval does not count`, async () => {
    const reviews = [review('review-app[bot]', 'APPROVED', undefined, 'Bot'), review('human', 'COMMENTED')];
    assert.equal(await run({approvalMode, reviews, ignore: 'review-app[bot]'}), 1);
  });

  test(`${approvalMode}: the ignore list is case-insensitive and comma/whitespace separated`, async () => {
    const reviews = [review('Cursor-Approver', 'APPROVED'), review('other-bot[bot]', 'APPROVED')];
    assert.equal(await run({approvalMode, reviews, ignore: ' cursor-approver,\n OTHER-BOT[bot] '}), 1);
  });

  test(`${approvalMode}: a human approval still counts alongside an ignored bot`, async () => {
    const reviews = [review('review-app[bot]', 'APPROVED'), review('human', 'APPROVED')];
    assert.equal(await run({approvalMode, reviews, ignore: 'review-app[bot]'}), 0);
  });

  test(`${approvalMode}: with an ignore list, a review with no usable login does not count`, async () => {
    // A deleted App's approval could be the ignored bot's; a null login must not throw either.
    for (const user of [null, {login: null, type: 'Bot'}]) {
      const reviews = [{user, state: 'APPROVED', submitted_at: '2026-10-01T00:00:00Z'}];
      assert.equal(await run({approvalMode, reviews, ignore: 'review-app[bot]'}), 1);
    }
  });

  test(`${approvalMode}: warns when the only counted approval is a Bot's`, async () => {
    const warnings = [];
    const reviews = [review('typo-app[bot]', 'APPROVED', undefined, 'Bot')];
    assert.equal(await run({approvalMode, reviews, ignore: 'review-app[bot]', warnings}), 0);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /typo-app\[bot\]/);
    const quiet = [];
    await run({approvalMode, reviews: [...reviews, review('human')], warnings: quiet});
    assert.deepEqual(quiet, []);
  });
}

test('latest-per-reviewer: a dismissed human approval still does not count', async () => {
  const reviews = [review('human', 'APPROVED', '2026-10-01T00:00:00Z'), review('human', 'DISMISSED', '2026-10-01T01:00:00Z')];
  assert.equal(await run({reviews}), 1);
});
