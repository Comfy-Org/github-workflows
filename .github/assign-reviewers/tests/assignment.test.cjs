const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const workflow = readFileSync(resolve(__dirname, '../../workflows/assign-reviewers.yml'), 'utf8');
// Execute the shipped inline script, never a copied implementation.
const script = workflow.split('          script: |\n')[1].split('\n').map(line => line.replace(/^            /, '')).join('\n');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const runScript = new AsyncFunction('github', 'context', 'core', 'process', script);
const file = (filename) => ({filename, changes: 10});
const approval = (login, state = 'APPROVED', type = 'User') => ({user: {login, type}, state, author_association: 'MEMBER'});
const ruleConfig = 'default_pool: [generalist]\nrules:\n  - paths: ["src/api/**"]\n    reviewers: [alice, bob]\n  - paths: ["src/ui/**"]\n    reviewers: [carol]\n';
async function run(options = {}) {
  const files = options.files || [file('src/api/main.js')];
  const pr = {number: 123, user: {login: 'author', type: 'User'}, state: 'open', draft: false, labels: [], head: {sha: 'head'}, base: {sha: 'base', ref: 'main'}, assignees: [], changed_files: files.length, ...options.pr};
  const history = options.history || [];
  const logs = [], writes = [], reads = [];
  let liveReads = 0, summary = '';
  const github = {rest: {
    pulls: {
      get: async () => {
        liveReads++;
        if (options.liveError) throw Error('unavailable');
        return {data: {...pr, ...(liveReads > 1 ? options.beforeWrite : {})}};
      },
      listFiles: async ({pull_number}) => ({data: pull_number === pr.number ? files : history.find(h => h.number === pull_number).files}),
      listReviews: async ({pull_number}) => {
        if (options.historyError) throw Error('history unavailable');
        return {data: history.find(h => h.number === pull_number).reviews};
      },
    },
    repos: {getContent: async (args) => {
      reads.push(args);
      return {data: {content: Buffer.from(options.config || ruleConfig).toString('base64')}};
    }},
    search: {issuesAndPullRequests: async ({q}) => {
      if (q.includes('is:merged')) return {data: {items: history, incomplete_results: options.incompleteHistory || false}};
      const login = q.match(/assignee:([^ ]+)/)[1];
      if (options.loadErrors?.includes(login)) throw Error('load unavailable');
      return {data: {total_count: options.loads?.[login] ?? 0, incomplete_results: false}};
    }},
    issues: {
      checkUserCanBeAssigned: async ({assignee}) => {if (options.unassignable?.includes(assignee)) throw Error('not assignable'); return {status: 204};},
      addAssignees: async (args) => {writes.push(args); return {data: {assignees: args.assignees.map(login => ({login}))}};},
    },
  }};
  github.paginate = async (endpoint, args) => (await endpoint(args)).data;
  const core = {info: m => logs.push(m), warning: m => logs.push(m), summary: {
    addHeading() {return this;}, addRaw(m) {summary += m; return this;}, async write() {},
  }};
  await runScript(github, {repo: {owner: 'example', repo: 'project'}, payload: {pull_request: pr}}, core, {env: {
    REVIEWER_CONFIG_PATH: '.github/reviewers.yml', NUM_REVIEWERS: '2', SKIP_LABEL: 'skip-auto-assign', ...options.env,
  }});
  return {selected: writes.flatMap(w => w.assignees), logs, reads, summary};
}
const past = (number, files, reviews) => ({number, files: files.map(file), reviews});
test('one domain gets one expert, despite a two-person maximum', async () => {
  assert.deepEqual((await run({loads: {alice: 5, bob: 1}})).selected, ['bob']);
});
test('second owner covers a different subsystem', async () => {
  assert.deepEqual((await run({files: [file('src/api/main.js'), file('src/api/other.js'), file('src/ui/main.js')]})).selected, ['alice', 'carol']);
});
test('minor bucket cannot displace dominant expertise through lower load', async () => {
  assert.deepEqual((await run({files: [file('src/api/a.js'), file('src/api/b.js'), file('src/ui/a.js')], loads: {alice: 20, bob: 30}, env: {NUM_REVIEWERS: '1', LOAD_CAP: '5'}})).selected, ['alice']);
});
test('history discovers exact-file expert outside fallback roster', async () => {
  const result = await run({files: [file('src/router/schema.json')], history: [past(90, ['src/router/schema.json'], [approval('robin')])]});
  assert.deepEqual(result.selected, ['robin']); assert.match(result.summary, /#90/);
});
test('directory inference requires two separate approvals', async () => {
  const options = {files: [file('src/router/new.json')], history: [past(90, ['src/router/old.json'], [approval('robin')])]};
  assert.deepEqual((await run(options)).selected, ['generalist']);
  options.history.push(past(91, ['src/router/other.json'], [approval('robin')]));
  assert.deepEqual((await run(options)).selected, ['robin']);
});
test('top-level sibling and different subsystem history do not confer expertise', async () => {
  assert.deepEqual((await run({files: [file('src/new.js'), file('services/api/new.go')], history: [past(90, ['src/old.js', 'services/worker/new.go'], [approval('robin')]), past(91, ['src/other.js', 'services/worker/a.go'], [approval('robin')])]})).selected, ['generalist']);
});
test('history refines configured roster but cannot override it', async () => {
  assert.deepEqual((await run({loads: {alice: 0, bob: 3}, history: [past(90, ['src/api/main.js'], [approval('bob'), approval('outsider')])]})).selected, ['bob']);
});
test('dismissed approvals, bots and comments are not ownership evidence', async () => {
  assert.deepEqual((await run({files: [file('src/router/schema.json')], history: [past(90, ['src/router/schema.json'], [approval('alice'), approval('alice', 'DISMISSED'), approval('bot', 'APPROVED', 'Bot'), approval('bob', 'COMMENTED')])]})).selected, ['generalist']);
});
test('comment after approval does not erase the approval', async () => {
  assert.deepEqual((await run({files: [file('src/router/schema.json')], history: [past(90, ['src/router/schema.json'], [approval('alice'), approval('alice', 'COMMENTED')])]})).selected, ['alice']);
});
test('author/exclusions normalize case and @ prefix; growth pool ignored', async () => {
  assert.deepEqual((await run({config: 'rules:\n  - paths: ["src/**"]\n    reviewers: [Author, Alice, BOB]\n', env: {EXCLUDE: '@ALICE', GROWTH_POOL: 'random'}})).selected, ['bob']);
});
test('unknown load is not treated as zero', async () => {
  assert.deepEqual((await run({loadErrors: ['alice'], loads: {bob: 9}})).selected, ['bob']);
});
test('cap chooses an available equally qualified expert', async () => {
  assert.deepEqual((await run({loads: {alice: 3, bob: 0}, env: {LOAD_CAP: '2'}, history: [past(90, ['src/api/main.js'], [approval('alice')])]})).selected, ['bob']);
});
test('unassignable preferred candidate is replaced with next qualified expert', async () => {
  assert.deepEqual((await run({unassignable: ['alice']})).selected, ['bob']);
});
test('unreadable history preserves configured expertise', async () => {
  assert.deepEqual((await run({historyError: true, history: [past(90, [], [])]})).selected, ['alice']);
});
test('routing config is read from base, never the PR head', async () => {
  assert.equal((await run()).reads[0].ref, 'base');
});
test('allowlist stays author-scoped, not trigger-actor scoped', async () => {
  assert.deepEqual((await run({env: {AUTHOR_ALLOWLIST: 'someone-else'}})).selected, []);
  assert.deepEqual((await run({env: {AUTHOR_ALLOWLIST: '@AUTHOR'}})).selected, ['alice']);
});
for (const [name, change] of Object.entries({manual: {assignees: [{login: 'manual'}]}, draft: {draft: true}, closed: {state: 'closed'}, skip: {labels: [{name: 'skip-auto-assign'}]}, reviewer: {requested_reviewers: [{login: 'manual'}]}, team: {requested_teams: [{slug: 'team'}]}, pushed: {head: {sha: 'new'}}, retargeted: {base: {sha: 'new-base', ref: 'release'}}})) {
  test(`live ${name} state before mutation prevents assignments`, async () => assert.deepEqual((await run({beforeWrite: change})).selected, []));
}
test('initial manual assignment and unreadable live state skip', async () => {
  assert.deepEqual((await run({pr: {assignees: [{login: 'manual'}]}})).selected, []);
  assert.deepEqual((await run({liveError: true})).selected, []);
});
test('incomplete diff skips assignment', async () => assert.deepEqual((await run({pr: {changed_files: 5000}})).selected, []));
test('rename considers original ownership path', async () => assert.deepEqual((await run({files: [{filename: 'new/place/file.js', previous_filename: 'src/api/file.js'}]})).selected, ['alice']));
test('no default fallback when matched experts are excluded', async () => assert.deepEqual((await run({env: {EXCLUDE: 'alice bob'}})).selected, []));
test('generated artifact volume does not outweigh source ownership', async () => {
  const files = [file('src/ui/main.js'), ...Array.from({length: 5}, (_, i) => file(`src/api/a${i}.gen.go`))];
  assert.deepEqual((await run({files, env: {NUM_REVIEWERS: '1'}})).selected, ['carol']);
});
test('block-style config and ** glob at root remain supported', async () => {
  assert.deepEqual((await run({files: [file('api/x.js')], config: 'default_pool:\n  - generalist\nrules:\n  - paths:\n      - "**/api/**"\n    reviewers:\n      - bob\n'})).selected, ['bob']);
});
test('source filenames containing lock are not downweighted', async () => {
  assert.deepEqual((await run({files: [file('src/api/clock.js'), file('src/api/blocking.js'), file('src/ui/main.js')], env: {NUM_REVIEWERS: '1'}})).selected, ['alice']);
});
test('incomplete history search is not trusted', async () => {
  assert.deepEqual((await run({incompleteHistory: true, history: [past(90, ['src/api/main.js'], [approval('bob')])]})).selected, ['alice']);
});
test('ten-owner hard limit and one-owner requested limit are enforced', async () => {
  const files = Array.from({length: 12}, (_, i) => file(`src/area${i}/main.js`));
  const config = 'rules:\n' + files.map((f, i) => `  - paths: ["${f.filename}"]\n    reviewers: [owner${i}]\n`).join('');
  assert.equal((await run({files, config, env: {NUM_REVIEWERS: '99'}})).selected.length, 10);
  assert.equal((await run({files, config, env: {NUM_REVIEWERS: '1'}})).selected.length, 1);
});
