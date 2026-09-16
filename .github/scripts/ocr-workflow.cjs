'use strict';

const path = require('node:path');
const crypto = require('node:crypto');
const { evaluateReview } = require('./ocr-gate.cjs');
const { MARKER, encodeState, readState, chooseReview, validateRange, nativeCheckpointMatches } = require('./ocr-state.cjs');

const policyFiles = ['.github/workflows/ocr.yml', '.github/scripts/ocr-gate.cjs',
  '.github/scripts/ocr-state.cjs', '.github/scripts/ocr-workflow.cjs'];
const trustedComment = (comment) => comment?.user?.login === 'github-actions[bot]'
  && comment.user?.type === 'Bot'
  && (!comment.performed_via_github_app || comment.performed_via_github_app.slug === 'github-actions')
  && typeof comment.body === 'string' && comment.body.startsWith(MARKER);

async function commentsFor(github, context, number) {
  return github.paginate(github.rest.issues.listComments, {
    ...context.repo, issue_number: Number(number), per_page: 100,
  });
}

async function currentPR(github, context, env) {
  const { data: pr } = await github.rest.pulls.get({
    ...context.repo, pull_number: Number(env.PR_NUMBER),
  });
  if (pr.state !== 'open' || pr.head.sha !== env.REVIEW_HEAD || pr.base.sha !== env.REVIEW_BASE) {
    throw new Error('The PR changed during review. Run @ocr again for the current commits.');
  }
  return pr;
}

async function prepareReview({ github, context, core, fs, env }) {
  await currentPR(github, context, env);
  const hash = crypto.createHash('sha256');
  for (const file of policyFiles) hash.update(file).update('\0').update(fs.readFileSync(path.join(env.GITHUB_WORKSPACE, file)));
  const policy = hash.digest('hex');
  const comments = await commentsFor(github, context, env.PR_NUMBER);
  const previous = readState(comments);
  let selection = chooseReview(previous, {
    head: env.REVIEW_HEAD, base: env.REVIEW_BASE, policy, forceFull: env.FORCE_FULL === 'true',
  });
  if (!selection.full && !nativeCheckpointMatches(comments, {
    head: selection.checkpoint, run: selection.sourceRun, pr: env.PR_NUMBER,
  })) {
    selection = { full: true, checkpoint: null, sourceRun: null, claudeHead: null,
      reason: 'native checkpoint does not match accepted gate' };
  }
  // The upstream action performs another checkout. Keep the validated policy from
  // the executing trusted workflow revision, not the PR comparison base or
  // whatever happens to be checked out afterward.
  const snapshot = path.join(env.RUNNER_TEMP, 'scout-ocr-policy');
  fs.mkdirSync(snapshot, { recursive: true });
  for (const file of policyFiles.filter((file) => file.endsWith('.cjs'))) {
    fs.copyFileSync(path.join(env.GITHUB_WORKSPACE, file), path.join(snapshot, path.basename(file)));
  }
  for (const [key, value] of Object.entries({
    full_review: String(selection.full), checkpoint: selection.checkpoint || '',
    source_run: selection.sourceRun || '', claude_head: selection.claudeHead || '',
    policy, reason: selection.reason,
  })) core.setOutput(key, value);
  core.info(`Review baseline: ${selection.reason}`);
}

