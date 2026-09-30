// Notices when production has fallen behind main with nothing on the way to fix it.
// A deploy run skips when a newer queued run covers its commit (deploy-supersede.cjs).
// If that newer run is then cancelled, or never reaches `deploy`, no run reports
// anything: every run is green and production stays behind main (G10).
// This only alerts. It never dispatches a deploy: a run is usually cancelled on
// purpose (host trouble, a held migration), and an unattended redeploy drains
// workers on a host shared with staging.
const { WAITING, findLiveRun, listMainRuns } = require('./deploy-supersede.cjs');
const { findOpenIssue, fileOrComment } = require('./deploy-failure-issue.cjs');

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
  const { head, headRuns: unsorted, reliable, runs } = await listMainRuns({
    github, context, core, workflowId, branch,
  });

  // Explicit states, as in deploy-supersede.cjs: a run parked in e.g.
  // `action_required` never deploys and must not silence the alert.
  const active = runs.find((run) => WAITING.has(run.status) || run.status === 'in_progress');
  if (active) return { state: 'deploying', head, run: active };

  const live = await findLiveRun({
    github, context, core, runs, jobName, consequence: 'the commit production runs is unknown',
  });
  if (live && live.head_sha === head) return { state: 'current', head, live };
  // Only head's runs are trustworthy: whether it is behind, and by what, is unknown.
  if (!reliable) return { state: 'unknown', head };

  const headRuns = [...unsorted].sort((a, b) => b.run_number - a.run_number);
  const latest = headRuns[0] || null;
  const since = behindSince({ headRuns, commitDate: branch.commit.commit.committer.date });
  const minutes = Math.floor((now - since) / 60000);
  let state = 'behind';
  if (minutes < thresholdMinutes) state = 'waiting';
  // deploy.yml's own `report` job already filed a failed test or deploy stage.
  else if (latest && latest.conclusion === 'failure') state = 'reported';
  // A schedule has no pusher to mention: page main's author, else whoever ran its deploy.
  const notify = branch.commit.author?.login || latest?.actor?.login || null;
  return { state, head, live, latest, minutes, notify };
}

function describeLag({ context, env, workflowId, lag }) {
  const repoUrl = `${env.GITHUB_SERVER_URL}/${context.repo.owner}/${context.repo.repo}`;
  const liveText = lag.live
    ? `\`${lag.live.head_sha.slice(0, 12)}\` (${lag.live.html_url})`
    : 'unknown (no recent run deployed)';
  let latestText = 'No deploy run exists for it.';
  if (lag.latest) {
    const outcome = lag.latest.status === 'completed'
      ? `ended \`${lag.latest.conclusion}\`` : `is stuck in \`${lag.latest.status}\``;
    latestText = `Its latest deploy run ${outcome}: ${lag.latest.html_url}`;
  }
  return [
    marker(lag.head),
    `Production is behind main: main is at \`${lag.head.slice(0, 12)}\`, production runs ${liveText}.`,
    `Main has been ahead for ${lag.minutes} minutes and no production deploy is queued or running.`,
    latestText,
    ...(lag.notify ? [`cc @${lag.notify}`] : []),
    '',
    `Re-run the latest deploy: run the production deploy workflow on \`main\` from ${repoUrl}/actions/workflows/${workflowId}.`,
    'Re-running an older run ships only that run\'s commit.',
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
  if (lag.state === 'unknown') {
    core.warning(`Could not tell whether production is behind main ${lag.head.slice(0, 12)}; not filing an issue.`);
    return lag;
  }
  const quiet = () => {
    core.info(`Production deploy state: ${lag.state} (main at ${lag.head.slice(0, 12)}).`);
    return lag;
  };
  if (lag.state !== 'behind' && lag.state !== 'reported') return quiet();
  const existing = await findOpenIssue({ github, context });
  // A failed run's report is enough while its issue is open; once someone closes
  // it with production still behind, say so again.
  if (lag.state === 'reported' && existing) return quiet();
  const body = describeLag({ context, env, workflowId, lag });
  if (existing && await alreadyReported({ github, context, issue: existing, head: lag.head })) {
    core.warning(`Production is behind main; already reported on #${existing.number}.`);
    return lag;
  }
  const issue = await fileOrComment({ github, context, existing, body });
  core.warning(`Production is behind main; ${issue.opened ? 'opened' : 'updated'} #${issue.number}.`);
  return lag;
}

module.exports = { THRESHOLD_MINUTES, assessDeployLag, checkDeployLag };
