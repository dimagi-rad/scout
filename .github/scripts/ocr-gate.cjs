'use strict';

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const nonempty = (value) => typeof value === 'string' && value.trim().length > 0;
const isSha = (value) => typeof value === 'string' && /^[0-9a-f]{40}$/.test(value);

// Validate the pinned OCR v1.12.2 JSON contract. A missing field is never a clean review.
function evaluateReview(result, expectedHead, expectedBase, postingFailed) {
  let significant = 0;
  const block = (reason) => ({ passed: false, reason, significant });
  if (!isObject(result) || !Array.isArray(result.comments)) return block('Missing or malformed OCR comments.');
  for (const comment of result.comments) {
    // OCR routes findings without valid inline locations to the review summary.
    if (!isObject(comment) || !nonempty(comment.content)) {
      return block('Malformed OCR finding.');
    }
    if (!['low', 'medium', 'high', 'critical'].includes(comment.severity)) {
      return block('OCR finding has missing or unknown severity.');
    }
    if (['high', 'critical'].includes(comment.severity)) significant += 1;
  }
  if (postingFailed !== 0 && postingFailed !== '0') return block('OCR comment publication failed or its outcome is unknown.');
  const manifest = result.manifest;
  if (!isObject(manifest) || manifest.schema_version !== 'ocr.run-manifest/v1'
      || manifest.operation !== 'review') return block('Missing or unsupported OCR review manifest.');
  if (result.status !== 'complete' || manifest.terminal_state !== 'complete' || manifest.run_failure != null) {
    return block('OCR did not complete successfully.');
  }
  if (result.summary?.budget_exceeded) return block('OCR exceeded its token budget.');
  if (!isSha(expectedHead) || !isSha(expectedBase) || !isObject(manifest.input)
      || manifest.input.resolved_head !== expectedHead || manifest.input.resolved_base !== expectedBase) {
    return block('OCR reviewed a different or unknown commit range.');
  }
  const coverage = manifest.coverage;
  const sets = ['selected', 'completed', 'reused', 'failed', 'waived'];
  if (!isObject(coverage) || sets.some((key) => !Array.isArray(coverage[key]))) {
    return block('Missing or malformed OCR coverage.');
  }
  if (coverage.selected.length === 0 || coverage.failed.length > 0 || coverage.waived.length > 0) {
    return block('OCR has unreviewed selected files.');
  }
  const selected = new Map();
  for (const entry of coverage.selected) {
    if (!isObject(entry) || !nonempty(entry.item_id) || !nonempty(entry.path) || selected.has(entry.item_id)) {
      return block('Malformed or duplicate selected coverage item.');
    }
    selected.set(entry.item_id, entry.path);
  }
  const reviewed = new Set();
  for (const entry of [...coverage.completed, ...coverage.reused]) {
    if (!isObject(entry) || !selected.has(entry.item_id) || selected.get(entry.item_id) !== entry.path
        || reviewed.has(entry.item_id)) return block('OCR coverage does not match the selected files.');
    reviewed.add(entry.item_id);
  }
  if (reviewed.size !== selected.size) return block('OCR has unreviewed selected files.');
  // Files excluded before selection are outside OCR's coverage denominator.
  if (significant > 0) return block(`OCR found ${significant} high/critical finding(s).`);
  return { passed: true, reason: 'OCR completed with no high/critical findings.', significant };
}

module.exports = { evaluateReview };
