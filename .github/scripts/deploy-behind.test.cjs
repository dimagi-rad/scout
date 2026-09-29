const { test } = require('node:test');
const assert = require('node:assert/strict');
const { THRESHOLD_MINUTES, assessDeployLag, checkDeployLag } = require('./deploy-behind.cjs');
const { LABEL, TITLE } = require('./deploy-failure-issue.cjs');

const HEAD = 'h'.repeat(40);
const LIVE = 'l'.repeat(40);
const NOW = Date.parse('2026-09-29T12:00:00Z');
const minutesAgo = (m) => new Date(NOW - m * 60000).toISOString();
const context = { repo: { owner: 'o', repo: 'r' }, runId: 999, ref: 'refs/heads/main' };
const env = { GITHUB_SERVER_URL: 'https://github.com' };
const workflowId = 'deploy.yml';
const jobName = 'deploy';

const run = (id, run_number, head_sha, status, extra = {}) => ({
  id, run_number, head_sha, status, conclusion: status === 'completed' ? 'success' : null,
  created_at: minutesAgo(60), html_url: `https://github.com/o/r/actions/runs/${id}`,
  ...extra,
});

// `deployed` maps a run id to its deploy job's conclusion (default success).
function fakeGithub({ runs = [], deployed = {}, commitDate = minutesAgo(120), issues = [], comments = [] } = {}) {
  const calls = [];
  const record = (name, result) => async (args) => {
    calls.push([name, args]);
    return { data: typeof result === 'function' ? result(args) : result };
  };
  return {
    calls,
    paginate: async (fn, args) => (await fn(args)).data,
    rest: {
      repos: {
        getBranch: record('branch', { commit: { sha: HEAD, commit: { committer: { date: commitDate } } } }),
      },
      actions: {
        listWorkflowRuns: record('runs', { workflow_runs: runs }),
        listJobsForWorkflowRun: record('jobs', (args) => ({
          jobs: [{ name: 'deploy', conclusion: deployed[args.run_id] || 'success' }],
        })),
      },
      issues: {
        listForRepo: record('list', issues),
        listComments: record('comments', comments),
        getLabel: record('getLabel', {}),
        createLabel: record('createLabel', {}),
        create: record('create', { number: 7 }),
        createComment: record('comment', {}),
      },
    },
  };
}

function fakeCore() {
  const out = { infos: [], warnings: [] };
  return { out, info: (m) => out.infos.push(m), warning: (m) => out.warnings.push(m) };
}

const assess = (github, thresholdMinutes = THRESHOLD_MINUTES) => assessDeployLag({
  github, context, core: fakeCore(), workflowId, jobName, now: NOW, thresholdMinutes,
});
const check = (github, core = fakeCore()) => checkDeployLag({
  github, context, core, env, workflowId, jobName, now: NOW,
});
const names = (github) => github.calls.map(([name]) => name);

// Run 12 (HEAD) was cancelled while pending, so run 11 skipped for nothing.
const strandedRuns = (createdMinutesAgo = 90) => [
  run(12, 112, HEAD, 'completed', { conclusion: 'cancelled', created_at: minutesAgo(createdMinutesAgo) }),
  run(11, 111, 'b'.repeat(40), 'completed'),
  run(10, 110, LIVE, 'completed'),
];
const strandedDeploys = { 12: 'skipped', 11: 'skipped' };

test('production running main is current', async () => {
  const github = fakeGithub({ runs: [run(10, 110, HEAD, 'completed')] });
  const lag = await assess(github);
  assert.equal(lag.state, 'current');
  assert.equal(lag.live.id, 10);
  assert.deepEqual(github.calls[1][1], {
    owner: 'o', repo: 'r', workflow_id: 'deploy.yml', branch: 'main', per_page: 100,
  });
});

test('main ahead past the threshold with nothing pending is behind', async () => {
  const github = fakeGithub({ runs: strandedRuns(), deployed: strandedDeploys });
  const lag = await assess(github);
  assert.equal(lag.state, 'behind');
  assert.equal(lag.live.id, 10);
  assert.equal(lag.latest.id, 12);
  assert.equal(lag.minutes, 90);
});

for (const status of ['pending', 'queued', 'waiting', 'in_progress']) {
  test(`a ${status} production run means a deploy is on the way`, async () => {
    const github = fakeGithub({
      runs: [run(13, 113, HEAD, status), ...strandedRuns()], deployed: strandedDeploys,
    });
    const lag = await assess(github);
    assert.equal(lag.state, 'deploying');
    assert.equal(lag.run.id, 13);
    assert.deepEqual(names(github), ['branch', 'runs']);
  });
}

test('the threshold is measured from the push, not the commit date', async () => {
  const github = () => fakeGithub({
    runs: strandedRuns(THRESHOLD_MINUTES - 1), deployed: strandedDeploys, commitDate: minutesAgo(600),
  });
  assert.equal((await assess(github())).state, 'waiting');
  assert.equal((await assess(github(), THRESHOLD_MINUTES - 1)).state, 'behind');
});

