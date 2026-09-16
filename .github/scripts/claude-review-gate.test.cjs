const assert = require('node:assert/strict');
const test = require('node:test');
const { evaluateClaudeReview } = require('./claude-review-gate.cjs');
const HEAD = 'a'.repeat(40), BASE = 'b'.repeat(40);
const RECEIPT = { nonce: 'c'.repeat(64), repository: 'owner/repo', pr: 42, run: '123', attempt: '1', head: HEAD, base: BASE };
const marker = receipt => `<!-- scout-claude-artifact:v1 ${JSON.stringify(receipt)} -->`;
const comment = (id = 11, receipt = RECEIPT) => ({ id, user: { login: 'github-actions[bot]', type: 'Bot' }, body: `Review completed: no blocking findings.\n\n${marker(receipt)}` });
function input(overrides = {}) {
  return { expectedHead: HEAD, expectedBase: BASE, expectedReceipt: RECEIPT,
    currentPr: { state: 'open', head: HEAD, base: BASE },
    actionOutcome: 'success', actionConclusion: 'success',
    structuredResult: { complete: true, reviewed_head: HEAD, blocking_findings: 0 },
    sdkMessages: [{ type: 'result', subtype: 'success', is_error: false, permission_denials: [] }],
    baselineIssueCommentIds: [10], issueComments: [comment()], ...overrides };
}
function blocked(value) {
  const result = evaluateClaudeReview(value);
  assert.equal(result.passed, false);
  assert.equal(typeof result.reason, 'string');
  return result;
}
test('matching new receipt, structured completion and SDK success pass', () => {
  assert.deepEqual(evaluateClaudeReview(input()), { passed: true, outcome: 'no_blocking_findings', newIssueCommentIds: [11], blockingFindings: 0 });
});
test('SDK success cannot substitute for the exact run receipt', () => {
  const alternates = { nonce: 'd'.repeat(64), repository: 'owner/other', pr: 43,
    run: '124', attempt: '2', head: 'e'.repeat(40), base: 'f'.repeat(40) };
  for (const [field, value] of Object.entries(alternates)) {
    const wrong = { ...RECEIPT, [field]: value };
    blocked(input({ issueComments: [comment(11, wrong)] }));
  }
  blocked(input({ issueComments: [] }));
  blocked(input({ issueComments: [comment(10)] }));
  blocked(input({ issueComments: [{ ...comment(), body: '## Code review\nNo issues found. Checked for bugs and CLAUDE.md compliance.' }] }));
});
test('lookalike bot, other app, duplicate markers and malformed comments block', () => {
  for (const patch of [{ user: { login: 'attacker', type: 'Bot' } }, { user: { login: 'github-actions[bot]', type: 'User' } }, { performed_via_github_app: { slug: 'other' } }, { body: `${marker(RECEIPT)}\n${marker(RECEIPT)}` }]) blocked(input({ issueComments: [{ ...comment(), ...patch }] }));
  for (const value of [null, {}, [null], [{ body: marker(RECEIPT) }]]) blocked(input({ issueComments: value }));
  for (const value of [null, {}, [''], [null]]) blocked(input({ baselineIssueCommentIds: value }));
});
test('SDK permission denials are blocking and only safe tool names are returned', () => {
  const denials = [{ tool_name: 'Bash', tool_input: 'SECRET' }, { tool_name: 'unsafe\nSECRET' }];
  const value = input(); value.sdkMessages[0].permission_denials = denials;
  const result = blocked(value);
  assert.deepEqual(result.deniedTools, ['Bash', 'unknown']);
  assert.equal(result.denialCount, 2); assert.doesNotMatch(JSON.stringify(result), /SECRET/);
});
test('missing, malformed or unsuccessful SDK data blocks', () => {
  for (const sdkMessages of [undefined, null, {}, [], [{ type: 'assistant' }], [{ type: 'result', subtype: 'error', is_error: true }]]) blocked(input({ sdkMessages }));
  for (const permission_denials of [null, {}, 'none', 0]) {
    const value = input(); value.sdkMessages[0].permission_denials = permission_denials; blocked(value);
  }
  const value = input(); value.sdkMessages.push({ type: 'result', subtype: 'error', is_error: true }); blocked(value);
});
test('SDK omission of optional empty denial metadata remains compatible', () => {
  const value = input(); delete value.sdkMessages[0].permission_denials;
  assert.equal(evaluateClaudeReview(value).passed, true);
});
test('action failures and malformed or incomplete structured results block', () => {
  for (const field of ['actionOutcome', 'actionConclusion']) for (const value of [undefined, 'failure', 'cancelled', 'skipped']) blocked(input({ [field]: value }));
  for (const structuredResult of [undefined, null, {}, [], { complete: false, reviewed_head: HEAD, blocking_findings: 0 }, { complete: true, reviewed_head: BASE, blocking_findings: 0 }]) blocked(input({ structuredResult }));
  for (const count of [-1, 1.2, '0', null, Number.MAX_SAFE_INTEGER + 1]) blocked(input({ structuredResult: { complete: true, reviewed_head: HEAD, blocking_findings: count } }));
});
test('delivered blocking findings do not pass the gate', () => {
  const result = blocked(input({ structuredResult: { complete: true, reviewed_head: HEAD, blocking_findings: 2 } }));
  assert.equal(result.outcome, 'blocking_findings'); assert.equal(result.blockingFindings, 2);
  assert.deepEqual(result.newIssueCommentIds, [11]);
});
test('changed revisions, closed PR and malformed expected receipt block', () => {
  for (const currentPr of [null, { state: 'closed', head: HEAD, base: BASE }, { state: 'open', head: BASE, base: BASE }, { state: 'open', head: HEAD, base: HEAD }]) blocked(input({ currentPr }));
  for (const expectedReceipt of [null, {}, { ...RECEIPT, nonce: '' }, { ...RECEIPT, run: '0' }, { ...RECEIPT, attempt: '-1' }, { ...RECEIPT, head: BASE }, { ...RECEIPT, extra: true }]) blocked(input({ expectedReceipt }));
  for (const expectedHead of [null, undefined, 'main', 'z'.repeat(40)]) blocked(input({ expectedHead }));
  blocked(null); blocked(undefined);
});


test('quoting the generic marker prefix is not a second receipt artifact', () => {
  const c = comment();
  c.body = 'The generic prefix `<!-- scout-claude-artifact:` is validated.\n' + c.body;
  assert.equal(evaluateClaudeReview(input({ issueComments: [c] })).passed, true);
});
