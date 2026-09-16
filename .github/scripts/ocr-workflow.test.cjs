'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const { prepareReview, finishReview, prepareClaude, finishClaude } = require('./ocr-workflow.cjs');
const { MARKER, encodeState, readState } = require('./ocr-state.cjs');

const HEAD = 'a'.repeat(40), BASE = 'b'.repeat(40), PRIOR = 'c'.repeat(40), MERGE = 'd'.repeat(40);
const POLICY = 'e'.repeat(64);
const policyFiles = ['.github/workflows/ocr.yml', '.github/scripts/ocr-gate.cjs',
  '.github/scripts/ocr-state.cjs', '.github/scripts/ocr-workflow.cjs'];
function state(overrides = {}) {
  return { version: 1, head: PRIOR, base: BASE, policy: POLICY, run: '10', passed: true, claudeHead: null, ...overrides };
}
function comment(value) {
  return { id: 7, user: { login: 'github-actions[bot]', type: 'Bot' },
    body: `${MARKER}\nGate explanation\n${encodeState(value)}` };
}
function nativeComment(overrides = {}) {
  const payload = { v: 1, head: PRIOR, run: '10', pr: 12, terminal_state: 'complete', ...overrides };
  return { id: 8, user: { login: 'github-actions[bot]', type: 'Bot' },
    body: `<!-- ocr-summary -->\n<!-- ocr-checkpoint:v1 ${Buffer.from(JSON.stringify(payload)).toString('base64')} -->` };
}
function report(from = MERGE) {
  const item = { item_id: 'a', path: 'a.py' };
  return { status: 'complete', comments: [], manifest: {
    schema_version: 'ocr.run-manifest/v1', operation: 'review', terminal_state: 'complete',
    input: { mode: 'range', resolved_head: HEAD, resolved_base: from },
    coverage: { selected: [item], completed: [item], reused: [], failed: [], waived: [] },
  } };
}
function harness(overrides = {}) {
  const h = {
    env: { PR_NUMBER: '12', REVIEW_HEAD: HEAD, REVIEW_BASE: BASE, POLICY,
      GITHUB_WORKSPACE: '/workspace', RUNNER_TEMP: '/runner', OCR_OUTCOME: 'success',
      FULL_REVIEW: 'true', RANGE_MODE: 'full', RANGE_FROM: '', RANGE_TO: HEAD,
      POSTING_FAILED: '0', SAME_REPO: 'true', GITHUB_SERVER_URL: 'https://github.com',
      GITHUB_REPOSITORY: 'owner/repo', ...overrides },
    context: { repo: { owner: 'owner', repo: 'repo' }, runId: 20 },
    comments: [], outputs: {}, outputHistory: [], writes: [], copies: [], gitCalls: [], failures: [],
    result: report(), pr: { state: 'open', head: { sha: HEAD }, base: { sha: BASE } },
    files: new Map(policyFiles.map(file => [`/workspace/${file}`, `trusted ${file}`])),
  };
  h.core = {
    setOutput(key, value) { h.outputs[key] = value; h.outputHistory.push([key, value]); },
    info() {}, warning() {}, setFailed(message) { h.failures.push(message); },
    summary: { addRaw(body) { h.summary = body; return this; }, async write() {
      if (h.summaryError) throw new Error('summary failed');
    } },
  };
  h.fs = {
    readFileSync(file) {
      if (file === '/tmp/ocr-result.json') return JSON.stringify(h.result);
      if (h.files.has(file)) return h.files.get(file);
      throw new Error(`Missing file: ${file}`);
    },
    mkdirSync() {},
    copyFileSync(source, destination) { h.copies.push([source, destination]); h.files.set(destination, h.files.get(source)); },
  };
  const publish = async payload => {
    if (h.publishError) throw new Error('publication failed');
    h.writes.push(payload);
    h.comments = [{ ...comment(state()), id: payload.comment_id || 7, body: payload.body }];
  };
  h.github = {
    paginate: async () => h.comments,
    rest: { pulls: { get: async () => ({ data: h.pr }) },
      issues: { listComments() {}, updateComment: publish, createComment: publish } },
  };
  h.execFileSync = (command, args) => {
    h.gitCalls.push([command, args]);
    if (args.includes('--is-ancestor')) {
      if (h.notAncestor) throw new Error('not ancestor');
      return '';
    }
    return `${MERGE}\n`;
  };
  return h;
}
function incremental(h) {
  Object.assign(h.env, { FULL_REVIEW: 'false', RANGE_MODE: 'checkpoint', RANGE_FROM: PRIOR,
    CHECKPOINT_BEFORE: PRIOR, RANGE_SOURCE_RUN: '10', RANGE_ANCESTRY: 'ancestor',
    EXPECTED_CHECKPOINT: PRIOR, EXPECTED_SOURCE_RUN: '10' });
  h.comments = [comment(state())];
  h.result = report(PRIOR);
  return h;
}