test('the earliest run of the head commit starts the clock', async () => {
  const runs = [
    run(14, 114, HEAD, 'completed', { conclusion: 'cancelled', created_at: minutesAgo(5) }),
    ...strandedRuns(50),
  ];
  const lag = await assess(fakeGithub({ runs, deployed: { ...strandedDeploys, 14: 'skipped' } }));
  assert.equal(lag.state, 'behind');
  assert.equal(lag.minutes, 50);
  assert.equal(lag.latest.id, 14);
});

test('a head commit with no deploy run falls back to its commit date', async () => {
  const runs = [run(10, 110, LIVE, 'completed')];
  assert.equal((await assess(fakeGithub({ runs, commitDate: minutesAgo(10) }))).state, 'waiting');
  const lag = await assess(fakeGithub({ runs, commitDate: minutesAgo(60) }));
  assert.equal(lag.state, 'behind');
  assert.equal(lag.latest, null);
});

test('no recent successful deploy still counts as behind', async () => {
  const github = fakeGithub({ runs: strandedRuns(), deployed: { ...strandedDeploys, 10: 'failure' } });
  const lag = await assess(github);
  assert.equal(lag.state, 'behind');
  assert.equal(lag.live, null);
});

test('being behind opens the deploy-failure issue with a re-run instruction', async () => {
  const github = fakeGithub({ runs: strandedRuns(), deployed: strandedDeploys });
  const core = fakeCore();
  await check(github, core);
  assert.deepEqual(names(github).filter((n) => !['branch', 'runs', 'jobs'].includes(n)), ['list', 'getLabel', 'create']);
  const [, created] = github.calls.at(-1);
  assert.equal(created.title, TITLE);
  assert.deepEqual(created.labels, [LABEL]);
  assert.match(created.body, /Production is behind main: main is at `hhhhhhhhhhhh`, production runs `llllllllllll`/);
  assert.match(created.body, /ahead for 90 minutes/);
  assert.ok(created.body.includes('ended `cancelled`: https://github.com/o/r/actions/runs/12'), created.body);
  assert.match(created.body, /Re-run the latest deploy/);
  assert.ok(created.body.includes('https://github.com/o/r/actions/workflows/deploy.yml'), created.body);
  assert.match(core.out.warnings[0], /opened #7/);
});

test('being behind comments once per head commit on an open issue', async () => {
  const issues = [{ number: 3, pull_request: {} }, { number: 5, body: 'deploy stage failed' }];
  const github = fakeGithub({ runs: strandedRuns(), deployed: strandedDeploys, issues });
  await check(github);
  const comment = github.calls.find(([name]) => name === 'comment')[1];
  assert.equal(comment.issue_number, 5);
  assert.match(comment.body, /Production is behind main/);

  const repeat = fakeGithub({
    runs: strandedRuns(), deployed: strandedDeploys, issues, comments: [{ body: comment.body }],
  });
  const core = fakeCore();
  await check(repeat, core);
  assert.equal(names(repeat).includes('comment'), false);
  assert.match(core.out.warnings[0], /already reported on #5/);
});

test('a report of an older head commit does not silence a newer one', async () => {
  const issues = [{ number: 5, body: '<!-- deploy-behind:' + 'b'.repeat(40) + ' -->' }];
  const github = fakeGithub({ runs: strandedRuns(), deployed: strandedDeploys, issues });
  await check(github);
  assert.ok(names(github).includes('comment'));
});

for (const [label, runs] of [
  ['current', [run(10, 110, HEAD, 'completed')]],
  ['deploying', [run(13, 113, HEAD, 'in_progress'), ...strandedRuns()]],
  ['waiting', strandedRuns(10)],
]) {
  test(`${label} touches no issue`, async () => {
    const github = fakeGithub({ runs, deployed: strandedDeploys, issues: [{ number: 5 }] });
    assert.equal((await check(github)).state, label);
    assert.deepEqual(names(github).filter((n) => !['branch', 'runs', 'jobs'].includes(n)), []);
  });
}

test('a run parked outside the queue does not hold the alert back', async () => {
  const github = fakeGithub({
    runs: [run(13, 113, HEAD, 'action_required'), ...strandedRuns()], deployed: strandedDeploys,
  });
  assert.equal((await assess(github)).state, 'behind');
});

test('a head run that failed was already reported by deploy.yml', async () => {
  const runs = [
    run(12, 112, HEAD, 'completed', { conclusion: 'failure', created_at: minutesAgo(90) }),
    run(10, 110, LIVE, 'completed'),
  ];
  const github = fakeGithub({ runs, deployed: { 12: 'failure' }, issues: [{ number: 5 }] });
  const lag = await check(github);
  assert.equal(lag.state, 'reported');
  assert.equal(lag.latest.id, 12);
  assert.deepEqual(names(github).filter((n) => !['branch', 'runs', 'jobs'].includes(n)), []);
});

test('no recent deploy says the live commit is unknown, not that a guard is off', async () => {
  const core = fakeCore();
  const github = fakeGithub({ runs: strandedRuns(), deployed: { ...strandedDeploys, 10: 'failure' } });
  await assessDeployLag({ github, context, core, workflowId, jobName, now: NOW, thresholdMinutes: 45 });
  assert.match(core.out.warnings[0], /the commit production runs is unknown\.$/);
});
