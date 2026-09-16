const assert = require('node:assert/strict');
const test = require('node:test');

const { evaluateClaudeReview } = require('./claude-review-gate.cjs');

const HEAD = 'a'.repeat(40);
const BASE = 'b'.repeat(40);
const BOT = 'github-actions[bot]';
const NO_ISSUES = '## Code review\n\nNo issues found. Checked for bugs and CLAUDE.md compliance.';

function sdkResult(overrides = {}) {
  return [{ type: 'assistant' }, {
    type: 'result', subtype: 'success', is_error: false, permission_denials: [], ...overrides,
  }];
}

function issueComment(id, body = NO_ISSUES, login = BOT) {
  return { id, body, user: { login } };
}

function reviewComment(id, commitId = HEAD, login = BOT) {
  return { id, commit_id: commitId, user: { login } };
}

function input(overrides = {}) {
  return {
    expectedHead: HEAD,
    expectedBase: BASE,
    currentPr: { state: 'open', head: HEAD, base: BASE },
    sdkMessages: sdkResult(),
    baselineIssueCommentIds: [10],
    baselineReviewCommentIds: [20],
    issueComments: [issueComment(10, 'OCR summary')],
    reviewComments: [reviewComment(20)],
    ...overrides,
  };
}

function blocked(value) {
  const decision = evaluateClaudeReview(value);
  assert.equal(decision.passed, false);
  assert.equal(typeof decision.reason, 'string');
  assert.ok(decision.reason.length > 0);
  return decision;
}

test('SDK success with nine permission denials and no new comments fails safely', () => {
  const permission_denials = Array.from({ length: 9 }, (_, index) => ({
    tool_name: index % 2 ? 'Agent' : 'Bash',
    tool_input: { command: `secret-${index}` },
  }));
  const decision = blocked(input({ sdkMessages: sdkResult({ permission_denials }) }));
  assert.equal(decision.denialCount, 9);
  assert.deepEqual(decision.deniedTools, ['Agent', 'Bash']);
  assert.doesNotMatch(decision.reason, /secret-/);
});

test('SDK success without denials but without a new review artifact fails', () => {
  assert.match(blocked(input()).reason, /artifact/i);
});

test('new exact no-findings issue comment on an unchanged PR passes', () => {
  const decision = evaluateClaudeReview(input({
    issueComments: [issueComment(10, 'OCR summary'), issueComment(11)],
  }));
  assert.deepEqual(decision, {
    passed: true,
    outcome: 'no_issues',
    newIssueCommentIds: [11],
    newReviewCommentIds: [],
  });
});

test('absent permission denial metadata is accepted', () => {
  const sdkMessages = sdkResult();
  delete sdkMessages[1].permission_denials;
  const decision = evaluateClaudeReview(input({
    sdkMessages,
    issueComments: [issueComment(10, 'OCR summary'), issueComment(11)],
  }));
  assert.equal(decision.passed, true);
  assert.equal(decision.outcome, 'no_issues');
});

test('new inline comment at the expected head passes with findings', () => {
  const decision = evaluateClaudeReview(input({
    reviewComments: [reviewComment(20), reviewComment(21)],
  }));
  assert.deepEqual(decision, {
    passed: true,
    outcome: 'findings',
    newIssueCommentIds: [],
    newReviewCommentIds: [21],
  });
});

test('new inline comment at an older head fails', () => {
  const decision = blocked(input({
    reviewComments: [reviewComment(20), reviewComment(21, 'c'.repeat(40))],
  }));
  assert.match(decision.reason, /artifact/i);
});

test('only a pre-existing matching comment fails', () => {
  blocked(input({
    issueComments: [issueComment(10)],
    reviewComments: [reviewComment(20)],
  }));
});

test('changed head, changed base, and closed PR fail even with a new comment', () => {
  const issueComments = [issueComment(10), issueComment(11)];
  for (const currentPr of [
    { state: 'open', head: 'c'.repeat(40), base: BASE },
    { state: 'open', head: HEAD, base: 'c'.repeat(40) },
    { state: 'closed', head: HEAD, base: BASE },
  ]) {
    assert.match(blocked(input({ currentPr, issueComments })).reason, /changed|open/i);
  }
});

test('missing execution data or final result fails', () => {
  for (const sdkMessages of [undefined, null, {}, [], [{ type: 'assistant' }]]) {
    blocked(input({ sdkMessages }));
  }
});

test('malformed denial metadata and unsuccessful final results fail closed', () => {
  for (const permission_denials of [null, {}, 'none', 0]) {
    assert.match(
      blocked(input({ sdkMessages: sdkResult({ permission_denials }) })).reason,
      /denial metadata/i,
    );
  }
  for (const result of [
    { subtype: 'success', is_error: true, permission_denials: [] },
    { subtype: 'error', is_error: false, permission_denials: [] },
  ]) {
    blocked(input({ sdkMessages: [{ type: 'result', ...result }] }));
  }
});

test('the final SDK result controls the decision', () => {
  const sdkMessages = [
    ...sdkResult(),
    { type: 'result', subtype: 'error', is_error: true, permission_denials: [] },
  ];
  blocked(input({ sdkMessages, issueComments: [issueComment(10), issueComment(11)] }));
});

test('untrusted authors cannot satisfy either artifact path', () => {
  const issueComments = [issueComment(10), issueComment(11, NO_ISSUES, 'attacker')];
  const reviewComments = [reviewComment(20), reviewComment(21, HEAD, 'dependabot[bot]')];
  blocked(input({ issueComments, reviewComments }));
});

test('lookalike receipts and malformed comment collections fail closed', () => {
  for (const body of [
    '## Code review\n\nNo issues found.',
    'No issues found. Checked for bugs and CLAUDE.md compliance.',
    '## code review\n\nNo issues found. Checked for bugs and CLAUDE.md compliance.',
  ]) {
    blocked(input({ issueComments: [issueComment(10), issueComment(11, body)] }));
  }
  for (const field of ['issueComments', 'reviewComments', 'baselineIssueCommentIds', 'baselineReviewCommentIds']) {
    blocked(input({ [field]: null }));
  }
});

test('missing or malformed expected revisions fail closed', () => {
  for (const field of ['expectedHead', 'expectedBase']) {
    for (const value of [undefined, null, '', 'main', 'a'.repeat(39), 'z'.repeat(40)]) {
      const currentPr = { state: 'open', head: HEAD, base: BASE, [field.slice(8).toLowerCase()]: value };
      assert.match(
        blocked(input({ [field]: value, currentPr, issueComments: [issueComment(10), issueComment(11)] })).reason,
        /revision/i,
      );
    }
  }
});
