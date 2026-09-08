const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const workflow = readFileSync(resolve(__dirname, '../../workflows/assign-reviewers.yml'), 'utf8');
// Execute the shipped inline script, never a copied implementation.
const script = workflow.split('          script: |\n')[1].split('\n      - name: Publish reviewer history manifest')[0].split('\n').map(line => line.replace(/^            /, '')).join('\n');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const runScript = new AsyncFunction('github', 'context', 'core', 'process', 'require', script);
const file = (filename) => ({filename, changes: 10});
const approval = (login, state = 'APPROVED', type = 'User') => ({user: {login, type}, state, author_association: 'MEMBER'});
const ruleConfig = 'default_pool: [generalist]\nrules:\n  - paths: ["src/api/**"]\n    reviewers: [alice, bob]\n  - paths: ["src/ui/**"]\n    reviewers: [carol]\n';
async function run(options = {}) {
  const files = options.files || [file('src/api/main.js')];
  const pr = {number: 123, user: {login: 'author', type: 'User'}, state: 'open', draft: false, labels: [], head: {sha: 'head'}, base: {sha: 'base', ref: 'main'}, assignees: [], changed_files: files.length, ...options.pr};
  const history = options.history || [];
  const logs = [], writes = [], reads = [], calls = [], outputs = {}, failures = [];
  const repository = {full_name: 'example/project', default_branch: 'main'};
  let liveReads = 0, summary = '';
  const github = {rest: {
    pulls: {
      get: async () => {
        liveReads++;
        if (options.liveError) throw Error('unavailable');
        return {data: {...pr, ...(liveReads > 1 ? options.beforeWrite : {})}};
      },
      listFiles: async ({pull_number, page = 1}) => {
        calls.push(`files:${pull_number}:${page}`);
        const all = pull_number === pr.number ? files : history.find(h => h.number === pull_number).files;
        return {data: all.slice((page - 1) * 100, page * 100)};
      },
      listReviews: async ({pull_number}) => {
        calls.push(`reviews:${pull_number}`);
        if (options.historyError) throw Error('history unavailable');
        return {data: history.find(h => h.number === pull_number).reviews};
      },
    },
    repos: {get: async () => ({data: repository}), getContent: async (args) => {
      reads.push(args);
      return {data: {content: Buffer.from(options.config || ruleConfig).toString('base64')}};
    }},
    search: {issuesAndPullRequests: async ({q}) => {
      calls.push(q.includes('is:merged') ? 'history-search' : 'load');
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
  github.rest.actions = {
    listWorkflowRuns: async ({event}) => {calls.push(`runs:${event}`); if (options.cacheError) throw Error('artifact access denied'); return {data: {workflow_runs: (options.runs || []).filter(r => r.event === event)}};},
    listWorkflowRunArtifacts: async () => ({data: {artifacts: options.artifacts || []}}),
    downloadArtifact: async () => {calls.push('download'); return {data: options.archive};},
  };
  github.paginate = async (endpoint, args) => (await endpoint(args)).data;
  const core = {setOutput: (k, v) => {outputs[k] = v;}, setFailed: m => failures.push(m), info: m => logs.push(m), warning: m => logs.push(m), summary: {
    addHeading() {return this;}, addRaw(m) {summary += m; return this;}, async write() {},
  }};
  await runScript(github, {repo: {owner: 'example', repo: 'project'}, payload: {pull_request: pr}, eventName: 'pull_request', ref: 'refs/pull/123/merge', runId: 42, ...options.context}, core, {env: {
    REVIEWER_CONFIG_PATH: '.github/reviewers.yml', NUM_REVIEWERS: '2', SKIP_LABEL: 'skip-auto-assign', ...options.env,
  }}, options.require || require);
  return {calls, outputs, failures, selected: writes.flatMap(w => w.assignees), logs, reads, summary};
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

const {execFileSync} = require('node:child_process');
const {mkdtempSync, readFileSync: read, rmSync} = require('node:fs');
const {tmpdir} = require('node:os');
const {join} = require('node:path');
function zipManifest(manifest, name = 'manifest.json') {
  return execFileSync('python3', ['-c', 'import io,json,sys,zipfile; data=json.load(sys.stdin); b=io.BytesIO(); z=zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED); z.writestr(data["name"],data["text"]); z.close(); sys.stdout.buffer.write(b.getvalue())'], {input: JSON.stringify({name, text: JSON.stringify(manifest)})});
}
function cachedOptions(options = {}) {
  const manifest = {
    version: 1, repository: 'example/project', base: 'main', run_id: '41',
    generated_at: new Date(Date.now() - 60000).toISOString(),
    records: [{number: 90, approvers: ['bob'], files: [file('src/api/main.js')]}],
    ...options.manifest,
  };
  return {env: {HISTORY_WORKFLOW: 'assign-reviewers.yml'}, runs: [{
    id: 41, event: 'schedule', head_branch: 'main', status: 'completed', conclusion: 'success',
    path: '.github/workflows/assign-reviewers.yml', head_repository: {full_name: 'example/project'},
    created_at: new Date(Date.now() - 120000).toISOString(), ...options.run,
  }], artifacts: [{id: 5, name: 'reviewer-history-v1', expired: false, size_in_bytes: 1024}], archive: zipManifest(manifest), ...options.rest};
}
test('valid cache avoids all historical API calls but still fetches current files and workload', async () => {
  const result = await run(cachedOptions());
  assert.deepEqual(result.selected, ['bob']);
  assert.equal(result.calls.filter(c => c.startsWith('reviews:')).length, 0);
  assert.equal(result.calls.includes('history-search'), false);
  assert(result.calls.includes('files:123:1'));
  assert(result.calls.includes('load'));
  assert.match(result.summary, /cached/);
});
test('current author and exclusions apply to cached neutral evidence', async () => {
  const options = cachedOptions({manifest: {records: [{number: 90, approvers: ['author', 'bob', 'alice'], files: [file('src/api/main.js')]}]}});
  options.env.EXCLUDE = '@BOB';
  assert.deepEqual((await run(options)).selected, ['alice']);
});
test('current workload still chooses between equally qualified cached experts', async () => {
  const options = cachedOptions({manifest: {records: [{number: 90, approvers: ['alice', 'bob'], files: [file('src/api/main.js')]}]}});
  assert.deepEqual((await run({...options, loads: {alice: 0, bob: 5}})).selected, ['alice']);
  assert.deepEqual((await run({...options, loads: {alice: 5, bob: 0}})).selected, ['bob']);
});
for (const [label, manifest] of Object.entries({
  stale: {generated_at: new Date(Date.now() - 13 * 3600000).toISOString()},
  future: {generated_at: new Date(Date.now() + 3600000).toISOString()},
  undated: {generated_at: 'not-a-date'},
  otherRepo: {repository: 'example/other'}, otherBase: {base: 'release'},
  oldSchema: {version: 0}, wrongRun: {run_id: '999'},
  duplicatePR: {records: [{number: 90, approvers: ['bob'], files: []}, {number: 90, approvers: ['bob'], files: []}]},
  duplicateApproval: {records: [{number: 90, approvers: ['bob', 'BOB'], files: []}]},
  badLogin: {records: [{number: 90, approvers: ['bot[bot]'], files: []}]},
  badPath: {records: [{number: 90, approvers: ['bob'], files: [{filename: null}]}]},
})) {
  test(`${label} manifest is rejected and live history is used`, async () => {
    const result = await run(cachedOptions({manifest}));
    assert.deepEqual(result.selected, ['alice']);
    assert(result.calls.includes('history-search'));
  });
}
for (const [label, runChange] of Object.entries({
  pullRequest: {event: 'pull_request'}, workflowRun: {event: 'workflow_run'}, otherBranch: {head_branch: 'topic'},
  fork: {head_repository: {full_name: 'attacker/project'}}, otherWorkflow: {path: '.github/workflows/evil.yml'},
  incomplete: {status: 'in_progress'}, failed: {conclusion: 'failure'},
  oldRun: {created_at: new Date(Date.now() - 13 * 3600000).toISOString()},
})) {
  test(`${label} producer cannot supply cached routing evidence`, async () => {
    const result = await run(cachedOptions({run: runChange}));
    assert(result.calls.includes('history-search'));
    assert(!result.calls.includes('download'));
  });
}
test('missing artifact, denied access, and corrupt archives fall back without failing assignment', async () => {
  for (const rest of [{artifacts: []}, {cacheError: true}, {archive: Buffer.from('corrupt')}, {artifacts: [{id: 5, name: 'reviewer-history-v1', expired: true}]}]) {
    const result = await run(cachedOptions({rest}));
    assert.deepEqual(result.selected, ['alice']);
    assert(result.calls.includes('history-search'));
  }
});
test('ZIP member paths are never extracted and unexpected names are rejected', async () => {
  const result = await run(cachedOptions({rest: {archive: zipManifest({}, '../../manifest.json')}}));
  assert(result.calls.includes('history-search'));
});
test('empty valid manifest is a cache hit, not a reason to repeat the lookup', async () => {
  const result = await run(cachedOptions({manifest: {records: []}}));
  assert.deepEqual(result.selected, ['alice']);
  assert(!result.calls.includes('history-search'));
});
test('manual dispatch on default branch can supply the snapshot', async () => {
  const result = await run(cachedOptions({run: {event: 'workflow_dispatch'}}));
  assert.deepEqual(result.selected, ['bob']);
});
test('generator writes neutral evidence and cached/live selection agree', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'reviewer-manifest-'));
  try {
    const history = [past(90, ['src/api/main.js'], [approval('author'), approval('bob')])];
    const generated = await run({history, env: {GENERATE_HISTORY: 'true', RUNNER_TEMP: directory, AUTHOR_ALLOWLIST: 'different-person', EXCLUDE: 'bob'}, context: {eventName: 'workflow_dispatch', ref: 'refs/heads/main'}});
    assert.equal(generated.outputs.manifest_ready, 'true');
    assert.deepEqual(generated.selected, []);
    const manifest = JSON.parse(read(join(directory, 'reviewer-history/manifest.json')));
    assert.deepEqual(manifest.records[0].approvers, ['author', 'bob']);
    const live = await run({history});
    const cached = await run(cachedOptions({manifest, run: {id: 42}}));
    assert.deepEqual(cached.selected, live.selected);
    assert(!cached.calls.includes('history-search'));
  } finally {rmSync(directory, {recursive: true, force: true});}
});
for (const context of [{eventName: 'pull_request', ref: 'refs/heads/main'}, {eventName: 'workflow_dispatch', ref: 'refs/heads/topic'}]) {
  test(`generator refuses ${context.eventName} on ${context.ref}`, async () => {
    const result = await run({context, env: {GENERATE_HISTORY: 'true'}});
    assert.equal(result.failures.length, 1);
    assert(!result.calls.includes('history-search'));
    assert.equal(result.outputs.manifest_ready, undefined);
  });
}
test('refresh failure cannot publish partial evidence', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'reviewer-manifest-fail-'));
  try {
    await assert.rejects(run({history: [past(90, [], [])], historyError: true, context: {eventName: 'schedule', ref: 'refs/heads/main'}, env: {GENERATE_HISTORY: 'true', RUNNER_TEMP: directory}}), /history unavailable/);
    assert.throws(() => read(join(directory, 'reviewer-history/manifest.json')), /ENOENT/);
  } finally {rmSync(directory, {recursive: true, force: true});}
});
test('historical sweeps stop after three pages and add no ownership evidence', async () => {
  const history = [past(90, Array.from({length: 500}, (_, i) => `src/api/file${i}.js`), [approval('bob')])];
  const result = await run({history});
  assert.equal(result.calls.filter(c => c.startsWith('files:90:')).length, 3);
  assert.deepEqual(result.selected, ['alice']);
});