async function finishReview({ github, context, core, fs, execFileSync, env }) {
  core.setOutput('passed', 'false');
  let decision = { passed: false, reason: 'OCR failed. See the run logs and review artifacts.' };
  let from = '';
  let mergeBase = '';
  try {
    if (env.OCR_OUTCOME !== 'success') throw new Error(decision.reason);
    mergeBase = execFileSync('git', ['merge-base', env.REVIEW_BASE, env.REVIEW_HEAD], { encoding: 'utf8' }).trim();
    from = validateRange({
      mode: env.RANGE_MODE, from: env.RANGE_FROM, to: env.RANGE_TO,
      checkpointBefore: env.CHECKPOINT_BEFORE, sourceRun: env.RANGE_SOURCE_RUN, ancestry: env.RANGE_ANCESTRY,
    }, {
      head: env.REVIEW_HEAD, mergeBase, full: env.FULL_REVIEW === 'true',
      checkpoint: env.EXPECTED_CHECKPOINT, sourceRun: env.EXPECTED_SOURCE_RUN,
      isAncestor: (ancestor, head) => {
        try { execFileSync('git', ['merge-base', '--is-ancestor', ancestor, head]); return true; }
        catch { return false; }
      },
    });
    const result = JSON.parse(fs.readFileSync('/tmp/ocr-result.json', 'utf8'));
    decision = evaluateReview(result, env.REVIEW_HEAD, from, env.POSTING_FAILED);
  } catch (error) {
    core.warning(error.message);
    from = '';
    decision = { passed: false, reason: 'OCR output could not be validated. See the run logs and review artifacts.' };
  }
  await currentPR(github, context, env);
  const comments = await commentsFor(github, context, env.PR_NUMBER);
  const previousComments = comments.filter(trustedComment);
  if (previousComments.length > 1) throw new Error('Multiple Scout gate comments: cannot safely update review state.');
  const state = {
    version: 1, head: env.REVIEW_HEAD, base: env.REVIEW_BASE, policy: env.POLICY,
    run: String(context.runId), passed: decision.passed, claudeHead: null,
  };
  const claudeIncremental = decision.passed && env.RANGE_MODE === 'checkpoint'
    && env.CLAUDE_HEAD === from;
  core.setOutput('claude_from', claudeIncremental ? from : mergeBase);
  core.setOutput('claude_mode', claudeIncremental ? 'incremental' : 'full');
  const followup = decision.passed
    ? (env.SAME_REPO === 'true' ? 'Claude review will run next.' : 'Claude follow-up is disabled for fork PRs.')
    : 'Claude review was skipped. Fix significant findings or resolve the review error, then push an update or comment `@ocr`.';
  let grouping = '';
  try {
    if (fs.readFileSync('/tmp/ocr-stderr.log', 'utf8').includes('falling back to per-file dispatch')) {
      grouping = '\n\nOCR grouping failed and used per-file tasks; repeated context may increase token usage.';
    }
  } catch { /* Logging is diagnostic, never evidence of review completeness. */ }
  const runUrl = `${env.GITHUB_SERVER_URL}/${env.GITHUB_REPOSITORY}/actions/runs/${context.runId}`;
  const range = from ? `Reviewed range: \`${from}..${env.REVIEW_HEAD}\` (${env.RANGE_MODE}).` : 'No verified review range.';
  const body = `${MARKER}\n### OCR gate: ${decision.passed ? 'passed' : 'blocked'}\n\n${decision.reason}\n\n${range}\n\n${followup}${grouping}\n\n[Workflow run](${runUrl})\n\n${encodeState(state)}`;
  if (previousComments[0]) {
    await github.rest.issues.updateComment({ ...context.repo, comment_id: previousComments[0].id, body });
  } else {
    await github.rest.issues.createComment({ ...context.repo, issue_number: Number(env.PR_NUMBER), body });
  }
  await core.summary.addRaw(body).write();
  core.setOutput('passed', String(decision.passed));
  if (!decision.passed) core.setFailed(decision.reason);
}

async function prepareClaude({ github, context, fs, env }) {
  await currentPR(github, context, env);
  // Fetch untrusted review text as data with fixed read-only SDK methods. A
  // shell prefix such as `gh api --method GET:*` also permits a later --method
  // POST flag, so the model must not receive that general API capability.
  const [discussion, inline, reviews] = await Promise.all([
    commentsFor(github, context, env.PR_NUMBER),
    github.paginate(github.rest.pulls.listReviewComments, {
      ...context.repo, pull_number: Number(env.PR_NUMBER), per_page: 100,
    }),
    github.paginate(github.rest.pulls.listReviews, {
      ...context.repo, pull_number: Number(env.PR_NUMBER), per_page: 100,
    }),
  ]);
  fs.writeFileSync(path.join(env.RUNNER_TEMP, 'scout-prior-review.json'),
    JSON.stringify({ discussion, inline, reviews }));
}

async function finishClaude({ github, context, core, env }) {
  // Action completion alone is not evidence the model finished the requested
  // review: budget exhaustion or an incomplete/blocked report cannot advance it.
  if (env.CLAUDE_OUTCOME !== 'success' || env.CLAUDE_CONCLUSION !== 'success') return;
  let result;
  try { result = JSON.parse(env.CLAUDE_RESULT); } catch { return; }
  if (result?.complete !== true || result.reviewed_head !== env.REVIEW_HEAD || result.blocking_findings !== 0) return;
  await currentPR(github, context, env);
  const comments = await commentsFor(github, context, env.PR_NUMBER);
  const state = readState(comments);
  if (!state || !state.passed || state.head !== env.REVIEW_HEAD || state.base !== env.REVIEW_BASE
      || state.policy !== env.POLICY || state.run !== String(context.runId)) return;
  const comment = comments.find(trustedComment);
  const previousMarker = encodeState(state);
  state.claudeHead = env.REVIEW_HEAD;
  // Replace only the state, retaining the gate's visible explanation.
  if (!comment.body.includes(previousMarker)) return;
  await github.rest.issues.updateComment({
    ...context.repo, comment_id: comment.id, body: comment.body.replace(previousMarker, encodeState(state)),
  });
  core.info('Recorded completed Claude review for future incremental follow-ups.');
}

module.exports = { prepareReview, finishReview, prepareClaude, finishClaude };
