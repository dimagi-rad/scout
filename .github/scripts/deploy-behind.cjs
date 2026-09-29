// Notices when production has fallen behind main with nothing on the way to fix it.
// A deploy run skips when a newer queued run covers its commit (deploy-supersede.cjs).
// If that newer run is then cancelled, or never reaches `deploy`, no run reports
// anything: every run is green and production stays behind main (G10).
// This only alerts. It never dispatches a deploy: a run is usually cancelled on
// purpose (host trouble, a held migration), and an unattended redeploy drains
// workers on a host shared with staging.
const { findLiveRun } = require('./deploy-supersede.cjs');
const { LABEL, TITLE, findOpenIssue, ensureLabel } = require('./deploy-failure-issue.cjs');

const THRESHOLD_MINUTES = 45;

const marker = (sha) => `<!-- deploy-behind:${sha} -->`;

// When main reached this commit: the push that created its first deploy run, or
// the commit time if no run exists (e.g. a `[skip ci]` push).
function behindSince({ headRuns, commitDate }) {
  const created = headRuns.map((run) => Date.parse(run.created_at)).filter(Number.isFinite);
  return created.length ? Math.min(...created) : Date.parse(commitDate);
}

async function assessDeployLag({ github, context, core, workflowId, jobName, now, thresholdMinutes }) {
  const { data: branch } = await github.rest.repos.getBranch({ ...context.repo, branch: 'main' });
  const head = branch.commit.sha;
  const { data } = await github.rest.actions.listWorkflowRuns({
    ...context.repo, workflow_id: workflowId, branch: 'main', per_page: 100,
  });
  const runs = data.workflow_runs;

  const active = runs.find((run) => run.status !== 'completed');
  if (active) return { state: 'deploying', head, run: active };

  const live = await findLiveRun({ github, context, core, runs, jobName });
  if (live && live.head_sha === head) return { state: 'current', head, live };

  const headRuns = runs
    .filter((run) => run.head_sha === head)
    .sort((a, b) => b.run_number - a.run_number);
  const since = behindSince({ headRuns, commitDate: branch.commit.commit.committer.date });
  const minutes = Math.floor((now - since) / 60000);
  const state = minutes >= thresholdMinutes ? 'behind' : 'waiting';
  return { state, head, live, latest: headRuns[0] || null, minutes };
}

function describeLag({ context, env, lag }) {
  const repoUrl = `${env.GITHUB_SERVER_URL}/${context.repo.owner}/${context.repo.repo}`;
  const liveText = lag.live
    ? `\`${lag.live.head_sha.slice(0, 12)}\` (${lag.live.html_url})`
    : 'unknown (no recent run deployed)';
  const latestText = lag.latest
    ? `Its latest deploy run ended \`${lag.latest.conclusion}\`: ${lag.latest.html_url}`
    : 'No deploy run exists for it.';
  return [
    marker(lag.head),
    `Production is behind main: main is at \`${lag.head.slice(0, 12)}\`, production runs ${liveText}.`,
    `Main has been ahead for ${lag.minutes} minutes and no production deploy is queued or running.`,
    latestText,
    '',
    `Re-run the latest deploy: run "Deploy Scout (Production)" on \`main\` from ${repoUrl}/actions/workflows/deploy.yml.`,
    'This usually means a newer run that older runs skipped for was cancelled, or failed before deploying.',
  ].join('\n');
}

async function alreadyReported({ github, context, issue, head }) {
  if ((issue.body || '').includes(marker(head))) return true;
  const comments = await github.paginate(github.rest.issues.listComments, {
    ...context.repo, issue_number: issue.number, per_page: 100,
  });
  return comments.some((comment) => (comment.body || '').includes(marker(head)));
}

async function checkDeployLag({
  github, context, core, env, workflowId, jobName,
  now = Date.now(), thresholdMinutes = THRESHOLD_MINUTES,
}) {
  const lag = await assessDeployLag({ github, context, core, workflowId, jobName, now, thresholdMinutes });
  if (lag.state !== 'behind') {
    core.info(`Production deploy state: ${lag.state} (main at ${lag.head.slice(0, 12)}).`);
    return lag;
  }
  const body = describeLag({ context, env, lag });
  const existing = await findOpenIssue({ github, context });
  if (existing) {
    if (await alreadyReported({ github, context, issue: existing, head: lag.head })) {
      core.warning(`Production is behind main; already reported on #${existing.number}.`);
      return lag;
    }
    await github.rest.issues.createComment({ ...context.repo, issue_number: existing.number, body });
    core.warning(`Production is behind main; updated #${existing.number}.`);
    return lag;
  }
  await ensureLabel({ github, context });
  const { data: issue } = await github.rest.issues.create({
    ...context.repo, title: TITLE, labels: [LABEL], body,
  });
  core.warning(`Production is behind main; opened #${issue.number}.`);
  return lag;
}

module.exports = { THRESHOLD_MINUTES, assessDeployLag, checkDeployLag };
