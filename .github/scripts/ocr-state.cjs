'use strict';

const MARKER = '<!-- scout-ocr-gate -->';
const STATE_PREFIX = '<!-- scout-ocr-state:';
const SHA = /^[a-f0-9]{40}$/;
const isSha = value => typeof value === 'string' && SHA.test(value);
const RUN = /^[1-9][0-9]*$/;
const FIELDS = ['version', 'head', 'base', 'policy', 'run', 'passed', 'claudeHead'];

function validState(state) {
  return state !== null && typeof state === 'object' && !Array.isArray(state)
    && Object.keys(state).length === FIELDS.length && FIELDS.every(key => Object.hasOwn(state, key))
    && state.version === 1 && isSha(state.head) && isSha(state.base)
    && typeof state.policy === 'string' && /^[a-f0-9]{64}$/.test(state.policy)
    && typeof state.run === 'string' && RUN.test(state.run) && typeof state.passed === 'boolean'
    && (state.claudeHead === null || state.claudeHead === state.head);
}

function encodeState(state) {
  if (!validState(state)) throw new Error('Invalid OCR gate state');
  return `<!-- scout-ocr-state:v1 ${JSON.stringify(state)} -->`;
}

function readState(comments) {
  if (!Array.isArray(comments)) return null;
  const gates = comments.filter(comment => comment?.user?.login === 'github-actions[bot]'
    && comment.user.type === 'Bot'
    && (!comment.performed_via_github_app || comment.performed_via_github_app.slug === 'github-actions')
    && typeof comment.body === 'string' && comment.body.startsWith(MARKER));
  if (gates.length !== 1) return null;
  const body = gates[0].body;
  if (body.split(MARKER).length !== 2 || body.split(STATE_PREFIX).length !== 2) return null;
  const match = body.match(/<!-- scout-ocr-state:v1 (\{[^\r\n]*\}) -->\s*$/);
  if (!match || match[1].length > 2048) return null;
  try {
    const state = JSON.parse(match[1]);
    return validState(state) ? state : null;
  } catch { return null; }
}

// Preflight only: native OCR still validates the full payload and configuration.
// Avoid paying for a delta that the gate would reject after a cancelled run.
function nativeCheckpointMatches(comments, { head, run, pr }) {
  const summaries = comments.filter(comment => comment?.user?.login === 'github-actions[bot]'
    && comment.user.type === 'Bot'
    && (!comment.performed_via_github_app || comment.performed_via_github_app.slug === 'github-actions')
    && typeof comment.body === 'string' && comment.body.startsWith('<!-- ocr-summary -->'));
  if (summaries.length !== 1) return false;
  const markers = [...summaries[0].body.matchAll(/<!-- ocr-checkpoint:v1 ([A-Za-z0-9+/]+={0,2}) -->/g)];
  if (markers.length !== 1 || markers[0][1].length > 4096) return false;
  try {
    const checkpoint = JSON.parse(Buffer.from(markers[0][1], 'base64').toString('utf8'));
    return checkpoint?.v === 1 && checkpoint.head === head && checkpoint.run === run
      && checkpoint.pr === Number(pr) && checkpoint.terminal_state === 'complete';
  } catch { return false; }
}

function chooseReview(previous, { head, base, policy, forceFull }) {
  let reason;
  if (forceFull) reason = 'explicit full review';
  else if (!validState(previous)) reason = 'no accepted state';
  else if (!previous.passed) reason = 'previous review blocked or incomplete';
  else if (previous.base !== base) reason = 'base changed';
  else if (previous.policy !== policy) reason = 'review policy changed';
  else if (previous.head === head) reason = 'same-head rerun';
  if (reason) return { full: true, checkpoint: null, sourceRun: null, claudeHead: null, reason };
  return { full: false, checkpoint: previous.head, sourceRun: previous.run, claudeHead: previous.claudeHead, reason: 'accepted checkpoint' };
}

function validateRange(actual, expected) {
  const { head, mergeBase, checkpoint, sourceRun, full, isAncestor } = expected;
  if (!isSha(head) || !isSha(mergeBase) || actual.to !== head) throw new Error('OCR review head or merge-base is invalid');
  if (actual.mode === 'full') {
    if (actual.from !== '' && actual.from !== mergeBase) throw new Error('OCR full review does not start at merge-base');
    return mergeBase;
  }
  if (actual.mode !== 'checkpoint' || full || !isSha(checkpoint)
    || typeof sourceRun !== 'string' || !RUN.test(sourceRun)
    || actual.from !== checkpoint || actual.checkpointBefore !== checkpoint || actual.sourceRun !== sourceRun
    || actual.ancestry !== 'ancestor' || typeof isAncestor !== 'function' || isAncestor(checkpoint, head) !== true) {
    throw new Error('OCR checkpoint range does not match the accepted gate state and ancestry');
  }
  return checkpoint;
}

function parseCommand(body) {
  const tokens = typeof body === 'string' ? body.trim().toLowerCase().split(/\s+/) : [];
  if (tokens.shift() !== '@ocr') throw new Error('Expected @ocr [full] [budget=N]');
  let full = false, budget = 500000, hasBudget = false;
  for (const token of tokens) {
    if (token === 'full' && !full) full = true;
    else if (/^budget=[1-9][0-9]*$/.test(token) && !hasBudget) {
      budget = Number(token.slice(7));
      if (!Number.isSafeInteger(budget) || budget > 5000000) throw new Error('OCR budget must be between 1 and 5000000');
      hasBudget = true;
    } else throw new Error('Expected @ocr [full] [budget=N], with no repeated options');
  }
  return { full, budget };
}

module.exports = { MARKER, encodeState, readState, chooseReview, validateRange, parseCommand, nativeCheckpointMatches };
