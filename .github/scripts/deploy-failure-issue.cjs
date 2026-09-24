// Keeps one open `deploy-failure` issue while production deploys are failing.
// A failed deploy went unnoticed for six days because a red run on main
// notifies nobody; an issue (and the @mention in it) does.
const LABEL = 'deploy-failure';
const TITLE = 'Production deploy is failing';

function outcome({ testResult, deployResult }) {
  if (testResult === 'failure' || deployResult === 'failure') return 'failure';
  if (testResult === 'success' && deployResult === 'success') return 'success';
  // Cancelled or skipped runs say nothing about whether main can deploy.
  return 'unknown';
}

function describe({ context, env }) {
  const runUrl = `${env.GITHUB_SERVER_URL}/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}`;
  const stage = env.TEST_RESULT === 'failure' ? 'tests' : 'deploy';
  return { runUrl, stage, sha: context.sha.slice(0, 12) };
}

async function findOpenIssue({ github, context }) {
  const { data } = await github.rest.issues.listForRepo({
    ...context.repo, state: 'open', labels: LABEL, per_page: 20,
  });
  return data.find((issue) => !issue.pull_request) || null;
}

async function ensureLabel({ github, context }) {
  try {
    await github.rest.issues.getLabel({ ...context.repo, name: LABEL });
  } catch (error) {
    if (error.status !== 404) throw error;
    await github.rest.issues.createLabel({
      ...context.repo, name: LABEL, color: 'b60205', description: 'Production deploy failed on main',
    });
  }
}

async function reportDeploy({ github, context, core, env }) {
  const result = outcome({ testResult: env.TEST_RESULT, deployResult: env.DEPLOY_RESULT });
  if (result === 'unknown') {
    core.info(`No report for tests=${env.TEST_RESULT} deploy=${env.DEPLOY_RESULT}.`);
    return result;
  }
  const { runUrl, stage, sha } = describe({ context, env });
  const existing = await findOpenIssue({ github, context });

  if (result === 'success') {
    if (existing) {
      await github.rest.issues.createComment({
        ...context.repo, issue_number: existing.number,
        body: `Resolved: production deployed \`${sha}\` successfully in ${runUrl}.`,
      });
      await github.rest.issues.update({
        ...context.repo, issue_number: existing.number, state: 'closed', state_reason: 'completed',
      });
      core.info(`Closed #${existing.number}.`);
    }
    return result;
  }

  const line = `The ${stage} stage failed for \`${sha}\` (pushed by @${context.actor}): ${runUrl}`;
  if (existing) {
    await github.rest.issues.createComment({ ...context.repo, issue_number: existing.number, body: line });
    core.warning(`Production deploy failed; updated #${existing.number}.`);
    return result;
  }
  const body = [
    line,
    '',
    'Nothing after the failed step reached production, so later merges are not live either.',
    'Check the failed step first. `no space left on device` means the shared host disk is full:',
    'see DEPLOYMENT.md, "Host disk full". A successful deploy closes this issue automatically.',
  ].join('\n');
  await ensureLabel({ github, context });
  const { data: issue } = await github.rest.issues.create({
    ...context.repo, title: TITLE, labels: [LABEL], body,
  });
  core.warning(`Production deploy failed; opened #${issue.number}.`);
  return result;
}

module.exports = { LABEL, TITLE, outcome, reportDeploy };
