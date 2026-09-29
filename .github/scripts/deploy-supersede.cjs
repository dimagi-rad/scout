// Lets a queued production deploy step aside when a newer queued deploy of main
// will ship a superset of its commit. Runs wait in one concurrency group and each
// takes ~22 minutes, so a burst of merges queued hours of redundant redeploys.
// Only deploy.yml runs are considered: a pending deploy for the other destination
// on the shared host is never treated as a substitute.
const WAITING = new Set(['queued', 'pending', 'waiting', 'requested']);

async function findSupersedingRun({ github, context, workflowId }) {
  if (context.ref !== 'refs/heads/main') return null;
  const { data } = await github.rest.actions.listWorkflowRuns({
    ...context.repo, workflow_id: workflowId, branch: 'main', per_page: 100,
  });
  const newer = data.workflow_runs
    .filter((run) => run.id !== context.runId && run.run_number > context.runNumber)
    .filter((run) => WAITING.has(run.status))
    .sort((a, b) => b.run_number - a.run_number);
  for (const run of newer) {
    if (run.head_sha === context.sha) return run;
    // A force-push could queue a run whose commit no longer contains ours.
    const { data: comparison } = await github.rest.repos.compareCommitsWithBasehead({
      ...context.repo, basehead: `${context.sha}...${run.head_sha}`,
    });
    if (comparison.status === 'ahead' || comparison.status === 'identical') return run;
  }
  return null;
}

async function checkSuperseded({ github, context, core, workflowId }) {
  let run = null;
  try {
    run = await findSupersedingRun({ github, context, workflowId });
  } catch (error) {
    // Deploying one extra time is harmless; skipping the only deploy of main is not.
    core.warning(`Could not check for a newer deploy, deploying anyway: ${error.message}`);
  }
  core.setOutput('superseded', run ? 'true' : 'false');
  if (run) {
    core.notice(
      `Skipping: run ${run.html_url} is queued to deploy ${run.head_sha.slice(0, 12)}, `
      + `which includes ${context.sha.slice(0, 12)}.`,
    );
  }
  return run;
}

module.exports = { WAITING, findSupersedingRun, checkSuperseded };
