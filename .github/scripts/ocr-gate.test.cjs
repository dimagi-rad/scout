const assert = require('node:assert/strict');
const test = require('node:test');
const { evaluateReview } = require('./ocr-gate.cjs');

const HEAD = 'a'.repeat(40);
const BASE = 'b'.repeat(40);
const item = (id) => ({ item_id: id, path: `${id}.py` });
function report(severities = []) {
  return {
    status: 'complete',
    comments: severities.map((severity) => ({ severity, path: 'a.py', content: 'Finding', start_line: 1, end_line: 1 })),
    manifest: {
      schema_version: 'ocr.run-manifest/v1', operation: 'review', terminal_state: 'complete',
      input: { mode: 'range', resolved_head: HEAD, resolved_base: BASE },
      coverage: { selected: [item('a')], completed: [item('a')], reused: [], failed: [], waived: [] },
    },
  };
}
function evaluate(result, postingFailed = '0') {
  return evaluateReview(result, HEAD, BASE, postingFailed);
}
function blocked(result, postingFailed) {
  const decision = evaluate(result, postingFailed);
  assert.equal(decision.passed, false);
  assert.equal(typeof decision.reason, 'string');
  assert.ok(decision.reason.length > 0);
  assert.ok(Number.isInteger(decision.significant));
}

test('complete clean and low/medium reviews pass', () => {
  for (const findings of [[], ['low'], ['medium', 'low']]) {
    assert.equal(evaluate(report(findings)).passed, true);
    assert.equal(evaluate(report(findings)).significant, 0);
  }
});
test('high and critical findings block and are counted', () => {
  const result = evaluate(report(['high', 'low', 'critical', 'medium']));
  assert.equal(result.passed, false);
  assert.equal(result.significant, 2);
});
test('missing or unrecognized severity fails closed', () => {
  for (const severity of [undefined, null, '', 'info', 1, 'HIGH']) blocked(report([severity]));
});
test('missing or malformed result and comment data fail closed', () => {
  for (const value of [undefined, null, [], {}, 'invalid']) blocked(value);
  for (const comments of [undefined, null, {}, [null], ['high'], [{ severity: 'low' }]]) {
    blocked({ ...report(), comments });
  }
});
test('only successful complete manifests pass', () => {
  for (const status of [undefined, 'success', 'failed', 'skipped', 'partial']) blocked({ ...report(), status });
  for (const terminal_state of [undefined, 'partial', 'failed', 'skipped']) {
    const result = report(); result.manifest.terminal_state = terminal_state; blocked(result);
  }
  for (const manifest of [undefined, null, [], {}, { terminal_state: 'complete' }]) blocked({ ...report(), manifest });
  const result = report(); result.manifest.schema_version = 'future'; blocked(result);
});
test('requires exact expected commit SHAs for both ends of the range', () => {
  for (const field of ['resolved_head', 'resolved_base']) {
    for (const value of [undefined, '', 'main', 'c'.repeat(40)]) {
      const result = report(); result.manifest.input[field] = value; blocked(result);
    }
  }
  assert.equal(evaluateReview(report(), undefined, undefined, '0').passed, false);
});
test('posting failures and invalid publication counts block', () => {
  for (const count of ['1', 1, undefined, null, '', ' ', '-1', -1, '0.5', 0.5, 'oops', true, Infinity]) {
    const decision = evaluateReview(report(), HEAD, BASE, count);
    assert.equal(decision.passed, false, `count ${String(count)}`);
  }
  assert.equal(evaluate(report(), 0).passed, true);
});
test('coverage must be a complete disjoint partition of the selected files', () => {
  for (const field of ['selected', 'completed', 'reused', 'failed', 'waived']) {
    const result = report(); delete result.manifest.coverage[field]; blocked(result);
  }
  const mutations = [
    (c) => { c.completed = []; },
    (c) => { c.failed = [item('a')]; c.completed = []; },
    (c) => { c.reused = [item('a')]; },
    (c) => { c.completed = [item('other')]; },
    (c) => { c.selected = [item('a'), item('a')]; },
    (c) => { c.selected = []; c.completed = []; },
    (c) => { c.selected = [null]; },
  ];
  for (const mutate of mutations) { const result = report(); mutate(result.manifest.coverage); blocked(result); }
  const result = report(); result.manifest.run_failure = { classification: 'input' }; blocked(result);
});
test('reused work passes but waived selected files block', () => {
  const result = report();
  result.manifest.coverage = {
    selected: [item('a'), item('b'), item('c')], completed: [item('a')],
    reused: [item('b')], waived: [{ ...item('c'), reason: 'Excluded by configuration' }], failed: [],
  };
  blocked(result);
  result.manifest.coverage.waived = [];
  result.manifest.coverage.completed.push(item('c'));
  assert.equal(evaluate(result).passed, true);
});

test('budget stops and non-review operations block', () => {
  const result = report(); result.summary = { budget_exceeded: true }; blocked(result);
  delete result.summary; result.manifest.operation = 'scan'; blocked(result);
});

test('file-level low and medium findings pass regardless of line placement', () => {
  for (const severity of ['low', 'medium']) {
    for (const location of [{}, { start_line: 0, end_line: 0 }, { start_line: -1, end_line: 4 }]) {
      const result = report();
      result.comments = [{ content: 'File-level finding', severity, ...location }];
      assert.equal(evaluate(result).passed, true);
    }
  }
});
test('file-level high findings block and count as significant', () => {
  const result = report();
  result.comments = [{ content: 'File-level finding', severity: 'high' }];
  assert.equal(evaluate(result).passed, false);
  assert.equal(evaluate(result).significant, 1);
});
