const assert = require('node:assert/strict');
const test = require('node:test');
const { evaluateClaudeReview, describeDenials } = require('./claude-review-gate.cjs');
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
// Denials stay fatal even on a complete, receipted review (PR #487): PR498 reported
// 0 findings after 25 denials, so a refused reviewer's clean verdict is not evidence.
test('SDK permission denials are blocking and only safe tool names are returned', () => {
  const denials = [{ tool_name: 'Bash', tool_input: { command: 'cat SECRET' } }, { tool_name: 'unsafe\nSECRET' }];
  const value = input(); value.sdkMessages[0].permission_denials = denials;
  const result = blocked(value);
  assert.deepEqual(result.deniedTools, ['Bash', 'unknown']);
  assert.equal(result.denialCount, 2); assert.doesNotMatch(JSON.stringify(result), /SECRET/);
  assert.equal(result.outcome, undefined);
  assert.match(result.reason, /run log lists the denied calls/);
  value.sdkMessages[0].permission_denials = denials.slice(0, 1);
  assert.equal(blocked(value).denialCount, 1);
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

test('denial diagnostics name each call with truncated, printable input', () => {
  const long = `git grep foo | head ${'x'.repeat(300)}`;
  const lines = describeDenials([{ type: 'result', permission_denials: [
    { tool_name: 'Bash', tool_input: { command: 'git grep -n foo\n::error::injected\u001b[31m' } },
    { tool_name: 'Read', tool_input: { file_path: '/tmp/x.json' } },
    { tool_name: 'Bash', tool_input: { command: long } },
    { tool_name: 'bad\nname', tool_input: 'not an object' },
  ] }]);
  assert.equal(lines.length, 4);
  assert.equal(lines[0], 'Denied tool call 1: Bash command="git grep -n foo?::error::injected?[31m"');
  assert.equal(lines[1], 'Denied tool call 2: Read file_path="/tmp/x.json"');
  assert.equal(lines[2], `Denied tool call 3: Bash command=${JSON.stringify(`${long.slice(0, 200)}...`)}`);
  assert.equal(lines[3], 'Denied tool call 4: unknown');
  for (const line of lines) assert.match(line, /^[\x20-\x7e]+$/);
});
test('denial diagnostics tolerate missing or malformed execution data', () => {
  for (const value of [undefined, null, {}, [], [{ type: 'result' }], [{ type: 'result', permission_denials: 'x' }]]) {
    assert.deepEqual(describeDenials(value), []);
  }
  assert.deepEqual(describeDenials([{ type: 'result', permission_denials: [null] }]), ['Denied tool call 1: unknown']);
});
test('denial diagnostics are capped so the annotation limit cannot hide the count', () => {
  const denial = { tool_name: 'Bash', tool_input: { command: 'x' } };
  const lines = describeDenials([{ type: 'result', permission_denials: Array(13).fill(denial) }]);
  assert.equal(lines.length, 10);
  assert.equal(lines[8], 'Denied tool call 9: Bash command="x"');
  assert.equal(lines[9], '...and 4 more denied tool call(s).');
  assert.equal(describeDenials([{ type: 'result', permission_denials: Array(10).fill(denial) }]).at(-1),
    'Denied tool call 10: Bash command="x"');
});
