'use strict';

const path = require('node:path');
const crypto = require('node:crypto');
const { evaluateClaudeRun, evaluateClaudeReview, describeDenials, finalResult, renderReviewComment } = require('./claude-review-gate.cjs');
const { evaluateReview } = require('./ocr-gate.cjs');
const { MARKER, encodeState, readState, chooseReview, validateRange, nativeCheckpointMatches } = require('./ocr-state.cjs');

const policyFiles = ['.github/workflows/ocr.yml', '.github/scripts/ocr-gate.cjs',
  '.github/scripts/ocr-state.cjs', '.github/scripts/ocr-workflow.cjs', '.github/scripts/claude-review-gate.cjs'];
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

// issue_comment runs are attached to the default branch, not the PR, so the PR
// kept showing the failed `review` check from the last pull_request_target run
// even after an @ocr re-run passed (PR #501). Record the re-run's result on the
// PR head under the same name; GitHub shows the latest check run per name.
// One terminal check, never an in-progress one that a failed update could strand.
const REVIEW_CHECK = 'review';

async function recordReviewCheck({ github, context, core, env }) {
  const conclusion = { success: 'success', failure: 'failure' }[env.REVIEW_RESULT];
  if (context.eventName !== 'issue_comment' || !conclusion) {
    core.info(`No PR review check recorded for ${context.eventName} result ${env.REVIEW_RESULT}.`);
    return;
  }
  if (!/^[a-f0-9]{40}$/.test(env.REVIEW_HEAD || '')) throw new Error('Invalid review head.');
  const runUrl = `${env.GITHUB_SERVER_URL}/${env.GITHUB_REPOSITORY}/actions/runs/${context.runId}/attempts/${env.GITHUB_RUN_ATTEMPT}`;
  await github.rest.checks.create({
    ...context.repo, name: REVIEW_CHECK, head_sha: env.REVIEW_HEAD, status: 'completed', conclusion,
    external_id: String(context.runId), details_url: runUrl,
    output: {
      title: conclusion === 'success' ? 'Review re-run passed' : 'Review re-run failed',
      summary: `Result of the \`@ocr\` re-run for \`${env.REVIEW_HEAD}\`. See the PR comments for the gate and review. [Workflow run](${runUrl})`,
    },
  });
  core.info(`PR review check recorded as ${conclusion}.`);
}

