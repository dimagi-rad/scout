const EXPECTED_AUTHOR = 'github-actions[bot]';
const NO_ISSUES_HEADING = '## Code review';
const NO_ISSUES_TEXT = 'No issues found. Checked for bugs and CLAUDE.md compliance.';

function block(reason, details = {}) {
  return { passed: false, reason, ...details };
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasId(value) {
  return typeof value === 'number' || typeof value === 'string';
}

function validCommentCollection(comments) {
  return Array.isArray(comments) && comments.every(comment => isObject(comment) && hasId(comment.id));
}

function validIdCollection(ids) {
  return Array.isArray(ids) && ids.every(hasId);
}

function safeToolNames(denials) {
  const names = new Set();
  for (const denial of denials) {
    const name = isObject(denial) ? denial.tool_name : undefined;
    names.add(typeof name === 'string' && /^[A-Za-z0-9_.:-]{1,100}$/.test(name) ? name : 'unknown');
  }
  return [...names].sort();
}

function evaluateClaudeReview(input) {
  if (!isObject(input)) return block('Missing Claude review gate input.');

  const {
    expectedHead,
    expectedBase,
    currentPr,
    sdkMessages,
    baselineIssueCommentIds,
    baselineReviewCommentIds,
    issueComments,
    reviewComments,
  } = input;

  if (!/^[0-9a-f]{40}$/.test(expectedHead) || !/^[0-9a-f]{40}$/.test(expectedBase)) {
    return block('Missing or malformed expected review revision.');
  }
  if (!isObject(currentPr) || currentPr.state !== 'open') {
    return block('The pull request is no longer open.');
  }
  if (currentPr.head !== expectedHead || currentPr.base !== expectedBase) {
    return block('The pull request changed during Claude review.');
  }
  if (!Array.isArray(sdkMessages)) return block('Missing or malformed Claude execution data.');

  const result = sdkMessages.findLast(message => isObject(message) && message.type === 'result');
  if (!result) return block('Claude execution data has no final result.');
  if (result.subtype !== 'success' || result.is_error !== false) {
    return block('Claude did not finish successfully.');
  }
  if (Object.hasOwn(result, 'permission_denials') && !Array.isArray(result.permission_denials)) {
    return block('Claude returned malformed permission denial metadata.');
  }
  const denials = result.permission_denials || [];
  if (denials.length > 0) {
    const deniedTools = safeToolNames(denials);
    return block(
      `Claude review had ${denials.length} permission denial(s): ${deniedTools.join(', ')}.`,
      { denialCount: denials.length, deniedTools },
    );
  }

  if (!validIdCollection(baselineIssueCommentIds)
      || !validIdCollection(baselineReviewCommentIds)
      || !validCommentCollection(issueComments)
      || !validCommentCollection(reviewComments)) {
    return block('Missing or malformed GitHub review comment data.');
  }

  const baselineIssues = new Set(baselineIssueCommentIds.map(String));
  const baselineReviews = new Set(baselineReviewCommentIds.map(String));
  const newIssueCommentIds = issueComments
    .filter(comment => !baselineIssues.has(String(comment.id)))
    .filter(comment => comment.user?.login === EXPECTED_AUTHOR)
    .filter(comment => typeof comment.body === 'string'
      && comment.body.includes(NO_ISSUES_HEADING)
      && comment.body.includes(NO_ISSUES_TEXT))
    .map(comment => comment.id);
  const newReviewCommentIds = reviewComments
    .filter(comment => !baselineReviews.has(String(comment.id)))
    .filter(comment => comment.user?.login === EXPECTED_AUTHOR)
    .filter(comment => comment.commit_id === expectedHead)
    .map(comment => comment.id);

  if (newReviewCommentIds.length > 0) {
    return { passed: true, outcome: 'findings', newIssueCommentIds, newReviewCommentIds };
  }
  if (newIssueCommentIds.length > 0) {
    return { passed: true, outcome: 'no_issues', newIssueCommentIds, newReviewCommentIds };
  }
  return block('Claude produced no new trusted review artifact for the expected head.');
}

module.exports = { evaluateClaudeReview };
