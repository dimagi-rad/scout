const { test } = require('node:test');
const assert = require('node:assert/strict');
const { findSupersedingRun, checkSuperseded } = require('./deploy-supersede.cjs');

const OURS = 'a'.repeat(40);
const context = {
  repo: { owner: 'o', repo: 'r' }, ref: 'refs/heads/main', runId: 10, runNumber: 100, sha: OURS,
};
const run = (id, run_number, status, head_sha) => ({
  id, run_number, status, head_sha, html_url: `https://github.com/o/r/actions/runs/${id}`,
});

function fakeGithub({ runs = [], compare = {}, listError = null } = {}) {
  const calls = [];
  return {
    calls,
    rest: {
      actions: {
        listWorkflowRuns: async (args) => {
          calls.push(['list', args]);
          if (listError) throw listError;
          return { data: { workflow_runs: runs } };
        },
      },
      repos: {
        compareCommitsWithBasehead: async (args) => {
          calls.push(['compare', args]);
          const head = args.basehead.split('...')[1];
          return { data: { status: compare[head] || 'diverged' } };
        },
      },
    },
  };
}

function fakeCore() {
  const out = { outputs: {}, notices: [], warnings: [] };
  return {
    out,
    setOutput: (name, value) => { out.outputs[name] = value; },
    notice: (message) => out.notices.push(message),
    warning: (message) => out.warnings.push(message),
  };
}

test('a newer queued run of a descendant commit supersedes this one', async () => {
  const github = fakeGithub({
    runs: [run(12, 102, 'pending', 'c'.repeat(40)), run(10, 100, 'in_progress', OURS)],
    compare: { ['c'.repeat(40)]: 'ahead' },
  });
  const found = await findSupersedingRun({ github, context, workflowId: 'deploy.yml' });
  assert.equal(found.id, 12);
  assert.deepEqual(github.calls[0][1], {
    owner: 'o', repo: 'r', workflow_id: 'deploy.yml', branch: 'main', per_page: 100,
  });
  assert.equal(github.calls[1][1].basehead, `${OURS}...${'c'.repeat(40)}`);
});

test('the newest descendant wins when several runs are queued', async () => {
  const github = fakeGithub({
    runs: [run(11, 101, 'pending', 'b'.repeat(40)), run(13, 103, 'queued', 'd'.repeat(40))],
    compare: { ['b'.repeat(40)]: 'ahead', ['d'.repeat(40)]: 'ahead' },
  });
  assert.equal((await findSupersedingRun({ github, context, workflowId: 'deploy.yml' })).id, 13);
});

test('older, finished, running and non-descendant runs never supersede', async () => {
  const github = fakeGithub({
    runs: [
      run(9, 99, 'pending', 'e'.repeat(40)),
      run(14, 104, 'completed', 'f'.repeat(40)),
      run(15, 105, 'in_progress', 'f'.repeat(40)),
      run(16, 106, 'pending', '1'.repeat(40)),
    ],
    compare: { ['e'.repeat(40)]: 'ahead', ['f'.repeat(40)]: 'ahead', ['1'.repeat(40)]: 'diverged' },
  });
  assert.equal(await findSupersedingRun({ github, context, workflowId: 'deploy.yml' }), null);
  assert.deepEqual(github.calls.filter(([name]) => name === 'compare').length, 1);
});

test('a rerun of the same commit supersedes without a compare call', async () => {
  const github = fakeGithub({ runs: [run(12, 102, 'waiting', OURS)] });
  assert.equal((await findSupersedingRun({ github, context, workflowId: 'deploy.yml' })).id, 12);
  assert.deepEqual(github.calls.map(([name]) => name), ['list']);
});

test('deploys from other refs are never skipped', async () => {
  const github = fakeGithub({ runs: [run(12, 102, 'pending', OURS)] });
  const other = { ...context, ref: 'refs/heads/feature' };
  assert.equal(await findSupersedingRun({ github, context: other, workflowId: 'deploy.yml' }), null);
  assert.deepEqual(github.calls, []);
});

test('checkSuperseded sets the output and explains the skip', async () => {
  const core = fakeCore();
  const github = fakeGithub({ runs: [run(12, 102, 'pending', OURS)] });
  await checkSuperseded({ github, context, core, workflowId: 'deploy.yml' });
  assert.equal(core.out.outputs.superseded, 'true');
  assert.match(core.out.notices[0], /actions\/runs\/12/);
});

test('checkSuperseded deploys when there is nothing newer', async () => {
  const core = fakeCore();
  await checkSuperseded({ github: fakeGithub(), context, core, workflowId: 'deploy.yml' });
  assert.equal(core.out.outputs.superseded, 'false');
  assert.deepEqual(core.out.notices, []);
});

test('checkSuperseded fails open when the API errors', async () => {
  const core = fakeCore();
  const github = fakeGithub({ listError: new Error('boom') });
  await checkSuperseded({ github, context, core, workflowId: 'deploy.yml' });
  assert.equal(core.out.outputs.superseded, 'false');
  assert.match(core.out.warnings[0], /deploying anyway: boom/);
});