async function prepareReview({ github, context, core, fs, env }) {
  await currentPR(github, context, env);
  const hash = crypto.createHash('sha256');
  for (const file of policyFiles) hash.update(file).update('\0').update(fs.readFileSync(path.join(env.GITHUB_WORKSPACE, file)));
  const policy = hash.digest('hex');
  const comments = await commentsFor(github, context, env.PR_NUMBER);
  const previous = readState(comments);
  const receipt = readClaudeReceiptState(comments);
  const claudeReusable = previous?.claudeHead && receipt?.status === 'verified'
    && receipt.run === previous.run && receipt.head === previous.claudeHead && receipt.base === previous.base;
  const accepted = previous && !claudeReusable ? { ...previous, claudeHead: null } : previous;
  let selection = chooseReview(accepted, {
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

const CLAUDE_MARKER = '<!-- scout-claude-review -->';
const trustedClaudeReceipt = comment => comment?.user?.login === 'github-actions[bot]'
  && comment.user.type === 'Bot'
  && (!comment.performed_via_github_app || comment.performed_via_github_app.slug === 'github-actions')
  && typeof comment.body === 'string' && comment.body.startsWith(CLAUDE_MARKER);

async function publishClaudeReceipt({ github, context, core, env }, status, reason, summarize = true) {
  const comments = await commentsFor(github, context, env.PR_NUMBER);
  const receipts = comments.filter(trustedClaudeReceipt);
  if (receipts.length > 1) throw new Error('Ambiguous Claude receipt state.');
  const previous = receipts[0];
  const run = String(context.runId), attempt = env.GITHUB_RUN_ATTEMPT;
  if (!/^[1-9][0-9]*$/.test(run) || !/^[1-9][0-9]*$/.test(attempt)) throw new Error('Invalid review run identity.');
  if (previous) {
    const state = parseClaudeReceiptBody(previous.body);
    if (!state) throw new Error('Malformed Claude receipt state.');
    if (BigInt(state.run) > BigInt(run) || (state.run === run && BigInt(state.attempt) > BigInt(attempt))) return false;
  }
  let nonce = null;
  try { if (status === 'verified') nonce = JSON.parse(env.CLAUDE_RECEIPT).nonce; } catch { /* Blocked preparation can have no artifact identity. */ }
  const runUrl = `${env.GITHUB_SERVER_URL}/${env.GITHUB_REPOSITORY}/actions/runs/${run}/attempts/${attempt}`;
  const body = `${CLAUDE_MARKER}\n### Claude review: ${status}\n\n${reason}\n\nReviewed commit: \`${env.REVIEW_HEAD}\` · [Workflow run](${runUrl})\n\n<!-- scout-claude-state:v1 ${JSON.stringify({ run, attempt, status, head: env.REVIEW_HEAD, base: env.REVIEW_BASE, nonce })} -->`;
  if (previous) await github.rest.issues.updateComment({ ...context.repo, comment_id: previous.id, body });
  else await github.rest.issues.createComment({ ...context.repo, issue_number: Number(env.PR_NUMBER), body });
  if (summarize) await core.summary.addRaw(body).write();
  return body;
}

async function prepareClaude({ github, context, core, fs, env }) {
  const receipt = { nonce: crypto.randomBytes(32).toString('hex'), repository: env.GITHUB_REPOSITORY,
    pr: Number(env.PR_NUMBER), run: String(context.runId), attempt: env.GITHUB_RUN_ATTEMPT,
    head: env.REVIEW_HEAD, base: env.REVIEW_BASE };
  core.setSecret(receipt.nonce);
  const receiptEnv = { ...env, CLAUDE_RECEIPT: JSON.stringify(receipt) };
  let stage = 'receipt-state publication';
  try {
    const published = await publishClaudeReceipt({ github, context, core, env: receiptEnv }, 'pending',
      'Review preparation has started; completion is not yet verified.');
    if (!published) throw new Error('A newer review attempt exists.');
    stage = 'PR recheck';
    await currentPR(github, context, env);
    stage = 'receipt-file write';
    fs.writeFileSync(path.join(env.RUNNER_TEMP, 'scout-claude-receipt.json'), JSON.stringify(receipt), { mode: 0o600 });
    stage = 'context fetch';
    // Fixed read-only methods: no broad model gh-api capability.
    const [discussion, inline, reviews] = await Promise.all([
      commentsFor(github, context, env.PR_NUMBER),
      github.paginate(github.rest.pulls.listReviewComments, { ...context.repo, pull_number: Number(env.PR_NUMBER), per_page: 100 }),
      github.paginate(github.rest.pulls.listReviews, { ...context.repo, pull_number: Number(env.PR_NUMBER), per_page: 100 }),
    ]);
    stage = 'context-file write';
    fs.writeFileSync(path.join(env.RUNNER_TEMP, 'scout-prior-review.json'), JSON.stringify({ discussion, inline, reviews }));
    core.setOutput('issue_ids', JSON.stringify(discussion.map(comment => comment.id)));
  } catch {
    const reason = 'Claude review preparation failed; no completed review was established.';
    core.warning(`Claude preparation stopped during ${stage}.`);
    try { await publishClaudeReceipt({ github, context, core, env: receiptEnv }, 'blocked', reason); }
    catch { core.warning('The blocked Claude receipt could not be published.'); }
    throw new Error(reason);
  }
}

function readClaudeReceiptState(comments) {
  const receipts = comments.filter(trustedClaudeReceipt);
  if (receipts.length !== 1) return null;
  return parseClaudeReceiptBody(receipts[0].body);
}

function parseClaudeReceiptBody(body) {
  if (typeof body !== 'string' || body.split('<!-- scout-claude-state:').length !== 2) return null;
  const match = body.match(/<!-- scout-claude-state:v1 (\{[^\r\n]*\}) -->\s*$/);
  if (!match || match[1].length > 2048) return null;
  try {
    const state = JSON.parse(match[1]);
    if (!state || Array.isArray(state) || Object.keys(state).length !== 6
        || typeof state.run !== 'string' || !/^[1-9][0-9]*$/.test(state.run)
        || typeof state.attempt !== 'string' || !/^[1-9][0-9]*$/.test(state.attempt)
        || typeof state.head !== 'string' || !/^[a-f0-9]{40}$/.test(state.head)
        || typeof state.base !== 'string' || !/^[a-f0-9]{40}$/.test(state.base)
        || !['pending', 'blocked', 'verified'].includes(state.status)
        || (state.status === 'verified'
          ? typeof state.nonce !== 'string' || !/^[a-f0-9]{64}$/.test(state.nonce)
          : state.nonce !== null)) return null;
    return state;
  } catch { return null; }
}

function currentVerifiedReceipt(comments, env, context) {
  const state = readClaudeReceiptState(comments);
  if (!state) return false;
  try {
    return state.run === String(context.runId) && state.attempt === env.GITHUB_RUN_ATTEMPT
      && state.status === 'verified' && state.head === env.REVIEW_HEAD && state.base === env.REVIEW_BASE
      && state.nonce === JSON.parse(env.CLAUDE_RECEIPT).nonce;
  } catch { return false; }
}

async function finishClaude({ github, context, core, fs, env }) {
  core.setOutput('claude_verified', 'false');
  let decision = { passed: false, reason: 'Claude review evidence could not be loaded or validated.' };
  let comments, state;
  let stage = 'evidence loading';
  try {
    const { data: pr } = await github.rest.pulls.get({ ...context.repo, pull_number: Number(env.PR_NUMBER) });
    comments = await commentsFor(github, context, env.PR_NUMBER);
    const receipt = JSON.parse(fs.readFileSync(path.join(env.RUNNER_TEMP, 'scout-claude-receipt.json'), 'utf8'));
    env = { ...env, CLAUDE_RECEIPT: JSON.stringify(receipt) };
    if (receipt.run !== String(context.runId) || receipt.attempt !== env.GITHUB_RUN_ATTEMPT
        || receipt.repository !== env.GITHUB_REPOSITORY || receipt.pr !== Number(env.PR_NUMBER)) throw new Error('Receipt identity mismatch.');
    const sdkMessages = JSON.parse(fs.readFileSync(env.EXECUTION_FILE, 'utf8'));
    for (const line of describeDenials(sdkMessages)) core.warning(line);
    // Read from the execution file, not a step output: a review-sized comment
    // passed through env can exceed the per-variable limit and stop the step.
    const structuredResult = Array.isArray(sdkMessages) ? finalResult(sdkMessages)?.structured_output : undefined;
    const review = {
      expectedHead: env.REVIEW_HEAD, expectedBase: env.REVIEW_BASE, expectedReceipt: receipt,
      currentPr: { state: pr.state, head: pr.head.sha, base: pr.base.sha },
      actionOutcome: env.CLAUDE_OUTCOME, actionConclusion: env.CLAUDE_CONCLUSION,
      sdkMessages, structuredResult,
    };
    const run = evaluateClaudeRun(review);
    const claudeState = readClaudeReceiptState(comments);
    if (!run.passed) {
      decision = run;
    } else if (claudeState?.status !== 'pending' || claudeState.run !== String(context.runId)
        || claudeState.attempt !== env.GITHUB_RUN_ATTEMPT) {
      decision = { passed: false, reason: 'A newer Claude review attempt superseded this run.' };
    } else {
      // The workflow posts so the model needs no shell write: markdown in a
      // gh pr comment argument trips the Bash permission checker (run 35856432255).
      // The gate re-reads the PR and comments and verifies the artifact as before.
      stage = 'review posting';
      const { data: posted } = await github.rest.issues.createComment({ ...context.repo,
        issue_number: Number(env.PR_NUMBER), body: renderReviewComment(structuredResult.review_comment, receipt) });
      stage = 'posted review verification';
      const { data: latestPr } = await github.rest.pulls.get({ ...context.repo, pull_number: Number(env.PR_NUMBER) });
      comments = await commentsFor(github, context, env.PR_NUMBER);
      // The listing can lag the write. The create response is GitHub's own record
      // of the comment, and it still has to pass the full artifact check.
      if (posted?.id && !comments.some(comment => String(comment.id) === String(posted.id))) {
        comments = [...comments, posted];
      }
      decision = evaluateClaudeReview({ ...review,
        currentPr: { state: latestPr.state, head: latestPr.head.sha, base: latestPr.base.sha },
        baselineIssueCommentIds: JSON.parse(env.BASELINE_ISSUE_IDS), issueComments: comments });
    }
    state = readState(comments);
    if (decision.passed && (!state || !state.passed || state.head !== env.REVIEW_HEAD || state.base !== env.REVIEW_BASE
        || state.policy !== env.POLICY || state.run !== String(context.runId))) {
      decision = { passed: false, reason: 'The accepted OCR checkpoint no longer matches this Claude review.' };
    }
  } catch {
    // Deliberately do not log raw transcript or exceptions; denials are sanitized above.
    core.warning(`Claude verification stopped during ${stage}.`);
  }
  try {
    const published = await publishClaudeReceipt({ github, context, core, env }, decision.passed ? 'verified' : 'blocked',
      decision.passed ? 'Review completed with no high or critical findings.' : decision.reason, false);
    if (!published) {
      core.setFailed('A newer Claude review attempt superseded this run.');
      return;
    }
    if (!decision.passed) {
      await core.summary.addRaw(published).write();
      core.setFailed(decision.reason);
      return;
    }
    // Re-read after receipt publication; no stale checkpoint may be advanced.
    await currentPR(github, context, env);
    comments = await commentsFor(github, context, env.PR_NUMBER);
    const latest = readState(comments);
    if (!latest || JSON.stringify(latest) !== JSON.stringify(state)
        || !currentVerifiedReceipt(comments, env, context)) throw new Error('Review checkpoint or attempt changed.');
    const comment = comments.find(trustedComment);
    const previousMarker = encodeState(state);
    if (!comment.body.includes(previousMarker)) throw new Error('Accepted OCR marker is not replaceable.');
    state.claudeHead = env.REVIEW_HEAD;
    await github.rest.issues.updateComment({ ...context.repo, comment_id: comment.id,
      body: comment.body.replace(previousMarker, encodeState(state)) });
    const persistedComments = await commentsFor(github, context, env.PR_NUMBER);
    const persisted = readState(persistedComments);
    if (!persisted || JSON.stringify(persisted) !== JSON.stringify(state)
        || !currentVerifiedReceipt(persistedComments, env, context)) {
      throw new Error('Review checkpoint persistence could not be confirmed.');
    }
    await core.summary.addRaw(published).write();
    core.setOutput('claude_verified', 'true');
    core.info('Recorded verified Claude review for future incremental follow-ups.');
  } catch {
    const reason = 'Claude review receipt or checkpoint could not be published safely.';
    core.setFailed(reason);
    try { await publishClaudeReceipt({ github, context, core, env }, 'blocked', reason); }
    catch { core.warning('The blocked Claude receipt could not be published.'); }
  }
}

module.exports = { prepareReview, finishReview, prepareClaude, finishClaude, recordReviewCheck };
