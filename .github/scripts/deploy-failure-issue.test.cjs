const { test } = require('node:test');
const assert = require('node:assert/strict');
const { LABEL, TITLE, outcome, reportDeploy } = require('./deploy-failure-issue.cjs');

const context = { repo: { owner: 'o', repo: 'r' }, runId: 42, sha: 'a'.repeat(40), actor: 'merger' };
const env = (TEST_RESULT, DEPLOY_RESULT) => ({ TEST_RESULT, DEPLOY_RESULT, GITHUB_SERVER_URL: 'https://github.com' });
const core = { info() {}, warning() {} };

function fakeGithub({ issues = [], labelExists = true } = {}) {
  const calls = [];
  const record = (name, result) => async (args) => {
    calls.push([name, args]);
    if (typeof result === 'function') return result(args);
    return { data: result };
  };
  return {
    calls,
    rest: {
      issues: {
        listForRepo: record('list', issues),
        getLabel: record('getLabel', () => {
          if (labelExists) return { data: {} };
          throw Object.assign(new Error('Not Found'), { status: 404 });
        }),
        createLabel: record('createLabel', {}),
        create: record('create', { number: 7 }),
        createComment: record('comment', {}),
        update: record('update', {}),
      },
    },
  };
}

test('outcome treats either failure as failure and cancellation as unknown', () => {
  assert.equal(outcome({ testResult: 'failure', deployResult: 'skipped' }), 'failure');
  assert.equal(outcome({ testResult: 'success', deployResult: 'failure' }), 'failure');
  assert.equal(outcome({ testResult: 'success', deployResult: 'success' }), 'success');
  assert.equal(outcome({ testResult: 'success', deployResult: 'cancelled' }), 'unknown');
  assert.equal(outcome({ testResult: 'cancelled', deployResult: 'skipped' }), 'unknown');
});

test('first failure opens a labelled issue that links the run and mentions the pusher', async () => {
  const github = fakeGithub({ labelExists: false });
  assert.equal(await reportDeploy({ github, context, core, env: env('success', 'failure') }), 'failure');
  const names = github.calls.map(([name]) => name);
  assert.deepEqual(names, ['list', 'getLabel', 'createLabel', 'create']);
  const [, created] = github.calls.at(-1);
  assert.equal(created.title, TITLE);
  assert.deepEqual(created.labels, [LABEL]);
  assert.match(created.body, /deploy stage failed for `aaaaaaaaaaaa`/);
  assert.match(created.body, /@merger/);
  assert.ok(created.body.includes(': https://github.com/o/r/actions/runs/42\n'), created.body);
});

test('repeat failures comment on the open issue instead of opening another', async () => {
  const github = fakeGithub({ issues: [{ number: 3, pull_request: { url: 'x' } }, { number: 5 }] });
  await reportDeploy({ github, context, core, env: env('failure', 'skipped') });
  assert.deepEqual(github.calls.map(([name]) => name), ['list', 'comment']);
  assert.equal(github.calls[1][1].issue_number, 5);
  assert.match(github.calls[1][1].body, /tests stage failed/);
});

test('a successful deploy closes the open issue, and is a no-op when none is open', async () => {
  const github = fakeGithub({ issues: [{ number: 5 }] });
  await reportDeploy({ github, context, core, env: env('success', 'success') });
  assert.deepEqual(github.calls.map(([name]) => name), ['list', 'comment', 'update']);
  assert.deepEqual(github.calls[2][1], { owner: 'o', repo: 'r', issue_number: 5, state: 'closed', state_reason: 'completed' });

  const quiet = fakeGithub();
  await reportDeploy({ github: quiet, context, core, env: env('success', 'success') });
  assert.deepEqual(quiet.calls.map(([name]) => name), ['list']);
});

test('cancelled runs touch nothing', async () => {
  const github = fakeGithub({ issues: [{ number: 5 }] });
  assert.equal(await reportDeploy({ github, context, core, env: env('success', 'cancelled') }), 'unknown');
  assert.deepEqual(github.calls, []);
});
