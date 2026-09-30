const { test } = require('node:test');
const assert = require('node:assert/strict');
const { findReasonToSkip, checkSuperseded } = require('./deploy-supersede.cjs');

const OURS = 'a'.repeat(40);
const sha = (c) => c.repeat(40);
const context = {
  repo: { owner: 'o', repo: 'r' }, ref: 'refs/heads/main', runId: 10, runNumber: 100, sha: OURS,
};
const workflowId = 'deploy.yml';
const jobName = 'deploy';
const run = (id, run_number, status, head_sha, extra = {}) => ({
  id, run_number, status, head_sha, conclusion: null,
  updated_at: '2026-09-29T00:00:00Z', html_url: `https://github.com/o/r/actions/runs/${id}`,
  ...extra,
});
const done = (id, run_number, head_sha, updated_at, extra = {}) => run(
  id, run_number, 'completed', head_sha, { conclusion: 'success', updated_at, ...extra },
);

const ownRun = run(10, 100, 'in_progress', OURS, { created_at: '2026-09-29T00:00:00Z' });

// `compare` maps a head sha to the compare status of OURS...head (default diverged).
// `deployed` maps a run id to its deploy job's conclusion (default success).
// `pages` are the successive answers to the unfiltered run list (the last one
// repeats), standing in for its stale slices; filtered queries always see `runs`.
// Our own run is always on the list, as it is when the supersede job asks.
function fakeGithub({
  runs: given = [], pages = null, compare = {}, deployed = {}, listError = null, deployJob = 'deploy',
  head = OURS,
} = {}) {
  const calls = [];
  const runs = given.some((r) => r.id === ownRun.id) ? given : [...given, ownRun];
  let pageCalls = 0;
  return {
    calls,
    rest: {
      actions: {
        listWorkflowRuns: async (args) => {
          const kind = (args.head_sha && 'listHead') || (args.created && 'listSince') || 'list';
          calls.push([kind, args]);
          if (listError) throw listError;
          if (args.head_sha) {
            return { data: { workflow_runs: runs.filter((r) => r.head_sha === args.head_sha) } };
          }
          if (args.created) {
            const since = Date.parse(args.created.replace(/^>=/, ''));
            return { data: { workflow_runs: runs.filter((r) => Date.parse(r.created_at) >= since) } };
          }
          const page = pages ? pages[Math.min(pageCalls, pages.length - 1)] : runs;
          pageCalls += 1;
          return { data: { workflow_runs: page } };
        },
        listJobsForWorkflowRun: async (args) => {
          calls.push(['jobs', args]);
          const conclusion = deployed[args.run_id] || 'success';
          return { data: { jobs: [{ name: 'test / test', conclusion: 'success' }, { name: deployJob, conclusion }] } };
        },
      },
      repos: {
        getBranch: async (args) => {
          calls.push(['branch', args]);
          return { data: { commit: { sha: head, commit: { committer: { date: '2026-09-29T00:00:00Z' } } } } };
        },
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

const skip = (github, ctx = context, core = fakeCore()) => findReasonToSkip({
  github, context: ctx, core, workflowId, jobName,
});

test('a newer queued run of a descendant commit supersedes this one', async () => {
  const github = fakeGithub({
    runs: [run(12, 102, 'pending', sha('c')), run(10, 100, 'in_progress', OURS)],
    compare: { [sha('c')]: 'ahead' },
  });
  const reason = await skip(github);
  assert.equal(reason.run.id, 12);
  assert.equal(reason.why, 'is queued to deploy');
  assert.deepEqual(github.calls.map(([name]) => name), ['branch', 'list', 'listHead', 'compare']);
  assert.deepEqual(github.calls[1][1], {
    owner: 'o', repo: 'r', workflow_id: 'deploy.yml', branch: 'main', per_page: 100,
  });
  assert.equal(github.calls[2][1].head_sha, OURS);
  assert.equal(github.calls[3][1].basehead, `${OURS}...${sha('c')}`);
});

test('the newest descendant wins when several runs are queued', async () => {
  const github = fakeGithub({
    runs: [run(11, 101, 'pending', sha('b')), run(13, 103, 'queued', sha('d'))],
    compare: { [sha('b')]: 'ahead', [sha('d')]: 'ahead' },
  });
  assert.equal((await skip(github)).run.id, 13);
});

test('older, running and non-descendant queued runs never supersede', async () => {
  const github = fakeGithub({
    runs: [
      run(9, 99, 'pending', sha('e')),
      run(15, 105, 'in_progress', sha('f')),
      run(16, 106, 'pending', sha('1')),
    ],
    compare: { [sha('e')]: 'ahead', [sha('f')]: 'ahead', [sha('1')]: 'diverged' },
  });
  assert.equal(await skip(github), null);
  assert.equal(github.calls.filter(([name]) => name === 'compare').length, 1);
});

test('a queued rerun of the same commit supersedes without a compare call', async () => {
  const github = fakeGithub({ runs: [run(12, 102, 'waiting', OURS)] });
  assert.equal((await skip(github)).run.id, 12);
  assert.deepEqual(github.calls.map(([name]) => name), ['branch', 'list', 'listHead']);
});

test('an old run never deploys over a newer commit that is already live', async () => {
  const github = fakeGithub({
    runs: [done(20, 120, sha('c'), '2026-09-29T02:00:00Z')],
    compare: { [sha('c')]: 'ahead' },
  });
  const reason = await skip(github);
  assert.equal(reason.run.id, 20);
  assert.equal(reason.why, 'already deployed');
});

test('a newer commit deploys over the live one', async () => {
  const github = fakeGithub({
    runs: [done(5, 95, sha('9'), '2026-09-29T02:00:00Z')],
    compare: { [sha('9')]: 'behind' },
  });
  assert.equal(await skip(github), null);
});

test('redeploying the live commit is allowed', async () => {
  const github = fakeGithub({ runs: [done(5, 95, OURS, '2026-09-29T02:00:00Z')] });
  assert.equal(await skip(github), null);
  assert.equal(github.calls.filter(([name]) => name === 'compare').length, 0);
});

test('the live run is the latest one whose deploy job succeeded', async () => {
  // Run 30 finished last but was itself superseded (deploy skipped), and run 31
  // failed tests: neither is live, so run 29 (older than ours) is what counts.
  const github = fakeGithub({
    runs: [
      done(31, 131, sha('d'), '2026-09-29T03:00:00Z', { conclusion: 'failure' }),
      done(30, 130, sha('c'), '2026-09-29T02:30:00Z'),
      done(29, 99, sha('9'), '2026-09-29T02:00:00Z'),
    ],
    compare: { [sha('c')]: 'ahead', [sha('9')]: 'behind' },
    deployed: { 30: 'skipped', 31: 'skipped' },
  });
  assert.equal(await skip(github), null);
  const jobLookups = github.calls.filter(([name]) => name === 'jobs').map(([, args]) => args.run_id);
  assert.deepEqual(jobLookups, [31, 30, 29]);
});

test('deploys from other refs are never skipped', async () => {
  const github = fakeGithub({ runs: [run(12, 102, 'pending', OURS)] });
  assert.equal(await skip(github, { ...context, ref: 'refs/heads/feature' }), null);
  assert.deepEqual(github.calls, []);
});

test('checkSuperseded sets the output and explains the skip', async () => {
  const core = fakeCore();
  const github = fakeGithub({ runs: [run(12, 102, 'pending', OURS)] });
  await checkSuperseded({ github, context, core, workflowId, jobName });
  assert.equal(core.out.outputs.superseded, 'true');
  assert.match(core.out.notices[0], /actions\/runs\/12 is queued to deploy aaaaaaaaaaaa/);
});

test('checkSuperseded deploys when nothing newer is queued or live', async () => {
  const core = fakeCore();
  await checkSuperseded({ github: fakeGithub(), context, core, workflowId, jobName });
  assert.equal(core.out.outputs.superseded, 'false');
  assert.deepEqual(core.out.notices, []);
});

test('checkSuperseded fails open when the API errors', async () => {
  const core = fakeCore();
  const github = fakeGithub({ listError: new Error('boom') });
  await checkSuperseded({ github, context, core, workflowId, jobName });
  assert.equal(core.out.outputs.superseded, 'false');
  assert.match(core.out.warnings[0], /deploying anyway: boom/);
});

test('a run that deployed but failed afterwards is still live', async () => {
  // `report` runs after `deploy`, so its failure must not hide what is live.
  const github = fakeGithub({
    runs: [done(20, 120, sha('c'), '2026-09-29T02:00:00Z', { conclusion: 'failure' })],
    compare: { [sha('c')]: 'ahead' },
  });
  assert.equal((await skip(github)).why, 'already deployed');
});

test('the live run is found by run number, not by when a run was last touched', async () => {
  // Re-running one job of old run 5 bumps its updated_at past run 20's.
  const github = fakeGithub({
    runs: [
      done(5, 90, sha('9'), '2026-09-29T09:00:00Z'),
      done(20, 120, sha('c'), '2026-09-29T02:00:00Z'),
    ],
    compare: { [sha('c')]: 'ahead', [sha('9')]: 'behind' },
  });
  assert.equal((await skip(github)).run.id, 20);
});

test('the rollback guard warns when no recent run deployed', async () => {
  const core = fakeCore();
  const github = fakeGithub({
    runs: [done(20, 120, sha('c'), '2026-09-29T02:00:00Z')],
    deployed: { 20: 'skipped' },
  });
  assert.equal(await skip(github, context, core), null);
  assert.match(core.out.warnings[0], /rollback guard is inactive/);
});

test('the live run is matched by the configured deploy job name', async () => {
  const runs = [done(20, 120, sha('c'), '2026-09-29T02:00:00Z', { conclusion: 'timed_out' })];
  const compare = { [sha('c')]: 'ahead' };
  const renamed = { github: fakeGithub({ runs, compare, deployJob: 'ship' }), context, core: fakeCore(), workflowId };
  assert.equal((await findReasonToSkip({ ...renamed, jobName: 'ship' })).why, 'already deployed');
  const mismatched = { ...renamed, github: fakeGithub({ runs, compare, deployJob: 'ship' }) };
  assert.equal(await findReasonToSkip({ ...mismatched, jobName: 'deploy' }), null);
});

// #769: the run list answered with a weeks-old slice, missing every recent run.
const staleSlice = [done(3, 60, sha('9'), '2026-08-01T00:00:00Z', { created_at: '2026-08-01T00:00:00Z' })];

test('a stale run list is re-read before the rollback guard trusts it', async () => {
  // Re-running old run 10 while run 20 (a newer commit) is live must still skip.
  const core = fakeCore();
  const runs = [done(20, 120, sha('c'), '2026-09-29T02:00:00Z'), ownRun];
  const github = fakeGithub({
    runs, pages: [staleSlice, runs], head: sha('c'), compare: { [sha('c')]: 'ahead' },
  });
  const reason = await skip(github, context, core);
  assert.equal(reason.run.id, 20);
  assert.equal(reason.why, 'already deployed');
  assert.equal(github.calls.filter(([name]) => name === 'list').length, 2);
  assert.ok(github.calls.some(([name]) => name === 'listSince'), 'expected a created-since query');
  assert.deepEqual(core.out.warnings, []);
});

test('a run list that stays stale leaves only main head\'s runs to decide on', async () => {
  const core = fakeCore();
  const runs = [done(20, 120, sha('c'), '2026-09-29T02:00:00Z'), ownRun];
  const github = fakeGithub({
    runs, pages: [staleSlice], head: sha('c'), compare: { [sha('c')]: 'ahead' },
  });
  // Head's own run is live, so the old run still stands aside.
  assert.equal((await skip(github, context, core)).why, 'already deployed');
  assert.match(core.out.warnings[0], /stayed stale after a retry \(newest run #60, expected at least #120\)/);

  // Head's run did not deploy: nothing trustworthy says what is live, so deploy.
  const unknown = fakeGithub({
    runs, pages: [staleSlice], head: sha('c'), compare: { [sha('c')]: 'ahead' }, deployed: { 20: 'skipped' },
  });
  const warned = fakeCore();
  assert.equal(await skip(unknown, context, warned), null);
  assert.match(warned.out.warnings[0], /stayed stale/);
  assert.match(warned.out.warnings[1], /rollback guard is inactive/);
});

test('a page that tops out below our own run is stale', async () => {
  const core = fakeCore();
  const github = fakeGithub({ pages: [staleSlice] });
  assert.equal(await skip(github, context, core), null);
  assert.match(core.out.warnings[0], /expected at least #100/);
});