test('first full review publishes an accepted checkpoint only after publication', async () => {
  const h = harness();
  await prepareReview(h);
  assert.equal(h.outputs.full_review, 'true');
  h.env.POLICY = h.outputs.policy;
  await finishReview(h);
  assert.equal(h.outputs.passed, 'true');
  assert.deepEqual(h.outputHistory.filter(([key]) => key === 'passed'), [['passed', 'false'], ['passed', 'true']]);
  assert.deepEqual(readState(h.comments), state({ head: HEAD, policy: h.env.POLICY, run: '20' }));
  assert.equal(h.outputs.claude_from, MERGE);
  assert.equal(h.outputs.claude_mode, 'full');
  assert.deepEqual(h.failures, []);
});

test('trusted prior checkpoint selects and verifies the exact incremental range', async () => {
  const h = incremental(harness());
  await prepareReview(h);
  h.comments = [comment(state({ policy: h.outputs.policy })), nativeComment()];
  await prepareReview(h);
  assert.equal(h.outputs.full_review, 'false');
  assert.equal(h.outputs.checkpoint, PRIOR);
  assert.equal(h.outputs.source_run, '10');
  await finishReview(h);
  assert.equal(h.outputs.passed, 'true');
  assert.equal(h.writes[0].comment_id, 7);
  assert.ok(h.gitCalls.some(([command, args]) => command === 'git'
    && JSON.stringify(args) === JSON.stringify(['merge-base', '--is-ancestor', PRIOR, HEAD])));
});

test('blocked or partial reviews overwrite eligibility and force the next run to be full', async () => {
  for (const mutate of [h => { h.result.comments = [{ severity: 'high', content: 'Blocking finding' }]; },
    h => { h.result.status = 'partial'; }, h => { h.env.POSTING_FAILED = '1'; },
    h => { h.env.OCR_OUTCOME = 'failure'; }]) {
    const h = incremental(harness());
    await prepareReview(h);
    h.env.POLICY = h.outputs.policy;
    mutate(h);
    await finishReview(h);
    assert.equal(h.outputs.passed, 'false');
    assert.equal(readState(h.comments).passed, false);
    h.env.REVIEW_HEAD = 'f'.repeat(40);
    h.pr.head.sha = h.env.REVIEW_HEAD;
    await prepareReview(h);
    assert.equal(h.outputs.full_review, 'true');
    assert.equal(h.outputs.checkpoint, '');
  }
});

test('mismatched checkpoint provenance, manifest range, or ancestry blocks acceptance', async () => {
  for (const mutate of [h => { h.env.RANGE_FROM = MERGE; },
    h => { h.env.RANGE_SOURCE_RUN = '9'; }, h => { h.env.CHECKPOINT_BEFORE = MERGE; },
    h => { h.env.RANGE_TO = PRIOR; }, h => { h.notAncestor = true; },
    h => { h.result.manifest.input.resolved_base = MERGE; }]) {
    const h = incremental(harness()); mutate(h);
    await finishReview(h);
    assert.equal(h.outputs.passed, 'false');
    assert.equal(readState(h.comments).passed, false);
  }
});

test('stale or closed PRs fail before posting review or Claude state', async () => {
  for (const operation of [prepareReview, finishReview, prepareClaude, finishClaude]) {
    for (const mutate of [h => { h.pr.head.sha = PRIOR; }, h => { h.pr.base.sha = PRIOR; },
      h => { h.pr.state = 'closed'; }]) {
      const h = harness({ CLAUDE_OUTCOME: 'success', CLAUDE_CONCLUSION: 'success',
        CLAUDE_RESULT: JSON.stringify({ complete: true, reviewed_head: HEAD, blocking_findings: 0 }) });
      mutate(h);
      await assert.rejects(operation(h), /PR changed/);
      assert.deepEqual(h.writes, []);
      assert.notEqual(h.outputs.passed, 'true');
    }
  }
});

test('publication or summary failure never exposes a passed output', async () => {
  for (const flag of ['publishError', 'summaryError']) {
    const h = harness(); h[flag] = true;
    await assert.rejects(finishReview(h), /failed/);
    assert.equal(h.outputs.passed, 'false');
    assert.ok(!h.outputHistory.some(([key, value]) => key === 'passed' && value === 'true'));
  }
});

test('Claude uses a delta only when it previously completed the accepted checkpoint', async () => {
  for (const claudeHead of ['', HEAD, PRIOR]) {
    const h = incremental(harness({ CLAUDE_HEAD: claudeHead }));
    await finishReview(h);
    assert.equal(h.outputs.claude_mode, claudeHead === PRIOR ? 'incremental' : 'full');
    assert.equal(h.outputs.claude_from, claudeHead === PRIOR ? PRIOR : MERGE);
  }
});

