const { test } = require('node:test');
const assert = require('node:assert/strict');
const { MARKER, encodeState, readState, chooseReview, validateRange, parseCommand } = require('./ocr-state.cjs');
const A = 'a'.repeat(40), B = 'b'.repeat(40), C = 'c'.repeat(40), P = 'd'.repeat(64);
const state = (extra = {}) => ({ version: 1, head: A, base: B, policy: P, run: '123', passed: true, claudeHead: A, ...extra });
const comment = (body = `${MARKER}\nGate passed\n${encodeState(state())}`, extra = {}) => ({ body, user: { login: 'github-actions[bot]', type: 'Bot' }, ...extra });
const current = { head: C, base: B, policy: P, forceFull: false };

test('state round trips from exactly one trusted gate', () => {
  assert.deepEqual(readState([comment()]), state());
  assert.deepEqual(readState([comment(undefined, { performed_via_github_app: { slug: 'github-actions' } })]), state());
});
test('spoofed bot, wrong app, duplicate gates, duplicate state and malformed payload fail closed', () => {
  const good = comment();
  for (const comments of [[], [comment(undefined, { user: { login: 'attacker', type: 'Bot' } })],
    [comment(undefined, { user: { login: 'github-actions[bot]', type: 'User' } })],
    [comment(undefined, { performed_via_github_app: { slug: 'other' } })], [good, good],
    [comment(good.body + '\n' + encodeState(state()))], [comment(MARKER + '\n<!-- scout-ocr-state:v1 {broken} -->')]]) {
    assert.equal(readState(comments), null);
  }
});
test('unknown fields, missing fields and invalid values cannot authorize state', () => {
  const invalid = [state({ version: 2 }), state({ extra: true }), state({ head: 'HEAD' }), state({ policy: A }),
    state({ head: [A] }), state({ base: [B] }), state({ run: '0' }), state({ run: 123 }), state({ passed: 'true' }), state({ claudeHead: C })];
  const missing = state(); delete missing.base; invalid.push(missing);
  for (const value of invalid) {
    assert.throws(() => encodeState(value));
    assert.equal(readState([comment(`${MARKER}\n<!-- scout-ocr-state:v1 ${JSON.stringify(value)} -->`)]), null);
  }
});
test('clean accepted state authorizes delta with separate Claude checkpoint', () => {
  assert.deepEqual(chooseReview(state(), current), { full: false, checkpoint: A, sourceRun: '123', claudeHead: A, reason: 'accepted checkpoint' });
  assert.equal(chooseReview(state({ claudeHead: null }), current).claudeHead, null);
});
test('missing, blocked, changed base/policy, forced full and same head reset range', () => {
  for (const [previous, options] of [[null, current], [state({ passed: false }), current], [state(), { ...current, base: C }],
    [state(), { ...current, policy: 'e'.repeat(64) }], [state(), { ...current, forceFull: true }], [state(), { ...current, head: A }]]) {
    const choice = chooseReview(previous, options);
    assert.equal(choice.full, true); assert.equal(choice.checkpoint, null); assert.equal(choice.sourceRun, null);
  }
});
const expected = { head: C, mergeBase: B, checkpoint: A, sourceRun: '123', full: false, isAncestor: () => true };
const delta = { mode: 'checkpoint', from: A, to: C, checkpointBefore: A, sourceRun: '123', ancestry: 'ancestor' };
test('native full range empty or merge-base is normalized; delta verifies accepted source', () => {
  for (const from of ['', B]) assert.equal(validateRange({ mode: 'full', from, to: C }, expected), B);
  assert.equal(validateRange(delta, expected), A);
});
test('forged ranges and run IDs, invalid modes and nonancestor fail closed', () => {
  for (const change of [{ mode: 'other' }, { from: B }, { to: A }, { checkpointBefore: B }, { sourceRun: '124' }, { ancestry: 'unknown' }]) {
    assert.throws(() => validateRange({ ...delta, ...change }, expected));
  }
  assert.throws(() => validateRange(delta, { ...expected, full: true }));
  assert.throws(() => validateRange(delta, { ...expected, isAncestor: () => false }));
  assert.throws(() => validateRange(delta, { ...expected, isAncestor: () => 'true' }));
  assert.throws(() => validateRange({ mode: 'full', from: A, to: C }, expected));
  assert.throws(() => validateRange({ mode: 'full', from: '', to: C }, { ...expected, mergeBase: 'unknown' }));
});
test('manual commands allow full and a bounded budget in either order', () => {
  assert.deepEqual(parseCommand('@ocr'), { full: false, budget: 500000 });
  assert.deepEqual(parseCommand('@OCR FULL BUDGET=750000'), { full: true, budget: 750000 });
  assert.deepEqual(parseCommand('@ocr full budget=750000'), { full: true, budget: 750000 });
  assert.deepEqual(parseCommand('@ocr budget=5000000 full\n'), { full: true, budget: 5000000 });
  for (const command of ['@ocrx', '@ocr please', '@ocr full full', '@ocr budget=1 budget=2', '@ocr budget=0', '@ocr budget=5000001', '@ocr budget=-1', '@ocr budget=1.5']) assert.throws(() => parseCommand(command));
});
