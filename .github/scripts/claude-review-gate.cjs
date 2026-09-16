'use strict';

const RECEIPT_PREFIX = '<!-- scout-claude-artifact:v1 ';
const RECEIPT_FIELDS = ['nonce', 'repository', 'pr', 'run', 'attempt', 'head', 'base'];
const SHA = /^[0-9a-f]{40}$/;
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const isSha = value => typeof value === 'string' && SHA.test(value);
const hasId = value => (Number.isSafeInteger(value) && value > 0)
  || (typeof value === 'string' && /^[1-9][0-9]*$/.test(value));
const block = (reason, details = {}) => ({ passed: false, reason, ...details });

function validReceipt(value) {
  return isObject(value) && Object.keys(value).length === RECEIPT_FIELDS.length
    && RECEIPT_FIELDS.every(key => Object.hasOwn(value, key))
    && typeof value.nonce === 'string' && /^[0-9a-f]{64}$/.test(value.nonce)
    && typeof value.repository === 'string' && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(value.repository)
    && Number.isSafeInteger(value.pr) && value.pr > 0
    && typeof value.run === 'string' && /^[1-9][0-9]*$/.test(value.run)
    && typeof value.attempt === 'string' && /^[1-9][0-9]*$/.test(value.attempt)
    && isSha(value.head) && isSha(value.base);
}

function artifactMatches(comment, expected) {
  if (comment.user?.login !== 'github-actions[bot]' || comment.user.type !== 'Bot'
      || (comment.performed_via_github_app && comment.performed_via_github_app.slug !== 'github-actions')
      || typeof comment.body !== 'string' || comment.body.split(RECEIPT_PREFIX).length !== 2) return false;
  const match = comment.body.match(/<!-- scout-claude-artifact:v1 (\{[^\r\n]*\}) -->\s*$/);
  if (!match || match[1].length > 2048 || !comment.body.slice(0, match.index).trim()) return false;
  try {
    const receipt = JSON.parse(match[1]);
    return validReceipt(receipt) && RECEIPT_FIELDS.every(key => receipt[key] === expected[key]);
  } catch { return false; }
}

function safeToolNames(denials) {
  return [...new Set(denials.map(denial => {
    const name = isObject(denial) ? denial.tool_name : undefined;
    return typeof name === 'string' && /^[A-Za-z0-9_.:-]{1,100}$/.test(name) ? name : 'unknown';
  }))].sort();
}

function evaluateClaudeReview(input) {
  if (!isObject(input)) return block('Missing Claude review gate input.');
  const { expectedHead, expectedBase, expectedReceipt, currentPr, sdkMessages,
    actionOutcome, actionConclusion, structuredResult, baselineIssueCommentIds, issueComments } = input;
  if (!isSha(expectedHead) || !isSha(expectedBase)) return block('Missing or malformed expected review revision.');
  if (!validReceipt(expectedReceipt) || expectedReceipt.head !== expectedHead || expectedReceipt.base !== expectedBase) {
    return block('Missing or malformed expected review receipt.');
  }
  if (!isObject(currentPr) || currentPr.state !== 'open') return block('The pull request is no longer open.');
  if (currentPr.head !== expectedHead || currentPr.base !== expectedBase) return block('The pull request changed during Claude review.');
  if (actionOutcome !== 'success' || actionConclusion !== 'success') return block('The Claude action did not finish successfully.');
  if (!Array.isArray(sdkMessages)) return block('Missing or malformed Claude execution data.');
  const result = sdkMessages.findLast(message => isObject(message) && message.type === 'result');
  if (!result) return block('Claude execution data has no final result.');
  if (result.subtype !== 'success' || result.is_error !== false) return block('Claude did not finish successfully.');
  if (Object.hasOwn(result, 'permission_denials') && !Array.isArray(result.permission_denials)) return block('Claude returned malformed permission denial metadata.');
  const denials = result.permission_denials || [];
  if (denials.length) {
    const deniedTools = safeToolNames(denials);
    return block(`Claude review had ${denials.length} permission denial(s): ${deniedTools.join(', ')}.`,
      { denialCount: denials.length, deniedTools });
  }
  if (!isObject(structuredResult) || structuredResult.complete !== true
      || structuredResult.reviewed_head !== expectedHead
      || !Number.isSafeInteger(structuredResult.blocking_findings) || structuredResult.blocking_findings < 0) {
    return block('Claude returned incomplete or malformed structured review results.');
  }
  if (!Array.isArray(baselineIssueCommentIds) || !baselineIssueCommentIds.every(hasId)
      || !Array.isArray(issueComments) || !issueComments.every(comment => isObject(comment) && hasId(comment.id))) {
    return block('Missing or malformed GitHub review comment data.');
  }
  const baseline = new Set(baselineIssueCommentIds.map(String));
  const newIssueCommentIds = issueComments.filter(comment => !baseline.has(String(comment.id))
    && artifactMatches(comment, expectedReceipt)).map(comment => comment.id);
  if (!newIssueCommentIds.length) return block('Claude produced no new trusted artifact for this review run.');
  const blockingFindings = structuredResult.blocking_findings;
  if (blockingFindings) return block('Claude delivered blocking findings requiring correction.',
    { outcome: 'blocking_findings', newIssueCommentIds, blockingFindings });
  return { passed: true, outcome: 'no_blocking_findings', newIssueCommentIds, blockingFindings };
}

module.exports = { evaluateClaudeReview };