test('successful action without a verified complete clean Claude result never records completion', async () => {
  const valid = { complete: true, reviewed_head: HEAD, blocking_findings: 0 };
  for (const overrides of [
    { CLAUDE_RESULT: undefined }, { CLAUDE_RESULT: 'invalid' },
    ...[{}, { ...valid, complete: false }, { ...valid, reviewed_head: PRIOR },
      { ...valid, blocking_findings: 1 }, { ...valid, blocking_findings: '0' }]
      .map(result => ({ CLAUDE_RESULT: JSON.stringify(result) })),
    { CLAUDE_CONCLUSION: 'failure' }, { CLAUDE_OUTCOME: 'failure' },
  ]) {
    const h = harness({ CLAUDE_OUTCOME: 'success', CLAUDE_CONCLUSION: 'success',
      CLAUDE_RESULT: JSON.stringify(valid), ...overrides });
    h.comments = [comment(state({ head: HEAD, run: '20' }))];
    await finishClaude(h);
    assert.deepEqual(h.writes, []);
    assert.equal(readState(h.comments).claudeHead, null);
  }
});

test('verified Claude completion updates only the matching accepted gate and preserves its explanation', async () => {
  for (const overrides of [{}, { head: PRIOR }, { base: PRIOR }, { policy: 'f'.repeat(64) },
    { run: '19' }, { passed: false }]) {
    const h = harness({ CLAUDE_OUTCOME: 'success', CLAUDE_CONCLUSION: 'success',
      CLAUDE_RESULT: JSON.stringify({ complete: true, reviewed_head: HEAD, blocking_findings: 0 }) });
    h.comments = [comment(state({ head: HEAD, run: '20', ...overrides }))];
    await finishClaude(h);
    assert.equal(h.writes.length, Object.keys(overrides).length ? 0 : 1);
    if (!Object.keys(overrides).length) {
      assert.equal(readState(h.comments).claudeHead, HEAD);
      assert.match(h.comments[0].body, /Gate explanation/);
    }
  }
});

test('prepare snapshots every trusted script and fingerprints changes to each policy file', async () => {
  const original = harness(); await prepareReview(original);
  assert.match(original.outputs.policy, /^[a-f0-9]{64}$/);
  assert.deepEqual(original.copies.map(([source]) => source), policyFiles.filter(file => file.endsWith('.cjs')).map(file => `/workspace/${file}`));
  for (const [source, destination] of original.copies) {
    assert.equal(destination, path.join('/runner/scout-ocr-policy', path.basename(source)));
    assert.equal(original.files.get(destination), original.files.get(source));
  }
  for (const file of policyFiles) {
    const changed = harness(); changed.files.set(`/workspace/${file}`, 'changed policy');
    changed.comments = [comment(state({ policy: original.outputs.policy })), nativeComment()];
    await prepareReview(changed);
    assert.notEqual(changed.outputs.policy, original.outputs.policy);
    assert.equal(changed.outputs.full_review, 'true');
  }
});

test('cancelled native advancement forces full before spending on an unusable delta', async () => {
  const h = harness();
  await prepareReview(h);
  const prior = comment(state({ policy: h.outputs.policy }));
  for (const native of [[], [nativeComment({ head: HEAD, run: '19' })], [nativeComment({ pr: 99 })],
    [nativeComment(), nativeComment()]]) {
    h.comments = [prior, ...native];
    await prepareReview(h);
    assert.equal(h.outputs.full_review, 'true');
    assert.equal(h.outputs.checkpoint, '');
  }
});

test('prior review context is fetched with fixed read APIs before Claude, not an open CLI API grant', async () => {
  const h = harness();
  const calls = [];
  h.github.rest.pulls.listReviewComments = () => {};
  h.github.rest.pulls.listReviews = () => {};
  h.github.paginate = async (method, args) => { calls.push([method, args]); return [{ body: 'untrusted review text' }]; };
  h.fs.writeFileSync = (file, body) => h.files.set(file, body);
  await prepareClaude(h);
  assert.deepEqual(calls.map(([method]) => method), [h.github.rest.issues.listComments,
    h.github.rest.pulls.listReviewComments, h.github.rest.pulls.listReviews]);
  for (const [, args] of calls) assert.equal(args.issue_number || args.pull_number, 12);
  const artifact = JSON.parse(h.files.get('/runner/scout-prior-review.json'));
  assert.equal(artifact.inline[0].body, 'untrusted review text');
  h.github.paginate = async () => { throw new Error('API unavailable'); };
  await assert.rejects(prepareClaude(h), /API unavailable/);
});


test('policy checkouts use the executing workflow revision even when PR base predates the helpers', () => {
  const workflow = require('node:fs').readFileSync(path.join(__dirname, '../workflows/ocr.yml'), 'utf8');
  const checkouts = workflow.split(/      - name: /).filter(step => step.includes('uses: actions/checkout@'));
  assert.equal(checkouts.length, 2);
  for (const checkout of checkouts) {
    assert.match(checkout, /ref: \$\{\{ github\.workflow_sha \}\}/);
    assert.doesNotMatch(checkout, /ref:.*(?:outputs\.base|outputs\.head|pull_request)/);
  }
  // The PR comparison base remains separate from the policy source revision.
  assert.match(workflow, /REVIEW_BASE: \$\{\{ needs\.prepare\.outputs\.base \}\}/);
});
