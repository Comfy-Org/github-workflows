const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const workflow = readFileSync(resolve(__dirname, '../../workflows/assign-reviewers.yml'), 'utf8');
// Execute the shipped inline script, never a copied implementation. Extraction anchors on the
// `// PARITY-HARNESS:<name>:{begin,end}` sentinels emitted inside the script itself: the previous
// split on a 10-space-indented `script: |` plus the name of an unrelated downstream step captured
// the WRONG span, silently and still green, the moment either drifted. Every failure mode here
// throws with the sentinel that is missing, duplicated, out of order or out-dented.
//
// `origin` is the REPORTING FRAME, not just a label: `{file, offset}` where `offset` is the
// 0-based line of `text`'s first line within `file`. The nested `glob-matcher` / `config-parser`
// regions are extracted from the already-de-indented script, so without an offset their failures
// would quote a line number into that intermediate string as if it were a workflow-file line.
const WORKFLOW_ORIGIN = {file: 'assign-reviewers.yml', offset: 0};
const regionSpan = (text, name, origin = WORKFLOW_ORIGIN) => {
  const lines = text.split('\n');
  const at = (index) => origin.offset + index + 1;
  const only = (suffix) => {
    const marker = `// PARITY-HARNESS:${name}:${suffix}`;
    const hits = lines.flatMap((line, index) => line.trim() === marker ? [index] : []);
    if (hits.length !== 1) throw Error(`expected exactly one \`${marker}\` line in ${origin.file}, found ${hits.length}. The parity harness extracts by sentinel; restore the marker rather than reintroducing an indentation-based split.`);
    return hits[0];
  };
  const begin = only('begin'), end = only('end');
  if (end < begin) throw Error(`\`// PARITY-HARNESS:${name}\` sentinels are inverted in ${origin.file}: begin on line ${at(begin)}, end on line ${at(end)}.`);
  const indent = lines[begin].length - lines[begin].trimStart().length;
  // `<= end` deliberately includes the `:end` marker line: out-denting it out of the block
  // scalar changes the shipped YAML's shape while leaving a body-only check green.
  for (let index = begin + 1; index <= end; index++) {
    if (lines[index].trim() && !lines[index].startsWith(' '.repeat(indent))) throw Error(`line ${at(index)} of ${origin.file} is indented less than its \`// PARITY-HARNESS:${name}:begin\` sentinel; the extracted span would not be valid JavaScript.`);
  }
  return {text: lines.slice(begin + 1, end).map((line) => line.slice(indent)).join('\n'), lines, begin, end, indent};
};
const region = (text, name, origin) => regionSpan(text, name, origin).text;
// Ordering the sentinels correctly is not enough: they must bracket the WHOLE `script: |`
// scalar. JavaScript placed ABOVE `:begin` or BELOW `:end` still ships to production while
// being excluded from the bytes `runScript` and `helpers` execute — the same silent
// wrong-span failure this harness exists to eliminate, pointing the other way.
const assertBracketsBlockScalar = (span, name, origin = WORKFLOW_ORIGIN) => {
  const {lines, begin, end, indent} = span;
  const at = (index) => origin.offset + index + 1;
  let above = begin - 1;
  while (above >= 0 && !lines[above].trim()) above--;
  if (above < 0 || !/^\s*script:\s*\|[0-9+-]*\s*$/.test(lines[above])) {
    throw Error(`\`// PARITY-HARNESS:${name}:begin\` (line ${at(begin)} of ${origin.file}) must be the FIRST line of the \`script: |\` scalar, but line ${at(above)} is \`${(lines[above] ?? '').trim()}\`; JavaScript above the sentinel ships untested.`);
  }
  let below = end + 1;
  while (below < lines.length && !lines[below].trim()) below++;
  if (below < lines.length && lines[below].startsWith(' '.repeat(indent))) {
    throw Error(`\`// PARITY-HARNESS:${name}:end\` (line ${at(end)} of ${origin.file}) must be the LAST line of the \`script: |\` scalar, but line ${at(below)} (\`${lines[below].trim()}\`) is still inside it; JavaScript below the sentinel ships untested.`);
  }
  return span;
};
const scriptSpan = assertBracketsBlockScalar(regionSpan(workflow, 'script'), 'script');
const script = scriptSpan.text;
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const runScript = new AsyncFunction('github', 'context', 'core', 'process', 'require', script);
// The two parity-critical helpers, evaluated straight out of the shipped script so the corpus
// below drives the same bytes the runtime does. Their Python ports in refresh-reviewers/generate.py
// are driven from that same corpus file by test_generate.py. These are sub-spans of the script by
// design, so they take the sentinel and indent checks but not the whole-scalar bracket check;
// their origin offset keeps reported line numbers absolute in assign-reviewers.yml.
const SCRIPT_ORIGIN = {file: 'assign-reviewers.yml', offset: scriptSpan.begin + 1};
// `core` is ambient inside a github-script step but NOT inside this `new Function` scope, so the
// extracted region has to be handed one. It is a real capture, not a silencer: `parseReviewerConfig`
// calls `core.warning` on a duplicate top-level `default_pool:`, and `helperWarnings` is what lets a
// test assert that warning fires. Leave it out and the region throws ReferenceError on that path —
// which is a REAL failure mode of the shipped script too, so extend the stub rather than the sentinels
// whenever the parser reaches for another `core` method.
const helperWarnings = [];
const helperCore = {warning: (message) => helperWarnings.push(message), info: () => {}};
const helpers = new Function('core', `${region(script, 'glob-matcher', SCRIPT_ORIGIN)}\n${region(script, 'config-parser', SCRIPT_ORIGIN)}\nreturn {globToRegExp, matchesAny, parseReviewerConfig};`)(helperCore);
const corpus = JSON.parse(readFileSync(resolve(__dirname, '../parser-corpus.json'), 'utf8'));
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
// REVIEWER_SKIP_BASE_BRANCHES: the stacked-PR lane knob. The default-off case is
// asserted first and hardest, because it is every existing caller.
test('base-branch skip is off until the var is set', async () => {
  assert.deepEqual((await run()).selected, ['alice']);
  assert.deepEqual((await run({env: {SKIP_BASE_BRANCHES: ''}})).selected, ['alice']);
  assert.deepEqual((await run({env: {SKIP_BASE_BRANCHES: '   '}})).selected, ['alice']);
});
test('a PR onto a skipped base is not routed, and one onto the default branch still is', async () => {
  assert.deepEqual((await run({pr: {base: {sha: 'base', ref: 'stack/router-queue-poc'}}, env: {SKIP_BASE_BRANCHES: 'stack/**'}})).selected, []);
  assert.deepEqual((await run({env: {SKIP_BASE_BRANCHES: 'stack/**'}})).selected, ['alice']);
});
test('base-branch globs use the same semantics as the path rules', async () => {
  const skip = (ref, patterns) => run({pr: {base: {sha: 'base', ref}}, env: {SKIP_BASE_BRANCHES: patterns}}).then(r => r.selected.length === 0);
  // `stack/**` spans segments, so a nested stack branch is covered too.
  assert.equal(await skip('stack/a/b', 'stack/**'), true);
  // A bare pattern is an exact match, never a prefix: `release` must not swallow
  // `release/1.2` or `releases`. Getting this wrong silently disables routing on
  // branches nobody meant to exempt.
  assert.equal(await skip('release', 'release'), true);
  assert.equal(await skip('release/1.2', 'release'), false);
  assert.equal(await skip('releases', 'release'), false);
  // `*` stays within a segment.
  assert.equal(await skip('stack/one', 'stack/*'), true);
  assert.equal(await skip('stack/one/two', 'stack/*'), false);
  // Several patterns, whitespace-separated.
  assert.equal(await skip('wip/thing', 'stack/** wip/**'), true);
  // A non-matching pattern list leaves routing alone.
  assert.equal(await skip('main', 'stack/**'), false);
});
test('base-branch skip happens before any API call', async () => {
  // It is an early exit, so it must cost nothing: no file listing, no config read.
  const result = await run({pr: {base: {sha: 'base', ref: 'stack/x'}}, env: {SKIP_BASE_BRANCHES: 'stack/**'}});
  assert.deepEqual(result.calls, []);
  assert.deepEqual(result.reads, []);
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

// --- shared reviewers.yml parser corpus -------------------------------------
// One fixture file, two hand-ported implementations. Anything asserted below is asserted against
// the Python port by .github/refresh-reviewers/tests/test_generate.py from the SAME file, so a
// case added here lands on both sides at once. Never restate a corpus case as an inline literal.
test('the shared corpus is loaded and non-empty', () => {
  assert(corpus.configs.length > 0, 'parser-corpus.json has no config cases');
  assert(corpus.globs.length > 0, 'parser-corpus.json has no glob cases');
  assert(corpus.globs.every(entry => entry.cases.length > 0), 'a corpus glob entry has no path cases');
});
for (const {name, text, expected} of corpus.configs) {
  test(`corpus config — ${name}`, () => {
    assert.deepEqual(helpers.parseReviewerConfig(text), expected);
  });
}
// The corpus compares parsed CONFIGS, which is deliberately silent about the duplicate-key warning
// — the Python port prints its own `::warning::` line instead, so the text cannot live in the shared
// fixture. Each side therefore asserts its own channel; last-wins itself stays corpus-pinned above.
test('a duplicate top-level default_pool: warns once, naming the key', () => {
  helperWarnings.length = 0;
  assert.deepEqual(helpers.parseReviewerConfig('default_pool: [alice]\ndefault_pool:\n  - bob\n').default_pool, ['bob']);
  assert.equal(helperWarnings.length, 1);
  assert.match(helperWarnings[0], /duplicate top-level `default_pool:` key/);
});

test('the duplicate warning names the configured path, and omits the prefix without one', () => {
  // `reviewer_config_path` is a caller input, so the warning must not hardcode
  // `reviewers.yml` — a caller that configured another name would be told to go
  // look at a file its repo does not have.
  helperWarnings.length = 0;
  helpers.parseReviewerConfig('default_pool: [alice]\ndefault_pool: [bob]\n', '.github/owners.yml');
  assert.equal(helperWarnings.length, 1);
  assert.match(helperWarnings[0], /^\.github\/owners\.yml: duplicate top-level/);
  // Omitted (as the harness calls it): a bare message, never a literal `undefined:`.
  helperWarnings.length = 0;
  helpers.parseReviewerConfig('default_pool: [alice]\ndefault_pool: [bob]\n');
  assert.equal(helperWarnings.length, 1);
  assert.doesNotMatch(helperWarnings[0], /undefined/);
  assert.match(helperWarnings[0], /^duplicate top-level/);
});

test('an empty first default_pool: still warns on the duplicate', () => {
  // Keyed on "the key was seen", not on "the list is non-empty" — an emptiness test would make the
  // JS warn where the Python port (which keys on its own seen-flag) does not.
  helperWarnings.length = 0;
  assert.deepEqual(helpers.parseReviewerConfig('default_pool: []\ndefault_pool: [bob]\n').default_pool, ['bob']);
  assert.equal(helperWarnings.length, 1);
});
test('a single default_pool: is silent, however it is written', () => {
  helperWarnings.length = 0;
  for (const text of ['default_pool: [alice]\n', 'default_pool:\n  - alice\n', 'rules:\n  - reviewers: [a]\n', '  default_pool: [indented]\ndefault_pool: [alice]\n']) {
    helpers.parseReviewerConfig(text);
  }
  assert.deepEqual(helperWarnings, []);
});
for (const {glob, cases} of corpus.globs) {
  test(`corpus glob — ${glob}`, () => {
    for (const {path, matches} of cases) {
      assert.equal(helpers.globToRegExp(glob).test(path), matches, `globToRegExp(${JSON.stringify(glob)}).test(${JSON.stringify(path)})`);
      assert.equal(helpers.matchesAny(path, [glob]), matches, `matchesAny(${JSON.stringify(path)}, [${JSON.stringify(glob)}])`);
    }
  });
}
test('sentinel extraction fails loudly when a marker is missing', () => {
  assert.throws(() => region(workflow.replace('// PARITY-HARNESS:config-parser:begin', '// removed'), 'config-parser'), /found 0/);
});
test('sentinel extraction fails loudly when a marker is duplicated', () => {
  assert.throws(() => region(workflow.replace('// PARITY-HARNESS:script:end', '// PARITY-HARNESS:script:end\n            // PARITY-HARNESS:script:end'), 'script'), /found 2/);
});
test('sentinel extraction fails loudly when the markers are inverted', () => {
  assert.throws(() => region('  // PARITY-HARNESS:demo:end\n  const x = 1;\n  // PARITY-HARNESS:demo:begin\n', 'demo'), /inverted/);
});
test('sentinel extraction fails loudly when the span out-dents past its begin marker', () => {
  assert.throws(() => region('  // PARITY-HARNESS:demo:begin\nconst x = 1;\n  // PARITY-HARNESS:demo:end\n', 'demo'), /indented less than/);
});
test('sentinel extraction de-indents to the begin marker and keeps blank lines', () => {
  assert.equal(region('  // PARITY-HARNESS:demo:begin\n  const x = 1;\n\n    const y = 2;\n  // PARITY-HARNESS:demo:end\n', 'demo'), 'const x = 1;\n\n  const y = 2;');
});
test('sentinel extraction fails loudly when the END marker itself is out-dented', () => {
  // The body-only check used to stop at `end - 1`, so out-denting `:end` clean out of the
  // block scalar changed the shipped YAML's shape while the suite stayed green.
  assert.throws(() => region('  // PARITY-HARNESS:demo:begin\n  const x = 1;\n// PARITY-HARNESS:demo:end\n', 'demo'), /indented less than/);
});
test('nested region failures report line numbers absolute in the workflow file', () => {
  const nested = 'x\n  // PARITY-HARNESS:demo:begin\nconst bad = 1;\n  // PARITY-HARNESS:demo:end\n';
  // Framed at a nested position, the offending line is reported in workflow coordinates...
  assert.throws(() => region(nested, 'demo', {file: 'assign-reviewers.yml', offset: 99}), /line 102 of assign-reviewers\.yml/);
  // ...and without the frame it would name line 3 of an intermediate string, which is the
  // misreport this origin threading exists to prevent. Both directions are asserted so the
  // offset cannot be dropped and stay green.
  assert.throws(() => region(nested, 'demo'), /line 3 of assign-reviewers\.yml/);
});
test('the nested-region frame matches where the script really starts in the workflow', () => {
  const workflowLines = workflow.split('\n');
  assert.equal(workflowLines[SCRIPT_ORIGIN.offset - 1].trim(), '// PARITY-HARNESS:script:begin');
  assert.equal(workflowLines[SCRIPT_ORIGIN.offset].trim(), script.split('\n')[0].trim());
});
test('the script sentinels must bracket the whole `script: |` scalar', () => {
  const scalar = (body) => `      - name: run\n        with:\n          script: |\n${body}\n      - name: next\n`;
  const good = scalar('            // PARITY-HARNESS:demo:begin\n            const x = 1;\n            // PARITY-HARNESS:demo:end');
  assert.doesNotThrow(() => assertBracketsBlockScalar(regionSpan(good, 'demo'), 'demo'));
  // JavaScript ABOVE the begin sentinel ships but is never executed by this suite.
  const above = scalar('            const stray = 1;\n            // PARITY-HARNESS:demo:begin\n            const x = 1;\n            // PARITY-HARNESS:demo:end');
  assert.throws(() => assertBracketsBlockScalar(regionSpan(above, 'demo'), 'demo'), /must be the FIRST line/);
  // ...and BELOW the end sentinel, likewise.
  const below = scalar('            // PARITY-HARNESS:demo:begin\n            const x = 1;\n            // PARITY-HARNESS:demo:end\n            const stray = 2;');
  assert.throws(() => assertBracketsBlockScalar(regionSpan(below, 'demo'), 'demo'), /must be the LAST line/);
});
test('the shipped script region really does span its whole block scalar', () => {
  // Non-vacuous guard on the assertion above: it runs at import time against the real file,
  // so this pins that the real file is the shape it accepts, not merely that it did not throw.
  const workflowLines = workflow.split('\n');
  assert.match(workflowLines[scriptSpan.begin - 1], /^\s*script:\s*\|\s*$/);
  const after = workflowLines.slice(scriptSpan.end + 1).find((line) => line.trim());
  assert.ok(after, '`script:end` is the last non-blank line of the file; expected the next workflow step');
  assert.ok(!after.startsWith(' '.repeat(scriptSpan.indent)), 'a line after `script:end` is still inside the scalar');
});
