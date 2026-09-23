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

// The gate and the denial log must read the same message, or the log could
// contradict the block reason.
const finalResult = sdkMessages => sdkMessages.findLast(message => isObject(message) && message.type === 'result');

const DENIAL_INPUT_LIMIT = 200;
// GitHub shows only the first 10 warning annotations per step.
const DENIAL_LIST_LIMIT = 10;

function sanitizeDenialInput(value) {
  if (typeof value !== 'string') return '';
  const printable = value.replace(/[^\x20-\x7e]/g, '?');
  return printable.length > DENIAL_INPUT_LIMIT ? `${printable.slice(0, DENIAL_INPUT_LIMIT)}...` : printable;
}

// Run-log diagnostics only: tool inputs are model-authored from untrusted PR
// content, so they must never reach a PR comment or the gate's reason.
function describeDenials(sdkMessages) {
  if (!Array.isArray(sdkMessages)) return [];
  const result = finalResult(sdkMessages);
  if (!result || !Array.isArray(result.permission_denials)) return [];
  const denials = result.permission_denials;
  const shown = denials.length > DENIAL_LIST_LIMIT ? DENIAL_LIST_LIMIT - 1 : denials.length;
  const lines = denials.slice(0, shown).map((denial, index) => {
    const [tool] = safeToolNames([denial]);
    const toolInput = isObject(denial) && isObject(denial.tool_input) ? denial.tool_input : {};
    const field = ['command', 'file_path', 'path', 'pattern'].find(key => typeof toolInput[key] === 'string');
    const detail = field ? ` ${field}=${JSON.stringify(sanitizeDenialInput(toolInput[field]))}` : '';
    return `Denied tool call ${index + 1}: ${tool}${detail}`;
  });
  if (shown < denials.length) lines.push(`...and ${denials.length - shown} more denied tool call(s).`);
  return lines;
}

// Checks the run itself. Passing means the workflow may post Claude's comment;
// evaluateClaudeReview then checks the posted artifact and the findings.
function evaluateClaudeRun(input) {
  if (!isObject(input)) return block('Missing Claude review gate input.');
  const { expectedHead, expectedBase, expectedReceipt, currentPr, sdkMessages,
    actionOutcome, actionConclusion, structuredResult } = input;
  if (!isSha(expectedHead) || !isSha(expectedBase)) return block('Missing or malformed expected review revision.');
  if (!validReceipt(expectedReceipt) || expectedReceipt.head !== expectedHead || expectedReceipt.base !== expectedBase) {
    return block('Missing or malformed expected review receipt.');
  }
  if (!isObject(currentPr) || currentPr.state !== 'open') return block('The pull request is no longer open.');
  if (currentPr.head !== expectedHead || currentPr.base !== expectedBase) return block('The pull request changed during Claude review.');
  if (actionOutcome !== 'success' || actionConclusion !== 'success') return block('The Claude action did not finish successfully.');
  if (!Array.isArray(sdkMessages)) return block('Missing or malformed Claude execution data.');
  const result = finalResult(sdkMessages);
  if (!result) return block('Claude execution data has no final result.');
  if (result.subtype !== 'success' || result.is_error !== false) return block('Claude did not finish successfully.');
  if (Object.hasOwn(result, 'permission_denials') && !Array.isArray(result.permission_denials)) return block('Claude returned malformed permission denial metadata.');
  const denials = result.permission_denials || [];
  if (denials.length) {
    const deniedTools = safeToolNames(denials);
    return block(`Claude review had ${denials.length} permission denial(s): ${deniedTools.join(', ')}. The run log lists the denied calls.`,
      { denialCount: denials.length, deniedTools });
  }
  if (!isObject(structuredResult) || structuredResult.complete !== true
      || structuredResult.reviewed_head !== expectedHead
      || !Number.isSafeInteger(structuredResult.blocking_findings) || structuredResult.blocking_findings < 0
      || typeof structuredResult.review_comment !== 'string' || !structuredResult.review_comment.trim()) {
    return block('Claude returned incomplete or malformed structured review results.');
  }
  return { passed: true };
}

function evaluateClaudeReview(input) {
  const run = evaluateClaudeRun(input);
  if (!run.passed) return run;
  const { expectedReceipt, structuredResult, baselineIssueCommentIds, issueComments } = input;
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

const COMMENT_LIMIT = 65536; // GitHub's issue comment body limit, in characters.
// Leaves room for the receipt marker, whose JSON artifactMatches caps at 2048.
const REVIEW_TEXT_LIMIT = COMMENT_LIMIT - 4096;
const TRUNCATION_NOTE = "\n\n_The workflow truncated this review to fit GitHub's comment size limit._";

// review_comment is untrusted model output. Every workflow state marker is an
// HTML comment, so breaking each "<!--" opener stops the text from forging a
// receipt, gate state or OCR summary, or making trusted-comment lookups ambiguous.
function renderReviewComment(text, receipt) {
  if (typeof text !== 'string' || !validReceipt(receipt)) throw new Error('Invalid review comment input.');
  let body = text.replaceAll('<!--', '<!\u200b--').trim();
  if (!body) throw new Error('Empty review comment.');
  if (body.length > REVIEW_TEXT_LIMIT) {
    body = body.slice(0, REVIEW_TEXT_LIMIT - TRUNCATION_NOTE.length).replace(/[\ud800-\udbff]$/, '') + TRUNCATION_NOTE;
  }
  return `${body}\n\n${RECEIPT_PREFIX}${JSON.stringify(receipt)} -->`;
}

module.exports = { evaluateClaudeRun, evaluateClaudeReview, describeDenials, finalResult, renderReviewComment, COMMENT_LIMIT };
