// Decides whether a production deploy run should stand aside. Runs wait in one
// concurrency group and each takes ~22 minutes, so a burst of merges queued hours
// of redundant redeploys. Only deploy.yml runs are considered: a staging run for
// the other destination on the shared host is never treated as a substitute.
const WAITING = new Set(['queued', 'pending', 'waiting', 'requested']);
// Enough to reach past a burst of superseded runs, whose deploy job was skipped.
const MAX_JOB_LOOKUPS = 20;

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

// The run whose deploy job most recently succeeded is what production runs now.
// A superseded run also concludes `success`, so the deploy job itself is checked.
async function findLiveRun({ github, context, runs }) {
  const finished = runs
    .filter((run) => run.id !== context.runId && run.conclusion === 'success')
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .slice(0, MAX_JOB_LOOKUPS);
  for (const run of finished) {
    const { data } = await github.rest.actions.listJobsForWorkflowRun({
      ...context.repo, run_id: run.id, filter: 'latest', per_page: 100,
    });
    if (data.jobs.some((job) => job.name === 'deploy' && job.conclusion === 'success')) return run;
  }
  return null;
}

async function findReasonToSkip({ github, context, workflowId }) {
  if (context.ref !== 'refs/heads/main') return null;
  const { data } = await github.rest.actions.listWorkflowRuns({
    ...context.repo, workflow_id: workflowId, branch: 'main', per_page: 100,
  });
  const runs = data.workflow_runs;

  const queued = await findSupersedingRun({ github, context, runs });
  if (queued) return { run: queued, why: 'is queued to deploy' };

  // Re-running an old run would otherwise roll production back past later merges.
  // Redeploying the live commit itself stays allowed.
  const live = await findLiveRun({ github, context, runs });
  if (live && live.head_sha !== context.sha && await contains({ github, context, sha: live.head_sha })) {
    return { run: live, why: 'already deployed' };
  }
  return null;
}

async function checkSuperseded({ github, context, core, workflowId }) {
  let reason = null;
  try {
    reason = await findReasonToSkip({ github, context, workflowId });
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

module.exports = { findReasonToSkip, checkSuperseded };
