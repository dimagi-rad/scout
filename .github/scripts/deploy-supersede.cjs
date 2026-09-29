// Decides whether a production deploy run should stand aside. Runs wait in one
// concurrency group and each takes ~22 minutes, so a burst of merges queued hours
// of redundant redeploys. Only deploy.yml runs are considered: a staging run for
// the other destination on the shared host is never treated as a substitute.
const WAITING = new Set(['queued', 'pending', 'waiting', 'requested']);
// Superseded runs (deploy job skipped) are among the candidates, so this must
// reach past a burst of them; each lookup is one API call.
const MAX_JOB_LOOKUPS = 50;

async function contains({ github, context, sha }) {
  if (sha === context.sha) return true;
  const { data } = await github.rest.repos.compareCommitsWithBasehead({
    ...context.repo, basehead: `${context.sha}...${sha}`,
  });
  return data.status === 'ahead';
}

// A newer queued run of a commit that contains ours will ship ours too. The
// compare call guards against a force-push that dropped our commit.
async function findSupersedingRun({ github, context, runs }) {
  const newer = runs
    .filter((run) => run.id !== context.runId && run.run_number > context.runNumber)
    .filter((run) => WAITING.has(run.status))
    .sort((a, b) => b.run_number - a.run_number);
  for (const run of newer) {
    if (await contains({ github, context, sha: run.head_sha })) return run;
  }
  return null;
}

// The newest run whose deploy job succeeded is what production runs now. Run
// numbers, not timestamps: re-running one job of an old run bumps its updated_at,
// and the concurrency group already makes deploys land in run-number order.
async function findLiveRun({ github, context, core, runs, jobName }) {
  const finished = runs
    // Any conclusion: a run can deploy and still end failed, cancelled or timed out.
    .filter((run) => run.id !== context.runId && run.status === 'completed')
    .sort((a, b) => b.run_number - a.run_number);
  for (const run of finished.slice(0, MAX_JOB_LOOKUPS)) {
    const { data } = await github.rest.actions.listJobsForWorkflowRun({
      ...context.repo, run_id: run.id, filter: 'latest', per_page: 100,
    });
    if (data.jobs.some((job) => job.name === jobName && job.conclusion === 'success')) return run;
  }
  if (finished.length) {
    core.warning(`No successful '${jobName}' job in the last ${Math.min(finished.length, MAX_JOB_LOOKUPS)} finished runs; the rollback guard is inactive for this run.`);
  }
  return null;
}

async function findReasonToSkip({ github, context, core, workflowId, jobName }) {
  if (context.ref !== 'refs/heads/main') return null;
  const { data } = await github.rest.actions.listWorkflowRuns({
    ...context.repo, workflow_id: workflowId, branch: 'main', per_page: 100,
  });
  const runs = data.workflow_runs;

  const queued = await findSupersedingRun({ github, context, runs });
  if (queued) return { run: queued, why: 'is queued to deploy' };

  // Re-running an old run would otherwise roll production back past later merges.
  // Redeploying the live commit itself stays allowed.
  const live = await findLiveRun({ github, context, core, runs, jobName });
  if (live && live.head_sha !== context.sha && await contains({ github, context, sha: live.head_sha })) {
    return { run: live, why: 'already deployed' };
  }
  return null;
}

async function checkSuperseded({ github, context, core, workflowId, jobName }) {
  let reason = null;
  try {
    reason = await findReasonToSkip({ github, context, core, workflowId, jobName });
  } catch (error) {
    // Deploying one extra time is harmless; skipping the only deploy of main is not.
    core.warning(`Could not check for a newer deploy, deploying anyway: ${error.message}`);
  }
  core.setOutput('superseded', reason ? 'true' : 'false');
  if (reason) {
    core.notice(
      `Skipping: run ${reason.run.html_url} ${reason.why} ${reason.run.head_sha.slice(0, 12)}, `
      + `which includes ${context.sha.slice(0, 12)}.`,
    );
  }
  return reason;
}

module.exports = { findLiveRun, findReasonToSkip, checkSuperseded };
